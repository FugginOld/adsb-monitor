"""Admin-port login (system/auth.py login_guard, routes/login.py, system/admin_login.py)."""
import json
import os
import stat

import pytest
from flask.testing import FlaskClient

import app as appmod
from conftest import TEST_ADMIN_PASSWORD
from system import admin_login

H = {'X-Requested-With': 'adsb-monitor'}


@pytest.fixture
def raw():
    """A browser with no login session (but the dashboard's write header on POSTs)."""
    appmod.app.config['TESTING'] = True
    return FlaskClient(appmod.app, appmod.app.response_class, use_cookies=True)


def login(c, pw=TEST_ADMIN_PASSWORD):
    return c.post('/api/login', json={'password': pw}, headers=H)


@pytest.fixture
def readonly_port():
    appmod._request_port.port = appmod.READONLY_PORT
    yield
    appmod._request_port.port = appmod.ADMIN_PORT


# ── locked without a session ─────────────────────────────────────────────────
def test_api_needs_login(raw):
    r = raw.get('/api/mode')
    assert r.status_code == 401
    assert r.get_json()['login'] == '/login'


def test_page_redirects_to_login(raw):
    r = raw.get('/')
    assert r.status_code == 302
    assert r.headers['Location'].endswith('/login')


@pytest.mark.parametrize('path', ['/api/logs/readsb', '/api/backup', '/api/settings/airspy'])
def test_admin_data_needs_login(raw, path):
    assert raw.get(path).status_code == 401


def test_post_needs_login_even_with_header(raw, fake_host):
    assert raw.post('/api/service/readsb/restart', headers=H).status_code == 401
    assert not any(c[0] == 'run' for c in fake_host.calls)


@pytest.mark.parametrize('path', ['/login', '/api/login/status', '/static/vendor/tabler/tabler-icons.min.css'])
def test_login_page_and_assets_are_open(raw, path):
    assert raw.get(path).status_code == 200


# ── logging in and out ───────────────────────────────────────────────────────
def test_wrong_password_is_refused(raw):
    r = login(raw, 'nope')
    assert r.status_code == 401
    assert raw.get('/api/mode').status_code == 401


def test_right_password_opens_the_admin_port(raw):
    r = login(raw)
    assert r.status_code == 200 and r.get_json()['ok'] is True
    assert raw.get('/api/mode').status_code == 200
    assert raw.get('/api/login/status').get_json()['logged_in'] is True


def test_session_cookie_flags(raw):
    r = login(raw)
    cookie = r.headers['Set-Cookie']
    assert cookie.startswith('adsb_monitor_admin=')
    assert 'HttpOnly' in cookie and 'SameSite=Strict' in cookie and 'Expires=' in cookie


def test_login_post_needs_the_csrf_header(raw):
    r = raw.post('/api/login', json={'password': TEST_ADMIN_PASSWORD})
    assert r.status_code == 403


def test_logout_ends_the_session(raw):
    login(raw)
    assert raw.post('/api/logout', headers=H).status_code == 200
    assert raw.get('/api/mode').status_code == 401


def test_forged_cookie_is_rejected(raw):
    raw.set_cookie('adsb_monitor_admin', 'eyJhZG1pbiI6dHJ1ZX0.forged.sig')
    assert raw.get('/api/mode').status_code == 401


def test_new_password_logs_out_existing_sessions(raw):
    login(raw)
    admin_login.set_password('a whole new password')
    assert raw.get('/api/mode').status_code == 401
    assert login(raw, TEST_ADMIN_PASSWORD).status_code == 401
    assert login(raw, 'a whole new password').status_code == 200


def test_too_many_failures_lock_out_that_client(raw):
    for _ in range(admin_login.FAIL_MAX):
        assert login(raw, 'wrong').status_code == 401
    r = login(raw)  # even the right password is refused for now
    assert r.status_code == 429
    admin_login.clear_failures('127.0.0.1')
    assert login(raw).status_code == 200


def test_success_clears_earlier_failures(raw):
    for _ in range(admin_login.FAIL_MAX - 1):
        login(raw, 'wrong')
    assert login(raw).status_code == 200
    raw.post('/api/logout', headers=H)
    for _ in range(admin_login.FAIL_MAX - 1):
        assert login(raw, 'wrong').status_code == 401
    assert login(raw).status_code == 200


# ── no password set yet: locked ──────────────────────────────────────────────
def test_no_password_keeps_admin_port_locked(raw, admin_auth):
    os.remove(admin_auth)
    assert raw.get('/api/mode').status_code == 401
    r = login(raw)
    assert r.status_code == 403 and r.get_json()['setup'] is True
    s = raw.get('/api/login/status').get_json()
    assert s['password_set'] is False and 'set-password' in s['set_password_cmd']


def test_no_password_ignores_an_old_session_cookie(raw, admin_auth):
    login(raw)
    os.remove(admin_auth)
    assert raw.get('/api/mode').status_code == 401


@pytest.mark.parametrize('content', ['', 'not json', '[]', '{"password_hash": "x"}'])
def test_broken_auth_file_counts_as_no_password(raw, admin_auth, content):
    admin_auth.write_text(content)
    assert admin_login.password_is_set() is False
    assert login(raw).status_code == 403


# ── read-only port is unaffected ─────────────────────────────────────────────
def test_readonly_port_needs_no_login(raw, readonly_port):
    assert raw.get('/api/mode').status_code == 200
    assert raw.get('/').status_code == 200


def test_readonly_port_refuses_login(raw, readonly_port):
    assert login(raw).status_code == 403


def test_readonly_port_still_refuses_admin_routes(raw, readonly_port):
    assert raw.post('/api/service/readsb/restart', headers=H).status_code == 403


# ── set_password storage ─────────────────────────────────────────────────────
def test_set_password_writes_hash_and_key_mode_0600(admin_auth):
    admin_login.set_password('long enough pw')
    data = json.loads(admin_auth.read_text())
    assert 'long enough pw' not in admin_auth.read_text()
    assert data['password_hash'] and len(data['secret_key']) == 64
    assert stat.S_IMODE(os.stat(admin_auth).st_mode) == 0o600
    assert admin_login.verify('long enough pw') and not admin_login.verify('other')


def test_set_password_rejects_short(admin_auth):
    before = admin_auth.read_text()
    with pytest.raises(ValueError):
        admin_login.set_password('short')
    assert admin_auth.read_text() == before
