"""FastAPI app: the VPS layer. Owns tokens, photo, live signaling, limits."""
from __future__ import annotations

import httpx
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from pathlib import Path

from .config import settings
from . import limits, consent
from pipeline import garment as garment_mod
from pipeline import photo_tryon

app = FastAPI(title="Virtual Try-On")


@app.get("/health")
def health():
    return {"ok": True, "brand": settings.brand_name,
            "live_backend": settings.live_backend}


@app.get("/usage")
def usage():
    return limits.usage_snapshot()


@app.post("/garment/prepare")
async def garment_prepare(file: UploadFile = File(...),
                          store_id: str = Form(...)):
    raw = await file.read()
    out = Path(settings.recordings_dir).parent / "garments" / store_id
    info = garment_mod.prepare_garment(raw, file.filename or "g.png", str(out))
    return info


@app.post("/photo/tryon")
async def photo_tryon_ep(person: UploadFile = File(...),
                         garment: UploadFile = File(...),
                         category: str = Form("tops")):
    p = await person.read()
    g = await garment.read()
    out = photo_tryon.run(p, g, category)
    return Response(content=out, media_type="image/png")


@app.post("/live/token")
async def live_token(store_id: str = Form(...),
                     consented: bool = Form(False),
                     is_adult: bool = Form(True)):
    """Start a live session. Enforces limits + consent. Returns a client token.
    For LIVE_BACKEND=decart the real key stays here; browser gets a scoped token.
    """
    try:
        limits.check_and_reserve_session(store_id)
        consent.require_consent(consented, is_adult)
    except (limits.LimitError, consent.ConsentError) as e:
        raise HTTPException(status_code=429, detail=str(e))

    if settings.live_backend == "decart":
        if not settings.decart_api_key:
            raise HTTPException(500, "DECART_API_KEY not set")
        # TODO: call Decart's session-token endpoint server-side and return a
        # short-lived, domain-scoped client token + model id. Never return the
        # raw DECART_API_KEY. See decart docs /getting-started.
        async with httpx.AsyncClient(timeout=15) as client:  # noqa: F841
            # placeholder shape; adjust to Decart's actual token API
            return {"backend": "decart", "model": settings.decart_model,
                    "max_seconds": settings.max_session_seconds,
                    "client_token": "TODO_scoped_token"}
    else:
        return {"backend": "local", "engine_url": settings.gpu_engine_url,
                "max_seconds": settings.max_session_seconds}


@app.post("/live/end")
async def live_end(store_id: str = Form(...), seconds: float = Form(0.0)):
    limits.record_seconds(store_id, seconds)
    return {"ok": True}


app.mount("/", StaticFiles(directory="web", html=True), name="web")
