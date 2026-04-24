#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# install-narve-service.sh — one-shot root-level fix:
#   1. Mask polymarket-gateway.service so it cannot respawn (currently stuck
#      in a Restart=on-failure loop — counter 3700+)
#   2. Install /etc/systemd/system/narve-gateway.service as the sudo-level
#      owner of port 7000
#   3. Start it and verify /health
#
# Run as a user with sudo access:
#     sudo bash ~/Habbig/gateway/scripts/install-narve-service.sh
#
# Or use interactive sudo (prompts for password once):
#     bash ~/Habbig/gateway/scripts/install-narve-service.sh
# ─────────────────────────────────────────────────────────────────────────────

set -u

say() { printf '\n→ %s\n' "$*"; }
ok()  { printf '  ✓ %s\n' "$*"; }
warn(){ printf '  ⚠ %s\n' "$*"; }
bad() { printf '  ✗ %s\n' "$*"; }

USER_NAME="${SUDO_USER:-${USER:-julianhabbig}}"
HOME_DIR="/home/${USER_NAME}"
GATEWAY_DIR="${HOME_DIR}/Habbig/gateway"
ENV_FILE="${HOME_DIR}/.gateway_env"
SERVICE_FILE="/etc/systemd/system/narve-gateway.service"

# ── sanity checks ────────────────────────────────────────────────────────────
if [ ! -d "$GATEWAY_DIR" ]; then
    bad "$GATEWAY_DIR does not exist"
    exit 1
fi
if [ ! -f "$ENV_FILE" ]; then
    bad "$ENV_FILE does not exist — create it with at least GATEWAY_COOKIE_SECRET, SITE_ACCESS_TOKEN, CREDENTIALS_ENCRYPTION_KEY"
    exit 1
fi

# ── secrets hygiene: the env file is loaded by systemd via EnvironmentFile=
# and must be locked down — root-owned, mode 0600 — or any local user can
# read every secret (cookie key, API tokens, Fernet key, DB path, etc.).
say "Enforcing secure perms on $ENV_FILE (root:root 0600)..."
if sudo chown root:root "$ENV_FILE" && sudo chmod 600 "$ENV_FILE"; then
    ok "$ENV_FILE is now root:root 0600"
else
    warn "Could not lock down $ENV_FILE — do this manually: sudo chown root:root $ENV_FILE && sudo chmod 600 $ENV_FILE"
fi

# ── obtain sudo up front (interactive — one password prompt) ────────────────
say "Requesting sudo (you may be prompted for your password)..."
if ! sudo -v; then
    bad "sudo authentication failed"
    exit 1
fi
ok "sudo cached"

# ── 1. stop + disable + mask the polymarket-gateway respawn loop ────────────
say "Stopping polymarket-gateway.service respawn loop..."
sudo systemctl stop polymarket-gateway.service 2>&1 | head -5 || true
sudo systemctl reset-failed polymarket-gateway.service 2>&1 | head -5 || true
sudo systemctl disable polymarket-gateway.service 2>&1 | head -5 || true
sudo systemctl mask polymarket-gateway.service 2>&1 | head -5 || true

MASK_STATE=$(sudo systemctl is-enabled polymarket-gateway.service 2>&1 || true)
if [ "$MASK_STATE" = "masked" ]; then
    ok "polymarket-gateway.service masked (cannot start)"
else
    warn "polymarket-gateway.service state: $MASK_STATE (expected 'masked')"
fi

# Verify the restart loop has actually stopped
sleep 2
ACTIVE_STATE=$(sudo systemctl is-active polymarket-gateway.service 2>&1 || true)
if [ "$ACTIVE_STATE" = "inactive" ] || [ "$ACTIVE_STATE" = "failed" ] || [ "$ACTIVE_STATE" = "masked" ]; then
    ok "polymarket-gateway.service no longer activating"
else
    warn "polymarket-gateway.service is still $ACTIVE_STATE — might respawn; masking did not take full effect"
fi

# ── 2. write narve-gateway.service ───────────────────────────────────────────
say "Writing $SERVICE_FILE..."
sudo tee "$SERVICE_FILE" > /dev/null <<EOF
[Unit]
Description=narve.ai gateway (FastAPI on port 7000)
After=network-online.target
Wants=network-online.target
Conflicts=polymarket-gateway.service

[Service]
Type=simple
User=${USER_NAME}
Group=${USER_NAME}
WorkingDirectory=${GATEWAY_DIR}
# Any file the uvicorn process creates (auth.db-wal/-shm, /tmp dumps,
# export ZIPs) gets 0600 instead of the default 0644. AUDIT #5 HIGH #2
# flagged auth.db as world-readable on disk; UMask at the unit level
# means new files are born restrictive — no need to chmod after the fact.
UMask=0077
# All secrets MUST come from the EnvironmentFile below — never add
# Environment=KEY=secret lines here (they leak via `systemctl show`,
# journalctl, and the unit file itself which is world-readable).
# The env file must be root:root 0600.
EnvironmentFile=${ENV_FILE}
# Non-secret toggles only — safe to pin inline.
Environment=PRODUCTION=1
Environment=PYTHONUNBUFFERED=1
ExecStartPre=/bin/bash -c '/usr/bin/fuser -k 7000/tcp 2>/dev/null || true; sleep 1'
# Tighten perms on the DB + env file on every start. Idempotent chmods
# so a file created with an older umask gets corrected without a manual
# runbook step. -f swallows errors when auth.db-wal/-shm don't exist
# yet (first boot before any write).
ExecStartPre=/bin/bash -c 'chmod 600 ${GATEWAY_DIR}/auth.db ${GATEWAY_DIR}/auth.db-wal ${GATEWAY_DIR}/auth.db-shm ${ENV_FILE} 2>/dev/null || true'
ExecStart=/usr/bin/python3 -m uvicorn server:app --host 127.0.0.1 --port 7000
Restart=on-failure
RestartSec=5
# Hard limit on restart storms — max 5 restarts per 60s. If we hit the limit
# the unit enters 'failed' and stays there instead of burning CPU forever.
StartLimitIntervalSec=60
StartLimitBurst=5
StandardOutput=append:/tmp/gateway.log
StandardError=append:/tmp/gateway.log

[Install]
WantedBy=multi-user.target
EOF

if [ -f "$SERVICE_FILE" ]; then
    ok "$SERVICE_FILE written ($(wc -l < "$SERVICE_FILE") lines)"
else
    bad "failed to write $SERVICE_FILE"
    exit 1
fi

# ── 3. enable + start ────────────────────────────────────────────────────────
say "Reloading systemd + enabling narve-gateway.service..."
sudo systemctl daemon-reload
sudo systemctl enable narve-gateway.service 2>&1 | head -3 || true

say "Killing any stray process on port 7000..."
sudo fuser -k 7000/tcp 2>/dev/null || true
sleep 2

say "Starting narve-gateway.service..."
sudo systemctl restart narve-gateway.service
sleep 5

# ── 4. verify ────────────────────────────────────────────────────────────────
say "Verifying..."
if sudo systemctl is-active narve-gateway.service >/dev/null 2>&1; then
    ok "narve-gateway.service active"
else
    bad "narve-gateway.service failed to start — journalctl tail:"
    sudo journalctl -u narve-gateway.service --no-pager -n 30
    exit 1
fi

if curl -sf http://127.0.0.1:7000/health >/dev/null; then
    ok "/health returning 200"
else
    bad "/health not responding"
    exit 1
fi

# Also verify the correct process (cwd must be Habbig, not Polymarket)
PID=$(ss -ltnp "sport = :7000" 2>/dev/null | awk 'NR>1 {print $6}' | grep -oE 'pid=[0-9]+' | head -1 | cut -d= -f2)
if [ -n "$PID" ]; then
    CWD=$(readlink "/proc/${PID}/cwd" 2>/dev/null || echo "")
    case "$CWD" in
        *Habbig*) ok "port 7000 owned by Habbig gateway (pid=$PID)" ;;
        *) bad "port 7000 owned by WRONG process: pid=$PID cwd=$CWD"; exit 1 ;;
    esac
fi

echo ""
echo "────────────────────────────────────────────────────────────"
echo "Done."
echo ""
echo "  • narve-gateway.service owns port 7000 (systemd-managed)"
echo "  • polymarket-gateway.service is masked (cannot respawn)"
echo "  • On reboot, narve-gateway will start automatically"
echo "  • crontab watchdog at ~/Habbig/gateway/scripts/narve-watchdog.sh"
echo "    remains as a belt-and-braces layer"
echo ""
echo "  Status:  sudo systemctl status narve-gateway"
echo "  Logs:    sudo journalctl -u narve-gateway -f"
echo "  Stop:    sudo systemctl stop narve-gateway"
echo "────────────────────────────────────────────────────────────"
