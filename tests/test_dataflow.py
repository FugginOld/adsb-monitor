"""system/dataflow.py: per-feeder data-flow sources, the no-data timer, and the
alerts/status routes that expose it."""
import json
import sqlite3
import time

import pytest

import app as appmod
from app import Result
from system import dataflow

NOW = 1_800_000_000.0
ADSBX = {'key': 'adsbexchange-feed', 'kind': 'service', 'label': 'ADSBExchange'}
FR24 = {'key': 'fr24feed', 'kind': 'service', 'label': 'FlightRadar24'}
PIAWARE = {'key': 'piaware', 'kind': 'service', 'label': 'FlightAware'}
MLAT = {'key': 'adsbexchange-mlat', 'kind': 'service', 'label': 'ADSBx MLAT'}
READSB = {'key': 'readsb', 'kind': 'service', 'label': 'readsb'}
AIRNAV = {'key': 'airnavradar', 'kind': 'docker', 'label': 'AirNav Radar'}


def prom(remote_secs, local_secs=3000):
    return (f'readsb_network_bytes_out 1000\n'
            f'readsb_net_connector_status{{host="feed1.adsbexchange.com",port="30004"}} {remote_secs}\n'
            f'readsb_net_connector_status{{host="127.0.0.1",port="30978"}} 0\n'
            f'readsb_net_connector_status{{host="127.0.0.1",port="30005"}} {local_secs}\n')


def feed_files(fake_host, remote_secs=600, status_age=2):
    fake_host.files['/run/adsbexchange-feed/status.json'] = {'now': NOW - status_age}
    fake_host.files['/run/adsbexchange-feed/stats.prom'] = prom(remote_secs)


# ── readsb feed clients (adsbexchange-feed / adsbfi-feed) ────────────────────
def test_feed_client_connected(fake_host):
    feed_files(fake_host)
    o = dataflow.observe(ADSBX, NOW)
    assert o.ok is True and 'feed1.adsbexchange.com' in o.detail


def test_feed_client_remote_down_even_though_local_978_is_zero_anyway(fake_host):
    feed_files(fake_host, remote_secs=0)
    o = dataflow.observe(ADSBX, NOW)
    assert o.ok is False and o.detail == 'not connected to feed1.adsbexchange.com'


def test_feed_client_ignores_loopback_connectors(fake_host):
    # 30978 (no 978 receiver) is always 0 here; it must not count as "down"
    feed_files(fake_host, remote_secs=10)
    assert dataflow.observe(ADSBX, NOW).ok is True


def test_feed_client_stale_status_file(fake_host):
    feed_files(fake_host, status_age=600)
    o = dataflow.observe(ADSBX, NOW)
    assert o.ok is False and 'stopped updating' in o.detail


def test_feed_client_without_files_is_unknown(fake_host):
    assert dataflow.observe(ADSBX, NOW).ok is None


# ── FR24 (monitor.json) ──────────────────────────────────────────────────────
def fr24(fake_host, **kw):
    d = {'feed_status': 'connected', 'feed_last_ac_sent_time': NOW - 3, 'feed_num_ac_tracked': 50}
    d.update(kw)
    fake_host.files[dataflow.FR24_MONITOR_URL] = d


def test_fr24_connected_and_sending(fake_host):
    fr24(fake_host)
    o = dataflow.observe(FR24, NOW)
    assert o.ok is True and o.age_s == 3


def test_fr24_link_down(fake_host):
    fr24(fake_host, feed_status='disconnected', feed_status_message='timeout')
    o = dataflow.observe(FR24, NOW)
    assert o.ok is False and o.detail == 'FR24 link disconnected: timeout'


def test_fr24_connected_but_not_sending(fake_host):
    fr24(fake_host, feed_last_ac_sent_time=NOW - 900)
    o = dataflow.observe(FR24, NOW)
    assert o.ok is False and 'no aircraft sent for 15m' in o.detail


def test_fr24_quiet_sky_is_not_an_outage(fake_host):
    fr24(fake_host, feed_last_ac_sent_time=NOW - 900, feed_num_ac_tracked=0)
    assert dataflow.observe(FR24, NOW).ok is True


def test_fr24_monitor_unreachable_is_unknown(fake_host):
    assert dataflow.observe(FR24, NOW).ok is None


# ── piaware (/run/piaware/status.json) ───────────────────────────────────────
def test_piaware_connected(fake_host):
    fake_host.files[dataflow.PIAWARE_STATUS] = {
        'time': (NOW - 4) * 1000, 'adept': {'status': 'green', 'message': 'Connected to FlightAware and logged in'}}
    o = dataflow.observe(PIAWARE, NOW)
    assert o.ok is True and o.detail == 'Connected to FlightAware and logged in'


def test_piaware_adept_red(fake_host):
    fake_host.files[dataflow.PIAWARE_STATUS] = {
        'time': NOW * 1000, 'adept': {'status': 'red', 'message': 'Not connected to FlightAware'}}
    o = dataflow.observe(PIAWARE, NOW)
    assert o.ok is False and o.detail == 'Not connected to FlightAware'


def test_piaware_status_not_updating(fake_host):
    fake_host.files[dataflow.PIAWARE_STATUS] = {'time': (NOW - 900) * 1000, 'adept': {'status': 'green'}}
    assert dataflow.observe(PIAWARE, NOW).ok is False


# ── mlat-client (journal) ────────────────────────────────────────────────────
MLAT_CMD = ('journalctl', '-u', 'adsbexchange-mlat', '-n', '200', '-o', 'short-unix', '--no-pager')


def journal(fake_host, *lines):
    out = '\n'.join(f'{ts:.6f} host adsbexchange-mlat[955]: {msg}' for ts, msg in lines)
    fake_host.commands[MLAT_CMD] = Result(0, out, '')


def test_mlat_ready(fake_host):
    journal(fake_host, (NOW - 1000, 'Handshake complete: Compression zlib2'),
            (NOW - 100, 'Server:   ready         0.1 kB/s from server      0.6 kB/s to server'),
            (NOW - 99, 'Results:  67.1 positions/minute'))
    assert dataflow.observe(MLAT, NOW).ok is True


def test_mlat_lost_connection_after_ready(fake_host):
    journal(fake_host, (NOW - 900, 'Server:   ready         0.1 kB/s from server'),
            (NOW - 30, 'Lost connection to feed1.adsbexchange.com:31090'),
            (NOW - 29, 'Reconnecting in 10.3 seconds'))
    o = dataflow.observe(MLAT, NOW)
    assert o.ok is False and o.detail == 'Reconnecting in 10.3 seconds'


def test_mlat_results_listener_loss_is_ignored(fake_host):
    journal(fake_host, (NOW - 200, 'Server:   ready         0.1 kB/s from server'),
            (NOW - 10, 'Beast-format results connection with 127.0.0.1:30104: connection lost'))
    assert dataflow.observe(MLAT, NOW).ok is True


def test_mlat_server_not_ready(fake_host):
    journal(fake_host, (NOW - 50, 'Server:   synchronizing   0.1 kB/s from server'))
    o = dataflow.observe(MLAT, NOW)
    assert o.ok is False and o.detail == 'MLAT server: synchronizing'


def test_mlat_silent_for_too_long(fake_host):
    journal(fake_host, (NOW - 3 * 3600, 'Server:   ready         0.1 kB/s from server'))
    o = dataflow.observe(MLAT, NOW)
    assert o.ok is False and 'no MLAT status for 3h' in o.detail


def test_mlat_no_journal_is_unknown(fake_host):
    assert dataflow.observe(MLAT, NOW).ok is None


# ── readsb and docker ────────────────────────────────────────────────────────
def readsb_stats(fake_host, msgs, age=5):
    import os
    fake_host.files[os.path.join(appmod.READSB_JSON, 'stats.json')] = {
        'now': NOW - age, 'last1min': {'messages_valid': msgs}}


def test_readsb_decoding(fake_host):
    readsb_stats(fake_host, 54000)
    o = dataflow.observe(READSB, NOW)
    assert o.ok is True and o.detail == '900 msg/s'


def test_readsb_no_messages(fake_host):
    readsb_stats(fake_host, 0)
    assert dataflow.observe(READSB, NOW).detail == 'no messages decoded in the last minute'


def test_readsb_stats_not_written(fake_host):
    readsb_stats(fake_host, 5000, age=900)
    assert dataflow.observe(READSB, NOW).ok is False


@pytest.mark.parametrize('health,ok', [('healthy', True), ('unhealthy', False), ('starting', None), ('', None)])
def test_docker_health(fake_host, health, ok):
    cmd = ('docker', 'inspect', '--format', '{{if .State.Health}}{{.State.Health.Status}}{{end}}', 'airnavradar')
    fake_host.commands[cmd] = Result(0, health + '\n', '')
    assert dataflow.observe(AIRNAV, NOW).ok is ok


def test_no_source_is_unknown(fake_host):
    assert dataflow.observe({'key': 'tar1090', 'kind': 'service'}, NOW).ok is None


def test_malformed_source_is_unknown_not_an_error(fake_host):
    fake_host.files['/run/piaware/status.json'] = {'time': 'not-a-number'}
    assert dataflow.observe(PIAWARE, NOW).ok is None


# ── the no-data timer ────────────────────────────────────────────────────────
def events(db):
    with sqlite3.connect(db) as c:
        return c.execute('SELECT service, state, detail FROM dataflow_events ORDER BY id').fetchall()


def test_stale_only_after_threshold_then_recovers(fake_host, ledger_db):
    feed_files(fake_host)
    assert dataflow.update(ADSBX, NOW)['state'] == 'ok'

    t = NOW + 30
    feed_files(fake_host, remote_secs=0, status_age=2 - 30)
    f = dataflow.update(ADSBX, t)
    assert f['state'] == 'pending' and f['since'] == t

    t2 = t + dataflow.STALE_AFTER_S - 1
    fake_host.files['/run/adsbexchange-feed/status.json'] = {'now': t2}
    assert dataflow.update(ADSBX, t2)['state'] == 'pending'

    t3 = t + dataflow.STALE_AFTER_S
    fake_host.files['/run/adsbexchange-feed/status.json'] = {'now': t3}
    f = dataflow.update(ADSBX, t3)
    assert f['state'] == 'stale' and f['since'] == t
    assert f['detail'] == 'not connected to feed1.adsbexchange.com (no data for 5m)'

    t4 = t3 + 30
    fake_host.files['/run/adsbexchange-feed/status.json'] = {'now': t4}
    fake_host.files['/run/adsbexchange-feed/stats.prom'] = prom(15)
    f = dataflow.update(ADSBX, t4)
    assert f['state'] == 'ok' and f['since'] is None
    assert events(ledger_db) == [
        ('adsbexchange-feed', 'stale', 'not connected to feed1.adsbexchange.com'),
        ('adsbexchange-feed', 'ok', 'connected to feed1.adsbexchange.com'),
    ]


def test_short_blip_records_nothing(fake_host, ledger_db):
    feed_files(fake_host)
    dataflow.update(ADSBX, NOW)
    feed_files(fake_host, remote_secs=0, status_age=-60)
    dataflow.update(ADSBX, NOW + 60)
    feed_files(fake_host, remote_secs=5, status_age=-90)
    assert dataflow.update(ADSBX, NOW + 90)['state'] == 'ok'
    assert events(ledger_db) == []


def test_unknown_reading_does_not_reset_the_timer(fake_host, ledger_db):
    fr24(fake_host, feed_status='disconnected')
    dataflow.update(FR24, NOW)
    del fake_host.files[dataflow.FR24_MONITOR_URL]          # monitor briefly unreachable
    assert dataflow.update(FR24, NOW + 100)['state'] == 'unknown'
    fr24(fake_host, feed_status='disconnected')
    assert dataflow.update(FR24, NOW + dataflow.STALE_AFTER_S)['state'] == 'stale'


@pytest.mark.parametrize('flow,label', [
    ({'state': 'ok', 'age_s': 3}, 'live'),
    ({'state': 'ok', 'age_s': None}, 'live'),
    ({'state': 'ok', 'age_s': 600}, '10m ago'),
    ({'state': 'pending'}, 'no data'),
    ({'state': 'stale'}, 'no data'),
    ({'state': 'unknown'}, None),
])
def test_last_seen_label(flow, label):
    assert dataflow.last_seen(flow) == label


def test_current_uses_poller_result_while_fresh(fake_host, ledger_db):
    feed_files(fake_host, status_age=0)
    fake_host.files['/run/adsbexchange-feed/status.json'] = {'now': time.time()}
    dataflow.update(ADSBX)
    fake_host.calls.clear()
    assert dataflow.current(ADSBX)['state'] == 'ok'
    assert fake_host.calls == []          # no re-read: the poller's result was used


# ── routes ───────────────────────────────────────────────────────────────────
@pytest.fixture
def client():
    appmod.app.config['TESTING'] = True
    return appmod.app.test_client()


@pytest.fixture
def two_feeders(monkeypatch):
    import routes.dashboard as dash
    feeders = [dict(ADSBX), dict(FR24)]
    monkeypatch.setattr(dash, 'load_config', lambda: feeders)
    return feeders


def test_alerts_lists_running_feeder_with_no_data(client, fake_host, ledger_db, two_feeders):
    fake_host.commands[('systemctl', 'is-active', 'adsbexchange-feed')] = Result(0, 'active', '')
    fake_host.commands[('systemctl', 'is-active', 'fr24feed')] = Result(0, 'active', '')
    now = time.time()
    fake_host.files['/run/adsbexchange-feed/status.json'] = {'now': now}
    fake_host.files['/run/adsbexchange-feed/stats.prom'] = prom(0)
    fake_host.files[dataflow.FR24_MONITOR_URL] = {
        'feed_status': 'connected', 'feed_last_ac_sent_time': now, 'feed_num_ac_tracked': 5}
    dataflow.update(ADSBX, now - dataflow.STALE_AFTER_S - 10)   # went quiet 5+ min ago
    dataflow.update(ADSBX, now)

    d = client.get('/api/alerts').get_json()
    assert d['alerts'] == [] and d['any_down'] is False and d['any_alert'] is True
    assert [a['key'] for a in d['data_alerts']] == ['adsbexchange-feed']
    assert 'not connected to feed1.adsbexchange.com' in d['data_alerts'][0]['detail']


def test_alerts_service_down_is_not_also_a_data_alert(client, fake_host, two_feeders):
    d = client.get('/api/alerts').get_json()           # nothing active -> both down
    assert {a['key'] for a in d['alerts']} == {'adsbexchange-feed', 'fr24feed'}
    assert d['data_alerts'] == []


def test_dataflow_events_route(client, ledger_db):
    from system.db import record_dataflow_event
    record_dataflow_event('fr24feed', 'stale', 'FR24 link down')
    record_dataflow_event('fr24feed', 'ok', 'connected')
    ev = client.get('/api/dataflow/events').get_json()['events']
    assert [(e['state'], e['detail']) for e in ev] == [('ok', 'connected'), ('stale', 'FR24 link down')]


def test_events_route_open_on_readonly_port(ledger_db):
    import routes.dashboard  # noqa: F401
    c = appmod.app.test_client()
    appmod._request_port.port = appmod.READONLY_PORT
    try:
        r = c.get('/api/dataflow/events')
    finally:
        appmod._request_port.port = appmod.ADMIN_PORT
    assert r.status_code == 200 and json.loads(r.data)['events'] == []
