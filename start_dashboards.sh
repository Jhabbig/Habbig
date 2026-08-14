#!/bin/bash
#
# Polymarket Dashboard Launcher
# Starts all dashboards plus the central gateway.
#
# Port assignments:
#   7000 — Gateway (central auth + reverse proxy) (gateway/server.py)
#   8000 — Crypto Dashboard          (crypto-dashboard/server.py — Market Edge)
#   8050 — Stock Dashboard           (stock-dashboard/stock_dashboard.py — Market Edge)
#   8051 — Midterm Prediction        (midterm-dashboard/backend/main.py)
#   5050 — Weather Dashboard         (polymarket_weather_dashboard/server.py — includes Disasters + Climate tabs)
#   8888 — Sports Dashboard          (sports-dashboard/sports_dashboard.py)
#   7050 — World State Dashboard     (world-state-dashboard/server.py)
#   8052 — Top Traders Dashboard     (top-traders-dashboard/server.py)
#   7060 — Central Bank Dashboard    (centralbank-dashboard/server.py)
#  18789 — Truth Research            (Dashboard-x-truth-research-prediction, uvicorn app.main:app)
#   7070 — AI Race Dashboard         (ai-race-dashboard/server.py)
#   7054 — Crypto Trackers           (crypto-trackers-dashboard/server.py)
#   7062 — Religion & Cults Tracker  (religion-dashboard/server.py)
#   8053 — Whale Watch               (whale-dashboard/backend/main.py)
#   7051 — Voters Dashboard          (voters-dashboard/server.py)
#
# The whole fleet is live again. Most revived dashboards gate every route
# behind GATEWAY_SSO_SECRET (503 without it). This script loads
# gateway/.env.production then gateway/.env (later wins) and exports both to
# every dashboard process — same model as the systemd units' EnvironmentFile.
# For a local preview without secrets: DEV_MODE=1 ./start_dashboards.sh start
#

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# Colors
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

ALL_PORTS="7000 8000 8050 8051 5050 8888 7050 8052 7060 18789 7070 7054 7062 8053 7051"

# Kill dashboard processes — prefer PID files, fall back to port scan
cleanup() {
    echo -e "${YELLOW}Stopping all dashboards...${NC}"
    local used_pids=false
    for PIDFILE in /tmp/dashboard_*.pid; do
        if [ -f "$PIDFILE" ]; then
            used_pids=true
            PID=$(cat "$PIDFILE")
            if [ -n "$PID" ] && kill -0 "$PID" 2>/dev/null; then
                kill "$PID" 2>/dev/null || true
                echo "  Stopped PID $PID (from $PIDFILE)"
            fi
            rm -f "$PIDFILE"
        fi
    done
    # Fall back to port-based killing only if no PID files were found
    if [ "$used_pids" = false ]; then
        for PORT in $ALL_PORTS; do
            PID=$(lsof -ti :$PORT 2>/dev/null || true)
            if [ -n "$PID" ]; then
                kill $PID 2>/dev/null || true
                echo "  Stopped process on port $PORT (PID $PID)"
            fi
        done
    fi
    echo -e "${GREEN}All dashboards stopped.${NC}"
}

start_all() {
    echo ""
    echo -e "${BLUE}=========================================${NC}"
    echo -e "${BLUE}  Polymarket Dashboard Launcher${NC}"
    echo -e "${BLUE}=========================================${NC}"
    echo ""

    # Activate venv if present
    if [ -f "$SCRIPT_DIR/venv/bin/activate" ]; then
        source "$SCRIPT_DIR/venv/bin/activate"
    fi

    # Load + export the shared gateway env so SSO-gated dashboards receive
    # GATEWAY_SSO_SECRET (and API keys) — the same file the systemd units
    # load via EnvironmentFile. Without this, voters/centralbank/trackers/
    # airace fail closed (503 on every route). gateway/.env wins locally.
    set -a
    [ -f "$SCRIPT_DIR/gateway/.env.production" ] && source "$SCRIPT_DIR/gateway/.env.production"
    [ -f "$SCRIPT_DIR/gateway/.env" ] && source "$SCRIPT_DIR/gateway/.env"
    set +a

    # 1. Crypto Dashboard (port 8000)
    echo -e "${GREEN}[1/15]${NC} Starting Crypto Dashboard on port 8000..."
    python3 "$SCRIPT_DIR/crypto-dashboard/server.py" > /tmp/dashboard_crypto.log 2>&1 &
    echo $! > /tmp/dashboard_crypto.pid
    echo "       PID: $(cat /tmp/dashboard_crypto.pid)"

    # 2. Stock Dashboard (port 8050)
    echo -e "${GREEN}[2/15]${NC} Starting Stock Dashboard on port 8050..."
    python3 "$SCRIPT_DIR/stock-dashboard/stock_dashboard.py" --port 8050 > /tmp/dashboard_stock.log 2>&1 &
    echo $! > /tmp/dashboard_stock.pid
    echo "       PID: $(cat /tmp/dashboard_stock.pid)"

    # 3. Midterm Prediction Dashboard (port 8051)
    echo -e "${GREEN}[3/15]${NC} Starting Midterm Dashboard on port 8051..."
    cd "$SCRIPT_DIR/midterm-dashboard/backend"
    python3 main.py > /tmp/dashboard_midterm.log 2>&1 &
    echo $! > /tmp/dashboard_midterm.pid
    echo "       PID: $(cat /tmp/dashboard_midterm.pid)"
    cd "$SCRIPT_DIR"

    # 4. Weather Dashboard (port 5050)
    echo -e "${GREEN}[4/15]${NC} Starting Weather Dashboard on port 5050..."
    python3 "$SCRIPT_DIR/polymarket_weather_dashboard/server.py" > /tmp/dashboard_weather.log 2>&1 &
    echo $! > /tmp/dashboard_weather.pid
    echo "       PID: $(cat /tmp/dashboard_weather.pid)"

    # 5. Sports Dashboard (port 8888)
    echo -e "${GREEN}[5/15]${NC} Starting Sports Dashboard on port 8888..."
    python3 "$SCRIPT_DIR/sports-dashboard/sports_dashboard.py" > /tmp/dashboard_sports.log 2>&1 &
    echo $! > /tmp/dashboard_sports.pid
    echo "       PID: $(cat /tmp/dashboard_sports.pid)"

    # 6. World State Dashboard (port 7050)
    echo -e "${GREEN}[6/15]${NC} Starting World State Dashboard on port 7050..."
    python3 "$SCRIPT_DIR/world-state-dashboard/server.py" > /tmp/dashboard_world.log 2>&1 &
    echo $! > /tmp/dashboard_world.pid
    echo "       PID: $(cat /tmp/dashboard_world.pid)"

    # 7. Top Traders Dashboard (port 8052)
    echo -e "${GREEN}[7/15]${NC} Starting Top Traders Dashboard on port 8052..."
    python3 "$SCRIPT_DIR/top-traders-dashboard/server.py" > /tmp/dashboard_traders.log 2>&1 &
    echo $! > /tmp/dashboard_traders.pid
    echo "       PID: $(cat /tmp/dashboard_traders.pid)"

    # 8. Central Bank Dashboard (port 7060)
    echo -e "${GREEN}[8/15]${NC} Starting Central Bank Dashboard on port 7060..."
    python3 "$SCRIPT_DIR/centralbank-dashboard/server.py" > /tmp/dashboard_centralbank.log 2>&1 &
    echo $! > /tmp/dashboard_centralbank.pid
    echo "       PID: $(cat /tmp/dashboard_centralbank.pid)"

    # 9. Truth Research (port 18789) — uvicorn app, must run from its own dir
    echo -e "${GREEN}[9/15]${NC} Starting Truth Research on port 18789..."
    cd "$SCRIPT_DIR/Dashboard-x-truth-research-prediction"
    python3 -m uvicorn app.main:app --host 127.0.0.1 --port 18789 > /tmp/dashboard_truth.log 2>&1 &
    echo $! > /tmp/dashboard_truth.pid
    echo "       PID: $(cat /tmp/dashboard_truth.pid)"
    cd "$SCRIPT_DIR"

    # 10. AI Race Dashboard (port 7070)
    echo -e "${GREEN}[10/15]${NC} Starting AI Race Dashboard on port 7070..."
    python3 "$SCRIPT_DIR/ai-race-dashboard/server.py" > /tmp/dashboard_airace.log 2>&1 &
    echo $! > /tmp/dashboard_airace.pid
    echo "       PID: $(cat /tmp/dashboard_airace.pid)"

    # 11. Crypto Trackers Dashboard (port 7054)
    echo -e "${GREEN}[11/15]${NC} Starting Crypto Trackers Dashboard on port 7054..."
    python3 "$SCRIPT_DIR/crypto-trackers-dashboard/server.py" > /tmp/dashboard_trackers.log 2>&1 &
    echo $! > /tmp/dashboard_trackers.pid
    echo "       PID: $(cat /tmp/dashboard_trackers.pid)"

    # 12. Religion & Cults Tracker (port 7062) — alerts DB path must be
    # writable outside Docker (its default /app/data only exists in-container),
    # and the bind stays on loopback: this app has no SSO gating of its own,
    # so exposing it beyond the same-host gateway would bypass the paywall.
    echo -e "${GREEN}[12/15]${NC} Starting Religion Dashboard on port 7062..."
    mkdir -p "$SCRIPT_DIR/religion-dashboard/data"
    ALERTS_DB_PATH="$SCRIPT_DIR/religion-dashboard/data/alerts.sqlite" \
    BIND_HOST=127.0.0.1 \
        python3 "$SCRIPT_DIR/religion-dashboard/server.py" > /tmp/dashboard_religion.log 2>&1 &
    echo $! > /tmp/dashboard_religion.pid
    echo "       PID: $(cat /tmp/dashboard_religion.pid)"

    # 13. Whale Watch (port 8053)
    echo -e "${GREEN}[13/15]${NC} Starting Whale Watch on port 8053..."
    python3 "$SCRIPT_DIR/whale-dashboard/backend/main.py" > /tmp/dashboard_whale.log 2>&1 &
    echo $! > /tmp/dashboard_whale.pid
    echo "       PID: $(cat /tmp/dashboard_whale.pid)"

    # 14. Voters Dashboard (port 7051)
    echo -e "${GREEN}[14/15]${NC} Starting Voters Dashboard on port 7051..."
    python3 "$SCRIPT_DIR/voters-dashboard/server.py" > /tmp/dashboard_voters.log 2>&1 &
    echo $! > /tmp/dashboard_voters.pid
    echo "       PID: $(cat /tmp/dashboard_voters.pid)"

    # 15. Gateway (port 7000) — starts last so upstreams are up first
    echo -e "${GREEN}[15/15]${NC} Starting Gateway on port 7000..."
    cd "$SCRIPT_DIR/gateway"
    python3 server.py > /tmp/dashboard_gateway.log 2>&1 &
    echo $! > /tmp/dashboard_gateway.pid
    echo "       PID: $(cat /tmp/dashboard_gateway.pid)"
    cd "$SCRIPT_DIR"

    sleep 2
    echo ""
    echo -e "${BLUE}=========================================${NC}"
    echo -e "${BLUE}  All dashboards running!${NC}"
    echo -e "${BLUE}=========================================${NC}"
    echo ""
    echo -e "  ${GREEN}Gateway (entry point):${NC} http://localhost:7000"
    echo -e "  ${GREEN}Unified view:${NC}          http://localhost:7000/one  (every dashboard, one screen)"
    echo -e "  ${GREEN}Crypto Dashboard:${NC}      http://localhost:8000"
    echo -e "  ${GREEN}Stock Dashboard:${NC}       http://localhost:8050"
    echo -e "  ${GREEN}Midterm Dashboard:${NC}     http://localhost:8051"
    echo -e "  ${GREEN}Weather Dashboard:${NC}     http://localhost:5050  (incl. /disasters + /climate)"
    echo -e "  ${GREEN}Sports Dashboard:${NC}      http://localhost:8888"
    echo -e "  ${GREEN}World State:${NC}           http://localhost:7050"
    echo -e "  ${GREEN}Top Traders:${NC}           http://localhost:8052"
    echo -e "  ${GREEN}Central Bank:${NC}          http://localhost:7060"
    echo -e "  ${GREEN}Truth Research:${NC}        http://localhost:18789"
    echo -e "  ${GREEN}AI Race:${NC}               http://localhost:7070"
    echo -e "  ${GREEN}Crypto Trackers:${NC}       http://localhost:7054"
    echo -e "  ${GREEN}Religion Tracker:${NC}      http://localhost:7062"
    echo -e "  ${GREEN}Whale Watch:${NC}           http://localhost:8053"
    echo -e "  ${GREEN}Voters Dashboard:${NC}      http://localhost:7051"
    echo ""
    echo -e "  Local subdomain test: http://crypto.localhost:7000"
    echo -e "  Logs: /tmp/dashboard_*.log"
    echo -e "  Stop: ${YELLOW}$0 stop${NC}"
    echo ""
}

status() {
    echo ""
    echo -e "${BLUE}Dashboard Status:${NC}"
    echo -e "  Port 7000  (Gateway):        $(lsof -ti :7000 >/dev/null 2>&1 && echo -e "${GREEN}RUNNING${NC}" || echo -e "${RED}STOPPED${NC}")"
    echo -e "  Port 8000  (Crypto):         $(lsof -ti :8000 >/dev/null 2>&1 && echo -e "${GREEN}RUNNING${NC}" || echo -e "${RED}STOPPED${NC}")"
    echo -e "  Port 8050  (Stock):          $(lsof -ti :8050 >/dev/null 2>&1 && echo -e "${GREEN}RUNNING${NC}" || echo -e "${RED}STOPPED${NC}")"
    echo -e "  Port 8051  (Midterm):        $(lsof -ti :8051 >/dev/null 2>&1 && echo -e "${GREEN}RUNNING${NC}" || echo -e "${RED}STOPPED${NC}")"
    echo -e "  Port 5050  (Weather):        $(lsof -ti :5050 >/dev/null 2>&1 && echo -e "${GREEN}RUNNING${NC}" || echo -e "${RED}STOPPED${NC}")"
    echo -e "  Port 8888  (Sports):         $(lsof -ti :8888 >/dev/null 2>&1 && echo -e "${GREEN}RUNNING${NC}" || echo -e "${RED}STOPPED${NC}")"
    echo -e "  Port 7050  (World):          $(lsof -ti :7050 >/dev/null 2>&1 && echo -e "${GREEN}RUNNING${NC}" || echo -e "${RED}STOPPED${NC}")"
    echo -e "  Port 8052  (Top Traders):    $(lsof -ti :8052 >/dev/null 2>&1 && echo -e "${GREEN}RUNNING${NC}" || echo -e "${RED}STOPPED${NC}")"
    echo -e "  Port 7060  (Central Bank):   $(lsof -ti :7060 >/dev/null 2>&1 && echo -e "${GREEN}RUNNING${NC}" || echo -e "${RED}STOPPED${NC}")"
    echo -e "  Port 18789 (Truth Research): $(lsof -ti :18789 >/dev/null 2>&1 && echo -e "${GREEN}RUNNING${NC}" || echo -e "${RED}STOPPED${NC}")"
    echo -e "  Port 7070  (AI Race):        $(lsof -ti :7070 >/dev/null 2>&1 && echo -e "${GREEN}RUNNING${NC}" || echo -e "${RED}STOPPED${NC}")"
    echo -e "  Port 7054  (Trackers):       $(lsof -ti :7054 >/dev/null 2>&1 && echo -e "${GREEN}RUNNING${NC}" || echo -e "${RED}STOPPED${NC}")"
    echo -e "  Port 7062  (Religion):       $(lsof -ti :7062 >/dev/null 2>&1 && echo -e "${GREEN}RUNNING${NC}" || echo -e "${RED}STOPPED${NC}")"
    echo -e "  Port 8053  (Whale):          $(lsof -ti :8053 >/dev/null 2>&1 && echo -e "${GREEN}RUNNING${NC}" || echo -e "${RED}STOPPED${NC}")"
    echo -e "  Port 7051  (Voters):         $(lsof -ti :7051 >/dev/null 2>&1 && echo -e "${GREEN}RUNNING${NC}" || echo -e "${RED}STOPPED${NC}")"
    echo ""
}

case "${1:-start}" in
    start)
        cleanup 2>/dev/null
        start_all
        ;;
    stop)
        cleanup
        ;;
    restart)
        cleanup
        start_all
        ;;
    status)
        status
        ;;
    *)
        echo "Usage: $0 {start|stop|restart|status}"
        exit 1
        ;;
esac
