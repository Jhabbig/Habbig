#!/bin/bash
set -e
PIP=$(command -v pip3 || command -v pip)
PYTHON=$(command -v python3 || command -v python)
APP_NAME="PolymarketDashboard"
VERSION="1.0.0"
DIST_DIR="dist"
DMG_DIR="dmg_staging"
DMG_OUTPUT="${APP_NAME}-${VERSION}-arm64.dmg"

echo "=== Polymarket Signal Dashboard - macOS Build ==="
echo "-> Cleaning..."
rm -rf build/ dist/ "${DMG_DIR}/" "${DMG_OUTPUT}"

echo "-> Installing deps..."
$PIP install -r requirements.txt
$PIP install Pillow pyinstaller

echo "-> Generating icons..."
$PYTHON scripts/generate_icons.py

echo "-> Building .app..."
$PYTHON -m PyInstaller polymarket.spec --noconfirm --clean

APP_PATH=""
for p in "${DIST_DIR}/${APP_NAME}.app" "${DIST_DIR}/${APP_NAME}/${APP_NAME}.app"; do
    [ -d "$p" ] && APP_PATH="$p" && break
done
[ -z "$APP_PATH" ] && echo "ERROR: .app not found" && ls -la "${DIST_DIR}/" 2>/dev/null && exit 1
echo "  Found: ${APP_PATH}"

echo "-> Staging DMG..."
mkdir -p "${DMG_DIR}"
cp -r "${APP_PATH}" "${DMG_DIR}/"

echo "-> Creating .dmg..."
if command -v create-dmg &>/dev/null; then
    create-dmg --volname "${APP_NAME} ${VERSION}" --volicon "app/desktop/assets/icon.icns" --window-pos 200 120 --window-size 660 400 --icon-size 160 --icon "${APP_NAME}.app" 180 170 --hide-extension "${APP_NAME}.app" --app-drop-link 480 170 "${DMG_OUTPUT}" "${DMG_DIR}/" || true
else
    echo "  create-dmg not found, using hdiutil..."
    hdiutil create -volname "${APP_NAME}" -srcfolder "${DMG_DIR}" -ov -format UDZO "${DMG_OUTPUT}"
fi

[ -f "${DMG_OUTPUT}" ] && echo -e "\n=== Done: ${DMG_OUTPUT} ($(du -sh "${DMG_OUTPUT}" | cut -f1)) ===" || echo -e "\n.app at: ${APP_PATH}"
