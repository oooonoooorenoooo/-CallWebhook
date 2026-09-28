#!/usr/bin/with-contenv bash
set -euo pipefail
TARGET=/homeassistant/custom_components/callwebhook
BASE=https://raw.githubusercontent.com/oooonoooorenoooo/-CallWebhook/main/homeassistant/custom_components/callwebhook
mkdir -p "$TARGET" /homeassistant/callwebhook
curl -fsSL "$BASE/__init__.py" -o "$TARGET/__init__.py"
curl -fsSL "$BASE/manifest.json" -o "$TARGET/manifest.json"
if ! grep -Eq '^[[:space:]]*callwebhook:[[:space:]]*$' /homeassistant/configuration.yaml; then
  printf '\ncallwebhook:\n' >> /homeassistant/configuration.yaml
fi
echo "CallWebhook backend files installed successfully."
if [ -n "${SUPERVISOR_TOKEN:-}" ]; then
  echo "Requesting Home Assistant restart..."
  if curl -fsS -X POST -H "Authorization: Bearer $SUPERVISOR_TOKEN" -H "Content-Type: application/json" http://supervisor/core/restart >/dev/null; then
    echo "Home Assistant restart requested."
  else
    echo "Automatic restart was not accepted. Please restart Home Assistant manually."
  fi
else
  echo "Home Assistant must now be restarted manually to load CallWebhook."
fi
