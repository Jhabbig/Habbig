#!/bin/bash
#
# Install all Narve dashboard systemd services.
# Run on the Ubuntu production box:  sudo bash deploy/install-services.sh
#
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SERVICES="narve-gateway narve-crypto narve-weather narve-sports narve-world narve-midterm narve-traders narve-stock"

echo "Installing systemd service units..."
for svc in $SERVICES; do
    cp "$SCRIPT_DIR/$svc.service" /etc/systemd/system/
    echo "  Installed $svc.service"
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
echo "  sudo systemctl start narve-crypto narve-weather narve-sports narve-world narve-midterm narve-traders narve-stock"
echo "  sudo systemctl start narve-gateway"
echo ""
echo "To check status:"
echo "  systemctl status 'narve-*'"
echo ""
echo "Logs:"
echo "  journalctl -u narve-gateway -f"
