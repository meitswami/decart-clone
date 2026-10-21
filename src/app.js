// Live virtual try-on: camera -> Decart lucy-vton realtime model -> video of you wearing the garment.
// Flow: short-lived key from our server, WebRTC stream, garment image + prompt sent with set().

import { createDecartClient, models } from "@decartai/sdk";

const MODEL = "lucy-vton-latest";
const MAX_GARMENT_SIDE = 1200;

const $ = (id) => document.getElementById(id);
const els = {
  stage: $("stage"),
  camera: $("camera"),
  output: $("output"),
  placeholder: $("placeholder"),
  catalog: $("catalog"),
  drop: $("drop"),
  dropImg: $("drop-img"),
  fileInput: $("file-input"),
  urlForm: $("url-form"),
  urlInput: $("url-input"),
  desc: $("desc"),
  codeRow: $("code-row"),
  code: $("code"),
  start: $("start"),
  stop: $("stop"),
  status: $("status"),
  timer: $("timer"),
};

let serverConfig = { accessCodeRequired: false, maxSessionSeconds: 120, ready: true };
let garment = null; // { blob, prompt, enhance, label }
let cameraStream = null;
let session = null;
let starting = false;
let countdown = null;

// ---------- status helpers ----------

function setStatus(text, tone = "") {
  els.status.textContent = text;
  els.status.dataset.tone = tone;
}

function setRunning(running) {
  els.start.disabled = running;
  els.stop.disabled = !running;
}

function formatTime(s) {
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

function startCountdown() {
  let left = serverConfig.maxSessionSeconds;
  els.timer.textContent = `${formatTime(left)} left`;
  clearInterval(countdown);
  countdown = setInterval(() => {
    left -= 1;
    els.timer.textContent = left > 0 ? `${formatTime(left)} left` : "";
    if (left <= 0) stop("Time's up for this session. Press Start to go again.");
  }, 1000);
}

// ---------- server ----------

const client = createDecartClient({
  // Called before every connect / reconnect, so each dial gets a fresh key.
  apiKeyProvider: async () => {
    const headers = {};
    if (serverConfig.accessCodeRequired) headers["x-access-code"] = els.code.value.trim();
    const res = await fetch("/api/token", { method: "POST", headers });
    const body = await res.json().catch(() => ({}));
    if (!res.ok || !body.apiKey) throw new Error(body.message || `Couldn't start a session (HTTP ${res.status}).`);
    return body.apiKey;
  },
});

async function loadConfig() {
  try {
    const res = await fetch("/api/config");
    if (res.ok) serverConfig = { ...serverConfig, ...(await res.json()) };
  } catch {}
  els.codeRow.hidden = !serverConfig.accessCodeRequired;
  try {
    const saved = sessionStorage.getItem("tryon_code");
    if (saved) els.code.value = saved;
  } catch {}
  if (!serverConfig.ready) setStatus("This site isn't fully set up yet (no Decart key on the server).", "error");
}

// ---------- garments ----------

async function toGarmentJpeg(blob) {
  const bitmap = await createImageBitmap(blob);
  const scale = Math.min(1, MAX_GARMENT_SIDE / Math.max(bitmap.width, bitmap.height));
  const w = Math.round(bitmap.width * scale);
  const h = Math.round(bitmap.height * scale);
  const canvas = new OffscreenCanvas(w, h);
  const ctx = canvas.getContext("2d");
  ctx.fillStyle = "#fff"; // transparent PNGs would turn black as JPEG
  ctx.fillRect(0, 0, w, h);
  ctx.drawImage(bitmap, 0, 0, w, h);
  bitmap.close();
  return canvas.convertToBlob({ type: "image/jpeg", quality: 0.92 });
}

async function applyGarment() {
  if (!session || !garment) return;
  setStatus(`Putting on the ${garment.label}…`);
  try {
    await session.set({
      image: garment.blob,
      ...(garment.prompt ? { prompt: garment.prompt } : {}),
      enhance: garment.enhance,
    });
    setStatus(`Wearing the ${garment.label}. Step back so your upper body is in frame.`, "ok");
  } catch (err) {
    setStatus(`Couldn't apply the garment: ${err.message || err}`, "error");
  }
}

function markSelected(id) {
  for (const btn of els.catalog.querySelectorAll("button")) {
    btn.setAttribute("aria-pressed", String(btn.dataset.id === id));
  }
  els.drop.classList.toggle("selected", id === "custom");
}

async function chooseCatalog(item) {
  markSelected(item.id);
  try {
    const blob = await (await fetch(item.image)).blob();
    garment = { blob, prompt: item.prompt, enhance: false, label: item.name };
  } catch {
    setStatus("Couldn't load that garment. Try another.", "error");
    return;
  }
  if (session) applyGarment();
  else setStatus(`${item.name} selected. Press Start to try it on.`);
}

function customPrompt() {
  const d = els.desc.value.trim().replace(/[.\s]+$/, "");
  if (!d) return null;
  return /^(substitute|add|replace)\b/i.test(d) ? d : `Substitute the current top with ${/^(a|an|the)\s/i.test(d) ? d : `a ${d}`}`;
}

async function chooseCustom(blob) {
  if (!blob || !blob.type.startsWith("image/")) {
    setStatus("That isn't a photo. Try a JPG, PNG or WebP of the garment.", "error");
    return;
  }
  let jpeg;
  try {
    jpeg = await toGarmentJpeg(blob);
  } catch {
    setStatus("Couldn't read that image. Try a different one.", "error");
    return;
  }
  if (els.dropImg.src) URL.revokeObjectURL(els.dropImg.src);
  els.dropImg.src = URL.createObjectURL(jpeg);
  els.drop.classList.add("has-image");
  const prompt = customPrompt();
  garment = { blob: jpeg, prompt, enhance: true, label: "your garment" };
  markSelected("custom");
  if (session) applyGarment();
  else setStatus(prompt ? "Your garment is ready. Press Start." : "Your garment is ready. Tip: describe it below for a better fit. Then press Start.");
}

async function chooseCustomUrl(url) {
  setStatus("Loading image…");
  try {
    const res = await fetch(`/api/image?url=${encodeURIComponent(url)}`);
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      throw new Error(body.message || `HTTP ${res.status}`);
    }
    await chooseCustom(await res.blob());
  } catch (err) {
    setStatus(`Couldn't load that image: ${err.message || err}`, "error");
  }
}

async function renderCatalog() {
  let items = [];
  try {
    items = await (await fetch("/garments/catalog.json")).json();
  } catch {
    return;
  }
  for (const item of items) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.dataset.id = item.id;
    btn.setAttribute("aria-pressed", "false");
    btn.title = item.name;
    const img = document.createElement("img");
    img.src = item.image;
    img.alt = "";
    img.loading = "lazy";
    const label = document.createElement("span");
    label.textContent = item.name;
    btn.append(img, label);
    btn.addEventListener("click", () => chooseCatalog(item));
    els.catalog.append(btn);
  }
}

// Images dragged from another tab arrive as a URL (or HTML with an <img>), not a file.
function urlFromDrop(dt) {
  const uri = (dt.getData("text/uri-list") || "").split("\n").find((l) => l && !l.startsWith("#"));
  if (uri) return uri.trim();
  const html = dt.getData("text/html");
  const match = html && html.match(/<img[^>]+src=["']([^"']+)["']/i);
  if (match) return match[1].replace(/&amp;/g, "&");
  const text = (dt.getData("text/plain") || "").trim();
  return /^https?:\/\//i.test(text) ? text : null;
}

els.drop.addEventListener("click", () => els.fileInput.click());
els.drop.addEventListener("keydown", (e) => {
  if (e.key === "Enter" || e.key === " ") {
    e.preventDefault();
    els.fileInput.click();
  }
});
els.fileInput.addEventListener("change", () => {
  const file = els.fileInput.files?.[0];
  if (file) chooseCustom(file);
  els.fileInput.value = "";
});
for (const type of ["dragenter", "dragover"]) {
  els.drop.addEventListener(type, (e) => {
    e.preventDefault();
    els.drop.classList.add("dragging");
  });
}
els.drop.addEventListener("dragleave", () => els.drop.classList.remove("dragging"));
els.drop.addEventListener("drop", (e) => {
  e.preventDefault();
  els.drop.classList.remove("dragging");
  const file = e.dataTransfer.files?.[0];
  if (file) return chooseCustom(file);
  const url = urlFromDrop(e.dataTransfer);
  if (url) return chooseCustomUrl(url);
  setStatus("Couldn't find an image in what you dropped.", "error");
});
document.addEventListener("paste", (e) => {
  if (e.target instanceof HTMLInputElement) return;
  const item = [...(e.clipboardData?.items || [])].find((i) => i.type.startsWith("image/"));
  if (item) chooseCustom(item.getAsFile());
});
els.urlForm.addEventListener("submit", (e) => {
  e.preventDefault();
  const url = els.urlInput.value.trim();
  if (url) chooseCustomUrl(url);
});
els.desc.addEventListener("change", () => {
  if (garment && garment.label === "your garment") {
    garment.prompt = customPrompt();
    if (session) applyGarment();
  }
});

// ---------- session ----------

const END_REASONS = {
  moderation_violation: "The content filter ended the session.",
  insufficient_credits: "The try-on service is out of credits right now.",
  session_limit: "Too many people are trying on right now. Please try again in a minute.",
  policy_violation: "The session was refused.",
};

async function start() {
  if (starting || session) return;
  if (serverConfig.accessCodeRequired && !els.code.value.trim()) {
    setStatus("Enter the access code first.", "error");
    els.code.focus();
    return;
  }
  try {
    sessionStorage.setItem("tryon_code", els.code.value.trim());
  } catch {}
  starting = true;
  setRunning(true);
  const model = models.realtime(MODEL);

  try {
    setStatus("Asking for camera access…");
    cameraStream = await navigator.mediaDevices.getUserMedia({
      audio: false,
      video: {
        facingMode: "user",
        width: { ideal: model.width },
        height: { ideal: model.height },
        frameRate: model.fps,
      },
    });
    els.camera.srcObject = cameraStream;
    els.placeholder.hidden = true;

    setStatus("Connecting…");
    const s = await client.realtime.connect(cameraStream, {
      model,
      mirror: true, // act like a mirror
      onRemoteStream: (stream) => {
        els.output.srcObject = stream;
        els.stage.classList.add("streaming");
      },
      onCapacityWait: ({ retryAfterMs }) =>
        setStatus(`Servers are busy. Retrying in ${Math.round(retryAfterMs / 1000)}s…`),
    });
    if (!cameraStream) {
      // Stopped while connecting.
      s.disconnect();
      return;
    }
    session = s;
    session.on("connectionChange", (state) => {
      if (state === "reconnecting") setStatus("Connection dropped. Reconnecting…");
      else if (state === "disconnected" && session) stop("Disconnected.");
    });
    session.on("queuePosition", (q) => setStatus(`Waiting in line (position ${q?.position ?? "?"})…`));
    session.on("sessionEnded", ({ reason }) => stop(END_REASONS[reason] || "The session ended.", "error"));
    session.on("error", (err) => console.warn("[decart]", err));

    startCountdown();
    if (garment) await applyGarment();
    else setStatus("Live. Pick a garment to try it on.", "ok");
  } catch (err) {
    const msg = err?.name === "NotAllowedError" ? "Camera access was blocked. Allow it in the address bar and try again."
      : err?.name === "NotFoundError" ? "No camera found."
      : err?.message || String(err);
    stop(msg, "error");
  } finally {
    starting = false;
  }
}

function stop(message = "Stopped.", tone = "") {
  clearInterval(countdown);
  countdown = null;
  els.timer.textContent = "";
  if (session) {
    const s = session;
    session = null;
    try { s.disconnect(); } catch {}
  }
  if (cameraStream) {
    cameraStream.getTracks().forEach((t) => t.stop());
    cameraStream = null;
  }
  els.camera.srcObject = null;
  els.output.srcObject = null;
  els.placeholder.hidden = false;
  els.stage.classList.remove("streaming");
  setRunning(false);
  setStatus(message, tone);
}

els.start.addEventListener("click", start);
els.stop.addEventListener("click", () => stop());

// Realtime is billed per second, so end the session when the tab goes away.
document.addEventListener("visibilitychange", () => {
  if (document.hidden && session) stop("Stopped because the tab was hidden.");
});
window.addEventListener("pagehide", () => stop());

setStatus("Pick a garment, then press Start.");
loadConfig();
renderCatalog();
