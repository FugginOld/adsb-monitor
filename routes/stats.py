"""Stats routes: airspy/readsb/band/aircraft-type/system stats, metrics history, uptime."""
import os

from flask import Blueprint, jsonify, request

import app
from system.config_io import load_config
from system.db import _daily_uptime, get_metrics_history, get_service_uptime_pct
from system.stats import (
    gain_recommendation, get_airspy_stats, get_band_stats, get_readsb_deep_stats,
    get_system_metrics,
)

bp = Blueprint('stats', __name__)


@bp.route('/api/stats/airspy')
def api_airspy_stats():
    stats = get_airspy_stats()
    return jsonify({
        'stats': stats,
        'recommendation': gain_recommendation(stats),
    })

@bp.route('/api/stats/readsb')
def api_readsb_stats():
    return jsonify(get_readsb_deep_stats())

@bp.route('/api/stats/bands')
def api_band_stats():
    return jsonify(get_band_stats())

@bp.route('/api/stats/aircraft_types')
def api_aircraft_types():
    """Return aircraft type breakdown from readsb stats."""
    try:
        stats = app.HOST.read_json(os.path.join(app.READSB_JSON, 'stats.json')) or {}
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
    except Exception:
        app.app.logger.exception("Failed to load aircraft type stats")
        return jsonify({'error': 'Internal server error'}), 500


@bp.route('/api/stats/system')
def api_system_stats():
    return jsonify(get_system_metrics())

@bp.route('/api/stats/history')
def api_history():
    minutes = int(request.args.get('minutes', 60))
    return jsonify(get_metrics_history(minutes))

@bp.route('/api/stats/uptime/history')
def api_uptime_history():
    """Return 7-day daily uptime % for all services."""
    return jsonify({f['key']: _daily_uptime(f['key']) for f in load_config()})


@bp.route('/api/stats/uptime/<service>')
def api_service_uptime(service):
    days = int(request.args.get('days', 7))
    pct  = get_service_uptime_pct(service, days)
    return jsonify({'service': service, 'uptime_pct': pct, 'days': days})
