"""On-demand GPU. The site (VPS) wakes a RunPod GPU only when a shopper starts a
try-on; the GPU switches itself off after GPU_IDLE_MINUTES with no use
(scripts/pod_watchdog.sh). Models live on a RunPod network volume, so any GPU in
that data centre can start with everything already installed.

Wake order:
  1. our pod is RUNNING           -> wait until its worker reports models loaded
  2. our pod is stopped (EXITED)  -> resume it (fastest)
  3. resume refused (its GPU was taken) or no pod yet -> create a fresh pod on
     the same network volume (no reinstall), retire the old empty pod

The page polls /gpu/status and shows a loader with the stage and an estimate
learned from past wake-ups.
"""
from __future__ import annotations

import asyncio
import json
import logging
import statistics
import time
from pathlib import Path

import httpx

from .config import settings

log = logging.getLogger("tryon.gpu")

UA = {"User-Agent": "vto-server/1.0"}  # RunPod's proxy rejects default library user-agents
DEFAULT_ETA = {"resume": 100, "create": 210, "boot": 75}
STAGES = {"request": "Finding a free GPU", "boot": "Starting the GPU", "models": "Loading the AI models"}


class GpuPool:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self._live: dict = {"state": "unknown"}
        self._ip_hits: dict[str, list[float]] = {}
        self._ready_cache = (0.0, False)
        self._state_file = Path(settings.gpu_state_file)
        self._state = self._load()

    # ---------- persistence ----------
    def _load(self) -> dict:
        try:
            return json.loads(self._state_file.read_text())
        except Exception:
            return {"pod_id": None, "history": [], "wakes": {}}

    def _save(self) -> None:
        self._state_file.parent.mkdir(parents=True, exist_ok=True)
        self._state_file.write_text(json.dumps(self._state, indent=1))

    @property
    def pod_id(self) -> str | None:
        return self._state.get("pod_id")

    def worker_url(self) -> str | None:
        return settings.gpu_worker_url_template.format(pod=self.pod_id) if self.pod_id else None

    def eta(self, kind: str) -> int:
        past = [h["seconds"] for h in self._state.get("history", []) if h["kind"] == kind][-5:]
        return int(statistics.median(past)) if past else DEFAULT_ETA[kind]

    # ---------- RunPod API ----------
    def _auth(self) -> dict:
        return {"Authorization": f"Bearer {settings.runpod_api_key}", **UA}

    async def _gql(self, c: httpx.AsyncClient, query: str, variables: dict | None = None) -> dict:
        r = await c.post(settings.runpod_graphql_url, json={"query": query, "variables": variables or {}}, headers=self._auth())
        data = r.json()
        if data.get("errors"):
            raise RuntimeError(data["errors"][0].get("message", "RunPod error"))
        return data["data"]

    async def _pod(self, c: httpx.AsyncClient) -> dict | None:
        if not self.pod_id:
            return None
        try:
            d = await self._gql(c, "query($i: PodFilter) { pod(input: $i) { id desiredStatus "
                                   "runtime { uptimeInSeconds } } }", {"i": {"podId": self.pod_id}})
        except RuntimeError:
            return None
        return d.get("pod")

    async def _create(self, c: httpx.AsyncClient) -> str:
        origin = settings.origin
        env = {"WORKER_TOKEN": settings.worker_token, "ENGINE_SECRET": settings.engine_secret,
               "IDLE_MIN": str(settings.gpu_idle_minutes), "GIT_BRANCH": settings.gpu_git_branch,
               "PUBLIC_ORIGIN": origin}
        if origin:
            env["CAPTURE_UPLOAD_URL"] = f"{origin}/captures/upload"
        body = {
            "name": "vto-gpu", "imageName": settings.gpu_image, "cloudType": "SECURE", "gpuCount": 1,
            "gpuTypeIds": [g.strip() for g in settings.gpu_types.split(",") if g.strip()],
            "gpuTypePriority": "custom", "networkVolumeId": settings.gpu_network_volume,
            "volumeMountPath": "/workspace", "containerDiskInGb": 30, "ports": ["8000/http"], "env": env,
            "dockerStartCmd": ["bash", "-c",
                               "bash /workspace/app/virtual-tryon/scripts/pod_boot.sh & exec /start.sh"],
        }
        r = await c.post(f"{settings.runpod_rest_url}/pods", json=body, headers=self._auth())
        if r.status_code >= 300:
            raise RuntimeError("no_gpu" if "no instances" in r.text.lower() else f"create failed: {r.text[:200]}")
        return r.json()["id"]

    async def _healthy(self, c: httpx.AsyncClient) -> bool:
        try:
            r = await c.get(f"{self.worker_url()}/health", headers=UA, timeout=8)
            return r.status_code == 200 and bool(r.json().get("model_loaded"))
        except Exception:
            return False

    # ---------- public ----------
    async def is_ready(self) -> bool:
        t, ok = self._ready_cache
        if time.time() - t < 10:
            return ok
        async with httpx.AsyncClient(timeout=15) as c:
            ok = bool(self.pod_id) and await self._healthy(c)
        self._ready_cache = (time.time(), ok)
        return ok

    async def status(self) -> dict:
        if self._task and not self._task.done():
            s = dict(self._live)
            s["elapsed_s"] = int(time.time() - s.get("started", time.time()))
            return {k: v for k, v in s.items() if k != "started"}
        ready = await self.is_ready()
        if ready:
            return {"state": "ready"}
        if self._live.get("state") == "unavailable" and time.time() - self._live.get("at", 0) < 60:
            return {"state": "unavailable", "message": self._live.get("message")}
        return {"state": "asleep", "eta_s": self.eta("resume")}

    def _rate_ok(self, ip: str) -> bool:
        now = time.time()
        hits = [t for t in self._ip_hits.get(ip, []) if now - t < 3600]
        if len(hits) >= settings.gpu_wakes_per_ip_per_hour:
            return False
        self._ip_hits[ip] = hits + [now]
        day = time.strftime("%Y-%m-%d", time.gmtime())
        wakes = self._state.setdefault("wakes", {})
        if wakes.get(day, 0) >= settings.gpu_daily_wake_limit:
            return False
        return True

    async def wake(self, ip: str) -> dict:
        if settings.gpu_mode != "runpod" or not settings.runpod_api_key or not settings.gpu_network_volume:
            return {"state": "unavailable", "message": "Try-on servers aren't configured."}
        async with self._lock:
            if self._task and not self._task.done():
                return await self.status()
            if await self.is_ready():
                return {"state": "ready"}
            if not self._rate_ok(ip):
                return {"state": "unavailable", "message": "Too many try-ons started. Please wait a few minutes."}
            self._live = {"state": "waking", "stage": STAGES["request"], "kind": "resume",
                          "eta_s": self.eta("resume"), "started": time.time()}
            self._task = asyncio.create_task(self._wake())
        return await self.status()

    async def _wake(self) -> None:
        t0 = time.time()
        day = time.strftime("%Y-%m-%d", time.gmtime())
        self._state.setdefault("wakes", {})[day] = self._state["wakes"].get(day, 0) + 1
        try:
            async with httpx.AsyncClient(timeout=30) as c:
                pod = await self._pod(c)
                kind = "resume"
                if pod and pod.get("desiredStatus") == "RUNNING":
                    kind = "boot"
                elif pod and pod.get("desiredStatus") == "EXITED":
                    try:
                        await self._gql(c, "mutation($i: PodResumeInput!) { podResume(input: $i) { id } }",
                                        {"i": {"podId": self.pod_id, "gpuCount": 1}})
                    except RuntimeError as e:
                        log.info("resume refused (%s); creating a fresh pod", e)
                        kind = "create"
                else:
                    kind = "create"
                if kind == "create":
                    self._live.update(kind="create", eta_s=self.eta("create"))
                    old = self.pod_id
                    self._state["pod_id"] = await self._create(c)
                    self._save()
                    log.info("created pod %s (replacing %s)", self.pod_id, old)
                    if old and settings.gpu_cleanup_superseded:
                        try:  # old pod holds no data (models are on the network volume)
                            await c.delete(f"{settings.runpod_rest_url}/pods/{old}", headers=self._auth())
                        except Exception:
                            pass
                else:
                    self._live.update(kind=kind, eta_s=self.eta(kind))
                self._live["stage"] = STAGES["boot"]
                deadline = t0 + settings.gpu_wake_timeout
                while time.time() < deadline:
                    pod = await self._pod(c)
                    if pod and (pod.get("runtime") or {}).get("uptimeInSeconds"):
                        break
                    await asyncio.sleep(3)
                self._live["stage"] = STAGES["models"]
                while time.time() < deadline:
                    if await self._healthy(c):
                        break
                    await asyncio.sleep(3)
                else:
                    raise RuntimeError("timeout")
            secs = round(time.time() - t0)
            hist = self._state.setdefault("history", [])
            hist.append({"kind": kind, "seconds": secs, "ts": int(time.time())})
            self._state["history"] = hist[-30:]
            self._save()
            self._ready_cache = (time.time(), True)
            self._live = {"state": "ready"}
            log.info("GPU ready via %s in %ss", kind, secs)
        except Exception as e:
            msg = ("All try-on GPUs are busy right now. Please try again in a few minutes."
                   if "no_gpu" in str(e) else "The try-on servers didn't start. Please try again.")
            log.warning("GPU wake failed: %s", e)
            self._save()
            self._live = {"state": "unavailable", "message": msg, "at": time.time()}


pool = GpuPool()
