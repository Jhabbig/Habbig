#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# whale-watchdog.sh — user-cron watchdog for whale-dashboard
#
# Runs once per minute. Ensures a process is listening on port 8053; if not,
# starts one with the right env vars from ~/.gateway_env (the same file the
# Habbig gateway reads). Mirrors the pattern in
# Habbig/gateway/scripts/narve-watchdog.sh — same crash-loop backoff logic,
# different port and different binary.
#
# Install on prod (no sudo needed):
#   crontab -e
#   * * * * * /home/julianhabbig/Polymarket/whale-dashboard/scripts/whale-watchdog.sh \
#       >> /tmp/whale-watchdog.log 2>&1
# ─────────────────────────────────────────────────────────────────────────────

set -u

PORT=8053
PYTHON="/home/julianhabbig/Polymarket/venv/bin/python"
ENTRY="/home/julianhabbig/Polymarket/whale-dashboard/backend/main.py"
ENV_FILE="/home/julianhabbig/.gateway_env"
LOG_FILE="/tmp/dashboard_whale.log"
PID_FILE="/tmp/dashboard_whale.pid"

STATE_FILE="/tmp/whale-watchdog.state"
MAX_RESTARTS=5
WINDOW_SEC=60
BACKOFF_SEC=300

log() { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*"; }

find_pid_on_port() {
    ss -ltnp "sport = :${PORT}" 2>/dev/null \
        | awk 'NR>1 {print $6}' \
        | grep -oE 'pid=[0-9]+' | head -1 | cut -d= -f2
}

check_backoff() {
    local now ts count
    now=$(date +%s)
    if [ -f "$STATE_FILE" ]; then
        # Format: "<unix_ts> <restart_count_in_window>"
        read -r ts count < "$STATE_FILE"
        # Discard restart counter if last restart was outside our window
        if [ $((now - ts)) -gt "$WINDOW_SEC" ]; then
            count=0
        fi
        if [ "$count" -ge "$MAX_RESTARTS" ] && [ $((now - ts)) -lt "$BACKOFF_SEC" ]; then
            log "in backoff: $count restarts in window, waiting another $((BACKOFF_SEC - (now - ts)))s"
            return 1
        fi
    fi
    return 0
}

record_restart() {
    local now count
    now=$(date +%s)
    if [ -f "$STATE_FILE" ]; then
        local ts old_count
        read -r ts old_count < "$STATE_FILE"
        if [ $((now - ts)) -gt "$WINDOW_SEC" ]; then
            count=1
        else
            count=$((old_count + 1))
        fi
    else
        count=1
    fi
    echo "$now $count" > "$STATE_FILE"
}

start_whale() {
    log "starting whale on port $PORT"
    cd /home/julianhabbig/Polymarket/whale-dashboard/backend
    # Load env file using a python helper so unquoted values with spaces
    # (e.g. SEC_USER_AGENT=WhaleDashboard ops@narve.ai) survive sourcing.
    # ENV_FILE is passed via env so we don't have to escape it through the
    # nested heredoc layers.
    eval "$(ENV_FILE="$ENV_FILE" python3 -c '
import os, shlex
for line in open(os.environ["ENV_FILE"]):
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, _, v = line.partition("=")
        print(f"export {k}={shlex.quote(v)}")
')"
    PORT="$PORT" PYTHONUNBUFFERED=1 nohup "$PYTHON" "$ENTRY" \
        >> "$LOG_FILE" 2>&1 &
    sleep 3
    local pid
    pid=$(find_pid_on_port)
    if [ -n "$pid" ]; then
        echo "$pid" > "$PID_FILE"
        log "whale started, pid=$pid"
    else
        log "whale FAILED to start — see $LOG_FILE"
    fi
}

# ── main ───────────────────────────────────────────────────────────────
PID=$(find_pid_on_port)
if [ -n "$PID" ]; then
    # Sanity check the process is actually whale-dashboard (cwd contains it).
    if readlink "/proc/$PID/cwd" 2>/dev/null | grep -q "whale-dashboard"; then
        # Healthy — nothing to do.
        exit 0
    fi
    log "port $PORT held by pid=$PID but cwd doesn't look like whale-dashboard — leaving it alone"
    exit 0
fi

# Port is free → whale isn't running.
if check_backoff; then
    record_restart
    start_whale
fi
