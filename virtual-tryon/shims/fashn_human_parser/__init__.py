"""Drop-in stand-in for the `fashn_human_parser` package, backed by our
MediaPipe parser (Apache-2.0) instead of the original SegFormer weights
(NVIDIA non-commercial licence).

FASHN VTON imports this package for its preprocessing (cutting the garment out
of an on-model product photo), and our own code uses it for masks. With
`virtual-tryon/shims` first on PYTHONPATH (scripts/pod_boot.sh), both get the
MediaPipe parser and the original weights are never loaded.

The label names below are the standard 18-class body/clothing scheme.
"""
from __future__ import annotations

from typing import Dict, List

IDS_TO_LABELS: Dict[int, str] = {
    0: "background", 1: "face", 2: "hair", 3: "top", 4: "dress", 5: "skirt", 6: "pants", 7: "belt",
    8: "bag", 9: "hat", 10: "scarf", 11: "glasses", 12: "arms", 13: "hands", 14: "legs", 15: "feet",
    16: "torso", 17: "jewelry",
}
LABELS_TO_IDS: Dict[str, int] = {v: k for k, v in IDS_TO_LABELS.items()}
CATEGORY_TO_BODY_COVERAGE: Dict[str, str] = {"tops": "upper", "bottoms": "lower", "one-pieces": "full"}
BODY_COVERAGE_TO_LABELS: Dict[str, List[str]] = {
    "upper": ["top", "dress", "scarf"],
    "lower": ["skirt", "pants", "belt"],
    "full": ["top", "dress", "scarf", "skirt", "pants", "belt"],
}
IDENTITY_LABELS: List[str] = ["face", "hair", "jewelry", "bag", "glasses", "hat"]


class FashnHumanParser:
    """Same interface as the original: FashnHumanParser(device=...).predict(image)."""

    def __init__(self, *args, **kwargs):
        from pipeline import body_parse
        self._p = body_parse.get()
        if self._p is None:
            raise RuntimeError("MediaPipe parser unavailable (mediapipe or its models missing)")

    def predict(self, image):
        return self._p.predict(image)

    @staticmethod
    def get_label_name(label_id: int) -> str:
        return IDS_TO_LABELS.get(label_id, "unknown")

    @staticmethod
    def get_labels() -> dict:
        return IDS_TO_LABELS.copy()


__version__ = "mediapipe-shim-1"
__all__ = ["FashnHumanParser", "IDS_TO_LABELS", "LABELS_TO_IDS", "CATEGORY_TO_BODY_COVERAGE",
           "BODY_COVERAGE_TO_LABELS", "IDENTITY_LABELS"]
