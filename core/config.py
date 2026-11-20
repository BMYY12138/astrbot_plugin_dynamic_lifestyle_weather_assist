"""Configuration parsing for the DLS weather assistant plugin.

This module deliberately has no AstrBot imports so it can be unit tested
outside a running AstrBot instance.
"""

from __future__ import annotations

import datetime
import re
from dataclasses import dataclass, field
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_TIMEZONE = "Asia/Shanghai"
DEFAULT_UPDATE_TIME = "23:30"
DEFAULT_COUNTRY_CODE = "CN"

TARGET_TOMORROW = "tomorrow"
TARGET_TODAY = "today"
TARGET_BOTH = "both"
VALID_TARGETS = (TARGET_TOMORROW, TARGET_TODAY, TARGET_BOTH)

_TARGET_ALIASES: dict[str, str] = {
    "tomorrow": TARGET_TOMORROW,
    "tomorow": TARGET_TOMORROW,
    "明天": TARGET_TOMORROW,
    "明日": TARGET_TOMORROW,
    "today": TARGET_TODAY,
    "今天": TARGET_TODAY,
    "今日": TARGET_TODAY,
    "both": TARGET_BOTH,
    "all": TARGET_BOTH,
    "今天和明天": TARGET_BOTH,
    "明天和今天": TARGET_BOTH,
    "全部": TARGET_BOTH,
}

_CLOCK_RE = re.compile(r"^\s*(\d{1,2})\s*[:：]\s*(\d{1,2})\s*$")


def parse_clock(value: Any) -> tuple[int, int] | None:
    """Parse a 24-hour clock string such as ``23:30``.

    Args:
        value: Raw configuration value.

    Returns:
        ``(hour, minute)`` when the value is a valid clock time, otherwise
        ``None``.
    """
    if not isinstance(value, str):
        return None
    match = _CLOCK_RE.match(value)
    if match is None:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return hour, minute


def format_clock(hour: int, minute: int) -> str:
    """Format an hour/minute pair as ``HH:MM``."""
    return f"{hour:02d}:{minute:02d}"


def normalize_target(value: Any, default: str = TARGET_TOMORROW) -> str:
    """Normalize a target value to one of ``tomorrow``/``today``/``both``.

    Args:
        value: Raw configuration or command value.
        default: Value returned when the input cannot be recognized.

    Returns:
        A canonical target string.
    """
    if isinstance(value, str):
        key = value.strip().lower()
        if key in _TARGET_ALIASES:
            return _TARGET_ALIASES[key]
        if key in VALID_TARGETS:
            return key
    return default


def _coerce_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        key = value.strip().lower()
        if key in {"1", "true", "yes", "on", "是", "开"}:
            return True
        if key in {"0", "false", "no", "off", "否", "关"}:
            return False
    return default


def _coerce_int(value: Any, default: int, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool):
        return default
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, number))


def _coerce_coordinate(value: Any, *, limit: float) -> float | None:
    """Convert a configured coordinate to a float, treating 0 as unset."""
    if isinstance(value, bool) or value in (None, ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number == 0 or abs(number) > limit:
        return None
    return number


@dataclass(frozen=True)
class LocationSpec:
    """An unresolved location request.

    ``latitude``/``longitude`` take precedence when both are present. Otherwise
    ``text`` is geocoded by the weather provider.
    """

    text: str = ""
    latitude: float | None = None
    longitude: float | None = None
    source: str = ""

    @property
    def has_target(self) -> bool:
        """Whether enough information exists to look up a location."""
        return bool(self.text.strip()) or (
            self.latitude is not None and self.longitude is not None
        )

    @property
    def uses_coordinates(self) -> bool:
        return self.latitude is not None and self.longitude is not None

    def label(self) -> str:
        text = self.text.strip()
        if text:
            return text
        if self.uses_coordinates:
            return f"{self.latitude:.4f},{self.longitude:.4f}"
        return "未配置"


@dataclass(frozen=True)
class Settings:
    """Validated plugin settings."""

    location: str = ""
    latitude: float | None = None
    longitude: float | None = None
    country_code: str = DEFAULT_COUNTRY_CODE
    update_hour: int = 23
    update_minute: int = 30
    target: str = TARGET_TOMORROW
    sync_on_start: bool = True
    regenerate_today: bool = False
    timezone: str = DEFAULT_TIMEZONE
    tz: ZoneInfo = field(default_factory=lambda: ZoneInfo(DEFAULT_TIMEZONE))
    request_timeout: int = 20
    retry_count: int = 3
    debug: bool = False
    warnings: tuple[str, ...] = ()

    @property
    def update_time(self) -> str:
        """The daily sync time formatted as ``HH:MM``."""
        return format_clock(self.update_hour, self.update_minute)

    def effective_location(self, override: dict[str, Any] | None) -> LocationSpec:
        """Merge the persisted runtime override with config values.

        Priority:
        1. Runtime override coordinates (set through the chat command).
        2. Config coordinates.
        3. Runtime override city name.
        4. Config city name.
        """
        data = override if isinstance(override, dict) else {}
        override_text = str(data.get("location") or "").strip()
        override_lat = _coerce_coordinate(data.get("latitude"), limit=90)
        override_lon = _coerce_coordinate(data.get("longitude"), limit=180)

        if override_lat is not None and override_lon is not None:
            return LocationSpec(
                text=override_text,
                latitude=override_lat,
                longitude=override_lon,
                source="override",
            )
        if override_text:
            return LocationSpec(text=override_text, source="override")
        if self.latitude is not None and self.longitude is not None:
            return LocationSpec(
                text=self.location,
                latitude=self.latitude,
                longitude=self.longitude,
                source="config_coordinates",
            )
        return LocationSpec(text=self.location.strip(), source="config")

    def target_offsets(self, target: str | None = None) -> tuple[int, ...]:
        """Return day offsets (0 = today, 1 = tomorrow) for a target."""
        selected = normalize_target(target if target is not None else self.target)
        if selected == TARGET_TODAY:
            return (0,)
        if selected == TARGET_BOTH:
            return (0, 1)
        return (1,)


def parse_settings(config: Any) -> Settings:
    """Build validated :class:`Settings` from an AstrBot config object.

    Args:
        config: Mapping-like plugin config (``AstrBotConfig`` or ``dict``).

    Returns:
        Parsed settings. Invalid values fall back to safe defaults and are
        reported through :attr:`Settings.warnings`.
    """

    def get(key: str, default: Any) -> Any:
        try:
            return config.get(key, default)
        except Exception:
            return default

    warnings: list[str] = []

    location = str(get("location", "") or "").strip()
    latitude = _coerce_coordinate(get("latitude", 0), limit=90)
    longitude = _coerce_coordinate(get("longitude", 0), limit=180)
    if (latitude is None) != (longitude is None):
        warnings.append("经纬度需要同时填写，当前只填了一个，已忽略坐标。")
        latitude = None
        longitude = None

    country_code = str(get("country_code", DEFAULT_COUNTRY_CODE) or "").strip().upper()
    if len(country_code) > 2:
        warnings.append(f"国家/地区代码 {country_code!r} 不是两位代码，已忽略。")
        country_code = ""

    clock = parse_clock(get("update_time", DEFAULT_UPDATE_TIME))
    if clock is None:
        warnings.append(
            f"每日同步时间 {get('update_time', '')!r} 无效，已回退到 {DEFAULT_UPDATE_TIME}。"
        )
        clock = parse_clock(DEFAULT_UPDATE_TIME) or (23, 30)

    target = normalize_target(get("target", TARGET_TOMORROW))
    if get("target", TARGET_TOMORROW) not in VALID_TARGETS:
        raw_target = get("target", "")
        if normalize_target(raw_target, default="") == "":
            warnings.append(f"同步目标 {raw_target!r} 无效，已回退到明天。")
            target = TARGET_TOMORROW

    timezone = str(get("timezone", DEFAULT_TIMEZONE) or "").strip() or DEFAULT_TIMEZONE
    try:
        tz = ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError):
        warnings.append(f"时区 {timezone!r} 无效，已回退到 {DEFAULT_TIMEZONE}。")
        timezone = DEFAULT_TIMEZONE
        tz = ZoneInfo(DEFAULT_TIMEZONE)

    return Settings(
        location=location,
        latitude=latitude,
        longitude=longitude,
        country_code=country_code,
        update_hour=clock[0],
        update_minute=clock[1],
        target=target,
        sync_on_start=_coerce_bool(get("sync_on_start", True), True),
        regenerate_today=_coerce_bool(get("regenerate_today", False), False),
        timezone=timezone,
        tz=tz,
        request_timeout=_coerce_int(
            get("request_timeout", 20), 20, minimum=5, maximum=60
        ),
        retry_count=_coerce_int(get("retry_count", 3), 3, minimum=1, maximum=5),
        debug=_coerce_bool(get("debug_mode", False), False),
        warnings=tuple(warnings),
    )


def today_in(settings: Settings) -> datetime.date:
    """Return the current date in the configured timezone."""
    return datetime.datetime.now(settings.tz).date()


def dates_for_target(settings: Settings, target: str | None = None) -> list[datetime.date]:
    """Return concrete dates for a sync target."""
    today = today_in(settings)
    return [today + datetime.timedelta(days=offset) for offset in settings.target_offsets(target)]