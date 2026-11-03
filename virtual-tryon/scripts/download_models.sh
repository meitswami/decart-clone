#!/usr/bin/env bash
# Run ONLY on the GPU box. Fetches source repos (pinned) + weights.
set -euo pipefail
mkdir -p models && cd models
pip install torch==2.5.1 torchvision==0.20.1 --index-url https://download.pytorch.org/whl/cu124
pip install -r ../requirements-gpu.txt
git clone https://github.com/chenfengxu714/StreamDiffusionV2.git
git clone https://github.com/OpenImagingLab/FlashVSR.git
git clone https://github.com/fashn-AI/fashn-vton-1.5.git fashn-vton-1.5
echo "Done. Verify: python -c 'import torch; print(torch.cuda.is_available())'"
