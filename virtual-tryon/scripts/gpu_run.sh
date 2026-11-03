#!/usr/bin/env bash
# Start the try-on site ON the GPU pod (after scripts/gpu_setup.sh).
#
#   ENGINE_SECRET=... ADMIN_TOKEN=... bash scripts/gpu_run.sh
#
# Serves EVERYTHING from one process on :8000, reachable at
#   https://<pod-id>-8000.proxy.runpod.net        (works on mobile; camera needs HTTPS)
# Photo try-on (FASHN + 4K), person analysis (CLIP), live try-on over a
# WebSocket on the same origin, consented captures + /admin gallery.
# Optional: DECART_API_KEY + LIVE_BACKEND=decart to use the hosted live model.
set -euo pipefail
cd "$(dirname "$0")/.."
. /workspace/venv/bin/activate
: "${ENGINE_SECRET:?set ENGINE_SECRET}" "${ADMIN_TOKEN:?set ADMIN_TOKEN}"
# CUDA 12 libraries that ship with torch, so onnxruntime (pose model) finds them.
export LD_LIBRARY_PATH="$(ls -d /usr/local/lib/python3*/dist-packages/nvidia/*/lib 2>/dev/null | tr '\n' ':')${LD_LIBRARY_PATH:-}"
export FASHN_WEIGHTS_DIR=/workspace/vto/models/fashn-vton-1.5/weights
export ESRGAN_WEIGHTS=/workspace/vto/models/RealESRGAN_x4plus.pth
export HF_HOME=/workspace/hf KEEPALIVE_FILE=/workspace/.keepalive
export CAPTURES_DIR=/workspace/vto/captures RECORDINGS_DIR=/workspace/vto/recordings
export LIVE_BACKEND="${LIVE_BACKEND:-local}" LIVE_TRANSPORT="${LIVE_TRANSPORT:-ws}"
export BRAND_NAME="${BRAND_NAME:-Try-On}" MAX_SESSION_SECONDS="${MAX_SESSION_SECONDS:-60}"
export TOKENS_PER_IP_PER_HOUR="${TOKENS_PER_IP_PER_HOUR:-60}" TRUST_PROXY=true

pgrep -f pod_watchdog.sh >/dev/null || IDLE_MIN="${IDLE_MIN:-30}" nohup bash scripts/pod_watchdog.sh >/workspace/watchdog.log 2>&1 &
pkill -f "uvicorn server.main:app" 2>/dev/null || true
nohup uvicorn server.main:app --host 0.0.0.0 --port 8000 --ws-max-size 4194304 --timeout-keep-alive 75 \
  >/workspace/site.log 2>&1 &
for i in $(seq 1 60); do curl -fs localhost:8000/health >/dev/null && break; sleep 2; done
curl -fs localhost:8000/health; echo
echo "site up on :8000 (log: /workspace/site.log)"
