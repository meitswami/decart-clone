"""Photo try-on: FASHN VTON v1.5 (Apache-2.0) + 4K composite (pipeline/hires.py).

Three ways this runs, picked automatically:
  1. GPU_WORKER_URL set (the VPS): forward to the GPU worker over HTTPS.
  2. Weights present on this machine (the GPU box): run the model here.
  3. Neither (CPU dev box): return a labelled stub so the API can be demoed.
"""
from __future__ import annotations

import inspect
import io
import logging
import os
import threading
import time
from pathlib import Path

from PIL import Image, ImageDraw, ImageOps

log = logging.getLogger("tryon.photo")

WEIGHTS_DIR = Path(os.environ.get("FASHN_WEIGHTS_DIR", "models/fashn-vton-1.5/weights"))
GPU_WORKER_URL = os.environ.get("GPU_WORKER_URL", "").rstrip("/")
GPU_WORKER_TOKEN = os.environ.get("GPU_WORKER_TOKEN", "")
MODEL_SIZE = (int(os.environ.get("FASHN_W", 576)), int(os.environ.get("FASHN_H", 864)))

# Quality presets: diffusion steps / guidance. "live" is for live keyframes.
PRESETS = {
    "live": {"num_timesteps": 8, "guidance_scale": 1.5},
    "fast": {"num_timesteps": 20, "guidance_scale": 1.5},
    "balanced": {"num_timesteps": 30, "guidance_scale": 1.5},
    "best": {"num_timesteps": 50, "guidance_scale": 1.5},
}

_PIPE = None
_PIPE_LOCK = threading.Lock()
_UPSCALER = None


def _available() -> bool:
    return (WEIGHTS_DIR / "model.safetensors").exists()


def load():
    """Load FASHN once and keep it warm."""
    global _PIPE
    with _PIPE_LOCK:
        if _PIPE is None:
            from fashn_vton import TryOnPipeline
            t = time.time()
            _PIPE = TryOnPipeline(weights_dir=str(WEIGHTS_DIR))
            log.info("FASHN loaded in %.1fs", time.time() - t)
    return _PIPE


def load_upscaler():
    """Real-ESRGAN x4plus (BSD-3) via spandrel (MIT), tiled so 4K fits in VRAM.
    None means Lanczos fallback (no GPU / not installed)."""
    global _UPSCALER
    if _UPSCALER is not None:
        return _UPSCALER or None
    try:
        import numpy as np
        import spandrel
        import torch
        if not torch.cuda.is_available():
            raise RuntimeError("no CUDA")
        path = Path(os.environ.get("ESRGAN_WEIGHTS", "models/RealESRGAN_x4plus.pth"))
        model = spandrel.ModelLoader().load_from_file(str(path)).cuda().eval()
        half = model.supports_half
        if half:
            model.half()
        scale = model.scale
        TILE, PAD = 384, 16

        @torch.inference_mode()
        def run(img_rgb):
            x = torch.from_numpy(np.ascontiguousarray(img_rgb)).cuda().permute(2, 0, 1)[None]
            x = (x.half() if half else x.float()) / 255.0
            _, _, h, w = x.shape
            out = torch.zeros((1, 3, h * scale, w * scale), device="cuda", dtype=x.dtype)
            for y0 in range(0, h, TILE):
                for x0 in range(0, w, TILE):
                    y1, x1 = min(y0 + TILE, h), min(x0 + TILE, w)
                    py0, px0 = max(y0 - PAD, 0), max(x0 - PAD, 0)
                    py1, px1 = min(y1 + PAD, h), min(x1 + PAD, w)
                    t = model(x[:, :, py0:py1, px0:px1])
                    oy, ox = (y0 - py0) * scale, (x0 - px0) * scale
                    out[:, :, y0 * scale:y1 * scale, x0 * scale:x1 * scale] = \
                        t[:, :, oy:oy + (y1 - y0) * scale, ox:ox + (x1 - x0) * scale]
            return (out[0].clamp(0, 1) * 255).round().byte().permute(1, 2, 0).cpu().numpy()
        _UPSCALER = run
        log.info("Real-ESRGAN x%s loaded (half=%s)", scale, half)
    except Exception as e:  # not installed / no weights / no GPU
        log.warning("Real-ESRGAN unavailable (%s); using Lanczos", e)
        _UPSCALER = False
    return _UPSCALER or None


_PARSER = None
_PARSER_LOCK = threading.Lock()


def load_parser():
    """fashn-human-parser (installed with FASHN). None if unavailable."""
    global _PARSER
    with _PARSER_LOCK:
        if _PARSER is None:
            try:
                from fashn_human_parser import FashnHumanParser
                _PARSER = FashnHumanParser()
                log.info("human parser loaded")
            except Exception as e:
                log.warning("human parser unavailable (%s); using difference masks", e)
                _PARSER = False
    return _PARSER or None


def person_visible(image_bytes: bytes) -> bool | None:
    """Is a person in this photo? (human parser). None if the parser is unavailable."""
    parser = load_parser()
    if parser is None:
        return None
    import numpy as np
    img = ImageOps.exif_transpose(Image.open(io.BytesIO(image_bytes))).convert("RGB")
    lab = parser.predict(img)
    return bool(np.isin(lab, [1, 2, 12, 13, 16]).mean() > 0.02)  # face, hair, arms, hands, torso


def detect_garment_photo_type(garment) -> str:
    """'model' if a person is visible in the garment photo (face/arms/hands/legs),
    else 'flat-lay'. Picking the wrong one makes the model blend the wearer's
    skin into the garment (seen on the first real run)."""
    parser = load_parser()
    if parser is None:
        return "flat-lay"
    import numpy as np
    lab = parser.predict(garment)
    person_px = np.isin(lab, [1, 12, 13, 14]).mean()  # face, arms, hands, legs
    return "model" if person_px > 0.01 else "flat-lay"


def garment_mask_fn(category: str):
    """Returns mask_fn(before_rgb, after_rgb) for hires/live, or None."""
    parser = load_parser()
    if parser is None:
        return None
    from .hires import semantic_mask
    lock = threading.Lock()

    def parse(img):
        with lock:
            return parser.predict(img)
    return lambda before, after: semantic_mask(before, after, category, parse)


def _call_model(person: Image.Image, garment: Image.Image, category: str,
                garment_photo_type: str, quality: str, seed: int) -> Image.Image:
    pipe = load()
    kwargs = {"person_image": person, "garment_image": garment, "category": category,
              "garment_photo_type": garment_photo_type, "seed": seed,
              "segmentation_free": True, **PRESETS.get(quality, PRESETS["balanced"])}
    # Pass only arguments this FASHN version accepts (API may evolve).
    params = inspect.signature(pipe.__call__).parameters
    if not any(p.kind == p.VAR_KEYWORD for p in params.values()):
        kwargs = {k: v for k, v in kwargs.items() if k in params}
    return pipe(**kwargs).images[0]


def run_local(person_bytes: bytes, garment_bytes: bytes, category: str = "tops", *,
              garment_photo_type: str = "auto", quality: str = "balanced",
              target_long_side: int | None = 3840, seed: int = 42) -> tuple[bytes, dict]:
    from .hires import compose_hires
    # Phone photos carry their rotation in EXIF; apply it or people come out sideways.
    person = ImageOps.exif_transpose(Image.open(io.BytesIO(person_bytes))).convert("RGB")
    garment = ImageOps.exif_transpose(Image.open(io.BytesIO(garment_bytes))).convert("RGB")
    t = time.time()
    timing = {}

    if garment_photo_type not in ("model", "flat-lay"):
        garment_photo_type = detect_garment_photo_type(garment)
    timing["garment_photo_type"] = garment_photo_type

    def model(p_low: Image.Image) -> Image.Image:
        t0 = time.time()
        out = _call_model(p_low, garment, category, garment_photo_type, quality, seed)
        timing["model_s"] = round(time.time() - t0, 2)
        return out

    final, info = compose_hires(person, model, model_size=MODEL_SIZE,
                                upscaler=load_upscaler(), target_long_side=target_long_side,
                                mask_fn=garment_mask_fn(category))
    info.update(timing, total_s=round(time.time() - t, 2), quality=quality)
    buf = io.BytesIO()
    final.save(buf, "JPEG", quality=95, subsampling=0)  # 4K PNGs are 20+ MB
    return buf.getvalue(), info


def run_remote(person_bytes: bytes, garment_bytes: bytes, category: str, *,
               url: str | None = None, token: str | None = None, **opts) -> tuple[bytes, dict]:
    import json

    import httpx
    files = {"person": ("person.jpg", person_bytes), "garment": ("garment.jpg", garment_bytes)}
    data = {"category": category, **{k: str(v) for k, v in opts.items() if v is not None}}
    r = httpx.post(f"{(url or GPU_WORKER_URL).rstrip('/')}/photo", files=files, data=data, timeout=240,
                   headers={"Authorization": f"Bearer {token or GPU_WORKER_TOKEN}",
                            "User-Agent": "vto-server/1.0"})
    r.raise_for_status()
    return r.content, json.loads(r.headers.get("x-tryon-info", "{}"))


def run(person_png: bytes, garment_png: bytes, category: str, **opts) -> bytes:
    """person + garment + category -> dressed image bytes (JPEG, up to 4K)."""
    if GPU_WORKER_URL:
        return run_remote(person_png, garment_png, category, **opts)[0]
    if _available():
        return run_local(person_png, garment_png, category, **opts)[0]
    # Dev stub: overlay a label so the pipeline is visibly wired end-to-end.
    person = ImageOps.exif_transpose(Image.open(io.BytesIO(person_png))).convert("RGB")
    d = ImageDraw.Draw(person)
    d.rectangle([0, 0, person.width, 28], fill=(0, 0, 0))
    d.text((6, 6), f"[photo try-on stub • {category}]", fill=(255, 255, 255))
    buf = io.BytesIO()
    person.save(buf, "JPEG", quality=90)
    return buf.getvalue()
