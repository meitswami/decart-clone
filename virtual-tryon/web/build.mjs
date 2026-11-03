// Bundles web/src/app.js (with the realtime SDK) into web/app.js.
// Uses the repo root's node_modules, so nothing new to install:
//   (repo root)  npm install
//   (repo root)  node virtual-tryon/web/build.mjs
// The built files are committed, so the server never needs Node.

import { build } from "esbuild";
import { createHash } from "node:crypto";
import { copyFileSync, readFileSync, writeFileSync } from "node:fs";
import { createRequire } from "node:module";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const require = createRequire(import.meta.url);

await build({
  entryPoints: [path.join(here, "src/app.js")],
  bundle: true,
  minify: true,
  format: "esm",
  target: "es2022",
  platform: "browser",
  outfile: path.join(here, "app.js"),
  logLevel: "info",
});

// The SDK loads this worker from next to the page script; without it the live
// video arrives but can't be decoded and stays black.
const sdkDir = path.dirname(require.resolve("@decartai/sdk/package.json"));
copyFileSync(path.join(sdkDir, "dist/realtime/browser/frame-metadata-worker.js"), path.join(here, "frame-metadata-worker.js"));

// Fingerprint the script in index.html so browsers fetch new builds.
const hash = createHash("sha256").update(readFileSync(path.join(here, "app.js"))).digest("hex").slice(0, 12);
const htmlPath = path.join(here, "index.html");
const html = readFileSync(htmlPath, "utf8");
const stamped = html.replace(/src="\/app\.js(\?v=[0-9a-f]+)?"/, `src="/app.js?v=${hash}"`);
if (!stamped.includes(`/app.js?v=${hash}`)) throw new Error("Couldn't find the app.js script tag in index.html");
writeFileSync(htmlPath, stamped);
console.log(`index.html now loads /app.js?v=${hash}`);
