"""Service restart route."""
from flask import Blueprint, jsonify

from system.auth import admin_required
from system.config_io import load_config
from system.services import service_action

bp = Blueprint('services', __name__)


@bp.route('/api/service/<name>/restart', methods=['POST'])
@admin_required
def restart_service(name):
    allowed = {f['key'] for f in load_config()}
    if name not in allowed:
        return jsonify({'ok': False, 'error': 'unknown service'})
    ok, out = service_action(name, 'restart')
    return jsonify({'ok': ok, 'output': out})
