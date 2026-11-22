"""Characterization tests for standalone file readers, driven through the Host seam.

Covers get_airspy_stats, get_readsb_deep_stats, _get_installed_version,
airspy_live_hint, and two read-only routes (airspy settings, aircraft types).
"""
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


# ── gain_recommendation: saturation judged on p95, not lone max ───────────────
def test_gain_rec_lone_peak_does_not_trigger_saturation():
    # One aircraft overhead pegs max to 68; p95 is fine -> no decrease nag.
    stats = {'snr': {'median': 15}, 'noise': {'median': 33},
             'rssi': {'p95': 46, 'max': 68}, 'gain': 11}
    assert appmod.gain_recommendation(stats)['action'] == 'ok'


def test_gain_rec_high_p95_triggers_saturation():
    stats = {'snr': {'median': 15}, 'noise': {'median': 33},
             'rssi': {'p95': 68, 'max': 70}, 'gain': 13}
    rec = appmod.gain_recommendation(stats)
    assert rec['action'] == 'decrease'
    assert 'gain 11' in rec['reason']  # 13 - 2


def test_gain_rec_falls_back_to_max_without_p95():
    stats = {'snr': {'median': 15}, 'noise': {'median': 33},
             'rssi': {'max': 68}, 'gain': 13}
    assert appmod.gain_recommendation(stats)['action'] == 'decrease'


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
def test_airspy_live_hint_prefers_live_stats(fake_host):
    import time
    fake_host.files = {appmod.AIRSPY_DEFAULT: 'G=-g\nGAIN=21\nM=\nSAMPLE_RATE=6',
                       appmod.AIRSPY_STATS: {'now': time.time(), 'gain': 21.0, 'samplerate': 12}}
    hint = appmod.airspy_live_hint()
    assert '12 MSPS' in hint
    assert 'gain 21' in hint and '(auto)' not in hint


def test_airspy_live_hint_without_g_flag_is_auto(fake_host):
    # G empty -> GAIN is never passed to -g, so the decoder runs auto gain.
    fake_host.files = {appmod.AIRSPY_DEFAULT: 'GAIN=11\nSAMPLE_RATE=6\nG=\nM='}
    hint = appmod.airspy_live_hint()
    assert 'gain auto' in hint and 'default rate' in hint


def test_parse_airspy_reports_effective_values():
    from system.sdr_settings import parse_airspy_options as p
    assert p('GAIN=11\nG=\nSAMPLE_RATE=6\nM=')['gain'] == 'auto'
    assert p('GAIN=11\nG=\nSAMPLE_RATE=6\nM=')['sample_rate'] == ''
    assert p('GAIN=11\nG=-g\nSAMPLE_RATE=20\nM=-m') == {'gain': '11', 'sample_rate': '20', 'options': ''}


def test_gain_rec_uses_histogram_when_present():
    buckets = [0] * 80
    buckets[60], buckets[67], buckets[68] = 8000, 5500, 2300   # ~49% at/above 67 dB
    rec = appmod.gain_recommendation({'gain': 21.0, 'rssi_buckets': buckets,
                                      'snr': {'median': 8}, 'noise': {'median': 58},
                                      'rssi': {'p95': 68, 'max': 69}})
    assert rec['action'] == 'decrease' and 'try gain 18' in rec['reason']


def test_gain_rec_auto_gain_offers_no_number():
    rec = appmod.gain_recommendation({'gain': 'auto', 'snr': {'median': 15}, 'noise': {'median': 33},
                                      'rssi': {'p95': 68, 'max': 70}})
    assert rec['action'] == 'decrease' and 'try gain' not in rec['reason']


# ── routes ────────────────────────────────────────────────────────────────────
def test_route_airspy_settings(client, fake_host):
    fake_host.files = {appmod.AIRSPY_DEFAULT: 'G=-g\nGAIN=21\nM=-m\nSAMPLE_RATE=12'}
    r = client.get('/api/settings/airspy')
    body = r.get_json()
    assert body['ok'] is True
    assert body['settings']['sample_rate'] == '12'
    assert body['settings']['gain'] == '21'
    assert body['settings']['gain'] == '21'


def test_route_aircraft_types(client, fake_host):
    stats = {'aircraft_count_by_type': {'adsb_icao': 7, 'mlat': 2, 'mode_s': 1}}
    fake_host.files = {os.path.join(appmod.READSB_JSON, 'stats.json'): stats}
    r = client.get('/api/stats/aircraft_types')
    body = r.get_json()
    assert body['adsb'] == 7
    assert body['mlat'] == 2
