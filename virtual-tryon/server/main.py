"""FastAPI app: the site layer. Owns tokens, photo, live, analysis, captures, limits."""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from pathlib import Path

from fastapi import FastAPI, File, Form, Request, UploadFile, WebSocket
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

from .config import settings
from . import captures, consent, limits, live_ws, sessions
from .gpu_pool import pool as gpu
from .decart_tokens import DecartTokenError, mint_client_token
from .engine_ticket import TicketError, mint_ticket
from pipeline import garment as garment_mod
from pipeline import person_analysis, photo_tryon

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("tryon")

app = FastAPI(title="Virtual Try-On", docs_url=None, redoc_url=None)

_HERE = Path(__file__).resolve().parent.parent
_WEB_DIR = _HERE / "web"
_SAMPLES_DIR = _WEB_DIR / "garments"  # store catalogue: catalog.json + images + thumbs


@app.middleware("http")
async def security_headers(request: Request, call_next):
    resp = await call_next(request)
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    resp.headers.setdefault("Permissions-Policy", "camera=(self), microphone=(), geolocation=()")
    if request.method != "GET" or request.url.path.startswith(("/live", "/photo", "/analyze")):
        _touch()
    return resp


@app.on_event("startup")
def _warm() -> None:
    """Load the models in the background so the first shopper doesn't wait."""
    import threading

    def go():
        try:
            if photo_tryon._available() and not _pool():
                photo_tryon.load()
                photo_tryon.load_upscaler()
                photo_tryon.load_parser()
            if person_analysis.available() and not _pool():
                person_analysis._load()
        except Exception:
            log.exception("model warm-up failed")
    threading.Thread(target=go, daemon=True).start()


@app.on_event("startup")
async def _gpu_guard() -> None:
    if settings.gpu_mode == "runpod":
        import asyncio
        asyncio.create_task(gpu.guard())


def _touch() -> None:
    """Real use keeps the GPU box awake (see scripts/pod_watchdog.sh)."""
    try:
        Path(settings.keepalive_file).touch(exist_ok=True)
    except OSError:
        pass


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


def _pool() -> bool:
    return settings.gpu_mode == "runpod"


def _photo_ready() -> bool:
    return _pool() or bool(photo_tryon.GPU_WORKER_URL) or photo_tryon._available()


def _asleep() -> JSONResponse:
    return JSONResponse({"error": "gpu_asleep", "message": "Waking up the try-on servers."},
                        status_code=503, headers={"Cache-Control": "no-store"})


@app.get("/health")
def health():
    return {"ok": True, "brand": settings.brand_name, "live_backend": settings.live_backend,
            "photo_model": _photo_ready()}


@app.get("/config")
def public_config():
    """What the page needs to know. Never includes secrets or provider names."""
    ready = bool(settings.engine_secret) if settings.live_backend == "local" else bool(settings.decart_api_key)
    return {"brand": settings.brand_name,
            "live_ready": ready,
            "live_backend": settings.live_backend,
            "live_transport": settings.live_transport,
            "max_session_seconds": settings.max_session_seconds,
            "access_code_required": bool(settings.access_code),
            "recording_enabled": settings.recording_enabled,
            "photo_model_ready": _photo_ready(),
            "analysis_ready": _pool() or person_analysis.available(),
            "gpu_mode": settings.gpu_mode,
            "samples": _SAMPLES_DIR.exists()}


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


# ---------- person analysis (AI gender estimate) ----------

@app.post("/analyze/person")
async def analyze_person(image: UploadFile = File(...)):
    data = await image.read()
    if len(data) > 25_000_000:
        return _err(413, "too_large", "Photos must be under 25 MB.")
    if _pool():  # only when the GPU is already awake; never wakes it by itself
        gpu.touch()
        if not await gpu.is_ready():
            return {"available": True, "asleep": True}
        import httpx
        try:
            async with httpx.AsyncClient(timeout=60) as c:
                r = await c.post(f"{gpu.worker_url()}/analyze", files={"image": ("p.jpg", data)},
                                 headers={"Authorization": f"Bearer {settings.worker_token}",
                                          "User-Agent": "vto-server/1.0"})
            return r.json() if r.status_code == 200 else {"available": False}
        except Exception:
            log.exception("remote analysis failed")
            return {"available": False}
    if not person_analysis.available():
        return {"available": False}
    try:
        res = await run_in_threadpool(person_analysis.analyze, data)
        parser = photo_tryon.load_parser() if photo_tryon._available() else None
        if parser is not None:
            res["person_found"] = await run_in_threadpool(_person_visible, parser, data)
        if not res["person_found"]:
            res.update(gender="unsure", confidence=0.0)
    except Exception:
        log.exception("analysis failed")
        return {"available": False}
    return {"available": True, **res}


def _person_visible(parser, data: bytes) -> bool:
    import io as _io

    import numpy as np
    from PIL import Image, ImageOps
    img = ImageOps.exif_transpose(Image.open(_io.BytesIO(data))).convert("RGB")
    lab = parser.predict(img)
    return bool(np.isin(lab, [1, 2, 12, 13, 16]).mean() > 0.02)  # face, hair, arms, hands, torso


# ---------- photo try-on ----------

@app.post("/photo/tryon")
async def photo_tryon_ep(person: UploadFile = File(...),
                         garment: UploadFile = File(...),
                         category: str = Form("tops"),
                         garment_photo_type: str = Form("auto"),
                         quality: str = Form("balanced"),
                         consented: bool = Form(False),
                         is_adult: bool = Form(False),
                         gender_estimate: str = Form(""),
                         gender_choice: str = Form(""),
                         source: str = Form("")):
    if category not in ("tops", "bottoms", "one-pieces"):
        return _err(422, "bad_category", "Garment type must be top, bottom or one-piece.")
    if garment_photo_type not in ("flat-lay", "model", "auto"):
        garment_photo_type = "auto"
    p = await person.read()
    g = await garment.read()
    if len(p) > 25_000_000 or len(g) > 25_000_000:
        return _err(413, "too_large", "Photos must be under 25 MB.")
    t = time.time()
    if _pool():
        gpu.touch()
        if not await gpu.is_ready():
            return _asleep()
    try:
        if _pool():
            out, _info = await run_in_threadpool(
                lambda: photo_tryon.run_remote(p, g, category, url=gpu.worker_url(), token=settings.worker_token,
                                               garment_photo_type=garment_photo_type, quality=quality))
        else:
            out = await run_in_threadpool(photo_tryon.run, p, g, category,
                                          garment_photo_type=garment_photo_type, quality=quality)
    except Exception:
        log.exception("photo try-on failed")
        return _err(502, "photo_failed", "Couldn't create the try-on photo. Please try again.")
    headers = {"Cache-Control": "no-store", "X-Tryon-Seconds": f"{time.time() - t:.1f}"}
    if consent.should_record(consented, is_adult):
        trial = captures.new_trial()
        captures.save_photo_trial(trial, person=p, garment=g, result=out, meta={
            "category": category, "garment_photo_type": garment_photo_type, "quality": quality,
            "gender_estimate": gender_estimate[:20], "gender_choice": gender_choice[:20],
            "source": source[:20], "seconds": round(time.time() - t, 1), "consent": True,
            "model": "fashn-vton-1.5" if _photo_ready() else "stub"})
        headers["X-Trial-Id"] = trial
    kind = "image/jpeg" if out[:2] == b"\xff\xd8" else "image/png"
    return Response(content=out, media_type=kind, headers=headers)


# ---------- live ----------

@app.post("/live/token")
async def live_token(request: Request,
                     store_id: str = Form(...),
                     consented: bool = Form(False),
                     is_adult: bool = Form(False),
                     engine: str = Form("")):
    """Start a live session: checks origin, access code, per-visitor and
    per-store limits, then hands out a short-lived key (hosted backend) or a
    signed ticket (our GPU engine). The real keys stay here.
    engine="gpu" / "hosted" overrides LIVE_DEFAULT_ENGINE for this session.
    """
    origin = settings.origin
    if origin and (request.headers.get("origin") or "").lower() != origin:
        return _err(403, "bad_origin", "Requests must come from the try-on page.")
    if settings.access_code and not _same_secret(
            request.headers.get("x-access-code", ""), settings.access_code):
        return _err(401, "access_code", "That access code isn't right.")

    store_id = store_id.strip()[:64] or "demo"
    can_gpu = _pool() or settings.live_backend == "local"
    want_hosted = engine == "hosted" or (engine != "gpu" and (
        settings.live_default_engine == "hosted" or not can_gpu))
    use_hosted = settings.live_backend == "decart" and bool(settings.decart_api_key) and want_hosted
    if not use_hosted and _pool():
        gpu.touch()
        if not await gpu.is_ready():
            return _asleep()  # page wakes the GPU (with a loader) and asks again
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

    if use_hosted:
        try:
            token = await mint_client_token(
                model=settings.decart_model, origin=origin,
                max_seconds=settings.max_session_seconds,
                metadata={"store_id": store_id})
        except DecartTokenError as e:
            limits.release_session(store_id)
            return _err(e.status, "live_unavailable", e.message)
        sid = sessions.start(store_id, record)
        trial = captures.new_trial() if record else None
        if trial:  # hosted engine: we may keep the shopper's own camera input only
            captures.update_meta(trial, kind="live", engine="hosted", consent=True, store_id=store_id)
        log.info("live session %s started for store %s", sid[:8], store_id)
        return JSONResponse({**base, "backend": "decart", "session_id": sid, "trial_id": trial,
                             "model": settings.decart_model,
                             "client_token": token["apiKey"],
                             "expires_at": token["expiresAt"]},
                            headers={"Cache-Control": "no-store"})

    sid = sessions.start(store_id, record)
    trial = captures.new_trial() if record else None
    try:
        ticket = mint_ticket(settings.engine_secret, session_id=sid, store_id=store_id,
                             max_seconds=settings.max_session_seconds, record=record,
                             trial_id=trial)
    except TicketError:
        sessions.end(sid)
        limits.release_session(store_id)
        return _err(503, "live_unavailable", "Live try-on isn't available right now.")
    if trial:
        captures.update_meta(trial, kind="live", engine="gpu", consent=True, store_id=store_id)
    engine_url = gpu.worker_url() if _pool() else settings.gpu_engine_url.rstrip("/")
    return JSONResponse({**base, "backend": "local", "transport": settings.live_transport,
                         "session_id": sid, "trial_id": trial,
                         "engine_url": engine_url, "ticket": ticket,
                         "ice_servers": json.loads(settings.ice_servers)},
                        headers={"Cache-Control": "no-store"})


@app.get("/gpu/status")
async def gpu_status():
    if not _pool():
        return {"state": "ready" if _photo_ready() else "off"}
    return JSONResponse(await gpu.status(), headers={"Cache-Control": "no-store"})


@app.post("/gpu/wake")
async def gpu_wake(request: Request):
    """Called by the page when a shopper presses Try it on / Start live."""
    if not _pool():
        return {"state": "ready" if _photo_ready() else "off"}
    return JSONResponse(await gpu.wake(_client_ip(request)), headers={"Cache-Control": "no-store"})


@app.websocket("/live/ws")
async def live_websocket(ws: WebSocket):
    await live_ws.handle(ws)


@app.post("/live/end")
async def live_end(session_id: str = Form(""), store_id: str = Form(""),
                   seconds: float = Form(0.0)):
    """Close a session. Duration is measured on the server, not trusted from
    the browser; store_id/seconds are accepted but ignored (old clients)."""
    used = sessions.end(session_id) if session_id else None
    return {"ok": True, "charged_seconds": round(used, 1) if used is not None else None}


# ---------- captures (consented training data) ----------

@app.post("/captures/video")
async def capture_video(trial_id: str = Form(...), video: UploadFile = File(...),
                        kind: str = Form("output")):
    """Browser-recorded try-on video for a trial the shopper already consented to."""
    try:
        meta = captures._dir(trial_id, create=False) / "meta.json"
    except ValueError:
        return _err(422, "bad_trial", "Unknown session.")
    if not meta.exists() or not json.loads(meta.read_text()).get("consent"):
        return _err(403, "no_consent", "This session wasn't saved.")
    data = await video.read()
    if len(data) > 200_000_000:
        return _err(413, "too_large", "Video must be under 200 MB.")
    ext = "mp4" if (video.content_type or "").endswith("mp4") else "webm"
    # "camera" = the shopper's own camera input (all we keep from the hosted engine)
    captures.save_file(trial_id, f"{'camera' if kind == 'camera' else 'video'}.{ext}", data)
    captures.update_meta(trial_id, video_bytes=len(data))
    return {"ok": True}


@app.post("/captures/upload")
async def capture_upload(request: Request, trial_id: str = Form(...), name: str = Form(""),
                         meta: str = Form(""), file: UploadFile | None = File(None)):
    """The GPU box sends live captures here (Bearer WORKER_TOKEN), so the
    gallery has everything in one place. Only for consented trials."""
    if not settings.worker_token or not _same_secret(
            request.headers.get("authorization", "").removeprefix("Bearer ").strip(), settings.worker_token):
        return _err(401, "auth", "Not authorised.")
    try:
        meta_file = captures._dir(trial_id, create=False) / "meta.json"
    except ValueError:
        return _err(422, "bad_trial", "Unknown session.")
    if not meta_file.exists() or not json.loads(meta_file.read_text()).get("consent"):
        return _err(403, "no_consent", "This session isn't being saved.")
    if meta:
        fields = json.loads(meta)
        fields.pop("consent", None)
        captures.update_meta(trial_id, **fields)
    if file is not None and name:
        data = await file.read()
        if len(data) > 20_000_000:
            return _err(413, "too_large", "Too large.")
        captures.save_file(trial_id, name, data)
    return {"ok": True}


def _admin_ok(request: Request) -> bool:
    if not settings.admin_token:
        return False
    given = request.query_params.get("token") or request.cookies.get("vto_admin", "")
    return _same_secret(given, settings.admin_token)


def _admin_resp(request: Request, resp: Response) -> Response:
    if request.query_params.get("token"):
        resp.set_cookie("vto_admin", request.query_params["token"], httponly=True, secure=True,
                        samesite="strict", max_age=7 * 86400)
    return resp


@app.get("/admin")
def admin_page(request: Request):
    if not _admin_ok(request):
        return HTMLResponse("<h1>Not authorised</h1>", status_code=401)
    return _admin_resp(request, FileResponse(_WEB_DIR / "admin.html", headers={"Cache-Control": "no-store"}))


@app.get("/admin/api/captures")
def admin_list(request: Request):
    if not _admin_ok(request):
        return _err(401, "auth", "Not authorised.")
    return {"stats": captures.stats(), "trials": captures.list_trials()}


@app.get("/admin/file/{day}/{trial_id}/{name:path}")
def admin_file(request: Request, day: str, trial_id: str, name: str):
    if not _admin_ok(request):
        return _err(401, "auth", "Not authorised.")
    try:
        return FileResponse(captures.file_path(day, trial_id, name), headers={"Cache-Control": "private"})
    except (ValueError, FileNotFoundError):
        return _err(404, "missing", "Not found.")


@app.get("/admin/export.zip")
def admin_export(request: Request):
    if not _admin_ok(request):
        return _err(401, "auth", "Not authorised.")
    return Response(captures.export_zip(), media_type="application/zip",
                    headers={"Content-Disposition": 'attachment; filename="vto-captures.zip"'})


if _SAMPLES_DIR.exists():
    app.mount("/garments", StaticFiles(directory=str(_SAMPLES_DIR)), name="samples")
app.mount("/", StaticFiles(directory=str(_WEB_DIR), html=True), name="web")
