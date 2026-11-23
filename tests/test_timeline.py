"""Timeline view backend: signal samples, change feed, /api/timeline."""
import sqlite3
import time

import pytest

import app as appmod
from app import Result
from fakes import FakeInitAdapter
from system import apply_guard, autotune, changes, db


# ── signal sample ───────────────────────────────────────────────────────────
def airspy_stats(now, buckets=None, gain=14.0):
    b = buckets if buckets is not None else [0] * 60 + [90] + [0] * 6 + [10]   # 10 of 100 at 67 dB
    return {'now': now, 'gain': gain, 'rssi_buckets': b,
            'snr': {'median': 12.3}, 'noise': {'median': 36.9}}


def test_signal_sample_from_fresh_stats(fake_host):
    now = time.time()
    fake_host.files[appmod.AIRSPY_STATS] = airspy_stats(now - 20)
    assert autotune.signal_sample(now) == {'strong_pct': 10.0, 'snr': 12.3, 'noise': 36.9, 'gain': 14.0}


def test_signal_sample_none_when_stale_or_missing(fake_host):
    now = time.time()
    assert autotune.signal_sample(now) is None
    fake_host.files[appmod.AIRSPY_STATS] = airspy_stats(now - 600)
    assert autotune.signal_sample(now) is None


def test_signal_sample_without_traffic_has_no_strong_pct(fake_host):
    now = time.time()
    fake_host.files[appmod.AIRSPY_STATS] = airspy_stats(now, buckets=[0] * 90)
    assert 'strong_pct' not in autotune.signal_sample(now)


# ── storage ─────────────────────────────────────────────────────────────────
def test_metrics_table_gains_signal_columns_on_old_db(tmp_path, monkeypatch):
    path = tmp_path / 'old.db'
    c = sqlite3.connect(path)
    c.execute('CREATE TABLE metrics (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, '
              'aircraft INTEGER, msg_rate INTEGER, max_range_nm INTEGER)')
    c.execute('INSERT INTO metrics (ts, aircraft, msg_rate, max_range_nm) VALUES (?,?,?,?)', (time.time(), 5, 50, 100))
    c.commit()
    c.close()
    monkeypatch.setattr(appmod, 'DB_FILE', str(path))
    db.init_db()
    db.init_db()                                             # idempotent
    cols = {r[1] for r in sqlite3.connect(path).execute('PRAGMA table_info(metrics)')}
    assert {'strong_pct', 'snr', 'noise', 'gain'} <= cols
    assert db.get_timeline(1)[0]['msg_rate'] == 50.0         # old rows still read


def test_timeline_buckets(ledger_db, monkeypatch):
    base = 1_800_000_000 - (1_800_000_000 % 300)
    rows = [(base + 10, 10, 100, 150, {'strong_pct': 4.0, 'gain': 13.0}),
            (base + 40, 20, 200, 170, {'strong_pct': 6.0, 'gain': 14.0}),
            (base + 310, 30, 300, 160, None)]
    for ts, ac, mr, rng, sig in rows:
        monkeypatch.setattr(db.time, 'time', lambda ts=ts: ts)
        db.record_metrics(ac, mr, rng, sig)
    monkeypatch.setattr(db.time, 'time', lambda: base + 400)
    out = db.get_timeline(1, 300)
    assert [b['ts'] for b in out] == [base, base + 300]
    assert out[0] == {'ts': base, 'msg_rate': 150.0, 'max_range_nm': 170, 'aircraft': 15.0,
                      'strong_pct': 5.0, 'snr': None, 'noise': None, 'gain': 14.0}
    assert out[1]['strong_pct'] is None and out[1]['gain'] is None


def test_record_change_dedupes_on_ref(ledger_db):
    assert db.record_change('update', 'readsb', 'readsb update finished', 'ok', ref='Sun 2026-11-22 10:00')
    assert not db.record_change('update', 'readsb', 'readsb update finished', 'ok', ref='Sun 2026-11-22 10:00')
    assert db.record_change('update', 'readsb', 'readsb update finished', 'ok', ref='Mon 2026-11-23 10:00')
    assert len([c for c in db.get_changes(1) if c['kind'] == 'update']) == 2


def test_changes_merge_all_sources_newest_first(ledger_db, monkeypatch):
    t = [time.time() - 3000]
    monkeypatch.setattr(db.time, 'time', lambda: t[0])
    db.record_service_event('readsb', 'ok')            # the ledger's opening row: not a change
    t[0] += 60
    db.record_service_event('readsb', 'error')
    t[0] += 60
    db.record_gain_change('13', '14', 'auto-tune: 0.3% strong signals < 0.5%')
    t[0] += 60
    db.record_dataflow_event('fr24feed', 'stale', 'FR24 link down')
    t[0] += 60
    db.record_change('restart', 'readsb', 'readsb restarted')
    monkeypatch.setattr(db.time, 'time', lambda: t[0] + 1)
    out = db.get_changes(24)
    assert [c['kind'] for c in out] == ['restart', 'data', 'gain', 'state']
    assert out[2]['old'] == '13' and out[2]['new'] == '14'
    assert out[3]['level'] == 'error' and out[1]['level'] == 'warn'


# ── restarts / updates ──────────────────────────────────────────────────────
def test_restart_detected_when_start_marker_moves(monkeypatch, change_records):
    init = FakeInitAdapter()
    marks = {'readsb': '100'}
    init.start_marker = lambda s: marks.get(s)
    monkeypatch.setattr(appmod, 'INIT', init)
    feeders = [{'key': 'readsb', 'kind': 'service', 'label': 'readsb'},
               {'key': 'gone', 'kind': 'service', 'label': 'gone'}]
    assert changes.detect_restarts(feeders) == []          # first look only learns
    assert changes.detect_restarts(feeders) == []          # unchanged
    marks['readsb'] = '250'
    assert changes.detect_restarts(feeders) == ['readsb']
    assert change_records == [('restart', 'readsb', 'readsb restarted', 'info', None)]


def test_docker_restart_uses_started_at(fake_host, change_records):
    fake_host.commands[('docker', 'inspect', '--format', '{{.State.StartedAt}}', 'airnavradar')] = \
        Result(0, '2026-11-22T10:00:00Z\n', '')
    f = [{'key': 'airnavradar', 'kind': 'docker', 'label': 'AirNav Radar'}]
    changes.detect_restarts(f)
    fake_host.commands[('docker', 'inspect', '--format', '{{.State.StartedAt}}', 'airnavradar')] = \
        Result(0, '2026-11-22T11:00:00Z\n', '')
    assert changes.detect_restarts(f) == ['airnavradar']


def test_systemd_start_marker(fake_host):
    init = appmod.SystemdAdapter(fake_host)
    key = ('systemctl', 'show', '-p', 'ActiveEnterTimestampMonotonic', '--value', 'readsb')
    fake_host.commands[key] = Result(0, '123456789\n', '')
    assert init.start_marker('readsb') == '123456789'
    fake_host.commands[key] = Result(0, '0\n', '')
    assert init.start_marker('readsb') is None


def test_finished_stack_update_recorded(monkeypatch, change_records):
    from system import stack_update
    monkeypatch.setattr(stack_update, 'supported', lambda: True)
    monkeypatch.setattr(stack_update, 'component_state', lambda k: (
        {'state': 'failed', 'finished': 'Sun 2026-11-22 10:00'} if k == 'readsb' else {'state': 'idle', 'finished': None}))
    changes.record_stack_updates()
    assert change_records == [('update', 'readsb', 'readsb update failed', 'error', 'Sun 2026-11-22 10:00')]


def test_config_check_outcomes_go_to_the_feed(change_records):
    check = {'id': 'x', 'unit': 'readsb', 'state': 'verifying', 'message': '', 'backups': None}
    apply_guard._set(check, 'verified', 'readsb restarted and is running with the new settings')
    apply_guard._set({**check, 'backups': {'/etc/default/readsb': 'x'}}, 'rolled_back', 'readsb is failed')
    apply_guard._set(check, 'superseded', 'changed again')           # not a feed item
    assert [(r[0], r[1], r[3]) for r in change_records] == [('config', 'readsb', 'ok'), ('config', 'readsb', 'warn')]
    assert change_records[1][2].startswith('revert:')


def test_recording_never_raises(monkeypatch):
    def boom(*a, **k):
        raise sqlite3.OperationalError('locked')
    monkeypatch.setattr(changes, 'record_change', boom)
    changes.record('config', 'readsb', 'x')


# ── route ───────────────────────────────────────────────────────────────────
@pytest.fixture
def client():
    appmod.app.config['TESTING'] = True
    return appmod.app.test_client()


def test_timeline_route(client, fake_host, ledger_db):
    db.record_metrics(10, 100, 150, {'strong_pct': 3.0, 'gain': 14.0, 'snr': 12.0, 'noise': 37.0})
    fake_host.files[appmod.READSB_DEFAULT] = 'RECEIVER_OPTIONS="--net-only"\n'
    fake_host.files[appmod.READSB_JSON + '/outline.json'] = {'actualRange': {}}
    r = client.get('/api/timeline?hours=6').get_json()
    assert r['ok'] and r['bucket_s'] == 60 and r['metrics'][0]['gain'] == 14.0
    assert r['links'] == {'outline': True, 'heatmap': False, 'ptracks': True}
    assert client.get('/api/timeline?hours=999').get_json()['hours'] == 25.0
    assert client.get('/api/timeline?hours=abc').get_json()['hours'] == 24.0


def test_timeline_on_the_readonly_port(client, fake_host, ledger_db):
    appmod._request_port.port = appmod.READONLY_PORT
    try:
        assert client.get('/api/timeline').status_code == 200
    finally:
        appmod._request_port.port = appmod.ADMIN_PORT
