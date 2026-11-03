#!/usr/bin/env bash
# One-time: add the MediaPipe body parser (Apache-2.0) to the self-contained env
# on the network volume and download its models. Safe to re-run.
set -euxo pipefail
ENV=/workspace/env312
. "$ENV/bin/activate"
# --no-deps: MediaPipe's own deps list pulls the GUI OpenCV build; we already
# have opencv-python-headless and only need these extras.
pip install -q --no-deps "mediapipe==1.2.0"
pip install -q "absl-py~=2.3" "flatbuffers~=25.9" certifi matplotlib
# MediaPipe's C library links libEGL (OpenGL ES loader) even on CPU. The slim
# base image has none, so keep the Debian libs next to the env on the volume.
SYS="$ENV/syslib"
if [ ! -e "$SYS/usr/lib/x86_64-linux-gnu/libEGL.so.1" ]; then
  apt-get update -qq
  mkdir -p /tmp/debs "$SYS" && cd /tmp/debs
  apt-get download -qq libegl1 libglvnd0 libgles2 libgl1 libglx0 libopengl0
  for d in *.deb; do dpkg -x "$d" "$SYS"; done
  cd - >/dev/null
fi
export LD_LIBRARY_PATH="$SYS/usr/lib/x86_64-linux-gnu${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
M=/workspace/vto/models/mediapipe
mkdir -p "$M"
python - <<'PY'
import os, urllib.request
base = "https://storage.googleapis.com/mediapipe-models/"
files = {
    "selfie_multiclass_256x256.tflite": "image_segmenter/selfie_multiclass_256x256/float32/latest/selfie_multiclass_256x256.tflite",
    "pose_landmarker_heavy.task": "pose_landmarker/pose_landmarker_heavy/float16/latest/pose_landmarker_heavy.task",
}
for name, path in files.items():
    dst = os.path.join("/workspace/vto/models/mediapipe", name)
    if not os.path.exists(dst) or os.path.getsize(dst) < 1_000_000:
        urllib.request.urlretrieve(base + path, dst + ".part"); os.replace(dst + ".part", dst)
    print(name, os.path.getsize(dst))
PY
MEDIAPIPE_MODELS="$M" python - <<'PY'
import sys, numpy as np
sys.path.insert(0, "/workspace/app/virtual-tryon")
from pipeline import body_parse as b
p = b.get()  # really loads the models (needs libEGL), not just the import
lab = p.predict(np.full((480, 360, 3), 128, np.uint8))
print("parser ok:", p is not None, lab.shape)
PY
echo MEDIAPIPE_DONE
