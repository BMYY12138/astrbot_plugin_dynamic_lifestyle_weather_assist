from __future__ import annotations

import asyncio
import dataclasses
import datetime
import json
import pathlib
import sys
import tempfile
import unittest
from zoneinfo import ZoneInfo

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from core.dls import DlsBridge  # noqa: E402

SLOTS = [
    {
        "time": name,
        "condition": "多云",
        "temperature": 10,
        "precipitation": "无",
        "precipitation_probability": 20,
        "wind": "北风 2级",
    }
    for name in ("凌晨", "早上", "上午", "中午", "下午", "傍晚", "晚上", "深夜")
]


class DlsBridgeTests(unittest.TestCase):
    def test_clean_slot(self) -> None:
        self.assertIsNotNone(DlsBridge._clean_slot(SLOTS[0]))
        self.assertIsNone(DlsBridge._clean_slot({"time": "凌晨"}))
        self.assertIsNone(DlsBridge._clean_slot("not a dict"))

    def test_apply_to_live_instance(self) -> None:
        today = datetime.datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
        slot_type = _FakeWeatherSlot
        store = _FakeWeatherStore()
        state = _FakeState(date=today)
        data_mgr = _FakeDataManager(state)
        instance = _FakeDlsInstance(store=store, data_mgr=data_mgr)

        bridge = DlsBridge()
        bridge.find_plugin = lambda: instance  # type: ignore[method-assign]
        bridge._slot_type = lambda _instance: slot_type  # type: ignore[method-assign]

        result = asyncio.run(bridge.apply(today, SLOTS))

        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.source, "dls")
        self.assertEqual(result.slots_written, 8)
        self.assertTrue(result.updated_state)
        self.assertEqual(len(store.saved.get(today, [])), 8)
        self.assertEqual(data_mgr.saved, 1)
        self.assertEqual(data_mgr.state.weather_summary, store.day.summary)

    def test_fallback_file_write_merges_and_prunes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            bridge = DlsBridge()
            bridge.dls_data_dir = lambda: pathlib.Path(temp_dir)  # type: ignore[method-assign]
            path = pathlib.Path(temp_dir) / "weather_overrides.json"
            path.write_text(
                json.dumps(
                    {
                        "2020-01-01": SLOTS,
                        "2099-01-01": SLOTS,
                    }
                ),
                encoding="utf-8",
            )
            bridge._write_override_file("2099-01-02", SLOTS)
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertNotIn("2020-01-01", data)
            self.assertIn("2099-01-01", data)
            self.assertIn("2099-01-02", data)
            self.assertEqual(len(data["2099-01-02"]), 8)

    def test_past_date_is_pruned(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            bridge = DlsBridge()
            bridge.dls_data_dir = lambda: pathlib.Path(temp_dir)  # type: ignore[method-assign]
            bridge._write_override_file("2020-01-01", SLOTS)
            data = json.loads(
                (pathlib.Path(temp_dir) / "weather_overrides.json").read_text(
                    encoding="utf-8"
                )
            )
            # 2020-01-01 is in the past and is pruned, leaving an empty object.
            self.assertEqual(data, {})


@dataclasses.dataclass
class _FakeWeatherSlot:
    time: str
    condition: str
    temperature: int
    precipitation: str = "无"
    precipitation_probability: int = 0
    wind: str = ""


@dataclasses.dataclass
class _FakeSeason:
    label: str = "深秋"


@dataclasses.dataclass
class _FakeDay:
    summary: str = "晴，-1~5℃，西北风 2级"
    slots: list = dataclasses.field(default_factory=list)
    season: _FakeSeason = dataclasses.field(default_factory=_FakeSeason)


class _FakeWeatherStore:
    def __init__(self) -> None:
        self.saved: dict[str, list] = {}
        self.day = _FakeDay()

    def set(self, date: str, slots: list) -> _FakeDay:
        self.saved[date] = slots
        self.day.slots = slots
        return self.day


@dataclasses.dataclass
class _FakeState:
    date: str
    status: str = "ok"
    weather_summary: str = ""
    weather: list = dataclasses.field(default_factory=list)
    season: str = ""


class _FakeDataManager:
    def __init__(self, state: _FakeState) -> None:
        self.state = state
        self.saved = 0

    def get_current(self, date: str):
        return self.state if self.state.date == date else None

    def set(self, state) -> None:
        self.state = state
        self.saved += 1


@dataclasses.dataclass
class _FakeDlsInstance:
    store: _FakeWeatherStore
    data_mgr: _FakeDataManager

    @property
    def weather_store(self):
        return self.store

    @property
    def generator(self):
        return None


if __name__ == "__main__":
    unittest.main()