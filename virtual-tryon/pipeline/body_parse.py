"""Body and clothing parser built only on Apache-2.0 parts (Google MediaPipe).

Replaces fashn-human-parser, whose weights inherit NVIDIA's non-commercial
SegFormer licence. Output is the same 18-label map the try-on code (and FASHN's
own preprocessing) expects, so it is a drop-in replacement:

  0 background  1 face  2 hair  3 top  4 dress  5 skirt  6 pants  7 belt  8 bag
  9 hat  10 scarf  11 glasses  12 arms  13 hands  14 legs  15 feet  16 torso
  17 jewelry

How:
  * MediaPipe "selfie multiclass" segmentation gives hair, body skin, face skin,
    clothes and accessories. It works at 256x256, so we run it on overlapping
    square tiles over the person (several per full-length photo), blend the
    per-class confidences, and snap the edges to the photo with a guided filter.
  * MediaPipe pose landmarks split clothes into upper (top) and lower (pants)
    at the hips, split skin into arms / legs, and find the hands.
"""
from __future__ import annotations

import hashlib
import math
import os
import threading
from collections import OrderedDict
from pathlib import Path

import cv2
import numpy as np

# MediaPipe selfie multiclass classes
MP_BG, MP_HAIR, MP_BODY, MP_FACE, MP_CLOTHES, MP_OTHERS = range(6)
# fashn-style label ids (see module docstring)
BG, FACE, HAIR, TOP, DRESS, SKIRT, PANTS, BELT, BAG, HAT, SCARF, GLASSES, ARMS, HANDS, LEGS, FEET, TORSO, JEWELRY = range(18)

MODEL_DIR = Path(os.environ.get("MEDIAPIPE_MODELS", "/workspace/vto/models/mediapipe"))
SEG_MODEL = "selfie_multiclass_256x256.tflite"
POSE_MODEL = "pose_landmarker_heavy.task"
WORK_LONG_SIDE = 1536  # parse at most at this size, then scale labels back up
FAST_MAX_PIXELS = 360_000  # live-sized frames (<= 640x480 + margin) skip the tiled pass

# pose landmark ids
L_SHOULDER, R_SHOULDER, L_HIP, R_HIP, L_KNEE, R_KNEE = 11, 12, 23, 24, 25, 26
HAND_POINTS = ((15, 17, 19, 21), (16, 18, 20, 22))  # wrist, pinky, index, thumb (left, right)


def available(model_dir: Path = MODEL_DIR) -> bool:
    try:
        import mediapipe  # noqa: F401
    except Exception:
        return False
    return (model_dir / SEG_MODEL).exists() and (model_dir / POSE_MODEL).exists()


def _guided_filter(guide: np.ndarray, src: np.ndarray, r: int, eps: float) -> np.ndarray:
    """He et al. guided filter (grey guide): edge-aware smoothing of `src`."""
    mean = lambda x: cv2.boxFilter(x, -1, (r, r))
    mi, mp_ = mean(guide), mean(src)
    cov = mean(guide * src) - mi * mp_
    var = mean(guide * guide) - mi * mi
    a = cov / (var + eps)
    b = mp_ - a * mi
    return mean(a) * guide + mean(b)


class BodyParser:
    def __init__(self, model_dir: Path | str = MODEL_DIR):
        import mediapipe as mp
        from mediapipe.tasks.python import BaseOptions, vision
        self._mp = mp
        model_dir = Path(model_dir)
        self._seg = vision.ImageSegmenter.create_from_options(vision.ImageSegmenterOptions(
            base_options=BaseOptions(model_asset_path=str(model_dir / SEG_MODEL)),
            output_confidence_masks=True, output_category_mask=False))
        self._pose = vision.PoseLandmarker.create_from_options(vision.PoseLandmarkerOptions(
            base_options=BaseOptions(model_asset_path=str(model_dir / POSE_MODEL)),
            num_poses=1, min_pose_detection_confidence=0.3, min_pose_presence_confidence=0.3))
        self._lock = threading.Lock()
        # The same picture is often parsed twice in one try-on (FASHN's own
        # preprocessing, then our paste-back mask) and the same garment on every
        # live keyframe: remember the last few results.
        self._cache: OrderedDict[bytes, np.ndarray] = OrderedDict()

    # ---------- MediaPipe calls ----------
    def _mp_image(self, rgb: np.ndarray):
        return self._mp.Image(image_format=self._mp.ImageFormat.SRGB, data=np.ascontiguousarray(rgb))

    def _seg_square(self, rgb: np.ndarray) -> np.ndarray:
        """Per-class confidences (h, w, 6) for one crop, run as a padded square."""
        h, w = rgb.shape[:2]
        s = max(h, w)
        sq = np.full((s, s, 3), 127, np.uint8)
        y0, x0 = (s - h) // 2, (s - w) // 2
        sq[y0:y0 + h, x0:x0 + w] = rgb
        side = min(s, 512)  # the model is 256 px; no point feeding it more
        small = cv2.resize(sq, (side, side), interpolation=cv2.INTER_AREA) if side != s else sq
        res = self._seg.segment(self._mp_image(small))
        conf = np.stack([m.numpy_view().reshape(side, side) for m in res.confidence_masks], -1).astype(np.float32)
        conf = cv2.resize(conf, (s, s), interpolation=cv2.INTER_LINEAR)
        return conf[y0:y0 + h, x0:x0 + w]

    def _pose_points(self, rgb: np.ndarray):
        h, w = rgb.shape[:2]
        sc = min(1.0, 1024 / max(h, w))
        small = cv2.resize(rgb, (int(w * sc), int(h * sc)), interpolation=cv2.INTER_AREA) if sc < 1 else rgb
        res = self._pose.detect(self._mp_image(small))
        if not res.pose_landmarks:
            return None
        return np.array([[p.x * w, p.y * h, getattr(p, "visibility", 1.0) or 0.0] for p in res.pose_landmarks[0]],
                        np.float32)

    # ---------- parsing ----------
    def _confidences(self, rgb: np.ndarray) -> np.ndarray:
        h, w = rgb.shape[:2]
        conf = self._seg_square(rgb)  # coarse pass over the whole photo
        if h * w <= FAST_MAX_PIXELS:  # live camera frames: one pass is enough and keeps keyframes quick
            return conf
        person = conf[..., MP_BG] < 0.5
        ys, xs = np.nonzero(person)
        if ys.size < 0.002 * h * w:
            return conf
        x0, x1 = np.percentile(xs, [0.2, 99.8]).astype(int)
        y0, y1 = np.percentile(ys, [0.2, 99.8]).astype(int)
        m = int(0.06 * max(x1 - x0, y1 - y0))
        x0, y0, x1, y1 = max(0, x0 - m), max(0, y0 - m), min(w, x1 + m), min(h, y1 + m)
        bw, bh = x1 - x0, y1 - y0
        # Square tiles over the person, ~30% overlap, so each tile gets the
        # model's full 256 px on a part of the body instead of the whole photo.
        side = min(max(bw, int(bh / 2.2)), max(bw, bh))
        side = max(side, 64)
        nx = max(1, math.ceil((bw - side) / (0.7 * side)) + 1) if bw > side else 1
        ny = max(1, math.ceil((bh - side) / (0.7 * side)) + 1) if bh > side else 1
        acc = np.zeros((bh, bw, 6), np.float32)
        wsum = np.zeros((bh, bw, 1), np.float32)
        for iy in range(ny):
            for ix in range(nx):
                tx = x0 + (0 if nx == 1 else round(ix * (bw - side) / (nx - 1)))
                ty = y0 + (0 if ny == 1 else round(iy * (bh - side) / (ny - 1)))
                tx, ty = max(0, min(tx, w - 1)), max(0, min(ty, h - 1))
                crop = rgb[ty:min(h, ty + side), tx:min(w, tx + side)]
                c = self._seg_square(crop)
                ch, cw = c.shape[:2]
                # feathered weights: tiles count less near their own edges
                wy = np.minimum(np.arange(ch) + 1, ch - np.arange(ch)).astype(np.float32)
                wx = np.minimum(np.arange(cw) + 1, cw - np.arange(cw)).astype(np.float32)
                wt = np.sqrt(np.outer(wy, wx))[..., None]
                ay, ax = ty - y0, tx - x0
                ys_, xs_ = slice(max(0, ay), min(bh, ay + ch)), slice(max(0, ax), min(bw, ax + cw))
                cy, cx = slice(ys_.start - ay, ys_.stop - ay), slice(xs_.start - ax, xs_.stop - ax)
                acc[ys_, xs_] += c[cy, cx] * wt[cy, cx]
                wsum[ys_, xs_] += wt[cy, cx]
        fine = acc / np.maximum(wsum, 1e-6)
        conf[y0:y1, x0:x1] = np.where(wsum > 0, fine, conf[y0:y1, x0:x1])
        return conf

    def predict(self, image) -> np.ndarray:
        """RGB image (array or PIL) -> (H, W) uint8 label map with the ids above."""
        rgb = np.asarray(image.convert("RGB") if hasattr(image, "convert") else image)
        if rgb.ndim == 2:
            rgb = cv2.cvtColor(rgb, cv2.COLOR_GRAY2RGB)
        rgb = np.ascontiguousarray(rgb[..., :3])
        key = hashlib.blake2b(rgb.tobytes(), digest_size=16, person=str(rgb.shape).encode()[:16]).digest()
        with self._lock:
            hit = self._cache.get(key)
            if hit is not None:
                self._cache.move_to_end(key)
                return hit.copy()
        lab = self._predict(rgb)
        with self._lock:
            self._cache[key] = lab
            while len(self._cache) > 8:
                self._cache.popitem(last=False)
        return lab.copy()

    def _predict(self, rgb: np.ndarray) -> np.ndarray:
        H, W = rgb.shape[:2]
        sc = min(1.0, WORK_LONG_SIDE / max(H, W))
        work = cv2.resize(rgb, (int(round(W * sc)), int(round(H * sc))), interpolation=cv2.INTER_AREA) if sc < 1 else rgb
        with self._lock:
            conf = self._confidences(work)
            pts = self._pose_points(work)
        # Snap class boundaries to the photo's own edges.
        guide = cv2.cvtColor(work, cv2.COLOR_RGB2GRAY).astype(np.float32) / 255.0
        r = max(5, int(max(work.shape[:2]) / 160)) | 1
        conf = np.stack([_guided_filter(guide, conf[..., k], r, 1e-3) for k in range(6)], -1)
        mp_lab = conf.argmax(-1).astype(np.uint8)
        lab = self._to_labels(mp_lab, pts, cv2.cvtColor(work, cv2.COLOR_RGB2LAB))
        if sc < 1:
            lab = cv2.resize(lab, (W, H), interpolation=cv2.INTER_NEAREST)
        return lab

    @staticmethod
    def _clothes_split(mp_lab: np.ndarray, pts, rgb_lab: np.ndarray) -> float:
        """Row where the upper garment ends and the lower one begins.

        Searches from just above the hips down to the knees for the row where the
        clothes colour changes most (shirt hem over trousers, kurta hem over
        churidar). If nothing changes (one long garment, or matching colours),
        everything down to the knees counts as the upper garment."""
        h = mp_lab.shape[0]
        clothes = mp_lab == MP_CLOTHES

        def vis(i):
            return pts is not None and pts[i, 2] > 0.3
        if pts is not None and (vis(L_HIP) or vis(R_HIP)):
            hip_y = float(np.mean([pts[i, 1] for i in (L_HIP, R_HIP) if vis(i)] or [pts[L_HIP, 1]]))
            sh = [pts[i, 1] for i in (L_SHOULDER, R_SHOULDER) if vis(i)]
            torso = max(8.0, hip_y - float(np.mean(sh))) if sh else 0.3 * h
            kn = [pts[i, 1] for i in (L_KNEE, R_KNEE) if vis(i)]
            knee_y = float(np.mean(kn)) if kn else hip_y + torso
        else:
            ys = np.nonzero(mp_lab != MP_BG)[0]
            if not ys.size:
                return h * 0.55
            top, bot = float(ys.min()), float(ys.max())
            torso = 0.3 * (bot - top)
            hip_y, knee_y = top + 0.5 * (bot - top), top + 0.72 * (bot - top)
        lo, hi = int(max(0, hip_y - 0.15 * torso)), int(min(h - 1, max(knee_y, hip_y + 0.5 * torso)))
        k = max(4, int(0.12 * torso))
        cnt = clothes.sum(1).astype(np.float32)
        if hi - lo < 2 * k or cnt[lo:hi].max() < 4:
            return min(h - 1.0, hip_y + 0.12 * torso)
        # per-row median clothes colour (Lab), then compare the band above vs below
        lab = rgb_lab.astype(np.float32)
        row_col = np.full((h, 3), np.nan, np.float32)
        for y in range(max(0, lo - k), min(h, hi + k)):
            m = clothes[y]
            if m.sum() >= 4:
                row_col[y] = np.median(lab[y][m], axis=0)
        best_y, best_d = None, 0.0
        for y in range(lo + k, hi - k + 1, 2):
            a, b = row_col[y - k:y], row_col[y:y + k]
            a, b = a[~np.isnan(a[:, 0])], b[~np.isnan(b[:, 0])]
            if len(a) < k // 2 or len(b) < k // 2:
                continue
            d = float(np.linalg.norm(np.median(a, 0) - np.median(b, 0)))
            if d > best_d:
                best_y, best_d = y, d
        if best_y is not None and best_d >= 15.0:
            return float(best_y)
        return float(hi)  # no clear change: one garment down to the knees

    def _to_labels(self, mp_lab: np.ndarray, pts, rgb_lab: np.ndarray) -> np.ndarray:
        h, w = mp_lab.shape
        out = np.zeros((h, w), np.uint8)
        out[mp_lab == MP_FACE] = FACE
        out[mp_lab == MP_HAIR] = HAIR
        out[mp_lab == MP_OTHERS] = JEWELRY  # glasses, jewellery, bags, hats: all kept as-is
        clothes = mp_lab == MP_CLOTHES
        skin = mp_lab == MP_BODY
        rows = np.arange(h, dtype=np.float32)[:, None]

        def vis(i):
            return pts is not None and pts[i, 2] > 0.3

        split = self._clothes_split(mp_lab, pts, rgb_lab)
        upper = rows < split
        out[clothes & upper] = TOP
        out[clothes & ~upper] = PANTS
        out[skin & upper] = ARMS
        out[skin & ~upper] = LEGS

        # Hands: skin around the wrist / pinky / index / thumb landmarks.
        if pts is not None:
            yy, xx = np.mgrid[0:h, 0:w]
            for side in HAND_POINTS:
                p = pts[list(side)]
                if p[0, 2] < 0.3:
                    continue
                c = p[:, :2].mean(0)
                span = max(8.0, float(np.max(np.linalg.norm(p[1:, :2] - p[0, :2], axis=1))))
                rad = 1.4 * span
                x0, x1 = int(max(0, c[0] - rad)), int(min(w, c[0] + rad + 1))
                y0, y1 = int(max(0, c[1] - rad)), int(min(h, c[1] + rad + 1))
                if x1 <= x0 or y1 <= y0:
                    continue
                near = (xx[y0:y1, x0:x1] - c[0]) ** 2 + (yy[y0:y1, x0:x1] - c[1]) ** 2 <= rad * rad
                region = out[y0:y1, x0:x1]
                region[near & skin[y0:y1, x0:x1]] = HANDS
        return out


_PARSER: BodyParser | None = None
_PARSER_LOCK = threading.Lock()


def get(model_dir: Path | str = MODEL_DIR) -> BodyParser | None:
    global _PARSER
    with _PARSER_LOCK:
        if _PARSER is None and available(Path(model_dir)):
            _PARSER = BodyParser(model_dir)
    return _PARSER
