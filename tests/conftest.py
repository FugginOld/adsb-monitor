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
