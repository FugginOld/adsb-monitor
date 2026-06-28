import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import app as appmod  # noqa: E402
from fakes import FakeHost  # noqa: E402


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
