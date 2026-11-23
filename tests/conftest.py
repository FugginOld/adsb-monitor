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
def change_records(monkeypatch, request):
    """The "what changed" feed (system/changes.py) writes to history.db. Capture
    its rows here instead, unless the test uses ledger_db (a real temp DB)."""
    from system import changes
    changes.reset()
    rows = []
    if 'ledger_db' not in request.fixturenames:
        monkeypatch.setattr(changes, 'record_change', lambda *a, **k: rows.append(a) or True)
    yield rows
    changes.reset()


@pytest.fixture(autouse=True)
def no_background_checks(monkeypatch):
    """Settings saves start a verify thread (system/apply_guard.py) that would
    sleep and then poll a later test's FakeHost. Record checks instead; tests of
    the check itself call apply_guard.run_check() with a fake clock."""
    from system import apply_guard
    started = []
    monkeypatch.setattr(apply_guard, '_start', started.append)
    with apply_guard._lock:
        apply_guard._checks.clear()
    return started


@pytest.fixture(autouse=True)
def fresh_dataflow_state():
    """Each test starts with no data-flow timers (system/dataflow.py keeps them in memory)."""
    from system import dataflow
    dataflow.reset()
    yield
    dataflow.reset()


@pytest.fixture(autouse=True)
def fresh_version_cache(monkeypatch):
    """No test may start a real refresh_versions: it runs on a daemon thread,
    outlives its test, records `airspy_adsb --version` / `dpkg-query` calls on a
    later test's FakeHost, and makes real GitHub requests. So the cache is marked
    fresh (get_versions() won't spawn one) and refresh_versions is a no-op
    everywhere it is imported (routes/versions.py, the stack-update finisher, app).
    Tests that care about refreshes monkeypatch it again themselves."""
    import time
    import routes.versions as rv
    import system.versions as versions
    def no_refresh(*_a, **_k):
        return None
    monkeypatch.setattr(versions, '_version_ts', time.time())
    monkeypatch.setattr(versions, 'refresh_versions', no_refresh)
    monkeypatch.setattr(rv, 'refresh_versions', no_refresh)
    monkeypatch.setattr(appmod, 'refresh_versions', no_refresh)


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
