"""Automated garment prep: background removal + category guess. CPU-friendly."""
from __future__ import annotations

import io
from pathlib import Path

from PIL import Image

_CATEGORY_HINTS = {
    "tops": ["shirt", "tee", "t-shirt", "top", "blouse", "hoodie", "jacket",
             "sweater", "kurta"],
    "bottoms": ["jeans", "trouser", "pant", "short", "skirt", "legging"],
    "one-pieces": ["dress", "gown", "jumpsuit", "saree", "sari", "robe"],
}


def guess_category(name: str) -> str:
    n = name.lower()
    for cat, words in _CATEGORY_HINTS.items():
        if any(w in n for w in words):
            return cat
    return "tops"  # safe default; store can override in the widget


def prepare_garment(raw: bytes, filename: str, out_dir: str) -> dict:
    """Remove background, normalize, save. Returns metadata for the widget."""
    try:
        from rembg import remove  # lazy import; heavy
        cut = remove(raw)  # bytes PNG with alpha
        img = Image.open(io.BytesIO(cut)).convert("RGBA")
    except Exception:
        # Fallback: keep original if rembg unavailable (e.g. during CPU-only dev)
        img = Image.open(io.BytesIO(raw)).convert("RGBA")

    # Flatten onto white for models that expect RGB product shots.
    bg = Image.new("RGBA", img.size, (255, 255, 255, 255))
    flat = Image.alpha_composite(bg, img).convert("RGB")
    if max(flat.size) > 1024:
        flat.thumbnail((1024, 1024))

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    stem = Path(filename).stem or "garment"
    path = out / f"{stem}.png"
    flat.save(path)
    return {
        "garment_path": str(path),
        "category": guess_category(filename),
        "width": flat.width,
        "height": flat.height,
    }
