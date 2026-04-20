#!/usr/bin/env bash
# Stop the annoyance dashboard by freeing its port. Matches the rest of
# the dashboards' deploy pattern — `fuser -k <port>/tcp` + sleep.
#
# Usage:
#   ./scripts/stop.sh                # prod (port 8053)
#   PORT=8054 ./scripts/stop.sh      # staging

set -euo pipefail

PORT="${PORT:-8053}"

if ! command -v fuser >/dev/null 2>&1; then
    echo "stop.sh: fuser not installed. Install psmisc (apt) or findutils." >&2
    exit 2
fi

if ! fuser "${PORT}/tcp" >/dev/null 2>&1; then
    echo "stop.sh: nothing listening on port ${PORT}"
    exit 0
fi

fuser -k "${PORT}/tcp"
sleep 2

if fuser "${PORT}/tcp" >/dev/null 2>&1; then
    echo "stop.sh: process on port ${PORT} survived SIGTERM — retrying with SIGKILL" >&2
    fuser -k -KILL "${PORT}/tcp" || true
    sleep 1
fi

if fuser "${PORT}/tcp" >/dev/null 2>&1; then
    echo "stop.sh: port ${PORT} still occupied. Manual intervention required." >&2
    exit 3
fi

echo "annoyance dashboard stopped (port ${PORT} freed)"
