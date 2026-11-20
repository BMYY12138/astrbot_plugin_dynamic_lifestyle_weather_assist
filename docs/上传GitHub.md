# 上传到 GitHub

这个仓库已经按可直接上传的结构整理好了。下面以
`https://github.com/fish/astrbot_plugin_dynamic_lifestyle_weather_assist`
为例；如果你的用户名或仓库名不同，把 `fish` 和仓库名替换掉即可。

> GitHub 仓库名不能包含空格。插件目录名、`metadata.yaml` 里的 `name` 必须一致且是合法的
> Python 标识符，所以这里使用 `astrbot_plugin_dynamic_lifestyle_weather_assist`。
> 想展示成 “Dynamic Lifestyle Plugin Weather Assist”，可以写在仓库描述和
> `metadata.yaml` 的 `display_name` / `desc` 里。

## 1. 先检查本地

```powershell
cd D:\DSH\astrbot_plugin_dynamic_lifestyle_weather_assist
python -m unittest discover -s tests -t . -v
```

确认没有把本机的 `settings.json`、`state.json`、`__pycache__` 带进仓库
（`.gitignore` 已经处理）。

## 2. 初始化并提交

```powershell
git init
git add .
git commit -m "feat: initial release of DLS weather assistant"
git branch -M main
git remote add origin https://github.com/fish/astrbot_plugin_dynamic_lifestyle_weather_assist.git
git push -u origin main
```

如果远程仓库还没创建，先在 GitHub 网页上新建一个空仓库（不要勾选 README /
.gitignore / LICENSE，避免和本地冲突），再执行 `git remote add` 和 `push`。

## 3. 改掉占位信息（如需要）

如果 GitHub 用户名不是 `fish`，修改：

1. `metadata.yaml` 的 `author` 和 `repo`；
2. `README.md` 顶部的仓库链接和 Releases 链接；
3. `core/location.py`、`core/open_meteo.py` 里的 User-Agent 中的 GitHub 地址（可选）；
4. `LICENSE` 的版权人。

然后：

```powershell
git add .
git commit -m "chore: update repository metadata"
git push
```

## 4. 打一个 Release

AstrBot 面板的“从文件安装”需要 zip。用 `git archive` 生成干净包：

```powershell
New-Item -ItemType Directory -Force release | Out-Null
git archive --format=zip `
  --prefix=astrbot_plugin_dynamic_lifestyle_weather_assist/ `
  -o release/astrbot_plugin_dynamic_lifestyle_weather_assist.zip HEAD
```

然后在 GitHub：

1. 仓库 → **Releases** → **Draft a new release**；
2. Tag 填 `v1.0.0`；
3. 上传 `release/astrbot_plugin_dynamic_lifestyle_weather_assist.zip`；
4. 发布。

## 5. 发布前检查清单

- [ ] `python -m unittest discover -s tests -t .` 全部通过
- [ ] `metadata.yaml` 的 `name` 与插件目录名一致
- [ ] `metadata.yaml` 的 `astrbot_version` 与实际测试版本一致
- [ ] `README.md` 的安装、配置、指令与代码一致
- [ ] 没有提交 `state.json`、`settings.json`、`weather_overrides.json`
- [ ] Release zip 解压后第一层就是插件目录
- [ ] 在 AstrBot 里从 zip 安装一次，确认能加载

## 6. 之后每次发版

1. 修改 `metadata.yaml` 的 `version`；
2. 更新 `CHANGELOG.md`；
3. 跑测试；
4. 提交、打 tag、上传新的 zip。

```powershell
git add .
git commit -m "release: v1.0.1"
git tag v1.0.1
git push
git push --tags
```