#!/bin/bash
# Standalone local-mode launcher — runs the culture dashboard on
# localhost:7070 with no gateway / SSO / enquire flow. All data persists
# in culture.db in this directory.
#
# Usage:
#   ./run.sh                # bind to port 7070
#   PORT=8888 ./run.sh      # custom port
#
# First run sets up a virtualenv and installs requirements automatically.
# Optional env vars (see .env.example for the full set):
#   ANTHROPIC_API_KEY     enable the daily LLM digest
#   APIFY_TOKEN / etc.    enable paid scraping backends for TT / IG / X
#   YOUTUBE_API_KEY       enable YouTube trending
#   NYT_BOOKS_API_KEY     better NYT bestsellers

set -e
cd "$(dirname "$0")"

if [ ! -d .venv ]; then
  echo "Creating virtualenv…"
  python3 -m venv .venv
  .venv/bin/pip install --upgrade pip setuptools wheel
  .venv/bin/pip install -r requirements.txt
fi

# Source .env if present so the user can stash secrets out-of-tree.
if [ -f .env ]; then
  set -a; . ./.env; set +a
fi

PORT="${PORT:-7070}"
echo "Culture dashboard → http://localhost:$PORT"
DEV_MODE=1 .venv/bin/python -m uvicorn server:app --host 127.0.0.1 --port "$PORT"
