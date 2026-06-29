"""Tests for the Config store seam — adapter selection + get/set orchestration.

The existing test_writers.py already covers set_feeder_settings docker/piaware
behaviour; these add adapter-selection + the read side (get_feeder_settings).
"""
import app as appmod


# ── _config_adapter selection (once, used by both read and write) ─────────────
def test_adapter_selection_by_format(fake_host):
    assert appmod._config_adapter({'docker_container': 'x'}) is appmod.CONFIG_ADAPTERS['docker']
    assert appmod._config_adapter({'config_file': '/x', 'format': 'shell_vars'}) is appmod.CONFIG_ADAPTERS['shell_vars']
    assert appmod._config_adapter({'config_file': '/x'}) is appmod.CONFIG_ADAPTERS['ini_flat']  # default
    assert appmod._config_adapter({'write_via_cmd': True}) is appmod.CONFIG_ADAPTERS['piaware']
    assert appmod._config_adapter({'extra_files': {}}) is None


# ── get_feeder_settings: read via adapter + extra_files overlay + projection ──
def test_get_feeder_settings_ini_flat_with_defaults(fake_host):
    # fr24feed is ini_flat at /etc/fr24feed.ini
    fake_host.files = {'/etc/fr24feed.ini': 'fr24key=SECRET\nhost=1.2.3.4\n'}
    out = appmod.get_feeder_settings('fr24feed')
    assert out['fr24key'] == 'SECRET'
    assert out['host'] == '1.2.3.4'
    assert out['port'] == '30005'   # field default applied (absent in file)


def test_get_feeder_settings_extra_files_overlay(fake_host):
    # adsbfi-feed: no adapter, UUID comes from an extra_file
    fake_host.files = {'/usr/local/share/adsbfi/adsbfi-uuid': '  the-uuid  \n'}
    out = appmod.get_feeder_settings('adsbfi-feed')
    assert out['UUID'] == 'the-uuid'


def test_get_feeder_settings_unknown_key(fake_host):
    assert appmod.get_feeder_settings('nope') == {}


# ── set_feeder_settings: file path filters readonly; no-adapter rejected ──────
def test_set_feeder_settings_ini_flat_filters_and_merges(fake_host):
    fake_host.files = {'/etc/fr24feed.ini': 'fr24key=old\nhost=keep\n'}
    ok, msg = appmod.set_feeder_settings('fr24feed', {'fr24key': 'new', 'host': '9.9.9.9'})
    assert ok is True
    written = fake_host.writes['/etc/fr24feed.ini']
    assert 'fr24key=new' in written
    assert 'host=9.9.9.9' in written


def test_set_feeder_settings_no_write_method(fake_host):
    ok, msg = appmod.set_feeder_settings('adsbfi-feed', {'UUID': 'x'})
    assert ok is False
    assert msg == 'No write method defined'
