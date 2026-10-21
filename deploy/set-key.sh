#!/usr/bin/env bash
# Saves the Decart API key on the server without showing it on screen, then restarts the app.
#
#   ssh -t root@your-server bash /opt/live-virtual-try-on/deploy/set-key.sh
set -euo pipefail

ENV_FILE="/etc/live-virtual-try-on.env"
SERVICE="live-virtual-try-on"

[ "$(id -u)" -eq 0 ] || { echo "Run as root."; exit 1; }
[ -f "$ENV_FILE" ] || { echo "$ENV_FILE is missing. Run deploy/setup.sh first."; exit 1; }

read -rsp "Paste your Decart API key, then press Enter (it stays hidden): " KEY
echo
KEY="$(printf '%s' "$KEY" | tr -d '[:space:]')"
if [ "${#KEY}" -lt 10 ]; then
  echo "That doesn't look like a key. Nothing changed."
  exit 1
fi
case "$KEY" in
  *[\"\'\#\\]*) echo "The key has unexpected characters (quotes, # or backslash). Nothing changed."; exit 1 ;;
esac

TMP="$(mktemp)"
trap 'rm -f "$TMP"' EXIT
KEY="$KEY" awk 'BEGIN { k = ENVIRON["KEY"]; done = 0 }
  /^DECART_API_KEY=/ { print "DECART_API_KEY=" k; done = 1; next }
  { print }
  END { if (!done) print "DECART_API_KEY=" k }' "$ENV_FILE" > "$TMP"
install -m 640 -o root -g tryon "$TMP" "$ENV_FILE"

systemctl restart "$SERVICE"
sleep 1
PORT="$(sed -nE 's/^PORT=([0-9]+).*/\1/p' "$ENV_FILE" | tail -1)"
if curl -fsS "http://127.0.0.1:${PORT:-3000}/api/config" | grep -q '"ready":true'; then
  echo "Key saved (ending in ...${KEY: -4}). The app restarted and is ready."
else
  echo "Key saved, but the app doesn't report ready. Check: journalctl -u $SERVICE -n 20"
fi
