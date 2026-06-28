"""Storage-side tests for the Uptime ledger — against a temp DB_FILE (ledger_db).

The uptime math itself is covered DB-free in test_fold_uptime; these verify the
SQLite query layer and that the readers wire rows -> fold_uptime correctly.
"""
import sqlite3
import time

import app as appmod

DAY = 86400


def _seed(events):
    """events: list of (ts, status) inserted into service_events."""
    conn = sqlite3.connect(appmod.DB_FILE)
    c = conn.cursor()
    c.executemany('INSERT INTO service_events (ts, service, status) VALUES (?,?,?)',
                  [(ts, 'readsb', status) for ts, status in events])
    conn.commit()
    conn.close()


# ── _query_events: window filtering ──────────────────────────────────────────
def test_query_events_filters_to_window(ledger_db):
    _seed([(100, 'ok'), (200, 'error'), (300, 'ok')])
    rows = appmod._query_events('readsb', 150, 250)
    # in-window event (200,'error'), prefixed with the seed (last event < 150)
    assert rows == [(100, 'ok'), (200, 'error')]


def test_query_events_includes_endpoints(ledger_db):
    _seed([(100, 'ok'), (200, 'error')])
    rows = appmod._query_events('readsb', 100, 200)
    assert rows == [(100, 'ok'), (200, 'error')]


# ── get_service_uptime_pct ───────────────────────────────────────────────────
def test_uptime_pct_none_when_no_events(ledger_db):
    assert appmod.get_service_uptime_pct('readsb', days=7) is None


def test_uptime_pct_three_of_seven_days_up(ledger_db):
    now = time.time()
    # 'ok' starting 3 days ago, nothing before -> ~3/7 of the 7-day window up
    _seed([(now - 3 * DAY, 'ok')])
    pct = appmod.get_service_uptime_pct('readsb', days=7)
    assert pct is not None
    assert abs(pct - round(3 / 7 * 100, 1)) < 0.3


# ── get_uptime_bars ──────────────────────────────────────────────────────────
def test_uptime_bars_returns_one_entry_per_day(ledger_db):
    bars = appmod.get_uptime_bars('readsb', days=7)
    assert len(bars) == 7
    # no events -> each day folds to 0.0 (not None)
    assert all(b == 0.0 for b in bars)


def test_uptime_bars_seed_fills_old_days(ledger_db):
    # Regression: service went 'ok' 30 days ago with no changes since. Every day
    # in the 7-day window must read 100% — not just the day next to the event.
    now = time.time()
    _seed([(now - 30 * DAY, 'ok')])
    bars = appmod.get_uptime_bars('readsb', days=7)
    assert all(b == 100.0 for b in bars)
