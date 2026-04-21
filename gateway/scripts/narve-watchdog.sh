#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# narve-watchdog.sh — user-crontab watchdog for the Habbig/narve.ai gateway
#
# Runs once per minute. Ensures the process on port 7000 is the Habbig gateway
# and NOT the Polymarket gateway (which has a habit of being manually started
# or kept alive by polymarket-gateway.service's infinite restart loop).
#
# Detection logic:
#   1. Find the PID holding port 7000 (via /proc/net/tcp)
#   2. Read /proc/$PID/cwd to check the working directory
#   3. If cwd does NOT contain "Habbig", kill it and restart Habbig uvicorn
#   4. If nothing is on port 7000, start Habbig uvicorn
#
# Install as user cron (no sudo needed):
#   crontab -e
#   * * * * * /home/julianhabbig/Habbig/gateway/scripts/narve-watchdog.sh >> /tmp/narve-watchdog.log 2>&1
#
# This does NOT fix polymarket-gateway.service's restart loop — that needs
# root: sudo systemctl stop polymarket-gateway.service && sudo systemctl mask
# polymarket-gateway.service — but it will re-claim the port faster than the
# Polymarket service can steal it.
# ─────────────────────────────────────────────────────────────────────────────

set -u

HABBIG_DIR="/home/julianhabbig/Habbig/gateway"
PORT=7000
ENV_FILE="/home/julianhabbig/.gateway_env"
LOG_FILE="/tmp/gateway.log"

# ── crash-loop backoff ──────────────────────────────────────────────────
# Track how many restarts we've done inside a rolling window. If the
# gateway has restarted more than MAX_RESTARTS within WINDOW_SEC, back
# off for BACKOFF_SEC before attempting again, so we don't hammer a
# broken config 60 times an hour.
STATE_FILE="/tmp/narve-watchdog.state"
MAX_RESTARTS=5
WINDOW_SEC=60
BACKOFF_SEC=300

log() {
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*"
}

find_pid_on_port() {
    # Fast path: ss (available by default on Ubuntu)
    local pid
    pid=$(ss -ltnp "sport = :${PORT}" 2>/dev/null | awk 'NR>1 {print $6}' | grep -oE 'pid=[0-9]+' | head -1 | cut -d= -f2)
    if [ -n "$pid" ]; then
        echo "$pid"
        return 0
    fi
    # Fallback: fuser
    fuser "${PORT}/tcp" 2>/dev/null | tr -s ' ' '\n' | grep -E '^[0-9]+$' | head -1
}

is_habbig_pid() {
    local pid="$1"
    local cwd
    cwd=$(readlink "/proc/${pid}/cwd" 2>/dev/null || echo "")
    case "$cwd" in
        *Habbig*) return 0 ;;
        *) return 1 ;;
    esac
}

should_backoff() {
    # Returns 0 (true) if we should skip starting due to recent crash loop.
    # Prune timestamps older than WINDOW_SEC; if >=MAX_RESTARTS remain and
    # a backoff marker is fresh, skip.
    local now
    now=$(date +%s)
    local pruned=""
    if [ -f "$STATE_FILE" ]; then
        while IFS= read -r ts; do
            [ -z "$ts" ] && continue
            if [ "$((now - ts))" -lt "$WINDOW_SEC" ]; then
                pruned="${pruned}${ts}"$'\n'
            fi
        done < "$STATE_FILE"
    fi
    # Write back pruned state.
    printf '%s' "$pruned" > "$STATE_FILE"

    local count
    count=$(printf '%s' "$pruned" | grep -c '.' || true)
    if [ "$count" -ge "$MAX_RESTARTS" ]; then
        # Have we already slept past the backoff for this storm?
        local last
        last=$(printf '%s' "$pruned" | tail -1)
        if [ -n "$last" ] && [ "$((now - last))" -lt "$BACKOFF_SEC" ]; then
            log "crash-loop detected: ${count} restarts in ${WINDOW_SEC}s — backing off until $((BACKOFF_SEC - (now - last)))s from now"
            return 0
        fi
    fi
    return 1
}

record_restart() {
    date +%s >> "$STATE_FILE"
}

start_habbig() {
    if should_backoff; then
        return 1
    fi
    record_restart
    log "starting Habbig gateway on port ${PORT}"
    cd "$HABBIG_DIR" || { log "FATAL: cannot cd to $HABBIG_DIR"; return 1; }

    if [ -f "$ENV_FILE" ]; then
        set -a
        # shellcheck disable=SC1090
        . "$ENV_FILE"
        set +a
    else
        log "WARN: $ENV_FILE missing"
    fi

    nohup env PRODUCTION=1 python3 -m uvicorn server:app \
        --host 127.0.0.1 \
        --port "$PORT" \
        >> "$LOG_FILE" 2>&1 &
    disown 2>/dev/null || true
    sleep 3
    local new_pid
    new_pid=$(find_pid_on_port)
    if [ -n "$new_pid" ] && is_habbig_pid "$new_pid"; then
        log "Habbig uvicorn started, pid=$new_pid"
        return 0
    fi
    log "ERROR: Habbig failed to start, tail of log:"
    tail -15 "$LOG_FILE" 2>/dev/null || true
    return 1
}

# Main
PID=$(find_pid_on_port)

if [ -z "$PID" ]; then
    log "port $PORT is free — starting Habbig"
    start_habbig
    exit $?
fi

if is_habbig_pid "$PID"; then
    # Correct process is running — nothing to do. Stay quiet in the log.
    exit 0
fi

CWD=$(readlink "/proc/${PID}/cwd" 2>/dev/null || echo "unknown")
log "port $PORT is held by pid=$PID cwd=$CWD (NOT Habbig) — killing and restarting"

# First, try graceful kill, then escalate
kill "$PID" 2>/dev/null || true
sleep 2
# Also nuke anything still holding the port
fuser -k "${PORT}/tcp" 2>/dev/null || true
sleep 2

start_habbig
