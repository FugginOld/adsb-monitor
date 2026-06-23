"""Characterization tests for standalone file readers, driven through the Host seam.

Covers get_airspy_stats, get_readsb_deep_stats, _get_installed_version,
airspy_live_hint, and two read-only routes (airspy settings, aircraft types).
"""
import json
import os

import pytest

import app as appmod


@pytest.fixture
def client():
    appmod.app.config['TESTING'] = True
    return appmod.app.test_client()


# ── get_airspy_stats ──────────────────────────────────────────────────────────
def test_airspy_stats_returns_parsed_json(fake_host):
    fake_host.files = {appmod.AIRSPY_STATS: {'snr': {'median': 12}, 'gain': 21}}
    assert appmod.get_airspy_stats() == {'snr': {'median': 12}, 'gain': 21}


def test_airspy_stats_missing_returns_empty(fake_host):
    assert appmod.get_airspy_stats() == {}


# ── get_readsb_deep_stats ────────────────────────────────────────────────────
def test_readsb_deep_stats_computes_fields(fake_host):
    stats = {
        'last1min': {'messages_valid': 1200, 'position_count_total': 50,
                     'position_count_by_type': {'adsb_icao': 40}},
        'total': {'messages_valid': 99, 'position_count_total': 5,
                  'start': 0, 'end': 3600},
    }
    fake_host.files = {os.path.join(appmod.READSB_JSON, 'stats.json'): stats}
    out = appmod.get_readsb_deep_stats()
    assert out['msgs_last_min'] == 1200
    assert out['adsb_pct'] == 80.0          # 40/50 * 100
    assert out['uptime_hrs'] == 1.0


def test_readsb_deep_stats_missing_returns_empty(fake_host):
    assert appmod.get_readsb_deep_stats() == {}


# ── _get_installed_version (file-backed sources) ──────────────────────────────
def test_installed_version_single_file(fake_host):
    fake_host.files = {'/etc/foo.version': '1.2.3\n'}
    assert appmod._get_installed_version('foo', {'installed_file': '/etc/foo.version'}) == '1.2.3'


def test_installed_version_first_present_of_many(fake_host):
    fake_host.files = {'/etc/b.version': '9\n'}
    src = {'installed_files': ['/etc/a.version', '/etc/b.version']}
    assert appmod._get_installed_version('x', src) == '9'


def test_installed_version_missing_is_none(fake_host):
    assert appmod._get_installed_version('foo', {'installed_file': '/etc/missing'}) is None


# ── airspy_live_hint ──────────────────────────────────────────────────────────
def test_airspy_live_hint_reads_defaults(fake_host):
    fake_host.files = {appmod.AIRSPY_DEFAULT: 'SAMPLE_RATE=12\nGAIN=21'}
    hint = appmod.airspy_live_hint()
    assert '12 MSPS' in hint
    assert 'gain 21' in hint


# ── routes ────────────────────────────────────────────────────────────────────
def test_route_airspy_settings(client, fake_host):
    fake_host.files = {appmod.AIRSPY_DEFAULT: 'SAMPLE_RATE=12\nGAIN=21'}
    r = client.get('/api/settings/airspy')
    body = r.get_json()
    assert body['ok'] is True
    assert body['settings']['sample_rate'] == '12'
    assert body['settings']['gain'] == '21'


def test_route_aircraft_types(client, fake_host):
    stats = {'aircraft_count_by_type': {'adsb_icao': 7, 'mlat': 2, 'mode_s': 1}}
    fake_host.files = {os.path.join(appmod.READSB_JSON, 'stats.json'): stats}
    r = client.get('/api/stats/aircraft_types')
    body = r.get_json()
    assert body['adsb'] == 7
    assert body['mlat'] == 2
