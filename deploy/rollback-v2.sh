#!/usr/bin/env bash
# Points the website back at the previous app (which never stopped running).
#   sudo bash /opt/live-virtual-try-on/deploy/rollback-v2.sh
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "Run with sudo."; exit 1; }
ENV_FILE="/etc/live-virtual-try-on-v2.env"
DOMAIN="$(sed -nE 's/^PUBLIC_ORIGIN=(.*)$/\1/p' "$ENV_FILE" | tail -1 | sed -E 's#^https?://##; s#/.*$##')"
SITE="/etc/nginx/sites-available/$DOMAIN"; [ -f "$SITE" ] || SITE="/etc/nginx/conf.d/$DOMAIN.conf"
[ -f "$SITE.before-v2" ] || { echo "No backup at $SITE.before-v2; nothing to roll back."; exit 1; }
cp -p "$SITE.before-v2" "$SITE"
nginx -t && systemctl reload nginx
echo "The website is back on the previous app. The new one is still installed:"
echo "re-run deploy/setup-v2.sh to switch again."
