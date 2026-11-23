#!/usr/bin/env bash
# Run adsb-monitor as the dedicated, unprivileged `adsb-monitor` user.
#
#   sudo installer/service-user.sh enable    switch to the service user
#   sudo installer/service-user.sh disable   back to running as root
#   sudo installer/service-user.sh status
#
# enable:
#   - checks /opt/adsb-monitor (code + venv) is root-owned and not writable by
#     anyone else: the root helper imports that code, so it must stay root's;
#   - creates the system user (groups: systemd-journal for logs, plugdev for
#     airspy_info; NOT docker, which is root-equivalent);
#   - copies the monitor's data (history.db, feeders.ini, autotune*.json,
#     admin_auth.json) to /var/lib/adsb-monitor, owned by the user. The copies
#     in /opt stay as they were, untouched, as a fallback;
#   - installs /etc/sudoers.d/adsb-monitor allowing exactly
#       /opt/adsb-monitor/venv/bin/python -I /opt/adsb-monitor/helper.py <verb> ...
#     (every request is validated there: system/privileged.py);
#   - adds the drop-in adsb-monitor.service.d/service-user.conf (User=, data dir);
#   - restarts the monitor and checks it serves; if not, it undoes the drop-in
#     and sudoers file and restarts as root again.
# disable removes the drop-in and the sudoers file and restarts as root. The data
# stays in /var/lib/adsb-monitor (root reads it there too).
set -euo pipefail

SVC_USER=adsb-monitor
DEST=/opt/adsb-monitor
DATA=/var/lib/adsb-monitor
DROPIN_DIR=/etc/systemd/system/adsb-monitor.service.d
DROPIN=$DROPIN_DIR/service-user.conf
SUDOERS=/etc/sudoers.d/adsb-monitor
HELPER_CMD="$DEST/venv/bin/python -I $DEST/helper.py"
DATA_FILES=(history.db history.db-wal history.db-shm feeders.ini autotune.json autotune_state.json admin_auth.json)

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
. "$SCRIPT_DIR/lib.sh"

[ "$(id -u)" -eq 0 ] || { err "run with sudo"; exit 1; }

readonly_port() {
    systemctl show adsb-monitor -p Environment --value | tr ' ' '\n' \
        | sed -n 's/^READONLY_PORT=//p' | head -1
}

serving() {   # the read-only port answers /api/status within ~40 s
    local port; port=$(readonly_port); port=${port:-5001}
    for _ in $(seq 1 20); do
        if systemctl is-active -q adsb-monitor \
           && curl -fsS -o /dev/null --max-time 3 "http://127.0.0.1:${port}/api/status"; then
            return 0
        fi
        sleep 2
    done
    return 1
}

check_code_tree() {
    local bad
    # symlinks (the venv's python) always show 0777, so check files and dirs only
    bad=$(find "$DEST" ! -type l \( ! -user root -o -perm /022 \) -print 2>/dev/null | head -5 || true)
    if [ -n "$bad" ]; then
        err "these are not root-owned or are group/world-writable; fix before enabling:"
        echo "$bad"
        exit 1
    fi
    [ -f "$DEST/helper.py" ] || { err "$DEST/helper.py missing: deploy this version first (./update.sh)"; exit 1; }
}

remove_config() {
    rm -f "$DROPIN" "$SUDOERS"
    rmdir "$DROPIN_DIR" 2>/dev/null || true
    systemctl daemon-reload
}

enable() {
    check_code_tree
    if ! id "$SVC_USER" >/dev/null 2>&1; then
        info "Creating system user $SVC_USER"
        useradd --system --home-dir "$DATA" --no-create-home --shell /usr/sbin/nologin \
                --user-group "$SVC_USER"
    fi
    local groups="systemd-journal"
    getent group plugdev >/dev/null && groups="$groups plugdev"

    info "Stopping adsb-monitor to copy its data"
    systemctl stop adsb-monitor
    install -d -o "$SVC_USER" -g "$SVC_USER" -m 0750 "$DATA"
    for f in "${DATA_FILES[@]}"; do
        if [ -e "$DEST/$f" ] && [ ! -e "$DATA/$f" ]; then
            cp -p "$DEST/$f" "$DATA/$f"
        fi
        [ -e "$DATA/$f" ] && chown "$SVC_USER:$SVC_USER" "$DATA/$f"
    done
    [ -e "$DATA/admin_auth.json" ] && chmod 0600 "$DATA/admin_auth.json"
    ok "Data in $DATA (originals left in $DEST)"

    local tmp; tmp=$(mktemp)
    cat > "$tmp" <<EOF
# adsb-monitor: the only root command the service user may run. Every request
# is validated by $DEST/system/privileged.py. Installed by installer/service-user.sh.
# The poller reads docker status through it every 30 s, so sudo logs only refusals;
# the helper itself logs each request that changes something (auth log).
Defaults:$SVC_USER !log_allowed, !pam_session
$SVC_USER ALL=(root) NOPASSWD: $HELPER_CMD *
EOF
    if ! visudo -cqf "$tmp"; then
        rm -f "$tmp"; err "sudoers file failed visudo check; nothing changed"; systemctl start adsb-monitor; exit 1
    fi
    install -o root -g root -m 0440 "$tmp" "$SUDOERS"; rm -f "$tmp"

    install -d -m 0755 "$DROPIN_DIR"
    cat > "$DROPIN" <<EOF
# Installed by installer/service-user.sh; remove with: sudo installer/service-user.sh disable
[Service]
User=$SVC_USER
Group=$SVC_USER
SupplementaryGroups=$groups
Environment=ADSB_MONITOR_DATA=$DATA
EOF
    systemctl daemon-reload

    if ! sudo -u "$SVC_USER" sudo -n $HELPER_CMD ping >/dev/null 2>&1; then
        err "the service user can't reach the helper through sudo; rolling back"
        remove_config; systemctl start adsb-monitor; exit 1
    fi
    info "Starting adsb-monitor as $SVC_USER"
    systemctl start adsb-monitor
    if serving; then
        ok "adsb-monitor is running as $(ps -o user= -p "$(systemctl show -p MainPID --value adsb-monitor)")"
    else
        err "adsb-monitor did not come up as $SVC_USER; rolling back to root"
        journalctl -u adsb-monitor -n 20 --no-pager || true
        remove_config; systemctl restart adsb-monitor
        serving && ok "back on root" || err "still not serving: check journalctl -u adsb-monitor"
        exit 1
    fi
}

disable() {
    remove_config
    systemctl restart adsb-monitor
    if serving; then ok "adsb-monitor is running as root again (data stays in $DATA)"
    else err "not serving: check journalctl -u adsb-monitor"; exit 1; fi
}

status() {
    local pid; pid=$(systemctl show -p MainPID --value adsb-monitor)
    echo "running as: $( [ "${pid:-0}" != 0 ] && ps -o user= -p "$pid" || echo 'not running')"
    echo "drop-in:    $( [ -f "$DROPIN" ] && echo "$DROPIN" || echo none)"
    echo "sudoers:    $( [ -f "$SUDOERS" ] && echo "$SUDOERS" || echo none)"
    echo "data dir:   $( [ -d "$DATA" ] && echo "$DATA" || echo "$DEST (no $DATA)")"
    if id "$SVC_USER" >/dev/null 2>&1 && [ -f "$SUDOERS" ]; then
        sudo -u "$SVC_USER" sudo -n $HELPER_CMD ping >/dev/null 2>&1 \
            && echo "helper:     reachable" || echo "helper:     NOT reachable"
    fi
}

case "${1:-}" in
    enable)  enable ;;
    disable) disable ;;
    status)  status ;;
    *) echo "usage: sudo $0 enable|disable|status"; exit 64 ;;
esac
