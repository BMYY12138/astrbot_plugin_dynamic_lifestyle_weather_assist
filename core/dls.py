"""Bridge between the weather assistant and ``astrbot_plugin_dynamic_life_state``.

The DLS plugin owns the "每日日程" page. Its weather data lives in two
places:

* ``weather_overrides.json`` in the DLS plugin data directory, keyed by date.
* The in-memory ``WeatherOverrideStore`` loaded when DLS starts.

Writing the JSON file alone is not enough while DLS is running, so this bridge
first tries to call the live plugin instance through AstrBot's star registry.
If DLS is not loaded, it falls back to writing the override file, which takes
effect after DLS is reloaded.
"""

from __future__ import annotations

import datetime
import importlib
import os
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .store import read_json, write_json

DLS_PLUGIN_NAME = "astrbot_plugin_dynamic_life_state"
CHINA_TIMEZONE = ZoneInfo("Asia/Shanghai")

_SLOT_KEYS = (
    "time",
    "condition",
    "temperature",
    "precipitation",
    "precipitation_probability",
    "wind",
)


class DlsBridgeError(Exception):
    """Raised when DLS weather cannot be applied."""


@dataclass(frozen=True)
class ApplyResult:
    """Result of applying one day of weather to DLS."""

    date: str
    source: str  # "dls" | "file" | "error"
    summary: str = ""
    slots_written: int = 0
    updated_state: bool = False
    regenerated: bool = False
    warnings: tuple[str, ...] = ()
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error


@dataclass(frozen=True)
class DlsStatus:
    """Connection status for the DLS plugin."""

    found: bool
    version: str = ""
    root_dir_name: str = ""
    data_dir: str = ""

    @property
    def label(self) -> str:
        return "已连接" if self.found else "未找到"


class DlsBridge:
    """Locate DLS and write weather into its store/state."""

    def __init__(self, plugin_name: str = DLS_PLUGIN_NAME, logger: Any = None) -> None:
        self.plugin_name = plugin_name
        self.logger = logger

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    def _log(self, level: str, message: str) -> None:
        if self.logger is None:
            return
        try:
            getattr(self.logger, level)(message)
        except Exception:
            pass

    @staticmethod
    def _iter_metadata() -> list[Any]:
        try:
            from astrbot.core.star.star import star_registry

            if star_registry:
                return list(star_registry)
        except Exception:
            pass
        try:
            from astrbot.core.star.star import star_map

            return list(star_map.values())
        except Exception:
            return []

    def _matching_metadata(self) -> Any | None:
        for meta in self._iter_metadata():
            name = getattr(meta, "name", None)
            root = getattr(meta, "root_dir_name", None)
            if name != self.plugin_name and root != self.plugin_name:
                continue
            return meta
        return None

    def find_plugin(self) -> Any | None:
        """Return the live DLS plugin instance, or ``None``."""
        meta = self._matching_metadata()
        if meta is None:
            return None
        if not getattr(meta, "activated", True):
            return None
        instance = getattr(meta, "star_cls", None)
        if instance is None:
            return None
        store = getattr(instance, "weather_store", None)
        if store is None:
            return None
        if getattr(instance, "data_mgr", None) is None:
            return None
        return instance

    def status(self) -> DlsStatus:
        """Return whether DLS is reachable."""
        meta = self._matching_metadata()
        instance = self.find_plugin()
        version = str(getattr(meta, "version", "") or "") if meta else ""
        root = str(getattr(meta, "root_dir_name", "") or "") if meta else ""
        data_dir = ""
        if instance is not None:
            data_dir = str(getattr(instance, "data_dir", "") or "")
        return DlsStatus(
            found=instance is not None,
            version=version,
            root_dir_name=root,
            data_dir=data_dir,
        )

    # ------------------------------------------------------------------
    # Apply weather
    # ------------------------------------------------------------------

    async def apply(
        self,
        date: str,
        slots: list[dict],
        *,
        regenerate: bool = False,
    ) -> ApplyResult:
        """Write one day of weather into DLS.

        Args:
            date: Target date in ``YYYY-MM-DD`` form.
            slots: Eight DLS-compatible weather dictionaries.
            regenerate: When the date is today, ask DLS to regenerate the
                daily schedule after updating the weather. This consumes LLM
                tokens and is therefore opt-in.

        Returns:
            An :class:`ApplyResult`; failures are reported instead of raised so
            one bad date does not abort the whole sync.
        """
        try:
            parsed_date = datetime.date.fromisoformat(date)
        except ValueError:
            return ApplyResult(date=date, source="error", error="日期格式错误。")

        clean_slots = [self._clean_slot(item) for item in slots]
        if len(clean_slots) != 8 or any(item is None for item in clean_slots):
            return ApplyResult(date=date, source="error", error="天气时段数据不完整。")

        instance = self.find_plugin()
        if instance is None:
            try:
                self._write_override_file(date, clean_slots)
            except Exception as exc:  # noqa: BLE001 - report to caller
                return ApplyResult(
                    date=date,
                    source="error",
                    error=f"写入天气覆写文件失败：{exc}",
                )
            self._log(
                "warning",
                f"[DLSWeather] DLS plugin not loaded; wrote weather_overrides.json for {date}",
            )
            return ApplyResult(
                date=date,
                source="file",
                summary="",
                slots_written=len(clean_slots),
                warnings=(
                    "未找到动态生活状态插件，天气已写入 weather_overrides.json，"
                    "重载该插件后生效。",
                ),
            )

        try:
            weather_slot_type = self._slot_type(instance)
            slot_objects = [weather_slot_type(**item) for item in clean_slots]
            day = instance.weather_store.set(date, slot_objects)
        except Exception as exc:  # noqa: BLE001 - report to caller
            self._log(
                "error",
                f"[DLSWeather] Failed to write weather into DLS for {date}: {exc}",
            )
            return ApplyResult(
                date=date,
                source="error",
                error=f"写入动态生活状态插件失败：{exc}",
            )

        warnings: list[str] = []
        updated_state = False
        today = datetime.datetime.now(CHINA_TIMEZONE).date().isoformat()
        if date == today:
            updated_state, state_warning = self._update_today_state(instance, date, day)
            if state_warning:
                warnings.append(state_warning)

        regenerated = False
        if regenerate and date == today:
            regenerated, regen_warning = await self._regenerate_today(instance, parsed_date)
            if regen_warning:
                warnings.append(regen_warning)

        return ApplyResult(
            date=date,
            source="dls",
            summary=str(getattr(day, "summary", "") or ""),
            slots_written=len(clean_slots),
            updated_state=updated_state,
            regenerated=regenerated,
            warnings=tuple(warnings),
        )

    @staticmethod
    def _clean_slot(item: Any) -> dict | None:
        if not isinstance(item, dict):
            return None
        cleaned: dict[str, Any] = {}
        for key in _SLOT_KEYS:
            if key not in item:
                return None
            cleaned[key] = item[key]
        return cleaned

    @staticmethod
    def _slot_type(instance: Any) -> type:
        store = instance.weather_store
        store_module = type(store).__module__
        # e.g. data.plugins.<plugin>.core.weather -> ...core
        core_package = store_module.rsplit(".", 1)[0]
        state_module = importlib.import_module(f"{core_package}.state")
        slot_type = getattr(state_module, "WeatherSlot", None)
        if slot_type is None:
            raise DlsBridgeError("DLS 的 WeatherSlot 类型不可用。")
        return slot_type

    def _update_today_state(
        self,
        instance: Any,
        date: str,
        day: Any,
    ) -> tuple[bool, str]:
        """Attach the new weather to today's existing DLS state."""
        try:
            state = instance.data_mgr.get_current(date)
        except Exception as exc:  # noqa: BLE001 - DLS internals may change
            return False, f"读取今日日程失败：{exc}"
        if state is None or getattr(state, "status", "") != "ok":
            return False, ""
        try:
            updated = replace(
                state,
                weather_summary=str(getattr(day, "summary", "") or ""),
                weather=list(getattr(day, "slots", [])),
                season=str(getattr(getattr(day, "season", None), "label", "") or ""),
            )
            instance.data_mgr.set(updated)
        except Exception as exc:  # noqa: BLE001 - DLS internals may change
            return False, f"更新今日日程天气失败：{exc}"
        return True, ""

    async def _regenerate_today(
        self,
        instance: Any,
        parsed_date: datetime.date,
    ) -> tuple[bool, str]:
        generator = getattr(instance, "generator", None)
        if generator is None:
            return False, "动态生活状态插件没有可用的日程生成器，已跳过重排。"
        if getattr(generator, "is_generating", False):
            return False, "日程正在生成中，已跳过一次重排。"
        try:
            result = await generator.generate(parsed_date, force=True)
        except Exception as exc:  # noqa: BLE001 - DLS internals may change
            return False, f"重新生成今日日程失败：{exc}"
        if getattr(result, "succeeded", False):
            return True, ""
        reason = str(getattr(result, "error", "") or "").strip() or "未知原因"
        return False, f"重新生成今日日程失败：{reason}"

    # ------------------------------------------------------------------
    # Fallback file writing
    # ------------------------------------------------------------------

    def dls_data_dir(self) -> Path:
        """Return the DLS plugin data directory."""
        try:
            from astrbot.core.star.star_tools import StarTools

            return Path(StarTools.get_data_dir(self.plugin_name))
        except Exception:
            root = os.environ.get("ASTRBOT_ROOT", "").strip()
            if not root:
                root = str(Path.home() / ".astrbot")
            return Path(root) / "data" / "plugin_data" / self.plugin_name

    def _write_override_file(self, date: str, slots: list[dict]) -> None:
        path = self.dls_data_dir() / "weather_overrides.json"
        data = read_json(path, {})
        if not isinstance(data, dict):
            data = {}
        data[date] = slots
        # Match DLS behavior: drop dates before today.
        today = datetime.datetime.now(CHINA_TIMEZONE).date().isoformat()
        data = {
            key: value
            for key, value in data.items()
            if isinstance(key, str) and key >= today
        }
        write_json(path, data)
        self._log(
            "info",
            f"[DLSWeather] Wrote fallback weather override to {path} for {date}",
        )