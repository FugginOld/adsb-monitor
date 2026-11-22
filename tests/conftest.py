import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import app as appmod  # noqa: E402
from flask.testing import FlaskClient  # noqa: E402
from fakes import FakeHost  # noqa: E402


TEST_ADMIN_PASSWORD = 'correct horse battery'


class DashboardClient(FlaskClient):
    """Test client that behaves like a logged-in dashboard tab.

    Sends the header the dashboard's own fetch calls add, and starts with an
    admin login session (the admin_auth fixture below has set a password).
    Tests of the guards themselves use a plain FlaskClient
    (tests/test_csrf_guard.py, tests/test_admin_login.py).
    """
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.environ_base['HTTP_X_REQUESTED_WITH'] = 'adsb-monitor'
        with self.session_transaction() as s:
            s['admin'] = True


appmod.app.test_client_class = DashboardClient


@pytest.fixture(autouse=True)
def admin_auth(tmp_path, monkeypatch):
    """Point ADMIN_AUTH_FILE at a temp file holding TEST_ADMIN_PASSWORD (cheap hash)."""
    import json
    from werkzeug.security import generate_password_hash
    from system import admin_login
    path = tmp_path / 'admin_auth.json'
    path.write_text(json.dumps({
        'password_hash': generate_password_hash(TEST_ADMIN_PASSWORD, method='pbkdf2:sha256:1000'),
        'secret_key': 'test-secret-key',
    }))
    monkeypatch.setattr(appmod, 'ADMIN_AUTH_FILE', str(path))
    monkeypatch.setattr(admin_login, 'FAIL_DELAY_S', 0)
    admin_login._failures.clear()
    yield path
    admin_login._failures.clear()


@pytest.fixture(autouse=True)
def fake_host():
    """Swap HOST and INIT for FakeHost/SystemdAdapter around every test, then restore."""
    original_host = appmod.HOST
    original_init = appmod.INIT
    fake = FakeHost()
    appmod.HOST = fake
    appmod.INIT = appmod.SystemdAdapter(fake)
    yield fake
    appmod.HOST = original_host
    appmod.INIT = original_init


@pytest.fixture
def ledger_db(tmp_path):
    """Point DB_FILE at a fresh temp SQLite file with the schema created. Opt-in."""
    original = appmod.DB_FILE
    appmod.DB_FILE = str(tmp_path / 'test.db')
    appmod.init_db()
    yield appmod.DB_FILE
    appmod.DB_FILE = original
