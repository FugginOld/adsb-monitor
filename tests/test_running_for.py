"""Characterization tests for the running-for (uptime-string) resolvers, through Host.

Time math is wall-clock-relative, so elapsed formatting is asserted loosely (\\d+[dhm]);
the degradation paths are asserted exactly.
"""
import re
import time
from datetime import datetime, timezone

import app as appmod
from app import Result

ELAPSED = re.compile(r'^\d+[dhm]')


# ── get_service_uptime_str ───────────────────────────────────────────────────
def test_service_uptime_recent_returns_string(fake_host):
    started = datetime.now(timezone.utc).strftime('%a %Y-%m-%d %H:%M:%S UTC')
    cmd = ('systemctl', 'show', 'readsb', '--property=ActiveEnterTimestamp')
    fake_host.commands = {cmd: Result(0, f'ActiveEnterTimestamp={started}', '')}
    out = appmod.get_service_uptime_str('readsb')
    assert out is not None and ELAPSED.match(out)


def test_service_uptime_na_is_none(fake_host):
    cmd = ('systemctl', 'show', 'readsb', '--property=ActiveEnterTimestamp')
    fake_host.commands = {cmd: Result(0, 'ActiveEnterTimestamp=n/a', '')}
    assert appmod.get_service_uptime_str('readsb') is None


def test_service_uptime_missing_is_none(fake_host):
    assert appmod.get_service_uptime_str('readsb') is None


# ── get_docker_uptime_str ────────────────────────────────────────────────────
def test_docker_uptime_recent_returns_string(fake_host):
    started = datetime.fromtimestamp(time.time() - 7200, timezone.utc).isoformat()
    cmd = ('docker', 'inspect', '--format', '{{.State.StartedAt}}', 'airnavradar')
    fake_host.commands = {cmd: Result(0, started, '')}
    out = appmod.get_docker_uptime_str('airnavradar')
    assert out == '2h 0m'


def test_docker_uptime_missing_is_none(fake_host):
    assert appmod.get_docker_uptime_str('airnavradar') is None
