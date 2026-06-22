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
sudo cp adsb-monitor.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now adsb-monitor

echo ""
echo "✓ Done. Monitor running at http://$(hostname -I | awk '{print $1}'):5000"
echo ""
echo "To add/remove feeders, edit: ${DEST}/feeders.ini"
echo "Then restart: sudo systemctl restart adsb-monitor"
