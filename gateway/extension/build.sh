#!/usr/bin/env bash
# build.sh — Package the narve.ai browser extension for Chrome Web Store
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
DIST_DIR="$SCRIPT_DIR/../dist"
EXT_DIR="$DIST_DIR/extension"

echo "Building narve.ai extension…"

# Clean previous build
rm -rf "$EXT_DIR" "$DIST_DIR/narve-extension.zip"
mkdir -p "$EXT_DIR"

# Copy extension files
cp "$SCRIPT_DIR/manifest.json" "$EXT_DIR/"
cp "$SCRIPT_DIR/background.js" "$EXT_DIR/"
cp "$SCRIPT_DIR/content.js" "$EXT_DIR/"
cp "$SCRIPT_DIR/content.css" "$EXT_DIR/"

# Popup
mkdir -p "$EXT_DIR/popup"
cp "$SCRIPT_DIR/popup/popup.html" "$EXT_DIR/popup/"
cp "$SCRIPT_DIR/popup/popup.js" "$EXT_DIR/popup/"
cp "$SCRIPT_DIR/popup/popup.css" "$EXT_DIR/popup/"

# Icons
mkdir -p "$EXT_DIR/icons"
cp "$SCRIPT_DIR/icons/"*.png "$EXT_DIR/icons/"

# Create zip for Chrome Web Store submission
cd "$DIST_DIR"
zip -r narve-extension.zip extension/

echo ""
echo "Done. Output:"
echo "  Directory: $EXT_DIR"
echo "  Archive:   $DIST_DIR/narve-extension.zip"
echo ""
ls -lh "$DIST_DIR/narve-extension.zip"
