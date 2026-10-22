# Live Virtual Try-On

See clothes on yourself with your webcam, in two modes:

- **Live video** (paid): streams your camera to Decart's realtime **Lucy VTON**
  model and plays back video of you wearing the garment you pick.
- **Photo (free)**: takes one photo of you and sends it, with the garment, to the
  public [Leffa](https://github.com/franciszzj/Leffa) demo on Hugging Face (MIT
  model). The result arrives in 20–40 seconds. Hugging Face's free GPU quota allows
  about **one photo per day per internet connection** without a login. Leffa does
  tops, bottoms and dresses (not hats).

If live mode can't run (no key, out of credits, limits reached) the page offers
photo mode instead, and starts in photo mode when the server has no Decart key.

Live at **https://live-virtual-try-on.brandzaha.com**

## How live mode works

1. The browser asks this app's server for a session key (`POST /api/token`).
   The server holds the real Decart API key and hands out a short-lived key that
   only works for the try-on models, only from this website, and only for a capped
   session length.
2. The browser opens the camera and connects to `lucy-vton-latest` over WebRTC
   using the official [`@decartai/sdk`](https://www.npmjs.com/package/@decartai/sdk).
3. Picking a garment sends its photo plus a short description with `set()`.
   The model dresses you in it in real time. Switching garments needs no reconnect.

Garments can come from the built-in samples, an uploaded photo, a pasted image,
an image link, or an image dragged in from a shop in another tab. The
"Describe it" field improves results for your own photos.

This is the same design as the Anywear Chrome extension, rebuilt from Decart's
public SDK and docs.

## How photo mode works

`src/photo.js` calls the Leffa demo's `/leffa_predict_vt` endpoint straight from
the visitor's browser with [`@gradio/client`](https://www.npmjs.com/package/@gradio/client),
so each visitor uses their own free quota and this server does no extra work.
Requests to `*.hf.space` are sent without cookies, because the demo's cross-site
rules don't allow them. Tops use the VITON-HD model; bottoms and dresses use
DressCode. The photo is a 3:4 portrait cropped from the camera, mirrored like the
preview.

## Run locally

Needs Node.js 20+.

```bash
npm install
cp .env.example .env      # then paste your key after DECART_API_KEY=
npm start
```

Open http://localhost:3000. After changing anything in `src/`, run `npm run build`
(`build.mjs` bundles `public/app.js` and stamps its fingerprint into `index.html`
so browsers pick up the new version; the built file is committed so servers don't
need a build step). Photo mode works without a Decart key.

## Deploy (Ubuntu/Debian VPS)

Point a DNS A record at the server, then on the server:

```bash
git clone https://github.com/meitswami/decart-clone.git /tmp/decart-clone
sudo bash /tmp/decart-clone/deploy/setup.sh --email you@example.com
sudo bash /opt/live-virtual-try-on/deploy/set-key.sh   # paste your Decart key (stays hidden)
```

`setup.sh` installs Node.js 22 (only if Node is missing), nginx and certbot if needed. It never upgrades packages already on the server, puts the app in
`/opt/live-virtual-try-on`, runs it as a locked-down `systemd` service on
127.0.0.1:3070 (`--port` to change), adds an nginx site, and gets a free HTTPS certificate (browsers
only allow camera access over HTTPS). It refuses to touch ports 80/443 if
something other than nginx owns them. Run it again to update to the latest code.

Useful commands:

```bash
sudo systemctl status live-virtual-try-on
sudo journalctl -u live-virtual-try-on -f
```

## Settings

Set in `/etc/live-virtual-try-on.env` on the server (or `.env` locally).

| Setting | Default | What it does |
|---|---|---|
| `DECART_API_KEY` | (required) | Your key from https://platform.decart.ai |
| `PUBLIC_ORIGIN` | (none) | Site address; session keys only work from it. Set it in production. |
| `ACCESS_CODE` | (none) | If set, visitors must enter this code to start. |
| `MAX_SESSION_SECONDS` | 120 | Longest a single session can run (enforced by Decart too). |
| `TOKENS_PER_IP_PER_HOUR` | 10 | Sessions one visitor can start per hour. |
| `DAILY_TOKEN_LIMIT` | 300 | Sessions the whole site can start per day. |
| `TRUST_PROXY` | off | Set to `1` behind nginx so limits use the visitor's real IP. |
| `HOST` / `PORT` | 127.0.0.1 / 3000 | Where the app listens. |

## Cost

Decart charges about **$0.02 per second** of live try-on ($1.20 per minute).
With the defaults, the worst case is 300 sessions × 2 minutes per day, so lower
`DAILY_TOKEN_LIMIT` or set an `ACCESS_CODE` if that is more than you want to
risk. Sessions also stop when the visitor presses Stop or leaves the tab.

## Security notes

- The real API key never reaches the browser.
- Session keys are locked to the try-on models, to `PUBLIC_ORIGIN`, and to
  `MAX_SESSION_SECONDS`, and expire 2 minutes after minting.
- `/api/image` only fetches public-internet images on standard ports, re-checks
  the address at connect time, follows at most 3 redirects, and caps size at 12 MB.

## Files

- `server.mjs`: the server (page, session keys, image fetch, limits)
- `src/app.js`: page logic for both modes; `src/photo.js`: the free Leffa photo call
- `build.mjs`: bundles `src/` into `public/app.js` and fingerprints it in `index.html`
- `public/index.html`: the page
- `public/garments/`: sample garments and their prompts (`catalog.json`)
- `deploy/`: setup script, systemd service, nginx site, settings template

## Credits

Sample garment photos and prompts are from
[DecartAI/tryon-examples](https://github.com/DecartAI/tryon-examples), MIT
License (see `public/garments/LICENSE-tryon-examples.txt`). Try-on video is
generated by Decart's Lucy VTON model. Free photo try-on uses
[Leffa](https://github.com/franciszzj/Leffa) (MIT) through its public Hugging Face
demo, [franciszzj/Leffa](https://huggingface.co/spaces/franciszzj/Leffa).
