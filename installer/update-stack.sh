#!/bin/bash

# ─────────────────────────────────────────────────────────────────────────────
# update-stack.sh — update the SDR stack and re-deploy the monitor (whiptail TUI).
#
# Pick which components to update (airspy_adsb, readsb+tar1090, graphs1090, the
# monitor); each is updated by re-running its upstream installer, except the
# monitor which is re-copied from this repo checkout. Preserves the monitor's
# feeders.ini and history.db. Pass --all to update everything non-interactively.
# Run with sudo. For dashboard-only updates use the repo-root update.sh instead.
# ─────────────────────────────────────────────────────────────────────────────

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=installer/lib.sh
source "$SCRIPT_DIR/lib.sh"

if [ "$EUID" -ne 0 ]; then err "Run with sudo: sudo ./update-stack.sh"; exit 1; fi

# Allow non-interactive --all or TUI selection
if [ "${1:-}" = "--all" ]; then
  TARGETS="airspy readsb graphs1090 monitor"
else
  ensure_tui
  TARGETS=$(whiptail --title "Update ADS-B Stack" --checklist \
"Choose components to UPDATE (SPACE toggles):" 16 70 5 \
"airspy"     "airspy_adsb decoder binary" ON \
"readsb"     "readsb + tar1090" ON \
"graphs1090" "graphs1090" ON \
"monitor"    "adsb-monitor (from this folder)" ON \
3>&1 1>&2 2>&3) || exit 0
  TARGETS=$(echo "$TARGETS" | tr -d '"')
fi

[ -z "$TARGETS" ] && { info "Nothing selected."; exit 0; }
clear
info "Updating: $TARGETS"

for target in $TARGETS; do
  case "$target" in
    airspy)
      if systemctl list-unit-files | grep -q airspy_adsb; then
        info "Updating airspy_adsb..."
        bash -c "$(wget -O - https://raw.githubusercontent.com/wiedehopf/airspy-conf/master/update-binary.sh)" >/dev/null 2>&1 && \
          ok "airspy_adsb updated" || warn "airspy_adsb update failed"
      else
        warn "airspy_adsb not installed, skipping"
      fi ;;
    readsb)
      info "Updating readsb + tar1090..."
      bash -c "$(wget -q -O - https://raw.githubusercontent.com/wiedehopf/adsb-scripts/master/readsb-install.sh)" >/dev/null 2>&1 && \
        ok "readsb + tar1090 updated" || warn "readsb update failed" ;;
    graphs1090)
      info "Updating graphs1090..."
      bash -c "$(curl -L -o - https://github.com/wiedehopf/graphs1090/raw/master/install.sh)" >/dev/null 2>&1 && \
        ok "graphs1090 updated" || warn "graphs1090 update failed" ;;
    monitor)
      info "Updating adsb-monitor..."
      DEST=/opt/adsb-monitor
      if [ ! -d "$DEST" ]; then warn "adsb-monitor not installed, skipping"; continue; fi
      # Preserve feeders.ini and history.db
      cp "$SCRIPT_DIR/../app.py" "$DEST/app.py"
      cp "$SCRIPT_DIR/../static/index.html" "$DEST/static/index.html"
      # Refresh venv deps in case requirements changed
      "$DEST/venv/bin/pip" install --quiet --upgrade flask psutil 2>/dev/null
      systemctl restart adsb-monitor
      ok "adsb-monitor updated (feeders.ini + history.db preserved)" ;;
  esac
done

# Show versions after update
echo ""
info "Current versions:"
[ -x /usr/local/bin/readsb ] && echo "  readsb:      $(readsb --version 2>&1 | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1)"
[ -f /usr/local/share/tar1090/git/version ] && echo "  tar1090:     $(cat /usr/local/share/tar1090/git/version)"
[ -f /usr/share/graphs1090/version ] && echo "  graphs1090:  $(cat /usr/share/graphs1090/version)"
command -v airspy_adsb >/dev/null && echo "  airspy_adsb: $(airspy_adsb --version 2>&1 | grep -oE 'v[0-9.]+\S*' | head -1)"

echo ""; ok "Update complete"
