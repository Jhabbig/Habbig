#!/bin/bash
#
# Install all Narve dashboard systemd services.
# Run on the Ubuntu production box:  sudo bash deploy/install-services.sh
#
set -e

if [ "$(id -u)" -ne 0 ]; then echo "Error: must run as root (sudo)"; exit 1; fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# Live products only. Parked dashboards (storefront-hidden, services stopped)
# and merged ones keep their unit files under deploy/parked/ for reference.
SERVICES="narve-gateway narve-crypto narve-stock narve-weather narve-midterm"
# Units retired by the storefront trim: stopped + disabled on upgrade so an
# existing box actually sheds them. The gateway serves a parked notice (or a
# redirect for merged dashboards) on their subdomains.
PARKED="narve-sports narve-world narve-traders narve-centralbank narve-disasters narve-crypto-trackers narve-whale"

echo "Installing systemd service units..."
for svc in $SERVICES; do
    cp "$SCRIPT_DIR/$svc.service" /etc/systemd/system/
    echo "  Installed $svc.service"
done

echo "Stopping + disabling parked/merged services (if present)..."
for svc in $PARKED; do
    if systemctl list-unit-files "$svc.service" --no-legend 2>/dev/null | grep -q "$svc"; then
        systemctl disable --now "$svc" 2>/dev/null || true
        rm -f "/etc/systemd/system/$svc.service"
        echo "  Parked $svc (stopped, disabled, unit removed)"
    fi
done

echo "Reloading systemd daemon..."
systemctl daemon-reload

echo "Enabling services to start on boot..."
for svc in $SERVICES; do
    systemctl enable "$svc"
done

# Secure the environment file
if [ -f /home/julianhabbig/Polymarket/gateway/.env.production ]; then
    chmod 600 /home/julianhabbig/Polymarket/gateway/.env.production
    chown julianhabbig:julianhabbig /home/julianhabbig/Polymarket/gateway/.env.production
fi

echo ""
echo "Done. To start everything:"
echo "  sudo systemctl start narve-crypto narve-stock narve-weather narve-midterm"
echo "  sudo systemctl start narve-gateway"
echo ""
echo "To check status:"
echo "  systemctl status 'narve-*'"
echo ""
echo "Logs:"
echo "  journalctl -u narve-gateway -f"
