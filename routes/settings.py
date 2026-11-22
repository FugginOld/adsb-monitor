"""Settings routes: airspy, airspy auto-tune, receiver, per-band SDR, feeder list, feeder config."""
from __future__ import annotations

import logging
from typing import Any, cast

from flask import Blueprint, jsonify, request

import app
from system import autotune
from system.auth import admin_required
from system.config_io import (
    FEEDER_CONFIGS, get_feeder_settings, load_config, save_feeders, set_feeder_settings,
)
from system.db import get_gain_changes, record_gain_change
from system.sdr_detect import detect_airspy_model
from system.sdr_settings import (
    R2_RATES, airspy_live_stats, airspy_settings_error, parse_airspy_options, parse_receiver_options, parse_sdr1090,
    parse_sdr978, write_airspy_options, write_receiver_options, write_sdr1090, write_sdr978,
)
from system.services import service_action

logger = logging.getLogger(__name__)

bp = Blueprint('settings', __name__)


@bp.route('/api/settings/airspy', methods=['GET'])
@admin_required
def get_airspy() -> Any:
    try:
        settings = parse_airspy_options(app.HOST.read_text(app.AIRSPY_DEFAULT) or '')
        model    = detect_airspy_model()
        out: dict[str, Any] = dict(settings)
        out['model'] = model
        # -m (MLAT freq) is R2-only; a Mini runs at the decoder's fixed rate.
        out['rate_options'] = list(R2_RATES) if model == 'r2' else []
        out['live'] = airspy_live_stats()
        return jsonify({'ok': True, 'settings': out})
    except Exception:
        logger.exception("Failed to read Airspy settings")
        return jsonify({'ok': False, 'error': 'An internal error has occurred.'})

@bp.route('/api/settings/airspy', methods=['POST'])
@admin_required
def set_airspy() -> Any:
    try:
        d = request.get_json(silent=True)
        if not isinstance(d, dict):
            return jsonify({'ok': False, 'error': 'expected a JSON object'}), 400
        # Auto-tune owns the gain while it's on: a different gain is only accepted
        # with an explicit override, which also turns auto-tune off so it doesn't
        # walk the gain straight back. Saving other fields with the same gain is fine.
        current = parse_airspy_options(app.HOST.read_text(app.AIRSPY_DEFAULT) or '')['gain']
        new_gain = str(d.get('gain', 'auto')).strip().lower()
        override = autotune.load_config()['enabled'] and new_gain != current
        if override and d.get('override_autotune') is not True:
            return jsonify({'ok': False, 'error': 'autotune_enabled', 'current_gain': current,
                            'message': f'Auto-tune is on and manages the gain (currently {current}).'}), 409
        model = detect_airspy_model()
        err = airspy_settings_error(d, model)
        if err:
            return jsonify({'ok': False, 'error': err}), 400
        write_airspy_options(d, model=model)
        if override:
            autotune.save_config({'enabled': False})
            record_gain_change(current, new_gain, 'manual override — auto-tune turned off')
            logger.info('manual Airspy gain %s -> %s; auto-tune turned off', current, new_gain)
        ok, out = service_action('airspy_adsb', 'restart')
        return jsonify({'ok': ok, 'output': out})
    except Exception:
        logger.exception("Failed to update Airspy settings")
        return jsonify({'ok': False, 'error': 'An internal error has occurred.'})

# Status is safe for the read-only port (no settings or keys in it); changes are admin-only.
@bp.route('/api/autotune', methods=['GET'])
def get_autotune() -> Any:
    try:
        return jsonify({'ok': True, 'config': autotune.load_config(),
                        'state': autotune.snapshot(), 'history': get_gain_changes(10)})
    except Exception:
        logger.exception("Failed to read auto-tune state")
        return jsonify({'ok': False, 'error': 'An internal error has occurred.'})

@bp.route('/api/autotune', methods=['POST'])
@admin_required
def set_autotune() -> Any:
    try:
        d = request.get_json(silent=True)
        if not isinstance(d, dict):
            return jsonify({'ok': False, 'error': 'expected a JSON object'}), 400
        err = autotune.update_error(d)
        if err:
            return jsonify({'ok': False, 'error': err}), 400
        return jsonify({'ok': True, 'config': autotune.save_config(d)})
    except Exception:
        logger.exception("Failed to update auto-tune settings")
        return jsonify({'ok': False, 'error': 'An internal error has occurred.'})

@bp.route('/api/settings/receiver', methods=['GET'])
@admin_required
def get_receiver() -> Any:
    try:
        return jsonify({'ok': True, 'settings': parse_receiver_options(app.HOST.read_text(app.READSB_DEFAULT) or '')})
    except Exception:
        logger.exception("Failed to read receiver settings")
        return jsonify({'ok': False, 'error': 'An internal error has occurred.'})

@bp.route('/api/settings/receiver', methods=['POST'])
@admin_required
def set_receiver() -> Any:
    try:
        text = app.HOST.read_text(app.READSB_DEFAULT) or ''
        app.HOST.write_text(app.READSB_DEFAULT, write_receiver_options(text, request.get_json()), backup=True)
        ok, out = service_action('readsb', 'restart')
        return jsonify({'ok': ok, 'output': out})
    except Exception:
        logger.exception("Failed to update receiver settings")
        return jsonify({'ok': False, 'error': 'An internal error has occurred.'})

@bp.route('/api/settings/sdr/<band>', methods=['GET'])
@admin_required
def get_sdr(band: str) -> Any:
    try:
        if band == '1090': return jsonify({'ok': True, 'settings': parse_sdr1090()})
        if band == '978':  return jsonify({'ok': True, 'settings': parse_sdr978()})
        return jsonify({'ok': False, 'error': 'unknown band'}), 404
    except Exception:
        logger.exception("Failed to read SDR settings")
        return jsonify({'ok': False, 'error': 'An internal error has occurred.'})

@bp.route('/api/settings/sdr/<band>', methods=['POST'])
@admin_required
def set_sdr(band: str) -> Any:
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
def get_feeders() -> Any:
    return jsonify({'ok': True, 'feeders': load_config()})

@bp.route('/api/settings/feeders', methods=['POST'])
@admin_required
def set_feeders() -> Any:
    try:
        save_feeders(request.get_json().get('feeders', []))
        return jsonify({'ok': True})
    except Exception:
        app.app.logger.exception("Failed to save feeders")
        return jsonify({'ok': False, 'error': 'An internal error occurred'})

@bp.route('/api/settings/feeder/<key>', methods=['GET'])
@admin_required
def get_feeder_cfg(key: str) -> Any:
    cfg = cast('dict[str, Any] | None', FEEDER_CONFIGS.get(key))
    if not cfg:
        return jsonify({'ok': False, 'error': 'No config defined for this feeder'})
    return jsonify({'ok': True, 'fields': cfg['fields'], 'settings': get_feeder_settings(key), 'label': cfg['label']})

@bp.route('/api/settings/feeder/<key>', methods=['POST'])
@admin_required
def set_feeder_cfg(key: str) -> Any:
    try:
        ok, msg = set_feeder_settings(key, request.get_json())
        if ok:
            fmap = {f['key']: f for f in load_config()}
            feeder = fmap.get(key)
            if feeder and feeder.get('kind') == 'service':
                service_key = str(feeder.get('key', ''))
                service_action(service_key, 'restart')
        return jsonify({'ok': ok, 'message': msg})
    except Exception:
        app.app.logger.exception("Failed to update feeder config for key=%s", key)
        return jsonify({'ok': False, 'error': 'An internal error occurred'})
