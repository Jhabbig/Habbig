#!/bin/bash
#
# Install all Habbig dashboard systemd services.
# Run on the Ubuntu production box:  sudo bash deploy/install-services.sh
#
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SERVICES="habbig-gateway habbig-crypto habbig-weather habbig-sports habbig-world habbig-midterm habbig-traders habbig-stock"

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

echo ""
echo "Done. To start everything:"
echo "  sudo systemctl start habbig-crypto habbig-weather habbig-sports habbig-world habbig-midterm habbig-traders habbig-stock"
echo "  sudo systemctl start habbig-gateway"
echo ""
echo "To check status:"
echo "  systemctl status 'habbig-*'"
echo ""
echo "Logs:"
echo "  journalctl -u habbig-gateway -f"
