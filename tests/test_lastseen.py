"""Characterization tests for the Feeder last-seen leaves + readsb_metrics,
driven through the Host seam.
"""
import time

import app as appmod
from app import Result


# ── get_feeder_last_seen (reads /run/<key>/status.json) ──────────────────────
def test_feeder_last_seen_live(fake_host):
    path = '/run/adsbexchange-feed/status.json'
    fake_host.files = {path: '{"now": %d}' % int(time.time())}
    assert appmod.get_feeder_last_seen('adsbexchange-feed') == 'live'


def test_feeder_last_seen_minutes_ago(fake_host):
    path = '/run/adsbexchange-feed/status.json'
    fake_host.files = {path: '{"now": %d}' % int(time.time() - 600)}  # 10 min
    assert appmod.get_feeder_last_seen('adsbexchange-feed') == '10m ago'


def test_feeder_last_seen_hours_ago(fake_host):
    path = '/run/adsbfi-feed/status.json'
    fake_host.files = {path: '{"now": %d}' % int(time.time() - 7200)}  # 2 h
    assert appmod.get_feeder_last_seen('adsbfi-feed') == '2h ago'


def test_feeder_last_seen_unknown_key_is_none(fake_host):
    assert appmod.get_feeder_last_seen('not-a-feeder') is None


def test_feeder_last_seen_missing_file_is_none(fake_host):
    assert appmod.get_feeder_last_seen('adsbexchange-feed') is None


# ── get_fr24_last_seen ───────────────────────────────────────────────────────
def test_fr24_last_seen_live_when_recent(fake_host):
    fake_host.commands = {('fr24feed-status',): Result(0, 'connected since 30 s ago', '')}
    assert appmod.get_fr24_last_seen() == 'live'


def test_fr24_last_seen_minutes(fake_host):
    fake_host.commands = {('fr24feed-status',): Result(0, 'connected 300 s', '')}
    assert appmod.get_fr24_last_seen() == '5m ago'


def test_fr24_last_seen_no_match_is_none(fake_host):
    fake_host.commands = {('fr24feed-status',): Result(0, 'idle', '')}
    assert appmod.get_fr24_last_seen() is None


# ── get_piaware_last_seen ────────────────────────────────────────────────────
def test_piaware_last_seen_connected(fake_host):
    fake_host.commands = {('piaware-status',): Result(0, 'piaware is connected to FlightAware', '')}
    assert appmod.get_piaware_last_seen() == 'live'


def test_piaware_last_seen_disconnected(fake_host):
    fake_host.commands = {('piaware-status',): Result(0, 'not connected', '')}
    assert appmod.get_piaware_last_seen() == 'disconnected'


# ── readsb_metrics (reads aircraft.json + stats.json) ────────────────────────
def test_readsb_metrics_counts_recent_aircraft_and_range(fake_host):
    aircraft = {'aircraft': [
        {'seen': 5, 'r_dst': 100.0},   # ~54 nm
        {'seen': 10},                  # counted, no range
        {'seen': 999, 'r_dst': 999},   # stale, ignored
    ]}
    stats = {'last1min': {'messages_valid': 600}}
    import os
    fake_host.files = {
        os.path.join(appmod.READSB_JSON, 'aircraft.json'): aircraft,
        os.path.join(appmod.READSB_JSON, 'stats.json'): stats,
    }
    m = appmod.readsb_metrics()
    assert m['aircraft'] == 2
    assert m['max_range_nm'] == 54   # round(100 * 0.539957)
    assert m['msg_rate'] == 10       # round(600 / 60)


def test_readsb_metrics_missing_files_degrade_to_zero(fake_host):
    assert appmod.readsb_metrics() == {'aircraft': 0, 'msg_rate': 0, 'max_range_nm': 0}
