#!/usr/bin/env bash
# Start the annoyance dashboard in the background (gateway-style pattern).
#
# Reads env from ~/.annoyance_env (prod) or ~/.annoyance_env_staging
# depending on $ENV_FILE. Matches how the other sibling dashboards are
# launched on the server — `nohup ... > /tmp/annoyance.log 2>&1 &`.
#
# Usage:
#   ./scripts/start.sh               # prod (port 8053, ~/.annoyance_env)
#   ENV_FILE=~/.annoyance_env_staging PORT=8054 ./scripts/start.sh

set -euo pipefail

DASH_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$DASH_DIR"

ENV_FILE="${ENV_FILE:-$HOME/.annoyance_env}"
PORT="${PORT:-8053}"
LOG_FILE="${LOG_FILE:-/tmp/annoyance.log}"

if [[ ! -f "$ENV_FILE" ]]; then
    echo "start.sh: env file not found: $ENV_FILE" >&2
    exit 1
fi

# Activate virtualenv if present (no-op if already sourced).
if [[ -f "$DASH_DIR/.venv/bin/activate" ]]; then
    # shellcheck disable=SC1091
    source "$DASH_DIR/.venv/bin/activate"
fi

# Refuse to double-boot — if the port is already occupied, abort.
if command -v fuser >/dev/null 2>&1; then
    if fuser "${PORT}/tcp" >/dev/null 2>&1; then
        echo "start.sh: port ${PORT} already in use. Run stop.sh first." >&2
        exit 2
    fi
fi

# shellcheck disable=SC2046
nohup env PORT="$PORT" $(grep -v '^\s*#' "$ENV_FILE" | xargs) \
    python3 server.py > "$LOG_FILE" 2>&1 &

PID=$!
sleep 2

if kill -0 "$PID" 2>/dev/null; then
    echo "annoyance dashboard started pid=$PID port=$PORT log=$LOG_FILE"
    exit 0
else
    echo "start.sh: server.py exited immediately. Tail of log:" >&2
    tail -n 40 "$LOG_FILE" >&2
    exit 3
fi
