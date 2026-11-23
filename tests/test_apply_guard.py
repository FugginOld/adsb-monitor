"""Verify-or-rollback for manual saves and one-click revert (system/apply_guard.py)."""
import os

import pytest

import app as appmod
from fakes import FakeInitAdapter
from system import apply_guard
from system.init_adapter import BACKUP_SUFFIX
from system.sdr_settings import BIASTEE_1090_CONF

READSB_STATS = os.path.join(appmod.READSB_JSON, 'stats.json')


class Clock:
    """Fake time for run_check: each sleep advances the clock; `on_tick` lets a
    test change the world as time passes."""
    def __init__(self, start, on_tick=None):
        self.t, self.on_tick = start, on_tick
    def now(self):
        return self.t
    def sleep(self, s):
        self.t += s
        if self.on_tick:
            self.on_tick(self.t)


@pytest.fixture
def init(monkeypatch):
    fake = FakeInitAdapter(statuses={}, actions={u: (True, '') for u in ('readsb', 'airspy_adsb', 'dump978-fa')})
    monkeypatch.setattr(appmod, 'INIT', fake)
    return fake


def set_status(init, unit, status):
    init.statuses[unit] = (status, 'active' if status == 'ok' else 'failed')


def save(fake_host, path, old, new):
    """Simulate a settings save: snapshot, write (with backup), like the routes do."""
    fake_host.files[path] = old
    prev = apply_guard.snapshot([path])
    fake_host.write_text(path, new, backup=True)
    return prev


# ── verification ─────────────────────────────────────────────────────────────
def test_readsb_verified_when_running_with_fresh_stats(fake_host, init, no_background_checks):
    prev = save(fake_host, appmod.READSB_DEFAULT, 'OLD\n', 'NEW\n')
    set_status(init, 'readsb', 'ok')
    check = apply_guard.restart_and_verify('readsb', prev)
    assert check['state'] == 'verifying' and len(no_background_checks) == 1
    c = no_background_checks[0]
    fake_host.files[READSB_STATS] = {'now': c['started'] + 3}
    clock = Clock(c['started'])
    apply_guard.run_check(c, clock.sleep, clock.now)
    assert apply_guard.get_check(c['id'])['state'] == 'verified'
    assert fake_host.files[appmod.READSB_DEFAULT] == 'NEW\n'


def test_readsb_rolled_back_when_it_stays_down(fake_host, init, no_background_checks):
    prev = save(fake_host, appmod.READSB_DEFAULT, 'OLD\n', 'NEW\n')
    set_status(init, 'readsb', 'error')
    apply_guard.restart_and_verify('readsb', prev)
    c = no_background_checks[0]
    clock = Clock(c['started'])
    apply_guard.run_check(c, clock.sleep, clock.now)
    out = apply_guard.get_check(c['id'])
    assert out['state'] == 'rolled_back'
    assert 'readsb is failed' in out['message'] and 'restored the previous settings' in out['message']
    assert fake_host.files[appmod.READSB_DEFAULT] == 'OLD\n'
    # the backup still holds the last good copy, not the bad save
    assert fake_host.files[os.path.realpath(appmod.READSB_DEFAULT) + BACKUP_SUFFIX] == 'OLD\n'
    assert clock.t - c['started'] >= apply_guard.VERIFY_LIMIT_S['readsb']


def test_running_without_fresh_stats_is_not_enough(fake_host, init, no_background_checks):
    prev = save(fake_host, appmod.AIRSPY_DEFAULT, 'GAIN=13\n', 'GAIN=99\n')
    set_status(init, 'airspy_adsb', 'ok')
    apply_guard.restart_and_verify('airspy_adsb', prev)
    c = no_background_checks[0]
    fake_host.files[appmod.AIRSPY_STATS] = {'now': c['started'] - 30}    # stale: from before the restart
    clock = Clock(c['started'])
    apply_guard.run_check(c, clock.sleep, clock.now)
    out = apply_guard.get_check(c['id'])
    assert out['state'] == 'rolled_back' and 'fresh stats' in out['message']
    assert fake_host.files[appmod.AIRSPY_DEFAULT] == 'GAIN=13\n'


def test_airspy_gets_time_for_its_60s_stats(fake_host, init, no_background_checks):
    prev = save(fake_host, appmod.AIRSPY_DEFAULT, 'GAIN=13\n', 'GAIN=14\n')
    set_status(init, 'airspy_adsb', 'ok')
    apply_guard.restart_and_verify('airspy_adsb', prev)
    c = no_background_checks[0]
    def tick(t):   # first stats after the restart arrive 70 s later
        if t - c['started'] >= 70:
            fake_host.files[appmod.AIRSPY_STATS] = {'now': c['started'] + 70}
    clock = Clock(c['started'], tick)
    apply_guard.run_check(c, clock.sleep, clock.now)
    assert apply_guard.get_check(c['id'])['state'] == 'verified'


def test_flapping_unit_must_stay_up(fake_host, init, no_background_checks):
    prev = save(fake_host, appmod.READSB_DEFAULT, 'OLD\n', 'NEW\n')
    apply_guard.restart_and_verify('readsb', prev)
    c = no_background_checks[0]
    fake_host.files[READSB_STATS] = {'now': c['started'] + 1}
    def tick(t):   # alternates up/down every check: never STABLE_S in a row
        set_status(init, 'readsb', 'ok' if int(t // apply_guard.CHECK_EVERY_S) % 2 else 'error')
    clock = Clock(c['started'], tick)
    apply_guard.run_check(c, clock.sleep, clock.now)
    assert apply_guard.get_check(c['id'])['state'] == 'rolled_back'


def test_superseded_by_another_save(fake_host, init, no_background_checks):
    prev = save(fake_host, appmod.READSB_DEFAULT, 'OLD\n', 'NEW\n')
    set_status(init, 'readsb', 'error')
    apply_guard.restart_and_verify('readsb', prev)
    c = no_background_checks[0]
    clock = Clock(c['started'], lambda t: fake_host.files.__setitem__(appmod.READSB_DEFAULT, 'NEWER\n'))
    apply_guard.run_check(c, clock.sleep, clock.now)
    assert apply_guard.get_check(c['id'])['state'] == 'superseded'
    assert fake_host.files[appmod.READSB_DEFAULT] == 'NEWER\n'     # not rolled back


def test_failed_restart_rolls_back_at_once(fake_host, init, no_background_checks):
    prev = save(fake_host, appmod.READSB_DEFAULT, 'OLD\n', 'NEW\n')
    init.actions['readsb'] = (False, 'Job failed')
    out = apply_guard.restart_and_verify('readsb', prev)
    assert out['state'] == 'rollback_failed'        # the restart after restoring failed too
    assert fake_host.files[appmod.READSB_DEFAULT] == 'OLD\n'
    assert no_background_checks == []


def test_rollback_removes_a_file_the_save_created_and_reloads_systemd(fake_host, init, no_background_checks):
    fake_host.files[appmod.READSB_DEFAULT] = 'OLD\n'
    prev = apply_guard.snapshot([appmod.READSB_DEFAULT, BIASTEE_1090_CONF])
    assert prev[BIASTEE_1090_CONF] is None
    fake_host.write_text(BIASTEE_1090_CONF, '[Service]\nExecStartPre=x\n', backup=True)
    set_status(init, 'readsb', 'error')
    apply_guard.restart_and_verify('readsb', prev)
    c = no_background_checks[0]
    clock = Clock(c['started'])
    apply_guard.run_check(c, clock.sleep, clock.now)
    assert BIASTEE_1090_CONF not in fake_host.files
    assert ('run', ('systemctl', 'daemon-reload')) in fake_host.calls


# ── routes ───────────────────────────────────────────────────────────────────
@pytest.fixture
def client():
    appmod.app.config['TESTING'] = True
    return appmod.app.test_client()


def test_receiver_save_returns_a_check(client, fake_host, init, no_background_checks):
    fake_host.files[appmod.READSB_DEFAULT] = 'RECEIVER_OPTIONS="--net-only"\n'
    r = client.post('/api/settings/receiver', json={'lat': '', 'lon': ''}).get_json()
    assert r['ok'] is True and r['check']['state'] == 'verifying' and r['check']['unit'] == 'readsb'
    assert client.get(f"/api/settings/check/{r['check']['id']}").get_json()['check']['state'] == 'verifying'


def test_unknown_check_is_404(client):
    assert client.get('/api/settings/check/999').status_code == 404


# ── revert ───────────────────────────────────────────────────────────────────
def bak(path):
    return os.path.realpath(path) + BACKUP_SUFFIX


def test_backups_listed_without_contents(client, fake_host):
    fake_host.files[appmod.READSB_DEFAULT] = 'CURRENT\n'
    fake_host.files[bak(appmod.READSB_DEFAULT)] = 'PREVIOUS\n'
    fake_host.files[appmod.AIRSPY_DEFAULT] = 'SAME\n'
    fake_host.files[bak(appmod.AIRSPY_DEFAULT)] = 'SAME\n'                  # identical: not listed
    rows = client.get('/api/config/backups').get_json()['backups']
    assert [r['path'] for r in rows] == [appmod.READSB_DEFAULT]
    assert 'PREVIOUS' not in str(rows) and 'CURRENT' not in str(rows)
    assert rows[0]['unit'] == 'readsb' and rows[0]['backup_mtime']


def test_revert_swaps_file_and_backup_then_verifies(client, fake_host, init, no_background_checks):
    fake_host.files[appmod.READSB_DEFAULT] = 'CURRENT\n'
    fake_host.files[bak(appmod.READSB_DEFAULT)] = 'PREVIOUS\n'
    r = client.post('/api/config/revert', json={'path': appmod.READSB_DEFAULT}).get_json()
    assert r['ok'] is True and r['check']['unit'] == 'readsb'
    assert fake_host.files[appmod.READSB_DEFAULT] == 'PREVIOUS\n'
    assert fake_host.files[bak(appmod.READSB_DEFAULT)] == 'CURRENT\n'      # revert is itself revertible


def test_failed_revert_puts_file_and_backup_back(fake_host, init, no_background_checks):
    fake_host.files[appmod.READSB_DEFAULT] = 'CURRENT\n'
    fake_host.files[bak(appmod.READSB_DEFAULT)] = 'PREVIOUS\n'
    set_status(init, 'readsb', 'error')
    apply_guard.revert(appmod.READSB_DEFAULT)
    c = no_background_checks[0]
    clock = Clock(c['started'])
    apply_guard.run_check(c, clock.sleep, clock.now)
    assert fake_host.files[appmod.READSB_DEFAULT] == 'CURRENT\n'
    assert fake_host.files[bak(appmod.READSB_DEFAULT)] == 'PREVIOUS\n'


@pytest.mark.parametrize('path', ['/etc/passwd', '/etc/default/readsb/../shadow', ''])
def test_revert_refuses_unmanaged_paths(client, fake_host, path):
    r = client.post('/api/config/revert', json={'path': path})
    assert r.status_code == 400
    assert not [c for c in fake_host.calls if c[0] == 'write_text']


def test_revert_without_backup(client, fake_host):
    fake_host.files[appmod.READSB_DEFAULT] = 'CURRENT\n'
    r = client.post('/api/config/revert', json={'path': appmod.READSB_DEFAULT})
    assert r.status_code == 400 and r.get_json()['error'] == 'no backup for this file'


def test_revert_of_file_without_unit_needs_no_restart(client, fake_host, init, no_background_checks):
    fake_host.files[appmod.AUTOTUNE_FILE] = '{"enabled": true}'
    fake_host.files[bak(appmod.AUTOTUNE_FILE)] = '{"enabled": false}'
    r = client.post('/api/config/revert', json={'path': appmod.AUTOTUNE_FILE}).get_json()
    assert r['ok'] is True and 'check' not in r and no_background_checks == []


def test_revert_is_admin_only(fake_host):
    c = appmod.app.test_client()
    appmod._request_port.port = appmod.READONLY_PORT
    try:
        assert c.post('/api/config/revert', json={'path': appmod.READSB_DEFAULT}).status_code == 403
        assert c.get('/api/config/backups').status_code == 403
    finally:
        appmod._request_port.port = appmod.ADMIN_PORT


def test_airspy_backup_flags_autotune(client, fake_host, monkeypatch):
    from system import autotune
    monkeypatch.setattr(autotune, 'load_config', lambda: {'enabled': True})
    fake_host.files[appmod.AIRSPY_DEFAULT] = 'GAIN=14\n'
    fake_host.files[bak(appmod.AIRSPY_DEFAULT)] = 'GAIN=13\n'
    rows = client.get('/api/config/backups').get_json()['backups']
    assert rows[0]['path'] == appmod.AIRSPY_DEFAULT and rows[0]['autotune_on'] is True
