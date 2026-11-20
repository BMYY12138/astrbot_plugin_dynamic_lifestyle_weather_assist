# 设计说明

本文记录「动态生活天气助手」的实现思路、与 DLS 的对接契约，以及一些容易踩坑的地方。

## 目标与非目标

### 目标

1. 按用户配置的城市/坐标，每天定时获取真实天气。
2. 把天气转换成 DLS 能直接使用的 8 个自然时段。
3. 让「扩展功能 → 每日日程」显示真实天气，并让 DLS 生成日程时自然参考真实天气。
4. 尽量不侵入 DLS：不修改 DLS 的代码，不要求 DLS 新增 API。
5. 不依赖天气 API Key，默认方案开箱即用。

### 非目标

- 不做逐小时级别的日程重排（DLS 本身按自然时段组织）。
- 不做多城市/多会话分别配置（一个 AstrBot 实例一个位置，够用且简单）。
- 不替代 DLS 的天气编辑器；手动编辑仍以 DLS 页面为准。
- 不保证极端天气预警、空气质量等扩展数据。

## 时序

```text
        AstrBot 事件循环
               │
 23:30 ┌───────▼─────────────────────────────────────────────┐
       │ WeatherSync._scheduled_sync                        │
       │  ├─ parse_settings(config)                          │
       │  ├─ effective_location(config, override)             │
       │  ├─ resolve_location()  ← Open-Meteo geocoding       │
       │  ├─ fetch_forecast()    ← Open-Meteo forecast        │
       │  ├─ aggregate_day()     ← 24h → 8 natural slots      │
       │  └─ DlsBridge.apply()                                │
       │       ├─ 找到 DLS 实例 → weather_store.set()          │
       │       │    └─ 若是今天 → data_mgr.set(replace(...))   │
       │       └─ 找不到 → 写 weather_overrides.json           │
       └──────────────────────────────────────────────────────┘
               │
 00:00 ┌───────▼─────────────────────────────────────────────┐
       │ DLS: _daily_generate()                              │
       │  └─ Generator.generate(今天)                         │
       │       └─ WeatherOverrideStore.get(今天) → 真实天气     │
       └──────────────────────────────────────────────────────┘
```

关键点：**在 DLS 生成“今天”的日程之前写入“今天”的天气覆写**。
因为 DLS 的生成时间是 00:00，所以本插件默认在 23:30 拉取“明天”的天气。

## 与 DLS 的对接契约

### 发现插件

AstrBot 的 `astrbot.core.star.star.star_registry` 保存了每个已加载插件的
`StarMetadata`，其中：

- `metadata.name` / `metadata.root_dir_name`：插件目录名；
- `metadata.star_cls`：插件类实例；
- `metadata.activated`：是否启用。

`DlsBridge.find_plugin()` 按名字匹配 `astrbot_plugin_dynamic_life_state`，
并要求实例具备：

- `weather_store`（`WeatherOverrideStore`）
- `data_mgr`（`DataManager`）
- `generator`（可选，用于重排）

任何一项不满足都视为“未连接”，走文件回退，不会让主流程崩掉。

### 写入天气

DLS 的 `WeatherOverrideStore.set(date, slots)`：

1. 校验并规范化 8 个时段；
2. 写入 `weather_overrides.json`；
3. 返回组装好的 `DayWeather`（含 `summary`、`season`）。

`slots` 必须是 DLS 的 `WeatherSlot` 实例，不是 dict。因此：

1. 从 `type(weather_store).__module__` 推导 DLS 的 `core.state` 模块；
2. `importlib.import_module(...)` 取到 `WeatherSlot`；
3. `WeatherSlot(**slot_dict)` 构造后传入。

这样做比写死 import 路径更稳：插件目录名或加载方式变化时，仍能根据实际模块路径找到类型。

### 更新今日状态

DLS 的天气覆写和“状态里的天气”是两份数据：

- `weather_overrides.json`：生成日程时的天气来源；
- `LifeState.weather`：已经生成的今日日程里保存的天气快照。

只写覆写文件，不会改变已经生成的今日日程页面上的天气。因此当日期是今天时，
再执行：

```python
replace(
    state,
    weather_summary=day.summary,
    weather=list(day.slots),
    season=day.season.label,
)
```

并通过 `data_mgr.set()` 落盘。和 DLS 自带 WebUI 的 `save_weather` 逻辑一致。

### 文件回退

DLS 未加载时（例如先装天气助手、后装 DLS），直接写：

```text
data/plugin_data/astrbot_plugin_dynamic_life_state/weather_overrides.json
```

限制：DLS 的 `WeatherOverrideStore` 在初始化时把文件读进内存，之后 `get()` 只读内存。
所以外部写文件后，**需要重载 DLS 才能生效**。插件会在结果里明确提示这一点。

### 可选重排

`regenerate_today=true` 时，对“今天”的 `command` / `schedule` 同步会调用：

```python
await dls.generator.generate(date, force=True)
```

启动同步（`startup`）永不触发重排，避免每次重启都烧 token。

## 天气映射

### 时段

DLS 的 `NATURAL_SLOT_RANGES` 是固定 8 段、左闭右开：

```text
凌晨 00:00-06:00   早上 06:00-09:00   上午 09:00-12:00
中午 12:00-14:00   下午 14:00-18:00   傍晚 18:00-20:00
晚上 20:00-23:00   深夜 23:00-24:00
```

本插件逐段聚合：

- 气温：凌晨/深夜取最低，中午/下午取最高，其余取平均。这样 DLS 页面上的
  `low~high` 概览能反映当天的真实最低/最高，而不是 8 个平均值的范围。
- 天气现象：段内只要出现降水，就优先降水；多种降水取最重的一种；否则取出现次数最多的晴/多云/阴。
- 降水概率：段内最大值。
- 风力：段内平均风速 → 蒲福风级；风向按速度加权做圆形平均，避免 0°/360° 附近直接平均出错。

### WMO

Open-Meteo 使用 WMO 天气代码。`core/mapping.py` 里维护完整映射表，
未知代码回退到“阴”，保证不会因为新代码导致整个日期失败。

### 时间与时区

请求 Open-Meteo 时传入 `timezone=<配置时区>`，API 返回的 `time` 就是该时区的本地时间，
不带偏移量。代码用 `datetime.fromisoformat` 解析后直接按 `date()`/`hour` 分组。

DLS 固定使用 `Asia/Shanghai`，所以配置默认也是 `Asia/Shanghai`。
如果用户改时区，而 DLS 仍是上海时区，可能出现“气象日期”和“日程日期”错位；
README 里已提示通常不要改。

## 调度与容错

- APScheduler `AsyncIOScheduler`，cron 表达式由 `update_time` 生成。
- `coalesce=True`、`max_instances=1`、`misfire_grace_time=600`。
- 网络请求最多重试 `retry_count` 次，间隔逐步退避（3s、6s、9s…，单次不超过 15s）。
- `asyncio.Lock` 保证定时、启动、手动同步不会并发写 DLS。
- 启动同步延迟 5 秒，给其他插件（尤其是 DLS）留出初始化时间。
- 每一天独立处理：某一天映射失败不会阻止另一天写入；只有全部失败才整体报错。
- 同步结果写入 `state.json`（最多 10 条），`/天气日程 状态` 直接读它。

## 安全与隐私

- 不包含任何密钥；Open-Meteo 免费且无需注册。
- 只把配置的经纬度发给 Open-Meteo，不发送用户聊天内容、人格或其他 AstrBot 数据。
- 位置和状态文件保存在 AstrBot 本地插件数据目录，`.gitignore` 已排除。
- 指令均要求 AstrBot 管理员权限，避免普通群成员随意改位置或触发重排。

## 兼容性与已知限制

- 开发环境：AstrBot 4.29.4、DLS v0.6.0、Python 3.12、Windows。
- 对 DLS 的依赖是“运行时属性”而不是 import：DLS 升级后只要 `weather_store.set`、
  `data_mgr.get_current/set`、`generator.generate` 的语义不变，插件就能继续工作。
- 若 DLS 把 `WeatherSlot` 改成非 dataclass 或改名，本插件会写入失败并在日志/指令里报错，
  不会破坏 DLS 已有数据。
- 预报范围固定 3 天，只用于今天/明天，足够覆盖默认调度。
- 不支持按会话设置不同城市；如需要，可以把 `LocationOverrideStore` 扩展为按 UMO 存储。
- 天气是预报，不是实况；Open-Meteo 更新频率和精度受数据源限制。

## 测试策略

- 纯函数（配置解析、坐标解析、WMO 映射、8 时段聚合、风力换算）用标准库 `unittest` 覆盖，
  不依赖 AstrBot、不联网。
- DLS 桥接测试覆盖 slot 清洗、文件回退的合并与过期清理。
- 联网取数作为手动验证项：`resolve_location("北京")` + `fetch_forecast(...)` + `aggregate_day(...)`。
- AstrBot 内的端到端验证：安装后看 `backend.log` 的 `[DLSWeather]` 日志，
  再通过面板「每日日程」或插件扩展 API 检查 `weather_source=override`。