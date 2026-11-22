"""Dashboard routes: alerts (service down + no data), port mode, UI config, feeder status."""
from typing import Any

from flask import Blueprint, jsonify

import app
from system import dataflow
from system.auth import is_readonly
from system.config_io import load_config
from system.db import get_dataflow_events, get_service_uptime_pct, get_uptime_bars
from system.feeders import feeder_status, probe, readsb_metrics
from system.sdr_settings import airspy_live_hint
from system.versions import VERSION_SOURCES, _get_installed_version, get_versions

bp = Blueprint('dashboard', __name__)


@bp.route('/api/alerts')
def api_alerts() -> Any:
    """Services currently down, and running feeders that have sent no data for
    dataflow.STALE_AFTER_S (`data_alerts`)."""
    feeders = load_config()
    alerts, data_alerts = [], []
    for f in feeders:
        status, detail = feeder_status(f)
        if status != 'ok':
            alerts.append({'key': f['key'], 'label': f['label'], 'detail': detail})
            continue
        flow = dataflow.current(f)
        if flow['state'] == 'stale':
            data_alerts.append({'key': f['key'], 'label': f['label'], 'detail': flow['detail'],
                                'since': flow['since']})
    return jsonify({'alerts': alerts, 'any_down': len(alerts) > 0,
                    'data_alerts': data_alerts, 'any_alert': bool(alerts or data_alerts)})


@bp.route('/api/dataflow/events')
def api_dataflow_events() -> Any:
    """Recent data-flow changes (a feeder going stale or recovering), newest first."""
    return jsonify({'events': get_dataflow_events(limit=50)})


@bp.route('/api/mode')
def api_mode() -> Any:
    return jsonify({'readonly': is_readonly(), 'version': app.APP_VERSION})

@bp.route('/api/ui-config')
def api_ui_config() -> Any:
    # The dashboard appends this to the map iframe URL (?v=...), so a tar1090
    # update changes the URL and the browser fetches the new index.html instead
    # of a cached one whose hashed script/style files no longer exist.
    tar1090_version = _get_installed_version('tar1090', VERSION_SOURCES['tar1090'])
    if is_readonly():
        base = app.GRAPHS1090_URL_REMOTE
        return jsonify({
            'tar1090_url':       app.TAR1090_URL_REMOTE,
            'tar1090_version':   tar1090_version,
            'graphs1090_url':    base,
            'graphs1090_airspy': f'{base}/?graph=airspy_snr&timeframe=8h',
        })
    base = app.GRAPHS1090_URL_LOCAL
    return jsonify({
        'tar1090_url':       app.TAR1090_URL_LOCAL,
        'tar1090_version':   tar1090_version,
        'graphs1090_url':    base,
        'graphs1090_airspy': f'{base}/?graph=airspy_snr&timeframe=8h',
    })

@bp.route('/api/status')
def api_status() -> Any:
    feeders  = load_config()
    versions = get_versions()
    results: list[dict[str, Any]] = []
    for f in feeders:
        health = probe(f)
        ver    = versions.get(f['key'], {})
        hint   = airspy_live_hint() if f['key'] == 'airspy_adsb' else f.get('hint', '')

        uptime_pct  = get_service_uptime_pct(f['key'], days=7)
        uptime_bars = get_uptime_bars(f['key'], days=7)

        entry = {
            **f, 'status': health.status, 'detail': health.detail, 'hint': hint,
            'uptime_str':  health.running_for,
            'uptime_pct':  uptime_pct,
            'uptime_bars': uptime_bars,
            'last_seen':   health.last_seen,
            'data':        health.flow,
        }
        if f['key'] in VERSION_SOURCES:  # no source (e.g. dump978-fa) → no pill, not a stuck "checking…"
            entry['version']  = ver.get('installed')
            entry['latest']   = ver.get('latest')
            entry['outdated'] = ver.get('outdated', False)
        results.append(entry)
    return jsonify({'feeders': results, 'metrics': readsb_metrics()})
