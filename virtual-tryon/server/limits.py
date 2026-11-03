"""Spend protection: per-store and site-wide session + second caps.

In-memory + JSON-file persistence so it survives restarts without a database.
For production scale, swap the JSON store for Redis (same interface). The
counters reset at UTC midnight.
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path

from .config import settings

_LOCK = threading.Lock()
_STATE_FILE = Path(settings.recordings_dir).parent / "limits_state.json"


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _load() -> dict:
    try:
        data = json.loads(_STATE_FILE.read_text())
    except Exception:
        data = {}
    if data.get("day") != _today():
        data = {"day": _today(), "stores": {}, "site_sessions": 0, "site_seconds": 0}
    data.setdefault("stores", {})
    data.setdefault("site_sessions", 0)
    data.setdefault("site_seconds", 0)
    return data


def _save(data: dict) -> None:
    _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    _STATE_FILE.write_text(json.dumps(data))


class LimitError(Exception):
    """Raised when a cap is hit. `.reason` is safe to show the shopper."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def check_and_reserve_session(store_id: str) -> None:
    """Call before starting a live session. Raises LimitError if not allowed."""
    with _LOCK:
        data = _load()
        store = data["stores"].setdefault(store_id, {"sessions": 0, "seconds": 0})

        if data["site_sessions"] >= settings.site_daily_sessions:
            raise LimitError("The free try-on is busy today. Please try again tomorrow.")
        if data["site_seconds"] >= settings.site_daily_seconds:
            raise LimitError("The free try-on is busy today. Please try again tomorrow.")
        if store["sessions"] >= settings.per_store_daily_sessions:
            raise LimitError("This store's daily free try-ons are used up. Please try again tomorrow.")

        store["sessions"] += 1
        data["site_sessions"] += 1
        _save(data)


def record_seconds(store_id: str, seconds: float) -> None:
    """Call when a live session ends, with the actual duration used."""
    seconds = max(0.0, min(float(seconds), settings.max_session_seconds))
    with _LOCK:
        data = _load()
        store = data["stores"].setdefault(store_id, {"sessions": 0, "seconds": 0})
        store["seconds"] += seconds
        data["site_seconds"] += seconds
        _save(data)


def usage_snapshot() -> dict:
    with _LOCK:
        return _load()
