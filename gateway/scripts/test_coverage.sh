#!/usr/bin/env bash
# scripts/test_coverage.sh — run the gateway test suite with coverage.
#
# Local:     scripts/test_coverage.sh
# CI:        scripts/test_coverage.sh --cov-fail-under=60
# Focused:   scripts/test_coverage.sh tests/integration/
#
# Honours any extra args as pytest args, so you can scope + add flags.
# Writes the HTML report to /tmp/cov_html and terminal output to stdout.

set -euo pipefail

HERE="$(cd "$(dirname "$0")/.." && pwd)"
cd "$HERE"

# Default exclusion: slow/network. CI overrides via GATEWAY_TEST_MARKERS.
MARKERS="${GATEWAY_TEST_MARKERS:-not slow and not network}"

python3 -m pytest tests/ \
    --cov=. \
    --cov-config=.coveragerc \
    --cov-report=term-missing \
    --cov-report=html:/tmp/cov_html \
    -m "$MARKERS" \
    "$@"

echo
echo "Coverage HTML: file:///tmp/cov_html/index.html"
