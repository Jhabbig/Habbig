#!/usr/bin/env bash
# Start the World State dashboard locally with gateway auth bypassed.
# Usage: ./run_local.sh
set -euo pipefail

cd "$(dirname "$0")"

# Prefer the repo-root venv (has fastapi/uvicorn/defusedxml/httpx pre-installed),
# fall back to a local venv, then to system python3.
ROOT_VENV="../venv/bin/python3"
LOCAL_VENV="venv/bin/python3"
if [[ -x "$ROOT_VENV" ]]; then
  PY="$ROOT_VENV"
elif [[ -x "$LOCAL_VENV" ]]; then
  PY="$LOCAL_VENV"
else
  PY="$(command -v python3)"
fi

# Verify deps are importable; bail with a helpful hint if not.
if ! "$PY" -c "import fastapi, uvicorn, defusedxml, httpx" 2>/dev/null; then
  echo "Missing deps. Install them with:" >&2
  echo "  $PY -m pip install -r requirements.txt httpx" >&2
  exit 1
fi

PORT="${PORT:-7050}"
# Bind to 0.0.0.0 so it's reachable from any browser on this machine
# (and any device on the local network). Override with HOST=127.0.0.1 to lock down.
HOST="${HOST:-0.0.0.0}"
URL="http://localhost:${PORT}"

# If something is already on this port, fail loud rather than silently colliding.
if lsof -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "Port $PORT is already in use. Either close it, or set PORT=xxxx ./run_local.sh." >&2
  exit 1
fi

# Open the browser ~1.5s after launch (best-effort; macOS-only via `open`).
if command -v open >/dev/null 2>&1; then
  ( sleep 1.5 && open "$URL" ) &
fi

export DEV_MODE=1
echo "→ World State dashboard: $URL  (Ctrl-C to stop)"
exec "$PY" -m uvicorn server:app --host "$HOST" --port "$PORT" "$@"
