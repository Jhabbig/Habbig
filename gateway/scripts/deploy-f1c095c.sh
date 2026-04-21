#!/usr/bin/env bash
# One-shot deploy script for commit f1c095c (subproducts + portfolio +
# bots + extension + webhook hardening).
#
# Run from your local shell, with Tailscale up. Everything is
# idempotent — safe to re-run if a step fails.
#
#   bash scripts/deploy-f1c095c.sh
#
# Env overrides (same as deploy-production.sh):
#   PROD_HOST=100.69.44.108
#   PROD_USER=julianhabbig
#   PROD_PATH=~/Habbig/gateway
#
# Post-deploy it also fixes the currently-broken CREDENTIALS_ENCRYPTION_KEY
# on prod (you saw "encryption": "error" in /health) by generating one if
# the server's .env.production doesn't already have a value, then
# restarting. No existing encrypted data on the server, so a fresh key is
# safe; we NEVER overwrite an existing non-empty key.

set -euo pipefail

PROD_HOST="${PROD_HOST:-100.69.44.108}"
PROD_USER="${PROD_USER:-julianhabbig}"
PROD_PATH="${PROD_PATH:-~/Habbig/gateway}"
PROD_PORT="${PROD_PORT:-7000}"
PROD_URL="${PROD_URL:-https://narve.ai}"
SSH="ssh -o ConnectTimeout=10 ${PROD_USER}@${PROD_HOST}"

TARGET_SHA="f1c095c"

cd "$(dirname "$0")/.."

echo "→ Local sanity checks"
python3 -c "import ast; ast.parse(open('server.py').read())"
python3 -c "import ast; ast.parse(open('db.py').read())"
for f in \
  subproduct_access.py subproduct_filters.py subproduct_dashboard_routes.py \
  subproduct_signup_routes.py extension_routes.py bot_routes.py \
  stripe_webhook_hardening.py \
  middleware/subproduct.py \
  portfolio/polymarket.py portfolio/kalshi.py portfolio/positions.py \
  portfolio/kelly.py portfolio/routes.py \
  jobs/sync_portfolios.py jobs/reconcile_subscriptions.py \
  jobs/telegram_sends.py \
  migrations/060_subproduct_subscriptions.py \
  migrations/061_processed_stripe_events.py \
  migrations/062_portfolio_integration.py \
  migrations/063_telegram_connections.py \
  migrations/064_discord_integration.py; do
  python3 -c "import ast; ast.parse(open('$f').read())" \
    || { echo "✗ $f syntax error"; exit 1; }
done

echo "→ Remote pull"
$SSH "cd $PROD_PATH && git fetch origin feature/platform-build && git reset --hard $TARGET_SHA"

echo "→ Ensure CREDENTIALS_ENCRYPTION_KEY is set on prod"
$SSH '
  cd '"$PROD_PATH"'
  if [ ! -f .env.production ]; then
    echo "✗ no .env.production on server"
    exit 1
  fi
  if grep -qE "^CREDENTIALS_ENCRYPTION_KEY=..+" .env.production; then
    echo "  already set — leaving it"
  else
    KEY=$(python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")
    # Strip any existing empty line so we do not end up with two.
    sed -i.bak "/^CREDENTIALS_ENCRYPTION_KEY=/d" .env.production
    echo "CREDENTIALS_ENCRYPTION_KEY=$KEY" >> .env.production
    echo "  generated and appended"
  fi
  if ! grep -qE "^EXTENSION_JWT_SECRET=..+" .env.production; then
    SECRET=$(python3 -c "import secrets; print(secrets.token_urlsafe(48))")
    sed -i.bak "/^EXTENSION_JWT_SECRET=/d" .env.production
    echo "EXTENSION_JWT_SECRET=$SECRET" >> .env.production
    echo "  EXTENSION_JWT_SECRET generated"
  fi
'

echo "→ Restart uvicorn"
$SSH '
  cd '"$PROD_PATH"'
  fuser -k '"$PROD_PORT"'/tcp 2>/dev/null || true
  sleep 2
  set -a
  # shellcheck disable=SC1091
  source .env.production
  set +a
  nohup env PRODUCTION=1 $(cat .env.production | grep -v "^#" | xargs) \
    python3 -m uvicorn server:app --host 127.0.0.1 --port '"$PROD_PORT"' \
    > /tmp/gateway.log 2>&1 &
  sleep 3
  tail -40 /tmp/gateway.log | grep -E "migrations: applied|Gateway started|ERROR|Traceback" || true
'

echo "→ Verify"
sleep 2
curl -sf "${PROD_URL}/health" | python3 -m json.tool | head -20

echo
echo "→ Post-deploy commit on server (matches memory's convention)"
$SSH "cd $PROD_PATH && git log --oneline -5"

echo
echo "→ Bot processes (optional: only if token env vars are set)"
$SSH '
  cd '"$PROD_PATH"'
  set -a
  source .env.production
  set +a
  if [ -n "${TELEGRAM_BOT_TOKEN:-}" ]; then
    pkill -f "telegram_bot.py" 2>/dev/null || true
    sleep 1
    nohup python3 ../bots/telegram_bot.py > /tmp/telegram_bot.log 2>&1 &
    echo "  telegram bot started"
  else
    echo "  TELEGRAM_BOT_TOKEN not set — skipping"
  fi
  if [ -n "${DISCORD_BOT_TOKEN:-}" ]; then
    pkill -f "discord_bot.py" 2>/dev/null || true
    sleep 1
    nohup python3 ../bots/discord_bot.py > /tmp/discord_bot.log 2>&1 &
    echo "  discord bot started"
  else
    echo "  DISCORD_BOT_TOKEN not set — skipping"
  fi
'

echo
echo "✓ Deploy complete. Check CLOUDFLARE_CHANGES.md for the DNS + WAF"
echo "  changes — those are applied via the Cloudflare dashboard since"
echo "  this run didn't have the Cloudflare MCP wired in."
