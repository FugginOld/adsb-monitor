"""Characterization tests for the side-effecting + writer cluster, through the Host seam.

Centerpiece: the docker-recreate path proves we issue exactly the right
`docker run` args given an env update — a spy assertion only FakeHost makes possible.
"""
import json

import pytest

import app as appmod
from app import Result


@pytest.fixture
def client():
    appmod.app.config['TESTING'] = True
    return appmod.app.test_client()


# ── read_docker_env ──────────────────────────────────────────────────────────
def test_read_docker_env_parses_env_lines(fake_host):
    cmd = ('docker', 'inspect', '--format',
           '{{range .Config.Env}}{{println .}}{{end}}', 'airnavradar')
    fake_host.commands = {cmd: Result(0, 'SHARING_KEY=old\nLAT=1\nPATH=/usr/bin\n', '')}
    env = appmod.read_docker_env('airnavradar')
    assert env['SHARING_KEY'] == 'old'
    assert env['LAT'] == '1'


# ── set_feeder_settings: docker recreate (the spy test) ──────────────────────
def test_docker_recreate_issues_correct_run(fake_host):
    env_cmd = ('docker', 'inspect', '--format',
               '{{range .Config.Env}}{{println .}}{{end}}', 'airnavradar')
    inspect_cmd = ('docker', 'inspect', 'airnavradar')
    fake_host.commands = {
        env_cmd: Result(0, 'SHARING_KEY=old\nLAT=1\n', ''),
        inspect_cmd: Result(0, json.dumps([{
            'Config': {'Image': 'ghcr.io/airnav:latest'},
            'HostConfig': {'ExtraHosts': ['host.docker.internal:host-gateway']},
        }]), ''),
    }
    # stop / rm / run aren't pre-registered; let them succeed by default
    fake_host.default_command = Result(0, '', '')

    ok, msg = appmod.set_feeder_settings('airnavradar', {'SHARING_KEY': 'newkey', 'LAT': '40.5'})
    assert ok is True
    assert msg == 'Container recreated'

    runs = [c for c in fake_host.calls if c[0] == 'run']
    issued = [c[1] for c in runs]
    assert ('docker', 'stop', 'airnavradar') in issued
    # old container renamed aside (not deleted) before the new one runs
    assert ('docker', 'rename', 'airnavradar', 'airnavradar_bak') in issued
    # backup dropped once the new container is up
    assert ('docker', 'rm', '-f', 'airnavradar_bak') in issued

    run_cmd = next(c for c in issued if c[:3] == ('docker', 'run', '-d'))
    # updated env carried through as -e pairs, preserving existing keys/order
    assert '-e' in run_cmd and 'SHARING_KEY=newkey' in run_cmd
    assert 'LAT=40.5' in run_cmd
    # preserved image + extra host
    assert run_cmd[-1] == 'ghcr.io/airnav:latest'
    assert '--add-host' in run_cmd and 'host.docker.internal:host-gateway' in run_cmd


# ── set_feeder_settings: docker recreate rolls back a failed run ─────────────
def test_docker_recreate_rolls_back_on_run_failure(fake_host):
    env_cmd = ('docker', 'inspect', '--format',
               '{{range .Config.Env}}{{println .}}{{end}}', 'airnavradar')
    inspect_cmd = ('docker', 'inspect', 'airnavradar')
    fake_host.commands = {
        env_cmd: Result(0, 'SHARING_KEY=old\n', ''),
        inspect_cmd: Result(0, json.dumps([{
            'Config': {'Image': 'img:latest'}, 'HostConfig': {'ExtraHosts': []},
        }]), ''),
    }
    # everything else (stop/rename/run/start) fails -> `docker run` fails
    fake_host.default_command = Result(1, '', 'boom')

    ok, msg = appmod.set_feeder_settings('airnavradar', {'SHARING_KEY': 'newkey'})
    assert ok is False
    issued = [c[1] for c in fake_host.calls if c[0] == 'run']
    # the original is renamed aside, then restored and restarted on failure
    assert ('docker', 'rename', 'airnavradar', 'airnavradar_bak') in issued
    assert ('docker', 'rename', 'airnavradar_bak', 'airnavradar') in issued
    assert ('docker', 'start', 'airnavradar') in issued


# ── set_feeder_settings: piaware write-via-cmd path ──────────────────────────
def test_piaware_write_via_cmd_issues_config_commands(fake_host):
    fake_host.commands[('piaware-config', 'feeder-id', 'abc-123')] = Result(0, '', '')
    ok, msg = appmod.set_feeder_settings('piaware', {'feeder-id': 'abc-123', 'empty': ''})
    assert ok is True
    issued = [c[1] for c in fake_host.calls if c[0] == 'run']
    assert ('piaware-config', 'feeder-id', 'abc-123') in issued
    # blank values are skipped
    assert all('empty' not in c for c in issued)


# ── write_airspy_options (read existing + write merged) ──────────────────────
def test_write_airspy_options_merges_and_writes(fake_host):
    fake_host.files = {appmod.AIRSPY_DEFAULT: 'GAIN=21\nSAMPLE_RATE=6\n'}
    appmod.write_airspy_options({'gain': '15', 'sample_rate': '20', 'options': '-v'}, model='r2')
    lines = fake_host.writes[appmod.AIRSPY_DEFAULT].splitlines()
    assert 'GAIN=15' in lines and 'G=-g' in lines
    assert 'SAMPLE_RATE=20' in lines and 'M=-m' in lines
    for key in ('AFFINITY=', 'NET=', 'STATS='):   # unit crash-loops without them
        assert any(line.startswith(key) for line in lines)


# The live-host bug: GAIN=11 with G empty ran auto gain; M=-m on a Mini crashed the decoder.
MINI_CONF = ('GAIN=11\nSAMPLE_RATE=6\nOPTIONS="-v -f 1"\nAFFINITY=0\n'
             'NET="-l 47787:beast"\nSTATS="-S /run/airspy_adsb/stats.json"\nG=\nM=\n')

def test_write_airspy_sets_gain_flag_so_the_gain_is_used(fake_host):
    fake_host.files = {appmod.AIRSPY_DEFAULT: MINI_CONF}
    appmod.write_airspy_options({'gain': '21', 'sample_rate': '6', 'options': '-v -f 1'}, model='mini')
    lines = fake_host.writes[appmod.AIRSPY_DEFAULT].splitlines()
    assert 'G=-g' in lines and 'GAIN=21' in lines


def test_write_airspy_never_passes_m_flag_on_a_mini(fake_host):
    fake_host.files = {appmod.AIRSPY_DEFAULT: MINI_CONF}
    for model in ('mini', 'unknown'):
        appmod.write_airspy_options({'gain': '21', 'sample_rate': '12', 'options': '-v'}, model=model)
        lines = fake_host.writes[appmod.AIRSPY_DEFAULT].splitlines()
        assert 'M=' in lines and 'SAMPLE_RATE=' in lines  # no stray -m, no stray positional


def test_write_airspy_auto_gain_is_passed_as_dash_g_auto(fake_host):
    fake_host.files = {appmod.AIRSPY_DEFAULT: MINI_CONF}
    appmod.write_airspy_options({'gain': 'auto', 'options': '-v'}, model='mini')
    lines = fake_host.writes[appmod.AIRSPY_DEFAULT].splitlines()
    assert 'G=-g' in lines and 'GAIN=auto' in lines


@pytest.mark.parametrize('settings, model', [
    ({'gain': '30'}, 'mini'),                       # out of airspy range
    ({'gain': '21', 'sample_rate': '6'}, 'r2'),     # not a valid -m value
    ({'gain': '21', 'options': 'x" ; rm -rf /'}, 'mini'),
])
def test_write_airspy_rejects_values_the_decoder_would_choke_on(fake_host, settings, model):
    fake_host.files = {appmod.AIRSPY_DEFAULT: MINI_CONF}
    with pytest.raises(ValueError):
        appmod.write_airspy_options(settings, model=model)
    assert appmod.AIRSPY_DEFAULT not in fake_host.writes


def test_route_set_airspy_bad_gain_is_400_and_not_written(client, fake_host):
    fake_host.files = {appmod.AIRSPY_DEFAULT: MINI_CONF}
    r = client.post('/api/settings/airspy', json={'gain': '99'})
    assert r.status_code == 400 and r.get_json()['ok'] is False
    assert appmod.AIRSPY_DEFAULT not in fake_host.writes


def test_set_airspy_gain_touches_only_gain_lines():
    from system.sdr_settings import set_airspy_gain
    before, after = MINI_CONF.splitlines(), set_airspy_gain(MINI_CONF, '19').splitlines()
    assert len(after) == len(before)
    assert set(after) - set(before) == {'GAIN=19', 'G=-g'}
    assert set(before) - set(after) == {'GAIN=11', 'G='}


# ── set_receiver route (read + write READSB_DEFAULT) ─────────────────────────
def test_route_set_receiver_writes(client, fake_host):
    fake_host.files = {appmod.READSB_DEFAULT: 'RECEIVER_OPTIONS="--lat 0 --lon 0 --max-range 100"\n'}
    fake_host.commands = {('systemctl', 'restart', 'readsb'): Result(0, 'ok', '')}
    r = client.post('/api/settings/receiver', json={'lat': '40.1', 'lon': '-74.2', 'max_range': '300'})
    body = r.get_json()
    assert body['ok'] is True
    written = fake_host.writes[appmod.READSB_DEFAULT]
    assert '40.1' in written and '300' in written


def test_write_piaware_only_runs_declared_fields(fake_host):
    fake_host.default_command = Result(0, '', '')
    appmod.set_feeder_settings('piaware', {
        'receiver-host': '10.0.0.5', '--show': 'x', 'allow-manual-updates': 'yes'})
    runs = [c[1] for c in fake_host.calls if c[0] == 'run']
    assert runs == [('piaware-config', 'receiver-host', '10.0.0.5')]


def test_route_errors_are_fixed_strings_not_echoed_input(client, fake_host):
    fake_host.files = {appmod.AIRSPY_DEFAULT: MINI_CONF}
    r = client.post('/api/settings/airspy', json={'gain': '<b>99</b>'})
    assert r.status_code == 400 and r.get_json()['error'] == 'gain must be 0-21 or auto'
    r = client.post('/api/autotune', json={'max_gain': '<b>'})
    assert r.status_code == 400 and '<b>' not in r.get_json()['error']
    assert appmod.AUTOTUNE_FILE not in fake_host.writes
