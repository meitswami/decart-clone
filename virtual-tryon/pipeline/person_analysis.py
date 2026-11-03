"""Person analysis for the try-on page: is there a person, and an AI estimate
of how they present (male / female), used to suggest garments.

Model: OpenAI CLIP ViT-L/14 (MIT), zero-shot against a few phrasings per
class (prompt ensembling). Appearance-based estimates can be wrong, so:
  * below MIN_CONFIDENCE the answer is "unsure";
  * the page always lets the shopper correct it;
  * it's stored only as "estimate" next to the shopper's own choice.
"""
from __future__ import annotations

import io
import logging
import os
import threading

from PIL import Image, ImageOps

log = logging.getLogger("tryon.analysis")

MODEL_ID = os.environ.get("CLIP_MODEL", "openai/clip-vit-large-patch14")
MIN_CONFIDENCE = float(os.environ.get("GENDER_MIN_CONFIDENCE", "0.75"))

PROMPTS = {
    "male": ["a photo of a man", "a photo of a male person", "a portrait of a man wearing clothes"],
    "female": ["a photo of a woman", "a photo of a female person", "a portrait of a woman wearing clothes"],
    "none": ["a photo with no people in it", "a product photo of clothing on a plain background",
             "a photo of an empty room"],
}

_lock = threading.Lock()
_state: dict = {}


def available() -> bool:
    try:
        import torch  # noqa: F401
        import transformers  # noqa: F401
        return True
    except Exception:
        return False


def _load():
    with _lock:
        if "model" not in _state:
            import torch
            from transformers import CLIPModel, CLIPProcessor
            dev = "cuda" if torch.cuda.is_available() else "cpu"
            dtype = torch.float16 if dev == "cuda" else torch.float32
            try:  # newer transformers: dtype=; older: torch_dtype=
                model = CLIPModel.from_pretrained(MODEL_ID, dtype=dtype)
            except TypeError:
                model = CLIPModel.from_pretrained(MODEL_ID, torch_dtype=dtype)
            model = model.to(dev).eval()
            proc = CLIPProcessor.from_pretrained(MODEL_ID)
            labels, texts = [], []
            for lab, ps in PROMPTS.items():
                labels += [lab] * len(ps)
                texts += ps
            _state.update(model=model, proc=proc, dev=dev, dtype=dtype, labels=labels, texts=texts)
            log.info("CLIP loaded on %s", dev)
    return _state


def analyze(image_bytes: bytes) -> dict:
    """Returns {"person_found", "gender": male|female|unsure, "confidence", "scores"}."""
    import torch
    s = _load()
    img = ImageOps.exif_transpose(Image.open(io.BytesIO(image_bytes))).convert("RGB")
    # Full forward pass: logits_per_image is stable across transformers versions
    # (get_*_features changed return type in v5).
    with torch.no_grad():
        inp = s["proc"](text=s["texts"], images=img, return_tensors="pt", padding=True)
        inp = {k: (v.to(s["dev"], s["dtype"]) if v.is_floating_point() else v.to(s["dev"])) for k, v in inp.items()}
        logits = s["model"](**inp).logits_per_image.float().softmax(dim=-1)[0]
    scores: dict[str, float] = {}
    for lab, p in zip(s["labels"], logits.tolist()):
        scores[lab] = scores.get(lab, 0.0) + p
    person = scores["male"] + scores["female"]
    if scores["none"] > person:
        return {"person_found": False, "gender": "unsure", "confidence": 0.0,
                "scores": {k: round(v, 3) for k, v in scores.items()}}
    male_share = scores["male"] / person
    gender, conf = ("male", male_share) if male_share >= 0.5 else ("female", 1 - male_share)
    if conf < MIN_CONFIDENCE:
        gender = "unsure"
    return {"person_found": True, "gender": gender, "confidence": round(conf, 3),
            "scores": {k: round(v, 3) for k, v in scores.items()}}
