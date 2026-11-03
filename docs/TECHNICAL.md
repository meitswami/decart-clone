# Team docs: running and deploying

Internal notes for the team. The product overview is in the main [README](../README.md).

## What's in the repo

| Path | What it is |
|---|---|
| `server.mjs`, `src/`, `public/` | The current live site: page, live session keys, image fetch, limits |
| `build.mjs` | Bundles `src/` into `public/app.js` and fingerprints it in `index.html` |
| `deploy/` | VPS setup script, systemd service, nginx site, settings template |
| `virtual-tryon/` | Next version: own GPU engine, 4K photo, fit assistant, consented captures (see `virtual-tryon/README.md` and `virtual-tryon/WORKLOG.md`) |
| `docs/assets/` | README images and GIFs |

## New try-on (v2) on the existing domain

v2 = photo try-on up to 4K on an **on-demand GPU**, AI fit estimate, live try-on,
consented training captures and a private gallery. Your VPS serves the site; the
GPU (RunPod) **wakes only when a shopper presses Try it on** and switches itself
off after 15 idle minutes. Shoppers see a "Warming up" loader with an estimate
on the first try-on.

One-time, on the VPS (after merging to `main`):

```bash
sudo bash /opt/live-virtual-try-on/deploy/setup-v2.sh
```

It asks for a RunPod API key (hidden input; use a dedicated key), copies your
domain and live-engine key from the current settings, runs the new site on
127.0.0.1:3071 next to the current app, switches nginx (backup kept) and prints
the gallery link. Re-run any time to update. Go back to the old app instantly:

```bash
sudo bash /opt/live-virtual-try-on/deploy/rollback-v2.sh
```

How the GPU part works:
- Models and a self-contained Python environment live on a RunPod **network
  volume** (`07gwbgb8fj`, EU-RO-1). GPU boxes boot from the tiny
  `python:3.12-slim` image, so a fresh machine starts in under a minute.
- Wake order: resume our stopped box → if its GPU was taken, start a fresh box on
  the same volume (no reinstall). Keeps looking for a free GPU for up to 3 min.
- Safety: the box stops itself after 15 idle minutes; the site also stops it if
  it runs 25 min with no try-on, or never becomes healthy; a failed wake stops it.
- Limits: 6 wakes per visitor per hour, 200 per day (`/etc/live-virtual-try-on-v2.env`).
- Live try-on uses the hosted live engine by default (instant). Add `?engine=gpu`
  to the page address to use our own GPU engine.

## Run the live site locally

Needs Node.js 20+.

```bash
npm install
cp .env.example .env      # then add the live-engine key after DECART_API_KEY=
npm start
```

Open http://localhost:3000. After changing anything in `src/`, run `npm run build`
(the built file is committed so servers don't need a build step). Photo mode works
without a live-engine key.

## Deploy (Ubuntu/Debian VPS)

Point a DNS A record at the server, then on the server:

```bash
git clone https://github.com/meitswami/decart-clone.git /tmp/tryon
sudo bash /tmp/tryon/deploy/setup.sh --email you@example.com
sudo bash /opt/live-virtual-try-on/deploy/set-key.sh   # paste the live-engine key (stays hidden)
```

`setup.sh` installs Node.js 22 (only if Node is missing), nginx and certbot if
needed. It never upgrades packages already on the server, puts the app in
`/opt/live-virtual-try-on`, runs it as a locked-down `systemd` service on
127.0.0.1:3070 (`--port` to change), adds an nginx site and gets an HTTPS
certificate (browsers only allow the camera over HTTPS). It refuses to touch
ports 80/443 if something other than nginx owns them. Run it again to update.

```bash
sudo systemctl status live-virtual-try-on
sudo journalctl -u live-virtual-try-on -f
```

## Settings

Set in `/etc/live-virtual-try-on.env` on the server (or `.env` locally).

| Setting | Default | What it does |
|---|---|---|
| `DECART_API_KEY` | (required for live) | Live-engine API key. Stays on the server. |
| `PUBLIC_ORIGIN` | (none) | Site address; session keys only work from it. Set it in production. |
| `ACCESS_CODE` | (none) | If set, visitors must enter this code to start live. |
| `MAX_SESSION_SECONDS` | 120 | Longest a single live session can run. |
| `TOKENS_PER_IP_PER_HOUR` | 10 | Live sessions one visitor can start per hour. |
| `DAILY_TOKEN_LIMIT` | 300 | Live sessions the whole site can start per day. |
| `TRUST_PROXY` | off | Set to `1` behind nginx so limits use the visitor's real IP. |
| `HOST` / `PORT` | 127.0.0.1 / 3000 | Where the app listens. |

Live time is billed per second by the live engine, so keep `DAILY_TOKEN_LIMIT` and
`MAX_SESSION_SECONDS` at levels you're comfortable with, or set an `ACCESS_CODE`.

## Security notes

- The real API key never reaches the browser.
- Session keys are locked to the try-on models, to `PUBLIC_ORIGIN` and to
  `MAX_SESSION_SECONDS`, and expire 2 minutes after minting.
- `/api/image` only fetches public-internet images on standard ports, re-checks
  the address at connect time, follows at most 3 redirects and caps size at 12 MB.

## Credits and licences

See [CREDITS.md](CREDITS.md).
