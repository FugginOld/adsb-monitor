"""Service restart route."""
from typing import Any

from flask import Blueprint, jsonify

from system.auth import admin_required
from system.config_io import load_config
from system.services import service_action

bp = Blueprint('services', __name__)


@bp.route('/api/service/<name>/restart', methods=['POST'])
@admin_required
def restart_service(name: str) -> Any:
    # Act on the stored key, not the URL value: the match is the allowlist check,
    # and what reaches service_action is configured data, never request input.
    key = next((f['key'] for f in load_config() if f['key'] == name), None)
    if key is None:
        return jsonify({'ok': False, 'error': 'unknown service'})
    ok, out = service_action(key, 'restart')
    return jsonify({'ok': ok, 'output': out})
