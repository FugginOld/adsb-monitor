"""Uptime ledger + SQLite history.

Uptime is computed from a log of status *changes*: we only store a row when
a service flips ok<->error, not every poll. To get the uptime % over any
window, `fold_uptime` walks those events and sums the time spent in 'ok'.
It's a pure function (no DB, no clock) so it's trivially testable.
`_query_events` fetches the events for a window AND the last event just
before it — that pre-window "seed" is essential: a service that's been up
for days has no events inside a recent window, and without the seed it
would wrongly read 0%.

Persistence layer (history.db). Two tables:
  service_events — one row per status change, the source for the uptime
                   ledger. Kept ~10 years; `record_service_event` only
                   inserts when the status actually differs from the last
                   recorded one.
  metrics        — rolling aircraft count / message rate / range, sampled
                   every 30s by the background poller, pruned to ~25h
                   (sparkline data).
The get_* helpers here turn stored rows into the shapes the API/UI want:
per-day bars, an aggregate %, a metrics time series, and human "running for"
strings (the last two read live from the init system, not the DB).

`DB_FILE` and `INIT` stay defined in app.py (conftest.py monkeypatches them
by reassignment), so this module reaches them via `import app`.
"""
import sqlite3
import time
from datetime import datetime

import app


def fold_uptime(rows, start, end):
    """Fraction (0-100) of [start, end] a service was 'ok', from (ts, status) rows.

    Rows may include one pre-window event (ts < start) that seeds the starting status.
    """
    up = 0.0
    prev_ts, prev_status = start, 'unknown'
    for ts, status in rows:
        if ts < start:
            prev_status = status
            continue
        eff = max(ts, start)
        if prev_status == 'ok':
            up += eff - prev_ts
        prev_ts, prev_status = eff, status
    if prev_status == 'ok':
        up += end - prev_ts
    span = end - start
    return round(min(100, up / span * 100), 1) if span > 0 else None

def _query_events(service, start, end):
    """All (ts, status) events for a service in [start, end], oldest first,
    prefixed with the last event before `start` so fold_uptime can seed the
    window's starting status. Without this seed, a continuously-up service
    (one old event, no changes) shows 0% for every day except the one next to
    that event."""
    conn = sqlite3.connect(app.DB_FILE)
    c = conn.cursor()
    c.execute('SELECT ts, status FROM service_events WHERE service=? AND ts >= ? AND ts <= ? ORDER BY ts ASC',
              (service, start, end))
    rows = c.fetchall()
    c.execute('SELECT ts, status FROM service_events WHERE service=? AND ts < ? ORDER BY ts DESC LIMIT 1',
              (service, start))
    seed = c.fetchone()
    conn.close()
    return ([seed] + rows) if seed else rows

def init_db():
    conn = sqlite3.connect(app.DB_FILE)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS service_events (
        id        INTEGER PRIMARY KEY AUTOINCREMENT,
        ts        REAL NOT NULL,
        service   TEXT NOT NULL,
        status    TEXT NOT NULL
    )''')
    c.execute('CREATE INDEX IF NOT EXISTS idx_service_ts ON service_events(service, ts)')
    # Rolling metrics: aircraft count and msg rate every 30s
    c.execute('''CREATE TABLE IF NOT EXISTS metrics (
        id       INTEGER PRIMARY KEY AUTOINCREMENT,
        ts       REAL NOT NULL,
        aircraft INTEGER,
        msg_rate INTEGER,
        max_range_nm INTEGER
    )''')
    c.execute('CREATE INDEX IF NOT EXISTS idx_metrics_ts ON metrics(ts)')
    conn.commit()
    conn.close()

def record_service_event(service, status):
    conn = sqlite3.connect(app.DB_FILE)
    c = conn.cursor()
    # Only record if status changed
    c.execute('SELECT status FROM service_events WHERE service=? ORDER BY ts DESC LIMIT 1', (service,))
    row = c.fetchone()
    if not row or row[0] != status:
        c.execute('INSERT INTO service_events (ts, service, status) VALUES (?,?,?)',
                  (time.time(), service, status))
        # Prune events older than 10 years
        c.execute('DELETE FROM service_events WHERE ts < ?', (time.time() - 3650 * 86400,))
        conn.commit()
    conn.close()

def record_metrics(aircraft, msg_rate, max_range_nm):
    conn = sqlite3.connect(app.DB_FILE)
    c = conn.cursor()
    c.execute('INSERT INTO metrics (ts, aircraft, msg_rate, max_range_nm) VALUES (?,?,?,?)',
              (time.time(), aircraft, msg_rate, max_range_nm))
    # Prune metrics older than 25 hours (keep sparkline data)
    c.execute('DELETE FROM metrics WHERE ts < ?', (time.time() - 90000,))
    conn.commit()
    conn.close()

def _daily_uptime(service, days=7):
    """Per-day uptime as {'day': 'MM/DD', 'pct': float}, oldest first."""
    now = time.time()
    out = []
    for day_offset in range(days - 1, -1, -1):
        day_start = now - (day_offset + 1) * 86400
        day_end   = now - day_offset * 86400
        pct = fold_uptime(_query_events(service, day_start, day_end), day_start, day_end)
        out.append({'day': datetime.fromtimestamp(day_start + 43200).strftime('%m/%d'), 'pct': pct})
    return out

def get_uptime_bars(service, days=7):
    """Daily uptime pct for compact bar display (oldest first)."""
    return [d['pct'] for d in _daily_uptime(service, days)]


def get_service_uptime_pct(service, days=7):
    now = time.time()
    since = now - days * 86400
    rows = _query_events(service, since, now)
    if not rows:
        return None
    return fold_uptime(rows, since, now)

def get_metrics_history(minutes=60):
    conn = sqlite3.connect(app.DB_FILE)
    c = conn.cursor()
    since = time.time() - minutes * 60
    c.execute('SELECT ts, aircraft, msg_rate FROM metrics WHERE ts>=? ORDER BY ts ASC', (since,))
    rows = c.fetchall()
    conn.close()
    return [{'ts': r[0], 'aircraft': r[1], 'msg_rate': r[2]} for r in rows]

def get_service_uptime_str(service):
    """Get how long a service has been running (delegates to INIT)."""
    try:
        return app.INIT.running_since(service)
    except Exception:
        return None

def get_docker_uptime_str(container):
    try:
        r = app.HOST.run(['docker', 'inspect', '--format', '{{.State.StartedAt}}', container], timeout=5)
        ts_str = r.out.strip()
        if ts_str:
            dt = datetime.fromisoformat(ts_str.replace('Z', '+00:00'))
            elapsed = time.time() - dt.timestamp()
            d, rem = divmod(int(elapsed), 86400)
            h, rem = divmod(rem, 3600)
            m = rem // 60
            if d > 0: return f'{d}d {h}h'
            elif h > 0: return f'{h}h {m}m'
            else: return f'{m}m'
    except Exception:
        pass
    return None
