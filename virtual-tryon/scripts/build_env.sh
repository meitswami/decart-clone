#!/usr/bin/env bash
# Builds a SELF-CONTAINED Python environment on the network volume, so GPU pods
# can boot from the tiny python:3.12-slim image (fast to download anywhere)
# instead of a 10+ GB CUDA image. Run once, in a python:3.12-slim pod with the
# volume mounted at /workspace. Models are already on the volume.
set -euxo pipefail
mkdir -p /workspace/buildlog
exec > >(tee -a /workspace/buildlog/build.log) 2>&1
export PIP_CACHE_DIR=/workspace/pipcache PIP_DISABLE_PIP_VERSION_CHECK=1
ENV=/workspace/env312
[ -x "$ENV/bin/python" ] || python3 -m venv "$ENV"
. "$ENV/bin/activate"
pip install -q --upgrade pip
pip install -q torch==2.8.0 torchvision==0.23.0 --index-url https://download.pytorch.org/whl/cu128
pip install -q -e /workspace/vto/fashn-vton-1.5
# FASHN pulls GUI OpenCV (needs libGL) and the CUDA-13 onnxruntime: swap both.
pip uninstall -y -q opencv-python || true
pip install -q --force-reinstall --no-deps opencv-python-headless
pip install -q "onnxruntime-gpu==1.22.0" spandrel "transformers>=4.44" fastapi "uvicorn[standard]" \
  python-multipart pydantic-settings httpx aiortc==1.15.0 av==17.1.0
export LD_LIBRARY_PATH="$(ls -d $ENV/lib/python3*/site-packages/nvidia/*/lib | tr '\n' ':')"
python - <<'PY'
import torch, cv2, onnxruntime as ort, fashn_vton, spandrel, transformers
print("torch", torch.__version__, "cuda", torch.cuda.is_available(), torch.cuda.get_device_name(0) if torch.cuda.is_available() else "-")
print("cv2", cv2.__version__, "| ort", ort.__version__, ort.get_available_providers()[:2], "| transformers", transformers.__version__)
PY
bash "$(dirname "$0")/setup_mediapipe.sh"   # body parser (Apache-2.0)
du -sh "$ENV"
echo BUILD_DONE
