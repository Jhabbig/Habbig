#!/bin/bash
# Stop the scraper service
cd "$(dirname "$0")"

if [ -f scraper.pid ]; then
  PID=$(cat scraper.pid)
  if kill -0 "$PID" 2>/dev/null; then
    kill "$PID"
    echo "Scraper stopped (PID: $PID)"
  else
    echo "Scraper process $PID not running"
  fi
  rm -f scraper.pid
else
  echo "No scraper.pid file found"
fi
