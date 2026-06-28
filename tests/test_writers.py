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
    ok, msg = appmod.set_feeder_settings('piaware', {'feeder-id': 'abc-123', 'empty': ''})
    assert ok is True
    issued = [c[1] for c in fake_host.calls if c[0] == 'run']
    assert ('piaware-config', 'feeder-id', 'abc-123') in issued
    # blank values are skipped
    assert all('empty' not in c for c in issued)


# ── write_airspy_options (read existing + write merged) ──────────────────────
def test_write_airspy_options_merges_and_writes(fake_host):
    fake_host.files = {appmod.AIRSPY_DEFAULT: 'GAIN=21\nSAMPLE_RATE=6\n'}
    appmod.write_airspy_options({'gain': '30', 'sample_rate': '12', 'options': '-v'})
    written = fake_host.writes[appmod.AIRSPY_DEFAULT]
    assert 'GAIN=30' in written
    assert 'SAMPLE_RATE=12' in written


# ── set_receiver route (read + write READSB_DEFAULT) ─────────────────────────
def test_route_set_receiver_writes(client, fake_host):
    fake_host.files = {appmod.READSB_DEFAULT: 'RECEIVER_OPTIONS="--lat 0 --lon 0 --max-range 100"\n'}
    fake_host.commands = {('systemctl', 'restart', 'readsb'): Result(0, 'ok', '')}
    r = client.post('/api/settings/receiver', json={'lat': '40.1', 'lon': '-74.2', 'max_range': '300'})
    body = r.get_json()
    assert body['ok'] is True
    written = fake_host.writes[appmod.READSB_DEFAULT]
    assert '40.1' in written and '300' in written
