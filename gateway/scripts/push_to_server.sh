#!/usr/bin/env bash
#
# push_to_server.sh — deploy all pending changes to the production server.
#
# Run this when the server at 100.69.44.108 comes back online.
# It scps every changed file individually (never rsync with multiple
# source args — per CLAUDE.md gotcha) then restarts the gateway and
# commits on the server.
#
# Usage:
#   chmod +x scripts/push_to_server.sh
#   ./scripts/push_to_server.sh
#
# Prerequisites:
#   - Tailscale connected (ssh julianhabbig@100.69.44.108 must work)
#   - SITE_ACCESS_TOKEN env var known (the script will prompt)

set -euo pipefail

SERVER="julianhabbig@100.69.44.108"
REMOTE_DIR="~/Habbig/gateway"
LOCAL_DIR="$(cd "$(dirname "$0")/.." && pwd)"

echo "=== narve.ai deploy script ==="
echo "Local:  $LOCAL_DIR"
echo "Remote: $SERVER:$REMOTE_DIR"
echo ""

# 1. Test SSH connectivity
echo "[1/6] Testing SSH connectivity..."
if ! ssh -o ConnectTimeout=10 "$SERVER" "echo OK" >/dev/null 2>&1; then
    echo "ERROR: Cannot reach $SERVER via SSH. Is Tailscale running?"
    exit 1
fi
echo "  SSH OK"

# 2. Create remote directories that may not exist yet
echo "[2/6] Ensuring remote directories exist..."
ssh "$SERVER" "mkdir -p $REMOTE_DIR/auth $REMOTE_DIR/migrations $REMOTE_DIR/email_system/templates $REMOTE_DIR/jobs $REMOTE_DIR/observability $REMOTE_DIR/intelligence $REMOTE_DIR/scripts $REMOTE_DIR/tests"

# 3. scp each file individually
echo "[3/6] Copying files..."

# --- Auth module (token-first flow) ---
scp "$LOCAL_DIR/auth/__init__.py"    "$SERVER:$REMOTE_DIR/auth/__init__.py"
scp "$LOCAL_DIR/auth/cookies.py"     "$SERVER:$REMOTE_DIR/auth/cookies.py"
scp "$LOCAL_DIR/auth/guards.py"      "$SERVER:$REMOTE_DIR/auth/guards.py"
scp "$LOCAL_DIR/auth/middleware.py"   "$SERVER:$REMOTE_DIR/auth/middleware.py"

# --- Migrations ---
for f in "$LOCAL_DIR"/migrations/*.py; do
    scp "$f" "$SERVER:$REMOTE_DIR/migrations/$(basename "$f")"
done

# --- Email system ---
scp "$LOCAL_DIR/email_system/__init__.py"  "$SERVER:$REMOTE_DIR/email_system/__init__.py"
scp "$LOCAL_DIR/email_system/service.py"   "$SERVER:$REMOTE_DIR/email_system/service.py"
scp "$LOCAL_DIR/email_system/renderer.py"  "$SERVER:$REMOTE_DIR/email_system/renderer.py"
scp "$LOCAL_DIR/email_system/unsubscribe.py" "$SERVER:$REMOTE_DIR/email_system/unsubscribe.py"
for f in "$LOCAL_DIR"/email_system/templates/*.html; do
    scp "$f" "$SERVER:$REMOTE_DIR/email_system/templates/$(basename "$f")"
done

# --- Jobs module ---
for f in "$LOCAL_DIR"/jobs/*.py; do
    scp "$f" "$SERVER:$REMOTE_DIR/jobs/$(basename "$f")"
done

# --- Observability ---
for f in "$LOCAL_DIR"/observability/*.py; do
    scp "$f" "$SERVER:$REMOTE_DIR/observability/$(basename "$f")"
done

# --- Intelligence ---
for f in "$LOCAL_DIR"/intelligence/*.py; do
    scp "$f" "$SERVER:$REMOTE_DIR/intelligence/$(basename "$f")"
done

# --- Core server files ---
scp "$LOCAL_DIR/server.py"          "$SERVER:$REMOTE_DIR/server.py"
scp "$LOCAL_DIR/server_features.py" "$SERVER:$REMOTE_DIR/server_features.py"
scp "$LOCAL_DIR/db.py"              "$SERVER:$REMOTE_DIR/db.py"
scp "$LOCAL_DIR/requirements.txt"   "$SERVER:$REMOTE_DIR/requirements.txt"
scp "$LOCAL_DIR/.env.example"       "$SERVER:$REMOTE_DIR/.env.example"

# --- Static templates (new + modified) ---
scp "$LOCAL_DIR/static/token.html"      "$SERVER:$REMOTE_DIR/static/token.html"
scp "$LOCAL_DIR/static/register.html"   "$SERVER:$REMOTE_DIR/static/register.html"
scp "$LOCAL_DIR/static/login.html"      "$SERVER:$REMOTE_DIR/static/login.html"
scp "$LOCAL_DIR/static/onboarding.html" "$SERVER:$REMOTE_DIR/static/onboarding.html"
scp "$LOCAL_DIR/static/landing.html"    "$SERVER:$REMOTE_DIR/static/landing.html"
scp "$LOCAL_DIR/static/pricing.html"    "$SERVER:$REMOTE_DIR/static/pricing.html"
scp "$LOCAL_DIR/static/subscribe.html"  "$SERVER:$REMOTE_DIR/static/subscribe.html"
scp "$LOCAL_DIR/static/intelligence.html" "$SERVER:$REMOTE_DIR/static/intelligence.html"
scp "$LOCAL_DIR/static/terms.html"      "$SERVER:$REMOTE_DIR/static/terms.html"
scp "$LOCAL_DIR/static/privacy.html"    "$SERVER:$REMOTE_DIR/static/privacy.html"
scp "$LOCAL_DIR/static/invite.html"     "$SERVER:$REMOTE_DIR/static/invite.html"
scp "$LOCAL_DIR/static/forgot-password.html" "$SERVER:$REMOTE_DIR/static/forgot-password.html"
scp "$LOCAL_DIR/static/reset-password.html"  "$SERVER:$REMOTE_DIR/static/reset-password.html"

# Static assets (skeletons, analytics, feedback, sentry)
for f in skeletons.css skeletons.js analytics.js feedback.js sentry-boot.js; do
    [ -f "$LOCAL_DIR/static/$f" ] && scp "$LOCAL_DIR/static/$f" "$SERVER:$REMOTE_DIR/static/$f"
done

# --- Docs ---
[ -f "$LOCAL_DIR/CLOUDFLARE_CHANGES.md" ] && scp "$LOCAL_DIR/CLOUDFLARE_CHANGES.md" "$SERVER:$REMOTE_DIR/CLOUDFLARE_CHANGES.md"
[ -f "$LOCAL_DIR/README.md" ] && scp "$LOCAL_DIR/README.md" "$SERVER:$REMOTE_DIR/README.md"
[ -f "$LOCAL_DIR/docker-compose.yml" ] && scp "$LOCAL_DIR/docker-compose.yml" "$SERVER:$REMOTE_DIR/docker-compose.yml"

echo "  All files copied."

# 4. Restart the gateway
echo "[4/6] Restarting gateway on port 7000..."
ssh "$SERVER" "cd $REMOTE_DIR && fuser -k 7000/tcp 2>/dev/null || true; sleep 2"

# Read the SITE_ACCESS_TOKEN from the server's .env if it exists
ssh "$SERVER" "cd $REMOTE_DIR && \
    if [ -f .env ]; then \
        set -a; source .env; set +a; \
    fi; \
    nohup env PRODUCTION=1 \
        SITE_ACCESS_TOKEN=\${SITE_ACCESS_TOKEN:-} \
        ENVIRONMENT=\${ENVIRONMENT:-production} \
        APP_VERSION=\${APP_VERSION:-1.0.0} \
        EMAIL_DRY_RUN=\${EMAIL_DRY_RUN:-true} \
        python3 -m uvicorn server:app --host 127.0.0.1 --port 7000 \
        > /tmp/gateway.log 2>&1 &
    sleep 3
    echo 'Gateway PID:' \$(lsof -ti :7000 2>/dev/null || echo 'NOT RUNNING')"

# 5. Verify
echo "[5/6] Verifying..."
ssh "$SERVER" "curl -sS -o /dev/null -w 'health=%{http_code}\n' http://127.0.0.1:7000/health && \
    curl -sS -o /dev/null -w 'token_page=%{http_code}\n' http://127.0.0.1:7000/token && \
    curl -sS -o /dev/null -w 'login_redirect=%{http_code}\n' -I http://127.0.0.1:7000/login"

# 6. Commit on server (CLAUDE.md gotcha — git op reverts changes otherwise)
echo "[6/6] Committing on server..."
ssh "$SERVER" "cd $REMOTE_DIR && \
    git add -A && \
    git -c user.email=narve@narve.ai -c user.name=narve \
        commit -m 'deploy: UX audit fixes + token-first auth + email system + jobs + migrations' \
        2>&1 | tail -3"

echo ""
echo "=== Deploy complete ==="
echo "Verify live: curl -I https://narve.ai/token"
