"""Admin-port login: one password (stored hashed) plus the session signing key.

Both live in `app.ADMIN_AUTH_FILE` (JSON, mode 0600, next to feeders.ini):
    {"password_hash": "<werkzeug hash>", "secret_key": "<hex>"}
Set it with `sudo /opt/adsb-monitor/venv/bin/python /opt/adsb-monitor/run.py set-password`.
Every set-password writes a new secret_key, so it also logs out every session.

The file is re-read whenever its mtime changes, so a new password takes effect
without restarting the monitor. No Flask imports here; the HTTP side is in
system/auth.py (login_guard, AdminSessionInterface) and routes/login.py.
"""
from __future__ import annotations

import json
import os
import secrets
import threading
import time
from typing import Any

from werkzeug.security import check_password_hash, generate_password_hash

import app

MIN_PASSWORD_LEN = 10
FAIL_WINDOW_S = 15 * 60   # failed logins counted over this window, per client IP
FAIL_MAX = 5              # failures allowed in the window before logins are refused
FAIL_DELAY_S = 1.0        # pause after each wrong password (slows guessing)

_lock = threading.Lock()
_cache: dict[str, Any] = {'mtime': None, 'data': {}}
_failures: dict[str, list[float]] = {}


def _load() -> dict[str, Any]:
    path = app.ADMIN_AUTH_FILE
    try:
        mtime = os.stat(path).st_mtime_ns
    except OSError:
        with _lock:
            _cache.update(mtime=None, data={})
        return {}
    with _lock:
        if _cache['mtime'] != mtime:
            try:
                with open(path, encoding='utf-8') as f:
                    data = json.load(f)
            except (OSError, ValueError):
                data = {}
            _cache.update(mtime=mtime, data=data if isinstance(data, dict) else {})
        return dict(_cache['data'])


def password_is_set() -> bool:
    d = _load()
    return isinstance(d.get('password_hash'), str) and bool(d.get('password_hash')) \
        and isinstance(d.get('secret_key'), str) and bool(d.get('secret_key'))


def secret_key() -> str | None:
    """Session signing key, or None while no password is set (sessions then can't exist)."""
    d = _load()
    return d['secret_key'] if password_is_set() else None


def verify(password: str) -> bool:
    if not password_is_set():
        return False
    return check_password_hash(_load()['password_hash'], password)


def set_password(password: str) -> None:
    """Hash and store a new password with a fresh signing key (logs out all sessions)."""
    if len(password) < MIN_PASSWORD_LEN:
        raise ValueError(f'Password must be at least {MIN_PASSWORD_LEN} characters')
    data = {'password_hash': generate_password_hash(password),
            'secret_key': secrets.token_hex(32)}
    path = app.ADMIN_AUTH_FILE
    tmp = f'{path}.tmp'
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        json.dump(data, f)
        f.flush()
        os.fsync(f.fileno())
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


# ── failed-login throttle (in memory, per client IP) ─────────────────────────
def _recent(ip: str, now: float) -> list[float]:
    times = [t for t in _failures.get(ip, []) if now - t < FAIL_WINDOW_S]
    if times:
        _failures[ip] = times
    else:
        _failures.pop(ip, None)
    return times


def too_many_failures(ip: str) -> bool:
    with _lock:
        return len(_recent(ip, time.time())) >= FAIL_MAX


def record_failure(ip: str) -> None:
    with _lock:
        now = time.time()
        _failures[ip] = _recent(ip, now) + [now]


def clear_failures(ip: str) -> None:
    with _lock:
        _failures.pop(ip, None)
