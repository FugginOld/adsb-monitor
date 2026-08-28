"""Characterization tests for the Feeder status leaves, driven through the Host seam.

These lock the current behaviour of systemd_status / docker_status / service_action
before and after they are migrated onto HOST.
"""
import app as appmod
from app import Result


# ── systemd_status ──────────────────────────────────────────────────────────
def test_systemd_status_active_is_ok(fake_host):
    fake_host.commands = {('systemctl', 'is-active', 'readsb'): Result(0, 'active\n', '')}
    assert appmod.systemd_status('readsb') == ('ok', 'active')


def test_systemd_status_inactive_is_error(fake_host):
    fake_host.commands = {('systemctl', 'is-active', 'readsb'): Result(3, 'inactive\n', '')}
    assert appmod.systemd_status('readsb') == ('error', 'inactive')


def test_systemd_status_missing_binary_degrades(fake_host):
    # default_command (failed, empty) models systemctl absent
    assert appmod.systemd_status('readsb') == ('error', '')


# ── docker_status ────────────────────────────────────────────────────────────
def test_docker_status_running_is_ok(fake_host):
    cmd = ('docker', 'inspect', '--format', '{{.State.Status}}', 'airnavradar')
    fake_host.commands = {cmd: Result(0, 'running\n', '')}
    assert appmod.docker_status('airnavradar') == ('ok', 'running')


def test_docker_status_exited_is_error(fake_host):
    cmd = ('docker', 'inspect', '--format', '{{.State.Status}}', 'airnavradar')
    fake_host.commands = {cmd: Result(0, 'exited\n', '')}
    assert appmod.docker_status('airnavradar') == ('error', 'exited')


def test_docker_status_not_found_is_error(fake_host):
    # empty stdout == container absent
    assert appmod.docker_status('ghost') == ('error', 'not found')


# ── service_action ───────────────────────────────────────────────────────────
def test_service_action_success(fake_host):
    fake_host.commands = {('systemctl', 'restart', 'readsb'): Result(0, 'done', '')}
    ok, detail = appmod.service_action('readsb', 'restart')
    assert ok is True
    assert detail == 'done'


def test_service_action_failure_returns_combined_output(fake_host):
    fake_host.commands = {('systemctl', 'restart', 'readsb'): Result(1, 'out', 'err')}
    ok, detail = appmod.service_action('readsb', 'restart')
    assert ok is False
    assert detail == 'outerr'


def test_service_action_records_the_call(fake_host):
    appmod.service_action('readsb', 'stop')
    assert ('run', ('systemctl', 'stop', 'readsb')) in fake_host.calls


# ── unit-name validation (CodeQL py/command-line-injection, alert 28) ────────
def test_service_action_rejects_names_outside_the_charset(fake_host):
    for bad in ['readsb; rm -rf /', '../../etc/passwd', 'a b', '-x', '', 'a\nb']:
        ok, msg = appmod.service_action(bad, 'restart')
        assert (ok, msg) == (False, 'invalid service name'), bad
    assert fake_host.calls == []

def test_service_action_rejects_unknown_actions(fake_host):
    ok, msg = appmod.service_action('readsb', 'mask')
    assert (ok, msg) == (False, 'invalid action')
    assert fake_host.calls == []

def test_save_feeders_drops_entries_that_would_reach_systemctl(fake_host):
    appmod.save_feeders([
        {'kind': 'service', 'key': 'readsb'},
        {'kind': 'service', 'key': 'evil; rm -rf /'},
        {'kind': 'exec',    'key': 'readsb'},
    ])
    written = fake_host.files[appmod.CONFIG_FILE]
    assert '[service:readsb]' in written
    assert 'evil' not in written and 'exec' not in written

def test_run_keeps_exception_text_out_of_the_response(caplog):
    # py/stack-trace-exposure: Result.err is returned to the browser verbatim.
    r = appmod.LinuxHost().run(['definitely-not-a-real-binary-xyz'])
    assert not r.ok and r.err == 'command failed'
