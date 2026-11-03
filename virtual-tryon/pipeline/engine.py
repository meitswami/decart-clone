"""Local real-time live engine: StreamDiffusionV2 wrapper. GPU box only.

This is the heart of the `local` backend. It holds a warm StreamDiffusionV2
pipeline and transforms each incoming camera frame, conditioned on the garment.
Marked TODO(gpu) where it must be wired to the upstream repo's exact API
(pinned commit in scripts/download_models.sh).
"""
from __future__ import annotations

import numpy as np


class LiveEngine:
    def __init__(self, garment_png: bytes, category: str):
        self.garment_png = garment_png
        self.category = category
        self._pipe = None
        # TODO(gpu): build the pipeline once, keep it warm:
        #   - load StreamDiffusionV2 (see scripts/download_models.sh commit)
        #   - apply LCM-LoRA, compile U-Net + VAE with TensorRT
        #   - set denoise steps = 1..4, stream-batch on
        #   - encode the garment as the conditioning reference
        # Keeping it warm is what makes it real-time; never build per frame.

    def process(self, frame_rgb: np.ndarray) -> np.ndarray:
        """One camera frame in, one dressed frame out. Must be <~33ms."""
        if self._pipe is None:
            # Dev passthrough until wired, so transport can be tested.
            return frame_rgb
        # TODO(gpu): return self._pipe(frame_rgb, reference=self._garment_latent)
        raise NotImplementedError

    def close(self):
        self._pipe = None
