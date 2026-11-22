"""Admin-port login routes: login page, login/logout, status.

The guard that enforces the login is system/auth.py `login_guard`; password
storage and the failed-login throttle are in system/admin_login.py.
"""
import time
from typing import Any

from flask import Blueprint, jsonify, request, send_from_directory, session

import app
from system import admin_login
from system.auth import is_readonly

bp = Blueprint('login', __name__)

SET_PASSWORD_CMD = 'sudo /opt/adsb-monitor/venv/bin/python /opt/adsb-monitor/run.py set-password'


@bp.route('/login')
def login_page() -> Any:
    return send_from_directory(app.app.static_folder or 'static', 'login.html')


@bp.route('/api/login/status')
def api_login_status() -> Any:
    return jsonify({
        'readonly': is_readonly(),
        'password_set': admin_login.password_is_set(),
        'logged_in': (not is_readonly()) and session.get('admin') is True,
        'set_password_cmd': SET_PASSWORD_CMD,
    })


@bp.route('/api/login', methods=['POST'])
def api_login() -> Any:
    if is_readonly():
        return jsonify({'ok': False, 'error': 'Not available on this port'}), 403
    if not admin_login.password_is_set():
        return jsonify({'ok': False, 'setup': True,
                        'error': f'No admin password set. Run: {SET_PASSWORD_CMD}'}), 403
    ip = request.remote_addr or '?'
    if admin_login.too_many_failures(ip):
        return jsonify({'ok': False, 'error': 'Too many failed logins. Try again in 15 minutes.'}), 429
    body = request.get_json(silent=True) or {}
    password = body.get('password') if isinstance(body, dict) else None
    if not isinstance(password, str) or not admin_login.verify(password):
        admin_login.record_failure(ip)
        time.sleep(admin_login.FAIL_DELAY_S)
        return jsonify({'ok': False, 'error': 'Wrong password'}), 401
    admin_login.clear_failures(ip)
    session.clear()
    session['admin'] = True
    session.permanent = True
    return jsonify({'ok': True})


@bp.route('/api/logout', methods=['POST'])
def api_logout() -> Any:
    session.clear()
    return jsonify({'ok': True})
