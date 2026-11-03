# Work log — White-label Virtual Try-On

Every action, decision and GPU minute, newest at the bottom of each day.
Times are IST (UTC+05:30). No secrets are ever written here.

## GPU spend (RunPod)

| Start (IST) | Stop (IST) | Pod | GPU | $/hr | Minutes | Cost (USD) | Why |
|---|---|---|---|---|---|---|---|
| 12:41 | 12:43 | k5qhy8mcfist46 | RTX 6000 Ada 48 GB (secure, US-WA-1) | 0.99 | ~3 | ~0.05 | boot, watchdog, self-stop test |

Stopped pods still bill a little for their disk: this pod's 80 GB volume is
about $0.022/hr (~$0.53/day) while stopped. Machine-readable log of every
create/start/stop: `data/gpu_spend.csv`.

Rules we follow:
- Pod runs only while work is happening. An in-pod watchdog
  (`scripts/pod_watchdog.sh`) stops the pod by itself after 20 min with no
  activity and low GPU use, and after a hard cap of 4 h, even if the Claude
  session drops.
- Old pods already in the account are never touched.

---

## 2026-11-03

### 12:11 — Decart white-label token wiring
- Attached repo `meitswami/decart-clone`. Found the root Node app (the live
  site) already mints scoped Decart keys correctly; only `virtual-tryon/`
  (Python) had the `TODO_scoped_token` placeholder.
- Ported the same method: `server/decart_tokens.py` →
  `POST https://api.decart.ai/v1/client/tokens` (header `X-API-KEY`), key
  locked to `DECART_MODEL`, `PUBLIC_ORIGIN`, `MAX_SESSION_SECONDS`, TTL 120 s.
- `/live/token`: origin lock, access code, per-IP limit, per-store/site caps,
  refund on mint failure, neutral (white-label) error text.
- `server/sessions.py`: server-measured session time; abandoned sessions
  charged the full cap.
- Consent only gates recording, not the try-on (DPDP Act).
- Python app port 3070 → **3071** (3070 is the production Node app on the VPS).
- Browser live client bundled with `@decartai/sdk` 0.2.12 (same pin as root).
- Tested: stub Decart (request shape, limits, refund), real Decart with a fake
  key (401 handled), headless Chrome end-to-end up to Decart signalling.
- Pushed branch `virtual-tryon-decart-token`, opened PR #2. Nothing outside
  `virtual-tryon/` changed.

### 12:17 — RunPod key received
- Verified with a read-only query: balance $47.89, $0/hr spend, 5 old pods
  all EXITED (left untouched). Key kept in a private file outside the repo.
- Advised the owner to rotate the key after setup (it was pasted in chat)
  and enable MFA.

### 12:32 — Owner approved GPU use (RTX 4090), with auto-off when idle
- Research findings that shape the plan:
  - FASHN VTON v1.5 (Apache-2.0): `TryOnPipeline(weights_dir)` →
    `pipeline(person_image, garment_image, category)`; ~2 GB weights; bf16 on
    Ampere+.
  - StreamDiffusionV2 (Apache-2.0) is **text-prompt video-to-video only**:
    no garment-image conditioning, so it cannot keep a real product's print
    or logo by itself.
- Plan:
  - **4K photo:** run FASHN on a person crop at model resolution, upscale the
    result 4x, and paste only the changed garment region back into the
    original full-resolution photo, so face/background keep true 4K detail.
  - **Live (local engine v1):** photo try-on on a keyframe every ~1 s, with
    the dressed garment tracked onto every camera frame in between (pose +
    optical flow + body mask). Keeps exact garment detail; may smear on very
    fast motion until the next keyframe lands.
- GPU image chosen: `runpod/pytorch:1.0.2-cu1281-torch280-ubuntu2404`.

### 12:36–12:43 — GPU box
- RTX 4090 sold out on RunPod (community and secure); RTX 6000 Ada also
  refused via the old GraphQL call. Added REST creation with a GPU priority
  list (`scripts/runpod_pod.py create_rest`). Got **RTX 6000 Ada 48 GB**,
  secure cloud, US-WA-1, $0.99/hr (same chip family as the 4090, 2x memory).
- Access: RunPod's HTTPS proxy → the pod's Jupyter (`scripts/pod_exec.py`).
  RunPod's Cloudflare blocks Python's default user-agent (error 1010); fixed
  by sending our own.
- Box: driver CUDA 13.2, torch 2.8.0+cu128 working, 128 vCPU, 503 GB RAM.
- Watchdog started inside the pod. Found `runpodctl` unconfigured there;
  made the RunPod API call with the pod's own injected key the primary stop
  path, and **tested it for real**: the pod stopped itself (EXITED, confirmed
  from outside).
- The step that installs FASHN VTON v1.5 (GitHub), Real-ESRGAN (PyPI) and
  model weights (Hugging Face) on the pod was **blocked by the agent's
  safety gate** pending the owner's explicit approval. Pod stopped while
  waiting so it doesn't bill.

### 12:44–13:00 — Built while waiting for the install approval (no GPU running)
- **4K photo** `pipeline/hires.py`: pad to model aspect → model at 576×864 →
  change mask (Lab difference, cleaned, hole-filled, feathered) → upscale
  (Real-ESRGAN when installed, else Lanczos) → paste only the garment into the
  original photo → optional upscale to a 3840 px long side.
  Test on a 12 MP photo: face and background **pixel-identical**, garment
  replaced; 1080p webcam input → 2880×3840. ~1 s on CPU.
- **FASHN wiring** `pipeline/photo_tryon.py`: warm model, quality presets
  (live 8 / fast 20 / balanced 30 / best 50 steps), runs locally on the GPU box
  or forwards to the GPU worker from the VPS; returns 4K JPEG.
- **GPU worker** `gpu/worker.py` (pod :8000): token auth, one job at a time,
  touches the watchdog keepalive.
- **Live engine v1** `pipeline/live_keyframe.py`: keyframe try-on in a
  background thread every ~1 s (sooner on big motion) + per-frame tracking.
  First version faded on flat-coloured fabric (25% of a moving shirt kept
  dressed); added a RANSAC affine fit for whole-garment motion with
  per-pixel flow only where reliable → **95% median, 82% worst**, ~23 ms/frame
  CPU at 640×480.
- **WebRTC** `webrtc/live_server.py` (pod :8900) + signed tickets
  (`server/engine_ticket.py`), `/live/token` mints them for
  `LIVE_BACKEND=local`. Session clock starts at the first frame, not at
  signalling. Tests: bad ticket 401; ~28 fps round trip; cap enforced; headless
  Chrome end-to-end (fake camera → engine → dressed video, server billed 8.0 s
  for an 8 s cap).
- Found: shoppers' browsers can't reach a pod over UDP; production live needs
  a TURN relay (coturn on the VPS). Made ICE servers configurable.
- `scripts/gpu_setup.sh` = the exact install awaiting approval;
  `scripts/gpu_run.sh` starts watchdog + worker + engine.
