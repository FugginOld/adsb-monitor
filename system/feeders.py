"""Feeder probe.

Ties the readers together into one health record per feeder. `probe`
returns a FeederHealth (status, detail, last-seen, running-for, data flow) — the
single object the /api/status route renders into each sidebar card.
Whether data is actually flowing (and so "last seen") comes from
system/dataflow.py, which reads each feeder's own status source.
`readsb_metrics` pulls the headline numbers (aircraft, message rate, range)
for the metrics panel. Callers never branch on feeder kind — the dispatch
lives here once. See CONTEXT.md.

`HOST` / `READSB_JSON` stay defined in app.py, reached via `import app`.
"""
from __future__ import annotations

import os
from typing import Any, Callable, TypeVar, cast

import app
from system import dataflow
from system.db import get_service_uptime_str, get_docker_uptime_str
from system.services import docker_status

T = TypeVar('T')

class FeederHealth:
    __slots__ = ('status', 'detail', 'last_seen', 'running_for', 'flow')
    def __init__(self, status: str, detail: str, last_seen: str | None = None,
                 running_for: str | None = None, flow: dict[str, Any] | None = None):
        self.status, self.detail = status, detail
        self.last_seen, self.running_for = last_seen, running_for
        self.flow = flow or {'state': 'unknown', 'detail': '', 'age_s': None, 'since': None, 'mlat': None}

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

def _feeder_running_for(feeder: dict[str, Any]) -> str | None:
    return _dispatch_by_kind(feeder,
        lambda: get_service_uptime_str(feeder['key']),
        lambda: get_docker_uptime_str(feeder['key']))

def probe(feeder: dict[str, Any]) -> FeederHealth:
    """Compose full Feeder health from the status, last-seen and running-for resolvers."""
    status, detail = feeder_status(feeder)
    flow = dataflow.current(feeder)
    return FeederHealth(status, detail,
                        last_seen=dataflow.last_seen(flow),
                        running_for=_feeder_running_for(feeder),
                        flow=flow)

def readsb_metrics() -> dict[str, int]:
    metrics = {'aircraft': 0, 'msg_rate': 0, 'max_range_nm': 0, 'max_range_15m_nm': 0}
    ac_data = app.HOST.read_json(os.path.join(app.READSB_JSON, 'aircraft.json'))
    if ac_data:
        aircraft = [a for a in ac_data.get('aircraft', []) if a.get('seen', 999) < 60]
        metrics['aircraft'] = len(aircraft)
        ranges = [a['r_dst'] for a in aircraft if 'r_dst' in a]
        # readsb's r_dst is already in nautical miles; no conversion.
        if ranges: metrics['max_range_nm'] = round(max(ranges))
    stats = app.HOST.read_json(os.path.join(app.READSB_JSON, 'stats.json'))
    if stats:
        msgs = stats.get('last1min', {}).get('messages_valid', 0)
        metrics['msg_rate'] = round(msgs / 60)
        # readsb's own 15-min maximum, as a cross-check on the live value.
        # Unlike r_dst this one is in metres.
        dist_m = stats.get('last15min', {}).get('max_distance')
        if isinstance(dist_m, (int, float)) and dist_m > 0:
            metrics['max_range_15m_nm'] = round(dist_m / 1852)
    return metrics
