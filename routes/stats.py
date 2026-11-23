"""Stats routes: airspy/readsb/band/aircraft-type/system stats, metrics history, uptime."""
import os
from typing import Any

from flask import Blueprint, jsonify, request

import app
from system.config_io import load_config
from system.db import _daily_uptime, get_changes, get_metrics_history, get_service_uptime_pct, get_timeline
from system.stats import (
    gain_recommendation, get_airspy_stats, get_band_stats, get_readsb_deep_stats,
    get_system_metrics,
)

bp = Blueprint('stats', __name__)


@bp.route('/api/stats/airspy')
def api_airspy_stats() -> Any:
    stats = get_airspy_stats()
    return jsonify({
        'stats': stats,
        'recommendation': gain_recommendation(stats),
    })

@bp.route('/api/stats/readsb')
def api_readsb_stats() -> Any:
    return jsonify(get_readsb_deep_stats())

@bp.route('/api/stats/bands')
def api_band_stats() -> Any:
    return jsonify(get_band_stats())

@bp.route('/api/stats/aircraft_types')
def api_aircraft_types() -> Any:
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
def api_system_stats() -> Any:
    return jsonify(get_system_metrics())

@bp.route('/api/stats/history')
def api_history() -> Any:
    minutes = int(request.args.get('minutes', 60))
    return jsonify(get_metrics_history(minutes))

@bp.route('/api/stats/uptime/history')
def api_uptime_history() -> Any:
    """Return 7-day daily uptime % for all services."""
    return jsonify({f['key']: _daily_uptime(f['key']) for f in load_config()})


@bp.route('/api/stats/uptime/<service>')
def api_service_uptime(service: str) -> Any:
    days = int(request.args.get('days', 7))
    pct  = get_service_uptime_pct(service, days)
    return jsonify({'service': service, 'uptime_pct': pct, 'days': days})


def _bucket_s(hours: float) -> int:
    """~150-300 points whatever the window: 1 min up to 6 h, 2 min up to 12 h, else 5 min."""
    return 60 if hours <= 6 else 120 if hours <= 12 else 300


@bp.route('/api/timeline')
def api_timeline() -> Any:
    """The Timeline view: bucketed metrics, everything that changed, and which
    tar1090 views have data behind them. Read-only, so on both ports."""
    try:
        hours = float(request.args.get('hours', 24))
    except ValueError:
        hours = 24.0
    hours = min(max(hours, 1.0), 25.0)          # metrics are kept for 25 h
    readsb = app.HOST.read_text(app.READSB_DEFAULT) or ''
    outline = app.HOST.read_json(os.path.join(app.READSB_JSON, 'outline.json'))
    return jsonify({
        'ok': True, 'hours': hours, 'bucket_s': _bucket_s(hours),
        'metrics': get_timeline(hours, _bucket_s(hours)),
        'changes': get_changes(hours),
        'links': {'outline': isinstance(outline, dict), 'heatmap': '--heatmap' in readsb,
                  'ptracks': True},
    })

