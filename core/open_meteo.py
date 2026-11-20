"""Open-Meteo forecast client and data model."""

from __future__ import annotations

import datetime
import json
import math
from dataclasses import dataclass
from typing import Any

import aiohttp

DEFAULT_USER_AGENT = (
    "astrbot-plugin-dls-weather/1.0 (+https://github.com/fish/"
    "astrbot_plugin_dynamic_lifestyle_weather_assist)"
)
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

HOURLY_FIELDS = (
    "temperature_2m",
    "weather_code",
    "precipitation_probability",
    "precipitation",
    "wind_speed_10m",
    "wind_direction_10m",
)


class WeatherFetchError(Exception):
    """Raised when forecast data cannot be fetched or parsed."""


@dataclass(frozen=True)
class HourlyPoint:
    """One hour of local-time weather."""

    time: datetime.datetime
    temperature: float
    weather_code: int
    precipitation_probability: float
    precipitation_mm: float
    wind_speed_kmh: float
    wind_direction_deg: float


@dataclass(frozen=True)
class Forecast:
    """A multi-day hourly forecast."""

    latitude: float
    longitude: float
    timezone: str
    hourly: tuple[HourlyPoint, ...]
    daily_low: dict[str, float]
    daily_high: dict[str, float]
    fetched_at: datetime.datetime
    units: dict[str, str]

    def points_for_date(self, target: datetime.date) -> list[HourlyPoint]:
        """Return hourly points whose local date equals ``target``."""
        return [point for point in self.hourly if point.time.date() == target]


def _to_float(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(number) or math.isinf(number):
        return None
    return number


def _to_int(value: Any) -> int | None:
    number = _to_float(value)
    if number is None:
        return None
    return int(round(number))


def _parse_times(values: Any) -> list[datetime.datetime]:
    if not isinstance(values, list):
        return []
    parsed: list[datetime.datetime] = []
    for value in values:
        if not isinstance(value, str):
            continue
        try:
            parsed.append(datetime.datetime.fromisoformat(value))
        except ValueError:
            continue
    return parsed


def _parse_daily(payload: dict[str, Any], key: str) -> dict[str, float]:
    daily = payload.get("daily")
    if not isinstance(daily, dict):
        return {}
    times = daily.get("time")
    values = daily.get(key)
    if not isinstance(times, list) or not isinstance(values, list):
        return {}
    result: dict[str, float] = {}
    for raw_time, raw_value in zip(times, values):
        if not isinstance(raw_time, str):
            continue
        number = _to_float(raw_value)
        if number is not None:
            result[raw_time[:10]] = number
    return result


def parse_forecast(payload: dict[str, Any]) -> Forecast:
    """Parse an Open-Meteo forecast payload.

    Args:
        payload: Decoded JSON object.

    Returns:
        Parsed :class:`Forecast`.

    Raises:
        WeatherFetchError: Required hourly data is missing or malformed.
    """
    if not isinstance(payload, dict):
        raise WeatherFetchError("天气服务返回了意外的数据格式。")
    if payload.get("error"):
        reason = payload.get("reason") or "未知错误"
        raise WeatherFetchError(f"天气服务返回错误：{reason}")

    hourly = payload.get("hourly")
    if not isinstance(hourly, dict):
        raise WeatherFetchError("天气服务没有返回逐小时数据。")

    times = _parse_times(hourly.get("time"))
    if not times:
        raise WeatherFetchError("天气服务没有返回有效的时间序列。")

    columns: dict[str, list[Any]] = {}
    for field in HOURLY_FIELDS:
        values = hourly.get(field)
        columns[field] = values if isinstance(values, list) else []

    points: list[HourlyPoint] = []
    for index, moment in enumerate(times):
        temperature = _to_float(_at(columns["temperature_2m"], index))
        code = _to_int(_at(columns["weather_code"], index))
        if temperature is None or code is None:
            continue
        probability = _to_float(_at(columns["precipitation_probability"], index))
        precipitation = _to_float(_at(columns["precipitation"], index))
        wind_speed = _to_float(_at(columns["wind_speed_10m"], index))
        wind_direction = _to_float(_at(columns["wind_direction_10m"], index))
        points.append(
            HourlyPoint(
                time=moment,
                temperature=temperature,
                weather_code=code,
                precipitation_probability=probability or 0.0,
                precipitation_mm=precipitation or 0.0,
                wind_speed_kmh=wind_speed or 0.0,
                wind_direction_deg=wind_direction or 0.0,
            )
        )

    if not points:
        raise WeatherFetchError("天气服务返回的逐小时数据全部无效。")

    units = payload.get("hourly_units")
    return Forecast(
        latitude=float(payload.get("latitude") or 0.0),
        longitude=float(payload.get("longitude") or 0.0),
        timezone=str(payload.get("timezone") or ""),
        hourly=tuple(sorted(points, key=lambda item: item.time)),
        daily_low=_parse_daily(payload, "temperature_2m_min"),
        daily_high=_parse_daily(payload, "temperature_2m_max"),
        fetched_at=datetime.datetime.now(datetime.timezone.utc).astimezone(),
        units={key: str(value) for key, value in units.items()}
        if isinstance(units, dict)
        else {},
    )


def _at(values: list[Any], index: int) -> Any:
    if index < len(values):
        return values[index]
    return None


def build_forecast_params(
    latitude: float,
    longitude: float,
    *,
    timezone: str,
    days: int = 3,
) -> dict[str, Any]:
    """Build query parameters for the forecast endpoint."""
    return {
        "latitude": f"{latitude:.4f}",
        "longitude": f"{longitude:.4f}",
        "hourly": ",".join(HOURLY_FIELDS),
        "daily": "temperature_2m_max,temperature_2m_min",
        "timezone": timezone,
        "forecast_days": max(1, min(16, int(days))),
        "wind_speed_unit": "kmh",
    }


async def fetch_forecast(
    latitude: float,
    longitude: float,
    *,
    timezone: str,
    days: int = 3,
    timeout: int = 20,
    user_agent: str = DEFAULT_USER_AGENT,
) -> Forecast:
    """Fetch and parse an Open-Meteo forecast.

    Args:
        latitude: Location latitude.
        longitude: Location longitude.
        timezone: IANA timezone name used for hourly labels.
        days: Number of forecast days to request.
        timeout: HTTP timeout in seconds.
        user_agent: User-Agent header.

    Returns:
        Parsed forecast.

    Raises:
        WeatherFetchError: Network, HTTP, JSON, or data validation failure.
    """
    params = build_forecast_params(
        latitude,
        longitude,
        timezone=timezone,
        days=days,
    )
    headers = {"User-Agent": user_agent, "Accept": "application/json"}
    try:
        client_timeout = aiohttp.ClientTimeout(total=max(5, int(timeout)))
        async with aiohttp.ClientSession(
            timeout=client_timeout,
            headers=headers,
            trust_env=True,
        ) as session:
            async with session.get(FORECAST_URL, params=params) as response:
                body = await response.text()
                if response.status != 200:
                    raise WeatherFetchError(
                        f"天气服务返回 HTTP {response.status}，请稍后重试。"
                    )
    except aiohttp.ClientError as exc:
        raise WeatherFetchError(f"无法连接天气服务：{exc}") from exc
    except TimeoutError as exc:
        raise WeatherFetchError("天气服务请求超时。") from exc

    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise WeatherFetchError("天气服务返回了无法解析的数据。") from exc
    return parse_forecast(payload)