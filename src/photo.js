// Free photo try-on: calls the public Leffa demo on Hugging Face (MIT-licensed model)
// straight from the visitor's browser, so each visitor uses their own free quota.

import { Client, handle_file } from "@gradio/client";

const SPACE = "franciszzj/Leffa";
const TIMEOUT_MS = 180_000;

// The demo's cross-site pre-check doesn't allow cookies, but the Gradio client sends them with
// every request (including its progress stream), so browsers block the calls. The demo doesn't
// need cookies, so requests to *.hf.space go out without them. Other sites are untouched.
const HF_SPACE_URL = /^https:\/\/[a-z0-9-]+\.hf\.space\//i;
const browserFetch = globalThis.fetch.bind(globalThis);
globalThis.fetch = (input, init) => {
  const url = typeof input === "string" ? input : input instanceof URL ? input.href : input?.url;
  return browserFetch(input, url && HF_SPACE_URL.test(url) ? { ...init, credentials: "omit" } : init);
};

let clientPromise = null;
function getClient() {
  if (!clientPromise) {
    clientPromise = Client.connect(SPACE, { events: ["data", "status"] }).catch((err) => {
      clientPromise = null; // let the next attempt reconnect
      throw err;
    });
  }
  return clientPromise;
}

function friendlyError(message) {
  const m = String(message || "");
  if (/quota|zerogpu|exceeded/i.test(m)) {
    // e.g. "You have exceeded your ZeroGPU quota (180s requested vs. 164s left). Try again in 23:49:21."
    const wait = m.match(/try again in (\d+):(\d+):(\d+)/i);
    let when = "later";
    if (wait) {
      const hours = Number(wait[1]);
      const minutes = Number(wait[2]);
      when = hours > 0 ? `in about ${hours + (minutes >= 30 ? 1 : 0)} hour${hours + (minutes >= 30 ? 1 : 0) === 1 ? "" : "s"}` : `in about ${Math.max(1, minutes)} min`;
    }
    return `You've used today's free photo for this connection. Try again ${when}, or use Live mode.`;
  }
  if (/busy|queue is full|too many/i.test(m)) {
    return "The free photo service is busy. Please try again in a minute.";
  }
  if (/person|pose|keypoint|human/i.test(m)) {
    return "Couldn't find a person in the photo. Face the camera with your upper body in view.";
  }
  return m ? `The free photo service had a problem: ${m.slice(0, 160)}` : "The free photo service had a problem. Please try again.";
}

/**
 * Dress the person in `person` with the garment in `garment`.
 * garmentType: "upper_body" | "lower_body" | "dresses"
 * onStatus({ position }) reports the visitor's place in the shared queue (null once generating).
 * Returns the URL of the generated image (hosted temporarily by Hugging Face).
 */
export async function photoTryOn({ person, garment, garmentType = "upper_body", onStatus }) {
  let app;
  try {
    app = await getClient();
  } catch {
    throw new Error("Couldn't reach the free photo service. Please try again in a moment.");
  }

  // handle_file() only works with File objects in browsers (a plain Blob hits a Node-only check).
  const asFile = (blob, name) => (blob instanceof File ? blob : new File([blob], name, { type: blob.type || "image/jpeg" }));
  const job = app.submit("/leffa_predict_vt", {
    src_image_path: handle_file(asFile(person, "person.jpg")),
    ref_image_path: handle_file(asFile(garment, "garment.jpg")),
    ref_acceleration: false,
    step: 30,
    scale: 2.5,
    seed: 42,
    // VITON-HD was trained on tops; DressCode covers bottoms and dresses.
    vt_model_type: garmentType === "upper_body" ? "viton_hd" : "dress_code",
    vt_garment_type: garmentType,
    vt_repaint: false,
  });

  const timer = setTimeout(() => job.cancel?.(), TIMEOUT_MS);
  try {
    for await (const msg of job) {
      if (msg.type === "status") {
        if (msg.stage === "error") throw new Error(friendlyError(msg.message));
        if (msg.stage === "pending") onStatus?.({ position: msg.queue && Number.isFinite(msg.position) && msg.position > 0 ? msg.position : null });
      } else if (msg.type === "data") {
        const url = msg.data?.[0]?.url;
        if (!url) throw new Error("The free photo service returned no image. Please try again.");
        return url;
      }
    }
    throw new Error("The free photo service took too long. Please try again.");
  } finally {
    clearTimeout(timer);
  }
}
