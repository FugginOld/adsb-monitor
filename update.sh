#!/bin/bash
set -e

DEST=/opt/adsb-monitor

echo "==> Updating ADS-B monitor files..."
sudo cp app.py            ${DEST}/app.py
sudo cp static/index.html ${DEST}/static/index.html
# Note: feeders.ini is NOT overwritten to preserve your customizations

sudo systemctl restart adsb-monitor
echo "✓ Updated. Monitor at http://$(hostname -I | awk '{print $1}'):5000"
