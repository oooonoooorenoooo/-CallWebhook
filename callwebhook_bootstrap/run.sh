#!/usr/bin/env bash
set -euo pipefail
TARGET=/config/custom_components/callwebhook
BASE=https://raw.githubusercontent.com/oooonoooorenoooo/-CallWebhook/main/homeassistant/custom_components/callwebhook
mkdir -p "$TARGET" /config/callwebhook
curl -fsSL "$BASE/__init__.py" -o "$TARGET/__init__.py"
curl -fsSL "$BASE/manifest.json" -o "$TARGET/manifest.json"
if ! grep -Eq '^[[:space:]]*callwebhook:[[:space:]]*$' /config/configuration.yaml; then
  printf '\ncallwebhook:\n' >> /config/configuration.yaml
fi
echo "CallWebhook backend installed. Requesting Home Assistant restart..."
curl -fsS -X POST -H "Authorization: Bearer ${SUPERVISOR_TOKEN:-}" -H "Content-Type: application/json" http://supervisor/core/restart >/dev/null || {
  echo "Automatic Home Assistant restart unavailable; backend files were installed successfully."
  exit 0
}
