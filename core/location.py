"""Location resolution via the Open-Meteo geocoding API."""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from typing import Any

import aiohttp

DEFAULT_USER_AGENT = (
    "astrbot-plugin-dls-weather/1.0 (+https://github.com/fish/"
    "astrbot_plugin_dynamic_lifestyle_weather_assist)"
)
GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"

_COORD_RE = re.compile(
    r"^\s*(-?\d{1,3}(?:\.\d+)?)\s*[,，]\s*(-?\d{1,3}(?:\.\d+)?)\s*$"
)


class LocationError(Exception):
    """Base class for location resolution failures."""


class LocationNotFound(LocationError):
    """Raised when a city name cannot be geocoded."""


class LocationRequestError(LocationError):
    """Raised when the geocoding service cannot be reached."""


@dataclass(frozen=True)
class Location:
    """A resolved location."""

    name: str
    latitude: float
    longitude: float
    admin1: str = ""
    country: str = ""
    country_code: str = ""
    timezone: str = ""
    source: str = "geocoding"

    @property
    def display(self) -> str:
        """Human-readable label such as ``北京（北京市，中国）``."""
        parts: list[str] = []
        if self.admin1 and self.admin1 != self.name:
            parts.append(self.admin1)
        if self.country:
            parts.append(self.country)
        suffix = f"（{'，'.join(parts)}）" if parts else ""
        return f"{self.name}{suffix}"

    @property
    def coordinates(self) -> str:
        return f"{self.latitude:.4f},{self.longitude:.4f}"


def parse_coordinates(text: str) -> tuple[float, float] | None:
    """Parse ``"39.9042,116.4074"`` into a validated coordinate pair.

    Args:
        text: Raw location string.

    Returns:
        ``(latitude, longitude)`` when the string is a valid coordinate pair,
        otherwise ``None``.
    """
    if not isinstance(text, str):
        return None
    match = _COORD_RE.match(text)
    if match is None:
        return None
    latitude = float(match.group(1))
    longitude = float(match.group(2))
    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        return None
    return latitude, longitude


def _clean_name(text: str) -> str:
    """Strip a trailing region qualifier such as ``北京,中国``."""
    value = (text or "").strip()
    for separator in ("，", ","):
        if separator in value:
            head = value.split(separator, 1)[0].strip()
            if head:
                return head
    return value


def _distance_score(location: dict[str, Any], name: str) -> tuple[int, int]:
    """Rank geocoding candidates by exact name and Chinese-name matches."""
    result_name = str(location.get("name") or "")
    exact = 0 if result_name == name else 1
    chinese_name = 0 if re.search(r"[\u4e00-\u9fff]", result_name) else 1
    return exact, chinese_name


async def resolve_location(
    text: str,
    *,
    latitude: float | None = None,
    longitude: float | None = None,
    language: str = "zh",
    country_code: str = "CN",
    timeout: int = 20,
    user_agent: str = DEFAULT_USER_AGENT,
) -> Location:
    """Resolve a city name or coordinate pair into a concrete location.

    Args:
        text: City name, ``"lat,lon"`` string, or a display label.
        latitude: Optional latitude; used together with ``longitude``.
        longitude: Optional longitude; used together with ``latitude``.
        language: Geocoding result language.
        country_code: Optional two-letter country code used to prefer local
            results. Empty string disables the preference.
        timeout: HTTP timeout in seconds.
        user_agent: User-Agent header.

    Returns:
        A resolved :class:`Location`.

    Raises:
        LocationNotFound: The name returned no geocoding results.
        LocationRequestError: The geocoding service failed.
    """
    if latitude is not None and longitude is not None:
        if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
            raise LocationError("经纬度超出有效范围。")
        label = text.strip() if text and text.strip() else f"{latitude:.4f},{longitude:.4f}"
        return Location(
            name=label,
            latitude=float(latitude),
            longitude=float(longitude),
            source="coordinates",
        )

    coordinates = parse_coordinates(text)
    if coordinates is not None:
        return Location(
            name=f"{coordinates[0]:.4f},{coordinates[1]:.4f}",
            latitude=coordinates[0],
            longitude=coordinates[1],
            source="coordinates",
        )

    name = _clean_name(text)
    if not name:
        raise LocationNotFound("还没有配置地理位置，请填写城市名或经纬度。")

    params = {
        "name": name,
        "count": 10,
        "language": language or "zh",
        "format": "json",
    }
    headers = {"User-Agent": user_agent, "Accept": "application/json"}
    try:
        client_timeout = aiohttp.ClientTimeout(total=max(5, int(timeout)))
        async with aiohttp.ClientSession(
            timeout=client_timeout,
            headers=headers,
            trust_env=True,
        ) as session:
            async with session.get(GEOCODING_URL, params=params) as response:
                body = await response.text()
                if response.status != 200:
                    raise LocationRequestError(
                        f"地理编码服务返回 HTTP {response.status}。"
                    )
    except aiohttp.ClientError as exc:
        raise LocationRequestError(f"无法连接地理编码服务：{exc}") from exc
    except TimeoutError as exc:
        raise LocationRequestError("地理编码请求超时。") from exc

    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise LocationRequestError("地理编码服务返回了无法解析的数据。") from exc
    if not isinstance(payload, dict):
        raise LocationRequestError("地理编码服务返回了意外的数据格式。")

    results = payload.get("results")
    if not isinstance(results, list) or not results:
        raise LocationNotFound(
            f"找不到位置「{name}」。可以换一个更常见的城市名，或直接填写经纬度，例如 39.9042,116.4074。"
        )

    candidates = [item for item in results if isinstance(item, dict)]
    candidates = [
        item
        for item in candidates
        if isinstance(item.get("latitude"), (int, float))
        and isinstance(item.get("longitude"), (int, float))
        and not math.isnan(float(item["latitude"]))
        and not math.isnan(float(item["longitude"]))
    ]
    if not candidates:
        raise LocationNotFound(f"位置「{name}」的坐标数据无效，请改用经纬度。")

    preferred: list[dict[str, Any]] = []
    if country_code:
        preferred = [
            item
            for item in candidates
            if str(item.get("country_code") or "").upper() == country_code
        ]
    pool = preferred or candidates
    chosen = min(pool, key=lambda item: _distance_score(item, name))

    return Location(
        name=str(chosen.get("name") or name).strip() or name,
        latitude=float(chosen["latitude"]),
        longitude=float(chosen["longitude"]),
        admin1=str(chosen.get("admin1") or "").strip(),
        country=str(chosen.get("country") or "").strip(),
        country_code=str(chosen.get("country_code") or "").strip().upper(),
        timezone=str(chosen.get("timezone") or "").strip(),
        source="geocoding",
    )