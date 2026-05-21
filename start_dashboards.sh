#!/bin/bash
#
# Polymarket Dashboard Launcher
# Starts all dashboards plus the central gateway.
#
# Port assignments:
#   7000 — Gateway (central auth + reverse proxy) (gateway/server.py)
#   8000 — Crypto Dashboard         (crypto-dashboard/server.py)
#   8050 — Stock Dashboard           (stock-dashboard/stock_dashboard.py)
#   8051 — Midterm Prediction        (midterm-dashboard/backend/main.py)
#   8052 — Top Traders Dashboard     (top-traders-dashboard/server.py)
#   5050 — Weather Dashboard         (polymarket_weather_dashboard/server.py)
#   8888 — Sports Dashboard          (sports-dashboard/sports_dashboard.py)
#   7050 — World State Dashboard     (world-state-dashboard/server.py)
#   7051 — Voters Atlas Dashboard    (voters-dashboard/server.py)
#   7052 — Climate Change Dashboard  (climate-dashboard/server.py)
#   7053 — World Health Dashboard    (world-health-dashboard/server.py)
#   7054 — Crypto Trackers Dashboard (crypto-trackers-dashboard/server.py)
#   7060 — Eco Disasters Dashboard   (ecological-disasters-dashboard/server.py)
#   7061 — Central Bank Dashboard    (centralbank-dashboard/server.py)
#

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# Colors
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

# Gateway port — honour env override (macOS AirPlay Receiver holds 7000).
# Falls back to 7000 to match production / config.json.
GATEWAY_PORT="${GATEWAY_PORT:-7000}"
ALL_PORTS="$GATEWAY_PORT 8000 8050 8051 8052 5050 8888 7050 7051 7052 7053 7054 7060 7061"

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

    # 1. Crypto Dashboard (port 8000)
    echo -e "${GREEN}[1/12]${NC} Starting Crypto Dashboard on port 8000..."
    python3 "$SCRIPT_DIR/crypto-dashboard/server.py" > /tmp/dashboard_crypto.log 2>&1 &
    echo $! > /tmp/dashboard_crypto.pid
    echo "       PID: $(cat /tmp/dashboard_crypto.pid)"

    # 2. Stock Dashboard (port 8050)
    echo -e "${GREEN}[2/12]${NC} Starting Stock Dashboard on port 8050..."
    python3 "$SCRIPT_DIR/stock-dashboard/stock_dashboard.py" --port 8050 > /tmp/dashboard_stock.log 2>&1 &
    echo $! > /tmp/dashboard_stock.pid
    echo "       PID: $(cat /tmp/dashboard_stock.pid)"

    # 3. Midterm Prediction Dashboard (port 8051)
    echo -e "${GREEN}[3/12]${NC} Starting Midterm Dashboard on port 8051..."
    cd "$SCRIPT_DIR/midterm-dashboard/backend"
    python3 main.py > /tmp/dashboard_midterm.log 2>&1 &
    echo $! > /tmp/dashboard_midterm.pid
    echo "       PID: $(cat /tmp/dashboard_midterm.pid)"
    cd "$SCRIPT_DIR"

    # 4. Top Traders Dashboard (port 8052)
    echo -e "${GREEN}[4/12]${NC} Starting Top Traders Dashboard on port 8052..."
    python3 "$SCRIPT_DIR/top-traders-dashboard/server.py" > /tmp/dashboard_top_traders.log 2>&1 &
    echo $! > /tmp/dashboard_top_traders.pid
    echo "       PID: $(cat /tmp/dashboard_top_traders.pid)"

    # 5. Weather Dashboard (port 5050)
    echo -e "${GREEN}[5/12]${NC} Starting Weather Dashboard on port 5050..."
    python3 "$SCRIPT_DIR/polymarket_weather_dashboard/server.py" > /tmp/dashboard_weather.log 2>&1 &
    echo $! > /tmp/dashboard_weather.pid
    echo "       PID: $(cat /tmp/dashboard_weather.pid)"

    # 6. Sports Dashboard (port 8888)
    echo -e "${GREEN}[6/12]${NC} Starting Sports Dashboard on port 8888..."
    python3 "$SCRIPT_DIR/sports-dashboard/sports_dashboard.py" > /tmp/dashboard_sports.log 2>&1 &
    echo $! > /tmp/dashboard_sports.pid
    echo "       PID: $(cat /tmp/dashboard_sports.pid)"

    # 7. World State Dashboard (port 7050)
    echo -e "${GREEN}[7/12]${NC} Starting World State Dashboard on port 7050..."
    cd "$SCRIPT_DIR/world-state-dashboard"
    python3 -m uvicorn server:app --host 127.0.0.1 --port 7050 > /tmp/dashboard_world.log 2>&1 &
    echo $! > /tmp/dashboard_world.pid
    echo "       PID: $(cat /tmp/dashboard_world.pid)"
    cd "$SCRIPT_DIR"

    # 8. Voters Atlas Dashboard (port 7051)
    echo -e "${GREEN}[8/14]${NC} Starting Voters Atlas on port 7051..."
    cd "$SCRIPT_DIR/voters-dashboard"
    python3 -m uvicorn server:app --host 127.0.0.1 --port 7051 > /tmp/dashboard_voters.log 2>&1 &
    echo $! > /tmp/dashboard_voters.pid
    echo "       PID: $(cat /tmp/dashboard_voters.pid)"
    cd "$SCRIPT_DIR"

    # 9. Climate Change Dashboard (port 7052)
    echo -e "${GREEN}[9/14]${NC} Starting Climate Dashboard on port 7052..."
    PORT=7052 python3 "$SCRIPT_DIR/climate-dashboard/server.py" > /tmp/dashboard_climate.log 2>&1 &
    echo $! > /tmp/dashboard_climate.pid
    echo "       PID: $(cat /tmp/dashboard_climate.pid)"

    # 10. World Health Dashboard (port 7053)
    echo -e "${GREEN}[10/14]${NC} Starting World Health Dashboard on port 7053..."
    cd "$SCRIPT_DIR/world-health-dashboard"
    PORT=7053 python3 -m uvicorn server:app --host 127.0.0.1 --port 7053 > /tmp/dashboard_world_health.log 2>&1 &
    echo $! > /tmp/dashboard_world_health.pid
    echo "       PID: $(cat /tmp/dashboard_world_health.pid)"
    cd "$SCRIPT_DIR"

    # 11. Crypto Trackers Dashboard (port 7054)
    if [ -d "$SCRIPT_DIR/crypto-trackers-dashboard" ]; then
        echo -e "${GREEN}[11/14]${NC} Starting Crypto Trackers Dashboard on port 7054..."
        cd "$SCRIPT_DIR/crypto-trackers-dashboard"
        PORT=7054 python3 -m uvicorn server:app --host 127.0.0.1 --port 7054 > /tmp/dashboard_crypto_trackers.log 2>&1 &
        echo $! > /tmp/dashboard_crypto_trackers.pid
        echo "       PID: $(cat /tmp/dashboard_crypto_trackers.pid)"
        cd "$SCRIPT_DIR"
    else
        echo -e "${YELLOW}[11/14]${NC} Skipping crypto-trackers (crypto-trackers-dashboard/ not present on this host)"
    fi

    # 12. Eco Disasters Dashboard (port 7060)
    if [ -d "$SCRIPT_DIR/ecological-disasters-dashboard" ]; then
        echo -e "${GREEN}[12/14]${NC} Starting Eco Disasters Dashboard on port 7060..."
        cd "$SCRIPT_DIR/ecological-disasters-dashboard"
        PORT=7060 python3 -m uvicorn server:app --host 127.0.0.1 --port 7060 > /tmp/dashboard_disasters.log 2>&1 &
        echo $! > /tmp/dashboard_disasters.pid
        echo "       PID: $(cat /tmp/dashboard_disasters.pid)"
        cd "$SCRIPT_DIR"
    else
        echo -e "${YELLOW}[12/14]${NC} Skipping disasters (ecological-disasters-dashboard/ not present on this host)"
    fi

    # 13. Central Bank Dashboard (port 7061)
    echo -e "${GREEN}[13/14]${NC} Starting Central Bank Dashboard on port 7061..."
    cd "$SCRIPT_DIR/centralbank-dashboard"
    PORT=7061 python3 -m uvicorn server:app --host 127.0.0.1 --port 7061 > /tmp/dashboard_centralbank.log 2>&1 &
    echo $! > /tmp/dashboard_centralbank.pid
    echo "       PID: $(cat /tmp/dashboard_centralbank.pid)"
    cd "$SCRIPT_DIR"

    # 14. Gateway — starts last so upstreams are up first.
    # GATEWAY_PORT env var overrides config.json (avoids macOS AirPlay on 7000).
    echo -e "${GREEN}[14/14]${NC} Starting Gateway on port $GATEWAY_PORT..."
    cd "$SCRIPT_DIR/gateway"
    GATEWAY_PORT="$GATEWAY_PORT" python3 server.py > /tmp/dashboard_gateway.log 2>&1 &
    echo $! > /tmp/dashboard_gateway.pid
    echo "       PID: $(cat /tmp/dashboard_gateway.pid)"
    cd "$SCRIPT_DIR"

    sleep 2
    echo ""
    echo -e "${BLUE}=========================================${NC}"
    echo -e "${BLUE}  All dashboards running!${NC}"
    echo -e "${BLUE}=========================================${NC}"
    echo ""
    echo -e "  ${GREEN}Gateway (entry point):${NC} http://localhost:$GATEWAY_PORT"
    echo -e "  ${GREEN}Crypto Dashboard:${NC}      http://localhost:8000"
    echo -e "  ${GREEN}Stock Dashboard:${NC}       http://localhost:8050"
    echo -e "  ${GREEN}Midterm Dashboard:${NC}     http://localhost:8051"
    echo -e "  ${GREEN}Top Traders Dashboard:${NC} http://localhost:8052"
    echo -e "  ${GREEN}Weather Dashboard:${NC}     http://localhost:5050"
    echo -e "  ${GREEN}Sports Dashboard:${NC}      http://localhost:8888"
    echo -e "  ${GREEN}World State Dashboard:${NC} http://localhost:7050"
    echo -e "  ${GREEN}Voters Atlas:${NC}          http://localhost:7051"
    echo -e "  ${GREEN}Climate Dashboard:${NC}     http://localhost:7052"
    echo -e "  ${GREEN}World Health Dashboard:${NC} http://localhost:7053"
    echo -e "  ${GREEN}Crypto Trackers:${NC}       http://localhost:7054"
    echo -e "  ${GREEN}Eco Disasters Dashboard:${NC} http://localhost:7060"
    echo -e "  ${GREEN}Central Bank Dashboard:${NC} http://localhost:7061"
    echo ""
    echo -e "  Local subdomain test: http://crypto.localhost:$GATEWAY_PORT"
    echo -e "  Logs: /tmp/dashboard_*.log"
    echo -e "  Stop: ${YELLOW}$0 stop${NC}"
    echo ""
}

status() {
    echo ""
    echo -e "${BLUE}Dashboard Status:${NC}"
    echo -e "  Port $GATEWAY_PORT (Gateway):  $(lsof -ti :$GATEWAY_PORT >/dev/null 2>&1 && echo -e "${GREEN}RUNNING${NC}" || echo -e "${RED}STOPPED${NC}")"
    echo -e "  Port 8000 (Crypto):   $(lsof -ti :8000 >/dev/null 2>&1 && echo -e "${GREEN}RUNNING${NC}" || echo -e "${RED}STOPPED${NC}")"
    echo -e "  Port 8050 (Stock):    $(lsof -ti :8050 >/dev/null 2>&1 && echo -e "${GREEN}RUNNING${NC}" || echo -e "${RED}STOPPED${NC}")"
    echo -e "  Port 8051 (Midterm):  $(lsof -ti :8051 >/dev/null 2>&1 && echo -e "${GREEN}RUNNING${NC}" || echo -e "${RED}STOPPED${NC}")"
    echo -e "  Port 8052 (Traders):  $(lsof -ti :8052 >/dev/null 2>&1 && echo -e "${GREEN}RUNNING${NC}" || echo -e "${RED}STOPPED${NC}")"
    echo -e "  Port 5050 (Weather):  $(lsof -ti :5050 >/dev/null 2>&1 && echo -e "${GREEN}RUNNING${NC}" || echo -e "${RED}STOPPED${NC}")"
    echo -e "  Port 8888 (Sports):   $(lsof -ti :8888 >/dev/null 2>&1 && echo -e "${GREEN}RUNNING${NC}" || echo -e "${RED}STOPPED${NC}")"
    echo -e "  Port 7050 (World):    $(lsof -ti :7050 >/dev/null 2>&1 && echo -e "${GREEN}RUNNING${NC}" || echo -e "${RED}STOPPED${NC}")"
    echo -e "  Port 7051 (Voters):   $(lsof -ti :7051 >/dev/null 2>&1 && echo -e "${GREEN}RUNNING${NC}" || echo -e "${RED}STOPPED${NC}")"
    echo -e "  Port 7052 (Climate):  $(lsof -ti :7052 >/dev/null 2>&1 && echo -e "${GREEN}RUNNING${NC}" || echo -e "${RED}STOPPED${NC}")"
    echo -e "  Port 7053 (Health):   $(lsof -ti :7053 >/dev/null 2>&1 && echo -e "${GREEN}RUNNING${NC}" || echo -e "${RED}STOPPED${NC}")"
    echo -e "  Port 7054 (Trackers): $(lsof -ti :7054 >/dev/null 2>&1 && echo -e "${GREEN}RUNNING${NC}" || echo -e "${RED}STOPPED${NC}")"
    echo -e "  Port 7060 (Disasters):$(lsof -ti :7060 >/dev/null 2>&1 && echo -e "${GREEN}RUNNING${NC}" || echo -e "${RED}STOPPED${NC}")"
    echo -e "  Port 7061 (CB):       $(lsof -ti :7061 >/dev/null 2>&1 && echo -e "${GREEN}RUNNING${NC}" || echo -e "${RED}STOPPED${NC}")"
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
