#!/usr/bin/env bash
# Start the GPU services ON the pod (after scripts/gpu_setup.sh).
#   WORKER_TOKEN=... ENGINE_SECRET=... ENGINE_ALLOWED_ORIGINS=https://your-site bash scripts/gpu_run.sh
# Photo worker  -> https://<pod-id>-8000.proxy.runpod.net
# Live engine   -> https://<pod-id>-8900.proxy.runpod.net
set -euo pipefail
cd "$(dirname "$0")/.."
. /workspace/venv/bin/activate
export FASHN_WEIGHTS_DIR=/workspace/vto/models/fashn-vton-1.5/weights
export ESRGAN_WEIGHTS=/workspace/vto/models/RealESRGAN_x4plus.pth
export HF_HOME=/workspace/hf KEEPALIVE=/workspace/.keepalive
: "${WORKER_TOKEN:?set WORKER_TOKEN}" "${ENGINE_SECRET:?set ENGINE_SECRET}"

pgrep -f pod_watchdog.sh >/dev/null || nohup bash scripts/pod_watchdog.sh >/workspace/watchdog.log 2>&1 &
nohup uvicorn gpu.worker:app --host 0.0.0.0 --port 8000 >/workspace/worker.log 2>&1 &
nohup uvicorn webrtc.live_server:app --host 0.0.0.0 --port 8900 >/workspace/live.log 2>&1 &
sleep 2; echo "started: worker :8000, live engine :8900 (logs in /workspace/*.log)"
