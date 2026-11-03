#!/usr/bin/env python3
"""Create / start / stop / inspect the GPU dev pod on RunPod.

Needs RUNPOD_API_KEY in the environment (never commit it).

  python scripts/runpod_pod.py status
  python scripts/runpod_pod.py create  --gpu "NVIDIA GeForce RTX 4090" [--secure]
  python scripts/runpod_pod.py start   <pod_id>
  python scripts/runpod_pod.py stop    <pod_id>

Every start/stop is appended to data/gpu_spend.csv so cost is traceable.
Only pods whose name starts with "vto-" are ever started or stopped by this
script; other pods in the account are left alone.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

API = "https://api.runpod.io/graphql"
IMAGE = "runpod/pytorch:1.0.2-cu1281-torch280-ubuntu2404"
NAME = "vto-dev"
IST = timezone(timedelta(hours=5, minutes=30))
SPEND_CSV = Path(__file__).resolve().parent.parent / "data" / "gpu_spend.csv"


def gql(query: str, variables: dict | None = None) -> dict:
    key = os.environ.get("RUNPOD_API_KEY")
    if not key:
        sys.exit("RUNPOD_API_KEY is not set")
    req = urllib.request.Request(
        API, data=json.dumps({"query": query, "variables": variables or {}}).encode(),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                 "User-Agent": "vto-pod-helper/1.0"})
    with urllib.request.urlopen(req, timeout=60) as r:
        out = json.load(r)
    if out.get("errors"):
        raise RuntimeError(json.dumps(out["errors"])[:600])
    return out["data"]


def log_spend(event: str, pod: dict) -> None:
    SPEND_CSV.parent.mkdir(parents=True, exist_ok=True)
    new = not SPEND_CSV.exists()
    with SPEND_CSV.open("a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["time_ist", "event", "pod_id", "name", "gpu", "cost_per_hr_usd"])
        w.writerow([datetime.now(IST).strftime("%Y-%m-%d %H:%M"), event, pod.get("id"),
                    pod.get("name"), (pod.get("machine") or {}).get("gpuDisplayName", ""),
                    pod.get("costPerHr")])


POD_FIELDS = """id name desiredStatus costPerHr imageName gpuCount
  machine { gpuDisplayName podHostId }
  runtime { uptimeInSeconds ports { ip isIpPublic privatePort publicPort type } }"""


def status() -> list[dict]:
    pods = gql(f"query {{ myself {{ clientBalance currentSpendPerHr pods {{ {POD_FIELDS} }} }} }}")["myself"]
    print(f"balance ${pods['clientBalance']:.2f}  spending ${pods['currentSpendPerHr']}/hr")
    for p in pods["pods"]:
        up = (p.get("runtime") or {}).get("uptimeInSeconds")
        print(f"  {p['id']}  {p['name']:<28} {p['desiredStatus']:<8} ${p['costPerHr']}/hr  up={up}s")
    return pods["pods"]


# Preferred GPUs, best value first. All are Ada/Blackwell/Ampere with >=24 GB.
GPU_PRIORITY = [
    "NVIDIA GeForce RTX 4090",
    "NVIDIA RTX 6000 Ada Generation",
    "NVIDIA L40S",
    "NVIDIA GeForce RTX 5090",
    "NVIDIA RTX A6000",
]


def create_rest(env: dict[str, str], cloud: str = "SECURE", gpus: list[str] | None = None) -> dict:
    """Create via the REST API, which accepts a priority list of GPU types."""
    key = os.environ.get("RUNPOD_API_KEY") or sys.exit("RUNPOD_API_KEY is not set")
    body = {
        "name": NAME, "imageName": IMAGE, "cloudType": cloud, "gpuCount": 1,
        "gpuTypeIds": gpus or GPU_PRIORITY, "gpuTypePriority": "custom",
        "allowedCudaVersions": ["12.8", "12.9", "13.0"],  # image is CUDA 12.8.1
        "containerDiskInGb": int(os.environ.get("CONTAINER_GB", 40)),
        "volumeInGb": int(os.environ.get("VOLUME_GB", 80)), "volumeMountPath": "/workspace",
        "minRAMPerGPU": int(os.environ.get("MIN_MEM_GB", 24)),
        "minVCPUPerGPU": int(os.environ.get("MIN_VCPU", 6)),
        "ports": ["8888/http", "8000/http", "8900/http", "22/tcp"], "env": env,
    }
    req = urllib.request.Request("https://rest.runpod.io/v1/pods", data=json.dumps(body).encode(),
                                 headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                                          "User-Agent": "vto-pod-helper/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            pod = json.load(r)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"{e.code} {e.read().decode()[:400]}") from None
    rec = {"id": pod["id"], "name": pod["name"], "costPerHr": pod.get("costPerHr"),
           "machine": {"gpuDisplayName": (pod.get("gpu") or {}).get("displayName", "")}}
    log_spend("create", rec)
    print(json.dumps(rec))
    return pod


def create(gpu: str, secure: bool, env: dict[str, str]) -> dict:
    inp = {
        "cloudType": "SECURE" if secure else "COMMUNITY",
        "gpuCount": 1, "gpuTypeId": gpu, "name": NAME, "imageName": IMAGE,
        "containerDiskInGb": int(os.environ.get("CONTAINER_GB", 60)), "volumeInGb": int(os.environ.get("VOLUME_GB", 100)), "volumeMountPath": "/workspace",
        "minVcpuCount": int(os.environ.get("MIN_VCPU", 8)), "minMemoryInGb": int(os.environ.get("MIN_MEM_GB", 30)),
        "ports": "8888/http,8000/http,8900/http,22/tcp",
        "env": [{"key": k, "value": v} for k, v in env.items()],
    }
    pod = gql(f"mutation($i: PodFindAndDeployOnDemandInput) {{ podFindAndDeployOnDemand(input: $i) {{ {POD_FIELDS} }} }}",
              {"i": inp})["podFindAndDeployOnDemand"]
    log_spend("create", pod)
    print(json.dumps({k: pod[k] for k in ("id", "name", "costPerHr")}))
    return pod


def _guard(pod_id: str) -> dict:
    pods = gql(f"query {{ myself {{ pods {{ {POD_FIELDS} }} }} }}")["myself"]["pods"]
    pod = next((p for p in pods if p["id"] == pod_id), None)
    if not pod:
        sys.exit(f"no pod {pod_id}")
    if not pod["name"].startswith("vto-"):
        sys.exit(f"refusing to touch non-vto pod {pod['name']}")
    return pod


def start(pod_id: str) -> None:
    pod = _guard(pod_id)
    gql('mutation($i: PodResumeInput!) { podResume(input: $i) { id desiredStatus } }',
        {"i": {"podId": pod_id, "gpuCount": 1}})
    log_spend("start", pod)
    print("started", pod_id)


def stop(pod_id: str) -> None:
    pod = _guard(pod_id)
    gql('mutation($i: PodStopInput!) { podStop(input: $i) { id desiredStatus } }', {"i": {"podId": pod_id}})
    log_spend("stop", pod)
    print("stopped", pod_id)


def wait_running(pod_id: str, timeout: int = 900) -> dict:
    t0 = time.time()
    while time.time() - t0 < timeout:
        pod = _guard(pod_id)
        if (pod.get("runtime") or {}).get("uptimeInSeconds"):
            return pod
        time.sleep(10)
    raise TimeoutError("pod did not come up")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["status", "create", "start", "stop", "wait"])
    ap.add_argument("pod_id", nargs="?")
    ap.add_argument("--gpu", default="NVIDIA GeForce RTX 4090")
    ap.add_argument("--secure", action="store_true")
    ap.add_argument("--env", action="append", default=[], help="KEY=VALUE for the pod")
    a = ap.parse_args()
    if a.cmd == "status":
        status()
    elif a.cmd == "create":
        create(a.gpu, a.secure, dict(e.split("=", 1) for e in a.env))
    elif a.cmd == "wait":
        print(json.dumps(wait_running(a.pod_id)["runtime"], indent=1))
    else:
        if not a.pod_id:
            sys.exit("pod_id required")
        {"start": start, "stop": stop}[a.cmd](a.pod_id)
