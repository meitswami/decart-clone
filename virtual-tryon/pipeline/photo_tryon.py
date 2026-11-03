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

from PIL import Image, ImageDraw

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
    """Real-ESRGAN x4plus (BSD-3) if installed; None means Lanczos fallback."""
    global _UPSCALER
    if _UPSCALER is not None:
        return _UPSCALER or None
    try:
        import numpy as np
        import torch
        from basicsr.archs.rrdbnet_arch import RRDBNet
        from realesrgan import RealESRGANer
        path = Path(os.environ.get("ESRGAN_WEIGHTS", "models/RealESRGAN_x4plus.pth"))
        net = RRDBNet(num_in_ch=3, num_out_ch=3, num_feat=64, num_block=23, num_grow_ch=32, scale=4)
        up = RealESRGANer(scale=4, model_path=str(path), model=net, tile=512, tile_pad=16,
                          half=torch.cuda.is_available())

        def run(img_rgb):
            out, _ = up.enhance(np.ascontiguousarray(img_rgb[..., ::-1]), outscale=4)  # BGR in/out
            return out[..., ::-1].copy()
        _UPSCALER = run
    except Exception as e:  # not installed / no weights
        log.warning("Real-ESRGAN unavailable (%s); using Lanczos", e)
        _UPSCALER = False
    return _UPSCALER or None


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
              garment_photo_type: str = "flat-lay", quality: str = "balanced",
              target_long_side: int | None = 3840, seed: int = 42) -> tuple[bytes, dict]:
    from .hires import compose_hires
    person = Image.open(io.BytesIO(person_bytes)).convert("RGB")
    garment = Image.open(io.BytesIO(garment_bytes)).convert("RGB")
    t = time.time()
    timing = {}

    def model(p_low: Image.Image) -> Image.Image:
        t0 = time.time()
        out = _call_model(p_low, garment, category, garment_photo_type, quality, seed)
        timing["model_s"] = round(time.time() - t0, 2)
        return out

    final, info = compose_hires(person, model, model_size=MODEL_SIZE,
                                upscaler=load_upscaler(), target_long_side=target_long_side)
    info.update(timing, total_s=round(time.time() - t, 2), quality=quality)
    buf = io.BytesIO()
    final.save(buf, "JPEG", quality=95, subsampling=0)  # 4K PNGs are 20+ MB
    return buf.getvalue(), info


def run_remote(person_bytes: bytes, garment_bytes: bytes, category: str, **opts) -> tuple[bytes, dict]:
    import json

    import httpx
    files = {"person": ("person.jpg", person_bytes), "garment": ("garment.jpg", garment_bytes)}
    data = {"category": category, **{k: str(v) for k, v in opts.items() if v is not None}}
    r = httpx.post(f"{GPU_WORKER_URL}/photo", files=files, data=data, timeout=180,
                   headers={"Authorization": f"Bearer {GPU_WORKER_TOKEN}",
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
    person = Image.open(io.BytesIO(person_png)).convert("RGB")
    d = ImageDraw.Draw(person)
    d.rectangle([0, 0, person.width, 28], fill=(0, 0, 0))
    d.text((6, 6), f"[photo try-on stub • {category}]", fill=(255, 255, 255))
    buf = io.BytesIO()
    person.save(buf, "PNG")
    return buf.getvalue()
