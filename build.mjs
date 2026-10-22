// Bundles src/app.js into public/app.js for the browser.
// @gradio/client lazily imports a Node-only WebSocket wrapper when it runs outside a browser;
// browsers have WebSocket built in, so that wrapper is replaced with a tiny stand-in.

import { build } from "esbuild";
import { createHash } from "node:crypto";
import { copyFileSync, readFileSync, writeFileSync } from "node:fs";

const nodeOnlyGradioWrapper = {
  name: "gradio-node-wrapper",
  setup(b) {
    b.onResolve({ filter: /^\.\/wrapper-[\w-]+\.js$/ }, (args) =>
      args.importer.includes("@gradio") ? { path: "gradio-ws-wrapper", namespace: "stub" } : undefined,
    );
    b.onLoad({ filter: /.*/, namespace: "stub" }, () => ({
      contents: "export const WebSocket = globalThis.WebSocket;",
      loader: "js",
    }));
  },
};

await build({
  entryPoints: ["src/app.js"],
  bundle: true,
  minify: true,
  format: "esm",
  target: "es2022",
  platform: "browser",
  outfile: "public/app.js",
  // Only reached on Node code paths that never run in the browser.
  external: ["path", "fs", "url", "stream", "zlib", "crypto", "http", "https", "os", "net", "tls"],
  plugins: [nodeOnlyGradioWrapper],
  logLevel: "info",
});

// The Decart SDK loads this worker from next to the page script (new URL("./frame-metadata-worker.js",
// import.meta.url)). Without it, live video arrives but can't be decoded and the output stays black.
copyFileSync("node_modules/@decartai/sdk/dist/realtime/browser/frame-metadata-worker.js", "public/frame-metadata-worker.js");
console.log("copied frame-metadata-worker.js");

// Stamp the script's fingerprint into index.html, so browsers fetch the new script after each
// update instead of a cached old one.
const hash = createHash("sha256").update(readFileSync("public/app.js")).digest("hex").slice(0, 12);
const html = readFileSync("public/index.html", "utf8");
const stamped = html.replace(/src="\/app\.js(\?v=[0-9a-f]+)?"/, `src="/app.js?v=${hash}"`);
if (stamped === html && !html.includes(`/app.js?v=${hash}`)) throw new Error("Couldn't find the app.js script tag in index.html");
writeFileSync("public/index.html", stamped);
console.log(`index.html now loads /app.js?v=${hash}`);
