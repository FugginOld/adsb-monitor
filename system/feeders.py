"""Feeder last-seen + feeder probe.

"Last seen" = how long ago a feeder last actually produced output, which is
a better health signal than "is the service running". Most feeders expose a
status JSON whose modification time we read (FEEDER_STATUS_FILES); FR24 and
PiAware need their own parsing, so they get dedicated helpers.

Ties the readers together into one health record per feeder. `probe`
returns a FeederHealth (status, detail, last-seen, running-for) — the
single object the /api/status route renders into each sidebar card.
`readsb_metrics` pulls the headline numbers (aircraft, message rate, range)
for the metrics panel. Callers never branch on feeder kind — the dispatch
lives here once. See CONTEXT.md.

`HOST` / `READSB_JSON` stay defined in app.py, reached via `import app`.
"""
from __future__ import annotations

import os
import re
import time
from typing import Any, Callable, TypeVar, cast

import app
from system.db import get_service_uptime_str, get_docker_uptime_str
from system.services import docker_status

FEEDER_STATUS_FILES = {
    'adsbexchange-feed': '/run/adsbexchange-feed/status.json',
    'adsbfi-feed':       '/run/adsbfi-feed/status.json',
}

T = TypeVar('T')

def format_last_seen(age: float) -> str:
    """age seconds -> 'live' / 'Xm ago' / 'Xh ago'."""
    if age < 120:
        return 'live'
    elif age < 3600:
        return f'{int(age//60)}m ago'
    else:
        return f'{int(age//3600)}h ago'

def get_feeder_last_seen(key: str) -> str | None:
    path = FEEDER_STATUS_FILES.get(key)
    if not path:
        return None
    data = app.HOST.read_json(path)
    if not data:
        return None
    now_ts = data.get('now', 0)
    if now_ts:
        return format_last_seen(time.time() - now_ts)
    return None

def get_fr24_last_seen() -> str | None:
    r = app.HOST.run(['fr24feed-status'], timeout=5)
    m = re.search(r'connected.*?(\d+)\s*s', r.out + r.err, re.I)
    if m:
        return format_last_seen(int(m.group(1)))
    return None

def get_piaware_last_seen() -> str | None:
    r = app.HOST.run(['piaware-status'], timeout=5)
    if not r.out and not r.err:
        return None
    if 'is connected to FlightAware' in r.out:
        return 'live'
    return 'disconnected'

class FeederHealth:
    __slots__ = ('status', 'detail', 'last_seen', 'running_for')
    def __init__(self, status: str, detail: str, last_seen: str | None = None,
                 running_for: str | None = None):
        self.status, self.detail = status, detail
        self.last_seen, self.running_for = last_seen, running_for

def _dispatch_by_kind(feeder: dict[str, Any], if_service: Callable[[], T], if_docker: Callable[[], T]) -> T:
    """The one place a Feeder's kind decides which resolver runs."""
    return if_service() if feeder['kind'] == 'service' else if_docker()

def feeder_status(feeder: dict[str, Any]) -> tuple[str, str]:
    """The single kind-dispatch: (status, detail) for a service or docker Feeder."""
    def via_service() -> tuple[str, str]:
        return cast('tuple[str, str]', app.INIT.status(feeder['key']))
    def via_docker() -> tuple[str, str]:
        return docker_status(feeder['key'])
    return _dispatch_by_kind(feeder, via_service, via_docker)

def _feeder_last_seen(key: str) -> str | None:
    if key in FEEDER_STATUS_FILES:
        return get_feeder_last_seen(key)
    if key == 'fr24feed':
        return get_fr24_last_seen()
    if key == 'piaware':
        return get_piaware_last_seen()
    return None

def _feeder_running_for(feeder: dict[str, Any]) -> str | None:
    return _dispatch_by_kind(feeder,
        lambda: get_service_uptime_str(feeder['key']),
        lambda: get_docker_uptime_str(feeder['key']))

def probe(feeder: dict[str, Any]) -> FeederHealth:
    """Compose full Feeder health from the status, last-seen and running-for resolvers."""
    status, detail = feeder_status(feeder)
    return FeederHealth(status, detail,
                        last_seen=_feeder_last_seen(feeder['key']),
                        running_for=_feeder_running_for(feeder))

def readsb_metrics() -> dict[str, int]:
    metrics = {'aircraft': 0, 'msg_rate': 0, 'max_range_nm': 0}
    ac_data = app.HOST.read_json(os.path.join(app.READSB_JSON, 'aircraft.json'))
    if ac_data:
        aircraft = [a for a in ac_data.get('aircraft', []) if a.get('seen', 999) < 60]
        metrics['aircraft'] = len(aircraft)
        ranges = [a['r_dst'] for a in aircraft if 'r_dst' in a]
        if ranges: metrics['max_range_nm'] = round(max(ranges) * 0.539957)
    stats = app.HOST.read_json(os.path.join(app.READSB_JSON, 'stats.json'))
    if stats:
        msgs = stats.get('last1min', {}).get('messages_valid', 0)
        metrics['msg_rate'] = round(msgs / 60)
    return metrics
