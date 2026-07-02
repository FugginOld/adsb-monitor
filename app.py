"""ADS-B Stack Health Monitor — Flask app entry point.

Serves a one-page dashboard (static/index.html) plus a JSON/SSE API that reports
on a local ADS-B receiving station: the SDR decoder (readsb / airspy_adsb), the
live map (tar1090) and graphs (graphs1090), and every feeder that ships data to
networks like FlightAware, FR24, ADSBExchange, etc.

Business logic lives in `system/*.py` (no Flask imports); HTTP glue lives in
`routes/*.py` Blueprints, registered below. This module holds the Flask app
itself, true globals (config paths, `HOST`/`INIT`/`DB_FILE` singletons —
re-exported below for existing tests, see route-module-split.md §5/§7/§9),
DB init, and the background poller / server bootstrap.

Two ports run the same app: an admin port (full access) and a read-only port
(status/stats only). See `is_readonly` / `admin_required`.
"""
from __future__ import annotations

import os
import threading
from typing import Any, Callable
from flask import Flask, send_from_directory
from werkzeug.middleware.proxy_fix import ProxyFix

# ProxyFix trusts one layer of X-Forwarded-* headers so links/IPs are correct
# when the app sits behind a reverse proxy (nginx, Caddy, NPM).
app = Flask(__name__, static_folder='static')
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)  # type: ignore[method-assign]

# ── Paths & runtime config ───────────────────────────────────────────────────
# Where things live on the host. feeders.ini and history.db sit next to this
# file in /opt/adsb-monitor; the /run and /etc paths are written by the SDR stack.
# The URL/port values come from the systemd unit's Environment= lines (generated
# at install time) and fall back to sane defaults when unset.
# ─────────────────────────────────────────────────────────────────────────────

APP_VERSION    = '1.0.0'  # bump alongside CHANGELOG.md
CONFIG_FILE: str = os.path.join(os.path.dirname(__file__), 'feeders.ini')
DB_FILE        = os.path.join(os.path.dirname(__file__), 'history.db')
READSB_JSON    = '/run/readsb'
AIRSPY_STATS   = '/run/airspy_adsb/stats.json'
AIRSPY_DEFAULT = '/etc/default/airspy_adsb'
READSB_DEFAULT = '/etc/default/readsb'
DUMP978_DEFAULT  = '/etc/default/dump978-fa'
SKYAWARE978_JSON = '/run/skyaware978/aircraft.json'
LOG_LINES      = 100

TAR1090_URL_LOCAL     = os.environ.get('TAR1090_URL_LOCAL',     os.environ.get('TAR1090_URL', ''))
GRAPHS1090_URL_LOCAL  = os.environ.get('GRAPHS1090_URL_LOCAL',  os.environ.get('GRAPHS1090_URL', ''))
TAR1090_URL_REMOTE    = os.environ.get('TAR1090_URL_REMOTE',    TAR1090_URL_LOCAL)
GRAPHS1090_URL_REMOTE = os.environ.get('GRAPHS1090_URL_REMOTE', GRAPHS1090_URL_LOCAL)
ADMIN_PORT    = int(os.environ.get('ADMIN_PORT',    '5000'))
READONLY_PORT = int(os.environ.get('READONLY_PORT', '5001'))

# ── Host + init adapters ───────────────────────────────────────────────────
# Moved to system/init_adapter.py (Result, LinuxHost, InitAdapter family,
# detect_init). HOST/INIT stay defined here — conftest.py monkeypatches
# appmod.HOST / appmod.INIT by reassignment, so they must be true globals
# in this module; moved code reaches them via `import app` + `app.HOST`/
# `app.INIT`, not a bare name.
# ───────────────────────────────────────────────────────────────────────────
from system.init_adapter import (  # noqa: E402
    Result, LinuxHost, InitAdapter, SystemdAdapter, OpenRCAdapter, NullAdapter, detect_init,
)

HOST = LinuxHost()
INIT = detect_init(HOST)

# ── Port tagging ───────────────────────────────────────────────────────────
# `_request_port` stays here (true global set per-request by make_tagged_app).
# is_readonly / admin_required moved to system/auth.py, which reaches this via
# `import app` + `app._request_port`.
# ───────────────────────────────────────────────────────────────────────────

_request_port = threading.local()

from system.auth import is_readonly, admin_required  # noqa: E402

# ── Re-exports from system/*.py ─────────────────────────────────────────────
# Every name below moved into system/*.py during the route-module-split
# (route-module-split.md). What's imported here is only what's still
# referenced by `appmod.X` in tests/*.py or by run.py/app.py's own code —
# NOT the full contents of each module (see ARCHITECTURE.md for that). Names
# routes/*.py needs are imported directly from system/*.py, not through here.
# Reaches HOST/INIT/DB_FILE/CONFIG_FILE/*_DEFAULT/*_JSON via `import app`.
# ───────────────────────────────────────────────────────────────────────────
from system.db import (  # noqa: E402
    fold_uptime, _query_events, init_db, record_service_event, record_metrics,
    _daily_uptime, get_uptime_bars, get_service_uptime_pct, get_metrics_history,
    get_service_uptime_str, get_docker_uptime_str,
)
from system.feeders import (  # noqa: E402
    FEEDER_STATUS_FILES, get_feeder_last_seen, get_fr24_last_seen, get_piaware_last_seen,
    feeder_status, probe, readsb_metrics,
)
from system.stats import (  # noqa: E402
    get_airspy_stats, gain_recommendation, get_system_metrics, get_readsb_deep_stats,
    _uat_msg_cache, _uat_msg_rate, get_band_stats,
)
from system.versions import (  # noqa: E402
    VERSION_SOURCES, _fetch_url, _get_installed_version, _get_latest_version, _is_outdated,
    refresh_versions, get_versions,
)
from system.config_io import (  # noqa: E402
    FEEDER_CONFIGS, read_flat_ini, write_flat_ini, read_shell_vars, write_shell_vars,
    read_piaware_config, read_docker_env, CONFIG_ADAPTERS, _config_adapter,
    get_feeder_settings, set_feeder_settings, load_config, get_config_map, save_feeders,
)
from system.services import systemd_status, docker_status, service_action  # noqa: E402
from system.sdr_detect import MINI_IDS, R2_IDS, detect_airspy_model  # noqa: E402
from system.sdr_settings import write_airspy_options, airspy_live_hint  # noqa: E402
from system.sdr_presence import _sdr_autostopped, _enforce_sdr, enforce_sdr_presence  # noqa: E402
from system.logstream import _sse, _journalctl, _log_command, stream_logs  # noqa: E402
from system.poll import background_poll  # noqa: E402

# ── Routes ─────────────────────────────────────────────────────────────────
# Moved to routes/*.py Blueprints (dashboard, stats, versions, settings,
# services, logs, backup), registered via routes/__init__.py:
# register_blueprints(app). GET / stays here.
# ───────────────────────────────────────────────────────────────────────────
from routes import register_blueprints  # noqa: E402

register_blueprints(app)

@app.route('/')
def index() -> Any:
    return send_from_directory('static', 'index.html')

# ── Server ─────────────────────────────────────────────────────────────────
# Boots the app. Runs two ThreadedWSGIServers over the same Flask app — one per
# port — so each request can be tagged with the port it arrived on (admin vs
# read-only, see Port tagging). __main__ initializes the DB, starts the
# version-refresh and background-poller daemon threads, then serves both ports.
# ───────────────────────────────────────────────────────────────────────────

def make_tagged_app(port: int) -> Callable[[dict[str, Any], Any], Any]:
    def tagged_app(environ: dict[str, Any], start_response: Any) -> Any:
        _request_port.port = port
        return app(environ, start_response)
    return tagged_app

def run_server(port: int) -> None:
    from werkzeug.serving import ThreadedWSGIServer
    server = ThreadedWSGIServer('0.0.0.0', port, make_tagged_app(port))
    server.serve_forever()

# No `if __name__ == '__main__':` here on purpose: system/*.py modules do
# `import app` to reach HOST/INIT/DB_FILE (see route-module-split.md §7/§9).
# Running this file directly would execute it once as `__main__` and again
# as `app` when those imports fire, colliding mid-import
# (ImportError: partially initialized module 'system.auth'). Start the
# server via run.py instead, which only ever imports this module by name.
