"""FastAPI app: the VPS layer. Owns tokens, photo, live signaling, limits."""
from __future__ import annotations

import hashlib
import hmac
import logging
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

from .config import settings
from . import limits, consent, sessions
from .decart_tokens import DecartTokenError, mint_client_token
from .engine_ticket import TicketError, mint_ticket
from pipeline import garment as garment_mod
from pipeline import photo_tryon

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("tryon")

app = FastAPI(title="Virtual Try-On", docs_url=None, redoc_url=None)

_WEB_DIR = Path(__file__).resolve().parent.parent / "web"


@app.middleware("http")
async def security_headers(request: Request, call_next):
    resp = await call_next(request)
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    resp.headers.setdefault("Permissions-Policy", "camera=(self), microphone=(), geolocation=()")
    return resp


def _client_ip(request: Request) -> str:
    if settings.trust_proxy:
        real = request.headers.get("x-real-ip")
        if real:
            return real.strip()
        fwd = request.headers.get("x-forwarded-for")
        if fwd:
            return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _same_secret(given: str, expected: str) -> bool:
    a = hashlib.sha256(given.encode()).digest()
    b = hashlib.sha256(expected.encode()).digest()
    return hmac.compare_digest(a, b)


def _err(status: int, code: str, message: str) -> JSONResponse:
    # `detail` kept for older clients of this API.
    return JSONResponse({"error": code, "message": message, "detail": message},
                        status_code=status, headers={"Cache-Control": "no-store"})


@app.get("/health")
def health():
    return {"ok": True, "brand": settings.brand_name,
            "live_backend": settings.live_backend}


@app.get("/config")
def public_config():
    """What the page needs to know. Never includes secrets or provider names."""
    ready = bool(settings.engine_secret) if settings.live_backend == "local" else bool(settings.decart_api_key)
    return {"brand": settings.brand_name,
            "live_ready": ready,
            "max_session_seconds": settings.max_session_seconds,
            "access_code_required": bool(settings.access_code),
            "recording_enabled": settings.recording_enabled,
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
                         category: str = Form("tops"),
                         garment_photo_type: str = Form("flat-lay"),
                         quality: str = Form("balanced")):
    if category not in ("tops", "bottoms", "one-pieces"):
        return _err(422, "bad_category", "Garment type must be top, bottom or one-piece.")
    if garment_photo_type not in ("flat-lay", "model"):
        garment_photo_type = "flat-lay"
    p = await person.read()
    g = await garment.read()
    if len(p) > 25_000_000 or len(g) > 25_000_000:
        return _err(413, "too_large", "Photos must be under 25 MB.")
    try:
        out = await run_in_threadpool(photo_tryon.run, p, g, category,
                                      garment_photo_type=garment_photo_type, quality=quality)
    except Exception:
        log.exception("photo try-on failed")
        return _err(502, "photo_failed", "Couldn't create the try-on photo. Please try again.")
    kind = "image/jpeg" if out[:2] == b"\xff\xd8" else "image/png"
    return Response(content=out, media_type=kind, headers={"Cache-Control": "no-store"})


@app.post("/live/token")
async def live_token(request: Request,
                     store_id: str = Form(...),
                     consented: bool = Form(False),
                     is_adult: bool = Form(False)):
    """Start a live session: checks origin, access code, per-visitor and
    per-store limits, then hands out a short-lived key scoped to one model,
    this site's origin and MAX_SESSION_SECONDS. The real key stays here.
    """
    origin = settings.origin
    if origin and (request.headers.get("origin") or "").lower() != origin:
        return _err(403, "bad_origin", "Requests must come from the try-on page.")
    if settings.access_code and not _same_secret(
            request.headers.get("x-access-code", ""), settings.access_code):
        return _err(401, "access_code", "That access code isn't right.")

    store_id = store_id.strip()[:64] or "demo"
    wait = sessions.rate_limit_wait_minutes(_client_ip(request))
    if wait:
        return _err(429, "rate_limited",
                    f"You've started a lot of sessions. Try again in about {wait} min.")
    try:
        limits.check_and_reserve_session(store_id)
    except limits.LimitError as e:
        return _err(429, "daily_limit", e.reason)

    record = consent.should_record(consented, is_adult)
    base = {"max_seconds": settings.max_session_seconds, "record": record}

    if settings.live_backend == "decart":
        try:
            token = await mint_client_token(
                model=settings.decart_model, origin=origin,
                max_seconds=settings.max_session_seconds,
                metadata={"store_id": store_id})
        except DecartTokenError as e:
            limits.release_session(store_id)
            return _err(e.status, "live_unavailable", e.message)
        sid = sessions.start(store_id, record)
        log.info("live session %s started for store %s", sid[:8], store_id)
        return JSONResponse({**base, "backend": "decart", "session_id": sid,
                             "model": settings.decart_model,
                             "client_token": token["apiKey"],
                             "expires_at": token["expiresAt"]},
                            headers={"Cache-Control": "no-store"})

    sid = sessions.start(store_id, record)
    try:
        ticket = mint_ticket(settings.engine_secret, session_id=sid, store_id=store_id,
                             max_seconds=settings.max_session_seconds)
    except TicketError:
        sessions.end(sid)
        limits.release_session(store_id)
        return _err(503, "live_unavailable", "Live try-on isn't available right now.")
    import json as _json
    return JSONResponse({**base, "backend": "local", "session_id": sid,
                         "engine_url": settings.gpu_engine_url.rstrip("/"), "ticket": ticket,
                         "ice_servers": _json.loads(settings.ice_servers)},
                        headers={"Cache-Control": "no-store"})


@app.post("/live/end")
async def live_end(session_id: str = Form(""), store_id: str = Form(""),
                   seconds: float = Form(0.0)):
    """Close a session. Duration is measured on the server, not trusted from
    the browser; store_id/seconds are accepted but ignored (old clients)."""
    used = sessions.end(session_id) if session_id else None
    return {"ok": True, "charged_seconds": round(used, 1) if used is not None else None}


app.mount("/", StaticFiles(directory=str(_WEB_DIR), html=True), name="web")
