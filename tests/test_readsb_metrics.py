"""Characterization tests for readsb_metrics, driven through the Host seam.

Per-feeder "last seen" now comes from system/dataflow.py (tests/test_dataflow.py).
"""
import app as appmod


# ── readsb_metrics (reads aircraft.json + stats.json) ────────────────────────
def test_readsb_metrics_counts_recent_aircraft_and_range(fake_host):
    aircraft = {'aircraft': [
        {'seen': 5, 'r_dst': 100.0},   # readsb reports nm
        {'seen': 10},                  # counted, no range
        {'seen': 999, 'r_dst': 999},   # stale, ignored
    ]}
    stats = {'last1min': {'messages_valid': 600}}
    import os
    fake_host.files = {
        os.path.join(appmod.READSB_JSON, 'aircraft.json'): aircraft,
        os.path.join(appmod.READSB_JSON, 'stats.json'): stats,
    }
    m = appmod.readsb_metrics()
    assert m['aircraft'] == 2
    assert m['max_range_nm'] == 100  # r_dst is already nm
    assert m['msg_rate'] == 10       # round(600 / 60)


def test_readsb_metrics_missing_files_degrade_to_zero(fake_host):
    assert appmod.readsb_metrics() == {'aircraft': 0, 'msg_rate': 0, 'max_range_nm': 0, 'max_range_15m_nm': 0}


def test_readsb_metrics_15min_max_is_converted_from_metres(fake_host):
    import os
    fake_host.files = {os.path.join(appmod.READSB_JSON, 'stats.json'):
                       {'last1min': {'messages_valid': 60}, 'last15min': {'max_distance': 301164}}}
    m = appmod.readsb_metrics()
    assert m['max_range_15m_nm'] == 163      # 301164 m / 1852


def test_readsb_metrics_15min_max_missing_is_zero(fake_host):
    import os
    fake_host.files = {os.path.join(appmod.READSB_JSON, 'stats.json'): {'last1min': {'messages_valid': 60}}}
    assert appmod.readsb_metrics()['max_range_15m_nm'] == 0
