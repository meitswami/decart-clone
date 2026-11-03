#!/usr/bin/env python3
"""Run commands on / move files to the RunPod pod over HTTPS, through the
pod's built-in Jupyter server behind RunPod's proxy (no SSH needed).

Env: POD_ID, JUPYTER_TOKEN  (the pod's JUPYTER_PASSWORD)

  python scripts/pod_exec.py run  "nvidia-smi"
  python scripts/pod_exec.py put  local.py /workspace/remote.py
  python scripts/pod_exec.py get  /workspace/out.png local.png

Every `run` touches /workspace/.keepalive so the idle watchdog knows work is
happening. Long jobs: start them with nohup into a log file and poll.
"""
from __future__ import annotations

import base64
import json
import os
import sys
import time
import uuid
import urllib.request

import websocket  # pip install websocket-client


def _base() -> tuple[str, dict]:
    pod, tok = os.environ["POD_ID"], os.environ["JUPYTER_TOKEN"]
    # RunPod's proxy (Cloudflare) rejects the default Python user-agent (error 1010).
    return f"https://{pod}-8888.proxy.runpod.net", {"Authorization": f"token {tok}", "User-Agent": "vto-pod-exec/1.0 (+curl-compatible)"}


def _req(method: str, path: str, body: dict | None = None, timeout: int = 120):
    base, h = _base()
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method,
                                 headers={**h, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
    return json.loads(raw) if raw else None


_KERNEL_FILE = os.path.join(os.environ.get("VTO_STATE_DIR", os.path.expanduser("~/.cache/vto")), "kernel_id")


def _kernel() -> str:
    try:
        kid = open(_KERNEL_FILE).read().strip()
        if any(k["id"] == kid for k in _req("GET", "/api/kernels")):
            return kid
    except Exception:
        pass
    kid = _req("POST", "/api/kernels", {"name": "python3"})["id"]
    os.makedirs(os.path.dirname(_KERNEL_FILE), exist_ok=True)
    open(_KERNEL_FILE, "w").write(kid)
    return kid


def run(cmd: str, timeout: int = 1800) -> int:
    base, h = _base()
    kid = _kernel()
    ws = websocket.create_connection(
        base.replace("https://", "wss://") + f"/api/kernels/{kid}/channels",
        header=[f"{k}: {v}" for k, v in h.items()], timeout=timeout)
    code = (
        "import subprocess, pathlib\n"
        "pathlib.Path('/workspace/.keepalive').touch()\n"
        f"p = subprocess.Popen({cmd!r}, shell=True, executable='/bin/bash', cwd='/workspace',"
        " stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)\n"
        "for line in p.stdout: print(line, end='', flush=True)\n"
        "print(f'__EXIT__{p.wait()}', flush=True)\n"
    )
    msg_id = uuid.uuid4().hex
    ws.send(json.dumps({
        "header": {"msg_id": msg_id, "username": "vto", "session": uuid.uuid4().hex,
                   "msg_type": "execute_request", "version": "5.3"},
        "parent_header": {}, "metadata": {}, "channel": "shell",
        "content": {"code": code, "silent": False, "store_history": False,
                    "user_expressions": {}, "allow_stdin": False, "stop_on_error": True},
    }))
    rc = None
    t0 = time.time()
    while time.time() - t0 < timeout:
        m = json.loads(ws.recv())
        if m.get("parent_header", {}).get("msg_id") != msg_id:
            continue
        t, c = m["msg_type"], m["content"]
        if t == "stream":
            for line in c["text"].splitlines(keepends=True):
                if line.startswith("__EXIT__"):
                    rc = int(line[8:].strip())
                else:
                    sys.stdout.write(line)
            sys.stdout.flush()
        elif t == "error":
            sys.stdout.write("\n".join(c.get("traceback", []))[-2000:] + "\n")
            rc = 1
        elif t == "status" and c.get("execution_state") == "idle":
            break
    ws.close()
    return 1 if rc is None else rc


def put(local: str, remote: str) -> None:
    content = base64.b64encode(open(local, "rb").read()).decode()
    path = remote.lstrip("/")
    _req("PUT", f"/api/contents/{path}", {"type": "file", "format": "base64", "content": content})
    print(f"put {local} -> /{path}")


def get(remote: str, local: str) -> None:
    path = remote.lstrip("/")
    out = _req("GET", f"/api/contents/{path}?content=1&format=base64&type=file", timeout=600)
    with open(local, "wb") as f:
        f.write(base64.b64decode(out["content"]))
    print(f"got /{path} -> {local}")


if __name__ == "__main__":
    op = sys.argv[1]
    if op == "run":
        sys.exit(run(sys.argv[2], int(sys.argv[3]) if len(sys.argv) > 3 else 1800))
    elif op == "put":
        put(sys.argv[2], sys.argv[3])
    elif op == "get":
        get(sys.argv[2], sys.argv[3])
    else:
        sys.exit(__doc__)
