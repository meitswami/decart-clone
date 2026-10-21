// Server for the live virtual try-on app.
//
//   GET  /               the page (files in ./public)
//   GET  /api/config     what the page needs to know (access code required? session cap?)
//   POST /api/token      mints a short-lived Decart key; the real key never leaves this server
//   GET  /api/image      fetches a garment image from another site (browsers block that cross-site)
//   GET  /healthz        simple health check
//
// Decart bills per second, so on a public site the token endpoint is the thing to protect.
// Every key we hand out is locked to the try-on models, to PUBLIC_ORIGIN, and to a capped
// session length, and minting is rate limited per visitor and per day.

import http from "node:http";
import https from "node:https";
import dns from "node:dns";
import net from "node:net";
import crypto from "node:crypto";
import { readFileSync } from "node:fs";
import { readFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { parseEnv } from "node:util";
import { createDecartClient } from "@decartai/sdk";

const ROOT = path.dirname(fileURLToPath(import.meta.url));
const PUBLIC_DIR = path.join(ROOT, "public");
const ENV_FILE = path.join(ROOT, ".env");

// ---------- configuration ----------

let fileEnv = {};
let fileEnvReadAt = 0;
function env(name, fallback = "") {
  // Re-read .env at most every 5 s, so adding the key doesn't need a restart.
  if (Date.now() - fileEnvReadAt > 5000) {
    try {
      fileEnv = parseEnv(readFileSync(ENV_FILE, "utf8"));
    } catch {
      fileEnv = {};
    }
    fileEnvReadAt = Date.now();
  }
  const value = process.env[name] || fileEnv[name];
  return value === undefined || value === "" ? fallback : String(value).trim();
}
const envInt = (name, fallback) => {
  const n = Number.parseInt(env(name), 10);
  return Number.isFinite(n) && n > 0 ? n : fallback;
};

const PORT = envInt("PORT", 3000);
const HOST = env("HOST", "127.0.0.1");
const TRUST_PROXY = env("TRUST_PROXY") === "1";
const MODELS = ["lucy-vton-latest", "lucy-vton-3.6", "lucy-vton-3.5"];
const TOKEN_TTL_SECONDS = 120; // only gates starting a session, not how long it runs
const MAX_IMAGE_BYTES = 12 * 1024 * 1024;

const config = () => ({
  apiKey: env("DECART_API_KEY"),
  publicOrigin: env("PUBLIC_ORIGIN").replace(/\/+$/, "").toLowerCase(),
  accessCode: env("ACCESS_CODE"),
  maxSessionSeconds: Math.max(10, envInt("MAX_SESSION_SECONDS", 120)),
  tokensPerIpPerHour: envInt("TOKENS_PER_IP_PER_HOUR", 10),
  dailyTokenLimit: envInt("DAILY_TOKEN_LIMIT", 300),
  imagesPerIpPerHour: envInt("IMAGES_PER_IP_PER_HOUR", 60),
});

// ---------- small helpers ----------

const SECURITY_HEADERS = {
  "X-Content-Type-Options": "nosniff",
  "Referrer-Policy": "strict-origin-when-cross-origin",
  "X-Frame-Options": "DENY",
  "Permissions-Policy": "camera=(self), microphone=(), geolocation=()",
};

function send(res, status, headers, body) {
  res.writeHead(status, { ...SECURITY_HEADERS, ...headers });
  res.end(body);
}

function sendJson(res, status, body) {
  send(res, status, { "Content-Type": "application/json", "Cache-Control": "no-store" }, JSON.stringify(body));
}

function clientIp(req) {
  if (TRUST_PROXY) {
    const real = req.headers["x-real-ip"];
    if (typeof real === "string" && real) return real.trim();
    const fwd = req.headers["x-forwarded-for"];
    if (typeof fwd === "string" && fwd) return fwd.split(",")[0].trim();
  }
  return req.socket.remoteAddress || "unknown";
}

function sameSecret(given, expected) {
  const a = crypto.createHash("sha256").update(String(given || "")).digest();
  const b = crypto.createHash("sha256").update(String(expected)).digest();
  return crypto.timingSafeEqual(a, b);
}

// Sliding one-hour window per visitor, plus a daily total that resets at midnight UTC.
function makeLimiter() {
  const hits = new Map();
  return (key, perHour) => {
    const now = Date.now();
    const recent = (hits.get(key) || []).filter((t) => now - t < 3600_000);
    if (recent.length >= perHour) {
      hits.set(key, recent);
      return Math.ceil((3600_000 - (now - recent[0])) / 60_000); // minutes until a slot frees
    }
    recent.push(now);
    hits.set(key, recent);
    if (hits.size > 10_000) {
      for (const [k, v] of hits) if (!v.some((t) => now - t < 3600_000)) hits.delete(k);
    }
    return 0;
  };
}
const tokenLimiter = makeLimiter();
const imageLimiter = makeLimiter();
const daily = { day: "", count: 0 };

// ---------- /api/token ----------

let decart = null;
let decartKey = "";
function decartClient(apiKey) {
  if (!decart || decartKey !== apiKey) {
    decart = createDecartClient({ apiKey });
    decartKey = apiKey;
  }
  return decart;
}

async function handleToken(req, res) {
  const cfg = config();
  if (!cfg.apiKey) {
    sendJson(res, 503, { error: "not_configured", message: "This server has no Decart API key yet." });
    return;
  }
  if (cfg.publicOrigin && (req.headers.origin || "").toLowerCase() !== cfg.publicOrigin) {
    sendJson(res, 403, { error: "bad_origin", message: "Requests must come from the try-on page." });
    return;
  }
  if (cfg.accessCode && !sameSecret(req.headers["x-access-code"], cfg.accessCode)) {
    sendJson(res, 401, { error: "access_code", message: "That access code isn't right." });
    return;
  }

  const ip = clientIp(req);
  const waitMinutes = tokenLimiter(ip, cfg.tokensPerIpPerHour);
  if (waitMinutes) {
    sendJson(res, 429, { error: "rate_limited", message: `You've started a lot of sessions. Try again in about ${waitMinutes} min.` });
    return;
  }
  const today = new Date().toISOString().slice(0, 10);
  if (daily.day !== today) Object.assign(daily, { day: today, count: 0 });
  if (daily.count >= cfg.dailyTokenLimit) {
    sendJson(res, 429, { error: "daily_limit", message: "Today's try-on limit has been reached. Please come back tomorrow." });
    return;
  }

  try {
    const token = await decartClient(cfg.apiKey).tokens.create({
      expiresIn: TOKEN_TTL_SECONDS,
      allowedModels: MODELS,
      ...(cfg.publicOrigin ? { allowedOrigins: [cfg.publicOrigin] } : {}),
      constraints: { realtime: { maxSessionDuration: cfg.maxSessionSeconds } },
    });
    daily.count += 1;
    console.log(`[token] issued #${daily.count} today`);
    sendJson(res, 200, { apiKey: token.apiKey, expiresAt: token.expiresAt });
  } catch (err) {
    console.error("[token] Decart refused to mint a token:", err?.message || err);
    sendJson(res, 502, { error: "token_failed", message: "Couldn't start a session with Decart right now." });
  }
}

// ---------- /api/image (safe fetch of other sites' images) ----------

// Only fetch from the public internet: never this machine, the private network or cloud metadata.
const blocked = new net.BlockList();
for (const [addr, bits] of [
  ["0.0.0.0", 8], ["10.0.0.0", 8], ["100.64.0.0", 10], ["127.0.0.0", 8], ["169.254.0.0", 16],
  ["172.16.0.0", 12], ["192.0.0.0", 24], ["192.168.0.0", 16], ["198.18.0.0", 15], ["224.0.0.0", 3],
]) blocked.addSubnet(addr, bits, "ipv4");
for (const [addr, bits] of [["::", 128], ["::1", 128], ["fc00::", 7], ["fe80::", 10], ["ff00::", 8], ["64:ff9b::", 96]]) {
  blocked.addSubnet(addr, bits, "ipv6");
}
function isBlockedIp(ip) {
  const mapped = ip.match(/^::ffff:(\d+\.\d+\.\d+\.\d+)$/i);
  if (mapped) return blocked.check(mapped[1], "ipv4");
  return blocked.check(ip, net.isIPv6(ip) ? "ipv6" : "ipv4");
}

// Checked at connect time, so a hostname can't switch to a private address after validation.
function safeLookup(hostname, options, callback) {
  dns.lookup(hostname, { ...options, all: true }, (err, addresses) => {
    if (err) return callback(err);
    if (!addresses.length || addresses.some((a) => isBlockedIp(a.address))) {
      return callback(new Error("That address isn't on the public internet."));
    }
    if (options.all) return callback(null, addresses);
    callback(null, addresses[0].address, addresses[0].family);
  });
}

function fetchImage(rawUrl, redirectsLeft = 3) {
  return new Promise((resolve, reject) => {
    let url;
    try {
      url = new URL(rawUrl);
    } catch {
      return reject(new Error("That isn't a valid address."));
    }
    if (url.protocol !== "http:" && url.protocol !== "https:") return reject(new Error("Only http and https addresses work."));
    if (url.port && url.port !== "80" && url.port !== "443") return reject(new Error("Only standard web ports are allowed."));
    if (url.username || url.password) return reject(new Error("Addresses with passwords aren't allowed."));
    const host = url.hostname.replace(/^\[|\]$/g, "");
    if (net.isIP(host) && isBlockedIp(host)) return reject(new Error("That address isn't on the public internet."));

    const lib = url.protocol === "https:" ? https : http;
    const req = lib.get(url, {
      lookup: safeLookup,
      timeout: 15_000,
      headers: { Accept: "image/avif,image/webp,image/png,image/jpeg,image/*;q=0.8", "User-Agent": "Mozilla/5.0 (live-virtual-try-on image fetch)" },
    }, (upstream) => {
      const { statusCode = 0, headers } = upstream;
      if (statusCode >= 300 && statusCode < 400 && headers.location) {
        upstream.resume();
        if (redirectsLeft <= 0) return reject(new Error("Too many redirects."));
        return resolve(fetchImage(new URL(headers.location, url).href, redirectsLeft - 1));
      }
      const type = String(headers["content-type"] || "").split(";")[0].trim().toLowerCase();
      if (statusCode !== 200 || !type.startsWith("image/") || type === "image/svg+xml") {
        upstream.resume();
        return reject(new Error(`That address didn't return a photo (status ${statusCode}, ${type || "no type"}).`));
      }
      const chunks = [];
      let size = 0;
      upstream.on("data", (chunk) => {
        size += chunk.length;
        if (size > MAX_IMAGE_BYTES) {
          upstream.destroy();
          reject(new Error("Image is larger than 12 MB."));
        } else {
          chunks.push(chunk);
        }
      });
      upstream.on("end", () => resolve({ type, body: Buffer.concat(chunks) }));
      upstream.on("error", reject);
    });
    req.on("timeout", () => req.destroy(new Error("The image took too long to load.")));
    req.on("error", reject);
  });
}

async function handleImage(req, reqUrl, res) {
  const waitMinutes = imageLimiter(clientIp(req), config().imagesPerIpPerHour);
  if (waitMinutes) {
    sendJson(res, 429, { error: "rate_limited", message: `Too many image loads. Try again in about ${waitMinutes} min.` });
    return;
  }
  try {
    const { type, body } = await fetchImage(reqUrl.searchParams.get("url") || "");
    send(res, 200, { "Content-Type": type, "Cache-Control": "no-store", "Content-Security-Policy": "default-src 'none'" }, body);
  } catch (err) {
    sendJson(res, 422, { error: "image_failed", message: String(err?.message || err).slice(0, 200) });
  }
}

// ---------- static files ----------

const CONTENT_TYPES = {
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".json": "application/json; charset=utf-8",
  ".jpg": "image/jpeg",
  ".png": "image/png",
  ".svg": "image/svg+xml",
  ".ico": "image/x-icon",
  ".txt": "text/plain; charset=utf-8",
};

async function handleStatic(reqUrl, res) {
  let rel;
  try {
    rel = reqUrl.pathname === "/" ? "index.html" : decodeURIComponent(reqUrl.pathname).replace(/^\/+/, "");
  } catch {
    return send(res, 400, {}, "Bad request");
  }
  const file = path.resolve(PUBLIC_DIR, rel);
  if (!file.startsWith(PUBLIC_DIR + path.sep)) return send(res, 403, {}, "Forbidden");
  try {
    const body = await readFile(file);
    const ext = path.extname(file);
    send(res, 200, {
      "Content-Type": CONTENT_TYPES[ext] || "application/octet-stream",
      "Cache-Control": ext === ".html" ? "no-cache" : "public, max-age=3600",
    }, body);
  } catch {
    send(res, 404, { "Content-Type": "text/plain" }, "Not found");
  }
}

// ---------- routing ----------

const server = http.createServer(async (req, res) => {
  let reqUrl;
  try {
    reqUrl = new URL(req.url, "http://localhost");
  } catch {
    return send(res, 400, {}, "Bad request");
  }
  try {
    const route = `${req.method} ${reqUrl.pathname}`;
    if (route === "POST /api/token") return await handleToken(req, res);
    if (route === "GET /api/image") return await handleImage(req, reqUrl, res);
    if (route === "GET /api/config") {
      const cfg = config();
      return sendJson(res, 200, { accessCodeRequired: Boolean(cfg.accessCode), maxSessionSeconds: cfg.maxSessionSeconds, ready: Boolean(cfg.apiKey) });
    }
    if (route === "GET /healthz") return sendJson(res, 200, { ok: true });
    if (req.method === "GET" || req.method === "HEAD") return await handleStatic(reqUrl, res);
    send(res, 405, {}, "Method not allowed");
  } catch (err) {
    console.error("[server]", err);
    if (!res.headersSent) sendJson(res, 500, { error: "server_error" });
  }
});

server.listen(PORT, HOST, () => {
  const cfg = config();
  console.log(`Live try-on running at http://${HOST === "0.0.0.0" ? "localhost" : HOST}:${PORT}`);
  if (!cfg.apiKey) console.log("Note: no DECART_API_KEY yet. The page loads, but Start needs the key.");
  if (!cfg.publicOrigin) console.log("Note: PUBLIC_ORIGIN not set; keys aren't locked to a site (fine for local use).");
});
