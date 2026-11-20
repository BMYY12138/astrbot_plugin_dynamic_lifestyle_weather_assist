from __future__ import annotations

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from core.location import Location, parse_coordinates  # noqa: E402


class LocationTests(unittest.TestCase):
    def test_parse_coordinates(self) -> None:
        self.assertEqual(parse_coordinates("39.9042,116.4074"), (39.9042, 116.4074))
        self.assertEqual(parse_coordinates(" 31.23，121.47 "), (31.23, 121.47))
        self.assertIsNone(parse_coordinates("北京"))
        self.assertIsNone(parse_coordinates("91,0"))
        self.assertIsNone(parse_coordinates("0,181"))

    def test_display(self) -> None:
        location = Location(
            name="北京",
            latitude=39.9042,
            longitude=116.4074,
            admin1="北京市",
            country="中国",
        )
        self.assertEqual(location.display, "北京（北京市，中国）")
        self.assertEqual(location.coordinates, "39.9042,116.4074")

    def test_display_without_region(self) -> None:
        location = Location(name="39.9042,116.4074", latitude=39.9, longitude=116.4)
        self.assertEqual(location.display, "39.9042,116.4074")


if __name__ == "__main__":
    unittest.main()