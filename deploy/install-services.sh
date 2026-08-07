#!/bin/bash
#
# Install all Narve dashboard systemd services.
# Run on the Ubuntu production box:  sudo bash deploy/install-services.sh
#
set -e

if [ "$(id -u)" -ne 0 ]; then echo "Error: must run as root (sudo)"; exit 1; fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=sites.conf
source "$SCRIPT_DIR/sites.conf"

echo "Installing systemd service units..."
for svc in "${SERVICES[@]}"; do
    cp "$SCRIPT_DIR/$svc.service" /etc/systemd/system/
    echo "  Installed $svc.service"
done

echo "Reloading systemd daemon..."
systemctl daemon-reload

# Units exec from deploy/venvs/<service>/ — build them before enabling, as
# the service user so ownership matches the rest of ~/Polymarket.
echo "Bootstrapping per-service venvs..."
sudo -u julianhabbig bash "$SCRIPT_DIR/bootstrap-venvs.sh"

echo "Enabling services to start on boot..."
for svc in "${SERVICES[@]}"; do
    systemctl enable "$svc"
done

# Secure the environment file
if [ -f /home/julianhabbig/Polymarket/gateway/.env.production ]; then
    chmod 600 /home/julianhabbig/Polymarket/gateway/.env.production
    chown julianhabbig:julianhabbig /home/julianhabbig/Polymarket/gateway/.env.production
fi

dash_services=()
for svc in "${SERVICES[@]}"; do
    [ "$svc" = "narve-gateway" ] || dash_services+=("$svc")
done

echo ""
echo "Done. To start everything:"
echo "  sudo systemctl start ${dash_services[*]}"
echo "  sudo systemctl start narve-gateway"
echo ""
echo "To check status:"
echo "  systemctl status 'narve-*'"
echo ""
echo "Logs:"
echo "  journalctl -u narve-gateway -f"
