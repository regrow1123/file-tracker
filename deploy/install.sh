#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BIN="${SCRIPT_DIR}/../build/file-tracker"

if [ ! -f "$BIN" ]; then
    echo "ERROR: build/file-tracker not found. Run 'make' first."
    exit 1
fi

echo "Installing file-tracker..."

# Stop any existing instances
systemctl stop file-tracker 2>/dev/null || true
pkill -x file-tracker 2>/dev/null || true
sleep 1

# Binary
install -m 0755 "$BIN" /usr/local/bin/file-tracker

# Config (don't overwrite existing)
mkdir -p /etc/file-tracker
if [ ! -f /etc/file-tracker/config.toml ]; then
    install -m 0644 "${SCRIPT_DIR}/config.toml" /etc/file-tracker/config.toml
    echo "  config: /etc/file-tracker/config.toml (edit broker address)"
else
    echo "  config: /etc/file-tracker/config.toml (kept existing)"
fi

# WAL directory
mkdir -p /var/lib/file-tracker

# systemd unit
install -m 0644 "${SCRIPT_DIR}/file-tracker.service" /etc/systemd/system/file-tracker.service
systemctl daemon-reload

echo ""
echo "Done. Next steps:"
echo "  1. Edit /etc/file-tracker/config.toml (set kafka brokers)"
echo "  2. systemctl enable --now file-tracker"
echo "  3. journalctl -u file-tracker -f"
