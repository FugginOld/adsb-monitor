"""Stack update routes (admin port only): start an update, poll status, follow its log."""
from __future__ import annotations

import logging
from typing import Any

from flask import Blueprint, Response, jsonify, stream_with_context

from system import stack_update
from system.auth import admin_required
from system.logstream import _journalctl, stream_logs
from system.versions import get_versions

logger = logging.getLogger(__name__)

bp = Blueprint('stack', __name__)


def _stored_key(component: str) -> str | None:
    """The COMPONENTS key equal to the URL value, or None. Callers act on the
    returned key — configured data — never on the request value itself, so the
    allowlist match is also what keeps request input out of the systemctl /
    systemd-run / journalctl command lines (same pattern as routes/services.py)."""
    return next((k for k in stack_update.COMPONENTS if k == component), None)


@bp.route('/api/stack/updates', methods=['GET'])
@admin_required
def api_stack_updates() -> Any:
    try:
        return jsonify({'ok': True, 'supported': stack_update.supported(),
                        'components': stack_update.status(), 'versions': get_versions()})
    except Exception:
        logger.exception("Failed to read stack update status")
        return jsonify({'ok': False, 'error': 'An internal error has occurred.'})

@bp.route('/api/stack/update/<component>', methods=['POST'])
@admin_required
def api_stack_update(component: str) -> Any:
    key = _stored_key(component)
    if key is None:
        return jsonify({'ok': False, 'error': 'unknown component'}), 404
    try:
        ok, msg = stack_update.start_update(key)
        return jsonify({'ok': ok, 'message' if ok else 'error': msg}), (200 if ok else 409)
    except Exception:
        logger.exception("Failed to start update of %s", key)
        return jsonify({'ok': False, 'error': 'An internal error has occurred.'})

@bp.route('/api/stack/update/<component>/logs')
@admin_required
def api_stack_update_logs(component: str) -> Response:
    key = _stored_key(component)
    if key is None:
        return Response('data: {"error": "unknown component"}\n\n', status=404, mimetype='text/event-stream')
    gen = stream_logs(_journalctl('-u', stack_update.unit_name(key)))
    return Response(stream_with_context(gen), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})
