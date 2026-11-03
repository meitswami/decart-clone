"""4K output for photo try-on.

Try-on models run at a fixed, modest resolution. Upscaling their whole output
to 4K blurs the face and background and shifts colours. Instead:

  1. Pad the shopper's full-resolution photo to the model's aspect ratio.
  2. Run the model on a downscaled copy (exact pixel correspondence kept).
  3. Find the region the model actually changed (the garment) by comparing
     its output with its input, cleaned up and feathered.
  4. Upscale the model output (Real-ESRGAN when available, else Lanczos) and
     paste ONLY the changed region into the original photo.
  5. Optionally upscale the final composite to a 4K long side when the
     source photo was smaller (e.g. a 1080p webcam).

Face, hair, hands and background therefore stay the shopper's own pixels at
native resolution; only the garment is generated.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import cv2
import numpy as np
from PIL import Image

Upscaler = Callable[[np.ndarray], np.ndarray]  # RGB uint8 -> RGB uint8 (any scale)


@dataclass
class PadBox:
    left: int
    top: int
    width: int   # original width
    height: int  # original height


def pad_to_aspect(img: np.ndarray, aspect_w_over_h: float) -> tuple[np.ndarray, PadBox]:
    """Edge-replicate pad so width/height == aspect. Returns padded image + box."""
    h, w = img.shape[:2]
    if abs(w / h - aspect_w_over_h) < 1e-3:
        return img, PadBox(0, 0, w, h)
    if w / h > aspect_w_over_h:  # too wide -> pad top/bottom
        new_h = int(round(w / aspect_w_over_h))
        top = (new_h - h) // 2
        out = cv2.copyMakeBorder(img, top, new_h - h - top, 0, 0, cv2.BORDER_REPLICATE)
        return out, PadBox(0, top, w, h)
    new_w = int(round(h * aspect_w_over_h))  # too tall -> pad left/right
    left = (new_w - w) // 2
    out = cv2.copyMakeBorder(img, 0, 0, left, new_w - w - left, cv2.BORDER_REPLICATE)
    return out, PadBox(left, 0, w, h)


def change_mask(before: np.ndarray, after: np.ndarray, *, thresh: float = 14.0,
                min_area_frac: float = 0.002, feather_frac: float = 0.012) -> np.ndarray:
    """Soft mask (float32 0..1) of where `after` differs from `before` (same size, RGB).

    Uses perceptual (Lab) distance, removes speckle, fills holes inside the
    garment and feathers the edge so the paste-back is seamless.
    """
    a = cv2.cvtColor(before, cv2.COLOR_RGB2LAB).astype(np.float32)
    b = cv2.cvtColor(after, cv2.COLOR_RGB2LAB).astype(np.float32)
    d = np.sqrt(((a - b) ** 2).sum(axis=2))
    h, w = d.shape
    k = max(3, int(min(h, w) * 0.006) | 1)
    d = cv2.GaussianBlur(d, (k, k), 0)
    m = (d > thresh).astype(np.uint8)

    ks = max(3, int(min(h, w) * 0.015) | 1)
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (ks, ks))
    m = cv2.morphologyEx(m, cv2.MORPH_OPEN, kernel)
    m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, kernel, iterations=2)

    # Keep meaningful components only; fill holes (logos/prints may match the
    # old shirt colour in places, which would otherwise punch holes).
    n, labels, stats, _ = cv2.connectedComponentsWithStats(m, 8)
    keep = np.zeros_like(m)
    for i in range(1, n):
        if stats[i, cv2.CC_STAT_AREA] >= min_area_frac * h * w:
            keep[labels == i] = 1
    contours, _ = cv2.findContours(keep, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    filled = np.zeros_like(keep)
    cv2.drawContours(filled, contours, -1, 1, thickness=cv2.FILLED)

    filled = cv2.dilate(filled, kernel, iterations=1)
    f = max(3, int(min(h, w) * feather_frac) | 1)
    return cv2.GaussianBlur(filled.astype(np.float32), (f, f), 0).clip(0, 1)


def lanczos_upscale(img: np.ndarray, size: tuple[int, int]) -> np.ndarray:
    return cv2.resize(img, size, interpolation=cv2.INTER_LANCZOS4)


def compose_hires(original: Image.Image, run_model: Callable[[Image.Image], Image.Image], *,
                  model_size: tuple[int, int] = (576, 864),
                  upscaler: Upscaler | None = None,
                  target_long_side: int | None = 3840) -> tuple[Image.Image, dict]:
    """Full 4K pipeline. `run_model(person_low) -> dressed_low` wraps the try-on model.

    Returns (final image, debug info). `model_size` is (width, height) the model
    is fed at; its aspect decides the padding.
    """
    src = np.asarray(original.convert("RGB"))
    aspect = model_size[0] / model_size[1]
    padded, box = pad_to_aspect(src, aspect)
    ph, pw = padded.shape[:2]

    feed = cv2.resize(padded, model_size, interpolation=cv2.INTER_AREA)
    out_low = np.asarray(run_model(Image.fromarray(feed)).convert("RGB"))
    if out_low.shape[:2] != feed.shape[:2]:
        out_low = cv2.resize(out_low, model_size, interpolation=cv2.INTER_LANCZOS4)

    m_low = change_mask(feed, out_low)

    up = upscaler(out_low) if upscaler else out_low
    up = lanczos_upscale(up, (pw, ph)) if up.shape[:2] != (ph, pw) else up
    m = cv2.resize(m_low, (pw, ph), interpolation=cv2.INTER_LINEAR)[..., None]

    comp = (padded.astype(np.float32) * (1 - m) + up.astype(np.float32) * m).round().clip(0, 255).astype(np.uint8)
    comp = comp[box.top:box.top + box.height, box.left:box.left + box.width]

    info = {"source_size": [box.width, box.height], "model_size": list(model_size),
            "changed_fraction": float(m_low.mean()), "upscaler": "esrgan" if upscaler else "lanczos"}
    long_side = max(comp.shape[:2])
    if target_long_side and long_side < target_long_side:
        s = target_long_side / long_side
        size = (int(round(comp.shape[1] * s)), int(round(comp.shape[0] * s)))
        comp = upscaler(comp) if upscaler else comp
        comp = lanczos_upscale(comp, size) if (comp.shape[1], comp.shape[0]) != size else comp
        info["final_upscaled_to"] = list(size)
    info["final_size"] = [comp.shape[1], comp.shape[0]]
    return Image.fromarray(comp), info
