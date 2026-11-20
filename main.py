"""AstrBot plugin: real weather for the DLS "每日日程" page.

The plugin periodically fetches a real forecast for a configured location and
writes it into ``astrbot_plugin_dynamic_life_state`` (DLS), so the "每日日程"
page under AstrBot's "扩展功能" shows real weather instead of the built-in
seasonal model.
"""

from __future__ import annotations

import asyncio
import datetime
import traceback
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from typing import Any

from apscheduler.executors.asyncio import AsyncIOExecutor
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from astrbot.api import logger
from astrbot.api.all import Context, Star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.star.star_tools import StarTools

from .core.config import (
    LocationSpec,
    Settings,
    dates_for_target,
    normalize_target,
    parse_settings,
)
from .core.dls import ApplyResult, DlsBridge
from .core.location import (
    Location,
    LocationError,
    resolve_location,
)
from .core.mapping import (
    DaySlots,
    WeatherDataError,
    aggregate_day,
    condition_icon,
)
from .core.open_meteo import Forecast, WeatherFetchError, fetch_forecast
from .core.store import LocationOverrideStore, SyncStateStore

LOG_PREFIX = "[DLSWeather]"
STARTUP_DELAY_SECONDS = 5
JOB_ID = "dls_weather_daily_sync"

_TARGET_LABELS = {
    "tomorrow": "明天",
    "today": "今天",
    "both": "今天和明天",
}


class SyncError(Exception):
    """A sync failure that can be shown to the user as-is."""


@dataclass
class DateOutcome:
    """Display data for one synced date."""

    date: str
    summary: str
    icon: str
    apply: ApplyResult
    error: str = ""

    def as_state(self) -> dict[str, Any]:
        return {
            "date": self.date,
            "summary": self.summary,
            "icon": self.icon,
            "source": self.apply.source,
            "updated_state": self.apply.updated_state,
            "regenerated": self.apply.regenerated,
            "warnings": list(self.apply.warnings),
            "error": self.error or self.apply.error,
        }


@dataclass
class SyncOutcome:
    """Result of one sync run."""

    location: Location
    results: list[DateOutcome]
    trigger: str

    @property
    def ok(self) -> bool:
        return any(not item.error for item in self.results)


class DynamicLifestyleWeatherAssistPlugin(Star):
    """Fetch real weather and feed it into DLS daily schedules."""

    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        super().__init__(context)
        self.config = config
        self.data_dir = StarTools.get_data_dir()
        self.location_store = LocationOverrideStore(self.data_dir / "settings.json")
        self.state_store = SyncStateStore(self.data_dir / "state.json")
        self.bridge = DlsBridge(logger=logger)
        self._scheduler: AsyncIOScheduler | None = None
        self._sync_lock = asyncio.Lock()
        self._startup_task: asyncio.Task | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def initialize(self) -> None:
        """Start the scheduler and the optional startup sync."""
        settings = self._settings()
        self._log_settings(settings)
        self._start_scheduler(settings)
        if settings.sync_on_start:
            self._startup_task = asyncio.create_task(self._startup_sync())

    async def terminate(self) -> None:
        """Stop background tasks cleanly."""
        task = self._startup_task
        self._startup_task = None
        if task is not None and not task.done():
            task.cancel()
        self._stop_scheduler()

    # ------------------------------------------------------------------
    # Settings / logging
    # ------------------------------------------------------------------

    def _settings(self) -> Settings:
        settings = parse_settings(self.config)
        for warning in settings.warnings:
            logger.warning(f"{LOG_PREFIX} {warning}")
        return settings

    def _log_settings(self, settings: Settings) -> None:
        spec = settings.effective_location(self.location_store.snapshot())
        if not spec.has_target:
            logger.warning(
                f"{LOG_PREFIX} 还没有配置地理位置：请在插件配置里填写城市（例如 北京），"
                "或使用 /天气日程 位置 <城市> 设置。"
            )
        else:
            logger.info(f"{LOG_PREFIX} 地理位置：{spec.label()}")
        logger.info(
            f"{LOG_PREFIX} 每日同步时间：{settings.update_time}"
            f"（{settings.timezone}），目标：{_TARGET_LABELS.get(settings.target, settings.target)}"
        )
        status = self.bridge.status()
        if status.found:
            logger.info(
                f"{LOG_PREFIX} 已连接动态生活状态插件 "
                f"{self._version_label(status.version)}，将直接写入「每日日程」天气。"
            )
        else:
            logger.warning(
                f"{LOG_PREFIX} 未找到动态生活状态插件；天气会写入 "
                "weather_overrides.json，待该插件重载后生效。"
            )

    # ------------------------------------------------------------------
    # Scheduler
    # ------------------------------------------------------------------

    def _start_scheduler(self, settings: Settings) -> None:
        try:
            scheduler = AsyncIOScheduler(
                timezone=settings.tz,
                executors={"default": AsyncIOExecutor()},
                job_defaults={
                    "coalesce": True,
                    "max_instances": 1,
                    "misfire_grace_time": 600,
                },
            )
            scheduler.add_job(
                self._scheduled_sync,
                "cron",
                hour=settings.update_hour,
                minute=settings.update_minute,
                id=JOB_ID,
                replace_existing=True,
            )
            scheduler.start()
            self._scheduler = scheduler
            logger.info(
                f"{LOG_PREFIX} 调度器已启动：每天 {settings.update_time} "
                f"（{settings.timezone}）自动同步天气。"
            )
        except Exception:
            logger.error(
                f"{LOG_PREFIX} 调度器启动失败：\n{traceback.format_exc()}"
            )

    def _stop_scheduler(self) -> None:
        scheduler = self._scheduler
        self._scheduler = None
        if scheduler is None:
            return
        try:
            if scheduler.running:
                scheduler.shutdown(wait=False)
        except Exception:
            pass

    async def _scheduled_sync(self) -> None:
        try:
            outcome = await self._run_sync(trigger="schedule")
        except SyncError as exc:
            logger.warning(f"{LOG_PREFIX} 定时同步失败：{exc}")
            return
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.error(f"{LOG_PREFIX} 定时同步异常：\n{traceback.format_exc()}")
            return
        if outcome is not None:
            logger.info(
                f"{LOG_PREFIX} 定时同步完成：{self._one_line_outcome(outcome)}"
            )

    async def _startup_sync(self) -> None:
        try:
            await asyncio.sleep(STARTUP_DELAY_SECONDS)
            settings = self._settings()
            spec = settings.effective_location(self.location_store.snapshot())
            if not spec.has_target:
                return
            logger.info(f"{LOG_PREFIX} 启动同步：补齐今天和明天的天气。")
            outcome = await self._run_sync(
                trigger="startup",
                targets=("today", "tomorrow"),
            )
            if outcome is not None:
                logger.info(
                    f"{LOG_PREFIX} 启动同步完成：{self._one_line_outcome(outcome)}"
                )
        except asyncio.CancelledError:
            raise
        except SyncError as exc:
            logger.warning(f"{LOG_PREFIX} 启动同步失败：{exc}")
        except Exception:
            logger.error(f"{LOG_PREFIX} 启动同步异常：\n{traceback.format_exc()}")

    # ------------------------------------------------------------------
    # Core sync
    # ------------------------------------------------------------------

    async def _run_sync(
        self,
        *,
        trigger: str,
        targets: tuple[str, ...] | None = None,
    ) -> SyncOutcome | None:
        """Resolve the location, fetch weather, and apply it to DLS.

        Args:
            trigger: ``schedule``, ``startup``, or ``command``.
            targets: Optional target names (today/tomorrow/both) overriding the
                configured target. Startup passes both days explicitly.

        Returns:
            The sync outcome, or ``None`` when another sync is already running.

        Raises:
            SyncError: Location, network, or all-date failure.
        """
        if self._sync_lock.locked():
            logger.info(f"{LOG_PREFIX} 已有同步任务在进行，跳过本次 {trigger} 同步。")
            return None

        async with self._sync_lock:
            settings = self._settings()
            spec = settings.effective_location(self.location_store.snapshot())
            if not spec.has_target:
                raise SyncError(
                    "还没有配置地理位置。请在插件配置里填写城市，"
                    "或使用 /天气日程 位置 <城市> 设置。"
                )

            now = datetime.datetime.now(settings.tz)
            synced_at = now.strftime("%Y-%m-%d %H:%M:%S")
            try:
                location = await self._resolve(spec, settings)
                if targets is None:
                    dates = dates_for_target(settings)
                elif len(targets) == 1:
                    dates = dates_for_target(settings, targets[0])
                else:
                    dates = dates_for_target(settings, "both")
                forecast = await self._fetch_with_retry(location, settings)
            except (LocationError, WeatherFetchError, SyncError) as exc:
                message = str(exc)
                self.state_store.record_error(
                    trigger=trigger,
                    error=message,
                    synced_at=synced_at,
                    location=spec_label_dict(spec),
                )
                raise SyncError(message) from exc

            results: list[DateOutcome] = []
            for target_date in dates:
                date_text = target_date.isoformat()
                try:
                    day: DaySlots = aggregate_day(forecast, target_date)
                except WeatherDataError as exc:
                    results.append(
                        DateOutcome(
                            date=date_text,
                            summary="",
                            icon="🌡️",
                            apply=ApplyResult(date=date_text, source="error"),
                            error=str(exc),
                        )
                    )
                    continue

                regenerate = bool(
                    settings.regenerate_today
                    and trigger in {"command", "schedule"}
                    and target_date == now.date()
                )
                apply_result = await self.bridge.apply(
                    date_text,
                    day.to_dls_slots(),
                    regenerate=regenerate,
                )
                summary = apply_result.summary or day.summary
                results.append(
                    DateOutcome(
                        date=date_text,
                        summary=summary,
                        icon=condition_icon(day.afternoon or day.morning),
                        apply=apply_result,
                        error=apply_result.error,
                    )
                )

            if not any(not item.error for item in results):
                error_text = "；".join(
                    item.error for item in results if item.error
                ) or "没有可用的天气数据。"
                self.state_store.record_error(
                    trigger=trigger,
                    error=error_text,
                    synced_at=synced_at,
                    location=spec_label_dict(spec),
                )
                raise SyncError(error_text)

            self.state_store.record_success(
                trigger=trigger,
                location={
                    "name": location.name,
                    "display": location.display,
                    "coordinates": location.coordinates,
                    "country": location.country,
                    "admin1": location.admin1,
                    "source": location.source,
                },
                results=[item.as_state() for item in results],
                synced_at=synced_at,
            )
            for item in results:
                if item.error:
                    logger.warning(
                        f"{LOG_PREFIX} {item.date} 同步失败：{item.error}"
                    )
                else:
                    logger.info(
                        f"{LOG_PREFIX} {item.date} 天气已同步：{item.summary}"
                        f"（来源：{item.apply.source}）"
                    )
            return SyncOutcome(location=location, results=results, trigger=trigger)

    async def _resolve(self, spec: LocationSpec, settings: Settings) -> Location:
        return await resolve_location(
            spec.text,
            latitude=spec.latitude,
            longitude=spec.longitude,
            country_code=settings.country_code,
            timeout=settings.request_timeout,
        )

    async def _fetch_with_retry(
        self,
        location: Location,
        settings: Settings,
    ) -> Forecast:
        last_error: WeatherFetchError | None = None
        for attempt in range(1, settings.retry_count + 1):
            try:
                return await fetch_forecast(
                    location.latitude,
                    location.longitude,
                    timezone=settings.timezone,
                    days=3,
                    timeout=settings.request_timeout,
                )
            except WeatherFetchError as exc:
                last_error = exc
                logger.warning(
                    f"{LOG_PREFIX} 第 {attempt}/{settings.retry_count} 次获取天气失败：{exc}"
                )
                if attempt < settings.retry_count:
                    await asyncio.sleep(min(15, 3 * attempt))
        assert last_error is not None
        raise last_error

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    @filter.command_group("天气日程", alias={"wxs", "weathersync"})
    def weather_group(self) -> None:
        """真实天气日程同步指令组。"""

    @weather_group.command("刷新", alias={"now", "sync"})
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def weather_now(
        self,
        event: AstrMessageEvent,
        target: str = "",
    ) -> AsyncGenerator[object, None]:
        """立即拉取一次天气并写入每日日程。

        Args:
            event: 当前消息事件。
            target: 可选目标：today / tomorrow / both（支持中文）。

        Yields:
            命令结果文本。
        """
        selected: str | None = None
        raw = target.strip()
        if raw:
            normalized = normalize_target(raw, default="")
            if not normalized:
                yield event.plain_result(
                    "目标只能是 today / tomorrow / both（或 今天 / 明天 / 全部）。"
                )
                return
            selected = normalized

        yield event.plain_result("正在拉取真实天气并写入每日日程，请稍候…")
        try:
            outcome = await self._run_sync(
                trigger="command",
                targets=(selected,) if selected else None,
            )
        except SyncError as exc:
            yield event.plain_result(f"❌ 同步失败：{exc}")
            return
        except Exception:
            logger.error(f"{LOG_PREFIX} 手动同步异常：\n{traceback.format_exc()}")
            yield event.plain_result("❌ 同步失败，请查看 AstrBot 日志。")
            return
        if outcome is None:
            yield event.plain_result("已有同步任务在进行，请稍候再试。")
            return
        yield event.plain_result(
            self._format_outcome(outcome, header="🌤️ 天气日程同步完成")
        )

    @weather_group.command("状态", alias={"status"})
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def weather_status(self, event: AstrMessageEvent) -> AsyncGenerator[object, None]:
        """查看位置、调度与上次同步状态。

        Yields:
            状态文本。
        """
        settings = self._settings()
        spec = settings.effective_location(self.location_store.snapshot())
        status = self.bridge.status()
        latest = self.state_store.latest

        lines = [
            "🌤️ 天气日程助手状态",
            f"📍 位置：{self._location_line(spec, latest)}",
            f"⏰ 每日同步：{settings.update_time}（{settings.timezone}）"
            f"｜目标：{_TARGET_LABELS.get(settings.target, settings.target)}",
            f"🚀 启动同步：{'开' if settings.sync_on_start else '关'}"
            f"｜今日重排日程：{'开' if settings.regenerate_today else '关'}",
            f"🧩 每日日程插件：{status.label}"
            + (
                f"（{self._version_label(status.version)}）"
                if status.found and status.version
                else ""
            ),
        ]
        if latest:
            timestamp = latest.get("synced_at") or "未知时间"
            state_label = "成功" if latest.get("status") == "ok" else "失败"
            trigger = {
                "schedule": "定时",
                "startup": "启动",
                "command": "手动",
            }.get(str(latest.get("trigger") or ""), str(latest.get("trigger") or "未知"))
            lines.append(f"🕘 上次同步：{timestamp}｜{state_label}｜{trigger}")
            if latest.get("error"):
                lines.append(f"❌ 上次错误：{latest['error']}")
            else:
                for item in latest.get("results") or []:
                    if not isinstance(item, dict):
                        continue
                    if item.get("error"):
                        lines.append(f"❌ {item.get('date')}：{item.get('error')}")
                    else:
                        lines.append(
                            f"🗓️ {item.get('date')}：{item.get('summary') or '已写入'}"
                        )
        else:
            lines.append("🕘 上次同步：暂无记录")
        lines.append(f"⏭️ 下次运行：{self._next_run_text()}")
        if settings.warnings:
            lines.append("⚠️ 配置提醒：" + "；".join(settings.warnings))
        yield event.plain_result("\n".join(lines))

    @weather_group.command("位置", alias={"location"})
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def weather_location(
        self,
        event: AstrMessageEvent,
        location: str = "",
    ) -> AsyncGenerator[object, None]:
        """查看或修改天气位置。

        Args:
            event: 当前消息事件。
            location: 城市名或 ``lat,lon``；留空时仅查看。

        Yields:
            操作结果文本。
        """
        settings = self._settings()
        raw = location.strip()
        if not raw:
            spec = settings.effective_location(self.location_store.snapshot())
            lines = [
                f"📍 当前位置：{spec.label()}",
                f"🗂️ 来源：{self._location_source_label(spec)}",
                "修改：/天气日程 位置 北京",
                "坐标：/天气日程 位置 39.9042,116.4074",
                "恢复配置：/天气日程 位置 重置",
            ]
            yield event.plain_result("\n".join(lines))
            return

        if raw.lower() in {"重置", "reset", "清除", "clear"}:
            self.location_store.clear()
            spec = settings.effective_location(None)
            if spec.has_target:
                yield event.plain_result(
                    f"✅ 已清除聊天里设置的临时位置，恢复为插件配置：{spec.label()}"
                )
            else:
                yield event.plain_result(
                    "✅ 已清除聊天里设置的临时位置。插件配置里还没有城市，"
                    "请填写或再次使用 /天气日程 位置 <城市>。"
                )
            return

        yield event.plain_result(f"正在校验位置「{raw}」…")
        try:
            resolved = await resolve_location(
                raw,
                country_code=settings.country_code,
                timeout=settings.request_timeout,
            )
        except LocationError as exc:
            yield event.plain_result(f"❌ {exc}")
            return

        self.location_store.set_location(
            resolved.name,
            latitude=resolved.latitude,
            longitude=resolved.longitude,
            updated_at=datetime.datetime.now(settings.tz).strftime(
                "%Y-%m-%d %H:%M:%S"
            ),
        )
        yield event.plain_result(
            "✅ 位置已保存并立即生效\n"
            f"📍 {resolved.display}（{resolved.coordinates}）\n"
            "下一次同步会使用这个位置；也可以发送 /天气日程 刷新 立即拉取。"
        )

    @weather_group.command("帮助", alias={"help"})
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def weather_help(self, event: AstrMessageEvent) -> AsyncGenerator[object, None]:
        """显示指令帮助。"""
        text = (
            "🌤️ 天气日程助手\n"
            "/天气日程 刷新 [今天|明天|全部] — 立即拉取并写入每日日程\n"
            "/天气日程 状态 — 查看位置、调度和上次同步结果\n"
            "/天气日程 位置 [城市|纬度,经度|重置] — 查看或修改位置\n"
            "/天气日程 帮助 — 显示本帮助\n"
            "自动同步时间在插件配置里修改，默认每天 23:30 预取明天的天气。"
        )
        yield event.plain_result(text)

    # ------------------------------------------------------------------
    # Formatting helpers
    # ------------------------------------------------------------------

    def _format_outcome(self, outcome: SyncOutcome, *, header: str) -> str:
        lines = [
            header,
            f"📍 {outcome.location.display}｜{outcome.location.coordinates}",
        ]
        for item in outcome.results:
            if item.error:
                lines.append(f"❌ {item.date}：{item.error}")
                continue
            lines.append(f"{item.icon} {item.date}：{item.summary or '天气已写入'}")
            lines.append(f"   {self._apply_note(item.apply)}")
            for warning in item.apply.warnings:
                lines.append(f"   ⚠️ {warning}")
        return "\n".join(lines)

    @staticmethod
    def _apply_note(apply: ApplyResult) -> str:
        if apply.error:
            return f"❌ {apply.error}"
        if apply.source == "file":
            return "⚠️ 动态生活状态插件未运行，已写入 weather_overrides.json，重载后生效。"
        parts = [f"✅ 已写入「每日日程」天气（{apply.slots_written} 个时段）"]
        if apply.updated_state:
            parts.append("今日日程天气已同步")
        if apply.regenerated:
            parts.append("已按新天气重排今日日程")
        return "；".join(parts)

    @staticmethod
    def _one_line_outcome(outcome: SyncOutcome) -> str:
        parts = [f"{item.date}={item.summary or item.error}" for item in outcome.results]
        return f"{outcome.location.display} " + "；".join(parts)

    def _location_line(self, spec: LocationSpec, latest: dict[str, Any]) -> str:
        if spec.uses_coordinates:
            base = spec.label()
        elif spec.text:
            base = spec.text
        else:
            base = "未配置"
        last_location = latest.get("location") if latest else None
        if isinstance(last_location, dict) and last_location.get("display"):
            return f"{base}（上次解析：{last_location['display']}）"
        return base

    @staticmethod
    def _location_source_label(spec: LocationSpec) -> str:
        if spec.source == "override":
            return "聊天指令设置的临时位置"
        if spec.source == "config_coordinates":
            return "插件配置里的经纬度"
        return "插件配置"

    def _next_run_text(self) -> str:
        scheduler = self._scheduler
        if scheduler is None:
            return "调度器未启动"
        try:
            job = scheduler.get_job(JOB_ID)
            if job is None or job.next_run_time is None:
                return "未知"
            return job.next_run_time.strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            return "未知"

    @staticmethod
    def _version_label(version: str) -> str:
        """Return a display version, avoiding ``vv0.6.0``."""
        value = str(version or "").strip()
        if not value:
            return "未知版本"
        return value if value.lower().startswith("v") else f"v{value}"


def spec_label_dict(spec: LocationSpec) -> dict[str, Any]:
    """Return a JSON-friendly location snapshot for the state file."""
    return {
        "label": spec.label(),
        "latitude": spec.latitude,
        "longitude": spec.longitude,
        "source": spec.source,
    }