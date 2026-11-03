"""Live try-on over a WebSocket on the SAME HTTPS origin as the page.

Why not WebRTC here: RunPod pods (and many mobile carriers) block the UDP that
WebRTC needs without a TURN relay. A WebSocket rides the normal HTTPS
connection, so it works through RunPod's proxy and on mobile data.

Protocol (wss://<host>/live/ws?ticket=...):
  client -> {"type":"start","garment_b64":..., "category":"tops"}       (text)
  server -> {"type":"ready","max_seconds":20,"trial_id":"..."}          (text)
  client -> JPEG camera frame                                            (binary, repeat)
  server -> JPEG dressed frame, one per input frame                      (binary)
  server -> {"type":"stats",...} every ~2 s, {"type":"end","reason":...} (text)
The session clock starts at the first frame. With consent, ~2 camera frames
per second are saved for training (inputs only).
"""
from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import time
from pathlib import Path

import cv2
import numpy as np
from fastapi import WebSocket, WebSocketDisconnect
from PIL import Image
from starlette.concurrency import run_in_threadpool

from . import captures
from .config import settings
from .engine_ticket import TicketError, verify_ticket

log = logging.getLogger("tryon.live_ws")
_active = 0
SAVE_EVERY_S = 0.5


def _dress_fn(garment: Image.Image, category: str):
    from pipeline import photo_tryon
    if photo_tryon._available():
        def dress(frame_rgb: np.ndarray) -> np.ndarray:
            img = photo_tryon._call_model(Image.fromarray(frame_rgb), garment, category,
                                          "flat-lay", "live", seed=7)
            return np.asarray(img.convert("RGB").resize((frame_rgb.shape[1], frame_rgb.shape[0])))
        return dress, "fashn"

    def stub(frame_rgb: np.ndarray) -> np.ndarray:  # visible stand-in until the model is installed
        time.sleep(0.4)
        out = frame_rgb.copy()
        h, w = out.shape[:2]
        sl = (slice(int(h * 0.35), int(h * 0.95)), slice(int(w * 0.3), int(w * 0.7)))
        out[sl] = (out[sl] * 0.45 + np.array([190, 40, 70]) * 0.55).astype(np.uint8)
        return out
    return stub, "stand-in"


async def handle(ws: WebSocket) -> None:
    global _active
    from pipeline.live_keyframe import LiveDresser

    await ws.accept()
    try:
        claims = verify_ticket(ws.query_params.get("ticket", ""), settings.engine_secret)
    except TicketError as e:
        await ws.send_text(json.dumps({"type": "end", "reason": str(e)}))
        await ws.close(code=4401)
        return
    if _active >= settings.engine_max_sessions:
        await ws.send_text(json.dumps({"type": "end", "reason": "busy"}))
        await ws.close(code=4429)
        return

    _active += 1
    keepalive = Path(settings.keepalive_file)
    record = bool(claims.get("record"))
    trial_id = claims.get("trial_id") or captures.new_trial()
    max_seconds = int(claims["max_seconds"])
    frames = 0
    t_first = None
    last_save = 0.0
    last_stats = time.time()
    try:
        start = json.loads(await asyncio.wait_for(ws.receive_text(), timeout=20))
        category = start.get("category", "tops")
        if category not in ("tops", "bottoms", "one-pieces"):
            category = "tops"
        g_bytes = base64.b64decode(start["garment_b64"][:20_000_000])
        garment = Image.open(io.BytesIO(g_bytes)).convert("RGB")
        dress, engine = _dress_fn(garment, category)
        dresser = LiveDresser(dress, key_interval=float(settings.live_key_interval))
        if record:
            captures.save_file(trial_id, "garment.jpg", g_bytes)
            captures.update_meta(trial_id, kind="live", category=category, store_id=claims.get("store_id"),
                                 engine=engine, consent=True, gender=start.get("gender"),
                                 user_agent=ws.headers.get("user-agent", "")[:200])
        await ws.send_text(json.dumps({"type": "ready", "max_seconds": max_seconds,
                                       "trial_id": trial_id if record else None, "engine": engine}))

        while True:
            msg = await asyncio.wait_for(ws.receive(), timeout=30)
            if msg.get("type") == "websocket.disconnect":
                break
            data = msg.get("bytes")
            if not data:
                continue
            now = time.time()
            if t_first is None:
                t_first = now
            if now - t_first > max_seconds:
                await ws.send_text(json.dumps({"type": "end", "reason": "time"}))
                break
            arr = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
            if arr is None:
                continue
            rgb = cv2.cvtColor(arr, cv2.COLOR_BGR2RGB)
            out = await run_in_threadpool(dresser.process, rgb)
            ok, jpg = cv2.imencode(".jpg", cv2.cvtColor(out, cv2.COLOR_RGB2BGR), [cv2.IMWRITE_JPEG_QUALITY, 80])
            await ws.send_bytes(jpg.tobytes())
            frames += 1
            if record and now - last_save >= SAVE_EVERY_S:
                last_save = now
                captures.save_file(trial_id, f"live_in/{frames:06d}.jpg", data)
            if now - last_stats > 2:
                last_stats = now
                keepalive.touch(exist_ok=True)
                st = dresser.stats
                await ws.send_text(json.dumps({"type": "stats", "keyframes": st["keyframes"],
                                               "key_ms": st["key_ms"][-1] if st["key_ms"] else None,
                                               "frame_ms": float(np.median(st["frame_ms"][-30:]))}))
    except (WebSocketDisconnect, asyncio.TimeoutError):
        pass
    except Exception:
        log.exception("live session failed")
        try:
            await ws.send_text(json.dumps({"type": "end", "reason": "error"}))
        except Exception:
            pass
    finally:
        _active -= 1
        used = (time.time() - t_first) if t_first else 0.0
        if record:
            captures.update_meta(trial_id, live_seconds=round(used, 1), live_frames=frames)
        log.info("live ws end frames=%s secs=%.1f", frames, used)
        try:
            await ws.close()
        except Exception:
            pass
