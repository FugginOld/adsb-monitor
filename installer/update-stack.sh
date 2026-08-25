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
  # whiptail --checklist has no disabled row, so a host with no Airspy gets the
  # entry pre-unchecked and labelled instead of greyed out. Same detection the
  # airspy case below uses, so the menu can't disagree with what the run does.
  if systemctl list-unit-files | grep -q airspy_adsb; then
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

# ── Upstream installer runner ───────────────────────────────────────────────
# Fetch to a file, then run it. Replaces `bash -c "$(wget -O - URL)"`, which had
# two defects:
#   - a failed download expands to an empty string and `bash -c ''` exits 0, so
#     a network blip reported "updated" while nothing had been installed;
#   - output went to /dev/null, so a genuine build failure printed "update
#     failed" and discarded every clue as to why.
# The log keeps a normal run as quiet as before; the tail is shown only on
# failure, where the noise is the point.
# ────────────────────────────────────────────────────────────────────────────
UPDATE_LOG=$(mktemp /tmp/adsb-update-XXXXXX.log)

run_upstream() {   # run_upstream <label> <url>
  local label="$1" url="$2" script rc
  script=$(mktemp) || { warn "$label update failed — mktemp failed"; return 1; }
  if ! curl -fsSL -o "$script" "$url" || [ ! -s "$script" ]; then
    warn "$label update failed — could not fetch $url"
    rm -f "$script"; return 1
  fi
  printf '\n===== %s =====\n' "$label" >> "$UPDATE_LOG"
  bash "$script" >> "$UPDATE_LOG" 2>&1; rc=$?
  rm -f "$script"
  [ "$rc" -eq 0 ] && { ok "$label updated"; return 0; }
  warn "$label update failed (exit $rc) — last 20 lines:"
  tail -n 20 "$UPDATE_LOG" >&2
  warn "full log: $UPDATE_LOG"
  return 1
}

for target in $TARGETS; do
  case "$target" in
    airspy)
      if systemctl list-unit-files | grep -q airspy_adsb; then
        info "Updating airspy_adsb..."
        run_upstream "airspy_adsb" "https://raw.githubusercontent.com/wiedehopf/airspy-conf/master/update-binary.sh"
      else
        warn "airspy_adsb not installed, skipping"
      fi ;;
    readsb)
      info "Updating readsb + tar1090..."
      run_upstream "readsb + tar1090" "https://raw.githubusercontent.com/wiedehopf/adsb-scripts/master/readsb-install.sh" ;;
    graphs1090)
      info "Updating graphs1090..."
      run_upstream "graphs1090" "https://github.com/wiedehopf/graphs1090/raw/master/install.sh" ;;
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
      # Refresh venv deps in case requirements changed
      "$DEST/venv/bin/pip" install --quiet --upgrade flask psutil 2>/dev/null
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
