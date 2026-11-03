"""Live-session bookkeeping: per-visitor rate limit + server-measured duration.

The browser reports nothing we bill on. We note when a session's key was
minted and, on /live/end (or when it is abandoned), charge the elapsed time,
capped at MAX_SESSION_SECONDS — which Decart also enforces on its side via
constraints.realtime.maxSessionDuration.
"""
from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass

from . import limits
from .config import settings

_LOCK = threading.Lock()
_ip_hits: dict[str, list[float]] = {}


def rate_limit_wait_minutes(ip: str) -> int:
    """Sliding one-hour window per visitor. 0 = allowed (and counted)."""
    now = time.time()
    with _LOCK:
        recent = [t for t in _ip_hits.get(ip, []) if now - t < 3600]
        if len(recent) >= settings.tokens_per_ip_per_hour:
            _ip_hits[ip] = recent
            return max(1, int((3600 - (now - recent[0])) // 60) + 1)
        recent.append(now)
        _ip_hits[ip] = recent
        if len(_ip_hits) > 10_000:
            for k in [k for k, v in _ip_hits.items() if not any(now - t < 3600 for t in v)]:
                del _ip_hits[k]
        return 0


@dataclass
class LiveSession:
    store_id: str
    started: float
    record: bool


_sessions: dict[str, LiveSession] = {}


def start(store_id: str, record: bool) -> str:
    sweep()
    sid = uuid.uuid4().hex
    with _LOCK:
        _sessions[sid] = LiveSession(store_id, time.time(), record)
    return sid


def end(session_id: str) -> float | None:
    """Close a session; returns the seconds charged, or None if unknown."""
    with _LOCK:
        s = _sessions.pop(session_id, None)
    if s is None:
        return None
    used = min(time.time() - s.started, settings.max_session_seconds)
    limits.record_seconds(s.store_id, used)
    sweep()
    return used


def sweep() -> None:
    """Charge the full cap for sessions the browser never closed."""
    cutoff = time.time() - settings.max_session_seconds - settings.decart_token_ttl_seconds
    with _LOCK:
        stale = [(k, v) for k, v in _sessions.items() if v.started < cutoff]
        for k, _ in stale:
            del _sessions[k]
    for _, s in stale:
        limits.record_seconds(s.store_id, settings.max_session_seconds)
