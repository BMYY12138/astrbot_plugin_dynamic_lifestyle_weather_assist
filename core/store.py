"""Small JSON stores used by the plugin.

All writes are atomic (write to a temporary file, then ``os.replace``) so a
crash cannot leave a half-written state file behind.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


def read_json(path: Path, default: Any) -> Any:
    """Read a JSON file, returning ``default`` when it is missing or invalid."""
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return default
    except OSError:
        return default
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return default
    return data if data is not None else default


def write_json(path: Path, data: Any) -> None:
    """Atomically write JSON to ``path``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, ensure_ascii=False, indent=2)
    descriptor, temp_name = tempfile.mkstemp(
        prefix=path.name + ".",
        suffix=".tmp",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


class JsonStore:
    """A tiny JSON-backed mapping store."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.data: dict[str, Any] = {}
        self.load()

    def load(self) -> None:
        raw = read_json(self.path, {})
        self.data = raw if isinstance(raw, dict) else {}

    def save(self) -> None:
        write_json(self.path, self.data)


class LocationOverrideStore(JsonStore):
    """Persistent runtime location override set through chat commands."""

    def set_location(
        self,
        location: str,
        *,
        latitude: float | None = None,
        longitude: float | None = None,
        updated_at: str = "",
    ) -> None:
        self.data = {
            "location": str(location or "").strip(),
            "latitude": latitude,
            "longitude": longitude,
            "updated_at": updated_at,
        }
        self.save()

    def clear(self) -> None:
        self.data = {}
        self.save()

    @property
    def has_override(self) -> bool:
        return bool(
            str(self.data.get("location") or "").strip()
            or (
                self.data.get("latitude") is not None
                and self.data.get("longitude") is not None
            )
        )

    def snapshot(self) -> dict[str, Any]:
        return dict(self.data)


class SyncStateStore(JsonStore):
    """Persistent record of the latest sync result."""

    MAX_HISTORY = 10

    def record_success(
        self,
        *,
        trigger: str,
        location: dict[str, Any],
        results: list[dict[str, Any]],
        synced_at: str,
    ) -> None:
        entry = {
            "status": "ok",
            "trigger": trigger,
            "synced_at": synced_at,
            "location": location,
            "results": results,
            "error": "",
        }
        self._push(entry)

    def record_error(
        self,
        *,
        trigger: str,
        error: str,
        synced_at: str,
        location: dict[str, Any] | None = None,
    ) -> None:
        entry = {
            "status": "error",
            "trigger": trigger,
            "synced_at": synced_at,
            "location": location or {},
            "results": [],
            "error": error,
        }
        self._push(entry)

    def _push(self, entry: dict[str, Any]) -> None:
        history = self.data.get("history")
        if not isinstance(history, list):
            history = []
        history.append(entry)
        self.data = {
            "latest": entry,
            "history": history[-self.MAX_HISTORY :],
        }
        self.save()

    @property
    def latest(self) -> dict[str, Any]:
        latest = self.data.get("latest")
        return latest if isinstance(latest, dict) else {}