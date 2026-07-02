"""Version refresh route."""
import threading
from typing import Any

from flask import Blueprint, jsonify

from system.auth import admin_required
from system.versions import get_versions, invalidate_cache, refresh_versions

bp = Blueprint('versions', __name__)


@bp.route('/api/versions/refresh', methods=['POST'])
@admin_required
def api_versions_refresh() -> Any:
    invalidate_cache()
    t = threading.Thread(target=refresh_versions, daemon=True)
    t.start()
    t.join(timeout=15)
    return jsonify({'ok': True, 'versions': get_versions()})
