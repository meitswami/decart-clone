#!/usr/bin/env bash
# Runs INSIDE the RunPod pod. Stops the pod when nobody is using it, so a
# dropped session or a forgotten pod can't burn credit.
#
#   nohup bash scripts/pod_watchdog.sh >/workspace/watchdog.log 2>&1 &
#
# Activity = the keepalive file was touched recently (every remote command we
# run touches it) OR the GPU is busy. The pod stops when:
#   - no activity for IDLE_MIN minutes (default 20), or
#   - it has been up HARD_CAP_MIN minutes (default 240) since the watchdog
#     started, busy or not (restart the watchdog to deliberately extend).
set -u
IDLE_MIN="${IDLE_MIN:-20}"
HARD_CAP_MIN="${HARD_CAP_MIN:-240}"
BUSY_GPU_PCT="${BUSY_GPU_PCT:-20}"
KEEPALIVE="${KEEPALIVE:-/workspace/.keepalive}"
START=$(date +%s)
touch "$KEEPALIVE"
log() { echo "$(date -u +%FT%TZ) $*"; }

stop_pod() {
  log "STOPPING pod ${RUNPOD_POD_ID:-?}: $1"
  sync
  # GraphQL with the pod-scoped key RunPod injects into every pod (verified to
  # authenticate); runpodctl is only a fallback (it is often unconfigured).
  out=$(curl -sS -m 30 https://api.runpod.io/graphql \
    -H "Authorization: Bearer ${RUNPOD_API_KEY:-}" -H "Content-Type: application/json" \
    -d "{\"query\":\"mutation { podStop(input:{podId:\\\"${RUNPOD_POD_ID}\\\"}) { id desiredStatus } }\"}")
  log "podStop -> $out"
  case "$out" in *EXITED*) return ;; esac
  runpodctl stop pod "${RUNPOD_POD_ID}" || log "runpodctl stop failed too"
}

log "watchdog up: idle=${IDLE_MIN}m cap=${HARD_CAP_MIN}m pod=${RUNPOD_POD_ID:-unknown}"
while sleep 60; do
  now=$(date +%s)
  util=$(nvidia-smi --query-gpu=utilization.gpu --format=csv,noheader,nounits 2>/dev/null | head -1 | tr -dc '0-9')
  if [ "${util:-0}" -ge "$BUSY_GPU_PCT" ]; then touch "$KEEPALIVE"; fi
  idle=$(( (now - $(stat -c %Y "$KEEPALIVE")) / 60 ))
  up=$(( (now - START) / 60 ))
  if [ $(( up % 10 )) -eq 0 ]; then log "up=${up}m idle=${idle}m gpu=${util:-?}%"; fi
  if [ "$idle" -ge "$IDLE_MIN" ]; then stop_pod "idle ${idle} min"; sleep 120; fi
  if [ "$up" -ge "$HARD_CAP_MIN" ]; then stop_pod "hard cap ${up} min"; sleep 120; fi
done
