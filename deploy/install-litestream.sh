#!/bin/bash
#
# Install Litestream + the narve replication service on the Ubuntu prod box.
# Run with sudo.
#
# Before running, create /etc/default/litestream containing:
#   LITESTREAM_ACCESS_KEY_ID=...
#   LITESTREAM_SECRET_ACCESS_KEY=...
#   LITESTREAM_BUCKET=narve-backups
#   LITESTREAM_ENDPOINT=https://s3.us-east-005.backblazeb2.com   # if using B2
#   LITESTREAM_REGION=us-east-005
#
# Then chmod 600 /etc/default/litestream
#
set -e

if [ "$(id -u)" -ne 0 ]; then echo "Error: must run as root (sudo)"; exit 1; fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# 1. Install the litestream binary if not already present.
if ! command -v litestream >/dev/null 2>&1; then
    echo "Installing litestream..."
    LS_VERSION="0.3.13"
    ARCH="$(dpkg --print-architecture)"   # amd64 / arm64
    cd /tmp
    curl -fsSL "https://github.com/benbjohnson/litestream/releases/download/v${LS_VERSION}/litestream-v${LS_VERSION}-linux-${ARCH}.tar.gz" -o litestream.tar.gz
    tar -xzf litestream.tar.gz
    install -m 0755 litestream /usr/local/bin/litestream
    rm -f litestream litestream.tar.gz
    echo "  Installed $(litestream version)"
else
    echo "litestream already installed: $(litestream version)"
fi

# 2. Place the config.
echo "Installing /etc/litestream.yml..."
install -m 0644 "$SCRIPT_DIR/litestream.yml" /etc/litestream.yml

# 3. Verify env file exists.
if [ ! -f /etc/default/litestream ]; then
    echo ""
    echo "WARNING: /etc/default/litestream not found."
    echo "  Create it with your S3/B2 credentials before starting the service."
    echo "  See header of $SCRIPT_DIR/install-litestream.sh for the format."
    echo ""
fi

# 4. Working dir for litestream metadata.
mkdir -p /var/lib/litestream
chown julianhabbig:julianhabbig /var/lib/litestream

# 5. Install + enable the service.
install -m 0644 "$SCRIPT_DIR/narve-litestream.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable narve-litestream

echo ""
echo "Done.  To start replication:"
echo "  sudo systemctl start narve-litestream"
echo ""
echo "To restore from backup:"
echo "  sudo systemctl stop narve-gateway narve-litestream"
echo "  sudo -u julianhabbig litestream restore -config /etc/litestream.yml /home/julianhabbig/Polymarket/gateway/auth.db"
echo "  sudo systemctl start narve-gateway narve-litestream"
echo ""
echo "Verify replication is working:"
echo "  litestream snapshots -config /etc/litestream.yml /home/julianhabbig/Polymarket/gateway/auth.db"
