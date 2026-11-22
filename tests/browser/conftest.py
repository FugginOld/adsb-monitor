"""Browser tests: a real HTTP server running the app on a fake station, driven
by headless Chromium (Playwright).

Skipped when Playwright isn't installed, so a plain `pytest -q` still works
everywhere; CI's `browser` job installs it. Locally:
    pip install playwright && playwright install chromium-headless-shell
The parent conftest's autouse fixtures (FakeHost, admin password, no real
version refreshes or verify threads) apply here too.
"""
import threading

import pytest

pytest.importorskip('playwright.sync_api')
from playwright.sync_api import sync_playwright  # noqa: E402
from werkzeug.serving import make_server  # noqa: E402

import app as appmod  # noqa: E402
from app import Result  # noqa: E402

FEEDERS_INI = """\
[service:airspy_adsb]
label = airspy_adsb
[service:readsb]
label = readsb
[service:fr24feed]
label = FlightRadar24
icon = plane
[service:adsbfi-feed]
label = adsb.fi
"""
TEST_ADMIN_PASSWORD = 'correct horse battery'   # same as tests/conftest.py's admin_auth fixture
UNITS = ('airspy_adsb', 'readsb', 'fr24feed', 'adsbfi-feed', 'tar1090', 'dump978-fa')


@pytest.fixture
def station(fake_host, admin_auth, ledger_db, tmp_path, monkeypatch):
    """The app on 127.0.0.1:<random>, admin port, against a FakeHost station."""
    ini = tmp_path / 'feeders.ini'
    ini.write_text(FEEDERS_INI)
    monkeypatch.setattr(appmod, 'CONFIG_FILE', str(ini))
    monkeypatch.setattr(appmod, 'AUTOTUNE_FILE', str(tmp_path / 'autotune.json'))
    monkeypatch.setattr(appmod, 'AUTOTUNE_STATE_FILE', str(tmp_path / 'autotune_state.json'))
    for u in UNITS:
        fake_host.commands[('systemctl', 'is-active', u)] = Result(0, 'active\n', '')
        fake_host.commands[('systemctl', 'restart', u)] = Result(0, '', '')
    fake_host.files[appmod.AIRSPY_DEFAULT] = 'G=-g\nGAIN=13\nSTATS=-S\nNET=-l 47787:beast\nAFFINITY=0\nM=\nOPTIONS="-v -f 1 -w 5 -e 20 -C 95 -E 60"\n'
    fake_host.files[appmod.READSB_DEFAULT] = 'RECEIVER_OPTIONS="--net-only"\nJSON_OPTIONS="--json-location-accuracy 2"\n'
    srv = make_server('127.0.0.1', 0, appmod.make_tagged_app(appmod.ADMIN_PORT), threaded=True)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield {'url': f'http://127.0.0.1:{srv.server_port}', 'host': fake_host, 'ini': ini}
    srv.shutdown()
    t.join(5)


@pytest.fixture(scope='session')
def browser():
    with sync_playwright() as p:
        b = p.chromium.launch()
        yield b
        b.close()


@pytest.fixture
def page(browser, station):
    """A logged-in dashboard tab. `page.problems` collects page errors, console
    errors and CSP violations; each test asserts it stays empty."""
    ctx = browser.new_context(viewport={'width': 1400, 'height': 900})
    pg = ctx.new_page()
    problems = []
    pg.on('pageerror', lambda e: problems.append(f'pageerror: {e}'))
    # The map/graphs iframes point nowhere in tests; their load failures aren't bugs.
    pg.on('console', lambda m: m.type == 'error' and 'Failed to load resource' not in m.text
          and problems.append(f'console: {m.text}'))
    pg.goto(station['url'] + '/')
    pg.wait_for_url('**/login')
    pg.fill('#password', TEST_ADMIN_PASSWORD)
    pg.click('#login-btn')
    pg.wait_for_url(station['url'] + '/')
    pg.wait_for_selector('#core-cards .card')
    pg.problems = problems
    yield pg
    ctx.close()
