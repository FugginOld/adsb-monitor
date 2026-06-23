import configparser
import subprocess
import json
import os
import re
import sqlite3
import threading
import time
import urllib.request
import psutil
import shutil
from datetime import datetime, timezone
from flask import Flask, jsonify, send_from_directory, Response, stream_with_context, request
from werkzeug.middleware.proxy_fix import ProxyFix

app = Flask(__name__, static_folder='static')
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

CONFIG_FILE    = os.path.join(os.path.dirname(__file__), 'feeders.ini')
DB_FILE        = os.path.join(os.path.dirname(__file__), 'history.db')
READSB_JSON    = '/run/readsb'
AIRSPY_STATS   = '/run/airspy_adsb/stats.json'
AIRSPY_DEFAULT = '/etc/default/airspy_adsb'
READSB_DEFAULT = '/etc/default/readsb'
LOG_LINES      = 100

TAR1090_URL_LOCAL     = os.environ.get('TAR1090_URL_LOCAL',     os.environ.get('TAR1090_URL', ''))
GRAPHS1090_URL_LOCAL  = os.environ.get('GRAPHS1090_URL_LOCAL',  os.environ.get('GRAPHS1090_URL', ''))
TAR1090_URL_REMOTE    = os.environ.get('TAR1090_URL_REMOTE',    TAR1090_URL_LOCAL)
GRAPHS1090_URL_REMOTE = os.environ.get('GRAPHS1090_URL_REMOTE', GRAPHS1090_URL_LOCAL)
ADMIN_PORT    = int(os.environ.get('ADMIN_PORT',    '5000'))
READONLY_PORT = int(os.environ.get('READONLY_PORT', '5001'))

# ── Host adapter ───────────────────────────────────────────────────────────
# The seam between business logic and the Linux host. See CONTEXT.md ("Host").
# Never raises: failures degrade to Result(ok=False) / None, mirroring this
# monitor's everything-degrades-gracefully behaviour. Swap `HOST` in tests.
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

class FakeHost:
    """Test double: answers from canned maps, records every call, reflects writes.

    commands: {(cmd, tuple): Result}   files: {path: text-or-obj}
    Unmatched run -> default_command (a failed Result); unmatched read -> default_file.
    """
    def __init__(self, commands=None, files=None, default_command=None, default_file=None):
        self.commands = {tuple(k): v for k, v in (commands or {}).items()}
        self.files = dict(files or {})
        self.default_command = default_command if default_command is not None else Result(1, '', '')
        self.default_file = default_file
        self.calls = []
        self.writes = {}
    def run(self, cmd, timeout=10):
        key = tuple(cmd)
        self.calls.append(('run', key))
        return self.commands.get(key, self.default_command)
    def read_text(self, path):
        self.calls.append(('read_text', path))
        return self.files.get(path, self.default_file)
    def read_json(self, path):
        self.calls.append(('read_json', path))
        raw = self.files.get(path, self.default_file)
        if raw is None:
            return None
        return json.loads(raw) if isinstance(raw, str) else raw
    def write_text(self, path, data):
        self.calls.append(('write_text', path, data))
        self.writes[path] = data
        self.files[path] = data

HOST = LinuxHost()

# ── Port tagging ───────────────────────────────────────────────────────────
import threading as _threading
_request_port = _threading.local()

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

# ── SQLite history ─────────────────────────────────────────────────────────
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

def get_uptime_bars(service, days=7):
    """Return list of daily uptime pct for compact bar display (oldest first)."""
    now = time.time()
    bars = []
    for day_offset in range(days - 1, -1, -1):
        day_start = now - (day_offset + 1) * 86400
        day_end   = now - day_offset * 86400
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute('SELECT ts, status FROM service_events WHERE service=? AND ts >= ? AND ts <= ? ORDER BY ts ASC',
                  (service, day_start - 86400, day_end))
        rows = c.fetchall()
        conn.close()
        up = 0.0
        prev_ts = day_start
        prev_status = 'unknown'
        for ts, status in rows:
            if ts < day_start:
                prev_status = status
                continue
            effective_ts = max(ts, day_start)
            if prev_status == 'ok':
                up += effective_ts - prev_ts
            prev_ts = effective_ts
            prev_status = status
        if prev_status == 'ok':
            up += day_end - prev_ts
        span = day_end - day_start
        pct = round(min(100, up / span * 100), 1) if span > 0 else None
        bars.append(pct)
    return bars


def get_service_uptime_pct(service, days=7):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    since = time.time() - days * 86400
    c.execute('SELECT ts, status FROM service_events WHERE service=? AND ts>=? ORDER BY ts ASC',
              (service, since))
    rows = c.fetchall()
    conn.close()
    if not rows:
        return None
    total = time.time() - since
    up_time = 0.0
    prev_ts = since
    prev_status = 'unknown'
    for ts, status in rows:
        if prev_status == 'ok':
            up_time += ts - prev_ts
        prev_ts = ts
        prev_status = status
    if prev_status == 'ok':
        up_time += time.time() - prev_ts
    return round((up_time / total) * 100, 1) if total > 0 else None

def get_uptime_history(service, days=7):
    """Return daily uptime % for sparkline."""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    since = time.time() - days * 86400
    c.execute('SELECT ts, status FROM service_events WHERE service=? AND ts>=? ORDER BY ts ASC',
              (service, since))
    rows = c.fetchall()
    conn.close()
    return rows

def get_metrics_history(minutes=60):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    since = time.time() - minutes * 60
    c.execute('SELECT ts, aircraft, msg_rate FROM metrics WHERE ts>=? ORDER BY ts ASC', (since,))
    rows = c.fetchall()
    conn.close()
    return [{'ts': r[0], 'aircraft': r[1], 'msg_rate': r[2]} for r in rows]

def get_service_uptime_str(service):
    """Get how long a systemd service has been running."""
    try:
        r = subprocess.run(
            ['systemctl', 'show', service, '--property=ActiveEnterTimestamp'],
            capture_output=True, text=True, timeout=3)
        line = r.stdout.strip()
        if '=' in line:
            ts_str = line.split('=', 1)[1].strip()
            if ts_str and ts_str != 'n/a':
                # Parse "Mon 2026-06-21 15:00:00 EDT"
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
                        else:
                            return f'{m}m'
                    except Exception:
                        continue
    except Exception:
        pass
    return None

def get_docker_uptime_str(container):
    try:
        r = subprocess.run(
            ['docker', 'inspect', '--format', '{{.State.StartedAt}}', container],
            capture_output=True, text=True, timeout=5)
        ts_str = r.stdout.strip()
        if ts_str:
            from datetime import datetime
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
def get_airspy_stats():
    try:
        with open(AIRSPY_STATS) as f:
            return json.load(f)
    except Exception:
        return {}

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

    if max_rssi >= 67:
        return {'action': 'decrease', 'reason': f'Strong signals near saturation (max RSSI {max_rssi:.0f} dB) — try gain {max(0, int(gain)-2)}'}
    elif median_snr < 8:
        return {'action': 'increase', 'reason': f'Low median SNR ({median_snr:.1f} dB) — try gain {min(21, int(gain)+2)}'}
    elif median_noise > 45:
        return {'action': 'decrease', 'reason': f'High noise floor ({median_noise:.0f} dB) — try gain {max(0, int(gain)-2)}'}
    else:
        return {'action': 'ok', 'reason': f'Signal levels look good (SNR {median_snr:.1f} dB, noise {median_noise:.0f} dB)'}

# ── System metrics ─────────────────────────────────────────────────────────
def get_system_metrics():
    metrics = {}
    try:
        metrics['cpu_pct']  = psutil.cpu_percent(interval=0.5)
        metrics['mem_pct']  = psutil.virtual_memory().percent
        metrics['mem_used'] = round(psutil.virtual_memory().used / 1024 / 1024)
        metrics['mem_total']= round(psutil.virtual_memory().total / 1024 / 1024)
        temp = psutil.sensors_temperatures()
        if 'cpu_thermal' in temp:
            metrics['cpu_temp'] = round(temp['cpu_thermal'][0].current, 1)
        elif 'coretemp' in temp:
            metrics['cpu_temp'] = round(temp['coretemp'][0].current, 1)
        du = shutil.disk_usage('/')
        metrics['disk_pct']  = round(du.used / du.total * 100, 1)
        metrics['disk_used'] = round(du.used / 1024 / 1024 / 1024, 1)
        metrics['disk_total']= round(du.total / 1024 / 1024 / 1024, 1)
    except Exception as e:
        metrics['error'] = str(e)
    return metrics

# ── readsb deep stats ──────────────────────────────────────────────────────
def get_readsb_deep_stats():
    try:
        with open(os.path.join(READSB_JSON, 'stats.json')) as f:
            stats = json.load(f)
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

# ── Version checking ───────────────────────────────────────────────────────
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
            try:
                v = open(path).read().strip()
                if v: return v
            except Exception:
                continue
        return None
    if 'installed_file' in src:
        try:
            v = open(src['installed_file']).read().strip()
            return v if v else None
        except Exception:
            return None
    if 'installed_cmd' in src:
        try:
            r = subprocess.run(src['installed_cmd'], capture_output=True, text=True, timeout=4)
            text = (r.stdout + r.stderr).strip()
            if 'installed_re' in src:
                m = re.search(src['installed_re'], text, re.MULTILINE)
                if m: return m.group(1).strip()
            return text.split('\n')[0][:40] if text else None
        except Exception:
            return None
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
MINI_IDS = {'60a1'}
R2_IDS   = {'60a8', '0002'}

def detect_airspy_model():
    try:
        r = subprocess.run(['lsusb'], capture_output=True, text=True, timeout=5)
        for line in r.stdout.splitlines():
            if '1d50' not in line.lower() and 'airspy' not in line.lower():
                continue
            m = re.search(r'1d50:([0-9a-f]{4})', line.lower())
            if m:
                pid = m.group(1)
                if pid in MINI_IDS: return 'mini'
                elif pid in R2_IDS: return 'r2'
            if 'mini' in line.lower(): return 'mini'
    except Exception:
        pass
    try:
        r = subprocess.run(['airspy_info'], capture_output=True, text=True, timeout=5)
        text = (r.stdout + r.stderr).lower()
        if 'mini' in text: return 'mini'
        elif 'r2' in text or 'r820' in text: return 'r2'
    except Exception:
        pass
    return 'unknown'

def recommended_sample_rate(model):
    return '6' if model == 'mini' else '12'

def airspy_live_hint():
    try:
        settings = parse_airspy_options(open(AIRSPY_DEFAULT).read())
        model = detect_airspy_model()
        rate  = settings.get('sample_rate', '?')
        gain  = settings.get('gain', '?')
        label = 'Mini' if model == 'mini' else 'R2' if model == 'r2' else ''
        return f'{rate} MSPS{" ("+label+")" if label else ""} · gain {gain} · Beast → :30004'
    except Exception:
        return '6 MSPS · gain 21 · Beast → :30004'

# ── Feeder configs ─────────────────────────────────────────────────────────
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
    try:
        for line in open(path).readlines():
            line = line.strip()
            if not line or line.startswith('#'): continue
            if '=' in line:
                k, _, v = line.partition('=')
                vals[k.strip()] = v.strip().strip('"').strip("'")
    except Exception:
        pass
    return vals

def write_flat_ini(path, data):
    with open(path, 'w') as f:
        for k, v in data.items():
            f.write(f'{k}={v}\n')

def read_shell_vars(path):
    vals = {}
    try:
        for line in open(path).readlines():
            line = line.strip().lstrip('export').strip()
            if not line or line.startswith('#'): continue
            if '=' in line:
                k, _, v = line.partition('=')
                vals[k.strip()] = v.strip().strip('"').strip("'")
    except Exception:
        pass
    return vals

def write_shell_vars(path, data):
    try: existing = open(path).read()
    except: existing = ''
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
    with open(path, 'w') as f:
        f.write('\n'.join(out) + '\n')

def read_piaware_config(fields):
    vals = {}
    try:
        r = subprocess.run(['piaware-config', '--show'], capture_output=True, text=True, timeout=5)
        for line in r.stdout.splitlines():
            if ' ' in line:
                k, _, v = line.partition(' ')
                vals[k.strip()] = v.strip().split()[0]
    except Exception:
        pass
    return vals

def read_docker_env(container):
    vals = {}
    try:
        r = subprocess.run(
            ['docker', 'inspect', '--format', '{{range .Config.Env}}{{println .}}{{end}}', container],
            capture_output=True, text=True, timeout=5)
        for line in r.stdout.splitlines():
            if '=' in line:
                k, _, v = line.partition('=')
                vals[k.strip()] = v.strip()
    except Exception:
        pass
    return vals

def get_feeder_settings(key):
    cfg = FEEDER_CONFIGS.get(key)
    if not cfg: return {}
    vals = {}
    if 'docker_container' in cfg:
        vals = read_docker_env(cfg['docker_container'])
    elif 'config_file' in cfg:
        fmt = cfg.get('format', 'ini_flat')
        vals = read_flat_ini(cfg['config_file']) if fmt == 'ini_flat' else read_shell_vars(cfg['config_file'])
    elif 'config_cmd_read' in cfg:
        vals = read_piaware_config(cfg.get('fields', []))
    for fname, fpath in cfg.get('extra_files', {}).items():
        try: vals[fname] = open(fpath).read().strip()
        except: vals[fname] = ''
    return {field['key']: vals.get(field['key'], field.get('default', '')) for field in cfg.get('fields', [])}

def set_feeder_settings(key, data):
    cfg = FEEDER_CONFIGS.get(key)
    if not cfg: return False, 'Unknown feeder'
    try:
        if 'docker_container' in cfg:
            container = cfg['docker_container']
            current = read_docker_env(container)
            current.update({k: v for k, v in data.items() if v != ''})
            r = subprocess.run(['docker', 'inspect', container], capture_output=True, text=True, timeout=5)
            info = json.loads(r.stdout)[0]
            image = info['Config']['Image']
            extra_hosts = info['HostConfig'].get('ExtraHosts') or []
            env_args  = [arg for k, v in current.items() for arg in ['-e', f'{k}={v}']]
            host_args = [arg for h in extra_hosts for arg in ['--add-host', h]]
            subprocess.run(['docker', 'stop', container], timeout=10)
            subprocess.run(['docker', 'rm',   container], timeout=10)
            subprocess.run(['docker', 'run', '-d', '--name', container, '--restart', 'unless-stopped']
                           + env_args + host_args + [image], check=True, timeout=15)
            return True, 'Container recreated'
        elif 'config_file' in cfg:
            fmt = cfg.get('format', 'ini_flat')
            writable = {f['key'] for f in cfg.get('fields', []) if not f.get('readonly')}
            write_data = {k: v for k, v in data.items() if k in writable}
            if fmt == 'ini_flat':
                existing = read_flat_ini(cfg['config_file'])
                existing.update(write_data)
                write_flat_ini(cfg['config_file'], existing)
            else:
                write_shell_vars(cfg['config_file'], write_data)
            return True, 'Saved'
        elif cfg.get('write_via_cmd'):
            for k, v in data.items():
                if v: subprocess.run(['piaware-config', k, v], timeout=5)
            return True, 'Saved'
        return False, 'No write method defined'
    except Exception as e:
        return False, str(e)

# ── Config ─────────────────────────────────────────────────────────────────
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
    with open(CONFIG_FILE, 'w') as fh:
        fh.write('# ADS-B Stack Monitor - Feeder Configuration\n')
        fh.write('# Section types: [service:<unit>] or [docker:<container>]\n\n')
        cfg.write(fh)

# ── Status ─────────────────────────────────────────────────────────────────
def systemd_status(service):
    r = HOST.run(['systemctl', 'is-active', service], timeout=3)
    state = r.out.strip()
    return ('ok' if state == 'active' else 'error'), state

def docker_status(container):
    r = HOST.run(['docker', 'inspect', '--format', '{{.State.Status}}', container], timeout=5)
    state = r.out.strip()
    if not state: return 'error', 'not found'
    return ('ok' if state == 'running' else 'error'), state

def service_action(service, action):
    r = HOST.run(['systemctl', action, service], timeout=10)
    return r.ok, r.out + r.err

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
    try: existing = open(AIRSPY_DEFAULT).read()
    except: existing = ''
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
    with open(AIRSPY_DEFAULT, 'w') as f:
        f.write('\n'.join(out) + '\n')

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

# ── Log streaming ──────────────────────────────────────────────────────────
def stream_systemd_logs(service):
    cmd = ['journalctl', '-u', service, '-f', '-n', str(LOG_LINES), '--no-pager', '--output=short-iso']
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        for line in proc.stdout:
            yield f"data: {json.dumps(line.rstrip())}\n\n"
    except GeneratorExit: proc.kill()
    except Exception as e: yield f"data: {json.dumps(f'[error] {e}')}\n\n"
    finally:
        try: proc.kill()
        except: pass

def stream_docker_logs(container):
    cmd = ['docker', 'logs', '-f', '--tail', str(LOG_LINES), '--timestamps', container]
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        for line in proc.stdout:
            yield f"data: {json.dumps(line.rstrip())}\n\n"
    except GeneratorExit: proc.kill()
    except Exception as e: yield f"data: {json.dumps(f'[error] {e}')}\n\n"
    finally:
        try: proc.kill()
        except: pass

# ── Background poller ──────────────────────────────────────────────────────
def background_poll():
    """Record service states and metrics to SQLite every 30s."""
    consecutive_errors = 0
    while True:
        try:
            feeders = load_config()
            for f in feeders:
                try:
                    status, _ = (systemd_status(f['key']) if f['kind'] == 'service'
                                 else docker_status(f['key']))
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
@app.route('/api/alerts')
def api_alerts():
    """Return any services currently down."""
    feeders = load_config()
    alerts = []
    for f in feeders:
        status, detail = (systemd_status(f['key']) if f['kind'] == 'service'
                         else docker_status(f['key']))
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
        status, detail = (systemd_status(f['key']) if f['kind'] == 'service' else docker_status(f['key']))
        ver    = versions.get(f['key'], {})
        hint   = f.get('hint', '')
        uptime_str = None
        last_seen  = None

        if f['key'] == 'airspy_adsb':
            hint = airspy_live_hint()
        if f['kind'] == 'service':
            uptime_str = get_service_uptime_str(f['key'])
        elif f['kind'] == 'docker':
            uptime_str = get_docker_uptime_str(f['key'])

        # Feeder last-seen
        if f['key'] in FEEDER_STATUS_FILES:
            last_seen = get_feeder_last_seen(f['key'])
        elif f['key'] == 'fr24feed':
            last_seen = get_fr24_last_seen()
        elif f['key'] == 'piaware':
            last_seen = get_piaware_last_seen()

        uptime_pct  = get_service_uptime_pct(f['key'], days=7)
        uptime_bars = get_uptime_bars(f['key'], days=7)

        results.append({
            **f, 'status': status, 'detail': detail, 'hint': hint,
            'version': ver.get('installed'), 'latest': ver.get('latest'),
            'outdated': ver.get('outdated', False),
            'uptime_str':  uptime_str,
            'uptime_pct':  uptime_pct,
            'uptime_bars': uptime_bars,
            'last_seen':   last_seen,
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

@app.route('/api/stats/aircraft_types')
def api_aircraft_types():
    """Return aircraft type breakdown from readsb stats."""
    try:
        with open(os.path.join(READSB_JSON, 'stats.json')) as f:
            stats = json.load(f)
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
    except Exception as e:
        return jsonify({'error': str(e)})


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
    feeders = load_config()
    result = {}
    days = 7
    now = time.time()
    for f in feeders:
        key = f['key']
        daily = []
        for day_offset in range(days - 1, -1, -1):
            day_start = now - (day_offset + 1) * 86400
            day_end   = now - day_offset * 86400
            conn = sqlite3.connect(DB_FILE)
            c = conn.cursor()
            # Get all events in window plus last event before window
            c.execute('SELECT ts, status FROM service_events WHERE service=? AND ts >= ? AND ts <= ? ORDER BY ts ASC',
                      (key, day_start - 86400, day_end))
            rows = c.fetchall()
            conn.close()
            # Calculate uptime for this day
            up = 0.0
            prev_ts = day_start
            prev_status = 'unknown'
            for ts, status in rows:
                if ts < day_start:
                    prev_status = status
                    continue
                effective_ts = max(ts, day_start)
                if prev_status == 'ok':
                    up += effective_ts - prev_ts
                prev_ts = effective_ts
                prev_status = status
            if prev_status == 'ok':
                up += day_end - prev_ts
            span = day_end - day_start
            pct = round(min(100, up / span * 100), 1) if span > 0 else None
            from datetime import datetime
            day_label = datetime.fromtimestamp(day_start + 43200).strftime('%m/%d')
            daily.append({'day': day_label, 'pct': pct})
        result[key] = daily
    return jsonify(result)


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
        settings = parse_airspy_options(open(AIRSPY_DEFAULT).read())
        model    = detect_airspy_model()
        settings['model'] = model
        settings['recommended_sample_rate'] = recommended_sample_rate(model)
        return jsonify({'ok': True, 'settings': settings})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)})

@app.route('/api/settings/airspy', methods=['POST'])
@admin_required
def set_airspy():
    try:
        write_airspy_options(request.get_json())
        ok, out = service_action('airspy_adsb', 'restart')
        return jsonify({'ok': ok, 'output': out})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)})

@app.route('/api/settings/receiver', methods=['GET'])
@admin_required
def get_receiver():
    try:
        return jsonify({'ok': True, 'settings': parse_receiver_options(open(READSB_DEFAULT).read())})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)})

@app.route('/api/settings/receiver', methods=['POST'])
@admin_required
def set_receiver():
    try:
        text = open(READSB_DEFAULT).read()
        with open(READSB_DEFAULT, 'w') as f:
            f.write(write_receiver_options(text, request.get_json()))
        ok, out = service_action('readsb', 'restart')
        return jsonify({'ok': ok, 'output': out})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)})

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
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)})

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
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)})

@app.route('/api/service/<name>/restart', methods=['POST'])
@admin_required
def restart_service(name):
    allowed = {f['key'] for f in load_config()}
    if name not in allowed:
        return jsonify({'ok': False, 'error': 'unknown service'})
    ok, out = service_action(name, 'restart')
    return jsonify({'ok': ok, 'output': out})

@app.route('/api/logs/<key>')
def api_logs(key):
    cfg = get_config_map()
    if key not in cfg:
        return Response('data: {"error": "unknown service"}\n\n', status=404, mimetype='text/event-stream')
    entry = cfg[key]
    gen = stream_docker_logs(key) if entry['kind'] == 'docker' else stream_systemd_logs(key)
    return Response(stream_with_context(gen), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})

@app.route('/api/backup')
@admin_required
def api_backup():
    """Download a zip of all config files."""
    import zipfile, io
    buf = io.BytesIO()
    files = {
        'feeders.ini':       CONFIG_FILE,
        'airspy_adsb':       AIRSPY_DEFAULT,
        'readsb':            READSB_DEFAULT,
        'fr24feed.ini':      '/etc/fr24feed.ini',
        'piaware.conf':      '/etc/piaware.conf',
        'adsb-monitor.service': '/etc/systemd/system/adsb-monitor.service',
    }
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        for name, path in files.items():
            try:
                zf.write(path, name)
            except Exception:
                pass
    buf.seek(0)
    from flask import send_file
    from datetime import datetime
    fname = f'adsb-config-{datetime.now().strftime("%Y%m%d-%H%M%S")}.zip'
    return send_file(buf, mimetype='application/zip',
                     as_attachment=True, download_name=fname)


@app.route('/api/debug-port')
def debug_port():
    return jsonify({
        'tagged_port': getattr(_request_port, 'port', 'not set'),
        'is_readonly': is_readonly(),
        'readonly_port': READONLY_PORT,
        'admin_port': ADMIN_PORT,
    })

@app.route('/')
def index():
    return send_from_directory('static', 'index.html')

# ── Server ─────────────────────────────────────────────────────────────────
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
