"""GPU worker: runs ON the RunPod box, behind RunPod's HTTPS proxy.

  uvicorn gpu.worker:app --host 0.0.0.0 --port 8000

The VPS server calls it with GPU_WORKER_URL=https://<pod>-8000.proxy.runpod.net
and GPU_WORKER_TOKEN (shared secret). Shoppers never talk to it directly.

  GET  /health   model/GPU status (no auth, no secrets)
  POST /photo    person + garment -> 4K JPEG   (Bearer token)
"""
from __future__ import annotations

import hmac
import json
import logging
import os
import threading
import time
from pathlib import Path

from fastapi import FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import Response
from starlette.concurrency import run_in_threadpool

from pipeline import photo_tryon

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("tryon.worker")

TOKEN = os.environ.get("WORKER_TOKEN", "")
KEEPALIVE = Path(os.environ.get("KEEPALIVE", "/workspace/.keepalive"))
_GPU_LOCK = threading.Lock()  # one generation at a time on one GPU

app = FastAPI(title="VTO GPU worker", docs_url=None, redoc_url=None)


def _auth(authorization: str | None) -> None:
    if not TOKEN:
        raise HTTPException(503, "worker token not configured")
    given = (authorization or "").removeprefix("Bearer ").strip()
    if not hmac.compare_digest(given.encode(), TOKEN.encode()):
        raise HTTPException(401, "bad token")


@app.on_event("startup")
def warm():
    if photo_tryon._available():
        photo_tryon.load()
        photo_tryon.load_upscaler()


@app.get("/health")
def health():
    gpu = None
    try:
        import torch
        if torch.cuda.is_available():
            gpu = torch.cuda.get_device_name(0)
    except Exception:
        pass
    return {"ok": True, "gpu": gpu, "weights": photo_tryon._available(),
            "model_loaded": photo_tryon._PIPE is not None,
            "esrgan": bool(photo_tryon._UPSCALER)}


def _photo_sync(p: bytes, g: bytes, category: str, gpt: str, quality: str, tls: int | None):
    with _GPU_LOCK:
        KEEPALIVE.touch(exist_ok=True)  # real work = not idle (watchdog)
        return photo_tryon.run_local(p, g, category, garment_photo_type=gpt,
                                     quality=quality, target_long_side=tls)


@app.post("/photo")
async def photo(person: UploadFile = File(...), garment: UploadFile = File(...),
                category: str = Form("tops"), garment_photo_type: str = Form("auto"),
                quality: str = Form("balanced"), target_long_side: int = Form(3840),
                authorization: str | None = Header(None)):
    _auth(authorization)
    if category not in ("tops", "bottoms", "one-pieces"):
        raise HTTPException(422, "bad category")
    t = time.time()
    img, info = await run_in_threadpool(_photo_sync, await person.read(), await garment.read(),
                                        category, garment_photo_type, quality,
                                        target_long_side or None)
    info["wall_s"] = round(time.time() - t, 2)
    log.info("photo %s", info)
    return Response(img, media_type="image/jpeg", headers={"x-tryon-info": json.dumps(info)})
