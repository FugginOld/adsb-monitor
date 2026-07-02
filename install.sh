#!/bin/bash

# ─────────────────────────────────────────────────────────────────────────────
# install.sh — minimal "monitor-only" installer.
#
# For when you already have readsb + feeders running and just want the dashboard.
# Copies the app into /opt/adsb-monitor, creates a Python venv with Flask,
# generates the systemd unit (with this host's IP), and enables the service.
# For a full SDR-stack install (decoder + feeders + monitor) use installer/install-stack.sh.
# Run with sudo. `set -e` aborts on the first error so a half-install can't go unnoticed.
# ─────────────────────────────────────────────────────────────────────────────

set -e

DEST=/opt/adsb-monitor

echo "==> Installing ADS-B monitor to ${DEST}..."
sudo mkdir -p ${DEST}/static

sudo cp app.py          ${DEST}/app.py
sudo cp run.py          ${DEST}/run.py
# rm -rf then copy: if ${DEST}/system already exists (re-running install.sh),
# `cp -r system ${DEST}/system` nests a copy inside it instead of overwriting,
# and stale/removed modules are left behind for Python to load by accident.
sudo rm -rf ${DEST}/system ${DEST}/routes
sudo cp -r system       ${DEST}/system
sudo cp -r routes       ${DEST}/routes
sudo cp feeders.ini     ${DEST}/feeders.ini
sudo cp static/index.html ${DEST}/static/index.html

if [ ! -d "${DEST}/venv" ]; then
  echo "==> Creating Python venv..."
  sudo python3 -m venv ${DEST}/venv
  sudo ${DEST}/venv/bin/pip install flask --quiet
else
  echo "==> venv already exists, skipping."
fi

echo "==> Installing systemd service..."
PI_IP=$(hostname -I | awk '{print $1}')
sudo tee /etc/systemd/system/adsb-monitor.service >/dev/null <<EOF
[Unit]
Description=ADS-B Stack Health Monitor
After=network.target readsb.service

[Service]
Type=simple
User=root
WorkingDirectory=${DEST}
ExecStart=${DEST}/venv/bin/python run.py
Restart=always
RestartSec=5
Environment="TAR1090_URL_LOCAL=http://${PI_IP}:8504"
Environment="GRAPHS1090_URL_LOCAL=http://${PI_IP}/graphs1090"
Environment="ADMIN_PORT=5000"
Environment="READONLY_PORT=5001"

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable --now adsb-monitor

echo ""
echo "✓ Done. Monitor running at http://${PI_IP}:5000"
echo ""
echo "To add/remove feeders, edit: ${DEST}/feeders.ini"
echo "Then restart: sudo systemctl restart adsb-monitor"
