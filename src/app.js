// Virtual try-on with two modes:
//  - Live video: camera -> Decart lucy-vton realtime model (paid, via short-lived keys from our server)
//  - Photo (free): one photo + garment -> Leffa (MIT) on its free Hugging Face demo

import { createDecartClient, models } from "@decartai/sdk";
import { photoTryOn } from "./photo.js";

const MODEL = "lucy-vton-latest";
const MAX_IMAGE_SIDE = 1200;

const $ = (id) => document.getElementById(id);
const els = {
  stage: $("stage"),
  camera: $("camera"),
  output: $("output"),
  placeholder: $("placeholder"),
  placeholderTitle: $("placeholder-title"),
  photoView: $("photo-view"),
  countdownView: $("countdown"),
  working: $("working"),
  workingText: $("working-text"),
  modeLive: $("mode-live"),
  modePhoto: $("mode-photo"),
  catalog: $("catalog"),
  drop: $("drop"),
  dropImg: $("drop-img"),
  fileInput: $("file-input"),
  urlForm: $("url-form"),
  urlInput: $("url-input"),
  desc: $("desc"),
  gtype: $("gtype"),
  codeRow: $("code-row"),
  code: $("code"),
  liveActions: $("live-actions"),
  start: $("start"),
  stop: $("stop"),
  photoActions: $("photo-actions"),
  snap: $("snap"),
  uploadMe: $("upload-me"),
  meInput: $("me-input"),
  download: $("download"),
  status: $("status"),
  timer: $("timer"),
  note: $("note"),
};

let serverConfig = { accessCodeRequired: false, maxSessionSeconds: 120, ready: true };
let mode = "live"; // "live" | "photo"
let garment = null; // { blob, prompt, enhance, label, type, custom }
let cameraStream = null;
let session = null;
let starting = false;
let countdown = null;
let personBlob = null; // photo mode: the visitor's photo
let resultUrl = null;
let photoBusy = false;
let photoQueued = false;

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

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

function suggestPhotoMode(message) {
  setStatus(`${message} You can still use the free Photo mode.`, "error");
  els.modePhoto.classList.remove("nudge");
  void els.modePhoto.offsetWidth; // restart the animation
  els.modePhoto.classList.add("nudge");
}

// ---------- server ----------

const client = createDecartClient({
  // Called before every connect / reconnect, so each dial gets a fresh key.
  apiKeyProvider: async () => {
    const headers = {};
    if (serverConfig.accessCodeRequired) headers["x-access-code"] = els.code.value.trim();
    const res = await fetch("/api/token", { method: "POST", headers });
    const body = await res.json().catch(() => ({}));
    if (!res.ok || !body.apiKey) {
      const err = new Error(body.message || `Couldn't start a session (HTTP ${res.status}).`);
      err.liveUnavailable = body.error !== "access_code";
      throw err;
    }
    return body.apiKey;
  },
});

async function loadConfig() {
  try {
    const res = await fetch("/api/config");
    if (res.ok) serverConfig = { ...serverConfig, ...(await res.json()) };
  } catch {}
  els.codeRow.hidden = !serverConfig.accessCodeRequired || mode !== "live";
  try {
    const saved = sessionStorage.getItem("tryon_code");
    if (saved) els.code.value = saved;
  } catch {}
  if (!serverConfig.ready) {
    setMode("photo");
    setStatus("Live mode isn't available right now, so you're in the free Photo mode.");
  }
}

// ---------- images ----------

async function toJpeg(blob, maxSide = MAX_IMAGE_SIDE) {
  const bitmap = await createImageBitmap(blob);
  const scale = Math.min(1, maxSide / Math.max(bitmap.width, bitmap.height));
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

// Grab a 3:4 portrait from the middle of the camera frame, mirrored like the preview.
async function captureFrame(video) {
  const vw = video.videoWidth;
  const vh = video.videoHeight;
  if (!vw || !vh) throw new Error("The camera didn't deliver a picture. Try again.");
  let cw = vw, ch = vh, sx = 0, sy = 0;
  if (vw / vh > 3 / 4) { cw = Math.round((vh * 3) / 4); sx = Math.round((vw - cw) / 2); }
  else { ch = Math.round((vw * 4) / 3); sy = Math.round((vh - ch) / 2); }
  const scale = Math.min(1, 1024 / ch);
  const w = Math.round(cw * scale);
  const h = Math.round(ch * scale);
  const canvas = new OffscreenCanvas(w, h);
  const ctx = canvas.getContext("2d");
  ctx.translate(w, 0);
  ctx.scale(-1, 1);
  ctx.drawImage(video, sx, sy, cw, ch, 0, 0, w, h);
  return canvas.convertToBlob({ type: "image/jpeg", quality: 0.92 });
}

// ---------- garments ----------

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

function garmentChanged() {
  if (mode === "live") {
    if (session) applyGarment();
    else setStatus(`${garment.custom ? "Your garment is" : `${garment.label} is`} ready. Press Start to try it on.`);
  } else if (personBlob) {
    runPhoto();
  } else {
    setStatus(`${garment.custom ? "Your garment is" : `${garment.label} is`} ready. Now take or upload a photo of yourself.`);
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
    garment = { blob, prompt: item.prompt, enhance: false, label: item.name, type: item.type, custom: false };
  } catch {
    setStatus("Couldn't load that garment. Try another.", "error");
    return;
  }
  garmentChanged();
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
    jpeg = await toJpeg(blob);
  } catch {
    setStatus("Couldn't read that image. Try a different one.", "error");
    return;
  }
  if (els.dropImg.src) URL.revokeObjectURL(els.dropImg.src);
  els.dropImg.src = URL.createObjectURL(jpeg);
  els.drop.classList.add("has-image");
  garment = { blob: jpeg, prompt: customPrompt(), enhance: true, label: "your garment", type: els.gtype.value, custom: true };
  markSelected("custom");
  garmentChanged();
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
    btn.dataset.type = item.type || "upper_body";
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
  updateCatalogForMode();
}

// The free photo model handles tops, bottoms and dresses, not hats.
function updateCatalogForMode() {
  for (const btn of els.catalog.querySelectorAll("button")) {
    const liveOnly = btn.dataset.type === "headwear";
    btn.disabled = mode === "photo" && liveOnly;
    btn.title = btn.disabled ? `${btn.querySelector("span").textContent} (Live mode only)` : btn.querySelector("span").textContent;
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
  if (garment?.custom) {
    garment.prompt = customPrompt();
    if (session) applyGarment();
  }
});
els.gtype.addEventListener("change", () => {
  if (garment?.custom) {
    garment.type = els.gtype.value;
    if (mode === "photo" && personBlob) runPhoto();
  }
});

// ---------- modes ----------

function setMode(next) {
  if (next === mode) return;
  if (session || starting) stop("Live session stopped.");
  mode = next;
  const photo = mode === "photo";
  els.modeLive.setAttribute("aria-pressed", String(!photo));
  els.modePhoto.setAttribute("aria-pressed", String(photo));
  els.modePhoto.classList.remove("nudge");
  els.liveActions.hidden = photo;
  els.photoActions.hidden = !photo;
  els.desc.hidden = photo;
  els.gtype.hidden = !photo;
  els.codeRow.hidden = photo || !serverConfig.accessCodeRequired;
  els.stage.querySelector(".tag").textContent = photo ? "Photo" : "Live";
  els.placeholderTitle.textContent = photo ? "Pick a garment, then take a photo" : "Pick a garment and press Start";
  els.note.textContent = photo
    ? "Free photo mode uses the public Leffa demo on Hugging Face: about one photo per day per internet connection, ready in 20–40 seconds. Your photo is sent there to create the result; this site doesn't store it."
    : "Your camera video is streamed to Decart's AI to create the try-on. This site doesn't store it.";
  els.photoView.hidden = !photo || !els.photoView.getAttribute("src");
  els.timer.textContent = "";
  updateCatalogForMode();

  if (photo && garment?.type === "headwear") {
    garment = null;
    markSelected(null);
    setStatus("Hats and caps work in Live mode only. Pick a top, or upload your own garment.");
  } else if (photo) {
    setStatus(personBlob ? "Pick a garment to try on your photo." : "Pick a garment, then take or upload a photo of yourself.");
  } else {
    setStatus(garment ? "Press Start to try it on live." : "Pick a garment, then press Start.");
  }
}

els.modeLive.addEventListener("click", () => setMode("live"));
els.modePhoto.addEventListener("click", () => setMode("photo"));

// ---------- photo mode ----------

function setPhotoButtons(enabled) {
  els.snap.disabled = !enabled;
  els.uploadMe.disabled = !enabled;
}

function showPersonPhoto() {
  if (els.photoView.dataset.objectUrl) URL.revokeObjectURL(els.photoView.dataset.objectUrl);
  const url = URL.createObjectURL(personBlob);
  els.photoView.dataset.objectUrl = url;
  els.photoView.src = url;
  els.photoView.hidden = false;
  els.placeholder.hidden = true;
  els.download.hidden = true;
  resultUrl = null;
}

async function takePhoto() {
  if (photoBusy) return;
  photoBusy = true;
  setPhotoButtons(false);
  let stream = null;
  try {
    setStatus("Asking for camera access…");
    stream = await navigator.mediaDevices.getUserMedia({
      audio: false,
      video: { facingMode: "user", width: { ideal: 1280 }, height: { ideal: 720 } },
    });
    els.photoView.hidden = true;
    els.placeholder.hidden = true;
    els.camera.srcObject = stream;
    await els.camera.play().catch(() => {});
    setStatus("Step back so your upper body is in frame, facing the camera…");
    for (const n of [3, 2, 1]) {
      els.countdownView.textContent = String(n);
      els.countdownView.hidden = false;
      await sleep(1000);
    }
    els.countdownView.hidden = true;
    personBlob = await captureFrame(els.camera);
  } catch (err) {
    els.countdownView.hidden = true;
    const msg = err?.name === "NotAllowedError" ? "Camera access was blocked. Allow it in the address bar, or use a photo instead."
      : err?.name === "NotFoundError" ? "No camera found. Use a photo of yourself instead."
      : err?.message || String(err);
    setStatus(msg, "error");
    if (personBlob) showPersonPhoto(); else els.placeholder.hidden = false;
    return;
  } finally {
    stream?.getTracks().forEach((t) => t.stop());
    els.camera.srcObject = null;
    photoBusy = false;
    setPhotoButtons(true);
  }
  showPersonPhoto();
  if (garment) runPhoto();
  else setStatus("Got your photo. Now pick a garment.", "ok");
}

async function useMyPhoto(file) {
  if (!file || !file.type.startsWith("image/")) {
    setStatus("That isn't a photo. Try a JPG or PNG of yourself.", "error");
    return;
  }
  try {
    personBlob = await toJpeg(file, 1280);
  } catch {
    setStatus("Couldn't read that photo. Try another.", "error");
    return;
  }
  showPersonPhoto();
  if (garment) runPhoto();
  else setStatus("Got your photo. Now pick a garment.", "ok");
}

async function runPhoto() {
  if (!personBlob || !garment) return;
  if (garment.type === "headwear") {
    setStatus("Hats and caps work in Live mode only. Pick a top.", "error");
    return;
  }
  if (photoBusy) {
    photoQueued = true; // run again with the newest garment when this one finishes
    return;
  }
  photoBusy = true;
  photoQueued = false;
  setPhotoButtons(false);
  const label = garment.label;
  showPersonPhoto();
  els.working.hidden = false;
  els.workingText.textContent = "Creating your try-on… usually 20–40 seconds";
  setStatus(`Putting the ${label} on your photo…`);
  const started = Date.now();
  const tick = setInterval(() => { els.timer.textContent = `${Math.round((Date.now() - started) / 1000)}s`; }, 1000);

  try {
    const url = await photoTryOn({
      person: personBlob,
      garment: garment.blob,
      garmentType: garment.type,
      onStatus: ({ position }) => {
        els.workingText.textContent = position
          ? `Waiting in line for the free service (position ${position})…`
          : "Creating your try-on… usually 20–40 seconds";
      },
    });
    if (photoQueued) return; // a newer garment was picked; its run starts in finally
    resultUrl = url;
    els.photoView.src = url;
    els.download.hidden = false;
    setStatus(`Here you are in the ${label}. Pick another garment to compare.`, "ok");
  } catch (err) {
    setStatus(err.message || String(err), "error");
  } finally {
    clearInterval(tick);
    els.timer.textContent = "";
    els.working.hidden = true;
    photoBusy = false;
    setPhotoButtons(true);
    if (photoQueued) runPhoto();
  }
}

els.snap.addEventListener("click", takePhoto);
els.uploadMe.addEventListener("click", () => els.meInput.click());
els.meInput.addEventListener("change", () => {
  const file = els.meInput.files?.[0];
  if (file) useMyPhoto(file);
  els.meInput.value = "";
});
els.download.addEventListener("click", async (e) => {
  e.preventDefault();
  if (!resultUrl) return;
  try {
    const blob = await (await fetch(resultUrl)).blob();
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = "try-on.webp";
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 10_000);
  } catch {
    window.open(resultUrl, "_blank", "noopener");
  }
});

// ---------- live session ----------

const END_REASONS = {
  moderation_violation: "The content filter ended the session.",
  insufficient_credits: "The live service is out of credits right now.",
  session_limit: "Too many people are using live try-on right now.",
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
    session.on("sessionEnded", ({ reason }) => {
      const msg = END_REASONS[reason] || "The session ended.";
      stop(msg, "error");
      if (reason === "insufficient_credits" || reason === "session_limit") suggestPhotoMode(msg);
    });
    session.on("error", (err) => console.warn("[decart]", err));

    startCountdown();
    if (garment) await applyGarment();
    else setStatus("Live. Pick a garment to try it on.", "ok");
  } catch (err) {
    const msg = err?.name === "NotAllowedError" ? "Camera access was blocked. Allow it in the address bar and try again."
      : err?.name === "NotFoundError" ? "No camera found."
      : err?.message || String(err);
    stop(msg, "error");
    if (err?.liveUnavailable || /credit|limit|busy|capacity/i.test(msg)) suggestPhotoMode(msg);
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

// Live is billed per second, so end the session when the tab goes away.
document.addEventListener("visibilitychange", () => {
  if (document.hidden && session) stop("Stopped because the tab was hidden.");
});
window.addEventListener("pagehide", () => {
  if (session) stop();
});

setStatus("Pick a garment, then press Start.");
renderCatalog();
loadConfig();
