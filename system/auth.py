"""Port tagging / read-only enforcement.

The same app serves two ports: the admin port (full control) and the
read-only port (status/stats only, safe to expose publicly behind a proxy).
A per-request thread-local (`app._request_port`) records which port the
request arrived on; `is_readonly` reads it and the `@admin_required`
decorator rejects writes that come in on the read-only port with a 403.
"""
from __future__ import annotations

from functools import wraps
from typing import Any, Callable

from flask import jsonify

import app


def is_readonly() -> bool:
    return getattr(app._request_port, 'port', app.ADMIN_PORT) == app.READONLY_PORT

def admin_required(f: Callable[..., Any]) -> Callable[..., Any]:
    @wraps(f)
    def decorated(*args: Any, **kwargs: Any) -> Any:
        if is_readonly():
            return jsonify({'ok': False, 'error': 'Settings not available on this port'}), 403
        return f(*args, **kwargs)
    return decorated
