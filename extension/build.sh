#!/usr/bin/env bash
# Build extension.zip for the Chrome Web Store.
#
# The zip contains only the files needed at runtime:
#   manifest.json, background.js, content.js, content.css,
#   popup/**, icons/**
#
# The placeholder PNG icons in icons/ should be replaced with the
# actual branded PNGs before publishing — the zip will still be valid
# with placeholders for local testing.

set -euo pipefail

cd "$(dirname "$0")"

OUT="extension.zip"
rm -f "$OUT"

zip -r "$OUT" \
  manifest.json \
  background.js \
  content.js \
  content.css \
  popup \
  icons

echo "built: $OUT"
