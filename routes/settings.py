"""Settings routes: airspy, receiver, per-band SDR, feeder list, feeder config."""
import logging

from flask import Blueprint, jsonify, request

import app
from system.auth import admin_required
from system.config_io import (
    FEEDER_CONFIGS, get_feeder_settings, load_config, save_feeders, set_feeder_settings,
)
from system.sdr import (
    detect_airspy_model, parse_airspy_options, parse_receiver_options, parse_sdr1090,
    parse_sdr978, write_airspy_options, write_receiver_options, write_sdr1090, write_sdr978,
)
from system.services import service_action

logger = logging.getLogger(__name__)

bp = Blueprint('settings', __name__)


@bp.route('/api/settings/airspy', methods=['GET'])
@admin_required
def get_airspy():
    try:
        settings = parse_airspy_options(app.HOST.read_text(app.AIRSPY_DEFAULT) or '')
        model    = detect_airspy_model()
        settings['model'] = model
        settings['recommended_sample_rate'] = '6' if model == 'mini' else '12'
        return jsonify({'ok': True, 'settings': settings})
    except Exception:
        logger.exception("Failed to read Airspy settings")
        return jsonify({'ok': False, 'error': 'An internal error has occurred.'})

@bp.route('/api/settings/airspy', methods=['POST'])
@admin_required
def set_airspy():
    try:
        write_airspy_options(request.get_json())
        ok, out = service_action('airspy_adsb', 'restart')
        return jsonify({'ok': ok, 'output': out})
    except Exception:
        logger.exception("Failed to update Airspy settings")
        return jsonify({'ok': False, 'error': 'An internal error has occurred.'})

@bp.route('/api/settings/receiver', methods=['GET'])
@admin_required
def get_receiver():
    try:
        return jsonify({'ok': True, 'settings': parse_receiver_options(app.HOST.read_text(app.READSB_DEFAULT) or '')})
    except Exception:
        logger.exception("Failed to read receiver settings")
        return jsonify({'ok': False, 'error': 'An internal error has occurred.'})

@bp.route('/api/settings/receiver', methods=['POST'])
@admin_required
def set_receiver():
    try:
        text = app.HOST.read_text(app.READSB_DEFAULT) or ''
        app.HOST.write_text(app.READSB_DEFAULT, write_receiver_options(text, request.get_json()))
        ok, out = service_action('readsb', 'restart')
        return jsonify({'ok': ok, 'output': out})
    except Exception:
        logger.exception("Failed to update receiver settings")
        return jsonify({'ok': False, 'error': 'An internal error has occurred.'})

@bp.route('/api/settings/sdr/<band>', methods=['GET'])
@admin_required
def get_sdr(band):
    try:
        if band == '1090': return jsonify({'ok': True, 'settings': parse_sdr1090()})
        if band == '978':  return jsonify({'ok': True, 'settings': parse_sdr978()})
        return jsonify({'ok': False, 'error': 'unknown band'}), 404
    except Exception:
        logger.exception("Failed to read SDR settings")
        return jsonify({'ok': False, 'error': 'An internal error has occurred.'})

@bp.route('/api/settings/sdr/<band>', methods=['POST'])
@admin_required
def set_sdr(band):
    try:
        d = request.get_json() or {}
        gain = str(d.get('gain', 'auto')).strip()
        biastee = bool(d.get('biastee', False))
        if band == '1090':
            write_sdr1090(gain, biastee)
            ok, out = service_action('readsb', 'restart')
        elif band == '978':
            write_sdr978(gain, biastee)
            ok, out = service_action('dump978-fa', 'restart')
        else:
            return jsonify({'ok': False, 'error': 'unknown band'}), 404
        return jsonify({'ok': ok, 'output': out})
    except Exception:
        logger.exception("Failed to update SDR settings")
        return jsonify({'ok': False, 'error': 'An internal error has occurred.'})

@bp.route('/api/settings/feeders', methods=['GET'])
@admin_required
def get_feeders():
    return jsonify({'ok': True, 'feeders': load_config()})

@bp.route('/api/settings/feeders', methods=['POST'])
@admin_required
def set_feeders():
    try:
        save_feeders(request.get_json().get('feeders', []))
        return jsonify({'ok': True})
    except Exception:
        app.app.logger.exception("Failed to save feeders")
        return jsonify({'ok': False, 'error': 'An internal error occurred'})

@bp.route('/api/settings/feeder/<key>', methods=['GET'])
@admin_required
def get_feeder_cfg(key):
    cfg = FEEDER_CONFIGS.get(key)
    if not cfg:
        return jsonify({'ok': False, 'error': 'No config defined for this feeder'})
    return jsonify({'ok': True, 'fields': cfg['fields'], 'settings': get_feeder_settings(key), 'label': cfg['label']})

@bp.route('/api/settings/feeder/<key>', methods=['POST'])
@admin_required
def set_feeder_cfg(key):
    try:
        ok, msg = set_feeder_settings(key, request.get_json())
        if ok:
            fmap = {f['key']: f for f in load_config()}
            if key in fmap and fmap[key]['kind'] == 'service':
                service_action(key, 'restart')
        return jsonify({'ok': ok, 'message': msg})
    except Exception:
        app.app.logger.exception("Failed to update feeder config for key=%s", key)
        return jsonify({'ok': False, 'error': 'An internal error occurred'})
