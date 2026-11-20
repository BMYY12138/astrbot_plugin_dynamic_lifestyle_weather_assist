from __future__ import annotations

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from core.config import (  # noqa: E402
    LocationSpec,
    dates_for_target,
    normalize_target,
    parse_clock,
    parse_settings,
)


class ConfigTests(unittest.TestCase):
    def test_parse_clock(self) -> None:
        self.assertEqual(parse_clock("23:30"), (23, 30))
        self.assertEqual(parse_clock("07：05"), (7, 5))
        self.assertIsNone(parse_clock("24:00"))
        self.assertIsNone(parse_clock("abc"))

    def test_defaults(self) -> None:
        settings = parse_settings({})
        self.assertEqual(settings.update_time, "23:30")
        self.assertEqual(settings.target, "tomorrow")
        self.assertEqual(settings.timezone, "Asia/Shanghai")
        self.assertEqual(settings.target_offsets(), (1,))

    def test_invalid_values_fall_back(self) -> None:
        settings = parse_settings(
            {
                "update_time": "99:99",
                "target": "nonsense",
                "timezone": "Not/AZone",
                "request_timeout": 999,
                "retry_count": 0,
            }
        )
        self.assertEqual(settings.update_time, "23:30")
        self.assertEqual(settings.target, "tomorrow")
        self.assertEqual(settings.timezone, "Asia/Shanghai")
        self.assertEqual(settings.request_timeout, 60)
        self.assertEqual(settings.retry_count, 1)
        self.assertTrue(settings.warnings)

    def test_target_aliases(self) -> None:
        self.assertEqual(normalize_target("明天"), "tomorrow")
        self.assertEqual(normalize_target("今天"), "today")
        self.assertEqual(normalize_target("全部"), "both")
        self.assertEqual(normalize_target("what", default="today"), "today")

    def test_coordinates_require_both(self) -> None:
        settings = parse_settings({"latitude": 39.9})
        self.assertIsNone(settings.latitude)
        self.assertIsNone(settings.longitude)
        self.assertTrue(any("经纬度" in item for item in settings.warnings))

    def test_effective_location_priority(self) -> None:
        settings = parse_settings({"location": "北京"})
        spec = settings.effective_location(None)
        self.assertIsInstance(spec, LocationSpec)
        self.assertEqual(spec.text, "北京")
        self.assertEqual(spec.source, "config")

        override = {"location": "上海", "latitude": 31.23, "longitude": 121.47}
        spec = settings.effective_location(override)
        self.assertEqual(spec.text, "上海")
        self.assertTrue(spec.uses_coordinates)
        self.assertEqual(spec.source, "override")

        coords = parse_settings(
            {"location": "北京", "latitude": 39.9, "longitude": 116.4}
        ).effective_location({"location": "上海"})
        self.assertFalse(coords.uses_coordinates)
        self.assertEqual(coords.text, "上海")
        self.assertEqual(coords.source, "override")

        config_coords = parse_settings(
            {"location": "北京", "latitude": 39.9, "longitude": 116.4}
        ).effective_location(None)
        self.assertTrue(config_coords.uses_coordinates)
        self.assertEqual(config_coords.source, "config_coordinates")

    def test_offset_dates_use_timezone(self) -> None:
        settings = parse_settings({"target": "both"})
        self.assertEqual(len(settings.target_offsets()), 2)
        self.assertEqual(len(dates_for_target(settings)), 2)


if __name__ == "__main__":
    unittest.main()