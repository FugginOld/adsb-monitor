"""Characterization tests for the low-level config readers/writers, through the Host seam.

These lock current behaviour before/after migrating open()/subprocess onto HOST.
"""
import app as appmod
from app import Result


# ── read_flat_ini ────────────────────────────────────────────────────────────
def test_read_flat_ini_parses_and_strips_quotes(fake_host):
    fake_host.files = {'/etc/fr24feed.ini': 'fr24key="ABC"\n# comment\nhost=127.0.0.1\n'}
    vals = appmod.read_flat_ini('/etc/fr24feed.ini')
    assert vals == {'fr24key': 'ABC', 'host': '127.0.0.1'}


def test_read_flat_ini_missing_is_empty(fake_host):
    assert appmod.read_flat_ini('/nope') == {}


# ── write_flat_ini ───────────────────────────────────────────────────────────
def test_write_flat_ini_writes_keyvals(fake_host):
    appmod.write_flat_ini('/etc/fr24feed.ini', {'host': 'x', 'port': '30005'})
    written = fake_host.writes['/etc/fr24feed.ini']
    assert 'host=x' in written
    assert 'port=30005' in written


# ── read_shell_vars ──────────────────────────────────────────────────────────
def test_read_shell_vars_handles_export_and_quotes(fake_host):
    fake_host.files = {'/etc/default/adsbexchange-feed': 'export ADSBX_USER="joe"\nLAT=40.1\n'}
    vals = appmod.read_shell_vars('/etc/default/adsbexchange-feed')
    assert vals['ADSBX_USER'] == 'joe'
    assert vals['LAT'] == '40.1'


# ── write_shell_vars (merge into existing) ───────────────────────────────────
def test_write_shell_vars_updates_existing_key_and_appends_new(fake_host):
    fake_host.files = {'/etc/default/adsbexchange-feed': 'ADSBX_USER="old"\nKEEP=1\n'}
    appmod.write_shell_vars('/etc/default/adsbexchange-feed', {'ADSBX_USER': 'new', 'LAT': '40'})
    written = fake_host.writes['/etc/default/adsbexchange-feed']
    assert 'ADSBX_USER="new"' in written
    assert 'KEEP=1' in written          # untouched line preserved
    assert 'LAT="40"' in written        # new key appended


# ── read_piaware_config ──────────────────────────────────────────────────────
def test_read_piaware_config_parses_show_output(fake_host):
    fake_host.commands = {('piaware-config', '--show'):
                          Result(0, 'feeder-id abc-123\nreceiver-port 30005 (default)\n', '')}
    vals = appmod.read_piaware_config([])
    assert vals['feeder-id'] == 'abc-123'
    assert vals['receiver-port'] == '30005'   # first token only
