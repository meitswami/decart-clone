"""Local live engine over WebRTC (aiortc). Runs ON the GPU box.

  uvicorn webrtc.live_server:app --host 0.0.0.0 --port 8900

Flow: the VPS `/live/token` (LIVE_BACKEND=local) gives the browser a signed,
short-lived engine ticket. The browser POSTs its SDP offer + garment to
`/offer`; we answer, receive the camera track, run every frame through
LiveDresser (keyframe try-on + tracking) and send the dressed track back.
Sessions end at the ticket's max_seconds.

Ticket = base64url(json) + "." + HMAC-SHA256(ENGINE_SECRET), minted by
server/engine_ticket.py on the VPS. No ticket, no GPU time.
"""
from __future__ import annotations

import asyncio
import base64
import io
import logging
import os
import time
from pathlib import Path

import numpy as np
from aiortc import RTCPeerConnection, RTCSessionDescription, VideoStreamTrack
from aiortc.contrib.media import MediaRelay
from av import VideoFrame
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from PIL import Image
from pydantic import BaseModel

from pipeline.live_keyframe import LiveDresser
from server.engine_ticket import TicketError, verify_ticket

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("tryon.live")

ENGINE_SECRET = os.environ.get("ENGINE_SECRET", "")
ALLOWED_ORIGINS = [o for o in os.environ.get("ENGINE_ALLOWED_ORIGINS", "").split(",") if o]
KEEPALIVE = Path(os.environ.get("KEEPALIVE", "/workspace/.keepalive"))
MAX_SESSIONS = int(os.environ.get("ENGINE_MAX_SESSIONS", "2"))

app = FastAPI(title="VTO live engine", docs_url=None, redoc_url=None)
app.add_middleware(CORSMiddleware, allow_origins=ALLOWED_ORIGINS or [], allow_methods=["POST", "GET"],
                   allow_headers=["content-type"])
relay = MediaRelay()
sessions: set[RTCPeerConnection] = set()


def make_dress_fn(garment: Image.Image, category: str):
    """Keyframe try-on. FASHN in "live" quality when weights exist, else a
    visible dev stand-in so transport can be tested without the model."""
    from pipeline import photo_tryon
    if photo_tryon._available():
        def dress(frame_rgb: np.ndarray) -> np.ndarray:
            img = photo_tryon._call_model(Image.fromarray(frame_rgb), garment, category,
                                          "flat-lay", "live", seed=7)
            return np.asarray(img.convert("RGB").resize((frame_rgb.shape[1], frame_rgb.shape[0])))
        return dress

    def stub(frame_rgb: np.ndarray) -> np.ndarray:  # tint the middle third: proves the path
        out = frame_rgb.copy()
        h, w = out.shape[:2]
        sl = (slice(h // 3, h), slice(w // 3, 2 * w // 3))
        out[sl] = (out[sl] * 0.4 + np.array([200, 30, 60]) * 0.6).astype(np.uint8)
        return out
    return stub


class DressedTrack(VideoStreamTrack):
    kind = "video"

    def __init__(self, source, dresser: LiveDresser, max_seconds: int):
        super().__init__()
        self.source, self.dresser, self.max_seconds = source, dresser, max_seconds
        self.deadline: float | None = None  # starts at the first frame, not at signalling

    async def recv(self) -> VideoFrame:
        frame = await self.source.recv()
        if self.deadline is None:
            self.deadline = time.time() + self.max_seconds
        if time.time() > self.deadline:
            self.stop()
            raise asyncio.CancelledError
        rgb = frame.to_ndarray(format="rgb24")
        out = await asyncio.get_running_loop().run_in_executor(None, self.dresser.process, rgb)
        if self.dresser.stats["frames"] % 30 == 0:
            KEEPALIVE.touch(exist_ok=True)
        nf = VideoFrame.from_ndarray(out, format="rgb24")
        nf.pts, nf.time_base = frame.pts, frame.time_base
        return nf


class Offer(BaseModel):
    sdp: str
    type: str
    ticket: str
    garment_b64: str          # JPEG/PNG, base64
    category: str = "tops"


@app.get("/health")
def health():
    return {"ok": True, "sessions": len(sessions)}


@app.post("/offer")
async def offer(o: Offer):
    try:
        claims = verify_ticket(o.ticket, ENGINE_SECRET)
    except TicketError as e:
        raise HTTPException(401, str(e))
    if len(sessions) >= MAX_SESSIONS:
        raise HTTPException(429, "busy")
    if o.category not in ("tops", "bottoms", "one-pieces"):
        raise HTTPException(422, "bad category")
    try:
        garment = Image.open(io.BytesIO(base64.b64decode(o.garment_b64[:20_000_000]))).convert("RGB")
    except Exception:
        raise HTTPException(422, "bad garment image")

    pc = RTCPeerConnection()
    sessions.add(pc)
    max_seconds = int(claims["max_seconds"])
    dresser = LiveDresser(make_dress_fn(garment, o.category))
    log.info("session start store=%s max=%ss", claims.get("store_id"), claims["max_seconds"])

    @pc.on("track")
    def on_track(track):
        if track.kind == "video":
            pc.addTrack(DressedTrack(relay.subscribe(track), dresser, max_seconds))

    @pc.on("connectionstatechange")
    async def on_state():
        if pc.connectionState in ("failed", "closed", "disconnected"):
            await pc.close()
            sessions.discard(pc)
            log.info("session end frames=%s keyframes=%s", dresser.stats["frames"], dresser.stats["keyframes"])

    async def hard_stop():  # 30 s to connect, then the session cap, then out
        await asyncio.sleep(30 + max_seconds + 2)
        if pc in sessions:
            await pc.close()
            sessions.discard(pc)
    asyncio.create_task(hard_stop())

    await pc.setRemoteDescription(RTCSessionDescription(sdp=o.sdp, type=o.type))
    await pc.setLocalDescription(await pc.createAnswer())
    return {"sdp": pc.localDescription.sdp, "type": pc.localDescription.type,
            "max_seconds": int(claims["max_seconds"])}


@app.on_event("shutdown")
async def shutdown():
    await asyncio.gather(*(pc.close() for pc in list(sessions)))
    sessions.clear()
