#!/bin/bash
#
# setup_cloudflare.sh — Register DNS routes for narve.ai + all dashboard
# subdomains in one shot.
#
# Usage:
#   ./gateway/setup_cloudflare.sh <tunnel-id>
#
# Prereqs:
#   1. You've run `cloudflared tunnel login` and `cloudflared tunnel create`
#   2. narve.ai is active in your Cloudflare account
#   3. The tunnel ID is visible via `cloudflared tunnel list`
#
# What it does:
#   For each dashboard subdomain in gateway/config.json plus the apex,
#   runs `cloudflared tunnel route dns <tunnel-id> <host>`. Safe to re-run;
#   Cloudflare will say "route already exists" for anything previously created.
#

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CONFIG_PATH="$SCRIPT_DIR/config.json"

# Colors
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

if [ -z "$1" ]; then
    echo -e "${RED}ERROR: missing tunnel ID${NC}"
    echo ""
    echo "Usage: $0 <tunnel-id>"
    echo ""
    echo "Find your tunnel ID with:"
    echo "    cloudflared tunnel list"
    echo ""
    exit 1
fi

TUNNEL_ID="$1"

if ! command -v cloudflared >/dev/null 2>&1; then
    echo -e "${RED}ERROR: cloudflared not found in PATH${NC}"
    echo "Install it first:"
    echo "  macOS:   brew install cloudflared"
    echo "  Ubuntu:  curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 -o /usr/local/bin/cloudflared && chmod +x /usr/local/bin/cloudflared"
    exit 1
fi

if [ ! -f "$CONFIG_PATH" ]; then
    echo -e "${RED}ERROR: config.json not found at $CONFIG_PATH${NC}"
    exit 1
fi

# Extract domain + subdomains from config.json using python3 (stdlib, no jq needed)
DOMAIN=$(python3 -c "import json; print(json.load(open('$CONFIG_PATH'))['domain'])")
SUBDOMAINS=$(python3 -c "
import json
cfg = json.load(open('$CONFIG_PATH'))
for d in cfg['dashboards'].values():
    sub = d.get('subdomain', '').strip()
    if sub:
        print(sub)
")

echo ""
echo -e "${BLUE}=========================================${NC}"
echo -e "${BLUE}  Cloudflare DNS setup for $DOMAIN${NC}"
echo -e "${BLUE}=========================================${NC}"
echo ""
echo -e "Tunnel ID:  ${YELLOW}$TUNNEL_ID${NC}"
echo -e "Domain:     ${YELLOW}$DOMAIN${NC}"
echo -e "Subdomains: $(echo "$SUBDOMAINS" | wc -l | xargs) + apex"
echo ""

route_host() {
    local HOST="$1"
    local TMPFILE
    TMPFILE="$(mktemp)"
    echo -e "${GREEN}→${NC} $HOST"
    if cloudflared tunnel route dns "$TUNNEL_ID" "$HOST" 2>&1 | tee "$TMPFILE"; then
        :
    else
        # Exit non-zero is fine for "already exists" — log and continue
        if grep -q "already exists" "$TMPFILE"; then
            echo -e "  ${YELLOW}(route already exists, skipping)${NC}"
        else
            echo -e "  ${RED}FAILED${NC}"
        fi
    fi
    rm -f "$TMPFILE"
    echo ""
}

# Apex first
route_host "$DOMAIN"

# Then each subdomain
while IFS= read -r SUB; do
    if [ -n "$SUB" ]; then
        route_host "$SUB.$DOMAIN"
    fi
done <<< "$SUBDOMAINS"

echo -e "${BLUE}=========================================${NC}"
echo -e "${GREEN}  DNS routes configured.${NC}"
echo -e "${BLUE}=========================================${NC}"
echo ""
echo "Next steps:"
echo "  1. Make sure ~/.cloudflared/config.yml has the wildcard ingress"
echo "     (see gateway/DEPLOY_NARVE.md step 4)"
echo "  2. Start the tunnel:"
echo -e "       ${YELLOW}cloudflared tunnel run narve-gateway${NC}"
echo "  3. Visit https://$DOMAIN/"
echo ""
