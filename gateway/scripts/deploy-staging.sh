#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# deploy-staging.sh — deploy the gateway to staging.narve.ai (port 7001)
#
# Runs the same scp+nohup flow as production but targets the staging slot:
#   - uvicorn on port 7001
#   - loads ~/.gateway_env_staging
#   - writes to auth-staging.db
#   - logs to /tmp/gateway_staging.log
#
# Usage:
#   bash scripts/deploy-staging.sh
#
# Environment overrides:
#   STAGING_HOST    - Tailscale IP or hostname (default: 100.69.44.108)
#   STAGING_USER    - SSH user (default: julianhabbig)
#   STAGING_PATH    - remote project dir (default: ~/Habbig/gateway)
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

STAGING_HOST="${STAGING_HOST:-100.69.44.108}"
STAGING_USER="${STAGING_USER:-julianhabbig}"
STAGING_PATH="${STAGING_PATH:-~/Habbig/gateway}"
STAGING_PORT="${STAGING_PORT:-7001}"
STAGING_URL="${STAGING_URL:-https://staging.narve.ai}"

SSH_TARGET="${STAGING_USER}@${STAGING_HOST}"

cd "$(dirname "$0")/.."

echo "→ deploy-staging.sh"
echo "  host: $STAGING_HOST"
echo "  path: $STAGING_PATH"
echo "  port: $STAGING_PORT"
echo "  url:  $STAGING_URL"
echo ""

# 1. Syntax check everything Python before we ship bad code
echo "→ Syntax check..."
python3 -c "import ast; ast.parse(open('server.py').read())" || { echo "✗ server.py syntax error"; exit 1; }
python3 -c "import ast; ast.parse(open('db.py').read())" || { echo "✗ db.py syntax error"; exit 1; }
for f in backend/markets/*.py; do
    python3 -c "import ast; ast.parse(open('$f').read())" || { echo "✗ $f syntax error"; exit 1; }
done
echo "  ✓ syntax OK"

# 2. Run unit tests — abort on any failure
if command -v pytest >/dev/null 2>&1; then
    echo "→ Running tests..."
    python3 -m pytest tests/ -x -q --tb=line || { echo "✗ tests failed — deployment aborted"; exit 1; }
    echo "  ✓ tests passed"
else
    echo "→ pytest not installed locally, skipping tests"
fi

# 3. scp every file that matters. We do this per-file because the memory
#    warns: "Never use rsync with multiple source args (puts files in wrong
#    dirs)". scp -r on selected dirs is safe. The list MUST stay in sync
#    with deploy-production.sh — staging is supposed to mirror prod, so
#    every directory the prod script ships needs to ship here too.
echo "→ Uploading files..."
scp -q server.py "${SSH_TARGET}:${STAGING_PATH}/server.py"
scp -q server_features.py "${SSH_TARGET}:${STAGING_PATH}/server_features.py" 2>/dev/null || true
scp -q db.py "${SSH_TARGET}:${STAGING_PATH}/db.py"
scp -q config.json "${SSH_TARGET}:${STAGING_PATH}/config.json"
scp -q requirements.txt "${SSH_TARGET}:${STAGING_PATH}/requirements.txt"
scp -qr backend "${SSH_TARGET}:${STAGING_PATH}/"
scp -qr security "${SSH_TARGET}:${STAGING_PATH}/" 2>/dev/null || true
scp -qr migrations "${SSH_TARGET}:${STAGING_PATH}/" 2>/dev/null || true
scp -qr email_system "${SSH_TARGET}:${STAGING_PATH}/" 2>/dev/null || true
scp -qr auth "${SSH_TARGET}:${STAGING_PATH}/" 2>/dev/null || true
scp -qr jobs "${SSH_TARGET}:${STAGING_PATH}/" 2>/dev/null || true
scp -qr static "${SSH_TARGET}:${STAGING_PATH}/"
scp -qr tests "${SSH_TARGET}:${STAGING_PATH}/"
echo "  ✓ files uploaded"

# 3b. Install / upgrade Python deps on the staging side too. Same pip
# trick as deploy-production.sh — --user + --break-system-packages on
# Debian/Ubuntu 23+. Idempotent: pip skips up-to-date packages.
echo "→ Installing Python dependencies on staging..."
ssh "$SSH_TARGET" "
    cd ${STAGING_PATH}
    python3 -m pip install --user --break-system-packages --quiet -r requirements.txt 2>&1 | tail -5 || true
"
echo "  ✓ deps installed"

# 4. Restart staging uvicorn. Prod on 7000 is NOT touched.
echo "→ Restarting staging uvicorn on port ${STAGING_PORT}..."
ssh "$SSH_TARGET" "
    set -e
    # Kill anything on the staging port (staging only)
    fuser -k ${STAGING_PORT}/tcp 2>/dev/null || true
    sleep 2

    # Load staging env, start uvicorn, redirect logs
    cd ${STAGING_PATH}
    if [ ! -f ~/.gateway_env_staging ]; then
        echo '✗ ~/.gateway_env_staging does not exist on the server'
        echo '  Copy staging/.env.staging there, fill in secrets, and re-run'
        exit 1
    fi

    set -a
    . ~/.gateway_env_staging
    set +a

    nohup python3 -m uvicorn server:app \\
        --host 127.0.0.1 \\
        --port ${STAGING_PORT} \\
        > /tmp/gateway_staging.log 2>&1 &
    sleep 4
    PID=\$(pgrep -f 'uvicorn server:app.*${STAGING_PORT}' || true)
    if [ -z \"\$PID\" ]; then
        echo '✗ staging uvicorn failed to start, tail of log:'
        tail -20 /tmp/gateway_staging.log
        exit 1
    fi
    echo \"  ✓ staging PID: \$PID\"
"

# 5. Verify staging /health from this machine (via Cloudflare Tunnel)
echo "→ Verifying ${STAGING_URL}/health..."
sleep 2
if curl -sf -o /tmp/staging_health.json -w '%{http_code}\n' "${STAGING_URL}/health" | grep -q '^200$'; then
    echo "  ✓ staging healthy"
    python3 -m json.tool /tmp/staging_health.json 2>/dev/null || cat /tmp/staging_health.json
    rm -f /tmp/staging_health.json
else
    echo "  ✗ staging /health did not return 200"
    echo "  check logs: ssh ${SSH_TARGET} 'tail -40 /tmp/gateway_staging.log'"
    exit 1
fi

echo ""
echo "✓ staging deployment complete — ${STAGING_URL}"
