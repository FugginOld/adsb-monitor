import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import app as appmod  # noqa: E402


@pytest.fixture(autouse=True)
def fake_host():
    """Swap the module-level HOST for a FakeHost around every test, then restore."""
    original = appmod.HOST
    fake = appmod.FakeHost()
    appmod.HOST = fake
    yield fake
    appmod.HOST = original


@pytest.fixture
def ledger_db(tmp_path):
    """Point DB_FILE at a fresh temp SQLite file with the schema created. Opt-in."""
    original = appmod.DB_FILE
    appmod.DB_FILE = str(tmp_path / 'test.db')
    appmod.init_db()
    yield appmod.DB_FILE
    appmod.DB_FILE = original
