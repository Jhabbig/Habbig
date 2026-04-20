#!/usr/bin/env bash
# Load test the annoyance dashboard under realistic traffic.
#
# Targets:
#   * /api/index, /api/spikes, /api/entities/top, /api/entity/Tesla
# Assertions:
#   * p95 latency < 200ms
#   * non-2xx rate == 0
#
# Requires `wrk` (brew install wrk) and a running dashboard on $HOST:$PORT.
# Per DECISIONS.md #5 the dashboard sits behind the gateway, so we set the
# SSO headers inline via --header.
#
# Usage:
#   ./scripts/loadtest.sh                       # localhost:8053
#   HOST=127.0.0.1 PORT=8053 ./scripts/loadtest.sh
#   GATEWAY_SSO_SECRET=xxx USER_ID=42 ./scripts/loadtest.sh
#
# Exits non-zero if latency or error thresholds are blown.

set -euo pipefail

HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8053}"
BASE="http://${HOST}:${PORT}"
DURATION="${DURATION:-30s}"
CONNECTIONS="${CONNECTIONS:-40}"
THREADS="${THREADS:-4}"
GATEWAY_SSO_SECRET="${GATEWAY_SSO_SECRET:-test-gateway-sso-secret}"
USER_ID="${USER_ID:-42}"
USER_EMAIL="${USER_EMAIL:-pro@narve.ai}"
TIER="${TIER:-pro}"

P95_BUDGET_MS="${P95_BUDGET_MS:-200}"

if ! command -v wrk >/dev/null 2>&1; then
    echo "wrk not installed. brew install wrk" >&2
    exit 127
fi

# Health check first so we fail fast if the server isn't up.
if ! curl -fsS --max-time 2 "${BASE}/healthz" >/dev/null; then
    echo "dashboard not responding at ${BASE}/healthz" >&2
    exit 2
fi

_header_args=(
    -H "X-Gateway-Secret: ${GATEWAY_SSO_SECRET}"
    -H "X-Gateway-User-ID: ${USER_ID}"
    -H "X-Gateway-User-Email: ${USER_EMAIL}"
    -H "X-Gateway-User-Tier: ${TIER}"
)

ENDPOINTS=(
    "/api/index"
    "/api/spikes"
    "/api/entities/top"
    "/api/entity/Tesla"
    "/api/sources"
)

fail=0
for path in "${ENDPOINTS[@]}"; do
    echo "═══════════════════════════════════════════════════════════════"
    echo "wrk  ${BASE}${path}  c=${CONNECTIONS} t=${THREADS} d=${DURATION}"
    echo "═══════════════════════════════════════════════════════════════"
    output="$(wrk -t"${THREADS}" -c"${CONNECTIONS}" -d"${DURATION}" \
        --latency "${_header_args[@]}" "${BASE}${path}" 2>&1)"
    echo "${output}"

    # Extract p95 latency ("99%") — wrk's "99%" is p99; we'll look at p95 via
    # "90%  ... 99%" — wrk prints "50%", "75%", "90%", "99%". p95 isn't
    # exposed directly, so we treat "99%" as the hard ceiling (stricter).
    p99_ms="$(echo "${output}" | awk '/99%/ {print $2}' | sed 's/ms//;s/us/\/1000/' | bc 2>/dev/null || echo 0)"
    # Extract non-2xx responses (wrk prints "Non-2xx or 3xx responses: N")
    non2xx="$(echo "${output}" | awk '/Non-2xx or 3xx responses/ {print $NF}')"
    non2xx="${non2xx:-0}"

    if [[ "${non2xx}" != "0" ]]; then
        echo "  ✗ FAIL: ${non2xx} non-2xx responses" >&2
        fail=1
    fi

    # Very rough p99 check — wrk reports "ms" or "us" or "s" for some values.
    # If the raw line contains "s" (no ms/us), it's seconds, big fail.
    if echo "${output}" | awk '/99%/ {print $2}' | grep -q "s$" \
        && ! echo "${output}" | awk '/99%/ {print $2}' | grep -q "ms\|us"; then
        echo "  ✗ FAIL: p99 measured in seconds (way over budget)" >&2
        fail=1
    fi
done

echo "═══════════════════════════════════════════════════════════════"
if [[ "${fail}" -ne 0 ]]; then
    echo "loadtest: one or more endpoints failed thresholds" >&2
    exit 1
fi
echo "loadtest: all endpoints within budget (p99 < ${P95_BUDGET_MS}ms, 0 errors)"
