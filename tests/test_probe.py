"""Tests for the Feeder probe — the deep module composing Feeder health.

These are the new interface-level tests; the resolver-level tests
(test_status, test_lastseen, test_running_for) remain as unit coverage underneath.
"""
import time

import app as appmod
from app import Result


# ── feeder_status (the single kind-dispatch) ─────────────────────────────────
def test_feeder_status_service(fake_host):
    fake_host.commands = {('systemctl', 'is-active', 'readsb'): Result(0, 'active', '')}
    assert appmod.feeder_status({'key': 'readsb', 'kind': 'service'}) == ('ok', 'active')


def test_feeder_status_docker(fake_host):
    cmd = ('docker', 'inspect', '--format', '{{.State.Status}}', 'airnavradar')
    fake_host.commands = {cmd: Result(0, 'running', '')}
    assert appmod.feeder_status({'key': 'airnavradar', 'kind': 'docker'}) == ('ok', 'running')


# ── probe: composes status + last_seen + running_for ─────────────────────────
def test_probe_service_with_statusfile_last_seen(fake_host):
    fake_host.commands = {('systemctl', 'is-active', 'adsbexchange-feed'): Result(0, 'active', '')}
    fake_host.files = {'/run/adsbexchange-feed/status.json': '{"now": %d}' % int(time.time())}
    h = appmod.probe({'key': 'adsbexchange-feed', 'kind': 'service'})
    assert h.status == 'ok'
    assert h.detail == 'active'
    assert h.last_seen == 'live'


def test_probe_fr24_last_seen_resolver(fake_host):
    fake_host.commands = {
        ('systemctl', 'is-active', 'fr24feed'): Result(0, 'active', ''),
        ('fr24feed-status',): Result(0, 'connected since 30 s', ''),
    }
    h = appmod.probe({'key': 'fr24feed', 'kind': 'service'})
    assert h.last_seen == 'live'


def test_probe_piaware_last_seen_resolver(fake_host):
    fake_host.commands = {
        ('systemctl', 'is-active', 'piaware'): Result(0, 'active', ''),
        ('piaware-status',): Result(0, 'piaware is connected to FlightAware', ''),
    }
    assert appmod.probe({'key': 'piaware', 'kind': 'service'}).last_seen == 'live'


def test_probe_no_last_seen_source_is_none(fake_host):
    fake_host.commands = {('systemctl', 'is-active', 'readsb'): Result(0, 'active', '')}
    h = appmod.probe({'key': 'readsb', 'kind': 'service'})
    assert h.last_seen is None


def test_probe_wires_running_for_for_docker(fake_host):
    from datetime import datetime, timezone
    started = datetime.fromtimestamp(time.time() - 3600, timezone.utc).isoformat()
    fake_host.commands = {
        ('docker', 'inspect', '--format', '{{.State.Status}}', 'airnavradar'): Result(0, 'running', ''),
        ('docker', 'inspect', '--format', '{{.State.StartedAt}}', 'airnavradar'): Result(0, started, ''),
    }
    h = appmod.probe({'key': 'airnavradar', 'kind': 'docker'})
    assert h.running_for == '1h 0m'


def test_probe_down_service_degrades(fake_host):
    # nothing registered -> default failed command everywhere
    h = appmod.probe({'key': 'ghost', 'kind': 'service'})
    assert h.status == 'error'
    assert h.last_seen is None
    assert h.running_for is None
