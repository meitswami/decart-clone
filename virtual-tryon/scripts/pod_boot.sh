#!/usr/bin/env bash
# Runs at EVERY GPU pod boot (pod dockerStartCmd), from the network volume.
# Pulls the latest code, then starts the idle watchdog and the GPU worker.
exec >>/workspace/boot.log 2>&1
echo "$(date -u +%FT%TZ) boot pod=${RUNPOD_POD_ID:-?} branch=${GIT_BRANCH:-main}"
cd /workspace/app || { echo "no /workspace/app on the volume"; exit 1; }
if timeout 90 git fetch -q --depth 1 origin "${GIT_BRANCH:-main}"; then
  git reset -q --hard FETCH_HEAD && git log --oneline -1
else
  echo "code update skipped (network); using the copy on the volume"
fi
cd /workspace/app/virtual-tryon
. /workspace/venv/bin/activate
export LD_LIBRARY_PATH="$(ls -d /usr/local/lib/python3*/dist-packages/nvidia/*/lib 2>/dev/null | tr '\n' ':')${LD_LIBRARY_PATH:-}"
export FASHN_WEIGHTS_DIR=/workspace/vto/models/fashn-vton-1.5/weights
export ESRGAN_WEIGHTS=/workspace/vto/models/RealESRGAN_x4plus.pth
export HF_HOME=/workspace/hf KEEPALIVE=/workspace/.keepalive KEEPALIVE_FILE=/workspace/.keepalive
export CAPTURES_DIR=/workspace/vto/captures
IDLE_MIN="${IDLE_MIN:-15}" nohup bash scripts/pod_watchdog.sh >>/workspace/watchdog.log 2>&1 &
exec uvicorn gpu.worker:app --host 0.0.0.0 --port 8000 --ws-max-size 4194304 --timeout-keep-alive 75
