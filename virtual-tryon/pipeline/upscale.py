"""FlashVSR sharpening/de-blur pass. GPU box only. Optional at first."""
from __future__ import annotations

import numpy as np


class Upscaler:
    def __init__(self, enabled: bool = True):
        self.enabled = enabled
        self._model = None
        # TODO(gpu): load FlashVSR (pinned commit in download_models.sh)

    def sharpen(self, frame_rgb: np.ndarray) -> np.ndarray:
        if not self.enabled or self._model is None:
            return frame_rgb
        # TODO(gpu): return self._model.step(frame_rgb)
        return frame_rgb
