"""What changed on the station, for the Timeline view's feed.

Three sources nothing else records go into `change_events` (system/db.py):

  restart  a unit or container started again: systemd's
           ActiveEnterTimestampMonotonic (or docker's StartedAt) moved since
           the last poll. Catches restarts from the dashboard, auto-tune,
           apt, the stack updater or a shell alike. The first poll after the
           monitor starts only learns the current values.
  config   a manual save or revert finished its verify-or-rollback check
           (system/apply_guard.py): verified, or rolled back.
  update   a stack update (system/stack_update.py) finished; deduped on the
           unit's exit timestamp, so it is stored once.

get_changes() in db.py merges these with gain steps, service up/down and
data-flow changes. Recording never raises: the feed is a convenience and must
not break a save or the poller.
"""
from __future__ import annotations

import logging
import threading
from typing import Any

import app
from system.db import record_change

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_markers: dict[str, str] = {}


def record(kind: str, subject: str, detail: str, level: str = 'info', ref: str | None = None) -> None:
    try:
        record_change(kind, subject, detail, level, ref)
    except Exception:
        logger.debug('could not record change %s %s', kind, subject, exc_info=True)


def _marker(feeder: dict[str, Any]) -> str | None:
    if feeder.get('kind') == 'docker':
        r = app.HOST.elevated('docker-inspect', feeder['key'], '{{.State.StartedAt}}', timeout=10)
        v = r.out.strip()
        return v if r.ok and v and not v.startswith('0001-') else None
    return app.INIT.start_marker(feeder['key'])


def detect_restarts(feeders: list[dict[str, Any]]) -> list[str]:
    """Record a 'restart' for each feeder whose start marker moved. Returns their keys."""
    restarted = []
    for f in feeders:
        try:
            m = _marker(f)
        except Exception:
            continue
        if m is None:
            continue
        with _lock:
            prev = _markers.get(f['key'])
            _markers[f['key']] = m
        if prev is not None and prev != m:
            restarted.append(f['key'])
            record('restart', f['key'], f'{f.get("label") or f["key"]} restarted')
    return restarted


def record_stack_updates() -> None:
    """Store each finished stack update once (deduped on its exit timestamp)."""
    from system import stack_update
    if not stack_update.supported():
        return
    for key, c in stack_update.COMPONENTS.items():
        try:
            st = stack_update.component_state(key)
        except Exception:
            continue
        if st['state'] not in ('success', 'failed') or not st.get('finished'):
            continue
        ok = st['state'] == 'success'
        record('update', key, f'{c["label"]} update ' + ('finished' if ok else 'failed'),
               'ok' if ok else 'error', ref=str(st['finished']))


def reset() -> None:
    """Forget start markers (tests)."""
    with _lock:
        _markers.clear()
