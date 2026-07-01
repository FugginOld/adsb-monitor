"""Dashboard routes: alerts, port mode, UI config, feeder status."""
from flask import Blueprint, jsonify

import app
from system.auth import is_readonly
from system.config_io import load_config
from system.db import get_service_uptime_pct, get_uptime_bars
from system.feeders import feeder_status, probe, readsb_metrics
from system.sdr import airspy_live_hint
from system.versions import VERSION_SOURCES, get_versions

bp = Blueprint('dashboard', __name__)


@bp.route('/api/alerts')
def api_alerts():
    """Return any services currently down."""
    feeders = load_config()
    alerts = []
    for f in feeders:
        status, detail = feeder_status(f)
        if status != 'ok':
            alerts.append({'key': f['key'], 'label': f['label'], 'detail': detail})
    return jsonify({'alerts': alerts, 'any_down': len(alerts) > 0})


@bp.route('/api/mode')
def api_mode():
    return jsonify({'readonly': is_readonly()})

@bp.route('/api/ui-config')
def api_ui_config():
    if is_readonly():
        base = app.GRAPHS1090_URL_REMOTE
        return jsonify({
            'tar1090_url':       app.TAR1090_URL_REMOTE,
            'graphs1090_url':    base,
            'graphs1090_airspy': f'{base}/?graph=airspy_snr&timeframe=8h',
        })
    base = app.GRAPHS1090_URL_LOCAL
    return jsonify({
        'tar1090_url':       app.TAR1090_URL_LOCAL,
        'graphs1090_url':    base,
        'graphs1090_airspy': f'{base}/?graph=airspy_snr&timeframe=8h',
    })

@bp.route('/api/status')
def api_status():
    feeders  = load_config()
    versions = get_versions()
    results  = []
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
        }
        if f['key'] in VERSION_SOURCES:  # no source (e.g. dump978-fa) → no pill, not a stuck "checking…"
            entry['version']  = ver.get('installed')
            entry['latest']   = ver.get('latest')
            entry['outdated'] = ver.get('outdated', False)
        results.append(entry)
    return jsonify({'feeders': results, 'metrics': readsb_metrics()})
