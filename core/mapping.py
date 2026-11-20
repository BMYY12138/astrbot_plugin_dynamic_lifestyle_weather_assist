"""Map hourly forecast data to the eight natural weather slots used by
``astrbot_plugin_dynamic_life_state``.

The output dictionaries use exactly the keys expected by the DLS
``WeatherSlot`` dataclass:

    time, condition, temperature, precipitation,
    precipitation_probability, wind
"""

from __future__ import annotations

import datetime
import math
from dataclasses import dataclass
from statistics import mean

from .open_meteo import Forecast, HourlyPoint

# (name, start minute, end minute), matching DLS NATURAL_SLOT_RANGES.
NATURAL_SLOTS: tuple[tuple[str, int, int], ...] = (
    ("凌晨", 0, 6 * 60),
    ("早上", 6 * 60, 9 * 60),
    ("上午", 9 * 60, 12 * 60),
    ("中午", 12 * 60, 14 * 60),
    ("下午", 14 * 60, 18 * 60),
    ("傍晚", 18 * 60, 20 * 60),
    ("晚上", 20 * 60, 23 * 60),
    ("深夜", 23 * 60, 24 * 60),
)

# WMO weather code -> (Chinese condition, severity).
# Severity is only used to pick one representative condition per slot:
# precipitation always wins over dry weather, and heavier weather wins over
# lighter weather.
WMO_CONDITIONS: dict[int, tuple[str, int]] = {
    0: ("晴", 0),
    1: ("晴", 1),
    2: ("多云", 2),
    3: ("阴", 3),
    45: ("雾", 4),
    48: ("雾", 4),
    51: ("小雨", 5),
    53: ("小雨", 5),
    55: ("中雨", 6),
    56: ("冻雨", 5),
    57: ("冻雨", 6),
    61: ("小雨", 5),
    63: ("中雨", 6),
    65: ("大雨", 7),
    66: ("冻雨", 6),
    67: ("冻雨", 7),
    71: ("小雪", 5),
    73: ("中雪", 6),
    75: ("大雪", 7),
    77: ("小雪", 5),
    80: ("阵雨", 5),
    81: ("中雨", 6),
    82: ("大雨", 7),
    85: ("小雪", 5),
    86: ("中雪", 6),
    95: ("雷阵雨", 8),
    96: ("雷阵雨", 9),
    99: ("雷阵雨", 9),
}

CONDITION_ICONS: tuple[tuple[str, str], ...] = (
    ("雷阵雨", "⛈️"),
    ("阵雨", "🌦️"),
    ("小雨", "🌦️"),
    ("中雨", "🌧️"),
    ("大雨", "🌧️"),
    ("小雪", "🌨️"),
    ("中雪", "❄️"),
    ("大雪", "❄️"),
    ("冻雨", "🌧️"),
    ("雾", "🌫️"),
    ("霾", "😷"),
    ("晴", "☀️"),
    ("多云", "⛅"),
    ("阴", "☁️"),
)

_PRECIPITATION_KEYWORDS = ("雨", "雪", "雷", "冻", "冰雹")

# Upper bounds of each Beaufort level in km/h.
_BEAUFORT_LIMITS = (1, 6, 12, 20, 29, 39, 50, 62, 75, 89, 103, 118)
_DIRECTIONS = ("北", "东北", "东", "东南", "南", "西南", "西", "西北")


class WeatherDataError(Exception):
    """Raised when a requested date has no usable forecast data."""


@dataclass(frozen=True)
class DaySlots:
    """One day of DLS-compatible weather slots."""

    date: datetime.date
    slots: tuple[dict, ...]
    summary: str
    low: int
    high: int
    morning: str
    afternoon: str

    @property
    def icon(self) -> str:
        """Icon for the main daytime condition."""
        return condition_icon(self.afternoon or self.morning)

    def to_dls_slots(self) -> list[dict]:
        """Return a mutable list of DLS weather slot dictionaries."""
        return [dict(item) for item in self.slots]


def condition_icon(condition: str) -> str:
    """Return an emoji icon for a Chinese condition string."""
    value = str(condition or "").strip()
    for key, icon in CONDITION_ICONS:
        if key in value:
            return icon
    return "🌡️"


def condition_for_code(code: int) -> tuple[str, int]:
    """Return ``(condition, severity)`` for a WMO weather code."""
    return WMO_CONDITIONS.get(int(code), ("阴", 3))


def _has_precipitation(condition: str) -> bool:
    return any(keyword in condition for keyword in _PRECIPITATION_KEYWORDS)


def wind_level(speed_kmh: float) -> int:
    """Convert wind speed in km/h to a Beaufort level (0-12)."""
    speed = max(0.0, float(speed_kmh))
    for level, upper in enumerate(_BEAUFORT_LIMITS):
        if speed < upper:
            return level
    return 12


def wind_direction(degrees: float) -> str:
    """Convert wind direction in degrees to an 8-point Chinese name."""
    try:
        value = float(degrees) % 360.0
    except (TypeError, ValueError):
        return "北"
    index = int(round(value / 45.0)) % 8
    return _DIRECTIONS[index]


def format_wind(speed_kmh: float, degrees: float) -> str:
    """Format wind as ``东南风 2级`` (or ``微风`` when calm)."""
    level = wind_level(speed_kmh)
    if level <= 0:
        return "微风"
    return f"{wind_direction(degrees)}风 {level}级"


def _mean_direction(points: list[HourlyPoint]) -> float:
    """Return a speed-weighted circular mean direction in degrees."""
    sin_sum = 0.0
    cos_sum = 0.0
    total = 0.0
    for point in points:
        weight = max(point.wind_speed_kmh, 0.1)
        radians = math.radians(point.wind_direction_deg)
        sin_sum += math.sin(radians) * weight
        cos_sum += math.cos(radians) * weight
        total += weight
    if total <= 0:
        return points[0].wind_direction_deg if points else 0.0
    return (math.degrees(math.atan2(sin_sum / total, cos_sum / total))) % 360.0


def _pick_condition(points: list[HourlyPoint]) -> str:
    """Pick the most representative condition for a slot."""
    counts: dict[int, int] = {}
    for point in points:
        counts[point.weather_code] = counts.get(point.weather_code, 0) + 1

    wet: list[tuple[int, int, str]] = []
    dry: list[tuple[int, int, str]] = []
    for code, count in counts.items():
        condition, severity = condition_for_code(code)
        target = wet if severity >= 5 else dry
        target.append((severity, count, condition))

    if wet:
        # Heavy precipitation wins; count breaks ties.
        severity, _count, condition = max(wet, key=lambda item: (item[0], item[1]))
        return condition
    if dry:
        # Most frequent dry condition; cloudier condition breaks ties.
        _severity, _count, condition = max(dry, key=lambda item: (item[1], item[0]))
        return condition
    return "阴"


def _slot_temperature(points: list[HourlyPoint], name: str) -> int:
    """Choose a representative temperature for a natural slot."""
    temperatures = [point.temperature for point in points]
    if name in ("凌晨", "深夜"):
        value = min(temperatures)
    elif name in ("中午", "下午"):
        value = max(temperatures)
    else:
        value = mean(temperatures)
    return max(-50, min(60, int(round(value))))


def _slot_points(points: list[HourlyPoint], name: str, start: int, end: int) -> list[HourlyPoint]:
    """Return points belonging to a slot, with a nearest-hour fallback."""
    selected = [
        point for point in points if start <= point.time.hour * 60 + point.time.minute < end
    ]
    if selected:
        return selected
    midpoint = (start + end) / 2
    nearest = min(
        points,
        key=lambda point: abs(point.time.hour * 60 + point.time.minute - midpoint),
    )
    return [nearest]


def aggregate_day(forecast: Forecast, target: datetime.date) -> DaySlots:
    """Aggregate hourly forecast data into DLS slots for one date.

    Args:
        forecast: Parsed multi-day forecast.
        target: Local date to aggregate.

    Returns:
        A :class:`DaySlots` with eight DLS-compatible slot dictionaries.

    Raises:
        WeatherDataError: No hourly points exist for the requested date.
    """
    points = forecast.points_for_date(target)
    if not points:
        raise WeatherDataError(
            f"天气服务没有返回 {target.isoformat()} 的逐小时数据，可能是预报范围不足。"
        )

    slots: list[dict] = []
    for name, start, end in NATURAL_SLOTS:
        slot_points = _slot_points(points, name, start, end)
        condition = _pick_condition(slot_points)
        temperature = _slot_temperature(slot_points, name)
        probability = int(
            round(max(point.precipitation_probability for point in slot_points))
        )
        probability = max(0, min(100, probability))
        wind = format_wind(
            mean(point.wind_speed_kmh for point in slot_points),
            _mean_direction(slot_points),
        )
        slots.append(
            {
                "time": name,
                "condition": condition[:20],
                "temperature": temperature,
                "precipitation": condition[:20] if _has_precipitation(condition) else "无",
                "precipitation_probability": probability,
                "wind": wind[:40],
            }
        )

    temperatures = [int(item["temperature"]) for item in slots]
    low, high = min(temperatures), max(temperatures)
    morning = next(item["condition"] for item in slots if item["time"] == "早上")
    afternoon = next(item["condition"] for item in slots if item["time"] == "下午")
    transition = morning if morning == afternoon else f"{morning}转{afternoon}"
    summary = f"{transition}，{low}~{high}℃"
    wind = next((item["wind"] for item in slots if item["wind"]), "")
    if wind:
        summary += f"，{wind}"
    return DaySlots(
        date=target,
        slots=tuple(slots),
        summary=summary,
        low=low,
        high=high,
        morning=morning,
        afternoon=afternoon,
    )