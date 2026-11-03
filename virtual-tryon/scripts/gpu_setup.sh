#!/usr/bin/env bash
# One-time setup ON the RunPod GPU box (image runpod/pytorch:1.0.2-cu1281-torch280-ubuntu2404).
# Everything lands in /workspace (the pod volume) so it survives stop/start.
#
# What it downloads, all open-source and licensed for commercial use:
#   - FASHN VTON v1.5 code  github.com/fashn-AI/fashn-vton-1.5  (Apache-2.0), pinned commit
#   - its weights (~2 GB)   via the repo's own scripts/download_weights.py (Hugging Face)
#   - Real-ESRGAN x4plus weights (BSD-3) from its GitHub release, loaded with `spandrel` (MIT)
#   - OpenAI CLIP ViT-L/14 (MIT) from Hugging Face, for the gender estimate
#   - our server deps       fastapi, uvicorn, aiortc, opencv (PyPI)
set -euxo pipefail

FASHN_COMMIT=7c0f10af3f91ad4048fe9729c470a13ef905d25a   # verified 2026-11-03
W=/workspace/vto
export HF_HOME=/workspace/hf PIP_CACHE_DIR=/workspace/pipcache
mkdir -p "$W/models"

# venv that reuses the image's CUDA torch (never reinstall torch)
[ -d /workspace/venv ] || python -m venv --system-site-packages /workspace/venv
. /workspace/venv/bin/activate
pip install -q --upgrade pip

# FASHN VTON v1.5
[ -d "$W/fashn-vton-1.5" ] || git clone -q https://github.com/fashn-AI/fashn-vton-1.5.git "$W/fashn-vton-1.5"
git -C "$W/fashn-vton-1.5" checkout -q "$FASHN_COMMIT"
pip install -q -e "$W/fashn-vton-1.5"
[ -f "$W/models/fashn-vton-1.5/weights/model.safetensors" ] || \
  python "$W/fashn-vton-1.5/scripts/download_weights.py" --weights-dir "$W/models/fashn-vton-1.5/weights"

# Real-ESRGAN x4plus for the 4K step (loaded with spandrel; no basicsr needed)
pip install -q spandrel
[ -f "$W/models/RealESRGAN_x4plus.pth" ] || \
  curl -fsSL -o "$W/models/RealESRGAN_x4plus.pth" \
  https://github.com/xinntao/Real-ESRGAN/releases/download/v0.1.0/RealESRGAN_x4plus.pth

# CLIP for the person analysis (gender estimate)
pip install -q "transformers>=4.44"
python -c "from transformers import CLIPModel, CLIPProcessor; m='openai/clip-vit-large-patch14'; CLIPModel.from_pretrained(m); CLIPProcessor.from_pretrained(m); print('clip ok')"

# Our services
pip install -q fastapi "uvicorn[standard]" python-multipart pydantic-settings httpx \
  opencv-python-headless aiortc==1.15.0 av==17.1.0

python - <<'PY'
import torch, fashn_vton, spandrel, transformers, aiortc
print("torch", torch.__version__, "cuda", torch.cuda.is_available(), torch.cuda.get_device_name(0))
print("fashn_vton ok, spandrel ok, transformers", transformers.__version__, "aiortc", aiortc.__version__)
PY
echo GPU_SETUP_DONE
