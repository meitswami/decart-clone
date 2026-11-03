"""Consent gate + recording of trial INPUTS (not Decart output) for training."""
from __future__ import annotations

import json
import time
import uuid
from pathlib import Path

from .config import settings


class ConsentError(Exception):
    pass


def new_trial_id() -> str:
    return uuid.uuid4().hex


def require_consent(consented: bool, is_adult: bool) -> None:
    if not settings.recording_enabled:
        return
    if not consented:
        raise ConsentError("Recording requires opt-in consent.")
    if not is_adult:
        raise ConsentError("Recording is not allowed for users under 18.")


def should_record(consented: bool, is_adult: bool) -> bool:
    """Try-on works for everyone; only the *recording* needs opt-in (DPDP Act:
    consent can't be a condition of the service). Under-18s are never recorded."""
    return bool(settings.recording_enabled and consented and is_adult)


def record_trial(trial_id: str, store_id: str, garment_path: str,
                 camera_clip: bytes | None, meta: dict) -> None:
    """Persist only what we are allowed to: shopper input clip + garment + meta.
    Never writes upstream (Decart) output frames unless RECORD_OUTPUT_FRAMES."""
    base = Path(settings.recordings_dir) / store_id / trial_id
    base.mkdir(parents=True, exist_ok=True)
    (base / "meta.json").write_text(json.dumps({
        "trial_id": trial_id, "store_id": store_id, "ts": time.time(),
        "garment_path": garment_path, **meta,
    }))
    if camera_clip is not None:
        (base / "camera.webm").write_bytes(camera_clip)


def purge_expired() -> int:
    """Delete recordings older than retention window. Returns count removed."""
    cutoff = time.time() - settings.recording_retention_days * 86400
    removed = 0
    root = Path(settings.recordings_dir)
    if not root.exists():
        return 0
    for meta in root.rglob("meta.json"):
        try:
            ts = json.loads(meta.read_text()).get("ts", 0)
        except Exception:
            continue
        if ts < cutoff:
            for f in meta.parent.iterdir():
                f.unlink(missing_ok=True)
            meta.parent.rmdir()
            removed += 1
    return removed
