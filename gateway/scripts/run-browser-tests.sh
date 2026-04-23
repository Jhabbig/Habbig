#!/usr/bin/env bash
# One-shot installer + runner for the Playwright browser suite.
#
# The browser tests live in gateway/tests/browser/ and skip by default
# when playwright isn't installed. This script closes that gap for
# local dev and CI:
#
#   1. Installs the `playwright` pip package (idempotent).
#   2. Runs `playwright install` to download the browser binaries
#      (idempotent; Playwright caches them under ~/.cache/ms-playwright).
#   3. Runs pytest against tests/browser with reasonable defaults.
#
# Optional flags:
#   --engines <chromium,firefox,webkit>   default: all three
#   --headed                              open a window (debugging)
#   --update-baselines                    re-generate screenshot baselines
#
# Example:
#   gateway/scripts/run-browser-tests.sh --engines chromium --headed
set -euo pipefail

cd "$(dirname "$0")/.."

ENGINES="chromium firefox webkit"
HEADED=0
UPDATE=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --engines)  ENGINES="${2//,/ }"; shift 2 ;;
    --headed)   HEADED=1; shift ;;
    --update-baselines) UPDATE=1; shift ;;
    -*)
      echo "Unknown option: $1" >&2; exit 2 ;;
    *)
      break ;;
  esac
done

if ! python3 -c "import playwright" 2>/dev/null; then
  echo "→ installing playwright (python package)…"
  python3 -m pip install --quiet playwright
fi

echo "→ ensuring playwright browser binaries for: $ENGINES"
python3 -m playwright install --with-deps $ENGINES

export NARVE_BROWSER_HEADED="$HEADED"
if [[ "$UPDATE" == "1" ]]; then
  rm -rf tests/browser/screenshots
  echo "→ baselines cleared — fresh set will be captured"
fi

exec python3 -m pytest tests/browser -v "$@"
