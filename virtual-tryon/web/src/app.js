// Test app for the white-label try-on server (virtual-tryon/server).
//   Photo: person photo (camera / upload / webcam) + garment -> /photo/tryon -> up to 4K
//   Live:  /live/token -> our GPU engine over a WebSocket (default), WebRTC, or the hosted backend
// No provider names appear in the UI. Built into ../app.js by ../build.mjs.

import { createDecartClient, models } from "@decartai/sdk";

const STORE_ID = new URLSearchParams(location.search).get("store") || "demo";
const $ = (s) => document.querySelector(s);
const isMobile = matchMedia("(pointer:coarse)").matches;

let cfg = { brand: "Try it on", live_ready: true, max_session_seconds: 20, recording_enabled: true };
const state = {
  garment: null, // { blob, name }
  person: null, // { blob, source }
  gender: null, // { gender, confidence }
  resultUrl: null,
  personUrl: null,
  showingBefore: false,
};
let webcamStream = null;
let webcamFacing = "user";
let liveFacing = "user";
let live = null; // active live session
let starting = false;

const status = (el, text, tone = "", busy = false) => {
  if (busy) el.innerHTML = `<span class="spin"></span>`;
  else el.textContent = "";
  el.append(text);
  el.dataset.tone = tone;
};

// ---------- consent (remembered on this device) ----------
try { $("#consent").checked = localStorage.getItem("vto_consent") === "1"; } catch {}
$("#consent").onchange = () => { try { localStorage.setItem("vto_consent", $("#consent").checked ? "1" : "0"); } catch {} };
const consented = () => $("#consent").checked;

// ---------- image helpers ----------
async function toJpeg(blob, maxSide = 1600, quality = 0.92) {
  const bmp = await createImageBitmap(blob, { imageOrientation: "from-image" });
  const s = Math.min(1, maxSide / Math.max(bmp.width, bmp.height));
  const c = document.createElement("canvas");
  c.width = Math.round(bmp.width * s);
  c.height = Math.round(bmp.height * s);
  const x = c.getContext("2d");
  x.fillStyle = "#fff";
  x.fillRect(0, 0, c.width, c.height);
  x.drawImage(bmp, 0, 0, c.width, c.height);
  bmp.close?.();
  return new Promise((r) => c.toBlob(r, "image/jpeg", quality));
}
const blobToB64 = (blob) => new Promise((ok, bad) => {
  const r = new FileReader();
  r.onload = () => ok(String(r.result).split(",")[1]);
  r.onerror = bad;
  r.readAsDataURL(blob);
});
function guessCategory(name) {
  const n = name.toLowerCase();
  if (/(dress|gown|saree|sari|jumpsuit|lehenga|robe|one.?piece)/.test(n)) return "one-pieces";
  if (/(jean|trouser|pant|short|skirt|legging|chino)/.test(n)) return "bottoms";
  return "tops";
}

// ---------- garment ----------
function setGarment(blob, name, category) {
  state.garment = { blob, name };
  $("#garmentPreview").src = URL.createObjectURL(blob);
  $("#garmentName").textContent = name;
  $("#category").value = category || guessCategory(name);
  $("#garmentPreviewBox").hidden = false;
}
$("#garmentFile").onchange = (e) => {
  const f = e.target.files[0];
  if (!f) return;
  document.querySelectorAll("#samples button").forEach((b) => b.setAttribute("aria-pressed", "false"));
  $("#gptype").value = "auto";
  setGarment(f, f.name.replace(/\.[^.]+$/, "") || "Your garment");
};
async function loadSamples() {
  if (!cfg.samples) return;
  try {
    const list = await (await fetch("/garments/catalog.json")).json();
    // Only garments the try-on dresses (no hats/accessories).
    for (const item of list.filter((i) => ["upper_body", "lower_body", "dresses"].includes(i.type))) {
      const b = document.createElement("button");
      b.type = "button";
      b.title = item.name;
      b.dataset.for = item.for || "all";
      b.setAttribute("aria-pressed", "false");
      const img = document.createElement("img");
      img.alt = item.name;
      img.loading = "lazy";
      img.src = item.thumb || item.image;
      b.append(img);
      b.onclick = async () => {
        document.querySelectorAll("#samples button").forEach((x) => x.setAttribute("aria-pressed", String(x === b)));
        const blob = await (await fetch(item.image)).blob();
        $("#gptype").value = "auto";
        setGarment(blob, item.name, item.type === "lower_body" ? "bottoms" : item.type === "dresses" ? "one-pieces" : "tops");
      };
      $("#samples").append(b);
    }
    if (new Set(list.map((i) => i.for).filter(Boolean)).size > 1) $("#sampleFilters").hidden = false;
  } catch {}
}
// Men / Women filter. Follows the AI estimate until the shopper picks one.
let filterPicked = false;
function setSampleFilter(f) {
  document.querySelectorAll("#sampleFilters .pill").forEach((p) => p.setAttribute("aria-pressed", String(p.dataset.f === f)));
  document.querySelectorAll("#samples button").forEach((b) => {
    b.hidden = !(f === "all" || !b.dataset.for || b.dataset.for === "all" || b.dataset.for === f);
  });
}
document.querySelectorAll("#sampleFilters .pill").forEach((p) => {
  p.onclick = () => { filterPicked = true; setSampleFilter(p.dataset.f); };
});

// ---------- person ----------
async function setPerson(blob, source) {
  state.person = { blob, source };
  state.gender = null;
  if (state.personUrl) URL.revokeObjectURL(state.personUrl);
  state.personUrl = URL.createObjectURL(blob);
  $("#personPreview").src = state.personUrl;
  $("#personPreviewBox").hidden = false;
  $("#genderChoice").value = "";
  if (cfg.analysis_ready) analyzePerson(blob);
}
async function analyzePerson(blob) {
  $("#genderBox").hidden = false;
  $("#genderText").textContent = "checking…";
  try {
    const fd = new FormData();
    fd.append("image", await toJpeg(blob, 768, 0.9), "person.jpg");
    const r = await (await fetch("/analyze/person", { method: "POST", body: fd })).json();
    if (r.asleep) { $("#genderText").textContent = "shown with your result"; return; }
    if (!r.available) { $("#genderBox").hidden = true; return; }
    state.gender = r;
    if (!r.person_found) $("#genderText").textContent = "no person found. Try another photo";
    else if (r.gender === "unsure") $("#genderText").textContent = "not sure";
    else {
      $("#genderText").textContent = `${r.gender === "female" ? "Female" : "Male"} · ${Math.round(r.confidence * 100)}%`;
      if (!filterPicked && r.confidence >= 0.8) setSampleFilter(r.gender === "female" ? "women" : "men");
    }
  } catch {
    $("#genderText").textContent = "unavailable";
  }
}
$("#personCam").onchange = (e) => e.target.files[0] && setPerson(e.target.files[0], "camera");
$("#personFile").onchange = (e) => e.target.files[0] && setPerson(e.target.files[0], "upload");

async function openWebcam() {
  closeWebcam();
  webcamStream = await navigator.mediaDevices.getUserMedia({
    audio: false, video: { facingMode: webcamFacing, width: { ideal: 1920 }, height: { ideal: 1080 } },
  });
  $("#webcam").srcObject = webcamStream;
  $("#webcam").classList.toggle("mirror", webcamFacing === "user");
  $("#webcamBox").hidden = false;
}
function closeWebcam() {
  webcamStream?.getTracks().forEach((t) => t.stop());
  webcamStream = null;
  $("#webcamBox").hidden = true;
}
$("#webcamBtn").onclick = () => openWebcam().catch((e) => status($("#photoStatus"), `Camera blocked: ${e.message}`, "error"));
$("#webcamFlip").onclick = () => { webcamFacing = webcamFacing === "user" ? "environment" : "user"; openWebcam().catch(() => {}); };
$("#webcamClose").onclick = closeWebcam;
$("#snapBtn").onclick = () => {
  const v = $("#webcam");
  const c = document.createElement("canvas");
  c.width = v.videoWidth;
  c.height = v.videoHeight;
  c.getContext("2d").drawImage(v, 0, 0);
  c.toBlob((b) => { setPerson(b, "webcam"); closeWebcam(); }, "image/jpeg", 0.95);
};

// ---------- GPU wake-up (servers sleep when idle) ----------
const ENGINE = new URLSearchParams(location.search).get("engine") || ""; // ?engine=gpu → our GPU live engine
let wakeCancelled = false;
$("#wakeCancel").onclick = () => { wakeCancelled = true; };
const fmtSecs = (s) => (s >= 90 ? `about ${Math.round(s / 60)} min` : `about ${Math.max(5, Math.round(s / 5) * 5)} s`);

async function ensureGpu() {
  if (cfg.gpu_mode !== "runpod") return;
  let st = await (await fetch("/gpu/status", { cache: "no-store" })).json().catch(() => ({}));
  if (st.state === "ready") return;
  st = await (await fetch("/gpu/wake", { method: "POST" })).json().catch(() => ({ state: "unavailable" }));
  if (st.state === "ready") return;
  wakeCancelled = false;
  $("#wake").hidden = false;
  const t0 = performance.now();
  try {
    while (true) {
      if (wakeCancelled) throw new Error("Cancelled. Press the button again when you're ready.");
      if (st.state === "unavailable") throw new Error(st.message || "The try-on servers are busy. Please try again shortly.");
      if (st.state === "ready") break;
      const eta = st.eta_s || 120;
      const el = Math.max(st.elapsed_s || 0, (performance.now() - t0) / 1000);
      $("#wakeBar").style.width = `${Math.min(95, (el / eta) * 100).toFixed(0)}%`;
      const left = eta - el;
      const tl = left > 3 ? `${fmtSecs(left)} left` : el > eta + 30 ? "Taking a little longer than usual…" : "Almost there…";
      $("#wakeEta").textContent = st.note ? `${st.note} ${tl}` : tl;
      let passed = true;
      for (const li of document.querySelectorAll("#wakeSteps li")) {
        const on = li.dataset.s === st.stage;
        if (on) passed = false;
        li.className = on ? "on" : passed ? "done" : "";
      }
      await new Promise((r) => setTimeout(r, 2000));
      st = await (await fetch("/gpu/status", { cache: "no-store" })).json().catch(() => st);
      if (st.state === "asleep") st = await (await fetch("/gpu/wake", { method: "POST" })).json().catch(() => st);
    }
    $("#wakeBar").style.width = "100%";
    $("#wakeEta").textContent = "Ready!";
    document.querySelectorAll("#wakeSteps li").forEach((li) => (li.className = "done"));
    await new Promise((r) => setTimeout(r, 500));
  } finally {
    $("#wake").hidden = true;
  }
}

// ---------- photo try-on ----------
$("#tryBtn").onclick = async () => {
  const st = $("#photoStatus");
  if (!state.garment) return status(st, "Pick a garment first (step 1).", "error");
  if (!state.person) return status(st, "Add a photo of yourself first.", "error");
  $("#tryBtn").disabled = true;
  try {
    await ensureGpu();
    status(st, cfg.photo_model_ready ? "Dressing you… (about 5–20 s)" : "Running (stand-in model: real model not installed yet)…", "", true);
    const fd = new FormData();
    fd.append("person", await toJpeg(state.person.blob, 4096, 0.95), "person.jpg");
    fd.append("garment", await toJpeg(state.garment.blob, 1600, 0.95), "garment.jpg");
    fd.append("category", $("#category").value);
    fd.append("garment_photo_type", $("#gptype").value);
    fd.append("quality", $("#quality").value);
    fd.append("consented", String(consented()));
    fd.append("is_adult", String(consented()));
    fd.append("gender_estimate", state.gender?.gender || "");
    fd.append("gender_choice", $("#genderChoice").value);
    fd.append("source", state.person.source);
    let res = await fetch("/photo/tryon", { method: "POST", body: fd });
    if (res.status === 503 && (await res.clone().json().catch(() => ({}))).error === "gpu_asleep") {
      await ensureGpu();
      res = await fetch("/photo/tryon", { method: "POST", body: fd });
    }
    if (!res.ok) {
      const j = await res.json().catch(() => ({}));
      throw new Error(j.message || `Something went wrong (HTTP ${res.status}).`);
    }
    const blob = await res.blob();
    if (state.resultUrl) URL.revokeObjectURL(state.resultUrl);
    state.resultUrl = URL.createObjectURL(blob);
    state.showingBefore = false;
    $("#compareBtn").textContent = "Before / after";
    $("#resultImg").src = state.resultUrl;
    $("#resultBox").hidden = false;
    $("#resultActions").hidden = false;
    $("#saveImg").href = state.resultUrl;
    $("#saveImg").download = `try-on-${Date.now()}.jpg`;
    const img = await createImageBitmap(blob);
    const saved = res.headers.get("X-Trial-Id") ? " · saved for training" : "";
    $("#resultInfo").textContent = `${img.width}×${img.height} · ${res.headers.get("X-Tryon-Seconds") || "?"} s${saved}`;
    img.close?.();
    status(st, "Done.", "ok");
    if (!state.gender && cfg.analysis_ready) analyzePerson(state.person.blob); // GPU is awake now
    $("#resultBox").scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (e) {
    status(st, e.message || String(e), "error");
  } finally {
    $("#tryBtn").disabled = false;
  }
};
$("#compareBtn").onclick = () => {
  state.showingBefore = !state.showingBefore;
  $("#resultImg").src = state.showingBefore ? state.personUrl : state.resultUrl;
  $("#compareBtn").textContent = state.showingBefore ? "Show result" : "Before / after";
};

// ---------- tabs ----------
function showTab(which) {
  const photo = which === "photo";
  $("#tabPhoto").setAttribute("aria-selected", String(photo));
  $("#tabLive").setAttribute("aria-selected", String(!photo));
  $("#panePhoto").hidden = !photo;
  $("#paneLive").hidden = photo;
  if (photo && live) stopLive("Stopped.");
  if (!photo) closeWebcam();
}
$("#tabPhoto").onclick = () => showTab("photo");
$("#tabLive").onclick = () => showTab("live");

// ---------- live: shared ----------
const lstat = (t, tone = "", busy = false) => status($("#liveStatus"), t, tone, busy);

async function mintSession() {
  const fd = new FormData();
  fd.append("store_id", STORE_ID);
  fd.append("consented", String(consented()));
  fd.append("is_adult", String(consented()));
  if (ENGINE) fd.append("engine", ENGINE);
  const headers = {};
  if (cfg.access_code_required) headers["x-access-code"] = $("#code").value.trim();
  let res = await fetch("/live/token", { method: "POST", body: fd, headers });
  if (res.status === 503 && (await res.clone().json().catch(() => ({}))).error === "gpu_asleep") {
    await ensureGpu();
    res = await fetch("/live/token", { method: "POST", body: fd, headers });
  }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.message || `Couldn't start (HTTP ${res.status}).`);
  return data;
}
function endSessionOnServer(sessionId) {
  if (!sessionId) return;
  const fd = new FormData();
  fd.append("session_id", sessionId);
  if (!navigator.sendBeacon?.("/live/end", fd)) fetch("/live/end", { method: "POST", body: fd, keepalive: true }).catch(() => {});
}
async function openCamera(w = 640, h = 480) {
  return navigator.mediaDevices.getUserMedia({
    audio: false, video: { facingMode: liveFacing, width: { ideal: w }, height: { ideal: h }, frameRate: { ideal: 30 } },
  });
}

// Every live session is recorded in the browser so it can be downloaded;
// it is uploaded for training only with consent (and only from our engine).
function startRecorder(stream) {
  const types = ["video/webm;codecs=vp9", "video/webm;codecs=vp8", "video/webm", "video/mp4"];
  const mimeType = types.find((t) => window.MediaRecorder?.isTypeSupported?.(t));
  if (!mimeType) return null;
  const rec = new MediaRecorder(stream, { mimeType, videoBitsPerSecond: 2_500_000 });
  const chunks = [];
  rec.ondataavailable = (e) => e.data.size && chunks.push(e.data);
  rec.start(1000);
  $("#recDot").hidden = false;
  return { rec, chunks, mimeType, t0: Date.now() };
}
function finishRecorder(r, trialId, upload) {
  if (!r) return;
  r.rec.onstop = () => {
    const blob = new Blob(r.chunks, { type: r.mimeType.split(";")[0] });
    if (!blob.size) return;
    const ext = r.mimeType.includes("mp4") ? "mp4" : "webm";
    const url = URL.createObjectURL(blob);
    const a = $("#saveVideo");
    a.href = url;
    a.download = `try-on-live-${Date.now()}.${ext}`;
    $("#recPreview").src = url;
    const secs = Math.round((Date.now() - r.t0) / 1000);
    $("#videoInfo").textContent = `${secs}s · ${(blob.size / 1048576).toFixed(1)} MB · ${ext.toUpperCase()}`;
    $("#videoActions").hidden = false;
    if (upload && trialId) {
      const fd = new FormData();
      fd.append("trial_id", trialId);
      fd.append("video", blob, `video.${ext}`);
      fetch("/captures/video", { method: "POST", body: fd }).catch(() => {});
    }
  };
  try { r.rec.stop(); } catch {}
}

function finishCameraRecorder(r, trialId) {
  r.rec.onstop = () => {
    const blob = new Blob(r.chunks, { type: r.mimeType.split(";")[0] });
    if (!blob.size || !trialId) return;
    const fd = new FormData();
    fd.append("trial_id", trialId);
    fd.append("kind", "camera");
    fd.append("video", blob, r.mimeType.includes("mp4") ? "camera.mp4" : "camera.webm");
    fetch("/captures/video", { method: "POST", body: fd }).catch(() => {});
  };
  try { r.rec.stop(); } catch {}
}

// ---------- live: our engine over WebSocket (works on mobile data) ----------
async function connectWs(data) {
  const w = isMobile ? 480 : 640;
  const h = isMobile ? 360 : 480;
  const stream = await openCamera(w, h);
  const src = $("#liveSrc");
  src.srcObject = stream;
  await src.play().catch(() => {});
  const out = $("#liveOut");
  out.hidden = false;
  $("#liveOutVideo").hidden = true;
  out.classList.toggle("mirror", liveFacing === "user");
  const ctx = out.getContext("2d");
  const cap = document.createElement("canvas");
  const cctx = cap.getContext("2d");

  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  const sameOrigin = !data.engine_url || data.engine_url.startsWith("http://127.") || data.engine_url.startsWith("http://localhost");
  const base = sameOrigin ? `${proto}//${location.host}` : data.engine_url.replace(/^http/, "ws");
  const ws = new WebSocket(`${base}/live/ws?ticket=${encodeURIComponent(data.ticket)}`);
  ws.binaryType = "blob";
  const s = { kind: "ws", ws, stream, sessionId: data.session_id, trialId: data.trial_id, timer: null, inflight: 0,
              got: 0, t0: 0, lat: [], sendTimes: [], recorder: null, upload: !!data.record };
  live = s;

  const sendFrame = () => {
    if (live !== s || ws.readyState !== 1 || s.inflight >= 4 || !src.videoWidth) return;
    cap.width = w;
    cap.height = Math.round((src.videoHeight / src.videoWidth) * w);
    cctx.drawImage(src, 0, 0, cap.width, cap.height);
    s.inflight++;
    s.sendTimes.push(performance.now());
    cap.toBlob((b) => { if (b && ws.readyState === 1) ws.send(b); else s.inflight--; }, "image/jpeg", 0.72);
  };

  await new Promise((ok, bad) => {
    ws.onopen = async () => ws.send(JSON.stringify({
      type: "start", category: $("#category").value,
      gender: $("#genderChoice").value || state.gender?.gender || null,
      garment_b64: await blobToB64(await toJpeg(state.garment.blob, 1024, 0.92)),
    }));
    ws.onerror = () => bad(new Error("Couldn't connect to live try-on."));
    ws.onmessage = async (ev) => {
      if (typeof ev.data === "string") {
        const m = JSON.parse(ev.data);
        if (m.type === "ready") {
          $("#engineTag").textContent = m.engine === "fashn" ? "live · our GPU" : "live · stand-in model";
          s.t0 = performance.now();
          s.recorder = startRecorder(out.captureStream(15));
          ok();
          sendFrame(); sendFrame();
        } else if (m.type === "stats") {
          s.stats = m;
        } else if (m.type === "end") {
          const msg = m.reason === "time" ? "Time's up. Press Start live to go again."
            : m.reason === "busy" ? "Live try-on is busy. Try again in a minute." : "The session ended.";
          if (live === s) stopLive(msg, m.reason === "time" ? "" : "error");
          bad(new Error(msg));
        }
        return;
      }
      s.inflight = Math.max(0, s.inflight - 1);
      s.got++;
      const t = s.sendTimes.shift();
      if (t) s.lat.push(performance.now() - t);
      const bmp = await createImageBitmap(ev.data);
      if (out.width !== bmp.width) { out.width = bmp.width; out.height = bmp.height; }
      ctx.drawImage(bmp, 0, 0);
      bmp.close?.();
      sendFrame();
    };
    ws.onclose = () => { if (live === s) stopLive("Disconnected."); bad(new Error("Disconnected.")); };
  });

  s.statsTimer = setInterval(() => {
    const secs = (performance.now() - s.t0) / 1000;
    const recent = s.lat.slice(-20);
    const avg = recent.length ? Math.round(recent.reduce((a, b) => a + b, 0) / recent.length) : 0;
    const k = s.stats ? ` · keyframes ${s.stats.keyframes}${s.stats.key_ms ? ` (${(s.stats.key_ms / 1000).toFixed(1)} s each)` : ""}${s.stats.frame_ms ? ` · ${Math.round(s.stats.frame_ms)} ms/frame` : ""}` : "";
    $("#liveStats").textContent = `${(s.got / Math.max(secs, 0.1)).toFixed(1)} fps · delay ${avg} ms${k}`;
  }, 1000);
}

// ---------- live: our engine over WebRTC (needs a TURN relay on real networks) ----------
async function connectRtc(data) {
  const stream = await openCamera();
  const pc = new RTCPeerConnection({ iceServers: data.ice_servers || [] });
  stream.getVideoTracks().forEach((t) => pc.addTrack(t, stream));
  const vid = $("#liveOutVideo");
  pc.ontrack = (e) => { vid.srcObject = e.streams[0] || new MediaStream([e.track]); vid.hidden = false; $("#liveOut").hidden = true; };
  await pc.setLocalDescription(await pc.createOffer());
  await new Promise((done) => {
    if (pc.iceGatheringState === "complete") return done();
    const t = setTimeout(done, 3000);
    pc.onicegatheringstatechange = () => pc.iceGatheringState === "complete" && (clearTimeout(t), done());
  });
  const res = await fetch(`${data.engine_url}/offer`, {
    method: "POST", headers: { "content-type": "application/json" },
    body: JSON.stringify({ sdp: pc.localDescription.sdp, type: pc.localDescription.type, ticket: data.ticket,
      garment_b64: await blobToB64(await toJpeg(state.garment.blob, 1024)), category: $("#category").value }),
  });
  if (!res.ok) { pc.close(); throw new Error("Couldn't connect to live try-on."); }
  await pc.setRemoteDescription(await res.json());
  live = { kind: "rtc", pc, stream, sessionId: data.session_id, timer: null, recorder: null };
  vid.onplaying = () => { if (live?.pc === pc && !live.recorder) live.recorder = startRecorder(vid.srcObject); };
}

// ---------- live: hosted backend ----------
async function connectHosted(data) {
  const model = models.realtime(data.model);
  const stream = await navigator.mediaDevices.getUserMedia({
    audio: false, video: { facingMode: liveFacing, width: { ideal: model.width }, height: { ideal: model.height }, frameRate: model.fps },
  });
  const client = createDecartClient({ apiKey: data.client_token }); // scoped short-lived key only
  const vid = $("#liveOutVideo");
  const session = await client.realtime.connect(stream, {
    model, mirror: liveFacing === "user",
    onRemoteStream: (remote) => { vid.srcObject = remote; vid.hidden = false; $("#liveOut").hidden = true; },
    onCapacityWait: ({ retryAfterMs }) => lstat(`Busy. Retrying in ${Math.round(retryAfterMs / 1000)}s…`),
  });
  live = { kind: "hosted", session, stream, sessionId: data.session_id, trialId: data.trial_id, timer: null,
           recorder: null, upload: false, camRecorder: data.record ? startRecorder(stream) : null };
  session.on("connectionChange", (st) => { if (st === "disconnected" && live) stopLive("Disconnected."); });
  session.on("sessionEnded", () => stopLive("The session ended.", "error"));
  await session.set({ image: await toJpeg(state.garment.blob, 1200), enhance: true });
  vid.onplaying = () => { if (live?.session === session && !live.recorder) live.recorder = startRecorder(vid.srcObject); };
}

async function startLive() {
  if (starting || live) return;
  if (!state.garment) return lstat("Pick a garment first (step 1).", "error");
  if (cfg.access_code_required && !$("#code").value.trim()) return lstat("Enter the access code first.", "error");
  starting = true;
  $("#liveStart").disabled = true;
  $("#videoActions").hidden = true;
  let data = null;
  try {
    lstat("Starting…", "", true);
    data = await mintSession();
    lstat("Connecting…", "", true);
    if (data.backend === "hosted") await connectHosted(data);
    else if (data.transport === "webrtc") await connectRtc(data);
    else await connectWs(data);
    if (!live) return;
    $("#liveStop").disabled = false;
    $("#liveSnap").disabled = false;
    lstat(cfg.photo_model_ready ? "Live. Hold still for a second while it dresses you." : "Live (stand-in model until the real one is installed).", "ok");
    let left = data.max_seconds;
    $("#timer").textContent = `${left}s left`;
    live.timer = setInterval(() => {
      left -= 1;
      $("#timer").textContent = left > 0 ? `${left}s left` : "";
      if (left <= 0) stopLive("Time's up. Press Start live to go again.");
    }, 1000);
  } catch (err) {
    if (data?.session_id && !live) endSessionOnServer(data.session_id);
    const msg = err?.name === "NotAllowedError" ? "Camera access was blocked. Allow it and try again." : err?.message || String(err);
    if (live) stopLive(msg, "error");
    else { lstat(msg, "error"); $("#liveStart").disabled = false; }
  } finally {
    starting = false;
  }
}

function stopLive(message = "Stopped.", tone = "") {
  const s = live;
  live = null;
  if (s) {
    clearInterval(s.timer);
    clearInterval(s.statsTimer);
    finishRecorder(s.recorder, s.trialId, s.upload && s.kind === "ws");
    if (s.recorder) message = `${message} Your video is ready below.`;
    if (s.camRecorder) finishCameraRecorder(s.camRecorder, s.trialId);
    try { s.ws?.close(); } catch {}
    try { s.pc?.close(); } catch {}
    try { s.session?.disconnect(); } catch {}
    s.stream?.getTracks().forEach((t) => t.stop());
    endSessionOnServer(s.sessionId);
  }
  $("#liveSrc").srcObject = null;
  $("#timer").textContent = "";
  $("#recDot").hidden = true;
  $("#liveSnap").disabled = true;
  $("#liveStart").disabled = false;
  $("#liveStop").disabled = true;
  lstat(message, tone);
}
$("#liveStart").onclick = startLive;
// Save the current live picture as a photo.
$("#liveSnap").onclick = () => {
  const vid = $("#liveOutVideo");
  const src = !vid.hidden && vid.videoWidth ? vid : $("#liveOut");
  const c = document.createElement("canvas");
  c.width = src.videoWidth || src.width;
  c.height = src.videoHeight || src.height;
  c.getContext("2d").drawImage(src, 0, 0, c.width, c.height);
  c.toBlob((b) => {
    if (!b) return;
    const a = document.createElement("a");
    a.href = URL.createObjectURL(b);
    a.download = `try-on-live-${Date.now()}.jpg`;
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 5000);
  }, "image/jpeg", 0.95);
};
$("#liveStop").onclick = () => stopLive();
$("#liveFlip").onclick = async () => {
  liveFacing = liveFacing === "user" ? "environment" : "user";
  if (live) { stopLive("Switching camera…"); await startLive(); }
  else lstat(`Will use the ${liveFacing === "user" ? "front" : "back"} camera.`);
};
document.addEventListener("visibilitychange", () => { if (document.hidden && live) stopLive("Stopped because the app was in the background."); });
window.addEventListener("pagehide", () => { if (live) stopLive(); });

// ---------- zoom viewer (garment, your photo, result) ----------
const zoom = { s: 1, fit: 1, x: 0, y: 0, pts: new Map(), pinch: null };
function zoomApply() {
  const img = $("#zoomImg");
  img.style.transform = `translate(-50%, -50%) translate(${zoom.x}px, ${zoom.y}px) scale(${zoom.s})`;
  $("#zoomPct").textContent = `${Math.round((zoom.s / zoom.fit) * 100)}%`;
}
function zoomTo(s, cx = 0, cy = 0) {
  const ns = Math.min(zoom.fit * 8, Math.max(zoom.fit * 0.5, s));
  zoom.x = cx - (cx - zoom.x) * (ns / zoom.s);
  zoom.y = cy - (cy - zoom.y) * (ns / zoom.s);
  zoom.s = ns;
  zoomApply();
}
function zoomFit() {
  const img = $("#zoomImg"), st = $("#zoomStage").getBoundingClientRect();
  if (!img.naturalWidth) return;
  zoom.fit = Math.min(st.width / img.naturalWidth, st.height / img.naturalHeight);
  zoom.s = zoom.fit; zoom.x = 0; zoom.y = 0;
  zoomApply();
}
function openZoom(src, title) {
  if (!src) return;
  const img = $("#zoomImg");
  $("#zoomTitle").textContent = title || "";
  img.alt = title || "Zoomed picture";
  $("#zoom").hidden = false;
  img.onload = zoomFit;
  img.src = src;
  if (img.complete) zoomFit();
  $("#zoomClose").focus();
}
const closeZoom = () => { $("#zoom").hidden = true; zoom.pts.clear(); zoom.pinch = null; };
const stageXY = (e) => {
  const r = $("#zoomStage").getBoundingClientRect();
  return [e.clientX - r.left - r.width / 2, e.clientY - r.top - r.height / 2];
};
$("#zoomIn").onclick = () => zoomTo(zoom.s * 1.5);
$("#zoomOut").onclick = () => zoomTo(zoom.s / 1.5);
$("#zoomFit").onclick = zoomFit;
$("#zoomClose").onclick = closeZoom;
$("#zoom").addEventListener("click", (e) => { if (e.target.id === "zoom") closeZoom(); });
document.addEventListener("keydown", (e) => {
  if ($("#zoom").hidden) return;
  if (e.key === "Escape") closeZoom();
  else if (e.key === "+" || e.key === "=") zoomTo(zoom.s * 1.5);
  else if (e.key === "-") zoomTo(zoom.s / 1.5);
});
window.addEventListener("resize", () => { if (!$("#zoom").hidden) zoomFit(); });
{
  const st = $("#zoomStage");
  st.addEventListener("wheel", (e) => { e.preventDefault(); const [x, y] = stageXY(e); zoomTo(zoom.s * (e.deltaY < 0 ? 1.2 : 1 / 1.2), x, y); }, { passive: false });
  st.addEventListener("dblclick", (e) => { const [x, y] = stageXY(e); zoom.s > zoom.fit * 1.5 ? zoomFit() : zoomTo(zoom.fit * 2.5, x, y); });
  st.addEventListener("pointerdown", (e) => { st.setPointerCapture(e.pointerId); zoom.pts.set(e.pointerId, [e.clientX, e.clientY]); st.classList.add("drag"); });
  st.addEventListener("pointermove", (e) => {
    const prev = zoom.pts.get(e.pointerId);
    if (!prev) return;
    zoom.pts.set(e.pointerId, [e.clientX, e.clientY]);
    if (zoom.pts.size === 2) {               // pinch
      const [a, b] = [...zoom.pts.values()];
      const d = Math.hypot(a[0] - b[0], a[1] - b[1]);
      const r = st.getBoundingClientRect();
      const mid = [(a[0] + b[0]) / 2 - r.left - r.width / 2, (a[1] + b[1]) / 2 - r.top - r.height / 2];
      if (zoom.pinch) zoomTo(zoom.s * (d / zoom.pinch), mid[0], mid[1]);
      zoom.pinch = d;
    } else if (zoom.pts.size === 1) {        // drag to look around
      zoom.x += e.clientX - prev[0];
      zoom.y += e.clientY - prev[1];
      zoomApply();
    }
  });
  const up = (e) => { zoom.pts.delete(e.pointerId); if (zoom.pts.size < 2) zoom.pinch = null; if (!zoom.pts.size) st.classList.remove("drag"); };
  st.addEventListener("pointerup", up);
  st.addEventListener("pointercancel", up);
}
const zoomGarment = () => openZoom($("#garmentPreview").src, $("#garmentName").textContent);
$("#garmentZoom").onclick = zoomGarment;
$("#garmentZoomLink").onclick = zoomGarment;
$("#personPreview").onclick = () => openZoom($("#personPreview").src, "Your photo");
$("#resultImg").onclick = () => openZoom($("#resultImg").src, "Your try-on");

// ---------- owner: GPU costs (only with ?admin=<gallery token>) ----------
let adminToken = "";
try {
  const q = new URLSearchParams(location.search);
  if (q.get("admin")) {
    sessionStorage.setItem("vtoAdmin", q.get("admin"));
    q.delete("admin");
    history.replaceState(null, "", location.pathname + (q.toString() ? `?${q}` : "") + location.hash);
  }
  adminToken = sessionStorage.getItem("vtoAdmin") || "";
} catch {}
const usd = (v) => (v == null ? "–" : v < 0.01 && v > 0 ? `$${v.toFixed(4)}` : `$${v.toFixed(2)}`);
const dur = (s) => (s >= 3600 ? `${(s / 3600).toFixed(1)} h` : s >= 60 ? `${Math.round(s / 60)} min` : `${Math.round(s)} s`);
const esc = (t) => String(t ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
async function loadCosts() {
  if (!adminToken) return;
  let d;
  try {
    const r = await fetch(`/admin/api/costs?token=${encodeURIComponent(adminToken)}`, { cache: "no-store" });
    if (!r.ok) { $("#costCard").hidden = true; return; }
    d = await r.json();
  } catch { return; }
  if (!d.enabled) return;
  $("#costCard").hidden = false;
  const a = d.all_time, avg = d.average;
  $("#costTiles").innerHTML = [
    [usd(d.balance), "RunPod balance left"],
    [usd(d.today.cost), `GPU spent today · ${d.today.sessions} session${d.today.sessions === 1 ? "" : "s"}`],
    [usd(a.cost), `GPU spent all time · ${a.sessions} sessions`],
    [d.spend_per_hr_now == null ? "–" : `${usd(d.spend_per_hr_now)}/h`, "Charging right now (GPU + storage)"],
    [`${usd(d.storage_per_month)}/mo`, "Model storage (always)"],
    [`${d.idle_minutes} min`, "GPU sleeps after this idle time"],
  ].map(([v, l]) => `<div class="costtile"><b>${esc(v)}</b><span>${esc(l)}</span></div>`).join("");
  const rows = [
    ["📸 Photo try-ons", a.photo.n, dur(a.photo.seconds), a.photo.cost, avg.photo == null ? "–" : `${usd(avg.photo)} each`],
    ["🎥 Live try-ons", a.live.n, dur(a.live.seconds), a.live.cost, avg.live == null ? "–" : `${usd(avg.live)} each`],
    ["🧠 AI fit estimates", a.fit.n, dur(a.fit.seconds), a.fit.cost, avg.fit == null ? "–" : `${usd(avg.fit)} each`],
    ["⏳ Waking the GPU", a.sessions, "", a.wake, avg.wake == null ? "–" : `${usd(avg.wake)} per wake-up`],
    ["💤 Idle before sleep", "", "", a.idle, `GPU waits ${d.idle_minutes} min after the last try-on`],
  ];
  $("#costSplit").innerHTML = `<tr><th>What</th><th class="num">Cost</th><th class="num">How many</th><th>Average</th><th class="num">GPU time</th></tr>` +
    rows.map((r) => `<tr><td>${esc(r[0])}</td><td class="num"><b>${usd(r[3])}</b></td><td class="num">${esc(r[1])}</td><td>${esc(r[4])}</td><td class="num">${esc(r[2])}</td></tr>`).join("") +
    `<tr><th>Total</th><th class="num">${usd(a.cost)}</th><th></th><th></th><th></th></tr>`;
  $("#costSessions").innerHTML = `<tr><th>Started</th><th class="num">Cost</th><th class="num">Photo</th><th class="num">Live</th><th class="num">Fit</th><th class="num">Awake</th><th>GPU</th><th class="num">Rate</th></tr>` +
    (d.sessions.length ? d.sessions.map((s) => `<tr><td>${esc(new Date(s.start * 1000).toLocaleString([], { dateStyle: "short", timeStyle: "short" }))}${s.running ? " · <b>awake</b>" : s.ok === false ? " · failed" : ""}</td>` +
      `<td class="num"><b>${usd(s.cost.total)}</b></td><td class="num">${s.counts.photo} · ${usd(s.cost.photo)}</td><td class="num">${s.counts.live} · ${usd(s.cost.live)}</td><td class="num">${s.counts.fit} · ${usd(s.cost.fit)}</td>` +
      `<td class="num">${dur(s.seconds.total)}</td><td>${esc(s.gpu || "–")}</td><td class="num">${s.rate_per_hr == null ? "–" : `${usd(s.rate_per_hr)}/h`}</td></tr>`).join("")
      : `<tr><td colspan="8">No GPU sessions recorded yet.</td></tr>`);
  $("#costNote").textContent = "GPU time is billed by RunPod per second while the GPU is awake: waking it up, running try-ons, and the idle minutes before it sleeps. Storage is billed all the time. Balance updates every minute.";
}
if (adminToken) { loadCosts(); setInterval(() => { if (!document.hidden) loadCosts(); }, 30000); }

// ---------- boot ----------
(async () => {
  try { cfg = { ...cfg, ...(await (await fetch("/config")).json()) }; } catch {}
  document.title = cfg.brand;
  $("#brand").textContent = cfg.brand;
  $("#engineTag").textContent = cfg.photo_model_ready ? "test build" : "test build · stand-in model";
  $("#codeRow").hidden = !cfg.access_code_required;
  if (!cfg.live_ready) $("#liveStart").disabled = true;
  if (isMobile) $("#webcamBtn").hidden = true; // phones: "Take photo" opens the real camera app (full resolution)
  const ctx = $("#liveOut").getContext("2d");
  ctx.fillStyle = "#111"; ctx.fillRect(0, 0, 640, 480);
  ctx.fillStyle = "#888"; ctx.font = "16px system-ui"; ctx.textAlign = "center";
  ctx.fillText("Press Start live", 320, 245);
  loadSamples();
})();
