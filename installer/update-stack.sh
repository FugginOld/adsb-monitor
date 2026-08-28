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

# Is airspy_adsb installed on this host?
#
# Deliberately NOT `systemctl list-unit-files | grep -q airspy_adsb`: this script
# runs under `set -o pipefail`, grep -q exits at the first match, systemctl then
# dies of SIGPIPE (141), and pipefail reports that as the pipeline's status. The
# test therefore failed exactly when the unit WAS present — and only on hosts
# that have an Airspy, since a no-match run lets systemctl finish cleanly.
# Command substitution has no pipe to break.
has_airspy() { [ -n "$(systemctl list-unit-files airspy_adsb.service --no-legend 2>/dev/null)" ]; }

# Allow non-interactive --all or TUI selection
if [ "${1:-}" = "--all" ]; then
  TARGETS="airspy readsb graphs1090 monitor"
else
  ensure_tui
  # whiptail --checklist has no disabled row, so a host with no Airspy gets the
  # entry pre-unchecked and labelled instead of greyed out. Same detection the
  # airspy case below uses, so the menu can't disagree with what the run does.
  if has_airspy; then
    AIRSPY_DESC="airspy_adsb decoder binary"; AIRSPY_STATE=ON
  else
    AIRSPY_DESC="airspy_adsb decoder binary (not installed)"; AIRSPY_STATE=OFF
  fi
  TARGETS=$(whiptail --title "Update ADS-B Stack" --checklist \
"Choose components to UPDATE (SPACE toggles):" 16 70 5 \
"airspy"     "$AIRSPY_DESC" "$AIRSPY_STATE" \
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
      if has_airspy; then
        info "Updating airspy_adsb..."
        run_upstream "airspy_adsb update" "https://raw.githubusercontent.com/wiedehopf/airspy-conf/master/update-binary.sh" \
          && ok "airspy_adsb updated"
      else
        warn "airspy_adsb not installed, skipping"
      fi ;;
    readsb)
      info "Updating readsb + tar1090..."
      run_upstream "readsb + tar1090 update" "https://raw.githubusercontent.com/wiedehopf/adsb-scripts/master/readsb-install.sh" \
        && ok "readsb + tar1090 updated" ;;
    graphs1090)
      info "Updating graphs1090..."
      run_upstream "graphs1090 update" "https://github.com/wiedehopf/graphs1090/raw/master/install.sh" \
        && ok "graphs1090 updated" ;;
    monitor)
      info "Updating adsb-monitor..."
      DEST=/opt/adsb-monitor
      if [ ! -d "$DEST" ]; then warn "adsb-monitor not installed, skipping"; continue; fi
      # Preserve feeders.ini and history.db
      cp "$SCRIPT_DIR/../app.py" "$DEST/app.py"
      cp "$SCRIPT_DIR/../run.py" "$DEST/run.py"
      # rm -rf then copy: if $DEST/system already exists (a prior deploy),
      # `cp -r` nests a copy inside it instead of overwriting, and stale/
      # removed modules are left behind for Python to load by accident.
      rm -rf "$DEST/system" "$DEST/routes"
      cp -r "$SCRIPT_DIR/../system" "$DEST/system"
      cp -r "$SCRIPT_DIR/../routes" "$DEST/routes"
      # static/ is now a tree, not one file: the Tabler icon font is vendored under
      # static/vendor/ so the dashboard's icons work on an offline/firewalled box.
      # Same rm -rf-then-copy rule as system/ and routes/ — a plain `cp -r static
      # $DEST/static` nests a copy inside the existing dir on every deploy after
      # the first, and stale files are left behind.
      rm -rf "$DEST/static"
      cp -r "$SCRIPT_DIR/../static" "$DEST/static"
      # Refresh venv deps in case requirements changed. Not --quiet and not
      # 2>/dev/null: a failed upgrade here is silent, and the monitor then gets
      # restarted against whatever is left in the venv.
      "$DEST/venv/bin/pip" install --upgrade flask psutil || warn "pip upgrade failed - continuing with the installed versions"
      # One-time migration: older installs point systemd straight at app.py,
      # which now crashes (system/*.py imports app.py as a module; running
      # it directly as __main__ collides with that import).
      UNIT=/etc/systemd/system/adsb-monitor.service
      if [ -f "$UNIT" ] && grep -q 'ExecStart=.*python app\.py' "$UNIT"; then
        sed -i 's#python app\.py#python run.py#' "$UNIT"
        systemctl daemon-reload
      fi
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
