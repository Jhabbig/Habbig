#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# smoke-test.sh — hit the most important endpoints and verify status codes
#
# Usage:
#   bash scripts/smoke-test.sh                           # hits https://narve.ai
#   URL=https://staging.narve.ai bash scripts/smoke-test.sh
# ─────────────────────────────────────────────────────────────────────────────

set -u

URL="${URL:-https://narve.ai}"
FAILED=0

check() {
    local path="$1"
    local expected="$2"
    local actual
    actual=$(curl -sS -o /dev/null -w '%{http_code}' "${URL}${path}")
    if [ "$actual" = "$expected" ]; then
        printf "  \033[32m✓\033[0m %-40s %s\n" "$path" "$actual"
    else
        printf "  \033[31m✗\033[0m %-40s %s (expected %s)\n" "$path" "$actual" "$expected"
        FAILED=$((FAILED + 1))
    fi
}

echo "→ Smoke testing $URL"
echo ""

# Public pages (no auth required)
echo "Public:"
check "/" 200
check "/health" 200
check "/gate" 200       # shows the site access gate page

echo ""
echo "Gate-protected endpoints (redirect to /gate without the gate cookie):"
# The gate middleware runs before every route that isn't in _PUBLIC_PATHS,
# so unauthenticated requests to anything non-public get 302 -> /gate.
check "/api/markets/unified" 302
check "/api/markets/portfolio" 302
check "/api/markets/orders" 302
check "/api/markets/connections" 302
check "/dashboards" 302
check "/settings" 302
check "/billing" 302
check "/admin" 302

echo ""
echo "Static assets (should 200 with Cache-Control):"
check "/_gateway_static/gateway.css" 200

echo ""
if [ "$FAILED" -eq 0 ]; then
    echo "✓ all checks passed"
else
    echo "✗ $FAILED check(s) failed"
    exit 1
fi

# Detailed /health JSON
echo ""
echo "→ /health detail:"
curl -sS "${URL}/health" | python3 -m json.tool 2>/dev/null || echo "(JSON parse failed)"
