"""On-demand GPU. The site (VPS) wakes a RunPod GPU only when a shopper starts a
try-on and stops it after GPU_IDLE_MINUTES with no use (guard below; the GPU's
own scripts/pod_watchdog.sh is a backup). Models live on a RunPod network volume, so any GPU in
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
import base64
import json
import logging
import re
import statistics
import time
from pathlib import Path

import httpx

from .config import settings

log = logging.getLogger("tryon.gpu")

UA = {"User-Agent": "vto-server/1.0"}  # RunPod's proxy rejects default library user-agents
# Measured on RunPod (EU-RO-1, Nov 2026): finding a GPU 0-150 s, starting 15-60 s,
# loading the models from the network volume ~190 s. Replaced by real history.
DEFAULT_ETA = {"resume": 240, "create": 360, "boot": 200, "any": 330}

# Runs first on every GPU boot: refresh the code on the volume from GitHub
# (python only: the slim image has no git/curl), keep the old copy if offline.
FETCH_CODE = (
    "import io,os,shutil,tarfile,urllib.request\n"
    "b=os.environ.get('GIT_BRANCH','main'); r=os.environ.get('GIT_REPO','meitswami/decart-clone')\n"
    "h={'User-Agent':'vto-pod'}\n"
    "if os.environ.get('GITHUB_TOKEN'): h['Authorization']='token '+os.environ['GITHUB_TOKEN']\n"
    "try:\n"
    " d=urllib.request.urlopen(urllib.request.Request(f'https://api.github.com/repos/{r}/tarball/{b}',headers=h),timeout=90).read()\n"
    " tmp='/workspace/app.new'; shutil.rmtree(tmp,ignore_errors=True); tarfile.open(fileobj=io.BytesIO(d)).extractall(tmp)\n"
    " src=os.path.join(tmp,os.listdir(tmp)[0]); shutil.rmtree('/workspace/app.old',ignore_errors=True)\n"
    " os.path.exists('/workspace/app') and os.rename('/workspace/app','/workspace/app.old')\n"
    " os.rename(src,'/workspace/app'); print('code updated', b)\n"
    "except Exception as e: print('code update skipped:', e)\n"
)
STAGES = {"request": "Finding a free GPU", "boot": "Starting the GPU", "models": "Loading the AI models"}


class GpuPool:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self._live: dict = {"state": "unknown"}
        self._ip_hits: dict[str, list[float]] = {}
        self._ready_cache = (0.0, False)
        self._last_activity = time.time()
        self._state_file = Path(settings.gpu_state_file)
        self._state = self._load()
        self._usage_file = self._state_file.with_name("gpu_usage.json")
        self._usage = self._load_usage()
        self._live_sessions: dict[str, float] = {}   # live session id -> start time
        self._account = (0.0, None)                  # (fetched at, RunPod account info)

    # ---------- persistence ----------
    def _load(self) -> dict:
        try:
            return json.loads(self._state_file.read_text())
        except Exception:
            return {"pod_id": None, "history": [], "wakes": {}}

    def _save(self) -> None:
        self._state_file.parent.mkdir(parents=True, exist_ok=True)
        self._state_file.write_text(json.dumps(self._state, indent=1))

    # ---------- cost ledger: one entry per GPU session (wake -> sleep) ----------
    def _load_usage(self) -> dict:
        try:
            return json.loads(self._usage_file.read_text())
        except Exception:
            return {"sessions": []}

    def _save_usage(self) -> None:
        try:
            self._usage_file.parent.mkdir(parents=True, exist_ok=True)
            self._usage["sessions"] = self._usage["sessions"][-300:]
            self._usage_file.write_text(json.dumps(self._usage, indent=1))
        except Exception:
            log.exception("could not save GPU usage")

    def _open_session(self) -> dict | None:
        s = self._usage["sessions"]
        return s[-1] if s and not s[-1].get("end") else None

    def _begin_session(self, kind: str) -> None:
        if self._open_session():
            return
        self._usage["sessions"].append({
            "pod": self.pod_id, "kind": kind, "start": time.time(), "ready": None, "end": None,
            "gpu": None, "rate": None, "ok": None,
            "photo": {"n": 0, "s": 0.0}, "live": {"n": 0, "s": 0.0}, "fit": {"n": 0, "s": 0.0}})
        self._save_usage()

    def _end_session(self, at: float | None = None, ok: bool | None = None) -> None:
        s = self._open_session()
        if s:
            s["end"] = at or time.time()
            if ok is not None:
                s["ok"] = ok
            self._save_usage()

    def record(self, kind: str, seconds: float) -> None:
        """GPU work done for a shopper: kind = photo | live | fit."""
        s = self._open_session()
        if s and kind in ("photo", "live", "fit"):
            s[kind]["n"] += 1
            s[kind]["s"] = round(s[kind]["s"] + max(0.0, float(seconds)), 1)
            self._save_usage()

    def live_started(self, sid: str) -> None:
        self.touch()
        self._live_sessions[sid] = time.time()

    def live_ended(self, sid: str) -> None:
        t = self._live_sessions.pop(sid, None)
        if t:
            self.touch()
            self.record("live", min(time.time() - t, settings.max_session_seconds + 5))

    def _live_active(self) -> bool:
        now, limit = time.time(), settings.max_session_seconds + 30
        for sid, t in list(self._live_sessions.items()):
            if now - t > limit:  # tab closed without ending: count its full length
                self._live_sessions.pop(sid, None)
                self.record("live", settings.max_session_seconds)
        return bool(self._live_sessions)

    @property
    def pod_id(self) -> str | None:
        return self._state.get("pod_id")

    def worker_url(self) -> str | None:
        return settings.gpu_worker_url_template.format(pod=self.pod_id) if self.pod_id else None

    def eta(self, kind: str) -> int:
        hist = self._state.get("history", [])
        past = [h["seconds"] for h in hist if kind == "any" or h["kind"] == kind][-5:]
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
                                   "machine { gpuDisplayName } runtime { uptimeInSeconds } } }",
                                {"i": {"podId": self.pod_id}})
        except RuntimeError:
            return None
        return d.get("pod")

    @staticmethod
    def _gpu_allowed(pod: dict | None) -> bool:
        """Is this pod's GPU still on our list? (e.g. a slow GPU we have since dropped)"""
        name = ((pod or {}).get("machine") or {}).get("gpuDisplayName")
        if not name:
            return True
        pat = re.compile(r"\b" + re.escape(name) + r"\b", re.I)
        return any(pat.search(t) for t in settings.gpu_types.split(","))

    async def _create_any(self, c: httpx.AsyncClient) -> str:
        """Try every volume (data centre) in order, retrying until a GPU frees up."""
        volumes = [v.strip() for v in settings.gpu_network_volume.split(",") if v.strip()]
        deadline = time.time() + settings.gpu_find_timeout
        attempt = 0
        while True:
            for vol in volumes:
                try:
                    return await self._create(c, vol)
                except RuntimeError as e:
                    if "no_gpu" not in str(e):
                        raise
            attempt += 1
            if time.time() > deadline:
                raise RuntimeError("no_gpu")
            self._live["stage"] = STAGES["request"]
            self._live["note"] = "GPUs are in high demand; still looking…"
            await asyncio.sleep(min(15, 5 + 3 * attempt))

    async def _create(self, c: httpx.AsyncClient, volume: str) -> str:
        origin = settings.origin
        env = {"WORKER_TOKEN": settings.worker_token, "ENGINE_SECRET": settings.engine_secret,
               # backup only: the site's guard sleeps the GPU after GPU_IDLE_MINUTES
               "IDLE_MIN": str(settings.gpu_idle_minutes + 3), "GIT_BRANCH": settings.gpu_git_branch,
               "GIT_REPO": settings.gpu_git_repo, "PUBLIC_ORIGIN": origin,
               "FETCH_CODE_B64": base64.b64encode(FETCH_CODE.encode()).decode(),
               "NVIDIA_DRIVER_CAPABILITIES": "compute,utility"}
        if settings.gpu_github_token:
            env["GITHUB_TOKEN"] = settings.gpu_github_token
        if origin:
            env["CAPTURE_UPLOAD_URL"] = f"{origin}/captures/upload"
        body = {
            "name": "vto-gpu", "imageName": settings.gpu_image, "cloudType": "SECURE", "gpuCount": 1,
            "gpuTypeIds": [g.strip() for g in settings.gpu_types.split(",") if g.strip()],
            "gpuTypePriority": "custom", "networkVolumeId": volume,
            "volumeMountPath": "/workspace", "containerDiskInGb": 30, "ports": ["8000/http"], "env": env,
            # Refresh the code on the volume FIRST (the boot script itself may be
            # new), then start the watchdog + worker (pod_boot.sh ends in uvicorn).
            "dockerStartCmd": ["bash", "-c",
                               "python3 -c 'import base64,os; exec(base64.b64decode(os.environ[\"FETCH_CODE_B64\"]))' "
                               ">> /workspace/boot.log 2>&1; "
                               "exec bash /workspace/app/virtual-tryon/scripts/pod_boot.sh"],
        }
        r = await c.post(f"{settings.runpod_rest_url}/pods", json=body, headers=self._auth())
        if r.status_code >= 300:
            raise RuntimeError("no_gpu" if "no instances" in r.text.lower() else f"create failed: {r.text[:200]}")
        return r.json()["id"]

    async def _health(self, c: httpx.AsyncClient) -> dict | None:
        try:
            r = await c.get(f"{self.worker_url()}/health", headers=UA, timeout=8)
            return r.json() if r.status_code == 200 else None
        except Exception:
            return None

    async def _healthy(self, c: httpx.AsyncClient) -> bool:
        h = await self._health(c)
        return bool(h and h.get("model_loaded"))

    async def _stop(self, c: httpx.AsyncClient) -> None:
        if self.pod_id:
            await self._gql(c, "mutation($i: PodStopInput!) { podStop(input: $i) { id } }",
                            {"i": {"podId": self.pod_id}})
            self._ready_cache = (time.time(), False)
            self._end_session()

    def touch(self) -> None:
        """A shopper did something that needs the GPU (backup idle timer)."""
        self._last_activity = time.time()

    async def guard(self) -> None:
        """Puts the GPU to sleep after GPU_IDLE_MINUTES with no use: no shopper
        request here, no live session open, and no work on the GPU itself (the
        worker reports seconds since its last job). Also stops a GPU that never
        becomes healthy, and closes the cost record when the GPU stopped itself."""
        unhealthy_since = None
        last_running = 0.0
        while True:
            await asyncio.sleep(20)
            try:
                if not self.pod_id or (self._task and not self._task.done()):
                    unhealthy_since = None
                    continue
                async with httpx.AsyncClient(timeout=20) as c:
                    pod = await self._pod(c)
                    now = time.time()
                    if not pod or pod.get("desiredStatus") != "RUNNING":
                        unhealthy_since = None
                        if self._open_session():  # stopped by its own watchdog / by hand
                            self._end_session(at=min(now, (last_running or now) + 20))
                        continue
                    last_running = now
                    if not self._open_session():  # running but not recorded (e.g. site restarted)
                        self._begin_session("boot")
                    h = await self._health(c)
                    healthy = bool(h and h.get("model_loaded"))
                    unhealthy_since = None if healthy else (unhealthy_since or now)
                    idle = now - self._last_activity
                    if h and h.get("idle_s") is not None:
                        idle = min(idle, float(h["idle_s"]))
                    if self._live_active():
                        idle = 0.0
                    limit = max(1.0, settings.gpu_idle_minutes) * 60
                    if (unhealthy_since and now - unhealthy_since > 600) or (healthy and idle >= limit):
                        log.info("guard: stopping GPU %s (idle %.0fs, unhealthy %s)", self.pod_id, idle,
                                 bool(unhealthy_since))
                        await self._stop(c)
                        unhealthy_since = None
            except Exception:
                log.exception("guard check failed")

    # ---------- costs (owner view) ----------
    async def _account_info(self) -> dict | None:
        t, info = self._account
        if info is not None and time.time() - t < 60:
            return info
        try:
            async with httpx.AsyncClient(timeout=20) as c:
                d = await self._gql(c, "{ myself { clientBalance currentSpendPerHr spendLimit "
                                       "networkVolumes { id size } } }")
            info = d.get("myself") or {}
        except Exception:
            log.exception("could not read RunPod balance")
            info = None
        self._account = (time.time(), info)
        return info

    async def costs(self) -> dict:
        now = time.time()
        fallback_rate = None
        rows = []
        for s in self._usage.get("sessions", []):
            rate = s.get("rate") or fallback_rate
            fallback_rate = rate or fallback_rate
            end = s.get("end") or now
            total = max(0.0, end - s["start"])
            boot = max(0.0, (s.get("ready") or end) - s["start"])
            work = {k: float(s[k]["s"]) for k in ("photo", "live", "fit")}
            avail = max(0.0, total - boot)
            if sum(work.values()) > avail > 0:  # overlapping jobs: share the awake time out
                f = avail / sum(work.values())
                work = {k: v * f for k, v in work.items()}
            idle = max(0.0, avail - sum(work.values()))
            per_s = (rate or 0) / 3600
            rows.append({
                "start": s["start"], "end": s.get("end"), "running": not s.get("end"), "gpu": s.get("gpu"),
                "rate_per_hr": rate, "kind": s.get("kind"), "ok": s.get("ok"),
                "seconds": {"total": round(total), "wake": round(boot), "idle": round(idle),
                            **{k: round(v) for k, v in work.items()}},
                "counts": {k: s[k]["n"] for k in ("photo", "live", "fit")},
                "cost": {"total": round(total * per_s, 4), "wake": round(boot * per_s, 4),
                         "idle": round(idle * per_s, 4), **{k: round(v * per_s, 4) for k, v in work.items()}},
            })
        acct = await self._account_info()
        vol_gb = 0
        if acct:
            ids = {v.strip() for v in settings.gpu_network_volume.split(",") if v.strip()}
            vol_gb = sum(v.get("size", 0) for v in acct.get("networkVolumes") or [] if v.get("id") in ids)

        def total(rs, key="total"):
            return round(sum(r["cost"][key] for r in rs), 4)
        today = time.strftime("%Y-%m-%d", time.localtime(now))
        rs_today = [r for r in rows if time.strftime("%Y-%m-%d", time.localtime(r["start"])) == today]
        sums = {k: {"n": sum(r["counts"][k] for r in rows), "cost": total(rows, k),
                    "seconds": sum(r["seconds"][k] for r in rows)} for k in ("photo", "live", "fit")}
        avg = {k: round(v["cost"] / v["n"], 4) if v["n"] else None for k, v in sums.items()}
        wakes = [r for r in rows if r["seconds"]["wake"]]
        return {
            "balance": round(acct["clientBalance"], 2) if acct and acct.get("clientBalance") is not None else None,
            "spend_limit": acct.get("spendLimit") if acct else None,
            "spend_per_hr_now": acct.get("currentSpendPerHr") if acct else None,
            "storage_per_month": round(vol_gb * 0.07, 2),
            "idle_minutes": settings.gpu_idle_minutes,
            "today": {"sessions": len(rs_today), "cost": total(rs_today)},
            "all_time": {"sessions": len(rows), "cost": total(rows), "wake": total(rows, "wake"),
                         "idle": total(rows, "idle"), **{k: v for k, v in sums.items()}},
            "average": {**avg, "wake": round(total(wakes, "wake") / len(wakes), 4) if wakes else None},
            "sessions": list(reversed(rows[-30:])),
        }

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
        return {"state": "asleep", "eta_s": self.eta("any")}

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
        self.touch()
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
                          "eta_s": self.eta("any"), "started": time.time()}
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
                    self._begin_session("boot")
                elif pod and pod.get("desiredStatus") == "EXITED" and not self._gpu_allowed(pod):
                    log.info("stopped pod has a GPU we no longer use (%s); creating a fresh pod",
                             (pod.get("machine") or {}).get("gpuDisplayName"))
                    kind = "create"
                elif pod and pod.get("desiredStatus") == "EXITED":
                    try:
                        await self._gql(c, "mutation($i: PodResumeInput!) { podResume(input: $i) { id } }",
                                        {"i": {"podId": self.pod_id, "gpuCount": 1}})
                        self._begin_session("resume")
                    except RuntimeError as e:
                        log.info("resume refused (%s); creating a fresh pod", e)
                        kind = "create"
                else:
                    kind = "create"
                if kind == "create":
                    self._live.update(kind="create", eta_s=self.eta("create"))
                    old = self.pod_id
                    self._state["pod_id"] = await self._create_any(c)
                    self._save()
                    self._begin_session("create")
                    log.info("created pod %s (replacing %s)", self.pod_id, old)
                    if old and settings.gpu_cleanup_superseded:
                        try:  # old pod holds no data (models are on the network volume)
                            await c.delete(f"{settings.runpod_rest_url}/pods/{old}", headers=self._auth())
                        except Exception:
                            pass
                else:
                    self._live.update(kind=kind, eta_s=self.eta(kind))
                self._live.pop("note", None)
                self._live["stage"] = STAGES["boot"]
                deadline = t0 + settings.gpu_wake_timeout
                boot_by, retries = time.time() + settings.gpu_boot_timeout, 1
                while time.time() < deadline:
                    pod = await self._pod(c)
                    if pod and (pod.get("runtime") or {}).get("uptimeInSeconds"):
                        break
                    if time.time() > boot_by and retries and deadline - time.time() > 240:
                        retries -= 1  # this machine is stuck: try another one
                        old = self.pod_id
                        log.warning("GPU %s didn't start in %ss; trying another machine", old,
                                    settings.gpu_boot_timeout)
                        await self._stop(c)
                        self._live.update(stage=STAGES["request"], note="Trying another GPU…")
                        kind = "create"
                        self._state["pod_id"] = await self._create_any(c)
                        self._save()
                        self._begin_session("create")
                        try:
                            await c.delete(f"{settings.runpod_rest_url}/pods/{old}", headers=self._auth())
                        except Exception:
                            pass
                        self._live.pop("note", None)
                        self._live["stage"] = STAGES["boot"]
                        boot_by = time.time() + settings.gpu_boot_timeout
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
            self.touch()  # the idle timer starts when the GPU is ready, not at boot
            s = self._open_session()
            if s:
                s["ready"], s["ok"] = time.time(), True
                try:
                    async with httpx.AsyncClient(timeout=20) as c:
                        d = await self._gql(c, "query($i: PodFilter) { pod(input: $i) { costPerHr "
                                               "machine { gpuDisplayName } } }", {"i": {"podId": self.pod_id}})
                    p = d.get("pod") or {}
                    s["rate"] = p.get("costPerHr")
                    s["gpu"] = (p.get("machine") or {}).get("gpuDisplayName")
                except Exception:
                    log.exception("could not read GPU price")
                self._save_usage()
            log.info("GPU ready via %s in %ss", kind, secs)
        except Exception as e:
            msg = ("All try-on GPUs are busy right now. Please try again in a few minutes."
                   if "no_gpu" in str(e) else "The try-on servers didn't start. Please try again.")
            log.warning("GPU wake failed: %s", e)
            self._save()
            try:  # never leave a half-started GPU billing
                async with httpx.AsyncClient(timeout=30) as c:
                    pod = await self._pod(c)
                    s = self._open_session()
                    if s:
                        s["ok"] = False
                    if pod and pod.get("desiredStatus") == "RUNNING":
                        await self._stop(c)
                        log.warning("stopped GPU %s after failed wake", self.pod_id)
                    self._end_session(ok=False)
            except Exception:
                log.exception("could not stop GPU after failed wake")
            self._live = {"state": "unavailable", "message": msg, "at": time.time()}


pool = GpuPool()
