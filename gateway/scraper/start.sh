#!/bin/bash
# Start the scraper service
cd "$(dirname "$0")/.."

if [ -f scraper/.env ]; then
  set -a
  source scraper/.env
  set +a
fi

SCRAPER_HOST="${SCRAPER_HOST:-127.0.0.1}"
SCRAPER_PORT="${SCRAPER_PORT:-8001}"

echo "Starting scraper on ${SCRAPER_HOST}:${SCRAPER_PORT}..."
nohup python3 -m uvicorn scraper.main:app --host "$SCRAPER_HOST" --port "$SCRAPER_PORT" --workers 1 > scraper/logs/scraper.log 2>&1 &
echo $! > scraper/scraper.pid
echo "Scraper started (PID: $(cat scraper/scraper.pid))"
