"""Log streaming routes: per-feeder logs, whole-system syslog."""
from flask import Blueprint, Response, stream_with_context

from system.auth import admin_required
from system.config_io import get_config_map
from system.logstream import _journalctl, _log_command, stream_logs

bp = Blueprint('logs', __name__)


@bp.route('/api/logs/<key>')
@admin_required
def api_logs(key):
    cfg = get_config_map()
    if key not in cfg:
        return Response('data: {"error": "unknown service"}\n\n', status=404, mimetype='text/event-stream')
    entry = cfg[key]
    gen = stream_logs(_log_command(entry))
    return Response(stream_with_context(gen), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})

@bp.route('/api/syslog')
@admin_required
def api_syslog():
    """Stream the whole-system journal (all units) for the Settings log window."""
    gen = stream_logs(_journalctl())
    return Response(stream_with_context(gen), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})
