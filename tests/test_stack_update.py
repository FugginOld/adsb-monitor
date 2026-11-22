"""Stack updates: the systemd-run invocation, one-at-a-time locking, status
mapping, version re-check on completion, and the /api/stack routes."""
import pytest

import app as appmod
from app import Result
from system import stack_update


def show(fake_host, component, active, load='loaded', result='success', status='0',
         started='Sun 2026-11-22 13:00:00 EST', finished=''):
    out = (f'ActiveState={active}\nResult={result}\nExecMainStatus={status}\n'
           f'ExecMainStartTimestamp={started}\nExecMainExitTimestamp={finished}\nLoadState={load}\n')
    fake_host.commands[tuple(stack_update.show_cmd(component))] = Result(0, out, '')


def systemd_run_calls(fake_host):
    return [c[1] for c in fake_host.calls if c[0] == 'run' and c[1][0] == 'systemd-run']


@pytest.fixture
def client():
    appmod.app.config['TESTING'] = True
    return appmod.app.test_client()


@pytest.fixture(autouse=True)
def clear_refreshed():
    stack_update._refreshed.clear()
    yield
    stack_update._refreshed.clear()


# ── start_update ─────────────────────────────────────────────────────────────
def test_start_update_runs_installer_in_transient_unit(fake_host):
    fake_host.default_command = Result(0, '', '')   # unknown units: LoadState empty -> idle
    ok, msg = stack_update.start_update('readsb')
    assert ok, msg
    (cmd,) = systemd_run_calls(fake_host)
    assert '--unit=adsb-update-readsb' in cmd and '--no-block' in cmd
    assert '--property=Type=oneshot' in cmd and '--property=RemainAfterExit=yes' in cmd
    # URL and args are positional parameters after the fixed runner script, never in its text
    i = cmd.index(stack_update.RUNNER)
    assert list(cmd[i + 1:]) == ['adsb-update', stack_update.COMPONENTS['readsb']['url'], 'binary-only']
    assert 'http' not in stack_update.RUNNER
    # previous run's unit is cleared first
    assert ('run', ('systemctl', 'reset-failed', 'adsb-update-readsb')) in fake_host.calls


def test_readsb_update_uses_binary_only_mode():
    # The full readsb-install.sh run rewrites fr24feed.ini; binary-only doesn't.
    assert stack_update.COMPONENTS['readsb']['args'] == ['binary-only']
    assert stack_update.COMPONENTS['tar1090']['args'] == ['/run/readsb']


def test_start_update_refuses_while_another_runs(fake_host):
    fake_host.default_command = Result(0, '', '')
    show(fake_host, 'tar1090', 'activating')
    ok, msg = stack_update.start_update('readsb')
    assert not ok and 'tar1090' in msg
    assert systemd_run_calls(fake_host) == []


def test_start_update_unknown_component(fake_host):
    assert stack_update.start_update('rm -rf /')[0] is False
    assert systemd_run_calls(fake_host) == []


def test_start_update_needs_systemd(fake_host):
    appmod.INIT = appmod.NullAdapter()
    ok, msg = stack_update.start_update('readsb')
    assert not ok and 'systemd' in msg
    assert systemd_run_calls(fake_host) == []


def test_start_update_reports_systemd_run_failure(fake_host):
    fake_host.default_command = Result(0, '', '')
    stack_update.start_update('graphs1090')   # learn the exact command once
    cmd = systemd_run_calls(fake_host)[0]
    fake_host.commands[cmd] = Result(1, '', 'Failed to start transient service unit')
    ok, msg = stack_update.start_update('graphs1090')
    assert not ok and 'Failed to start' in msg


# ── status ───────────────────────────────────────────────────────────────────
@pytest.mark.parametrize('active, load, expected', [
    ('inactive', 'not-found', 'idle'),
    ('activating', 'loaded', 'running'),
    ('active', 'loaded', 'success'),
    ('failed', 'loaded', 'failed'),
])
def test_component_state_mapping(fake_host, active, load, expected):
    show(fake_host, 'readsb', active, load=load)
    assert stack_update.component_state('readsb')['state'] == expected


def test_status_refreshes_versions_once_per_finished_run(fake_host, monkeypatch):
    calls = []
    import system.versions as versions
    monkeypatch.setattr(versions, 'refresh_versions', lambda: calls.append(1))
    show(fake_host, 'readsb', 'active', finished='Sun 2026-11-22 13:05:00 EST')
    stack_update.status(); stack_update.status()
    import time; time.sleep(0.05)   # refresh runs on a daemon thread
    assert len(calls) == 1


# ── routes ───────────────────────────────────────────────────────────────────
def test_route_updates_lists_components(client, fake_host):
    body = client.get('/api/stack/updates').get_json()
    assert body['ok'] is True and body['supported'] is True
    assert set(body['components']) == {'readsb', 'tar1090', 'graphs1090'}


def test_route_update_unknown_is_404(client, fake_host):
    assert client.post('/api/stack/update/dump1090').status_code == 404


def test_route_update_busy_is_409(client, fake_host):
    fake_host.default_command = Result(0, '', '')
    show(fake_host, 'graphs1090', 'activating')
    r = client.post('/api/stack/update/readsb')
    assert r.status_code == 409 and 'graphs1090' in r.get_json()['error']


def test_route_update_starts(client, fake_host):
    fake_host.default_command = Result(0, '', '')
    r = client.post('/api/stack/update/tar1090')
    assert r.status_code == 200 and r.get_json()['ok'] is True
    assert len(systemd_run_calls(fake_host)) == 1


def test_route_logs_follow_the_update_unit(client, fake_host, monkeypatch):
    import routes.stack as stack_routes
    seen = []
    monkeypatch.setattr(stack_routes, 'stream_logs', lambda cmd: (seen.append(cmd) or iter(['data: x\n\n'])))
    r = client.get('/api/stack/update/readsb/logs')
    assert r.status_code == 200
    assert seen and '-u' in seen[0] and 'adsb-update-readsb' in seen[0]


@pytest.mark.parametrize('method, path', [
    ('post', '/api/stack/update/readsb'), ('get', '/api/stack/updates'),
    ('get', '/api/stack/update/readsb/logs'),
])
def test_routes_blocked_on_readonly_port(client, fake_host, method, path):
    appmod._request_port.port = appmod.READONLY_PORT
    try:
        r = getattr(client, method)(path)
    finally:
        appmod._request_port.port = appmod.ADMIN_PORT
    assert r.status_code == 403
    assert systemd_run_calls(fake_host) == []


def test_runner_reloads_systemd_after_installer_and_keeps_its_status():
    r = stack_update.RUNNER
    assert 'systemctl daemon-reload' in r
    # reload comes after the installer runs, and the unit still exits with the installer's status
    assert r.index('bash "$t" "$@"') < r.index('systemctl daemon-reload') < r.rindex('exit $rc')
