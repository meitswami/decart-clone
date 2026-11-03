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
