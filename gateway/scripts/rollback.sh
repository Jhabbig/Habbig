#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# rollback.sh — revert the production gateway to a previous commit
#
# The server's git working tree is the source of truth (the memory explains:
# "MUST commit on server after deploy — git repo has old code committed, any
# git op reverts changes otherwise"). This script checks out the target
# commit ON THE SERVER and restarts uvicorn, it does NOT scp from the laptop.
#
# Usage:
#   bash scripts/rollback.sh                 # interactive — picks from git log
#   bash scripts/rollback.sh <commit_hash>   # direct rollback to given commit
#
# Environment overrides: same as deploy-production.sh
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

PROD_HOST="${PROD_HOST:-100.69.44.108}"
PROD_USER="${PROD_USER:-julianhabbig}"
PROD_PATH="${PROD_PATH:-~/Habbig/gateway}"
PROD_PORT="${PROD_PORT:-7000}"
PROD_URL="${PROD_URL:-https://narve.ai}"

SSH_TARGET="${PROD_USER}@${PROD_HOST}"
TARGET_COMMIT="${1:-}"

echo "→ rollback.sh"
echo "  host: $PROD_HOST"
echo "  path: $PROD_PATH"
echo ""

# 1. If no commit given, show the last 10 and prompt
if [ -z "$TARGET_COMMIT" ]; then
    echo "→ Recent commits on server:"
    ssh "$SSH_TARGET" "cd ${PROD_PATH} && git log --oneline -10"
    echo ""
    echo "Enter commit hash to roll back to (or Ctrl+C to abort):"
    read -r TARGET_COMMIT
fi

if [ -z "$TARGET_COMMIT" ]; then
    echo "Aborted — no commit specified."
    exit 0
fi

# 2. Confirm
echo ""
echo "⚠️  About to roll back production to commit: $TARGET_COMMIT"
echo "   Type 'rollback' to confirm:"
read -r CONFIRM
if [ "$CONFIRM" != "rollback" ]; then
    echo "Aborted."
    exit 0
fi

# 3. Checkout target commit on server and restart
echo "→ Rolling back on server..."
ssh "$SSH_TARGET" "
    set -e
    cd ${PROD_PATH}

    # Verify the commit exists before checking out
    if ! git rev-parse --verify ${TARGET_COMMIT}^{commit} >/dev/null 2>&1; then
        echo '✗ commit ${TARGET_COMMIT} not found on server'
        exit 1
    fi

    # Stash any uncommitted changes first (should not exist after deploy,
    # but better safe than sorry)
    git stash push --include-untracked --quiet 2>/dev/null || true

    git checkout ${TARGET_COMMIT}

    # Restart uvicorn
    fuser -k ${PROD_PORT}/tcp 2>/dev/null || true
    sleep 2

    if [ ! -f ~/.gateway_env ]; then
        echo '✗ ~/.gateway_env missing'
        exit 1
    fi

    set -a
    . ~/.gateway_env
    set +a

    nohup env PRODUCTION=1 python3 -m uvicorn server:app \\
        --host 127.0.0.1 \\
        --port ${PROD_PORT} \\
        > /tmp/gateway.log 2>&1 &
    sleep 4
    PID=\$(pgrep -f 'uvicorn server:app.*${PROD_PORT}' || true)
    echo \"  ✓ PID: \$PID\"
"

# 4. Verify
echo "→ Verifying ${PROD_URL}/health..."
sleep 2
if curl -sf -o /tmp/rollback_health.json -w '%{http_code}\n' "${PROD_URL}/health" | grep -q '^200$'; then
    echo "  ✓ rollback successful"
    python3 -m json.tool /tmp/rollback_health.json 2>/dev/null || cat /tmp/rollback_health.json
    rm -f /tmp/rollback_health.json
else
    echo "  ✗ rollback did not bring health back to 200"
    echo "  manual intervention required — ssh in and check /tmp/gateway.log"
    exit 1
fi

echo ""
echo "✓ rolled back to $TARGET_COMMIT"
