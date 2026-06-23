"""Tests for InitAdapter hierarchy (items 6-7 of cross-platform plan).

Each class is tested through its public interface only; internals are free to change.
FakeHost from conftest is used for SystemdAdapter / OpenRCAdapter so their HOST calls
are intercepted without hitting the real OS.  NullAdapter needs no host at all.
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import app as appmod
from app import Result


# ── helpers ──────────────────────────────────────────────────────────────────

def _fake():
    return appmod.FakeHost()


# ── SystemdAdapter ────────────────────────────────────────────────────────────

class TestSystemdAdapterStatus:
    def test_active_service_returns_ok(self):
        host = _fake()
        host.commands = {('systemctl', 'is-active', 'readsb'): Result(0, 'active\n', '')}
        adapter = appmod.SystemdAdapter(host)
        status, state = adapter.status('readsb')
        assert status == 'ok'
        assert state == 'active'

    def test_inactive_service_returns_error(self):
        host = _fake()
        host.commands = {('systemctl', 'is-active', 'readsb'): Result(3, 'inactive\n', '')}
        adapter = appmod.SystemdAdapter(host)
        status, state = adapter.status('readsb')
        assert status == 'error'
        assert state == 'inactive'

    def test_failed_service_returns_error(self):
        host = _fake()
        host.commands = {('systemctl', 'is-active', 'readsb'): Result(3, 'failed\n', '')}
        adapter = appmod.SystemdAdapter(host)
        status, _ = adapter.status('readsb')
        assert status == 'error'


class TestSystemdAdapterAction:
    def test_successful_action_returns_true(self):
        host = _fake()
        host.commands = {('systemctl', 'restart', 'readsb'): Result(0, 'ok\n', '')}
        adapter = appmod.SystemdAdapter(host)
        ok, out = adapter.action('readsb', 'restart')
        assert ok is True
        assert 'ok' in out

    def test_failed_action_returns_false_with_output(self):
        host = _fake()
        host.commands = {('systemctl', 'restart', 'readsb'): Result(1, '', 'unit not found\n')}
        adapter = appmod.SystemdAdapter(host)
        ok, out = adapter.action('readsb', 'restart')
        assert ok is False
        assert 'unit not found' in out


class TestSystemdAdapterRunningSince:
    def test_parses_timestamp_to_human_string(self):
        host = _fake()
        # a timestamp well in the past so elapsed > 0
        host.commands = {
            ('systemctl', 'show', 'readsb', '--property=ActiveEnterTimestamp'):
                Result(0, 'ActiveEnterTimestamp=Mon 2020-01-01 00:00:00 UTC\n', '')
        }
        adapter = appmod.SystemdAdapter(host)
        result = adapter.running_since('readsb')
        # should be a non-empty string like "2300d 12h" or similar
        assert result is not None
        assert len(result) > 0

    def test_returns_none_for_missing_timestamp(self):
        host = _fake()
        host.commands = {
            ('systemctl', 'show', 'readsb', '--property=ActiveEnterTimestamp'):
                Result(0, 'ActiveEnterTimestamp=\n', '')
        }
        adapter = appmod.SystemdAdapter(host)
        assert adapter.running_since('readsb') is None

    def test_returns_none_on_host_failure(self):
        host = _fake()
        # default_command is Result(1, '', '') — systemctl unavailable
        adapter = appmod.SystemdAdapter(host)
        assert adapter.running_since('readsb') is None


# ── OpenRCAdapter ─────────────────────────────────────────────────────────────

class TestOpenRCAdapterStatus:
    def test_started_maps_to_ok(self):
        host = _fake()
        host.commands = {('rc-service', 'readsb', 'status'): Result(0, ' * status: started\n', '')}
        adapter = appmod.OpenRCAdapter(host)
        status, state = adapter.status('readsb')
        assert status == 'ok'

    def test_stopped_maps_to_error(self):
        host = _fake()
        host.commands = {('rc-service', 'readsb', 'status'): Result(3, ' * status: stopped\n', '')}
        adapter = appmod.OpenRCAdapter(host)
        status, _ = adapter.status('readsb')
        assert status == 'error'

    def test_state_string_returned(self):
        host = _fake()
        host.commands = {('rc-service', 'readsb', 'status'): Result(0, ' * status: started\n', '')}
        adapter = appmod.OpenRCAdapter(host)
        _, state = adapter.status('readsb')
        assert 'started' in state


class TestOpenRCAdapterAction:
    def test_delegates_to_rc_service(self):
        host = _fake()
        host.commands = {('rc-service', 'readsb', 'restart'): Result(0, '', '')}
        adapter = appmod.OpenRCAdapter(host)
        ok, _ = adapter.action('readsb', 'restart')
        assert ok is True
        assert ('run', ('rc-service', 'readsb', 'restart')) in host.calls

    def test_failed_action_returns_false(self):
        host = _fake()
        host.commands = {('rc-service', 'readsb', 'restart'): Result(1, '', 'error\n')}
        adapter = appmod.OpenRCAdapter(host)
        ok, out = adapter.action('readsb', 'restart')
        assert ok is False
        assert 'error' in out


class TestOpenRCAdapterRunningSince:
    def test_always_returns_none(self):
        host = _fake()
        adapter = appmod.OpenRCAdapter(host)
        assert adapter.running_since('readsb') is None


# ── NullAdapter ───────────────────────────────────────────────────────────────

class TestNullAdapter:
    def test_status_returns_error(self):
        adapter = appmod.NullAdapter()
        status, msg = adapter.status('anything')
        assert status == 'error'
        assert 'unavailable' in msg.lower()

    def test_action_returns_false(self):
        adapter = appmod.NullAdapter()
        ok, msg = adapter.action('anything', 'restart')
        assert ok is False
        assert 'unavailable' in msg.lower()

    def test_running_since_returns_none(self):
        adapter = appmod.NullAdapter()
        assert adapter.running_since('anything') is None


# ── detect_init ───────────────────────────────────────────────────────────────

class TestDetectInit:
    def test_systemctl_present_returns_systemd_adapter(self):
        host = _fake()
        host.commands = {('systemctl', '--version'): Result(0, 'systemd 249\n', '')}
        adapter = appmod.detect_init(host)
        assert isinstance(adapter, appmod.SystemdAdapter)

    def test_rc_service_present_returns_openrc_adapter(self):
        host = _fake()
        # systemctl fails, rc-service succeeds
        host.commands = {('rc-service', '--version'): Result(0, 'OpenRC 0.44\n', '')}
        adapter = appmod.detect_init(host)
        assert isinstance(adapter, appmod.OpenRCAdapter)

    def test_neither_present_returns_null_adapter(self):
        host = _fake()  # default_command = Result(1,'','') — all fail
        adapter = appmod.detect_init(host)
        assert isinstance(adapter, appmod.NullAdapter)


# ── Integration: callers use INIT ─────────────────────────────────────────────

class TestCallersUseINIT:
    def test_feeder_status_delegates_to_init(self, monkeypatch):
        fake_init = appmod.FakeInitAdapter(
            statuses={'readsb': ('ok', 'active')}
        )
        monkeypatch.setattr(appmod, 'INIT', fake_init)
        status, detail = appmod.feeder_status({'kind': 'service', 'key': 'readsb'})
        assert status == 'ok'
        assert detail == 'active'

    def test_service_action_delegates_to_init(self, monkeypatch):
        fake_init = appmod.FakeInitAdapter(
            actions={'readsb': (True, 'done')}
        )
        monkeypatch.setattr(appmod, 'INIT', fake_init)
        ok, out = appmod.service_action('readsb', 'restart')
        assert ok is True
        assert out == 'done'

    def test_null_adapter_service_action_returns_error_message(self, monkeypatch):
        monkeypatch.setattr(appmod, 'INIT', appmod.NullAdapter())
        ok, msg = appmod.service_action('anything', 'restart')
        assert ok is False
        assert 'unavailable' in msg.lower()
