from __future__ import annotations

import datetime
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from core.mapping import (  # noqa: E402
    NATURAL_SLOTS,
    aggregate_day,
    condition_for_code,
    condition_icon,
    format_wind,
    wind_direction,
    wind_level,
)
from core.open_meteo import Forecast, HourlyPoint  # noqa: E402


def make_forecast(date: datetime.date, *, rain_hour: int | None = None) -> Forecast:
    points: list[HourlyPoint] = []
    for hour in range(24):
        code = 0 if hour < 12 else 2
        if rain_hour is not None and hour == rain_hour:
            code = 61
        # A realistic daily curve: coldest before dawn, peak around 15:00.
        if hour <= 15:
            temperature = 5.0 + hour * 0.8
        else:
            temperature = 17.0 - (hour - 15) * 0.5
        points.append(
            HourlyPoint(
                time=datetime.datetime.combine(date, datetime.time(hour=hour)),
                temperature=temperature,
                weather_code=code,
                precipitation_probability=80.0 if code == 61 else 5.0,
                precipitation_mm=1.0 if code == 61 else 0.0,
                wind_speed_kmh=13.0,
                wind_direction_deg=135.0,
            )
        )
    return Forecast(
        latitude=39.9,
        longitude=116.4,
        timezone="Asia/Shanghai",
        hourly=tuple(points),
        daily_low={date.isoformat(): 0.0},
        daily_high={date.isoformat(): 23.0},
        fetched_at=datetime.datetime.now(datetime.timezone.utc),
        units={},
    )


class MappingTests(unittest.TestCase):
    def test_eight_slots_in_order(self) -> None:
        date = datetime.date(2026, 11, 21)
        day = aggregate_day(make_forecast(date), date)
        self.assertEqual(len(day.slots), 8)
        self.assertEqual(
            [slot["time"] for slot in day.slots],
            [name for name, _start, _end in NATURAL_SLOTS],
        )
        for slot in day.slots:
            self.assertEqual(
                set(slot),
                {
                    "time",
                    "condition",
                    "temperature",
                    "precipitation",
                    "precipitation_probability",
                    "wind",
                },
            )

    def test_temperature_extremes(self) -> None:
        date = datetime.date(2026, 11, 21)
        day = aggregate_day(make_forecast(date), date)
        by_name = {slot["time"]: slot for slot in day.slots}
        # 凌晨 uses the coldest hour, 下午 uses the hottest hour.
        self.assertEqual(by_name["凌晨"]["temperature"], 5)
        self.assertEqual(by_name["下午"]["temperature"], 17)
        self.assertEqual(day.low, 5)
        self.assertEqual(day.high, 17)

    def test_precipitation_wins_condition(self) -> None:
        date = datetime.date(2026, 11, 21)
        day = aggregate_day(make_forecast(date, rain_hour=15), date)
        by_name = {slot["time"]: slot for slot in day.slots}
        self.assertEqual(by_name["下午"]["condition"], "小雨")
        self.assertEqual(by_name["下午"]["precipitation"], "小雨")
        self.assertGreaterEqual(by_name["下午"]["precipitation_probability"], 80)
        self.assertIn("晴转", day.summary)

    def test_wmo_mapping(self) -> None:
        self.assertEqual(condition_for_code(0)[0], "晴")
        self.assertEqual(condition_for_code(61)[0], "小雨")
        self.assertEqual(condition_for_code(95)[0], "雷阵雨")
        self.assertEqual(condition_for_code(999)[0], "阴")

    def test_icons(self) -> None:
        self.assertEqual(condition_icon("雷阵雨"), "⛈️")
        self.assertEqual(condition_icon("中雨"), "🌧️")
        self.assertEqual(condition_icon("未知"), "🌡️")

    def test_wind(self) -> None:
        self.assertEqual(wind_level(0), 0)
        self.assertEqual(wind_level(13), 3)
        self.assertEqual(wind_direction(135), "东南")
        self.assertEqual(wind_direction(360), "北")
        self.assertEqual(format_wind(0, 0), "微风")
        self.assertEqual(format_wind(13, 135), "东南风 3级")

    def test_missing_date_raises(self) -> None:
        from core.mapping import WeatherDataError

        date = datetime.date(2026, 11, 21)
        with self.assertRaises(WeatherDataError):
            aggregate_day(make_forecast(date), datetime.date(2026, 11, 22))


if __name__ == "__main__":
    unittest.main()