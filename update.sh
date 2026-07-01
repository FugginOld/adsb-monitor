#!/bin/bash

# ─────────────────────────────────────────────────────────────────────────────
# update.sh — update just the dashboard (app.py + frontend), nothing else.
#
# Run after `git pull` from the repo dir. Copies the new code into /opt/adsb-monitor
# and restarts the service. Deliberately does NOT touch feeders.ini or history.db,
# so your feeder config and uptime history survive the update.
# For updating the SDR stack itself (readsb/tar1090/graphs1090) use installer/update-stack.sh.
# ─────────────────────────────────────────────────────────────────────────────

set -e

DEST=/opt/adsb-monitor

echo "==> Updating ADS-B monitor files..."
sudo cp app.py            ${DEST}/app.py
sudo cp -r system         ${DEST}/system
sudo cp -r routes         ${DEST}/routes
sudo cp static/index.html ${DEST}/static/index.html
# Note: feeders.ini is NOT overwritten to preserve your customizations

sudo systemctl restart adsb-monitor
echo "✓ Updated. Monitor at http://$(hostname -I | awk '{print $1}'):5000"
