#!/bin/bash
set -e

# MidtermEdge Deployment Script
# Deploys to Linux server, sets up Python venv, builds frontend, starts services

PROJECT_DIR="$HOME/midterm-dashboard"
BACKEND_DIR="$PROJECT_DIR/backend"
FRONTEND_DIR="$PROJECT_DIR/frontend"

echo "=== MidtermEdge Deployment ==="

# Ensure we're in the project directory
cd "$PROJECT_DIR"

# --- Python Backend Setup ---
echo "[1/5] Setting up Python environment..."
if [ ! -d "$BACKEND_DIR/venv" ]; then
    python3 -m venv "$BACKEND_DIR/venv"
fi
source "$BACKEND_DIR/venv/bin/activate"
pip install --quiet --upgrade pip
pip install --quiet -r "$BACKEND_DIR/requirements.txt"

# --- Frontend Build ---
echo "[2/5] Building frontend..."
cd "$FRONTEND_DIR"
if [ ! -d "node_modules" ]; then
    npm install
fi
npm run build
cd "$PROJECT_DIR"

# --- Create admin user if not exists ---
echo "[3/5] Initializing database..."
python3 -c "
import asyncio
import sys
sys.path.insert(0, '$BACKEND_DIR')
from database import Database

async def init():
    db = Database('$BACKEND_DIR/midterm_dashboard.db')
    await db.connect()
    # Create admin user if not exists
    existing = await db.get_user_by_email('admin@midtermedge.com')
    if not existing:
        uid = await db.create_user('admin@midtermedge.com', 'changeme123!', 'Admin')
        if uid:
            await db.update_user_tier(uid, 'admin')
            print('  Created admin user: admin@midtermedge.com / changeme123!')
    else:
        print('  Admin user already exists')
    await db.close()

asyncio.run(init())
"

# --- Systemd Service ---
echo "[4/5] Setting up systemd service..."
SYSTEMD_DIR="$HOME/.config/systemd/user"
mkdir -p "$SYSTEMD_DIR"

cat > "$SYSTEMD_DIR/midtermedge.service" << EOF
[Unit]
Description=MidtermEdge Dashboard Server
After=network.target

[Service]
Type=simple
WorkingDirectory=$BACKEND_DIR
ExecStart=$BACKEND_DIR/venv/bin/python main.py
Restart=on-failure
RestartSec=5
Environment=PORT=8050

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
systemctl --user enable midtermedge.service
systemctl --user restart midtermedge.service

echo "[5/5] Service started!"
echo ""
echo "=== Deployment Complete ==="
echo "  Dashboard: http://$(hostname -I | awk '{print $1}'):8050"
echo "  Tailscale: http://$(tailscale ip -4 2>/dev/null || echo 'N/A'):8050"
echo "  Admin:     admin@midtermedge.com / changeme123! (CHANGE THIS)"
echo ""
echo "  Manage service:"
echo "    systemctl --user status midtermedge"
echo "    systemctl --user restart midtermedge"
echo "    journalctl --user -u midtermedge -f"
