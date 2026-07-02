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
sudo cp run.py            ${DEST}/run.py
# rm -rf then copy (not `cp -r system ${DEST}/system`): once ${DEST}/system
# already exists from a prior deploy, `cp -r` nests a copy inside it instead
# of overwriting, and stale files (e.g. a module removed/renamed upstream)
# are left behind — Python then loads whatever's already there.
sudo rm -rf ${DEST}/system ${DEST}/routes
sudo cp -r system         ${DEST}/system
sudo cp -r routes         ${DEST}/routes
sudo cp static/index.html ${DEST}/static/index.html
# Note: feeders.ini is NOT overwritten to preserve your customizations

# One-time migration: installs from before the system/routes split point
# systemd straight at app.py, which now crashes (system/*.py imports app.py
# as a module; running it directly as __main__ collides with that import).
# Repoint the unit at run.py if it's still on the old ExecStart.
UNIT=/etc/systemd/system/adsb-monitor.service
if [ -f "${UNIT}" ] && grep -q 'ExecStart=.*python app\.py' "${UNIT}"; then
  echo "==> Migrating systemd unit to run.py entry point..."
  sudo sed -i 's#python app\.py#python run.py#' "${UNIT}"
  sudo systemctl daemon-reload
fi

sudo systemctl restart adsb-monitor
echo "✓ Updated. Monitor at http://$(hostname -I | awk '{print $1}'):5000"
