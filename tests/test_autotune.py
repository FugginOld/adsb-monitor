"""Airspy gain auto-tune: pure decision logic, the poll step on FakeHost, the
verify/rollback path, and the /api/autotune routes."""
import json

import pytest

import app as appmod
from app import Result
from system import autotune
from system.db import get_gain_changes

CONF = ('GAIN=21\nSAMPLE_RATE=6\nOPTIONS="-v -f 1"\nAFFINITY=0\n'
        'NET="-l 47787:beast"\nSTATS="-S /run/airspy_adsb/stats.json"\nG=-g\nM=\n')
CFG_ON = {'enabled': True, 'window_min': 3, 'min_messages': 100, 'settle_s': 0}


def hist(strong, weak, width=80, at=67):
    b = [0] * width
    b[at], b[55] = strong, weak
    return b


@pytest.fixture(autouse=True)
def fresh_state(ledger_db):
    autotune._reset_state_for_tests()
    yield
    autotune._reset_state_for_tests()


class Clock:
    def __init__(self, t=1_000_000.0): self.t = t
    def __call__(self): return self.t


def setup_host(fake_host, cfg=CFG_ON, conf=CONF, restart_ok=True):
    fake_host.files[appmod.AIRSPY_DEFAULT] = conf
    fake_host.files[appmod.AUTOTUNE_FILE] = json.dumps(cfg)
    fake_host.commands[('systemctl', 'restart', 'airspy_adsb')] = Result(0 if restart_ok else 1, '', '' if restart_ok else 'boom')
    fake_host.commands[('systemctl', 'is-active', 'airspy_adsb')] = Result(0, 'active\n', '')


def feed(fake_host, clock, buckets, gain, n):
    """Advance one stats.json write (60 s) at a time, polling after each."""
    for _ in range(n):
        clock.t += 60
        fake_host.files[appmod.AIRSPY_STATS] = {'now': clock.t - 1, 'gain': float(gain), 'rssi_buckets': buckets}
        autotune.autotune_step(clock)


def restarts(fake_host):
    return [c for c in fake_host.calls if c == ('run', ('systemctl', 'restart', 'airspy_adsb'))]


# ── pure logic ───────────────────────────────────────────────────────────────
def test_strong_pct():
    assert autotune.strong_pct(hist(25, 75), 67) == (25.0, 100)
    assert autotune.strong_pct([0] * 80, 67) == (0.0, 0)


@pytest.mark.parametrize('pct, gain, expected', [
    (49.0, 21, 18),    # way over (>= 4x high) -> 3 steps
    (8.0, 21, 20),     # over -> 1 step
    (2.0, 15, None),   # dead band
    (0.1, 15, 16),     # under -> up 1
    (0.1, 21, None),   # already at max
    (49.0, 1, 0),      # clamps at min
])
def test_decide(pct, gain, expected):
    new, _ = autotune.decide(pct, 10_000, gain, autotune.DEFAULTS)
    assert new == expected


def test_decide_skips_quiet_windows():
    new, reason = autotune.decide(49.0, 10, 21, autotune.DEFAULTS)
    assert new is None and 'not enough traffic' in reason


@pytest.mark.parametrize('bad', [
    {'max_gain': 30}, {'min_gain': 10, 'max_gain': 5}, {'enabled': 'yes'},
    {'strong_low_pct': 6, 'strong_high_pct': 5}, {'bogus': 1}, {'window_min': 1.5},
])
def test_validate_config_rejects(bad):
    with pytest.raises(ValueError):
        autotune.validate_config(bad)


def test_invalid_config_file_falls_back_to_disabled(fake_host):
    fake_host.files[appmod.AUTOTUNE_FILE] = json.dumps({'enabled': True, 'max_gain': 99})
    assert autotune.load_config()['enabled'] is False


# ── poll step ────────────────────────────────────────────────────────────────
def test_disabled_does_nothing(fake_host):
    setup_host(fake_host, cfg={'enabled': False})
    feed(fake_host, Clock(), hist(50, 50), 21, 5)
    assert appmod.AIRSPY_DEFAULT not in fake_host.writes
    assert autotune.snapshot()['status'] == 'disabled'


def test_auto_gain_is_left_alone(fake_host):
    setup_host(fake_host, conf=CONF.replace('G=-g', 'G='))
    feed(fake_host, Clock(), hist(50, 50), 21, 5)
    assert appmod.AIRSPY_DEFAULT not in fake_host.writes
    assert 'auto' in autotune.snapshot()['status']


def test_lowers_gain_after_a_full_window_and_records_it(fake_host):
    setup_host(fake_host)
    clock = Clock()
    feed(fake_host, clock, hist(50, 50), 21, 2)
    assert appmod.AIRSPY_DEFAULT not in fake_host.writes          # window not full yet
    feed(fake_host, clock, hist(50, 50), 21, 1)
    written = fake_host.writes[appmod.AIRSPY_DEFAULT]
    assert 'GAIN=18' in written.splitlines()
    assert written.replace('GAIN=18', 'GAIN=21') == CONF          # nothing else touched
    assert len(restarts(fake_host)) == 1
    hist_rows = get_gain_changes()
    assert hist_rows[0]['old'] == '21' and hist_rows[0]['new'] == '18'


def test_same_stats_file_is_not_counted_twice(fake_host):
    setup_host(fake_host)
    clock = Clock()
    feed(fake_host, clock, hist(50, 50), 21, 1)
    for _ in range(5):                       # poller runs every 30 s, file only changes every 60 s
        clock.t += 30
        autotune.autotune_step(clock)
    assert autotune.snapshot()['samples'] == 1


def test_dead_band_slides_window_without_changing(fake_host):
    setup_host(fake_host)
    clock = Clock()
    feed(fake_host, clock, hist(2, 98), 21, 6)
    assert appmod.AIRSPY_DEFAULT not in fake_host.writes
    assert autotune.snapshot()['samples'] == 2                    # window_min - 1 after slide


def test_stale_stats_are_ignored(fake_host):
    setup_host(fake_host)
    clock = Clock()
    for _ in range(5):
        clock.t += 60
        fake_host.files[appmod.AIRSPY_STATS] = {'now': clock.t - 600, 'gain': 21.0, 'rssi_buckets': hist(50, 50)}
        autotune.autotune_step(clock)
    assert appmod.AIRSPY_DEFAULT not in fake_host.writes


def test_verifies_new_gain(fake_host):
    setup_host(fake_host)
    clock = Clock()
    feed(fake_host, clock, hist(50, 50), 21, 3)                   # -> 18, pending
    feed(fake_host, clock, hist(10, 90), 18, 2)                   # decoder came back at 18
    s = autotune.snapshot()
    assert s['verifying'] is False and 'verified' in s['status']
    assert len(restarts(fake_host)) == 1


def test_rolls_back_and_disables_when_decoder_does_not_come_back(fake_host):
    setup_host(fake_host)
    clock = Clock()
    feed(fake_host, clock, hist(50, 50), 21, 3)                   # -> 18, pending
    fake_host.commands[('systemctl', 'is-active', 'airspy_adsb')] = Result(3, 'activating\n', '')
    fake_host.files[appmod.AIRSPY_STATS] = {}                      # no fresh stats
    for _ in range(6):
        clock.t += 60
        autotune.autotune_step(clock)
    assert fake_host.files[appmod.AIRSPY_DEFAULT] == CONF          # original restored
    assert len(restarts(fake_host)) == 2
    assert json.loads(fake_host.files[appmod.AUTOTUNE_FILE])['enabled'] is False
    assert get_gain_changes()[0]['reason'].startswith('rollback')
    assert 'rollback' in autotune.snapshot()['status']


def test_failed_restart_rolls_back_immediately(fake_host):
    setup_host(fake_host, restart_ok=False)
    feed(fake_host, Clock(), hist(50, 50), 21, 3)
    assert fake_host.files[appmod.AIRSPY_DEFAULT] == CONF
    assert json.loads(fake_host.files[appmod.AUTOTUNE_FILE])['enabled'] is False


def test_manual_edit_during_verification_is_not_rolled_back(fake_host):
    setup_host(fake_host)
    clock = Clock()
    feed(fake_host, clock, hist(50, 50), 21, 3)                   # -> 18, pending
    manual = CONF.replace('GAIN=21', 'GAIN=12')
    fake_host.files[appmod.AIRSPY_DEFAULT] = manual
    clock.t += 300
    autotune.autotune_step(clock)
    assert fake_host.files[appmod.AIRSPY_DEFAULT] == manual
    assert autotune.snapshot()['verifying'] is False


# ── routes ───────────────────────────────────────────────────────────────────
@pytest.fixture
def client():
    appmod.app.config['TESTING'] = True
    return appmod.app.test_client()


def test_route_get_autotune_defaults(client, fake_host):
    body = client.get('/api/autotune').get_json()
    assert body['ok'] is True and body['config']['enabled'] is False
    assert body['history'] == []


def test_route_post_autotune_saves(client, fake_host):
    r = client.post('/api/autotune', json={'enabled': True, 'min_gain': 10})
    assert r.get_json()['ok'] is True
    saved = json.loads(fake_host.writes[appmod.AUTOTUNE_FILE])
    assert saved['enabled'] is True and saved['min_gain'] == 10


def test_route_post_autotune_rejects_bad_values(client, fake_host):
    r = client.post('/api/autotune', json={'max_gain': 40})
    assert r.status_code == 400
    assert appmod.AUTOTUNE_FILE not in fake_host.writes


def test_route_post_autotune_blocked_on_readonly_port(client, fake_host):
    # Port tagging is a thread-local set by make_tagged_app; the test client runs
    # the request on this thread, so set it directly.
    appmod._request_port.port = appmod.READONLY_PORT
    try:
        r = client.post('/api/autotune', json={'enabled': True})
    finally:
        appmod._request_port.port = appmod.ADMIN_PORT
    assert r.status_code == 403
    assert appmod.AUTOTUNE_FILE not in fake_host.writes


# ── manual gain override while auto-tune is on ───────────────────────────────
def test_route_gain_change_refused_while_autotune_on(client, fake_host):
    setup_host(fake_host)                                         # enabled, GAIN=21 G=-g
    r = client.post('/api/settings/airspy', json={'gain': '12', 'options': '-v -f 1'})
    assert r.status_code == 409
    body = r.get_json()
    assert body['error'] == 'autotune_enabled' and body['current_gain'] == '21'
    assert appmod.AIRSPY_DEFAULT not in fake_host.writes
    assert json.loads(fake_host.files[appmod.AUTOTUNE_FILE])['enabled'] is True


def test_route_gain_override_turns_autotune_off(client, fake_host):
    setup_host(fake_host)
    r = client.post('/api/settings/airspy', json={'gain': '12', 'options': '-v -f 1', 'override_autotune': True})
    assert r.status_code == 200
    assert 'GAIN=12' in fake_host.writes[appmod.AIRSPY_DEFAULT].splitlines()
    assert json.loads(fake_host.files[appmod.AUTOTUNE_FILE])['enabled'] is False
    row = get_gain_changes()[0]
    assert (row['old'], row['new']) == ('21', '12') and 'manual override' in row['reason']


def test_route_same_gain_saves_without_override(client, fake_host):
    setup_host(fake_host)                                         # only options change
    r = client.post('/api/settings/airspy', json={'gain': '21', 'options': '-v -f 2'})
    assert r.status_code == 200
    assert json.loads(fake_host.files[appmod.AUTOTUNE_FILE])['enabled'] is True


def test_route_invalid_gain_with_override_keeps_autotune_on(client, fake_host):
    setup_host(fake_host)
    r = client.post('/api/settings/airspy', json={'gain': '99', 'override_autotune': True})
    assert r.status_code == 400
    assert json.loads(fake_host.files[appmod.AUTOTUNE_FILE])['enabled'] is True


def test_live_hint_marks_autotune(fake_host):
    setup_host(fake_host)
    assert '· auto-tune' in appmod.airspy_live_hint()


# ── window / verification survive monitor restarts ───────────────────────────
def restart():
    """What a monitor restart does to auto-tune: in-memory state gone, restore on next poll."""
    autotune._reset_state_for_tests(loaded=False)


def saved_state(fake_host):
    return json.loads(fake_host.files[appmod.AUTOTUNE_STATE_FILE])


def test_samples_are_written_to_the_state_file(fake_host):
    setup_host(fake_host)
    feed(fake_host, Clock(), hist(50, 50), 21, 2)
    st = saved_state(fake_host)
    assert st['window_gain'] == '21' and len(st['samples']) == 2


def test_window_survives_a_restart(fake_host):
    setup_host(fake_host)
    clock = Clock()
    feed(fake_host, clock, hist(50, 50), 21, 2)
    restart()
    feed(fake_host, clock, hist(50, 50), 21, 1)                   # 3rd sample completes the window
    assert 'GAIN=18' in fake_host.writes[appmod.AIRSPY_DEFAULT].splitlines()


def test_window_completes_even_when_restarted_after_every_sample(fake_host):
    setup_host(fake_host)
    clock = Clock()
    for _ in range(3):
        restart()
        feed(fake_host, clock, hist(50, 50), 21, 1)
    assert 'GAIN=18' in fake_host.writes[appmod.AIRSPY_DEFAULT].splitlines()


def test_samples_older_than_a_window_are_dropped_after_long_downtime(fake_host):
    setup_host(fake_host)
    clock = Clock()
    feed(fake_host, clock, hist(50, 50), 21, 2)
    restart()
    clock.t += 2 * 3600                                           # monitor down for 2 h
    feed(fake_host, clock, hist(50, 50), 21, 1)
    assert appmod.AIRSPY_DEFAULT not in fake_host.writes
    assert autotune.snapshot()['samples'] == 1


def test_restored_samples_at_another_gain_are_dropped(fake_host):
    setup_host(fake_host)
    clock = Clock()
    feed(fake_host, clock, hist(50, 50), 21, 2)
    restart()
    fake_host.files[appmod.AIRSPY_DEFAULT] = CONF.replace('GAIN=21', 'GAIN=19')   # changed by hand meanwhile
    feed(fake_host, clock, hist(50, 50), 19, 1)
    assert autotune.snapshot()['samples'] == 1 and autotune.snapshot()['window_gain'] == '19'


def test_pending_verification_survives_restart_and_rolls_back(fake_host):
    setup_host(fake_host)
    clock = Clock()
    feed(fake_host, clock, hist(50, 50), 21, 3)                   # -> 18, pending
    assert saved_state(fake_host)['pending']['new'] == '18'
    restart()
    fake_host.commands[('systemctl', 'is-active', 'airspy_adsb')] = Result(3, 'failed\n', '')
    fake_host.files[appmod.AIRSPY_STATS] = {}
    for _ in range(6):
        clock.t += 60
        autotune.autotune_step(clock)
    assert fake_host.files[appmod.AIRSPY_DEFAULT] == CONF          # rolled back after the restart
    assert json.loads(fake_host.files[appmod.AUTOTUNE_FILE])['enabled'] is False
    assert saved_state(fake_host)['pending'] is None


def test_pending_verification_survives_restart_and_verifies(fake_host):
    setup_host(fake_host)
    clock = Clock()
    feed(fake_host, clock, hist(50, 50), 21, 3)                   # -> 18, pending
    restart()
    feed(fake_host, clock, hist(10, 90), 18, 2)
    assert 'verified' in autotune.snapshot()['status']
    assert len(restarts(fake_host)) == 1


@pytest.mark.parametrize('bad', [
    'not json', '{"version": 2, "samples": [[1, [1]]]}',
    '{"version": 1, "window_gain": "99", "samples": [[1, [1]]]}',
    '{"version": 1, "window_gain": "21", "samples": "x", "pending": {"ts": "bad"}}',
    '{"version": 1, "window_gain": "21", "samples": [[1, ["x"]], [2]]}',
])
def test_malformed_state_file_is_ignored(fake_host, bad):
    setup_host(fake_host)
    fake_host.files[appmod.AUTOTUNE_STATE_FILE] = bad
    restart()
    feed(fake_host, Clock(), hist(50, 50), 21, 1)
    s = autotune.snapshot()
    assert s['samples'] == 1 and s['verifying'] is False


def test_state_file_not_rewritten_when_nothing_changed(fake_host):
    setup_host(fake_host)
    clock = Clock()
    feed(fake_host, clock, hist(2, 98), 21, 1)
    def writes():
        return sum(1 for c in fake_host.calls if c[0] == 'write_text' and c[1] == appmod.AUTOTUNE_STATE_FILE)
    before = writes()
    for _ in range(4):                                            # 30 s polls, same stats.json
        clock.t += 30
        autotune.autotune_step(clock)
    assert writes() == before
