"""Version checking.

Compares the installed version of each stack component against the latest
published upstream. VERSION_SOURCES declares, per component, how to read
the installed version locally and where to fetch the latest. Results are
cached for VERSION_TTL seconds so the dashboard isn't hammering GitHub on
every refresh.

`_version_cache` / `_version_lock` / `_version_ts` are self-contained here
(nothing outside this module reassigns them — routes that trigger a refresh
do so through this module's own name, e.g. `system.versions._version_ts`,
not a copy). `HOST` stays in app.py, reached via `import app`.
"""
import re
import threading
import time
import urllib.request

import app

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
    'dump978-fa': {
        # dump978-fa has no --version; read the installed deb version. No upstream
        # version file, so latest_url=None — show the version, skip the update check.
        'installed_cmd': ['dpkg-query', '-W', "-f=${Version}", 'dump978-fa'],
        'installed_re':  r'([\d.]+)',
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
            txt = app.HOST.read_text(path)
            if txt and txt.strip():
                return txt.strip()
        return None
    if 'installed_file' in src:
        txt = app.HOST.read_text(src['installed_file'])
        return txt.strip() if txt and txt.strip() else None
    if 'installed_cmd' in src:
        r = app.HOST.run(src['installed_cmd'], timeout=4)
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
