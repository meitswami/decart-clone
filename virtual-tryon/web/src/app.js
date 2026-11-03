// Demo client for the white-label try-on server (virtual-tryon/server).
//   Photo: one camera frame + garment -> POST /photo/tryon -> image
//   Live:  POST /live/token -> scoped short-lived key -> realtime model over WebRTC
// Shoppers only see the brand from /config; no provider names appear in the UI.
//
// Built into ../app.js by ../build.mjs (bundles the SDK, copies its worker).

import { createDecartClient, models } from "@decartai/sdk";

const STORE_ID = new URLSearchParams(location.search).get("store") || "demo";
const MAX_IMAGE_SIDE = 1200;
const $ = (s) => document.querySelector(s);

let cfg = { brand: "Try it on", live_ready: true, max_session_seconds: 20, access_code_required: false };
let previewStream = null; // camera for photo mode / idle preview
let live = null; // { session, stream, sessionId, startedAt, timer }
let starting = false;

const setStatus = (text, tone = "") => {
  $("#status").textContent = text;
  $("#status").dataset.tone = tone;
};

// ---------- camera preview ----------

async function openPreview() {
  if (previewStream) return previewStream;
  previewStream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: "user" }, audio: false });
  $("#cam").srcObject = previewStream;
  return previewStream;
}

function closePreview() {
  previewStream?.getTracks().forEach((t) => t.stop());
  previewStream = null;
}

function snap() {
  const v = $("#cam");
  const c = document.createElement("canvas");
  c.width = v.videoWidth;
  c.height = v.videoHeight;
  c.getContext("2d").drawImage(v, 0, 0);
  return new Promise((r) => c.toBlob(r, "image/png"));
}

async function toJpeg(blob, maxSide = MAX_IMAGE_SIDE) {
  const bmp = await createImageBitmap(blob);
  const scale = Math.min(1, maxSide / Math.max(bmp.width, bmp.height));
  const w = Math.round(bmp.width * scale);
  const h = Math.round(bmp.height * scale);
  const canvas = new OffscreenCanvas(w, h);
  const ctx = canvas.getContext("2d");
  ctx.fillStyle = "#fff"; // transparent PNGs would turn black as JPEG
  ctx.fillRect(0, 0, w, h);
  ctx.drawImage(bmp, 0, 0, w, h);
  bmp.close();
  return canvas.convertToBlob({ type: "image/jpeg", quality: 0.92 });
}

// ---------- photo ----------

$("#photoBtn").onclick = async () => {
  const file = $("#garment").files[0];
  if (!file) return setStatus("Pick a garment first.", "error");
  try {
    await openPreview();
  } catch (e) {
    return setStatus(`Camera blocked: ${e.message}`, "error");
  }
  setStatus("Working…");
  const fd = new FormData();
  fd.append("person", await snap(), "person.png");
  fd.append("garment", file);
  fd.append("category", $("#category").value);
  const res = await fetch("/photo/tryon", { method: "POST", body: fd });
  if (!res.ok) return setStatus(`Something went wrong (HTTP ${res.status}).`, "error");
  $("#result").src = URL.createObjectURL(await res.blob());
  $("#result").hidden = false;
  setStatus("");
};

// ---------- live ----------

async function mintSession() {
  const fd = new FormData();
  fd.append("store_id", STORE_ID);
  const consented = $("#consent").checked;
  fd.append("consented", String(consented));
  fd.append("is_adult", String(consented)); // the box also confirms 18+
  const headers = {};
  if (cfg.access_code_required) headers["x-access-code"] = $("#code").value.trim();
  const res = await fetch("/live/token", { method: "POST", body: fd, headers });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.message || data.detail || `Couldn't start (HTTP ${res.status}).`);
  return data;
}

function endSessionOnServer(sessionId) {
  if (!sessionId) return;
  const fd = new FormData();
  fd.append("session_id", sessionId);
  // sendBeacon survives tab close; fetch is the fallback.
  if (!navigator.sendBeacon?.("/live/end", fd)) fetch("/live/end", { method: "POST", body: fd, keepalive: true }).catch(() => {});
}

async function applyGarment() {
  const file = $("#garment").files[0];
  if (!live?.session || !file) return;
  setStatus("Putting it on…");
  try {
    const prompt = $("#desc").value.trim();
    await live.session.set({ image: await toJpeg(file), ...(prompt ? { prompt } : {}), enhance: true });
    setStatus("Live. Step back so your upper body is in frame.", "ok");
  } catch (err) {
    setStatus(`Couldn't apply the garment: ${err.message || err}`, "error");
  }
}

const END_REASONS = {
  insufficient_credits: "Live try-on is unavailable right now.",
  session_limit: "Live try-on is busy right now. Try again in a minute.",
  moderation_violation: "The session was ended by the content filter.",
  policy_violation: "The session was refused.",
};

async function startLive() {
  if (starting || live) return;
  if (!$("#garment").files[0]) return setStatus("Pick a garment first.", "error");
  if (cfg.access_code_required && !$("#code").value.trim()) return setStatus("Enter the access code first.", "error");
  starting = true;
  $("#liveBtn").disabled = true;
  let data = null;
  let stream = null;
  try {
    setStatus("Starting…");
    data = await mintSession();
    if (data.backend !== "decart") throw new Error("This live engine isn't connected to the page yet.");
    const model = models.realtime(data.model);
    closePreview();
    stream = await navigator.mediaDevices.getUserMedia({
      audio: false,
      video: { facingMode: "user", width: { ideal: model.width }, height: { ideal: model.height }, frameRate: model.fps },
    });
    $("#cam").srcObject = stream;

    // The scoped key is the only credential this page ever sees.
    const client = createDecartClient({ apiKey: data.client_token });
    setStatus("Connecting…");
    const session = await client.realtime.connect(stream, {
      model,
      mirror: true,
      onRemoteStream: (remote) => {
        $("#out").srcObject = remote;
        $("#out").hidden = false;
      },
      onCapacityWait: ({ retryAfterMs }) => setStatus(`Busy. Retrying in ${Math.round(retryAfterMs / 1000)}s…`),
    });
    live = { session, stream, sessionId: data.session_id, timer: null };
    session.on("connectionChange", (s) => {
      if (s === "reconnecting") setStatus("Connection dropped. Reconnecting…");
      else if (s === "disconnected" && live) stopLive("Disconnected.");
    });
    session.on("sessionEnded", ({ reason }) => stopLive(END_REASONS[reason] || "The session ended.", "error"));
    session.on("error", (err) => console.warn("[live]", err));

    let left = data.max_seconds;
    $("#timer").textContent = `${left}s left`;
    live.timer = setInterval(() => {
      left -= 1;
      $("#timer").textContent = left > 0 ? `${left}s left` : "";
      if (left <= 0) stopLive("Time's up. Press Live try-on to go again.");
    }, 1000);
    await applyGarment();
  } catch (err) {
    stream?.getTracks().forEach((t) => t.stop());
    if (data?.session_id && !live) endSessionOnServer(data.session_id);
    const msg = err?.name === "NotAllowedError" ? "Camera access was blocked. Allow it in the address bar and try again." : err?.message || String(err);
    stopLive(msg, "error");
  } finally {
    starting = false;
  }
}

function stopLive(message = "Stopped.", tone = "") {
  if (live) {
    const { session, stream, sessionId, timer } = live;
    live = null;
    clearInterval(timer);
    try { session.disconnect(); } catch {}
    stream.getTracks().forEach((t) => t.stop());
    endSessionOnServer(sessionId);
  }
  $("#timer").textContent = "";
  $("#out").srcObject = null;
  $("#out").hidden = true;
  $("#cam").srcObject = null;
  $("#liveBtn").disabled = false;
  $("#stopBtn").disabled = true;
  setStatus(message, tone);
}

$("#liveBtn").onclick = async () => {
  await startLive();
  if (live) $("#stopBtn").disabled = false;
};
$("#stopBtn").onclick = () => stopLive();
$("#garment").onchange = () => { if (live) applyGarment(); };

// Live is billed per second: end it when the tab goes away.
document.addEventListener("visibilitychange", () => { if (document.hidden && live) stopLive("Stopped because the tab was hidden."); });
window.addEventListener("pagehide", () => { if (live) stopLive(); });

// ---------- boot ----------

(async () => {
  try {
    cfg = { ...cfg, ...(await (await fetch("/config")).json()) };
  } catch {}
  document.title = cfg.brand;
  $("#brand").textContent = cfg.brand;
  $("#liveBtn").textContent = `Live try-on (${cfg.max_session_seconds}s)`;
  $("#code-row").hidden = !cfg.access_code_required;
  $("#consent-row").hidden = !cfg.recording_enabled;
  if (!cfg.live_ready) {
    $("#liveBtn").disabled = true;
    setStatus("Live try-on isn't available right now. Photo try-on still works.");
  }
  openPreview().catch((e) => setStatus(`Camera blocked: ${e.message}`, "error"));
})();
