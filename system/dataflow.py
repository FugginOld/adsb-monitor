"""Feeder data flow: is each feeder actually sending data, not just running?

A unit can be `active` while its upload is stalled (aggregator unreachable,
lost Beast input, a hung client). This module asks each feeder's own status
source whether data is moving:

  adsbexchange-feed, adsbfi-feed  readsb feed clients: `/run/<key>/stats.prom`
                                  `readsb_net_connector_status{host=<remote>}`
                                  (seconds connected, 0 = down) plus the age of
                                  `/run/<key>/status.json`
  fr24feed                        `http://127.0.0.1:8754/monitor.json`:
                                  `feed_status` and `feed_last_ac_sent_time`
  piaware                         `/run/piaware/status.json`: `adept` (the
                                  FlightAware connection), refreshed every 5 s
  *-mlat (mlat-client units)      newest connect/disconnect/stats line in the
                                  unit's journal
  readsb                          `stats.json`: messages decoded in the last minute
  docker feeders                  the container's own health check

`observe()` is one read of those sources. `update()` (run by the poller every
30 s) keeps, per feeder, when data last stopped flowing: a feeder is `stale`
only after STALE_AFTER_S without data, so a short reconnect never raises an
alert. ok <-> stale transitions go to `history.db` (`dataflow_events`).
`current()` is what the routes read: the poller's latest result, or a fresh
check when the poller hasn't run recently (startup, tests).

States: ok, pending (no data, not yet for STALE_AFTER_S), stale (alert),
unknown (no source for this feeder, or the source itself is unreadable).
"""
from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass
from typing import Any

import app
from system.db import record_dataflow_event

STALE_AFTER_S = 5 * 60        # no data this long -> alert
FRESH_S = 120                 # a status file/stat older than this counts as "not updating"
MLAT_STATS_INTERVAL_S = 900   # mlat-client prints its stats block every 15 min
CACHE_MAX_AGE_S = 90          # current() re-checks if the poller's result is older

FEED_CLIENTS = ('adsbexchange-feed', 'adsbfi-feed')
FR24_MONITOR_URL = 'http://127.0.0.1:8754/monitor.json'
PIAWARE_STATUS = '/run/piaware/status.json'
_LOOPBACK = {'127.0.0.1', 'localhost', '::1'}
_CONNECTOR_RE = re.compile(r'^readsb_net_connector_status\{host="([^"]*)",port="(\d+)"\}\s+(\d+)', re.M)


@dataclass(frozen=True)
class Observation:
    ok: bool | None          # True = flowing, False = not flowing, None = can't tell
    detail: str = ''
    age_s: float | None = None   # seconds since data was last confirmed flowing


UNKNOWN = Observation(None)


# ── per-source readers ──────────────────────────────────────────────────────
def _feed_client(key: str, now: float) -> Observation:
    status = app.HOST.read_json(f'/run/{key}/status.json') or {}
    prom = app.HOST.read_text(f'/run/{key}/stats.prom')
    if not prom or not status.get('now'):
        return UNKNOWN
    age = max(0.0, now - float(status['now']))
    if age > FRESH_S:
        return Observation(False, f'feed client stopped updating ({_ago(age)})')
    remote = [(h, int(s)) for h, _p, s in _CONNECTOR_RE.findall(prom) if h not in _LOOPBACK]
    if not remote:
        return UNKNOWN
    up = [h for h, s in remote if s > 0]
    if up:
        return Observation(True, f'connected to {up[0]}', age)
    return Observation(False, f'not connected to {remote[0][0]}')


def _fr24(now: float) -> Observation:
    d = app.HOST.http_json(FR24_MONITOR_URL)
    if not isinstance(d, dict) or 'feed_status' not in d:
        return UNKNOWN
    status = str(d.get('feed_status') or '')
    if status != 'connected':
        msg = str(d.get('feed_status_message') or '').strip()
        return Observation(False, f'FR24 link {status or "down"}' + (f': {msg}' if msg else ''))
    try:
        sent_age = max(0.0, now - float(d.get('feed_last_ac_sent_time') or 0))
    except (TypeError, ValueError):
        return Observation(True, 'connected')
    try:
        tracked = int(d.get('feed_num_ac_tracked') or 0)
    except (TypeError, ValueError):
        tracked = 0
    if sent_age > FRESH_S and tracked > 0:
        return Observation(False, f'connected, but no aircraft sent for {_ago(sent_age)}')
    return Observation(True, 'connected', sent_age if sent_age <= FRESH_S else None)


def _piaware(now: float) -> Observation:
    d = app.HOST.read_json(PIAWARE_STATUS)
    if not isinstance(d, dict) or 'time' not in d:
        return UNKNOWN
    age = max(0.0, now - float(d['time']) / 1000)
    if age > FRESH_S:
        return Observation(False, f'piaware status not updated ({_ago(age)})')
    adept = d.get('adept') or {}
    if adept.get('status') == 'green':
        return Observation(True, str(adept.get('message') or 'connected'), age)
    return Observation(False, str(adept.get('message') or 'not connected to FlightAware'))


_MLAT_OK = ('Handshake complete', 'Connected to multilateration server')
_MLAT_BAD = ('Lost connection', 'Reconnecting in', 'Disconnected', 'failed:')


def _mlat_client(key: str, now: float) -> Observation:
    r = app.HOST.run(['journalctl', '-u', key, '-n', '200', '-o', 'short-unix', '--no-pager'], timeout=5)
    if not r.ok or not r.out:
        return UNKNOWN
    for line in reversed(r.out.splitlines()):
        ts_s, _, rest = line.partition(' ')
        msg = rest.split(': ', 1)[1] if ': ' in rest else ''
        try:
            ts = float(ts_s)
        except ValueError:
            continue
        if 'results connection' in msg:      # local output listeners, not the server link
            continue
        if msg.startswith('Server:'):
            state = msg.split()[1] if len(msg.split()) > 1 else ''
            if state != 'ready':
                return Observation(False, f'MLAT server: {state or "not ready"}')
        elif any(s in msg for s in _MLAT_BAD):
            return Observation(False, msg.strip()[:100])
        elif not any(s in msg for s in _MLAT_OK):
            continue
        age = max(0.0, now - ts)
        if age > 2 * MLAT_STATS_INTERVAL_S + FRESH_S:
            return Observation(False, f'no MLAT status for {_ago(age)}')
        return Observation(True, 'MLAT server ready', None)
    return UNKNOWN


def _readsb(now: float) -> Observation:
    import os
    s = app.HOST.read_json(os.path.join(app.READSB_JSON, 'stats.json'))
    if not isinstance(s, dict) or not s.get('now'):
        return UNKNOWN
    age = max(0.0, now - float(s['now']))
    if age > FRESH_S:
        return Observation(False, f'readsb stopped writing stats ({_ago(age)})')
    msgs = (s.get('last1min') or {}).get('messages_valid', (s.get('last1min') or {}).get('messages'))
    if msgs is None:
        return UNKNOWN
    if int(msgs) <= 0:
        return Observation(False, 'no messages decoded in the last minute')
    return Observation(True, f'{round(int(msgs) / 60)} msg/s', age)


def _docker(name: str) -> Observation:
    r = app.HOST.run(['docker', 'inspect', '--format',
                      '{{if .State.Health}}{{.State.Health.Status}}{{end}}', name], timeout=5)
    health = r.out.strip() if r.ok else ''
    if health == 'healthy':
        return Observation(True, 'container health check passing')
    if health == 'unhealthy':
        return Observation(False, 'container health check failing')
    return UNKNOWN


def observe(feeder: dict[str, Any], now: float | None = None) -> Observation:
    """One read of the feeder's own status source (no state kept)."""
    now = time.time() if now is None else now
    key = feeder['key']
    try:
        if feeder.get('kind') == 'docker':
            return _docker(key)
        if key in FEED_CLIENTS:
            return _feed_client(key, now)
        if key == 'fr24feed':
            return _fr24(now)
        if key == 'piaware':
            return _piaware(now)
        if key.endswith('-mlat'):
            return _mlat_client(key, now)
        if key == 'readsb':
            return _readsb(now)
    except Exception:  # a malformed status file must not break /api/status
        return UNKNOWN
    return UNKNOWN


# ── tracking over time ──────────────────────────────────────────────────────
_lock = threading.Lock()
_state: dict[str, dict[str, Any]] = {}   # key -> {'bad_since', 'flow', 'checked', 'recorded'}


def _ago(s: float) -> str:
    s = int(s)
    if s < 120:
        return f'{s}s'
    if s < 7200:
        return f'{s // 60}m'
    return f'{s // 3600}h'


def update(feeder: dict[str, Any], now: float | None = None) -> dict[str, Any]:
    """Observe, advance this feeder's no-data timer, record ok<->stale changes."""
    now = time.time() if now is None else now
    key = feeder['key']
    obs = observe(feeder, now)
    with _lock:
        st = _state.setdefault(key, {'bad_since': None, 'recorded': None})
        if obs.ok is False:
            st['bad_since'] = st['bad_since'] or now
        elif obs.ok is True:
            st['bad_since'] = None
        bad_for = now - st['bad_since'] if st['bad_since'] else 0.0
        if obs.ok is None:
            state = 'unknown'
        elif obs.ok:
            state = 'ok'
        else:
            state = 'stale' if bad_for >= STALE_AFTER_S else 'pending'
        detail = obs.detail
        if state in ('pending', 'stale') and bad_for >= 60:
            detail = f'{detail} (no data for {_ago(bad_for)})'
        flow = {'state': state, 'detail': detail, 'age_s': obs.age_s,
                'since': st['bad_since'] if state in ('pending', 'stale') else None}
        st.update(flow=flow, checked=now)
        to_record = None
        if state in ('ok', 'stale') and st['recorded'] != state:
            # first sighting of a healthy feeder isn't an event worth storing
            if not (st['recorded'] is None and state == 'ok'):
                to_record = state
            st['recorded'] = state
    if to_record:
        record_dataflow_event(key, to_record, obs.detail)
    return flow


def current(feeder: dict[str, Any]) -> dict[str, Any]:
    """Latest data-flow state for the routes; re-checks if the poller is behind."""
    with _lock:
        st = _state.get(feeder['key'])
        if st and st.get('flow') and time.time() - st['checked'] < CACHE_MAX_AGE_S:
            return dict(st['flow'])
    return update(feeder)


def last_seen(flow: dict[str, Any]) -> str | None:
    """Card label: 'live' / 'Xm ago' / 'no data', or None when there's no source."""
    state = flow.get('state')
    if state == 'ok':
        age = flow.get('age_s')
        if age is None or age < 120:
            return 'live'
        return f'{int(age // 60)}m ago' if age < 3600 else f'{int(age // 3600)}h ago'
    if state in ('pending', 'stale'):
        return 'no data'
    return None


def reset() -> None:
    """Forget all timers (tests)."""
    with _lock:
        _state.clear()
