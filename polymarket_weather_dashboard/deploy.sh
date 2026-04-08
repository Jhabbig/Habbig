#!/bin/bash
# ═════════════════════════════════���═════════════════════════════
# NoRain Deploy Script
# Run this from your Mac to push updates to the Ubuntu server
# Usage: ./deploy.sh
# ══════════════��══════════���═════════════════════════════════════

SERVER="100.69.44.108"
REMOTE_DIR="~/polymarket_weather_dashboard"
LOCAL_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "=== NoRain Deploy ==="
echo "From: $LOCAL_DIR"
echo "To:   $SERVER:$REMOTE_DIR"
echo ""

# Step 1: Sync files
echo "[1/3] Syncing files..."
rsync -avz --delete \
  --exclude 'venv/' \
  --exclude '__pycache__/' \
  --exclude 'history.db*' \
  --exclude 'data.db*' \
  --exclude '*.log' \
  --exclude '.git/' \
  "$LOCAL_DIR/" "$SERVER:$REMOTE_DIR/"

if [ $? -ne 0 ]; then
    echo "ERROR: rsync failed"
    exit 1
fi

# Step 2: Commit on server
echo ""
echo "[2/3] Committing on server..."
ssh "$SERVER" "cd $REMOTE_DIR && git add -A && git commit -m 'Deploy $(date +%Y-%m-%d_%H:%M)' 2>/dev/null || echo 'Nothing new to commit'"

# Step 3: Restart service
echo ""
echo "[3/3] Restarting NoRain service..."
ssh "$SERVER" "sudo -n systemctl restart norain 2>/dev/null"
sleep 3

# Verify
echo ""
echo "=== Checking status ==="
ssh "$SERVER" "sudo -n systemctl is-active norain 2>/dev/null"
ssh "$SERVER" "curl -s http://localhost:5050/api/markets 2>/dev/null | python3 -c \"import json,sys; d=json.load(sys.stdin); print('Markets loaded:', d['count'])\" 2>/dev/null || echo 'API not ready yet (may need a few seconds)'"

echo ""
echo "=== Deploy complete ==="
echo "Public URL: https://julianhabbig-legion-slim-5-14aph8.tail85a41a.ts.net/"
echo "Admin:      https://julianhabbig-legion-slim-5-14aph8.tail85a41a.ts.net/admin"
