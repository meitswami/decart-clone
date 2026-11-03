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


# Human-parser labels (fashn-human-parser, 18 classes).
PARSER_LABELS = {0: "background", 1: "face", 2: "hair", 3: "top", 4: "dress", 5: "skirt", 6: "pants",
                 7: "belt", 8: "bag", 9: "hat", 10: "scarf", 11: "glasses", 12: "arms", 13: "hands",
                 14: "legs", 15: "feet", 16: "torso", 17: "jewelry"}
_IDS = {v: k for k, v in PARSER_LABELS.items()}
COVERAGE = {"tops": ["top", "dress", "scarf"], "bottoms": ["skirt", "pants", "belt"],
            "one-pieces": ["top", "dress", "scarf", "skirt", "pants", "belt"]}
SKIN = {"tops": ["arms", "hands", "torso"], "bottoms": ["legs"], "one-pieces": ["arms", "hands", "torso", "legs"]}
IDENTITY = ["face", "hair", "hat", "glasses", "jewelry", "bag"]


def keep_hands(pb: np.ndarray) -> np.ndarray:
    """Pixels to keep from the original photo around each hand: the hand itself
    plus non-body pixels right next to it (a phone, cup or bag being held).
    Try-on models redraw hands and drop held objects; we keep the real ones."""
    hands = (pb == _IDS["hands"]).astype(np.uint8)
    keep = hands.astype(bool)
    if not keep.any():
        return keep
    n, lab, stats, _ = cv2.connectedComponentsWithStats(hands, 8)
    not_body = np.isin(pb, [_IDS["background"], _IDS["bag"]])
    for i in range(1, n):
        area = stats[i, cv2.CC_STAT_AREA]
        if area < 30:
            continue
        r = max(3, int(0.6 * np.sqrt(area))) | 1
        k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (r, r))
        near = cv2.dilate((lab == i).astype(np.uint8), k) > 0
        keep |= near & not_body
    return keep


def semantic_mask(before: np.ndarray, after: np.ndarray, category: str,
                  parse: Callable[[np.ndarray], np.ndarray], parse_after: bool = True) -> np.ndarray:
    """Garment region from a human parser instead of colour difference alone.

    region = old garment ∪ new garment ∪ (skin that actually changed, e.g. new
    sleeve length) − identity parts of the original (face, hair, hat...).
    Colour-difference masks punch holes wherever the new garment matches the old
    one (white logo letters on a former white tee); this doesn't.
    """
    h, w = before.shape[:2]
    pb = parse(before)
    if pb.shape != (h, w):
        pb = cv2.resize(pb.astype(np.uint8), (w, h), interpolation=cv2.INTER_NEAREST)
    diff = change_mask(before, after) > 0.5
    ids = lambda names: [_IDS[n] for n in names]
    if parse_after:
        pa = parse(after)
        if pa.shape != (h, w):
            pa = cv2.resize(pa.astype(np.uint8), (w, h), interpolation=cv2.INTER_NEAREST)
    else:  # live: the new garment = what changed on the person's own clothes/skin
        # (not the background around them: a moving camera turns that into a halo)
        body = (np.isin(pb, ids(COVERAGE["one-pieces"] + SKIN["one-pieces"]))).astype(np.uint8)
        r = max(3, int(min(h, w) * 0.012) | 1)
        near = cv2.dilate(body, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (r, r))) > 0
        pa = np.where(diff & near & ~np.isin(pb, ids(IDENTITY + ["hands"])), _IDS["top"], 0).astype(np.uint8)
        if category == "bottoms":
            pa[pa > 0] = _IDS["pants"]
    garment = np.isin(pb, ids(COVERAGE[category])) | np.isin(pa, ids(COVERAGE[category]))
    skin = (np.isin(pb, ids(SKIN[category])) | np.isin(pa, ids(SKIN[category]))) & diff
    m = (garment | skin).astype(np.uint8)
    ks = max(3, int(min(h, w) * (0.012 if parse_after else 0.006)) | 1)
    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (ks, ks))
    m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, k, iterations=2)
    m = cv2.dilate(m, k, iterations=1)
    m[np.isin(pb, ids(IDENTITY))] = 0  # never repaint the face, hair, hat...
    m[keep_hands(pb)] = 0              # nor the hands, or a phone/bag held in them
    f = max(3, int(min(h, w) * 0.008) | 1)
    return cv2.GaussianBlur(m.astype(np.float32), (f, f), 0).clip(0, 1)


def lanczos_upscale(img: np.ndarray, size: tuple[int, int]) -> np.ndarray:
    return cv2.resize(img, size, interpolation=cv2.INTER_LANCZOS4)


def compose_hires(original: Image.Image, run_model: Callable[[Image.Image], Image.Image], *,
                  model_size: tuple[int, int] = (576, 864),
                  upscaler: Upscaler | None = None,
                  target_long_side: int | None = 3840,
                  mask_fn: Callable[[np.ndarray, np.ndarray], np.ndarray] | None = None,
                  person_box: Callable[[np.ndarray], tuple[int, int, int, int] | None] | None = None,
                  ) -> tuple[Image.Image, dict]:
    """Full 4K pipeline. `run_model(person_low) -> dressed_low` wraps the try-on model.

    Returns (final image, debug info). `model_size` is (width, height) the model
    is fed at; its aspect decides the padding. `person_box(rgb) -> (x0, y0, x1, y1)`
    lets the model work on a crop around the person, so a full-length photo
    gets far more model pixels on the garment (sharper print, cleaner edges).
    """
    full = np.asarray(original.convert("RGB"))
    aspect = model_size[0] / model_size[1]
    region = None
    if person_box is not None:
        try:
            region = person_box(full)
        except Exception:
            region = None
    if region:
        x0, y0, x1, y1 = region
        src = full[y0:y1, x0:x1]
    else:
        x0, y0 = 0, 0
        src = full
    padded, box = pad_to_aspect(src, aspect)
    ph, pw = padded.shape[:2]

    feed = cv2.resize(padded, model_size, interpolation=cv2.INTER_AREA)
    out_low = np.asarray(run_model(Image.fromarray(feed)).convert("RGB"))
    if out_low.shape[:2] != feed.shape[:2]:
        out_low = cv2.resize(out_low, model_size, interpolation=cv2.INTER_LANCZOS4)

    mask_kind = "semantic" if mask_fn else "difference"
    try:
        m_low = mask_fn(feed, out_low) if mask_fn else change_mask(feed, out_low)
    except Exception:
        m_low, mask_kind = change_mask(feed, out_low), "difference (parser failed)"

    up = upscaler(out_low) if upscaler else out_low
    up = lanczos_upscale(up, (pw, ph)) if up.shape[:2] != (ph, pw) else up
    m = cv2.resize(m_low, (pw, ph), interpolation=cv2.INTER_LINEAR)[..., None]

    comp = (padded.astype(np.float32) * (1 - m) + up.astype(np.float32) * m).round().clip(0, 255).astype(np.uint8)
    comp = comp[box.top:box.top + box.height, box.left:box.left + box.width]
    if src is not full:
        whole = full.copy()
        whole[y0:y0 + comp.shape[0], x0:x0 + comp.shape[1]] = comp
        comp = whole

    info = {"source_size": [full.shape[1], full.shape[0]], "model_size": list(model_size),
            "person_crop": [int(v) for v in region] if region else None,
            "changed_fraction": float(m_low.mean()), "upscaler": "esrgan" if upscaler else "lanczos",
            "mask": mask_kind}
    long_side = max(comp.shape[:2])
    if target_long_side and long_side < target_long_side:
        s = target_long_side / long_side
        size = (int(round(comp.shape[1] * s)), int(round(comp.shape[0] * s)))
        comp = upscaler(comp) if upscaler else comp
        comp = lanczos_upscale(comp, size) if (comp.shape[1], comp.shape[0]) != size else comp
        info["final_upscaled_to"] = list(size)
    info["final_size"] = [comp.shape[1], comp.shape[0]]
    return Image.fromarray(comp), info
