"""Pose + body segmentation mask via MediaPipe. CPU ok, faster on GPU."""
from __future__ import annotations

import numpy as np


class PoseExtractor:
    def __init__(self):
        import mediapipe as mp
        self._seg = mp.solutions.selfie_segmentation.SelfieSegmentation(
            model_selection=1)
        self._pose = mp.solutions.pose.Pose(
            model_complexity=1, enable_segmentation=True,
            min_detection_confidence=0.5)

    def body_mask(self, rgb: np.ndarray) -> np.ndarray:
        """Return a float mask [H,W] in 0..1 where the person is."""
        res = self._seg.process(rgb)
        if res.segmentation_mask is None:
            return np.ones(rgb.shape[:2], dtype=np.float32)
        return res.segmentation_mask.astype(np.float32)

    def landmarks(self, rgb: np.ndarray):
        """Return pose landmarks or None. Used to place/scale the garment."""
        res = self._pose.process(rgb)
        return res.pose_landmarks
