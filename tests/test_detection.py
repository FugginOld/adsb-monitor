"""Characterization tests for the detection leftovers, through the Host seam:
detect_airspy_model (lsusb / airspy_info), _get_installed_version (cmd), save_feeders.
"""
import app as appmod
from app import Result


# ── detect_airspy_model ──────────────────────────────────────────────────────
def test_detect_airspy_mini_from_lsusb(fake_host):
    fake_host.commands = {('lsusb',): Result(0, 'Bus 001 Device 004: ID 1d50:60a1 AirSpy', '')}
    assert appmod.detect_airspy_model() == 'mini'


def test_detect_airspy_falls_back_to_airspy_info(fake_host):
    fake_host.commands = {
        ('lsusb',): Result(0, 'nothing relevant', ''),
        ('airspy_info',): Result(0, 'Found AirSpy R820T mini board', ''),
    }
    assert appmod.detect_airspy_model() == 'mini'


def test_detect_airspy_unknown(fake_host):
    assert appmod.detect_airspy_model() == 'unknown'


# ── _get_installed_version (cmd branch) ──────────────────────────────────────
def test_installed_version_cmd_with_regex(fake_host):
    src = {'installed_cmd': ['readsb', '--version'], 'installed_re': r'readsb version:\s*([\d.]+)'}
    fake_host.commands = {('readsb', '--version'): Result(0, 'readsb version: 3.14.1592', '')}
    assert appmod._get_installed_version('readsb', src) == '3.14.1592'


def test_installed_version_cmd_missing_is_none(fake_host):
    src = {'installed_cmd': ['readsb', '--version']}
    # default failed command -> empty text -> None
    assert appmod._get_installed_version('readsb', src) is None


# ── save_feeders ─────────────────────────────────────────────────────────────
def test_save_feeders_writes_sections(fake_host):
    appmod.save_feeders([
        {'key': 'readsb', 'kind': 'service', 'label': 'readsb', 'hint': 'h', 'icon': 'broadcast'},
        {'key': 'airnavradar', 'kind': 'docker'},
    ])
    written = fake_host.writes[appmod.CONFIG_FILE]
    assert '[service:readsb]' in written
    assert '[docker:airnavradar]' in written
    assert 'label = readsb' in written
