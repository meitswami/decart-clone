#!/usr/bin/env bash
# Installs or updates the try-on app on an Ubuntu/Debian server, behind nginx with HTTPS.
# Safe to run again: it pulls the latest code and restarts the app.
#
#   sudo bash deploy/setup.sh --email you@example.com
#
# --email is used for the free Let's Encrypt HTTPS certificate (needed for camera access).
set -euo pipefail

DOMAIN="live-virtual-try-on.brandzaha.com"
REPO="https://github.com/meitswami/decart-clone.git"
APP_DIR="/opt/live-virtual-try-on"
ENV_FILE="/etc/live-virtual-try-on.env"
SERVICE="live-virtual-try-on"
EMAIL=""

while [ $# -gt 0 ]; do
  case "$1" in
    --email) EMAIL="$2"; shift 2 ;;
    --domain) DOMAIN="$2"; shift 2 ;;
    *) echo "Unknown option: $1" >&2; exit 1 ;;
  esac
done

say() { printf '\n==> %s\n' "$*"; }
die() { printf '\nError: %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "Run as root (sudo bash deploy/setup.sh)."
command -v apt-get >/dev/null || die "This script supports Ubuntu/Debian only."

say "Checking what already serves ports 80 and 443"
LISTENERS="$(ss -ltnpH '( sport = :80 or sport = :443 )' 2>/dev/null || true)"
if [ -n "$LISTENERS" ] && ! grep -q nginx <<<"$LISTENERS"; then
  echo "$LISTENERS"
  die "Something other than nginx uses port 80/443. Not touching it; configure that proxy to forward $DOMAIN to 127.0.0.1:3000 instead."
fi

say "Installing system packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq ca-certificates curl git nginx certbot python3-certbot-nginx >/dev/null

NODE_MAJOR="$(node -v 2>/dev/null | sed -E 's/^v([0-9]+).*/\1/' || echo 0)"
if [ "${NODE_MAJOR:-0}" -lt 22 ]; then
  say "Installing Node.js 22"
  curl -fsSL https://deb.nodesource.com/setup_22.x | bash - >/dev/null
  apt-get install -y -qq nodejs >/dev/null
fi
echo "Node $(node -v), npm $(npm -v)"

say "Creating the app user"
id tryon >/dev/null 2>&1 || useradd --system --home "$APP_DIR" --shell /usr/sbin/nologin tryon

say "Getting the code"
if [ -d "$APP_DIR/.git" ]; then
  git -C "$APP_DIR" fetch -q origin
  git -C "$APP_DIR" reset -q --hard origin/main
else
  git clone -q "$REPO" "$APP_DIR"
fi
cd "$APP_DIR"
npm ci --omit=dev --no-audit --no-fund
chown -R root:tryon "$APP_DIR"
chmod -R g+rX,o-rwx "$APP_DIR"

say "Server settings"
if [ ! -f "$ENV_FILE" ]; then
  sed "s#^PUBLIC_ORIGIN=.*#PUBLIC_ORIGIN=https://$DOMAIN#" deploy/server.env.example > "$ENV_FILE"
  echo "Created $ENV_FILE (add your DECART_API_KEY there)."
fi
chown root:tryon "$ENV_FILE"
chmod 640 "$ENV_FILE"

say "Starting the app"
install -m 644 deploy/live-virtual-try-on.service "/etc/systemd/system/$SERVICE.service"
systemctl daemon-reload
systemctl enable -q "$SERVICE"
systemctl restart "$SERVICE"

say "Configuring nginx for $DOMAIN"
if [ -d /etc/nginx/sites-available ]; then
  SITE="/etc/nginx/sites-available/$DOMAIN"
  if [ ! -f "$SITE" ]; then
    sed "s/live-virtual-try-on.brandzaha.com/$DOMAIN/g" deploy/nginx.conf > "$SITE"
  fi
  ln -sf "$SITE" "/etc/nginx/sites-enabled/$DOMAIN"
else
  SITE="/etc/nginx/conf.d/$DOMAIN.conf"
  [ -f "$SITE" ] || sed "s/live-virtual-try-on.brandzaha.com/$DOMAIN/g" deploy/nginx.conf > "$SITE"
fi
nginx -t
systemctl enable -q nginx
systemctl reload nginx || systemctl restart nginx

if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then
  say "Opening the firewall for web traffic"
  ufw allow 'Nginx Full' >/dev/null
fi

say "HTTPS certificate"
if [ -d "/etc/letsencrypt/live/$DOMAIN" ]; then
  echo "Certificate already present."
elif [ -n "$EMAIL" ]; then
  certbot --nginx -d "$DOMAIN" --non-interactive --agree-tos -m "$EMAIL" --redirect
else
  echo "Skipped: re-run with --email you@example.com to get a free Let's Encrypt certificate."
fi

say "Health check"
sleep 1
curl -fsS "http://127.0.0.1:3000/healthz" && echo
curl -fsS "http://127.0.0.1:3000/api/config" && echo

if ! grep -qE '^DECART_API_KEY=.+' "$ENV_FILE"; then
  cat <<EOF

Almost done. Add your Decart API key:
  sudo nano $ENV_FILE        # paste it after DECART_API_KEY=
  sudo systemctl restart $SERVICE
EOF
fi
echo
echo "Done: https://$DOMAIN"
