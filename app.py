"""ADS-B Stack Health Monitor — single-file Flask backend.

Serves a one-page dashboard (static/index.html) plus a JSON/SSE API that reports
on a local ADS-B receiving station: the SDR decoder (readsb / airspy_adsb), the
live map (tar1090) and graphs (graphs1090), and every feeder that ships data to
networks like FlightAware, FR24, ADSBExchange, etc.

The whole app is intentionally one file. Reading top to bottom, the sections are:
  - Host adapter      — the single seam to the OS (subprocess + file IO), so the
                        rest of the code is unit-testable with a fake host.
  - Init adapter      — abstracts systemd vs OpenRC vs no-init service control.
  - Uptime ledger     — pure uptime math over recorded status-change events.
  - SQLite history    — persists service up/down events and rolling metrics.
  - Readers           — last-seen, airspy signal, system + readsb stats, versions.
  - Feeder configs    — declarative table of how to read/write each feeder's config.
  - Status / probe    — turns all of the above into a per-feeder health record.
  - Settings writers  — apply edits back to the underlying config files / units.
  - Log streaming     — follow journalctl / docker logs as Server-Sent Events.
  - Background poller  — records status + metrics to SQLite every 30s.
  - Routes            — the HTTP API the frontend calls.

Two ports run the same app: an admin port (full access) and a read-only port
(status/stats only). See `is_readonly` / `admin_required`.
"""
import configparser
import subprocess
import json
import io
import os
import re
import sqlite3
import threading
import time
import urllib.request
import psutil
import shutil
import logging
from collections import namedtuple
from datetime import datetime, timezone
from flask import Flask, jsonify, send_from_directory, Response, stream_with_context, request
from werkzeug.middleware.proxy_fix import ProxyFix

logger = logging.getLogger(__name__)

# ProxyFix trusts one layer of X-Forwarded-* headers so links/IPs are correct
# when the app sits behind a reverse proxy (nginx, Caddy, NPM).
app = Flask(__name__, static_folder='static')
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

# ── Paths & runtime config ───────────────────────────────────────────────────
# Where things live on the host. feeders.ini and history.db sit next to this
# file in /opt/adsb-monitor; the /run and /etc paths are written by the SDR stack.
# The URL/port values come from the systemd unit's Environment= lines (generated
# at install time) and fall back to sane defaults when unset.
# ─────────────────────────────────────────────────────────────────────────────

CONFIG_FILE    = os.path.join(os.path.dirname(__file__), 'feeders.ini')
DB_FILE        = os.path.join(os.path.dirname(__file__), 'history.db')
READSB_JSON    = '/run/readsb'
AIRSPY_STATS   = '/run/airspy_adsb/stats.json'
AIRSPY_DEFAULT = '/etc/default/airspy_adsb'
READSB_DEFAULT = '/etc/default/readsb'
DUMP978_DEFAULT  = '/etc/default/dump978-fa'
SKYAWARE978_JSON = '/run/skyaware978/aircraft.json'
LOG_LINES      = 100

TAR1090_URL_LOCAL     = os.environ.get('TAR1090_URL_LOCAL',     os.environ.get('TAR1090_URL', ''))
GRAPHS1090_URL_LOCAL  = os.environ.get('GRAPHS1090_URL_LOCAL',  os.environ.get('GRAPHS1090_URL', ''))
TAR1090_URL_REMOTE    = os.environ.get('TAR1090_URL_REMOTE',    TAR1090_URL_LOCAL)
GRAPHS1090_URL_REMOTE = os.environ.get('GRAPHS1090_URL_REMOTE', GRAPHS1090_URL_LOCAL)
ADMIN_PORT    = int(os.environ.get('ADMIN_PORT',    '5000'))
READONLY_PORT = int(os.environ.get('READONLY_PORT', '5001'))

# ── Host adapter ───────────────────────────────────────────────────────────
# The ONE place the app talks to the operating system: running commands and
# reading/writing files. Every other function goes through the `HOST` singleton
# instead of calling subprocess/open directly. That single seam is what lets the
# test suite swap in a FakeHost and exercise all the logic without a real Linux
# box. `Result` is a tiny value object bundling (exit code, stdout, stderr).
# Nothing here raises — failures degrade to Result(ok=False) / None, matching the
# monitor's graceful-degradation behaviour everywhere. See CONTEXT.md ("Host").
# ───────────────────────────────────────────────────────────────────────────

class Result:
    __slots__ = ('code', 'out', 'err')
    def __init__(self, code=0, out='', err=''):
        self.code, self.out, self.err = code, out, err
    @property
    def ok(self):
        return self.code == 0

class LinuxHost:
    def run(self, cmd, timeout=10):
        try:
            r = subprocess.run(list(cmd), capture_output=True, text=True, timeout=timeout)
            return Result(r.returncode, r.stdout, r.stderr)
        except Exception as e:
            return Result(1, '', str(e))
    def read_text(self, path):
        try:
            with open(path) as f:
                return f.read()
        except Exception:
            return None
    def read_json(self, path):
        try:
            with open(path) as f:
                return json.load(f)
        except Exception:
            return None
    def write_text(self, path, data):
        with open(path, 'w') as f:
            f.write(data)

HOST = LinuxHost()

# ── Init system adapter ────────────────────────────────────────────────────
# Different distros control services differently: systemd uses `systemctl`,
# Alpine/minimal SBCs use OpenRC's `rc-service`, and some hosts have neither.
# Each adapter exposes the same three operations — status / action (start, stop,
# restart) / running_since — so the rest of the app never branches on init system.
# `detect_init` probes the host once at startup and picks the right one; the
# chosen adapter is the `INIT` singleton. NullAdapter is the graceful fallback
# (monitor-only mode) that reports "service control unavailable". Tests swap the
# INIT singleton via monkeypatch or the fake_init fixture.
# ───────────────────────────────────────────────────────────────────────────

class InitAdapter:
    """Base: status/action/running_since for a named OS service."""
    def status(self, service):
        raise NotImplementedError
    def action(self, service, act):
        raise NotImplementedError
    def running_since(self, service):
        raise NotImplementedError

class SystemdAdapter(InitAdapter):
    def __init__(self, host):
        self._host = host
    def status(self, service):
        r = self._host.run(['systemctl', 'is-active', service], timeout=3)
        state = r.out.strip()
        return ('ok' if state == 'active' else 'error'), state
    def action(self, service, act):
        r = self._host.run(['systemctl', act, service], timeout=10)
        return r.ok, r.out + r.err
    def running_since(self, service):
        r = self._host.run(
            ['systemctl', 'show', service, '--property=ActiveEnterTimestamp'], timeout=3)
        line = r.out.strip()
        if '=' not in line:
            return None
        ts_str = line.split('=', 1)[1].strip()
        if not ts_str or ts_str == 'n/a':
            return None
        for fmt in ['%a %Y-%m-%d %H:%M:%S %Z', '%a %Y-%m-%d %H:%M:%S UTC']:
            try:
                dt = datetime.strptime(ts_str, fmt)
                elapsed = time.time() - dt.replace(tzinfo=timezone.utc).timestamp()
                if elapsed < 0:
                    elapsed = time.time() - dt.timestamp()
                d, rem = divmod(int(abs(elapsed)), 86400)
                h, rem = divmod(rem, 3600)
                m = rem // 60
                if d > 0:
                    return f'{d}d {h}h'
                elif h > 0:
                    return f'{h}h {m}m'
                return f'{m}m'
            except ValueError:
                continue
        return None

class OpenRCAdapter(InitAdapter):
    def __init__(self, host):
        self._host = host
    def status(self, service):
        r = self._host.run(['rc-service', service, 'status'], timeout=3)
        out = r.out.strip()
        return ('ok' if 'started' in out else 'error'), out
    def action(self, service, act):
        r = self._host.run(['rc-service', service, act], timeout=10)
        return r.ok, r.out + r.err
    def running_since(self, service):
        return None

class NullAdapter(InitAdapter):
    _MSG = 'service control unavailable — init system not detected'
    def status(self, service):
        return 'error', self._MSG
    def action(self, service, act):
        return False, self._MSG
    def running_since(self, service):
        return None

def detect_init(host):
    """Probe the host to pick the right InitAdapter."""
    if host.run(['systemctl', '--version'], timeout=3).ok:
        return SystemdAdapter(host)
    if host.run(['rc-service', '--version'], timeout=3).ok:
        return OpenRCAdapter(host)
    return NullAdapter()

INIT = detect_init(HOST)

# ── Port tagging ───────────────────────────────────────────────────────────
# The same app serves two ports: the admin port (full control) and the
# read-only port (status/stats only, safe to expose publicly behind a proxy).
# A per-request thread-local records which port the request arrived on;
# `is_readonly` reads it and the `@admin_required` decorator rejects writes that
# come in on the read-only port with a 403.
# ───────────────────────────────────────────────────────────────────────────

_request_port = threading.local()

def is_readonly():
    return getattr(_request_port, 'port', ADMIN_PORT) == READONLY_PORT

def admin_required(f):
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        if is_readonly():
            return jsonify({'ok': False, 'error': 'Settings not available on this port'}), 403
        return f(*args, **kwargs)
    return decorated

# ── Uptime ledger ──────────────────────────────────────────────────────────
# Uptime is computed from a log of status *changes* (see SQLite history below):
# we only store a row when a service flips ok<->error, not every poll. To get the
# uptime % over any window, `fold_uptime` walks those events and sums the time
# spent in 'ok'. It's a pure function (no DB, no clock) so it's trivially testable.
# `_query_events` fetches the events for a window AND the last event just before
# it — that pre-window "seed" is essential: a service that's been up for days has
# no events inside a recent window, and without the seed it would wrongly read 0%.
# See CONTEXT.md ("Uptime ledger").
# ───────────────────────────────────────────────────────────────────────────

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
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('SELECT ts, status FROM service_events WHERE service=? AND ts >= ? AND ts <= ? ORDER BY ts ASC',
              (service, start, end))
    rows = c.fetchall()
    c.execute('SELECT ts, status FROM service_events WHERE service=? AND ts < ? ORDER BY ts DESC LIMIT 1',
              (service, start))
    seed = c.fetchone()
    conn.close()
    return ([seed] + rows) if seed else rows

# ── SQLite history ─────────────────────────────────────────────────────────
# Persistence layer (history.db). Two tables:
#   service_events — one row per status change, the source for the uptime ledger.
#                    Kept ~10 years; `record_service_event` only inserts when the
#                    status actually differs from the last recorded one.
#   metrics        — rolling aircraft count / message rate / range, sampled every
#                    30s by the background poller, pruned to ~25h (sparkline data).
# The get_* helpers here turn stored rows into the shapes the API/UI want:
# per-day bars, an aggregate %, a metrics time series, and human "running for"
# strings (the last two read live from the init system, not the DB).
# ───────────────────────────────────────────────────────────────────────────

def init_db():
    conn = sqlite3.connect(DB_FILE)
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
    conn = sqlite3.connect(DB_FILE)
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
    conn = sqlite3.connect(DB_FILE)
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
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    since = time.time() - minutes * 60
    c.execute('SELECT ts, aircraft, msg_rate FROM metrics WHERE ts>=? ORDER BY ts ASC', (since,))
    rows = c.fetchall()
    conn.close()
    return [{'ts': r[0], 'aircraft': r[1], 'msg_rate': r[2]} for r in rows]

def get_service_uptime_str(service):
    """Get how long a service has been running (delegates to INIT)."""
    try:
        return INIT.running_since(service)
    except Exception:
        return None

def get_docker_uptime_str(container):
    try:
        r = HOST.run(['docker', 'inspect', '--format', '{{.State.StartedAt}}', container], timeout=5)
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

# ── Feeder last-seen ───────────────────────────────────────────────────────
# "Last seen" = how long ago a feeder last actually produced output, which is a
# better health signal than "is the service running". Most feeders expose a status
# JSON whose modification time we read (FEEDER_STATUS_FILES); FR24 and PiAware
# need their own parsing, so they get dedicated helpers.
# ───────────────────────────────────────────────────────────────────────────

FEEDER_STATUS_FILES = {
    'adsbexchange-feed': '/run/adsbexchange-feed/status.json',
    'adsbfi-feed':       '/run/adsbfi-feed/status.json',
}

def get_feeder_last_seen(key):
    path = FEEDER_STATUS_FILES.get(key)
    if not path:
        return None
    data = HOST.read_json(path)
    if not data:
        return None
    now_ts = data.get('now', 0)
    if now_ts:
        age = time.time() - now_ts
        if age < 120:
            return 'live'
        elif age < 3600:
            return f'{int(age//60)}m ago'
        else:
            return f'{int(age//3600)}h ago'
    return None

def get_fr24_last_seen():
    r = HOST.run(['fr24feed-status'], timeout=5)
    m = re.search(r'connected.*?(\d+)\s*s', r.out + r.err, re.I)
    if m:
        age = int(m.group(1))
        if age < 120: return 'live'
        return f'{age//60}m ago'
    return None

def get_piaware_last_seen():
    r = HOST.run(['piaware-status'], timeout=5)
    if not r.out and not r.err:
        return None
    if 'is connected to FlightAware' in r.out:
        return 'live'
    return 'disconnected'

# ── Airspy signal analysis ─────────────────────────────────────────────────
# Reads airspy_adsb's live stats.json and turns the raw SNR / noise / RSSI
# numbers into a plain-English gain recommendation (raise, lower, or leave it).
# Saturation is judged on the 95th-percentile RSSI rather than the single loudest
# sample, because one aircraft directly overhead pegs the max at any sane gain and
# would otherwise nag forever.
# ───────────────────────────────────────────────────────────────────────────

def get_airspy_stats():
    return HOST.read_json(AIRSPY_STATS) or {}

def gain_recommendation(stats):
    """Analyze airspy stats and suggest gain adjustment."""
    if not stats:
        return None
    snr = stats.get('snr', {})
    noise = stats.get('noise', {})
    rssi = stats.get('rssi', {})
    gain = stats.get('gain', 0)

    median_snr   = snr.get('median', 0)
    median_noise = noise.get('median', 0)
    p95_rssi     = rssi.get('p95', 0)
    max_rssi     = rssi.get('max', 0)

    # Judge saturation on the 95th-percentile RSSI, not the single loudest sample:
    # one aircraft overhead pegs max RSSI to ~68 dB at any sane gain, so a max-based
    # check nags forever and lowering gain never clears it. Fall back to max only if
    # the feed doesn't report p95.
    sat_rssi = p95_rssi or max_rssi
    if sat_rssi >= 67:
        return {'action': 'decrease', 'reason': f'Strong signals near saturation (RSSI {sat_rssi:.0f} dB) — try gain {max(0, int(gain)-2)}'}
    elif median_snr < 8:
        return {'action': 'increase', 'reason': f'Low median SNR ({median_snr:.1f} dB) — try gain {min(21, int(gain)+2)}'}
    elif median_noise > 45:
        return {'action': 'decrease', 'reason': f'High noise floor ({median_noise:.0f} dB) — try gain {max(0, int(gain)-2)}'}
    else:
        return {'action': 'ok', 'reason': f'Signal levels look good (SNR {median_snr:.1f} dB, noise {median_noise:.0f} dB)'}

# ── System metrics ─────────────────────────────────────────────────────────
# Host vitals for the dashboard's system panel — CPU %, memory, disk, load,
# temperature, uptime — read via psutil (with a fallback for CPU temp, which
# psutil can't always see on a Pi).
# ───────────────────────────────────────────────────────────────────────────

def get_system_metrics():
    metrics = {}
    try:
        metrics['cpu_pct']  = psutil.cpu_percent(interval=0.5)
        metrics['mem_pct']  = psutil.virtual_memory().percent
        metrics['mem_used'] = round(psutil.virtual_memory().used / 1024 / 1024)
        metrics['mem_total']= round(psutil.virtual_memory().total / 1024 / 1024)
        temp = psutil.sensors_temperatures() or {}
        for _key in ('cpu_thermal', 'coretemp', 'k10temp', 'acpitz', 'cpu-thermal', 'soc-thermal'):
            if _key in temp:
                metrics['cpu_temp'] = round(temp[_key][0].current, 1)
                break
        du = shutil.disk_usage('/')
        metrics['disk_pct']  = round(du.used / du.total * 100, 1)
        metrics['disk_used'] = round(du.used / 1024 / 1024 / 1024, 1)
        metrics['disk_total']= round(du.total / 1024 / 1024 / 1024, 1)
    except Exception as e:
        metrics['error'] = str(e)
    return metrics

# ── readsb deep stats ──────────────────────────────────────────────────────
# Parses readsb's own stats.json (aircraft seen, message rate, peak range,
# strong-signal %, per-type breakdown) for the detailed receiver-stats panel.
# ───────────────────────────────────────────────────────────────────────────

def get_readsb_deep_stats():
    try:
        stats = HOST.read_json(os.path.join(READSB_JSON, 'stats.json'))
        if not stats:
            return {}
        last  = stats.get('last1min', {})
        total = stats.get('total', {})
        return {
            'msgs_last_min':     last.get('messages_valid', 0),
            'positions_last_min':last.get('position_count_total', 0),
            'msgs_total':        total.get('messages_valid', 0),
            'positions_total':   total.get('position_count_total', 0),
            'adsb_pct': round(
                last.get('position_count_by_type', {}).get('adsb_icao', 0) /
                max(last.get('position_count_total', 1), 1) * 100, 1
            ),
            'uptime_hrs': round((total.get('end', 0) - total.get('start', 0)) / 3600, 1),
        }
    except Exception:
        return {}

# ── Per-band SDR stats (1090 + 978) ────────────────────────────────────────
# One status block per receiver band for the sidebar. 1090 comes from readsb's
# stats.json local block (signal/noise in dBFS → RSSI/SNR/noise) and aircraft.json;
# 978 comes from skyaware978's aircraft.json (per-aircraft RSSI, message count).
# dump978 exposes no noise floor, so the 978 block has RSSI but no SNR/noise.
# ───────────────────────────────────────────────────────────────────────────

# Cumulative UAT message count from the last poll, to derive a per-second rate.
# ponytail: single-value cache, fine for one monitor process; no locking needed.
_uat_msg_cache = {'msgs': None, 'now': None}

def _uat_msg_rate(msgs, now):
    if msgs is None or now is None:
        return None
    prev_m, prev_t = _uat_msg_cache['msgs'], _uat_msg_cache['now']
    _uat_msg_cache['msgs'], _uat_msg_cache['now'] = msgs, now
    if prev_m is None or prev_t is None or now <= prev_t:
        return None
    return round((msgs - prev_m) / (now - prev_t))

def get_band_stats():
    # has_rtl1090: readsb is driving an RTL dongle, not just aggregating an Airspy
    # feed (the Airspy path runs readsb with --net-only and has no local SDR/signal).
    readsb_text = HOST.read_text(READSB_DEFAULT) or ''
    has_rtl1090 = '--device-type rtlsdr' in readsb_text
    out = {
        'has_airspy':  os.path.exists(AIRSPY_DEFAULT),
        'has_rtl1090': has_rtl1090,
        'has_978':     os.path.exists(DUMP978_DEFAULT),
        'b1090': None, 'b978': None,
    }
    uat_count = 0
    if out['has_978']:
        sky = HOST.read_json(SKYAWARE978_JSON) or {}
        acs = [a for a in sky.get('aircraft', []) if a.get('seen', 999) < 60]
        uat_count = len(acs)
        rssis = [a['rssi'] for a in acs
                 if isinstance(a.get('rssi'), (int, float)) and a['rssi'] > -49.4]
        out['b978'] = {
            'active':   systemd_status('dump978-fa')[0] == 'ok',
            'aircraft': uat_count,
            'rssi':     round(sum(rssis) / len(rssis), 1) if rssis else None,
            'msg_rate': _uat_msg_rate(sky.get('messages'), sky.get('now')),
        }
    if has_rtl1090:
        stats = HOST.read_json(os.path.join(READSB_JSON, 'stats.json')) or {}
        local = stats.get('last1min', {}).get('local', {})
        signal, noise = local.get('signal'), local.get('noise')
        ac = HOST.read_json(os.path.join(READSB_JSON, 'aircraft.json')) or {}
        total_ac = len([a for a in ac.get('aircraft', []) if a.get('seen', 999) < 60])
        out['b1090'] = {
            'active':   systemd_status('readsb')[0] == 'ok',
            'aircraft': max(0, total_ac - uat_count),  # readsb json merges 978 in
            'msg_rate': round(stats.get('last1min', {}).get('messages_valid', 0) / 60),
            'rssi':     round(signal, 1) if isinstance(signal, (int, float)) else None,
            'noise':    round(noise, 1) if isinstance(noise, (int, float)) else None,
            'snr':      round(signal - noise, 1)
                        if isinstance(signal, (int, float)) and isinstance(noise, (int, float)) else None,
        }
    return out

# ── Version checking ───────────────────────────────────────────────────────
# Compares the installed version of each stack component against the latest
# published upstream. VERSION_SOURCES declares, per component, how to read the
# installed version locally and where to fetch the latest. Results are cached for
# VERSION_TTL seconds so the dashboard isn't hammering GitHub on every refresh.
# ───────────────────────────────────────────────────────────────────────────

VERSION_SOURCES = {
    'readsb': {
        'installed_cmd': ['readsb', '--version'],
        'installed_re':  r'readsb version:\s*([\d.]+)',
        'latest_url':    'https://raw.githubusercontent.com/wiedehopf/readsb/dev/debian/changelog',
        'latest_re':     r'^readsb \(([\d.]+)\)',
    },
    'tar1090': {
        'installed_files': [
            '/usr/local/share/tar1090/git/version',
            '/usr/local/share/tar1090/version',
            '/usr/share/tar1090/version',
        ],
        'latest_url': 'https://raw.githubusercontent.com/wiedehopf/tar1090/master/version',
    },
    'graphs1090': {
        'installed_file': '/usr/share/graphs1090/version',
        'latest_url':     'https://raw.githubusercontent.com/wiedehopf/graphs1090/master/version',
    },
    'airspy_adsb': {
        'installed_cmd': ['airspy_adsb', '--version'],
        'installed_re':  r'airspy_adsb\s+v?([\d.\w-]+)',
        'latest_url':    None,
    },
}

_version_cache = {}
_version_lock  = threading.Lock()
_version_ts    = 0
VERSION_TTL    = 3600

def _fetch_url(url, timeout=5):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.read().decode('utf-8', errors='replace')
    except Exception:
        return None

def _get_installed_version(key, src):
    if 'installed_files' in src:
        for path in src['installed_files']:
            txt = HOST.read_text(path)
            if txt and txt.strip():
                return txt.strip()
        return None
    if 'installed_file' in src:
        txt = HOST.read_text(src['installed_file'])
        return txt.strip() if txt and txt.strip() else None
    if 'installed_cmd' in src:
        r = HOST.run(src['installed_cmd'], timeout=4)
        text = (r.out + r.err).strip()
        if 'installed_re' in src:
            m = re.search(src['installed_re'], text, re.MULTILINE)
            if m: return m.group(1).strip()
        return text.split('\n')[0][:40] if text else None
    return None

def _get_latest_version(key, src):
    url = src.get('latest_url')
    if not url: return None
    text = _fetch_url(url)
    if not text: return None
    if 'latest_re' in src:
        m = re.search(src['latest_re'], text, re.MULTILINE)
        return m.group(1) if m else None
    return text.strip().split('\n')[0][:40]

def _is_outdated(installed, latest):
    if not installed or not latest: return False
    return installed.strip() != latest.strip()

def refresh_versions():
    global _version_ts
    result = {}
    for key, src in VERSION_SOURCES.items():
        installed = _get_installed_version(key, src)
        latest    = _get_latest_version(key, src)
        result[key] = {'installed': installed, 'latest': latest, 'outdated': _is_outdated(installed, latest)}
    with _version_lock:
        _version_cache.update(result)
        _version_ts = time.time()

def get_versions():
    global _version_ts
    if time.time() - _version_ts > VERSION_TTL:
        t = threading.Thread(target=refresh_versions, daemon=True)
        t.start()
        # Never block — return cache immediately (may be empty on first call)
    with _version_lock:
        return dict(_version_cache)

# ── Airspy detection ───────────────────────────────────────────────────────
# Identifies which Airspy model is plugged in (Mini vs R2) from its USB id, so
# the UI can suggest the correct sample rate, and surfaces a live one-line hint.
# ───────────────────────────────────────────────────────────────────────────

MINI_IDS = {'60a1'}
R2_IDS   = {'60a8', '0002'}

def detect_airspy_model():
    for line in HOST.run(['lsusb'], timeout=5).out.splitlines():
        if '1d50' not in line.lower() and 'airspy' not in line.lower():
            continue
        m = re.search(r'1d50:([0-9a-f]{4})', line.lower())
        if m:
            pid = m.group(1)
            if pid in MINI_IDS: return 'mini'
            elif pid in R2_IDS: return 'r2'
        if 'mini' in line.lower(): return 'mini'
    info = HOST.run(['airspy_info'], timeout=5)
    text = (info.out + info.err).lower()
    if 'mini' in text: return 'mini'
    elif 'r2' in text or 'r820' in text: return 'r2'
    return 'unknown'

def airspy_live_hint():
    try:
        settings = parse_airspy_options(HOST.read_text(AIRSPY_DEFAULT) or '')
        model = detect_airspy_model()
        rate  = settings.get('sample_rate', '?')
        gain  = settings.get('gain', '?')
        label = 'Mini' if model == 'mini' else 'R2' if model == 'r2' else ''
        return f'{rate} MSPS{" ("+label+")" if label else ""} · gain {gain} · Beast → :30004'
    except Exception:
        return '6 MSPS · gain 21 · Beast → :30004'

# ── Feeder configs ─────────────────────────────────────────────────────────
# Every feeder stores its settings differently — flat INI, shell-style KEY=val
# /etc/default files, piaware-config, or env vars baked into a docker container.
# FEEDER_CONFIGS is a declarative table describing, per feeder, where its config
# lives, which format it uses, and which fields the UI may edit. The read_*/write_*
# helpers below implement each format once; the table wires feeders to them so
# adding a feeder is data, not new code.
# ───────────────────────────────────────────────────────────────────────────

FEEDER_CONFIGS = {
    'fr24feed': {
        'label': 'FlightRadar24',
        'config_file': '/etc/fr24feed.ini',
        'format': 'ini_flat',
        'fields': [
            {'key': 'fr24key',   'label': 'Sharing Key',  'type': 'password'},
            {'key': 'host',      'label': 'Beast Host',   'type': 'text',   'default': '127.0.0.1'},
            {'key': 'port',      'label': 'Beast Port',   'type': 'text',   'default': '30005'},
            {'key': 'bs',        'label': 'Beast Format', 'type': 'select', 'options': ['yes','no'], 'default': 'yes'},
            {'key': 'mlat',      'label': 'MLAT',         'type': 'select', 'options': ['yes','no'], 'default': 'no'},
        ],
    },
    'piaware': {
        'label': 'FlightAware (PiAware)',
        'config_cmd_read': ['piaware-config', '--show'],
        'write_via_cmd': True,
        'write_cmd_template': ['piaware-config', '{key}', '{value}'],
        'restart_after_write': True,
        'fields': [
            {'key': 'feeder-id',       'label': 'Feeder ID (UUID)', 'type': 'text'},
            {'key': 'receiver-host',   'label': 'Receiver Host',    'type': 'text', 'default': 'localhost'},
            {'key': 'receiver-port',   'label': 'Receiver Port',    'type': 'text', 'default': '30005'},
            {'key': 'flightaware-user','label': 'FlightAware User', 'type': 'text'},
        ],
    },
    'adsbexchange-feed': {
        'label': 'ADSBExchange',
        'config_file': '/etc/default/adsbexchange-feed',
        'format': 'shell_vars',
        'extra_files': {'UUID': '/usr/local/share/adsbexchange/adsbexchange-uuid'},
        'fields': [
            {'key': 'UUID',        'label': 'Station UUID', 'type': 'text', 'readonly': True, 'source': 'extra'},
            {'key': 'ADSBX_USER', 'label': 'Username',     'type': 'text'},
            {'key': 'LAT',         'label': 'Latitude',    'type': 'text'},
            {'key': 'LONG',        'label': 'Longitude',   'type': 'text'},
            {'key': 'ALT',         'label': 'Altitude (ft)','type': 'text'},
        ],
    },
    'adsbfi-feed': {
        'label': 'adsb.fi',
        'extra_files': {'UUID': '/usr/local/share/adsbfi/adsbfi-uuid'},
        'fields': [
            {'key': 'UUID', 'label': 'Station UUID', 'type': 'text', 'readonly': True, 'source': 'extra'},
        ],
    },
    'airnavradar': {
        'label': 'AirNav Radar',
        'docker_container': 'airnavradar',
        'fields': [
            {'key': 'SHARING_KEY', 'label': 'Sharing Key', 'type': 'password'},
            {'key': 'LAT',         'label': 'Latitude',    'type': 'text'},
            {'key': 'LONG',        'label': 'Longitude',   'type': 'text'},
            {'key': 'ALT',         'label': 'Altitude (m)','type': 'text'},
        ],
    },
}

def read_flat_ini(path):
    vals = {}
    for line in (HOST.read_text(path) or '').splitlines():
        line = line.strip()
        if not line or line.startswith('#'): continue
        if '=' in line:
            k, _, v = line.partition('=')
            vals[k.strip()] = v.strip().strip('"').strip("'")
    return vals

def write_flat_ini(path, data):
    HOST.write_text(path, ''.join(f'{k}={v}\n' for k, v in data.items()))

def read_shell_vars(path):
    vals = {}
    for line in (HOST.read_text(path) or '').splitlines():
        line = line.strip().lstrip('export').strip()
        if not line or line.startswith('#'): continue
        if '=' in line:
            k, _, v = line.partition('=')
            vals[k.strip()] = v.strip().strip('"').strip("'")
    return vals

def write_shell_vars(path, data):
    existing = HOST.read_text(path) or ''
    lines = existing.splitlines()
    updated = set()
    out = []
    for line in lines:
        stripped = line.strip().lstrip('export').strip()
        if '=' in stripped and not stripped.startswith('#'):
            k = stripped.split('=')[0].strip()
            if k in data:
                out.append(f'{k}="{data[k]}"')
                updated.add(k)
                continue
        out.append(line)
    for k, v in data.items():
        if k not in updated:
            out.append(f'{k}="{v}"')
    HOST.write_text(path, '\n'.join(out) + '\n')

def read_piaware_config(fields):
    vals = {}
    r = HOST.run(['piaware-config', '--show'], timeout=5)
    for line in r.out.splitlines():
        if ' ' in line:
            k, _, v = line.partition(' ')
            vals[k.strip()] = v.strip().split()[0]
    return vals

def read_docker_env(container):
    vals = {}
    r = HOST.run(
        ['docker', 'inspect', '--format', '{{range .Config.Env}}{{println .}}{{end}}', container],
        timeout=5)
    for line in r.out.splitlines():
        if '=' in line:
            k, _, v = line.partition('=')
            vals[k.strip()] = v.strip()
    return vals

# ── Config store ───────────────────────────────────────────────────────────
# The write side of feeder settings. Each format has a `_write_*` function with
# the same (cfg, data) -> (ok, message) shape, paired with its reader in the
# CONFIG_ADAPTERS table. `_config_adapter` picks the right one for a feeder, and
# get_/set_feeder_settings are the two public entry points the routes call.
# `_write_docker` recreates the container atomically: it renames the old one
# aside, runs the new one, and only deletes the backup on success (rolling back
# on failure) so a bad value can never leave the feeder gone. See CONTEXT.md
# ("Config store").
# ───────────────────────────────────────────────────────────────────────────

Adapter = namedtuple('Adapter', ['read', 'write'])

def _writable_only(cfg, data):
    """Drop readonly fields — file formats filter, docker/piaware historically don't."""
    writable = {f['key'] for f in cfg.get('fields', []) if not f.get('readonly')}
    return {k: v for k, v in data.items() if k in writable}

def _write_ini_flat(cfg, data):
    existing = read_flat_ini(cfg['config_file'])
    existing.update(_writable_only(cfg, data))
    write_flat_ini(cfg['config_file'], existing)
    return True, 'Saved'

def _write_shell_vars(cfg, data):
    write_shell_vars(cfg['config_file'], _writable_only(cfg, data))
    return True, 'Saved'

def _write_piaware(cfg, data):
    for k, v in data.items():
        if v: HOST.run(['piaware-config', k, v], timeout=5)
    return True, 'Saved'

def _write_docker(cfg, data):
    container = cfg['docker_container']
    current = read_docker_env(container)
    current.update({k: v for k, v in data.items() if v != ''})
    r = HOST.run(['docker', 'inspect', container], timeout=5)
    info = json.loads(r.out)[0]
    image = info['Config']['Image']
    extra_hosts = info['HostConfig'].get('ExtraHosts') or []
    env_args  = [arg for k, v in current.items() for arg in ['-e', f'{k}={v}']]
    host_args = [arg for h in extra_hosts for arg in ['--add-host', h]]
    # Rename the old container aside instead of deleting it, so a failed
    # `docker run` (bad env value, missing image, ...) can be rolled back —
    # otherwise the feeder is gone with no way back.
    backup = f'{container}_bak'
    HOST.run(['docker', 'rm', '-f', backup], timeout=10)   # clear any stale backup
    HOST.run(['docker', 'stop', container], timeout=10)
    HOST.run(['docker', 'rename', container, backup], timeout=10)
    run = HOST.run(['docker', 'run', '-d', '--name', container, '--restart', 'unless-stopped']
                   + env_args + host_args + [image], timeout=15)
    if not run.ok:
        # roll back: restore and restart the original container
        HOST.run(['docker', 'rename', backup, container], timeout=10)
        HOST.run(['docker', 'start', container], timeout=10)
        return False, run.err or 'docker run failed'
    HOST.run(['docker', 'rm', '-f', backup], timeout=10)   # success: drop backup
    return True, 'Container recreated'

CONFIG_ADAPTERS = {
    'ini_flat':   Adapter(lambda cfg: read_flat_ini(cfg['config_file']),         _write_ini_flat),
    'shell_vars': Adapter(lambda cfg: read_shell_vars(cfg['config_file']),       _write_shell_vars),
    'piaware':    Adapter(lambda cfg: read_piaware_config(cfg.get('fields', [])), _write_piaware),
    'docker':     Adapter(lambda cfg: read_docker_env(cfg['docker_container']),  _write_docker),
}

def _config_adapter(cfg):
    """Select the format adapter once — used by both get and set."""
    if 'docker_container' in cfg:
        return CONFIG_ADAPTERS['docker']
    if 'config_file' in cfg:
        return CONFIG_ADAPTERS.get(cfg.get('format', 'ini_flat'))
    if cfg.get('config_cmd_read') or cfg.get('write_via_cmd'):
        return CONFIG_ADAPTERS['piaware']
    return None

def get_feeder_settings(key):
    cfg = FEEDER_CONFIGS.get(key)
    if not cfg: return {}
    adapter = _config_adapter(cfg)
    vals = adapter.read(cfg) if adapter else {}
    for fname, fpath in cfg.get('extra_files', {}).items():
        vals[fname] = (HOST.read_text(fpath) or '').strip()
    return {field['key']: vals.get(field['key'], field.get('default', '')) for field in cfg.get('fields', [])}

def set_feeder_settings(key, data):
    cfg = FEEDER_CONFIGS.get(key)
    if not cfg: return False, 'Unknown feeder'
    adapter = _config_adapter(cfg)
    if not adapter:
        return False, 'No write method defined'
    try:
        return adapter.write(cfg, data)
    except Exception:
        logger.exception("Failed to write feeder settings for key=%s", key)
        return False, 'Failed to update feeder settings'

# ── Config ─────────────────────────────────────────────────────────────────
# Loads the feeder list from feeders.ini — the [service:<unit>] / [docker:<name>]
# sections that define what shows up in the sidebar. `load_config` parses them,
# `get_config_map` indexes by key, and `save_feeders` writes the list back when
# the user edits sidebar entries in Settings.
# ───────────────────────────────────────────────────────────────────────────

def load_config():
    cfg = configparser.ConfigParser()
    cfg.read(CONFIG_FILE)
    items = []
    for section in cfg.sections():
        if ':' not in section: continue
        kind, name = section.split(':', 1)
        if kind not in ('service', 'docker'): continue
        items.append({
            'key': name, 'kind': kind,
            'label': cfg.get(section, 'label', fallback=name),
            'hint':  cfg.get(section, 'hint',  fallback=''),
            'icon':  cfg.get(section, 'icon',  fallback='plug'),
        })
    return items

def get_config_map():
    return {f['key']: f for f in load_config()}

def save_feeders(feeders):
    cfg = configparser.ConfigParser()
    for f in feeders:
        section = f'{f["kind"]}:{f["key"]}'
        cfg[section] = {'label': f.get('label', f['key']), 'hint': f.get('hint', ''), 'icon': f.get('icon', 'plug')}
    buf = io.StringIO()
    buf.write('# ADS-B Stack Monitor - Feeder Configuration\n')
    buf.write('# Section types: [service:<unit>] or [docker:<container>]\n\n')
    cfg.write(buf)
    HOST.write_text(CONFIG_FILE, buf.getvalue())

# ── Status ─────────────────────────────────────────────────────────────────
# Thin wrappers that delegate service status and start/stop/restart actions to
# the active INIT adapter (and to docker for container feeders).
# ───────────────────────────────────────────────────────────────────────────

def systemd_status(service):
    return INIT.status(service)

def docker_status(container):
    r = HOST.run(['docker', 'inspect', '--format', '{{.State.Status}}', container], timeout=5)
    state = r.out.strip()
    if not state: return 'error', 'not found'
    return ('ok' if state == 'running' else 'error'), state

def service_action(service, action):
    return INIT.action(service, action)

# ── Feeder probe ───────────────────────────────────────────────────────────
# Ties the readers together into one health record per feeder. `probe` returns a
# FeederHealth (status, detail, last-seen, running-for) — the single object the
# /api/status route renders into each sidebar card. `readsb_metrics` pulls the
# headline numbers (aircraft, message rate, range) for the metrics panel.
# Callers never branch on Feeder kind — the dispatch lives here once. See CONTEXT.md.
# ───────────────────────────────────────────────────────────────────────────

class FeederHealth:
    __slots__ = ('status', 'detail', 'last_seen', 'running_for')
    def __init__(self, status, detail, last_seen=None, running_for=None):
        self.status, self.detail = status, detail
        self.last_seen, self.running_for = last_seen, running_for

def feeder_status(feeder):
    """The single kind-dispatch: (status, detail) for a service or docker Feeder."""
    if feeder['kind'] == 'service':
        return INIT.status(feeder['key'])
    return docker_status(feeder['key'])

def _feeder_last_seen(key):
    if key in FEEDER_STATUS_FILES:
        return get_feeder_last_seen(key)
    if key == 'fr24feed':
        return get_fr24_last_seen()
    if key == 'piaware':
        return get_piaware_last_seen()
    return None

def _feeder_running_for(feeder):
    if feeder['kind'] == 'service':
        return get_service_uptime_str(feeder['key'])
    return get_docker_uptime_str(feeder['key'])

def probe(feeder):
    """Compose full Feeder health from the status, last-seen and running-for resolvers."""
    status, detail = feeder_status(feeder)
    return FeederHealth(status, detail,
                        last_seen=_feeder_last_seen(feeder['key']),
                        running_for=_feeder_running_for(feeder))

def readsb_metrics():
    metrics = {'aircraft': 0, 'msg_rate': 0, 'max_range_nm': 0}
    ac_data = HOST.read_json(os.path.join(READSB_JSON, 'aircraft.json'))
    if ac_data:
        aircraft = [a for a in ac_data.get('aircraft', []) if a.get('seen', 999) < 60]
        metrics['aircraft'] = len(aircraft)
        ranges = [a['r_dst'] for a in aircraft if 'r_dst' in a]
        if ranges: metrics['max_range_nm'] = round(max(ranges) * 0.539957)
    stats = HOST.read_json(os.path.join(READSB_JSON, 'stats.json'))
    if stats:
        msgs = stats.get('last1min', {}).get('messages_valid', 0)
        metrics['msg_rate'] = round(msgs / 60)
    return metrics

# ── Airspy / Receiver settings ─────────────────────────────────────────────
# Read/modify the two core SDR config files (/etc/default/airspy_adsb and
# /etc/default/readsb). These parse the existing options string, merge in the
# user's changes (gain, sample rate, lat/lon, max range) and write it back,
# preserving any flags the UI doesn't manage. The routes restart the affected
# service afterwards so changes take effect.
# ───────────────────────────────────────────────────────────────────────────

def parse_airspy_options(text):
    s = {'gain': '21', 'sample_rate': '6', 'options': ''}
    for line in text.splitlines():
        line = line.strip()
        if line.startswith('#') or '=' not in line: continue
        k, _, v = line.partition('=')
        k, v = k.strip(), v.strip().strip('"')
        if k == 'GAIN': s['gain'] = v
        elif k == 'SAMPLE_RATE': s['sample_rate'] = v
        elif k == 'OPTIONS': s['options'] = v
    return s

def write_airspy_options(settings):
    existing = HOST.read_text(AIRSPY_DEFAULT) or ''
    updates = {
        'GAIN':        settings.get('gain', '21'),
        'SAMPLE_RATE': settings.get('sample_rate', '6'),
        'OPTIONS':     f'"{settings.get("options", "-v -f 1 -w 5 -C 95 -E 60")}"',
    }
    updated = set()
    out = []
    for line in existing.splitlines():
        stripped = line.strip()
        if stripped.startswith('#') or '=' not in stripped:
            out.append(line); continue
        key = stripped.split('=')[0].strip()
        if key in updates:
            out.append(f'{key}={updates[key]}')
            updated.add(key)
        else:
            out.append(line)
    for key, val in updates.items():
        if key not in updated:
            out.append(f'{key}={val}')
    existing_keys = {l.split('=')[0].strip() for l in out if '=' in l and not l.strip().startswith('#')}
    required = {'AFFINITY': '0', 'NET': '-l 47787:beast -c 127.0.0.1:30004:beast',
                'STATS': '-S /run/airspy_adsb/stats.json', 'G': '', 'M': ''}
    for key, default in required.items():
        if key not in existing_keys:
            out.append(f'{key}={default}')
    HOST.write_text(AIRSPY_DEFAULT, '\n'.join(out) + '\n')

def parse_receiver_options(text):
    s = {'lat': '', 'lon': '', 'max_range': '500'}
    for line in text.splitlines():
        if 'RECEIVER_OPTIONS' in line:
            for flag, key in [('--lat','lat'),('--lon','lon'),('--max-range','max_range')]:
                m = re.search(rf'{flag}\s+([\d.-]+)', line)
                if m: s[key] = m.group(1)
    return s

def write_receiver_options(text, new_settings):
    lines = text.splitlines()
    out = []
    for line in lines:
        if 'RECEIVER_OPTIONS' in line:
            for flag, key in [('--lat','lat'),('--lon','lon'),('--max-range','max_range')]:
                val = new_settings.get(key, '')
                if not val: continue
                if re.search(rf'{flag}\s+[\d.-]+', line):
                    line = re.sub(rf'{flag}\s+[\d.-]+', f'{flag} {val}', line)
                else:
                    line = line.rstrip('"').rstrip() + f' {flag} {val}"'
        out.append(line)
    return '\n'.join(out) + '\n'

# ── Per-SDR gain + bias-tee (1090 readsb / 978 dump978) ────────────────────
# Gain and bias-tee for each RTL stick. 1090 gain is readsb's --gain (auto or a
# dB number); 978 gain is dump978's --sdr-gain (a number, or 'max' = no flag).
# Bias-tee differs: readsb has no flag, so 1090 uses an rtl_biast ExecStartPre
# drop-in (resolves the stick by serial, powers the LNA before readsb opens it);
# 978 uses SoapySDR's biastee=true device arg. Bias-tee feeds DC up the coax —
# only enable it with an inline LNA that expects it.
# ───────────────────────────────────────────────────────────────────────────

BIASTEE_1090_CONF = '/etc/systemd/system/readsb.service.d/biastee.conf'

def _opt_in_receiver(text, flag):
    for line in text.splitlines():
        if 'RECEIVER_OPTIONS' in line:
            m = re.search(rf'{flag}[ =]([^\s"]+)', line)
            if m: return m.group(1)
    return None

def parse_sdr1090():
    text = HOST.read_text(READSB_DEFAULT) or ''
    return {'gain': _opt_in_receiver(text, '--gain') or 'auto',
            'biastee': os.path.exists(BIASTEE_1090_CONF)}

def _set_receiver_flag(text, flag, value):
    """Set/replace `flag value` inside RECEIVER_OPTIONS="...". value None removes it."""
    out = []
    for line in text.splitlines():
        if 'RECEIVER_OPTIONS' in line:
            line = re.sub(rf'\s*{flag}[ =][^\s"]+', '', line)
            if value is not None:
                line = line.rstrip('"').rstrip() + f' {flag} {value}"'
        out.append(line)
    return '\n'.join(out) + '\n'

def write_sdr1090(gain, biastee):
    text = HOST.read_text(READSB_DEFAULT) or ''
    HOST.write_text(READSB_DEFAULT, _set_receiver_flag(text, '--gain', gain or 'auto'))
    serial = _opt_in_receiver(text, '--device') or ''
    if biastee:
        if serial:
            pre = (f'/bin/sh -c "for i in 0 1 2 3; do rtl_eeprom -d $i 2>/dev/null '
                   f'| grep -q {serial} && {{ rtl_biast -d $i -b 1; break; }}; done; true"')
        else:
            pre = '/bin/sh -c "rtl_biast -d 0 -b 1; true"'
        HOST.run(['mkdir', '-p', os.path.dirname(BIASTEE_1090_CONF)])
        HOST.write_text(BIASTEE_1090_CONF, f'[Service]\nExecStartPre={pre}\n')
    else:
        HOST.run(['rm', '-f', BIASTEE_1090_CONF])
    HOST.run(['systemctl', 'daemon-reload'])

def parse_sdr978():
    text = HOST.read_text(DUMP978_DEFAULT) or ''
    gain, biastee = 'max', False
    for line in text.splitlines():
        if 'RECEIVER_OPTIONS' in line:
            m = re.search(r'--sdr-gain[ =]([^\s"]+)', line)
            if m: gain = m.group(1)
            if 'biastee=true' in line: biastee = True
    return {'gain': gain, 'biastee': biastee}

def write_sdr978(gain, biastee):
    text = HOST.read_text(DUMP978_DEFAULT) or ''
    out = []
    for line in text.splitlines():
        if 'RECEIVER_OPTIONS' in line:
            line = re.sub(r'\s*--sdr-gain[ =][^\s"]+', '', line)
            if gain and gain not in ('max', 'auto'):
                line = line.rstrip('"').rstrip() + f' --sdr-gain {gain}"'
            line = line.replace(',biastee=true', '')
            if biastee:
                line = re.sub(r'(--sdr driver=rtlsdr,serial=[^\s",]+)', r'\1,biastee=true', line)
        out.append(line)
    HOST.write_text(DUMP978_DEFAULT, '\n'.join(out) + '\n')

# ── Log streaming ──────────────────────────────────────────────────────────
# Streams live logs to the browser via Server-Sent Events. `_log_command` builds
# the follow command (`journalctl -u <unit> -f` for services, `docker logs -f`
# for containers); `stream_logs` spawns it and yields each line as an SSE frame.
# journalctl is wrapped in `stdbuf -oL` to force line-buffering — without it,
# journalctl block-buffers when piped and a quiet unit's stream looks dead.
# `_sse` is the one-line JSON framing helper.
# ───────────────────────────────────────────────────────────────────────────

def _sse(text):
    """Frame one line of text as a Server-Sent Events data message."""
    return f"data: {json.dumps(text)}\n\n"

def _journalctl(*unit_args):
    """Follow-logs journalctl invocation. stdbuf -oL forces line-buffered output:
    journalctl block-buffers when its stdout is a pipe (non-tty), so quiet units
    never flush and the stream looks like it never connected. coreutils stdbuf
    ships on every systemd host."""
    return ['stdbuf', '-oL', '-eL',
            'journalctl', *unit_args, '-f', '-n', str(LOG_LINES), '--no-pager', '--output=short-iso']

def _log_command(feeder):
    """The follow-logs command for a Feeder — the single kind-dispatch for logs."""
    if feeder['kind'] == 'docker':
        return ['docker', 'logs', '-f', '--tail', str(LOG_LINES), '--timestamps', feeder['key']]
    return _journalctl('-u', feeder['key'])

def stream_logs(cmd):
    """Spawn a follow-logs process and yield its lines as SSE frames."""
    proc = None
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, bufsize=1)
        yield _sse('[connected]')
        for line in proc.stdout:
            yield _sse(line.rstrip())
        # stdout closed = the follow process exited on its own (unknown unit, no
        # container, journalctl/docker missing). Surface it instead of looking idle.
        code = proc.wait()
        if code:
            yield _sse(f'[stream ended — exit {code}; unit may have no journal or not exist]')
    except GeneratorExit:
        if proc: proc.kill()
    except Exception as e:
        yield _sse(f'[error] {e}')
    finally:
        if proc:
            try: proc.kill()
            except Exception: pass

# ── Background poller ──────────────────────────────────────────────────────
# A daemon thread (started at the bottom of the file) that runs forever: every
# 30s it records each service's status into the events ledger and samples readsb
# metrics into the metrics table. This is what populates the uptime history and
# sparklines over time, independent of whether anyone has the dashboard open.
# ───────────────────────────────────────────────────────────────────────────

def background_poll():
    """Record service states and metrics to SQLite every 30s."""
    consecutive_errors = 0
    while True:
        try:
            feeders = load_config()
            for f in feeders:
                try:
                    status, _ = feeder_status(f)
                    record_service_event(f['key'], status)
                except Exception:
                    pass
            m = readsb_metrics()
            record_metrics(m['aircraft'], m['msg_rate'], m['max_range_nm'])
            consecutive_errors = 0
        except Exception:
            consecutive_errors += 1
        # Backoff: 30s normal, up to 5min if errors persist
        sleep_time = min(30 * (2 ** min(consecutive_errors, 4)), 300)
        time.sleep(sleep_time)

# ── Routes ─────────────────────────────────────────────────────────────────
# The HTTP API the single-page frontend polls. Read endpoints (/api/status,
# /api/stats/*, /api/alerts, /api/versions, logs) work on both ports; write
# endpoints (settings, service control, backup, system log) carry @admin_required
# so they only work on the admin port. Each route is a thin shell that calls the
# functions defined above and returns JSON (or an SSE stream for logs).
# ───────────────────────────────────────────────────────────────────────────

@app.route('/api/alerts')
def api_alerts():
    """Return any services currently down."""
    feeders = load_config()
    alerts = []
    for f in feeders:
        status, detail = feeder_status(f)
        if status != 'ok':
            alerts.append({'key': f['key'], 'label': f['label'], 'detail': detail})
    return jsonify({'alerts': alerts, 'any_down': len(alerts) > 0})


@app.route('/api/mode')
def api_mode():
    return jsonify({'readonly': is_readonly()})

@app.route('/api/ui-config')
def api_ui_config():
    if is_readonly():
        base = GRAPHS1090_URL_REMOTE
        return jsonify({
            'tar1090_url':       TAR1090_URL_REMOTE,
            'graphs1090_url':    base,
            'graphs1090_airspy': f'{base}/?graph=airspy_snr&timeframe=8h',
        })
    base = GRAPHS1090_URL_LOCAL
    return jsonify({
        'tar1090_url':       TAR1090_URL_LOCAL,
        'graphs1090_url':    base,
        'graphs1090_airspy': f'{base}/?graph=airspy_snr&timeframe=8h',
    })

@app.route('/api/status')
def api_status():
    feeders  = load_config()
    versions = get_versions()
    results  = []
    for f in feeders:
        health = probe(f)
        ver    = versions.get(f['key'], {})
        hint   = airspy_live_hint() if f['key'] == 'airspy_adsb' else f.get('hint', '')

        uptime_pct  = get_service_uptime_pct(f['key'], days=7)
        uptime_bars = get_uptime_bars(f['key'], days=7)

        results.append({
            **f, 'status': health.status, 'detail': health.detail, 'hint': hint,
            'version': ver.get('installed'), 'latest': ver.get('latest'),
            'outdated': ver.get('outdated', False),
            'uptime_str':  health.running_for,
            'uptime_pct':  uptime_pct,
            'uptime_bars': uptime_bars,
            'last_seen':   health.last_seen,
        })
    return jsonify({'feeders': results, 'metrics': readsb_metrics()})

@app.route('/api/stats/airspy')
def api_airspy_stats():
    stats = get_airspy_stats()
    return jsonify({
        'stats': stats,
        'recommendation': gain_recommendation(stats),
    })

@app.route('/api/stats/readsb')
def api_readsb_stats():
    return jsonify(get_readsb_deep_stats())

@app.route('/api/stats/bands')
def api_band_stats():
    return jsonify(get_band_stats())

@app.route('/api/stats/aircraft_types')
def api_aircraft_types():
    """Return aircraft type breakdown from readsb stats."""
    try:
        stats = HOST.read_json(os.path.join(READSB_JSON, 'stats.json')) or {}
        types = stats.get('aircraft_count_by_type', {})
        # Group into meaningful categories
        return jsonify({
            'adsb':   types.get('adsb_icao', 0) + types.get('adsb_icao_nt', 0),
            'mlat':   types.get('mlat', 0),
            'mode_s': types.get('mode_s', 0),
            'tisb':   types.get('tisb_icao', 0) + types.get('tisb_other', 0),
            'other':  types.get('other', 0) + types.get('adsr_icao', 0),
            'raw':    types,
        })
    except Exception:
        app.logger.exception("Failed to load aircraft type stats")
        return jsonify({'error': 'Internal server error'}), 500


@app.route('/api/stats/system')
def api_system_stats():
    return jsonify(get_system_metrics())

@app.route('/api/stats/history')
def api_history():
    minutes = int(request.args.get('minutes', 60))
    return jsonify(get_metrics_history(minutes))

@app.route('/api/stats/uptime/history')
def api_uptime_history():
    """Return 7-day daily uptime % for all services."""
    return jsonify({f['key']: _daily_uptime(f['key']) for f in load_config()})


@app.route('/api/stats/uptime/<service>')
def api_service_uptime(service):
    days = int(request.args.get('days', 7))
    pct  = get_service_uptime_pct(service, days)
    return jsonify({'service': service, 'uptime_pct': pct, 'days': days})

@app.route('/api/versions/refresh', methods=['POST'])
@admin_required
def api_versions_refresh():
    global _version_ts
    _version_ts = 0
    t = threading.Thread(target=refresh_versions, daemon=True)
    t.start()
    t.join(timeout=15)
    return jsonify({'ok': True, 'versions': get_versions()})

@app.route('/api/settings/airspy', methods=['GET'])
@admin_required
def get_airspy():
    try:
        settings = parse_airspy_options(HOST.read_text(AIRSPY_DEFAULT) or '')
        model    = detect_airspy_model()
        settings['model'] = model
        settings['recommended_sample_rate'] = '6' if model == 'mini' else '12'
        return jsonify({'ok': True, 'settings': settings})
    except Exception:
        logger.exception("Failed to read Airspy settings")
        return jsonify({'ok': False, 'error': 'An internal error has occurred.'})

@app.route('/api/settings/airspy', methods=['POST'])
@admin_required
def set_airspy():
    try:
        write_airspy_options(request.get_json())
        ok, out = service_action('airspy_adsb', 'restart')
        return jsonify({'ok': ok, 'output': out})
    except Exception:
        logger.exception("Failed to update Airspy settings")
        return jsonify({'ok': False, 'error': 'An internal error has occurred.'})

@app.route('/api/settings/receiver', methods=['GET'])
@admin_required
def get_receiver():
    try:
        return jsonify({'ok': True, 'settings': parse_receiver_options(HOST.read_text(READSB_DEFAULT) or '')})
    except Exception:
        logger.exception("Failed to read receiver settings")
        return jsonify({'ok': False, 'error': 'An internal error has occurred.'})

@app.route('/api/settings/receiver', methods=['POST'])
@admin_required
def set_receiver():
    try:
        text = HOST.read_text(READSB_DEFAULT) or ''
        HOST.write_text(READSB_DEFAULT, write_receiver_options(text, request.get_json()))
        ok, out = service_action('readsb', 'restart')
        return jsonify({'ok': ok, 'output': out})
    except Exception:
        logger.exception("Failed to update receiver settings")
        return jsonify({'ok': False, 'error': 'An internal error has occurred.'})

@app.route('/api/settings/sdr/<band>', methods=['GET'])
@admin_required
def get_sdr(band):
    try:
        if band == '1090': return jsonify({'ok': True, 'settings': parse_sdr1090()})
        if band == '978':  return jsonify({'ok': True, 'settings': parse_sdr978()})
        return jsonify({'ok': False, 'error': 'unknown band'}), 404
    except Exception:
        logger.exception("Failed to read SDR settings")
        return jsonify({'ok': False, 'error': 'An internal error has occurred.'})

@app.route('/api/settings/sdr/<band>', methods=['POST'])
@admin_required
def set_sdr(band):
    try:
        d = request.get_json() or {}
        gain = str(d.get('gain', 'auto')).strip()
        biastee = bool(d.get('biastee', False))
        if band == '1090':
            write_sdr1090(gain, biastee)
            ok, out = service_action('readsb', 'restart')
        elif band == '978':
            write_sdr978(gain, biastee)
            ok, out = service_action('dump978-fa', 'restart')
        else:
            return jsonify({'ok': False, 'error': 'unknown band'}), 404
        return jsonify({'ok': ok, 'output': out})
    except Exception:
        logger.exception("Failed to update SDR settings")
        return jsonify({'ok': False, 'error': 'An internal error has occurred.'})

@app.route('/api/settings/feeders', methods=['GET'])
@admin_required
def get_feeders():
    return jsonify({'ok': True, 'feeders': load_config()})

@app.route('/api/settings/feeders', methods=['POST'])
@admin_required
def set_feeders():
    try:
        save_feeders(request.get_json().get('feeders', []))
        return jsonify({'ok': True})
    except Exception:
        app.logger.exception("Failed to save feeders")
        return jsonify({'ok': False, 'error': 'An internal error occurred'})

@app.route('/api/settings/feeder/<key>', methods=['GET'])
@admin_required
def get_feeder_cfg(key):
    cfg = FEEDER_CONFIGS.get(key)
    if not cfg:
        return jsonify({'ok': False, 'error': 'No config defined for this feeder'})
    return jsonify({'ok': True, 'fields': cfg['fields'], 'settings': get_feeder_settings(key), 'label': cfg['label']})

@app.route('/api/settings/feeder/<key>', methods=['POST'])
@admin_required
def set_feeder_cfg(key):
    try:
        ok, msg = set_feeder_settings(key, request.get_json())
        if ok:
            fmap = {f['key']: f for f in load_config()}
            if key in fmap and fmap[key]['kind'] == 'service':
                service_action(key, 'restart')
        return jsonify({'ok': ok, 'message': msg})
    except Exception:
        app.logger.exception("Failed to update feeder config for key=%s", key)
        return jsonify({'ok': False, 'error': 'An internal error occurred'})

@app.route('/api/service/<name>/restart', methods=['POST'])
@admin_required
def restart_service(name):
    allowed = {f['key'] for f in load_config()}
    if name not in allowed:
        return jsonify({'ok': False, 'error': 'unknown service'})
    ok, out = service_action(name, 'restart')
    return jsonify({'ok': ok, 'output': out})

@app.route('/api/logs/<key>')
@admin_required
def api_logs(key):
    cfg = get_config_map()
    if key not in cfg:
        return Response('data: {"error": "unknown service"}\n\n', status=404, mimetype='text/event-stream')
    entry = cfg[key]
    gen = stream_logs(_log_command(entry))
    return Response(stream_with_context(gen), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})

@app.route('/api/syslog')
@admin_required
def api_syslog():
    """Stream the whole-system journal (all units) for the Settings log window."""
    gen = stream_logs(_journalctl())
    return Response(stream_with_context(gen), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})

# Named config files in the backup. The name (zip arcname) maps back to an exact
# absolute path on restore — a whitelist, so a tampered zip can't write elsewhere.
CONFIG_BACKUP_FILES = {
    'feeders.ini':          CONFIG_FILE,
    'airspy_adsb':          AIRSPY_DEFAULT,
    'readsb':               READSB_DEFAULT,
    'dump978-fa':           DUMP978_DEFAULT,
    'skyaware978':          '/etc/default/skyaware978',
    'fr24feed.ini':         '/etc/fr24feed.ini',
    'piaware.conf':         '/etc/piaware.conf',
    'readsb-biastee.conf':  BIASTEE_1090_CONF,
    'adsb-monitor.service': '/etc/systemd/system/adsb-monitor.service',
}
# collectd RRD base. We back up the whole tree (the host-named subdir varies:
# graphs1090 may use 'localhost' or the real hostname), so the graph data is
# captured regardless, and restored to the same place.
GRAPHS_RRD_DIR = '/var/lib/collectd/rrd'

@app.route('/api/backup')
@admin_required
def api_backup():
    """Download a zip of all config files."""
    import zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        for name, path in CONFIG_BACKUP_FILES.items():
            try:
                zf.write(path, name)
            except Exception:
                pass
    buf.seek(0)
    from flask import send_file
    fname = f'adsb-config-{datetime.now().strftime("%Y%m%d-%H%M%S")}.zip'
    return send_file(buf, mimetype='application/zip',
                     as_attachment=True, download_name=fname)

@app.route('/api/backup/graphs')
@admin_required
def api_backup_graphs():
    """Download a zip of the collectd RRD graph history."""
    import zipfile
    if not os.path.isdir(GRAPHS_RRD_DIR):
        return jsonify({'ok': False, 'error': 'no graph data found'}), 404
    buf = io.BytesIO()
    base = os.path.realpath(GRAPHS_RRD_DIR)
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        for root, _dirs, fnames in os.walk(base):
            for fn in fnames:
                full = os.path.join(root, fn)
                zf.write(full, os.path.relpath(full, base))
    buf.seek(0)
    from flask import send_file
    fname = f'adsb-graphs-{datetime.now().strftime("%Y%m%d-%H%M%S")}.zip'
    return send_file(buf, mimetype='application/zip',
                     as_attachment=True, download_name=fname)

@app.route('/api/restore', methods=['POST'])
@admin_required
def api_restore():
    """Restore config files from an uploaded backup zip (whitelisted names only)."""
    import zipfile
    f = request.files.get('file')
    if not f:
        return jsonify({'ok': False, 'error': 'no file uploaded'}), 400
    restored = []
    try:
        with zipfile.ZipFile(f.stream) as zf:
            for name in zf.namelist():
                dest = CONFIG_BACKUP_FILES.get(name)
                if not dest:
                    continue  # ignore anything not in the whitelist
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                with zf.open(name) as src, open(dest, 'wb') as out:
                    shutil.copyfileobj(src, out)
                restored.append(name)
    except zipfile.BadZipFile:
        return jsonify({'ok': False, 'error': 'not a valid backup zip'}), 400
    except Exception:
        logger.exception("Config restore failed")
        return jsonify({'ok': False, 'error': 'An internal error has occurred.'}), 500
    # Re-apply: reload units (biastee drop-in) and bounce the decoders. Not adsb-monitor
    # itself — that would kill this request before the response is sent.
    HOST.run(['systemctl', 'daemon-reload'])
    for svc in ('readsb', 'dump978-fa'):
        HOST.run(['systemctl', 'try-restart', svc])
    return jsonify({'ok': True, 'restored': restored})

def _safe_path_under_base(base_dir, archive_name, require_rrd=False):
    """Return absolute path under base_dir for an archive entry, or None if invalid."""
    base_real = os.path.realpath(base_dir)
    entry = archive_name.replace('\\', '/')
    if entry.endswith('/'):
        return None
    parts = [p for p in entry.split('/') if p not in ('', '.')]
    if not parts or any(p == '..' for p in parts) or entry.startswith('/'):
        return None
    # Restrict each segment to a conservative safe character set.
    if any(not re.fullmatch(r'[A-Za-z0-9_.-]+', p) for p in parts):
        return None
    if require_rrd:
        # Graph restores must be collectd-style nested paths with a .rrd leaf.
        if len(parts) < 2:
            return None
        if any(not re.fullmatch(r'[A-Za-z0-9_.-]+', p) for p in parts[:-1]):
            return None
        leaf = parts[-1]
        if not leaf.endswith('.rrd'):
            return None
        if not re.fullmatch(r'[A-Za-z0-9_.-]+\.rrd', leaf):
            return None
    safe_name = '/'.join(parts)
    dest = os.path.realpath(os.path.join(base_real, safe_name))
    if os.path.commonpath([base_real, dest]) != base_real:
        return None
    return dest


@app.route('/api/restore/graphs', methods=['POST'])
@admin_required
def api_restore_graphs():
    """Restore collectd RRD graph history from an uploaded zip. Stops collectd first."""
    import zipfile
    f = request.files.get('file')
    if not f:
        return jsonify({'ok': False, 'error': 'no file uploaded'}), 400
    base = os.path.realpath(GRAPHS_RRD_DIR)
    count = 0
    try:
        HOST.run(['systemctl', 'stop', 'collectd'])
        os.makedirs(base, exist_ok=True)
        with zipfile.ZipFile(f.stream) as zf:
            for name in zf.namelist():
                dest = _safe_path_under_base(base, name, require_rrd=True)
                if not dest:
                    continue
                parent_dir = os.path.realpath(os.path.dirname(dest))
                if os.path.commonpath([base, parent_dir]) != base:
                    continue
                os.makedirs(parent_dir, exist_ok=True)
                # Refuse to follow symlinks at write target.
                if os.path.islink(dest):
                    continue
                flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
                if hasattr(os, 'O_NOFOLLOW'):
                    flags |= os.O_NOFOLLOW
                fd = os.open(dest, flags, 0o644)
                try:
                    with zf.open(name) as src, os.fdopen(fd, 'wb') as out:
                        shutil.copyfileobj(src, out)
                except Exception:
                    try:
                        os.close(fd)
                    except Exception:
                        pass
                    raise
                count += 1
    except zipfile.BadZipFile:
        HOST.run(['systemctl', 'start', 'collectd'])
        return jsonify({'ok': False, 'error': 'not a valid graph backup zip'}), 400
    except Exception:
        HOST.run(['systemctl', 'start', 'collectd'])
        logger.exception("Graph restore failed")
        return jsonify({'ok': False, 'error': 'An internal error has occurred.'}), 500
    HOST.run(['systemctl', 'start', 'collectd'])
    HOST.run(['systemctl', 'try-restart', 'graphs1090'])
    return jsonify({'ok': True, 'restored': count})


@app.route('/')
def index():
    return send_from_directory('static', 'index.html')

# ── Server ─────────────────────────────────────────────────────────────────
# Boots the app. Runs two ThreadedWSGIServers over the same Flask app — one per
# port — so each request can be tagged with the port it arrived on (admin vs
# read-only, see Port tagging). __main__ initializes the DB, starts the
# version-refresh and background-poller daemon threads, then serves both ports.
# ───────────────────────────────────────────────────────────────────────────

def make_tagged_app(port):
    def tagged_app(environ, start_response):
        _request_port.port = port
        return app(environ, start_response)
    return tagged_app

def run_server(port):
    from werkzeug.serving import make_server, ThreadedWSGIServer
    server = ThreadedWSGIServer('0.0.0.0', port, make_tagged_app(port))
    server.serve_forever()

if __name__ == '__main__':
    init_db()
    # Start background threads — version refresh is fully async, never blocks startup
    threading.Thread(target=refresh_versions, daemon=True).start()
    threading.Thread(target=background_poll,  daemon=True).start()
    threading.Thread(target=run_server, args=(READONLY_PORT,), daemon=True).start()
    run_server(ADMIN_PORT)
