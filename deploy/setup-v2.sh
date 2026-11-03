#!/usr/bin/env bash
# Puts the new try-on on the existing site: photo try-on up to 4K on an
# on-demand GPU (wakes when someone tries on, sleeps when idle), AI fit
# estimate, live try-on, consented training captures and a private gallery.
#
#   sudo bash /opt/live-virtual-try-on/deploy/setup-v2.sh
#
# Safe to re-run (updates code and restarts). The current app keeps running on
# its own port, so going back is one command:
#   sudo bash /opt/live-virtual-try-on/deploy/rollback-v2.sh
set -euo pipefail

APP_DIR="/opt/live-virtual-try-on"
OLD_ENV="/etc/live-virtual-try-on.env"
ENV_FILE="/etc/live-virtual-try-on-v2.env"
SERVICE="live-virtual-try-on-v2"
PORT="3071"
DATA_DIR="/var/lib/live-virtual-try-on"
BRANCH="${BRANCH:-main}"
VOLUME="${GPU_NETWORK_VOLUME:-07gwbgb8fj}"   # RunPod network volume holding the models

say() { printf '\n==> %s\n' "$*"; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
[ "$(id -u)" -eq 0 ] || die "Run with sudo."
[ -d "$APP_DIR/.git" ] || die "$APP_DIR not found. Run deploy/setup.sh first."
[ -f "$OLD_ENV" ] || die "$OLD_ENV not found. Run deploy/setup.sh first."
id tryon >/dev/null 2>&1 || die "User 'tryon' missing. Run deploy/setup.sh first."
get() { sed -nE "s/^$1=(.*)$/\1/p" "$2" 2>/dev/null | tail -1; }

say "Updating code ($BRANCH)"
git -C "$APP_DIR" fetch -q origin "$BRANCH"
git -C "$APP_DIR" reset -q --hard "origin/$BRANCH"
chown -R root:tryon "$APP_DIR"
chmod -R g+rX,o-rwx "$APP_DIR"

say "Python environment (site only: no GPU libraries)"
if ! python3 -c "import ensurepip, venv" >/dev/null 2>&1; then
  apt-get update -qq && apt-get install -y -qq python3-venv
fi
VENV="$APP_DIR/virtual-tryon/.venv"
[ -x "$VENV/bin/python" ] || python3 -m venv "$VENV"
"$VENV/bin/pip" install -q --upgrade pip
"$VENV/bin/pip" install -q -r "$APP_DIR/virtual-tryon/requirements-vps.txt"
chown -R root:tryon "$VENV"; chmod -R g+rX,o-rwx "$VENV"

say "Data folder for consented captures: $DATA_DIR"
mkdir -p "$DATA_DIR/captures" "$DATA_DIR/recordings"
chown -R tryon:tryon "$DATA_DIR"
chmod 750 "$DATA_DIR"

say "Settings: $ENV_FILE"
if [ ! -f "$ENV_FILE" ]; then
  ORIGIN="$(get PUBLIC_ORIGIN "$OLD_ENV")"
  DKEY="$(get DECART_API_KEY "$OLD_ENV")"
  MAXS="$(get MAX_SESSION_SECONDS "$OLD_ENV")"; MAXS="${MAXS:-120}"
  PERIP="$(get TOKENS_PER_IP_PER_HOUR "$OLD_ENV")"; PERIP="${PERIP:-10}"
  DAILY="$(get DAILY_TOKEN_LIMIT "$OLD_ENV")"; DAILY="${DAILY:-300}"
  rnd() { python3 -c "import secrets; print(secrets.token_urlsafe($1))"; }
  LIVE="local"; [ -n "$DKEY" ] && LIVE="decart"
  umask 027
  cat > "$ENV_FILE" <<EOT
BRAND_NAME=Live Virtual Try-On
HOST=127.0.0.1
PORT=$PORT
PUBLIC_ORIGIN=$ORIGIN
TRUST_PROXY=true
# Live try-on: "decart" = hosted live engine (instant), "local" = our GPU engine.
# Visitors can also open the page with ?engine=gpu to use our GPU engine.
LIVE_BACKEND=$LIVE
LIVE_TRANSPORT=ws
DECART_API_KEY=$DKEY
DECART_MODEL=lucy-vton-latest
MAX_SESSION_SECONDS=$MAXS
TOKENS_PER_IP_PER_HOUR=$PERIP
SITE_DAILY_SESSIONS=$DAILY
SITE_DAILY_SECONDS=$((DAILY * MAXS))
PER_STORE_DAILY_SESSIONS=$DAILY
# On-demand GPU (RunPod): wakes on Try it on, sleeps after GPU_IDLE_MINUTES.
GPU_MODE=runpod
RUNPOD_API_KEY=
GPU_NETWORK_VOLUME=$VOLUME
GPU_GIT_BRANCH=$BRANCH
GPU_IDLE_MINUTES=2
GPU_WAKES_PER_IP_PER_HOUR=6
GPU_DAILY_WAKE_LIMIT=200
GPU_STATE_FILE=$DATA_DIR/gpu_state.json
WORKER_TOKEN=$(rnd 32)
ENGINE_SECRET=$(rnd 32)
# Consented training captures + private gallery at /admin?token=ADMIN_TOKEN
RECORDING_ENABLED=true
CAPTURES_DIR=$DATA_DIR/captures
RECORDINGS_DIR=$DATA_DIR/recordings
KEEPALIVE_FILE=$DATA_DIR/.keepalive
ADMIN_TOKEN=$(rnd 18)
EOT
  chown root:tryon "$ENV_FILE"
  chmod 640 "$ENV_FILE"
  echo "Created $ENV_FILE (copied your domain and live-engine key from $OLD_ENV)."
fi

# Owner's choice (Nov 2026): GPU sleeps after 2 idle minutes (was 15).
sed -i -E 's/^GPU_IDLE_MINUTES=15$/GPU_IDLE_MINUTES=2/' "$ENV_FILE"

if ! grep -qE '^RUNPOD_API_KEY=.{10,}' "$ENV_FILE"; then
  echo
  echo "The site needs a RunPod API key to wake the GPU (create one at"
  echo "https://console.runpod.io/user/settings → API Keys, permission: All)."
  read -rsp "Paste the RunPod API key, then press Enter (it stays hidden): " RKEY
  echo
  RKEY="$(printf '%s' "$RKEY" | tr -d '[:space:]')"
  [ "${#RKEY}" -ge 10 ] || die "That doesn't look like a key. Nothing changed; re-run to try again."
  case "$RKEY" in *[\"\'\#\\]*) die "Unexpected characters in the key. Nothing changed." ;; esac
  TMP="$(mktemp)"; trap 'rm -f "$TMP"' EXIT
  RKEY="$RKEY" awk 'BEGIN{k=ENVIRON["RKEY"]} /^RUNPOD_API_KEY=/{print "RUNPOD_API_KEY=" k; next} {print}' "$ENV_FILE" > "$TMP"
  install -m 640 -o root -g tryon "$TMP" "$ENV_FILE"
fi

say "Service: $SERVICE (127.0.0.1:$PORT)"
cat > "/etc/systemd/system/$SERVICE.service" <<EOT
[Unit]
Description=Live Virtual Try-On v2 (site + on-demand GPU)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=tryon
Group=tryon
WorkingDirectory=$APP_DIR/virtual-tryon
EnvironmentFile=$ENV_FILE
ExecStart=$VENV/bin/uvicorn server.main:app --host 127.0.0.1 --port $PORT --proxy-headers --forwarded-allow-ips 127.0.0.1 --ws-max-size 4194304
Restart=always
RestartSec=3
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ReadWritePaths=$DATA_DIR
ProtectHome=true
ProtectKernelTunables=true
ProtectControlGroups=true
RestrictSUIDSGID=true
LockPersonality=true

[Install]
WantedBy=multi-user.target
EOT
systemctl daemon-reload
systemctl enable -q "$SERVICE"
systemctl restart "$SERVICE"
for i in $(seq 1 30); do curl -fs "http://127.0.0.1:$PORT/health" >/dev/null && break; sleep 1; done
curl -fs "http://127.0.0.1:$PORT/health" >/dev/null || { journalctl -u "$SERVICE" -n 40 --no-pager; die "New site didn't start (log above). The current site is untouched."; }

say "Pointing the website at the new site"
DOMAIN="$(get PUBLIC_ORIGIN "$ENV_FILE" | sed -E 's#^https?://##; s#/.*$##')"
[ -n "$DOMAIN" ] || die "PUBLIC_ORIGIN is empty in $ENV_FILE."
SITE="/etc/nginx/sites-available/$DOMAIN"; [ -f "$SITE" ] || SITE="/etc/nginx/conf.d/$DOMAIN.conf"
[ -f "$SITE" ] || die "nginx site for $DOMAIN not found."
OLD_PORT="$(get PORT "$OLD_ENV")"; OLD_PORT="${OLD_PORT:-3070}"
[ -f "$SITE.before-v2" ] || cp -p "$SITE" "$SITE.before-v2"
sed -i -E \
  -e "s#proxy_pass http://127\.0\.0\.1:$OLD_PORT;#proxy_pass http://127.0.0.1:$PORT;#" \
  -e "s#client_max_body_size [0-9]+[mM];#client_max_body_size 30m;#" \
  -e "s#proxy_read_timeout [0-9]+s;#proxy_read_timeout 300s;#" \
  "$SITE"
# Live try-on on our GPU engine streams over a WebSocket: let nginx pass the upgrade.
if ! grep -q 'proxy_set_header Upgrade' "$SITE"; then
  sed -i -E 's#^([[:space:]]*)proxy_http_version 1\.1;#&\n\1proxy_set_header Upgrade $http_upgrade;\n\1proxy_set_header Connection $http_connection;#' "$SITE"
fi
if ! nginx -t >/dev/null 2>&1; then
  cp -p "$SITE.before-v2" "$SITE"
  die "nginx rejected the change; restored the previous config. Nothing else changed."
fi
systemctl reload nginx

ADMIN="$(get ADMIN_TOKEN "$ENV_FILE")"
say "Done"
echo "Site:     https://$DOMAIN/"
echo "Gallery:  https://$DOMAIN/admin?token=$ADMIN   (keep this private)"
echo "Old app still runs on 127.0.0.1:$OLD_PORT. Roll back: sudo bash $APP_DIR/deploy/rollback-v2.sh"
echo "Logs:     sudo journalctl -u $SERVICE -f"
