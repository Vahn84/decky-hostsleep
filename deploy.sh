#!/usr/bin/env bash
# Build, package and deploy HostSleep to a Steam Deck over SSH.
# Usage: DECK_HOST=deck@192.168.1.xx ./deploy.sh
set -euo pipefail
cd "$(dirname "$0")"

DECK_HOST="${DECK_HOST:-deck@steamdeck.local}"
PLUGIN=HostSleep

npm run build

rm -rf build
mkdir -p "build/$PLUGIN/dist"
cp plugin.json package.json main.py README.md LICENSE "build/$PLUGIN/"
cp dist/index.js "build/$PLUGIN/dist/"
(cd build && zip -qr "$PLUGIN.zip" "$PLUGIN")
tar -czf "build/$PLUGIN.tar.gz" -C build "$PLUGIN"

echo "==> Copying to $DECK_HOST"
scp "build/$PLUGIN.tar.gz" "$DECK_HOST:/tmp/"

echo "==> Installing (you may be asked for the deck user's sudo password)"
ssh -t "$DECK_HOST" "sudo tar -xzf /tmp/$PLUGIN.tar.gz -C /home/deck/homebrew/plugins/ \
  && rm /tmp/$PLUGIN.tar.gz \
  && sudo systemctl restart plugin_loader"

echo "==> Done. Zip for manual install: build/$PLUGIN.zip"
