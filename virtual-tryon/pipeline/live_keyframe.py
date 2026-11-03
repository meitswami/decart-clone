"""Local live engine v1: keyframe try-on + per-frame garment tracking.

Why: open real-time video diffusion (StreamDiffusionV2) is text-guided only,
so it can't hold a real product's print/logo. Photo try-on (FASHN) holds it
perfectly but takes ~1 s. So:

  * every ~KEY_INTERVAL s (sooner on big motion) a background thread runs
    photo try-on on the current camera frame -> a dressed KEYFRAME;
  * every camera frame (30 fps), dense optical flow maps the frame back to the
    keyframe's source, and the keyframe's garment pixels are warped onto the
    live frame and blended in with a soft mask.

Garment detail is exact (it comes from photo try-on); latency per frame is a
few ms. Weak spot: very fast motion or turning around, until the next
keyframe lands (the mask is faded where flow is unreliable).
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable

import cv2
import numpy as np

from .hires import change_mask, pad_to_aspect

DressFn = Callable[[np.ndarray], np.ndarray]  # RGB frame (model aspect) -> dressed RGB, same size


@dataclass
class Keyframe:
    src_small: np.ndarray   # grayscale, work resolution
    dressed: np.ndarray     # RGB, frame resolution
    mask: np.ndarray        # float32 0..1, frame resolution
    made_at: float


class KeyframeCompositor:
    """Warps the latest dressed keyframe onto each new frame (CPU, ~ms)."""

    def __init__(self, work_width: int = 320):
        self.work_width = work_width
        self._flow = cv2.DISOpticalFlow_create(cv2.DISOPTICAL_FLOW_PRESET_FAST)
        self.key: Keyframe | None = None
        self.last_motion = 0.0  # median flow magnitude (work px) vs keyframe

    def _small(self, rgb: np.ndarray) -> np.ndarray:
        h, w = rgb.shape[:2]
        s = self.work_width / w
        g = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
        return cv2.resize(g, (self.work_width, int(round(h * s))), interpolation=cv2.INTER_AREA)

    def set_keyframe(self, src_rgb: np.ndarray, dressed_rgb: np.ndarray,
                     mask: np.ndarray | None = None) -> None:
        if dressed_rgb.shape != src_rgb.shape:
            dressed_rgb = cv2.resize(dressed_rgb, (src_rgb.shape[1], src_rgb.shape[0]),
                                     interpolation=cv2.INTER_LINEAR)
        m = change_mask(src_rgb, dressed_rgb) if mask is None else mask.astype(np.float32)
        self.key = Keyframe(self._small(src_rgb), dressed_rgb, m, time.time())

    def apply(self, frame_rgb: np.ndarray) -> np.ndarray:
        k = self.key
        if k is None:
            return frame_rgb
        h, w = frame_rgb.shape[:2]
        cur = self._small(frame_rgb)
        if cur.shape != k.src_small.shape:  # resolution changed: drop keyframe
            self.key = None
            return frame_rgb
        # Flow cur -> key: for each current pixel, where it was in the keyframe.
        fwd = self._flow.calc(cur, k.src_small, None)
        bwd = self._flow.calc(k.src_small, cur, None)
        sh, sw = cur.shape
        gx, gy = np.meshgrid(np.arange(sw, dtype=np.float32), np.arange(sh, dtype=np.float32))
        # Forward-backward consistency: unreliable flow (occlusion) -> fade mask.
        bx = cv2.remap(bwd[..., 0], gx + fwd[..., 0], gy + fwd[..., 1], cv2.INTER_LINEAR)
        by = cv2.remap(bwd[..., 1], gx + fwd[..., 0], gy + fwd[..., 1], cv2.INTER_LINEAR)
        err = np.sqrt((fwd[..., 0] + bx) ** 2 + (fwd[..., 1] + by) ** 2)
        conf = np.clip(1.0 - (err - 1.0) / 3.0, 0.0, 1.0).astype(np.float32)
        self.last_motion = float(np.median(np.linalg.norm(fwd, axis=2)))

        # 1) Robust whole-garment motion: affine fit (RANSAC) on confident flow
        #    vectors that land on the keyframe garment. Flat-coloured fabric has
        #    unreliable per-pixel flow; its overall motion is still well defined.
        s = w / sw
        key_mask_small = cv2.resize(k.mask, (sw, sh), interpolation=cv2.INTER_AREA)
        on_garment = cv2.remap(key_mask_small, gx + fwd[..., 0], gy + fwd[..., 1], cv2.INTER_LINEAR) > 0.5
        sel = np.where(on_garment & (conf > 0.6))
        A = None
        if sel[0].size >= 30:
            step = max(1, sel[0].size // 1500)
            src = np.stack([gx[sel][::step], gy[sel][::step]], 1)
            dst = src + fwd[sel][::step]
            A, inl = cv2.estimateAffine2D(src, dst, method=cv2.RANSAC, ransacReprojThreshold=1.5)
        if A is not None:
            base = np.stack([A[0, 0] * gx + A[0, 1] * gy + A[0, 2],
                             A[1, 0] * gx + A[1, 1] * gy + A[1, 2]], -1) - np.stack([gx, gy], -1)
            # 2) Local detail (arms, folds): per-pixel flow only where it is
            #    confident AND agrees roughly with the garment's motion.
            resid = np.linalg.norm(fwd - base, axis=2)
            w_local = cv2.GaussianBlur(conf * (resid < 4.0), (0, 0), 3)
            flow = base + w_local[..., None] * (fwd - base)
            fade = np.ones_like(conf)
        else:  # garment left the frame / no texture at all: plain flow, faded
            flow = fwd
            fade = cv2.GaussianBlur(conf, (0, 0), 4)

        flow_full = cv2.resize(flow.astype(np.float32), (w, h), interpolation=cv2.INTER_LINEAR) * np.float32(s)
        fade_full = cv2.resize(fade.astype(np.float32), (w, h), interpolation=cv2.INTER_LINEAR)
        X, Y = np.meshgrid(np.arange(w, dtype=np.float32), np.arange(h, dtype=np.float32))
        mx, my = X + flow_full[..., 0], Y + flow_full[..., 1]
        warped = cv2.remap(k.dressed, mx, my, cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
        wm = cv2.remap(k.mask, mx, my, cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT, borderValue=0)
        wm = (wm * fade_full)[..., None]
        out = frame_rgb.astype(np.float32) * (1 - wm) + warped.astype(np.float32) * wm
        return out.round().clip(0, 255).astype(np.uint8)


class LiveDresser:
    """Per-session driver: call process(frame) for every camera frame."""

    def __init__(self, dress: DressFn, *, model_aspect: float = 576 / 864,
                 key_interval: float = 1.0, motion_refresh_px: float = 6.0, work_width: int = 320,
                 mask_fn: Callable[[np.ndarray, np.ndarray], np.ndarray] | None = None):
        self.dress = dress
        self.mask_fn = mask_fn
        self.model_aspect = model_aspect
        self.key_interval = key_interval
        self.motion_refresh_px = motion_refresh_px
        self.comp = KeyframeCompositor(work_width)
        self._busy = threading.Lock()
        self._last_submit = 0.0
        self.stats = {"frames": 0, "keyframes": 0, "key_ms": [], "frame_ms": []}

    def _make_keyframe(self, frame: np.ndarray) -> None:
        t = time.time()
        try:
            padded, box = pad_to_aspect(frame, self.model_aspect)
            dressed = self.dress(padded)
            if dressed.shape != padded.shape:
                dressed = cv2.resize(dressed, (padded.shape[1], padded.shape[0]))
            dressed = dressed[box.top:box.top + box.height, box.left:box.left + box.width]
            mask = None
            if self.mask_fn is not None:
                try:
                    mask = self.mask_fn(frame, dressed)
                except Exception:
                    mask = None
            self.comp.set_keyframe(frame, dressed, mask)
            self.stats["keyframes"] += 1
            self.stats["key_ms"].append(round((time.time() - t) * 1000))
        finally:
            self._busy.release()

    def process(self, frame_rgb: np.ndarray) -> np.ndarray:
        t = time.time()
        due = (t - self._last_submit) >= self.key_interval
        moved = self.comp.key is not None and self.comp.last_motion > self.motion_refresh_px
        if (self.comp.key is None or due or moved) and self._busy.acquire(blocking=False):
            self._last_submit = t
            threading.Thread(target=self._make_keyframe, args=(frame_rgb.copy(),), daemon=True).start()
        out = self.comp.apply(frame_rgb)
        self.stats["frames"] += 1
        self.stats["frame_ms"].append(round((time.time() - t) * 1000, 1))
        return out
