"""Consented capture storage for training: photos, garments, results, live
frames and recorded videos, plus an index the admin gallery reads.

Layout:  {CAPTURES_DIR}/{YYYY-MM-DD}/{trial_id}/
             meta.json  person.jpg  garment.jpg  result.jpg
             live_in/000001.jpg ...   (sampled camera frames, ~2 fps)
             video.webm|mp4           (browser recording, if uploaded)

Nothing is written unless the shopper opted in (see consent.should_record).
"""
from __future__ import annotations

import io
import json
import re
import threading
import time
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from .config import settings

_LOCK = threading.Lock()
_SAFE = re.compile(r"^[a-f0-9]{32}$")


def root() -> Path:
    return Path(settings.captures_dir)


def new_trial() -> str:
    return uuid.uuid4().hex


def _dir(trial_id: str, create: bool = True) -> Path:
    if not _SAFE.match(trial_id):
        raise ValueError("bad trial id")
    # Trials live under their creation day; look up an existing one first.
    for d in sorted(root().glob(f"*/{trial_id}")):
        return d
    d = root() / datetime.now(timezone.utc).strftime("%Y-%m-%d") / trial_id
    if create:
        d.mkdir(parents=True, exist_ok=True)
    return d


def update_meta(trial_id: str, **fields) -> None:
    with _LOCK:
        d = _dir(trial_id)
        p = d / "meta.json"
        meta = json.loads(p.read_text()) if p.exists() else {"trial_id": trial_id, "created": time.time()}
        meta.update(fields)
        meta["updated"] = time.time()
        p.write_text(json.dumps(meta, indent=1))


def save_file(trial_id: str, name: str, data: bytes) -> None:
    if not re.match(r"^[a-z_]+(/[0-9]{6})?\.(jpg|png|webm|mp4)$", name):
        raise ValueError("bad file name")
    p = _dir(trial_id) / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)


def save_photo_trial(trial_id: str, *, person: bytes, garment: bytes, result: bytes | None,
                     meta: dict) -> None:
    save_file(trial_id, "person.jpg", person)
    save_file(trial_id, "garment.jpg", garment)
    if result:
        save_file(trial_id, "result.jpg", result)
    update_meta(trial_id, kind="photo", **meta)


def list_trials(limit: int = 200) -> list[dict]:
    out = []
    for meta in sorted(root().glob("*/*/meta.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:limit]:
        try:
            m = json.loads(meta.read_text())
        except Exception:
            continue
        d = meta.parent
        m["day"] = d.parent.name
        m["files"] = sorted(str(f.relative_to(d)) for f in d.rglob("*") if f.is_file() and f.name != "meta.json"
                            and not f.parent.name == "live_in")
        m["live_frames"] = len(list((d / "live_in").glob("*.jpg"))) if (d / "live_in").exists() else 0
        out.append(m)
    return out


def file_path(day: str, trial_id: str, name: str) -> Path:
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", day) or not _SAFE.match(trial_id):
        raise ValueError("bad path")
    p = (root() / day / trial_id / name).resolve()
    if not str(p).startswith(str(root().resolve()) + "/") or not p.is_file():
        raise FileNotFoundError(name)
    return p


def export_zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:  # images/videos are already compressed
        for f in root().rglob("*"):
            if f.is_file():
                z.write(f, f.relative_to(root()))
    return buf.getvalue()


def stats() -> dict:
    trials = list(root().glob("*/*/meta.json"))
    size = sum(f.stat().st_size for f in root().rglob("*") if f.is_file()) if root().exists() else 0
    return {"trials": len(trials), "bytes": size}
