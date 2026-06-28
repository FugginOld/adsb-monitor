#!/bin/bash
set -e

DEST=/opt/adsb-monitor

echo "==> Installing ADS-B monitor to ${DEST}..."
sudo mkdir -p ${DEST}/static

sudo cp app.py          ${DEST}/app.py
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
ExecStart=${DEST}/venv/bin/python app.py
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
