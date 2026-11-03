#!/usr/bin/env bash
# Runs at EVERY GPU pod boot, after the pod's start command refreshed the code
# on the volume. Starts the idle watchdog and the GPU worker.
exec >>/workspace/boot.log 2>&1
echo "$(date -u +%FT%TZ) boot pod=${RUNPOD_POD_ID:-?} branch=${GIT_BRANCH:-main}"
cd /workspace/app/virtual-tryon || { echo "no code on the volume"; exit 1; }
# Self-contained env (python:3.12-slim image) if present, else the older env
# that relied on the big CUDA image's system packages.
if [ -x /workspace/env312/bin/python ]; then ENV=/workspace/env312; else ENV=/workspace/venv; fi
. "$ENV/bin/activate"
export LD_LIBRARY_PATH="$(ls -d $ENV/lib/python3*/site-packages/nvidia/*/lib /usr/local/lib/python3*/dist-packages/nvidia/*/lib 2>/dev/null | tr '\n' ':')${LD_LIBRARY_PATH:-}"
export FASHN_WEIGHTS_DIR=/workspace/vto/models/fashn-vton-1.5/weights
export ESRGAN_WEIGHTS=/workspace/vto/models/RealESRGAN_x4plus.pth
export HF_HOME=/workspace/hf KEEPALIVE=/workspace/.keepalive KEEPALIVE_FILE=/workspace/.keepalive
export CAPTURES_DIR=/workspace/vto/captures
echo "env=$ENV python=$(python -V 2>&1)"
IDLE_MIN="${IDLE_MIN:-15}" nohup bash scripts/pod_watchdog.sh >>/workspace/watchdog.log 2>&1 &
exec uvicorn gpu.worker:app --host 0.0.0.0 --port 8000 --ws-max-size 4194304 --timeout-keep-alive 75
