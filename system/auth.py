"""Port tagging / read-only enforcement.

The same app serves two ports: the admin port (full control) and the
read-only port (status/stats only, safe to expose publicly behind a proxy).
A per-request thread-local (`app._request_port`) records which port the
request arrived on; `is_readonly` reads it and the `@admin_required`
decorator rejects writes that come in on the read-only port with a 403.

`csrf_guard` runs before every request and rejects cross-site writes: any
POST/PUT/PATCH/DELETE must carry `X-Requested-With: adsb-monitor` (an HTML form
can't set a custom header, and a cross-origin fetch that sets one needs a CORS
preflight this app never grants), and is refused outright when the browser's
`Sec-Fetch-Site` says it came from another site. The dashboard's own fetch calls
add the header (static/index.html).

`login_guard` runs after it and, on the admin port only, requires a logged-in
session for everything except the login page and its API (and the vendored
icon font). Until a password is set (`run.py set-password`), nobody can log in
and the admin port stays locked; the read-only port is never affected.
`AdminSessionInterface` signs the session cookie with the key stored next to
the password hash (system/admin_login.py), so no SECRET_KEY config is needed.
"""
from __future__ import annotations

from functools import wraps
from typing import Any, Callable

from flask import Flask, jsonify, redirect, request, session
from flask.sessions import SecureCookieSessionInterface
from itsdangerous import URLSafeTimedSerializer

import app
from system import admin_login


def is_readonly() -> bool:
    return getattr(app._request_port, 'port', app.ADMIN_PORT) == app.READONLY_PORT

def admin_required(f: Callable[..., Any]) -> Callable[..., Any]:
    @wraps(f)
    def decorated(*args: Any, **kwargs: Any) -> Any:
        if is_readonly():
            return jsonify({'ok': False, 'error': 'Settings not available on this port'}), 403
        return f(*args, **kwargs)
    return decorated


CSRF_HEADER = 'X-Requested-With'
CSRF_VALUE = 'adsb-monitor'
_SAFE_METHODS = frozenset({'GET', 'HEAD', 'OPTIONS'})
_OK_FETCH_SITES = frozenset({'same-origin', 'none'})

def csrf_guard() -> Any:
    """before_request hook: return a 403 for cross-site or header-less writes, else None."""
    if request.method in _SAFE_METHODS:
        return None
    site = request.headers.get('Sec-Fetch-Site')
    if site is not None and site not in _OK_FETCH_SITES:
        return jsonify({'ok': False, 'error': 'Cross-site request refused'}), 403
    if request.headers.get(CSRF_HEADER) != CSRF_VALUE:
        return jsonify({'ok': False, 'error': f'Missing {CSRF_HEADER} header'}), 403
    return None


LOGIN_OPEN_PATHS = frozenset({'/login', '/api/login', '/api/login/status'})
LOGIN_OPEN_PREFIXES = ('/static/vendor/',)

def login_guard() -> Any:
    """before_request hook: on the admin port, require a logged-in session."""
    if is_readonly():
        return None
    path = request.path
    if path in LOGIN_OPEN_PATHS or path.startswith(LOGIN_OPEN_PREFIXES):
        return None
    if admin_login.password_is_set() and session.get('admin') is True:
        return None
    if path.startswith('/api/'):
        return jsonify({'ok': False, 'error': 'Login required', 'login': '/login'}), 401
    return redirect('/login')


class AdminSessionInterface(SecureCookieSessionInterface):
    """Signs the session cookie with admin_login.secret_key(); no key, no session."""
    def get_signing_serializer(self, app: Flask) -> URLSafeTimedSerializer | None:  # noqa: ARG002
        key = admin_login.secret_key()
        if not key:
            return None
        return URLSafeTimedSerializer(
            key,
            salt=self.salt,
            serializer=self.serializer,
            signer_kwargs={'key_derivation': self.key_derivation,
                           'digest_method': self.digest_method},
        )
