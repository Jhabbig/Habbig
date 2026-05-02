#!/usr/bin/env bash
# ─────────────────────────────────────────────────
# deploy.sh — Push updates to Ubuntu server
# ─────────────────────────────────────────────────
# Automatically snapshots the server state before syncing.
#
# Usage:
#   ./deploy.sh                  Deploy all sites
#   ./deploy.sh gateway          Deploy just one site
#   ./deploy.sh -m "new CSS"     Deploy all with a snapshot note

set -euo pipefail

SERVER="${DEPLOY_SERVER:?Set DEPLOY_SERVER env var (e.g. user@host)}"
REMOTE_DIR="~/Polymarket"
LOCAL_DIR="$(cd "$(dirname "$0")" && pwd)"

# Sites that get deployed
SITES=(
    gateway
    crypto-dashboard
    stock-dashboard
    sports-dashboard
    polymarket_weather_dashboard
    world-state-dashboard
    midterm-dashboard
    Dashboard-x-truth-research-prediction
    polymarket-bot
    polymarket_weather_bot
    top-traders-dashboard
    whale-dashboard
)

# Excluded from rsync
EXCLUDES=(
    "__pycache__"
    "*.pyc"
    ".DS_Store"
    "node_modules"
    "venv"
    ".venv"
    ".git"
    ".snapshots"
    "*.log"
    ".env"
    "*.db"
    "*.db-wal"
    "*.db-shm"
)

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

# ── Parse args ───────────────────────────────────

site=""
message=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        -m|--message)
            if [ -z "${2:-}" ]; then echo "Error: -m requires a message"; exit 1; fi
            message="$2"; shift 2 ;;
        -h|--help)
            echo ""
            echo -e "${BOLD}deploy.sh${NC} — Push updates to Ubuntu server"
            echo ""
            echo "  ./deploy.sh                  Deploy all sites"
            echo "  ./deploy.sh gateway          Deploy just one site"
            echo "  ./deploy.sh -m \"note\"         Deploy with a snapshot note"
            echo ""
            exit 0
            ;;
        *) site="$1"; shift ;;
    esac
done

# Validate site if specified
if [[ -n "$site" ]]; then
    found=false
    for s in "${SITES[@]}"; do
        [[ "$s" == "$site" ]] && found=true && break
    done
    if ! $found; then
        echo -e "${RED}Error:${NC} Unknown site '$site'"
        echo "Available: ${SITES[*]}"
        exit 1
    fi
fi

# ── Build rsync exclude args ────────────────────

exclude_args=()
for pat in "${EXCLUDES[@]}"; do
    exclude_args+=(--exclude="$pat")
done

# ── Step 1: Snapshot on server ───────────────────

echo -e "${CYAN}[1/3] Snapshotting current server state...${NC}"

snapshot_msg="${message:-pre-deploy}"
if [[ -n "$site" ]]; then
    ssh "$SERVER" "cd $REMOTE_DIR && ./snapshot.sh save $(printf '%q' "$site") -m $(printf '%q' "$snapshot_msg")"
else
    ssh "$SERVER" "cd $REMOTE_DIR && ./snapshot.sh save -m $(printf '%q' "$snapshot_msg")"
fi

# ── Step 2: Sync files ──────────────────────────

echo ""
echo -e "${CYAN}[2/3] Syncing files to server...${NC}"

if [[ -n "$site" ]]; then
    echo -e "  Deploying ${BOLD}$site${NC}"
    rsync -avz --delete "${exclude_args[@]}" \
        "$LOCAL_DIR/$site/" "$SERVER:$REMOTE_DIR/$site/"
else
    for s in "${SITES[@]}"; do
        [[ -d "$LOCAL_DIR/$s" ]] || continue
        echo -e "  Deploying ${BOLD}$s${NC}"
        rsync -avz --delete "${exclude_args[@]}" \
            "$LOCAL_DIR/$s/" "$SERVER:$REMOTE_DIR/$s/"
    done
fi

# Also sync the snapshot script itself
rsync -avz "$LOCAL_DIR/snapshot.sh" "$SERVER:$REMOTE_DIR/snapshot.sh"

# ── Step 3: Done ─────────────────────────────────

echo ""
echo -e "${GREEN}[3/3] Deploy complete!${NC}"
echo -e "  ${YELLOW}Note:${NC} You may need to restart services on the server."
echo -e "  To revert: ssh $SERVER \"cd $REMOTE_DIR && ./snapshot.sh restore <id>\""
