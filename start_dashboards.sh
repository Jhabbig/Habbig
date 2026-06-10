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
#
# Parked dashboards (sports, world, top-traders, centralbank, crypto-trackers,
# religion, airace, whale) are no longer launched — the gateway serves a
# parked notice on their subdomains and /admin/fleet tracks them.
#

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# Colors
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

ALL_PORTS="7000 8000 8050 8051 5050"

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
    echo -e "${GREEN}[1/5]${NC} Starting Crypto Dashboard on port 8000..."
    python3 "$SCRIPT_DIR/crypto-dashboard/server.py" > /tmp/dashboard_crypto.log 2>&1 &
    echo $! > /tmp/dashboard_crypto.pid
    echo "       PID: $(cat /tmp/dashboard_crypto.pid)"

    # 2. Stock Dashboard (port 8050)
    echo -e "${GREEN}[2/5]${NC} Starting Stock Dashboard on port 8050..."
    python3 "$SCRIPT_DIR/stock-dashboard/stock_dashboard.py" --port 8050 > /tmp/dashboard_stock.log 2>&1 &
    echo $! > /tmp/dashboard_stock.pid
    echo "       PID: $(cat /tmp/dashboard_stock.pid)"

    # 3. Midterm Prediction Dashboard (port 8051)
    echo -e "${GREEN}[3/5]${NC} Starting Midterm Dashboard on port 8051..."
    cd "$SCRIPT_DIR/midterm-dashboard/backend"
    python3 main.py > /tmp/dashboard_midterm.log 2>&1 &
    echo $! > /tmp/dashboard_midterm.pid
    echo "       PID: $(cat /tmp/dashboard_midterm.pid)"
    cd "$SCRIPT_DIR"

    # 4. Weather Dashboard (port 5050)
    echo -e "${GREEN}[4/5]${NC} Starting Weather Dashboard on port 5050..."
    python3 "$SCRIPT_DIR/polymarket_weather_dashboard/server.py" > /tmp/dashboard_weather.log 2>&1 &
    echo $! > /tmp/dashboard_weather.pid
    echo "       PID: $(cat /tmp/dashboard_weather.pid)"

    # 5. Gateway (port 7000) — starts last so upstreams are up first
    echo -e "${GREEN}[5/5]${NC} Starting Gateway on port 7000..."
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
    echo -e "  ${GREEN}Crypto Dashboard:${NC}      http://localhost:8000"
    echo -e "  ${GREEN}Stock Dashboard:${NC}       http://localhost:8050"
    echo -e "  ${GREEN}Midterm Dashboard:${NC}     http://localhost:8051"
    echo -e "  ${GREEN}Weather Dashboard:${NC}     http://localhost:5050  (incl. /disasters + /climate)"
    echo ""
    echo -e "  Local subdomain test: http://crypto.localhost:7000"
    echo -e "  Logs: /tmp/dashboard_*.log"
    echo -e "  Stop: ${YELLOW}$0 stop${NC}"
    echo ""
}

status() {
    echo ""
    echo -e "${BLUE}Dashboard Status:${NC}"
    echo -e "  Port 7000 (Gateway):  $(lsof -ti :7000 >/dev/null 2>&1 && echo -e "${GREEN}RUNNING${NC}" || echo -e "${RED}STOPPED${NC}")"
    echo -e "  Port 8000 (Crypto):   $(lsof -ti :8000 >/dev/null 2>&1 && echo -e "${GREEN}RUNNING${NC}" || echo -e "${RED}STOPPED${NC}")"
    echo -e "  Port 8050 (Stock):    $(lsof -ti :8050 >/dev/null 2>&1 && echo -e "${GREEN}RUNNING${NC}" || echo -e "${RED}STOPPED${NC}")"
    echo -e "  Port 8051 (Midterm):  $(lsof -ti :8051 >/dev/null 2>&1 && echo -e "${GREEN}RUNNING${NC}" || echo -e "${RED}STOPPED${NC}")"
    echo -e "  Port 5050 (Weather):  $(lsof -ti :5050 >/dev/null 2>&1 && echo -e "${GREEN}RUNNING${NC}" || echo -e "${RED}STOPPED${NC}")"
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
