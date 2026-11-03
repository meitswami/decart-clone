#!/usr/bin/env bash
# One-off GPU job: install the MediaPipe parser on the volume, then run the same
# try-ons with the old and the new parser. Results + logs in /workspace/ab
# (served on port 8000 by the start command). Stops its own pod at the end.
set -x
cd /workspace/app/virtual-tryon
bash scripts/setup_mediapipe.sh > /workspace/ab/setup.log 2>&1
. /workspace/env312/bin/activate
export LD_LIBRARY_PATH="$(ls -d /workspace/env312/lib/python3*/site-packages/nvidia/*/lib /workspace/env312/syslib/usr/lib/x86_64-linux-gnu | tr '\n' ':')"
export FASHN_WEIGHTS_DIR=/workspace/vto/models/fashn-vton-1.5/weights ESRGAN_WEIGHTS=/workspace/vto/models/RealESRGAN_x4plus.pth
export HF_HOME=/workspace/hf MEDIAPIPE_MODELS=/workspace/vto/models/mediapipe
case " ${AB_MODES:-fashn mediapipe} " in *" fashn "*)
  PYTHONPATH=/workspace/app/virtual-tryon python scripts/ab_parser.py fashn /workspace/ab > /workspace/ab/fashn.log 2>&1 ;; esac
PYTHONPATH=/workspace/app/virtual-tryon/shims:/workspace/app/virtual-tryon python scripts/ab_parser.py mediapipe /workspace/ab > /workspace/ab/mediapipe.log 2>&1
echo ALL_DONE > /workspace/ab/done.txt
sleep "${HOLD_SECONDS:-1200}"
python3 - <<'PY'
import json, os, urllib.request
q = 'mutation { podStop(input:{podId:"%s"}) { id } }' % os.environ.get("RUNPOD_POD_ID", "")
urllib.request.urlopen(urllib.request.Request("https://api.runpod.io/graphql", data=json.dumps({"query": q}).encode(),
    headers={"Authorization": "Bearer " + os.environ.get("RUNPOD_API_KEY", ""), "Content-Type": "application/json",
             "User-Agent": "vto-ab/1.0"}), timeout=30).read()
PY
