"""Verify-or-rollback for manual config saves, and one-click revert.

Auto-tune already checks that airspy_adsb comes back after each gain change
and restores the old file if it doesn't (system/autotune.py). Manual saves get
the same protection here:

  prev = snapshot([paths the save may touch])
  ...write the new settings...
  check = restart_and_verify(unit, prev)

restart_and_verify restarts the unit and returns at once with a check id. A
background thread then requires the unit to stay active for STABLE_S and, for
the decoders, its stats.json to be rewritten after the restart (airspy_adsb
writes it every 60 s, readsb every 10 s). If that hasn't happened within the
unit's limit (VERIFY_LIMIT_S), every file that changed is put back as it was
(a file the save created is removed), systemd is reloaded if a drop-in was
involved, and the unit restarted again. The rollback never touches
`.adsb-monitor.bak`, so the backup stays the last good copy. If one of the
files is changed again while the check runs, the check stops ("superseded").

Revert (`managed_configs`, `revert`) restores a config from its
`.adsb-monitor.bak` through the same path; the version it replaces becomes the
new backup, so a revert can itself be reverted.

Checks live in memory: restarting the monitor mid-check drops the check
(the saved settings stay as written).
"""
from __future__ import annotations

import itertools
import logging
import os
import threading
import time
from typing import Any, Callable

import app
from system.init_adapter import BACKUP_SUFFIX
from system.services import service_action

logger = logging.getLogger(__name__)

VERIFY_LIMIT_S = {'airspy_adsb': 240, 'readsb': 60}
DEFAULT_LIMIT_S = 30
CHECK_EVERY_S = 5
STABLE_S = 10
KEEP_CHECKS = 20

_lock = threading.Lock()
_checks: dict[str, dict[str, Any]] = {}
_ids = itertools.count(1)


def snapshot(paths: list[str]) -> dict[str, str | None]:
    """Current contents of each path (None = file doesn't exist)."""
    return {p: app.HOST.read_text(p) for p in paths}


def _stats_path(unit: str) -> str | None:
    if unit == 'airspy_adsb':
        return app.AIRSPY_STATS
    if unit == 'readsb':
        return os.path.join(app.READSB_JSON, 'stats.json')
    return None


def _healthy(unit: str, since: float) -> tuple[bool, str]:
    status, detail = app.INIT.status(unit)
    if status != 'ok':
        return False, f'{unit} is {detail or "not active"}'
    stats_path = _stats_path(unit)
    if stats_path:
        s = app.HOST.read_json(stats_path)
        try:
            fresh = float((s or {}).get('now') or 0) > since
        except (TypeError, ValueError):
            fresh = False
        if not fresh:
            return False, f'{unit} has not written fresh stats since the restart'
    return True, 'ok'


def _restore(prev: dict[str, str | None], backups: dict[str, str] | None) -> None:
    reload = False
    for path, old in prev.items():
        if app.HOST.read_text(path) == old:
            continue
        if old is None:
            app.HOST.elevated('rm', path)
        else:
            app.HOST.write_text(path, old)          # no backup: .bak stays the last good copy
        reload = reload or path.startswith('/etc/systemd/')
    for path, data in (backups or {}).items():
        app.HOST.write_text(os.path.realpath(path) + BACKUP_SUFFIX, data)
    if reload:
        app.HOST.elevated('systemctl', 'daemon-reload')


_FEED_LEVEL = {'verified': 'ok', 'rolled_back': 'warn', 'rollback_failed': 'error'}


def _set(check: dict[str, Any], state: str, message: str) -> None:
    with _lock:
        check['state'], check['message'] = state, message
    if state in _FEED_LEVEL:            # the Timeline view's "what changed" feed
        from system import changes
        what = 'revert' if check.get('backups') else 'settings save'
        changes.record('config', check['unit'], f'{what}: {message}', _FEED_LEVEL[state])


def _rollback(check: dict[str, Any], why: str) -> None:
    unit = check['unit']
    _restore(check['prev'], check.get('backups'))
    ok, out = service_action(unit, 'restart')
    msg = f'{why}; restored the previous settings and restarted {unit}'
    if not ok:
        msg += f' (restart failed: {out.strip()[:120]})'
    logger.error('settings rollback for %s: %s', unit, why)
    _set(check, 'rolled_back' if ok else 'rollback_failed', msg)


def run_check(check: dict[str, Any], sleep_fn: Callable[[float], None] = time.sleep,
              now_fn: Callable[[], float] = time.time) -> None:
    """Watch one restart until verified, superseded or rolled back (blocks)."""
    unit, started = check['unit'], check['started']
    limit = VERIFY_LIMIT_S.get(unit, DEFAULT_LIMIT_S)
    ok_since: float | None = None
    why = f'{unit} did not come back'
    while True:
        sleep_fn(CHECK_EVERY_S)
        now = now_fn()
        if any(app.HOST.read_text(p) != new for p, new in check['new'].items()):
            _set(check, 'superseded', 'the settings were changed again before the check finished')
            return
        healthy, why = _healthy(unit, started)
        if healthy:
            ok_since = ok_since if ok_since is not None else now
            if now - ok_since >= STABLE_S:
                _set(check, 'verified', f'{unit} restarted and is running with the new settings')
                return
        else:
            ok_since = None
        if now - started >= limit:
            _rollback(check, f'{why} {int(now - started)} s after the save')
            return


def _start(check: dict[str, Any]) -> None:
    threading.Thread(target=run_check, args=(check,), daemon=True).start()


def restart_and_verify(unit: str, prev: dict[str, str | None],
                       backups: dict[str, str] | None = None) -> dict[str, Any]:
    """Restart `unit` after a save and verify it in the background.

    `prev`: snapshot() taken before writing. `backups`: .bak contents to put
    back on rollback (revert uses this). Returns the check's public view.
    """
    check = {'id': str(next(_ids)), 'unit': unit, 'started': time.time(), 'prev': prev,
             'new': snapshot(list(prev)), 'backups': backups,
             'state': 'verifying', 'message': f'checking that {unit} comes back'}
    with _lock:
        _checks[check['id']] = check
        for old in sorted(_checks, key=int)[:-KEEP_CHECKS]:
            del _checks[old]
    ok, out = service_action(unit, 'restart')
    if not ok:
        _rollback(check, f'restart failed: {out.strip()[:120]}')
    else:
        _start(check)
    return public(check)


def public(check: dict[str, Any]) -> dict[str, Any]:
    with _lock:
        return {'id': check['id'], 'unit': check['unit'], 'state': check['state'],
                'message': check['message'],
                'limit_s': VERIFY_LIMIT_S.get(check['unit'], DEFAULT_LIMIT_S)}


def get_check(check_id: str) -> dict[str, Any] | None:
    with _lock:
        c = _checks.get(check_id)
    return public(c) if c else None


# ── revert from .adsb-monitor.bak ────────────────────────────────────────────
def managed_configs() -> list[dict[str, Any]]:
    """Config files the monitor writes, with the unit to restart after a change."""
    from system.config_io import FEEDER_CONFIGS
    from system.sdr_settings import BIASTEE_1090_CONF
    items = [
        {'path': app.AIRSPY_DEFAULT, 'label': 'Airspy decoder', 'unit': 'airspy_adsb'},
        {'path': app.READSB_DEFAULT, 'label': 'readsb', 'unit': 'readsb'},
        {'path': BIASTEE_1090_CONF, 'label': '1090 bias-tee drop-in', 'unit': 'readsb'},
        {'path': app.DUMP978_DEFAULT, 'label': 'dump978-fa', 'unit': 'dump978-fa'},
    ]
    for key, cfg in FEEDER_CONFIGS.items():
        if cfg.get('config_file'):
            items.append({'path': cfg['config_file'], 'label': cfg.get('label', key), 'unit': key})
    items += [
        {'path': app.CONFIG_FILE, 'label': 'Sidebar feeders (feeders.ini)', 'unit': None},
        {'path': app.AUTOTUNE_FILE, 'label': 'Auto-tune settings', 'unit': None},
    ]
    return items


def list_backups() -> list[dict[str, Any]]:
    """Managed configs that have a backup differing from the current file (no contents)."""
    out = []
    for item in managed_configs():
        bak = os.path.realpath(item['path']) + BACKUP_SUFFIX
        data = app.HOST.read_text(bak)
        if data is None or data == app.HOST.read_text(item['path']):
            continue
        row = {**item, 'backup_mtime': app.HOST.mtime(bak)}
        if item['path'] == app.AIRSPY_DEFAULT:
            from system import autotune
            row['autotune_on'] = bool(autotune.load_config().get('enabled'))
        out.append(row)
    return out


def revert(requested: str) -> dict[str, Any]:
    """Restore a managed config file from its backup; the replaced version becomes
    the backup. Returns {'ok': False, 'error': ...} when it can't.

    `requested` only picks an entry of managed_configs(); every file operation uses
    that entry's own path, so nothing from the request reaches the filesystem."""
    item = next((i for i in managed_configs() if i['path'] == requested), None)
    if item is None:
        return {'ok': False, 'error': 'not a config file the monitor manages'}
    path: str = item['path']
    bak = app.HOST.read_text(os.path.realpath(path) + BACKUP_SUFFIX)
    if bak is None:
        return {'ok': False, 'error': 'no backup for this file'}
    prev = snapshot([path])
    if prev[path] == bak:
        return {'ok': False, 'error': 'the backup is the same as the current file'}
    app.HOST.write_text(path, bak, backup=True)
    if path.startswith('/etc/systemd/'):
        app.HOST.elevated('systemctl', 'daemon-reload')
    if not item['unit']:
        from system import changes
        changes.record('config', item['label'], 'restored from the backup')
        return {'ok': True, 'message': f'{item["label"]} restored from the backup'}
    check = restart_and_verify(item['unit'], prev, backups={path: bak})
    return {'ok': True, 'message': f'{item["label"]} restored; checking {item["unit"]}', 'check': check}
