from __future__ import annotations

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from core.open_meteo import Forecast, WeatherFetchError, parse_forecast  # noqa: E402


def payload() -> dict:
    times = [f"2026-11-21T{hour:02d}:00" for hour in range(24)]
    return {
        "latitude": 39.9,
        "longitude": 116.4,
        "timezone": "Asia/Shanghai",
        "hourly_units": {"temperature_2m": "°C"},
        "hourly": {
            "time": times,
            "temperature_2m": [float(hour % 10) for hour in range(24)],
            "weather_code": [0 if hour < 12 else 2 for hour in range(24)],
            "precipitation_probability": [5 for _ in range(24)],
            "precipitation": [0.0 for _ in range(24)],
            "wind_speed_10m": [10.0 for _ in range(24)],
            "wind_direction_10m": [90.0 for _ in range(24)],
        },
        "daily": {
            "time": ["2026-11-21"],
            "temperature_2m_min": [1.0],
            "temperature_2m_max": [12.0],
        },
    }


class OpenMeteoTests(unittest.TestCase):
    def test_parse(self) -> None:
        forecast = parse_forecast(payload())
        self.assertIsInstance(forecast, Forecast)
        self.assertEqual(len(forecast.hourly), 24)
        self.assertEqual(forecast.daily_low["2026-11-21"], 1.0)
        self.assertEqual(forecast.daily_high["2026-11-21"], 12.0)
        self.assertEqual(forecast.units.get("temperature_2m"), "°C")

    def test_error_payload(self) -> None:
        with self.assertRaises(WeatherFetchError):
            parse_forecast({"error": True, "reason": "bad request"})

    def test_missing_hourly(self) -> None:
        with self.assertRaises(WeatherFetchError):
            parse_forecast({"latitude": 1, "longitude": 2})


if __name__ == "__main__":
    unittest.main()