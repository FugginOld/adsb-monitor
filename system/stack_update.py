"""Stack component updates from the dashboard (readsb, tar1090, graphs1090).

Runs the same upstream wiedehopf installers `installer/update-stack.sh` uses,
each as a transient systemd unit (`adsb-update-<component>`) so an update:
  - keeps running if the browser goes away or the monitor itself restarts;
  - logs to the journal, which the log viewer can follow;
  - leaves a result behind (Type=oneshot + RemainAfterExit) for the UI to show.

Only the fixed COMPONENTS table decides what runs. The request supplies a
component key and nothing else; the URL and arguments are passed to the runner
as positional parameters, never interpolated into shell text.

Installer modes (checked against the upstream scripts):
  readsb      readsb-install.sh binary-only: rebuild + replace /usr/bin/readsb
              and the unit file, restart readsb, exit. Skips the full-install
              steps that rewrite fr24feed.ini / rbfeeder.ini and create users.
              Leaves /etc/default/readsb alone (the installer only creates it
              when missing; the RTL-SDR overwrite needs the `push-30004` arg).
  tar1090     tar1090 install.sh /run/readsb: update the main tar1090 instance
              (what readsb-install.sh runs itself). Extra instances installed
              by feeder scripts (tar1090-adsbx, ...) are updated by those.
  graphs1090  graphs1090 install.sh.

One update at a time across all components. Requires systemd.
"""
from __future__ import annotations

import threading
from typing import Any

import app
from system.init_adapter import SystemdAdapter

UNIT_PREFIX = 'adsb-update-'

COMPONENTS: dict[str, dict[str, Any]] = {
    'readsb': {
        'label':   'readsb',
        'url':     'https://raw.githubusercontent.com/wiedehopf/adsb-scripts/master/readsb-install.sh',
        'args':    ['binary-only'],
        'impact':  'Rebuilds readsb from source (several minutes on a Pi) and restarts it: '
                   'every feeder loses data for about a minute.',
    },
    'tar1090': {
        'label':   'tar1090',
        'url':     'https://github.com/wiedehopf/tar1090/raw/master/install.sh',
        'args':    ['/run/readsb'],
        'impact':  'Updates the map web interface and restarts tar1090; open maps need a reload. '
                   'The feed is not interrupted.',
    },
    'graphs1090': {
        'label':   'graphs1090',
        'url':     'https://github.com/wiedehopf/graphs1090/raw/master/install.sh',
        'args':    [],
        'impact':  'Updates graphs1090 and restarts collectd; graph data is kept. '
                   'The feed is not interrupted.',
    },
}

# Positional params: $1 = URL, $2.. = installer args. Downloads to a temp file
# first (a truncated download must not be executed half-way), then runs it.
RUNNER = (
    'set -u; t=$(mktemp) || exit 1; trap \'rm -f "$t"\' EXIT; '
    'url=$1; shift; echo "[adsb-monitor] fetching $url"; '
    'if ! curl -fsSL --retry 2 -o "$t" "$url" || [ ! -s "$t" ]; then '
    'echo "[adsb-monitor] download failed: $url"; exit 1; fi; '
    'echo "[adsb-monitor] running installer $*"; bash "$t" "$@" </dev/null; rc=$?; '
    'echo "[adsb-monitor] installer exited with status $rc"; exit $rc'
)

_lock = threading.Lock()
_refreshed: dict[str, str] = {}   # component -> exit timestamp whose versions were refreshed


def unit_name(component: str) -> str:
    return UNIT_PREFIX + component

def supported() -> bool:
    return isinstance(app.INIT, SystemdAdapter)

def show_cmd(component: str) -> list[str]:
    return ['systemctl', 'show', unit_name(component), '-p', 'ActiveState', '-p', 'Result',
            '-p', 'ExecMainStatus', '-p', 'ExecMainStartTimestamp',
            '-p', 'ExecMainExitTimestamp', '-p', 'LoadState']

def _show(component: str) -> dict[str, str]:
    r = app.HOST.run(show_cmd(component), timeout=5)
    props: dict[str, str] = {}
    for line in r.out.splitlines():
        k, sep, v = line.partition('=')
        if sep: props[k.strip()] = v.strip()
    return props

def component_state(component: str) -> dict[str, Any]:
    """{'state': idle|running|success|failed, 'exit_status', 'started', 'finished'}."""
    p = _show(component)
    active = p.get('ActiveState', '')
    if p.get('LoadState') in ('', 'not-found') and active in ('', 'inactive'):
        state = 'idle'
    elif active == 'activating':
        state = 'running'
    elif active == 'active':
        state = 'success'
    elif active == 'failed':
        state = 'failed'
    else:
        state = 'idle'
    return {'state': state, 'result': p.get('Result') or None,
            'exit_status': p.get('ExecMainStatus') or None,
            'started': p.get('ExecMainStartTimestamp') or None,
            'finished': p.get('ExecMainExitTimestamp') or None}

def running_component() -> str | None:
    for key in COMPONENTS:
        if component_state(key)['state'] == 'running':
            return key
    return None

def start_update(component: str) -> tuple[bool, str]:
    if component not in COMPONENTS:
        return False, 'unknown component'
    if not supported():
        return False, 'updates from the dashboard need systemd; use installer/update-stack.sh'
    with _lock:
        busy = running_component()
        if busy:
            return False, f'an update of {COMPONENTS[busy]["label"]} is already running'
        unit = unit_name(component)
        # Clear the previous run's leftover unit (RemainAfterExit / failed) so the name is free.
        app.HOST.run(['systemctl', 'stop', unit], timeout=10)
        app.HOST.run(['systemctl', 'reset-failed', unit], timeout=10)
        c = COMPONENTS[component]
        cmd = ['systemd-run', f'--unit={unit}', '--no-block',
               f'--description=adsb-monitor: update {c["label"]}',
               '--property=Type=oneshot', '--property=RemainAfterExit=yes',
               '--property=TimeoutStartSec=45min', '--setenv=HOME=/root',
               '--working-directory=/tmp',
               '/bin/bash', '-c', RUNNER, 'adsb-update', c['url'], *c['args']]
        r = app.HOST.run(cmd, timeout=15)
        if not r.ok:
            return False, (r.err or r.out or 'systemd-run failed').strip()[:300]
        _refreshed.pop(component, None)
        return True, f'update of {c["label"]} started'

def status() -> dict[str, Any]:
    """Every component's last/ current run. Kicks off a version re-check once
    per finished run so the 'update' badge clears without waiting for the TTL."""
    from system.versions import refresh_versions
    out: dict[str, Any] = {}
    for key, c in COMPONENTS.items():
        st = component_state(key)
        if st['state'] in ('success', 'failed') and st['finished'] and _refreshed.get(key) != st['finished']:
            _refreshed[key] = st['finished']
            threading.Thread(target=refresh_versions, daemon=True).start()
        out[key] = {'label': c['label'], 'impact': c['impact'], **st}
    return out
