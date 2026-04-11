#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# deploy-production.sh — deploy the gateway to narve.ai (port 7000)
#
# Pre-flight:
#   1. Runs local syntax check + pytest
#   2. Verifies staging is healthy (refuses to deploy if staging is red)
#   3. Prompts for literal "deploy" confirmation
#   4. scp's files, restarts uvicorn, verifies health
#
# Usage:
#   bash scripts/deploy-production.sh
#
# Environment overrides:
#   PROD_HOST       - Tailscale IP (default: 100.69.44.108)
#   PROD_USER       - SSH user (default: julianhabbig)
#   PROD_PATH       - remote project dir (default: ~/Habbig/gateway)
#   SKIP_STAGING    - set to 1 to bypass the staging health check (NOT RECOMMENDED)
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

PROD_HOST="${PROD_HOST:-100.69.44.108}"
PROD_USER="${PROD_USER:-julianhabbig}"
PROD_PATH="${PROD_PATH:-~/Habbig/gateway}"
PROD_PORT="${PROD_PORT:-7000}"
PROD_URL="${PROD_URL:-https://narve.ai}"
STAGING_URL="${STAGING_URL:-https://staging.narve.ai}"

SSH_TARGET="${PROD_USER}@${PROD_HOST}"

cd "$(dirname "$0")/.."

echo "→ deploy-production.sh"
echo ""

# 1. Local syntax check
echo "→ Syntax check..."
python3 -c "import ast; ast.parse(open('server.py').read())" || { echo "✗ server.py syntax error"; exit 1; }
python3 -c "import ast; ast.parse(open('db.py').read())" || { echo "✗ db.py syntax error"; exit 1; }
for f in backend/markets/*.py; do
    python3 -c "import ast; ast.parse(open('$f').read())" || { echo "✗ $f syntax error"; exit 1; }
done
echo "  ✓ syntax OK"

# 2. Local tests
if command -v pytest >/dev/null 2>&1; then
    echo "→ Running tests..."
    python3 -m pytest tests/ -x -q --tb=line || { echo "✗ tests failed — deployment aborted"; exit 1; }
    echo "  ✓ tests passed"
fi

# 3. Verify staging is healthy
if [ "${SKIP_STAGING:-0}" = "1" ]; then
    echo "→ SKIP_STAGING=1 — bypassing staging health check (dangerous!)"
else
    echo "→ Verifying staging is healthy..."
    STAGING_STATUS=$(curl -sS -o /tmp/staging_health.json -w '%{http_code}' "${STAGING_URL}/health" || echo "000")
    if [ "$STAGING_STATUS" != "200" ]; then
        echo "  ✗ staging returned HTTP $STAGING_STATUS"
        echo "  refusing to deploy to production while staging is red"
        echo "  override with: SKIP_STAGING=1 bash scripts/deploy-production.sh"
        rm -f /tmp/staging_health.json
        exit 1
    fi
    STAGING_JSON_STATUS=$(python3 -c "import json; print(json.load(open('/tmp/staging_health.json'))['status'])" 2>/dev/null || echo "unknown")
    if [ "$STAGING_JSON_STATUS" = "error" ]; then
        echo "  ✗ staging health status is 'error' — fix staging first"
        cat /tmp/staging_health.json
        rm -f /tmp/staging_health.json
        exit 1
    fi
    rm -f /tmp/staging_health.json
    echo "  ✓ staging healthy (status=$STAGING_JSON_STATUS)"
fi

# 4. Confirmation prompt
echo ""
echo "⚠️  You are about to deploy to PRODUCTION (${PROD_URL})."
echo "   Host:    $PROD_HOST"
echo "   Path:    $PROD_PATH"
echo "   Staging: ${SKIP_STAGING:+SKIPPED}${SKIP_STAGING:-✓ healthy}"
echo ""
echo "   Type 'deploy' to confirm, anything else to abort:"
read -r CONFIRM
if [ "$CONFIRM" != "deploy" ]; then
    echo "Aborted."
    exit 0
fi

# 5. Capture current HEAD on server for easy rollback reference
echo "→ Capturing rollback reference..."
ROLLBACK_COMMIT=$(ssh "$SSH_TARGET" "cd ${PROD_PATH} && git rev-parse HEAD 2>/dev/null" || echo "unknown")
echo "  previous HEAD: $ROLLBACK_COMMIT"
echo "  (to rollback: bash scripts/rollback.sh $ROLLBACK_COMMIT)"

# 6. Upload files — per-file scp to avoid the rsync multiple-source gotcha
echo "→ Uploading files to production..."
scp -q server.py "${SSH_TARGET}:${PROD_PATH}/server.py"
scp -q server_features.py "${SSH_TARGET}:${PROD_PATH}/server_features.py" 2>/dev/null || true
scp -q db.py "${SSH_TARGET}:${PROD_PATH}/db.py"
scp -q config.json "${SSH_TARGET}:${PROD_PATH}/config.json"
scp -q requirements.txt "${SSH_TARGET}:${PROD_PATH}/requirements.txt"
scp -qr backend "${SSH_TARGET}:${PROD_PATH}/"
scp -qr security "${SSH_TARGET}:${PROD_PATH}/"
scp -qr migrations "${SSH_TARGET}:${PROD_PATH}/"
scp -qr email_system "${SSH_TARGET}:${PROD_PATH}/"
scp -qr auth "${SSH_TARGET}:${PROD_PATH}/" 2>/dev/null || true
scp -qr jobs "${SSH_TARGET}:${PROD_PATH}/" 2>/dev/null || true
scp -qr static "${SSH_TARGET}:${PROD_PATH}/"
scp -qr tests "${SSH_TARGET}:${PROD_PATH}/"
echo "  ✓ files uploaded"

# 6b. Install / upgrade Python deps on server (idempotent — pip skips up-to-date)
# --break-system-packages is required on Debian/Ubuntu 23+ (PEP 668) because
# --user installs still live under the system-managed Python. We trust the
# --user prefix to isolate from dpkg-managed packages.
echo "→ Installing Python dependencies on server..."
ssh "$SSH_TARGET" "
    cd ${PROD_PATH}
    python3 -m pip install --user --break-system-packages --quiet -r requirements.txt 2>&1 | tail -5 || true
"
echo "  ✓ deps installed"

# 7. Restart prod uvicorn
echo "→ Restarting production uvicorn on port ${PROD_PORT}..."
ssh "$SSH_TARGET" "
    set -e
    fuser -k ${PROD_PORT}/tcp 2>/dev/null || true
    sleep 2

    cd ${PROD_PATH}
    if [ ! -f ~/.gateway_env ]; then
        echo '✗ ~/.gateway_env does not exist on the server'
        exit 1
    fi

    set -a
    . ~/.gateway_env
    set +a

    # Warn loudly if the Fernet key required for TOTP secret encryption is missing.
    if [ -z \"\${CREDENTIALS_ENCRYPTION_KEY:-}\" ]; then
        echo '  ⚠ CREDENTIALS_ENCRYPTION_KEY is unset in ~/.gateway_env'
        echo '    2FA TOTP setup will fail until this is configured.'
        echo '    Generate one with:'
        echo '      python3 -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\"'
    fi

    nohup env PRODUCTION=1 python3 -m uvicorn server:app \\
        --host 127.0.0.1 \\
        --port ${PROD_PORT} \\
        > /tmp/gateway.log 2>&1 &
    sleep 4
    PID=\$(pgrep -f 'uvicorn server:app.*${PROD_PORT}' || true)
    if [ -z \"\$PID\" ]; then
        echo '✗ production uvicorn failed to start, tail of log:'
        tail -20 /tmp/gateway.log
        exit 1
    fi
    echo \"  ✓ production PID: \$PID\"
"

# 8. Post-deploy git commit on server — the memory warns this is essential:
#    "MUST commit on server after deploy" or future git ops revert changes.
echo "→ Committing deployed state on server..."
ssh "$SSH_TARGET" "
    cd ${PROD_PATH}
    if git status --porcelain | grep -q .; then
        git add -A
        git commit -m 'Deploy: $(date -u +%Y-%m-%d' '%H:%M:%SZ)' >/dev/null
        echo '  ✓ committed'
    else
        echo '  (no changes to commit)'
    fi
"

# 9. Verify prod /health
echo "→ Verifying ${PROD_URL}/health..."
sleep 2
if curl -sf -o /tmp/prod_health.json -w '%{http_code}\n' "${PROD_URL}/health" | grep -q '^200$'; then
    echo "  ✓ production healthy"
    python3 -m json.tool /tmp/prod_health.json 2>/dev/null || cat /tmp/prod_health.json
    rm -f /tmp/prod_health.json
else
    echo "  ✗ production /health did not return 200"
    echo "  IMMEDIATE ACTION: bash scripts/rollback.sh ${ROLLBACK_COMMIT}"
    exit 1
fi

echo ""
echo "✓ production deployment complete — ${PROD_URL}"
echo "  rollback ref: $ROLLBACK_COMMIT"
