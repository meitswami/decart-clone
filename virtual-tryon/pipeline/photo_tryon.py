"""Photo try-on via FASHN VTON v1.5. Runs on the GPU box.

On a CPU-only box (no weights) this returns a clear stub so the API and web
client can be developed and demoed. On the GPU box with weights present it
runs the real model.
"""
from __future__ import annotations

import io
from pathlib import Path

from PIL import Image

_MODEL = None
_WEIGHTS_DIR = Path("models/fashn-vton-1.5")


def _available() -> bool:
    return _WEIGHTS_DIR.exists() and any(_WEIGHTS_DIR.glob("*.safetensors"))


def _load():
    global _MODEL
    if _MODEL is not None:
        return _MODEL
    # TODO(gpu): import and load FASHN per its repo README:
    #   from fashn_vton import FashnVTON
    #   _MODEL = FashnVTON.from_pretrained(str(_WEIGHTS_DIR), device="cuda",
    #                                      dtype="bf16")
    raise RuntimeError("FASHN not loaded")


def run(person_png: bytes, garment_png: bytes, category: str) -> bytes:
    """person + garment + category -> dressed image (PNG bytes)."""
    if not _available():
        # Dev stub: overlay a label so the pipeline is visibly wired end-to-end.
        person = Image.open(io.BytesIO(person_png)).convert("RGB")
        from PIL import ImageDraw
        d = ImageDraw.Draw(person)
        d.rectangle([0, 0, person.width, 28], fill=(0, 0, 0))
        d.text((6, 6), f"[photo try-on stub - {category}]", fill=(255, 255, 255))
        buf = io.BytesIO(); person.save(buf, "PNG"); return buf.getvalue()

    model = _load()
    # TODO(gpu): result = model.tryon(person_png, garment_png, category=category)
    #            return result_png_bytes
    raise NotImplementedError("wire FASHN .tryon() here")
