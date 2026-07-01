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
import os
import threading
from flask import Flask, send_from_directory
from werkzeug.middleware.proxy_fix import ProxyFix

# ProxyFix trusts one layer of X-Forwarded-* headers so links/IPs are correct
# when the app sits behind a reverse proxy (nginx, Caddy, NPM).
app = Flask(__name__, static_folder='static')
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

# ── Paths & runtime config ───────────────────────────────────────────────────
# Where things live on the host. feeders.ini and history.db sit next to this
# file in /opt/adsb-monitor; the /run and /etc paths are written by the SDR stack.
# The URL/port values come from the systemd unit's Environment= lines (generated
# at install time) and fall back to sane defaults when unset.
# ─────────────────────────────────────────────────────────────────────────────

CONFIG_FILE    = os.path.join(os.path.dirname(__file__), 'feeders.ini')
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

# ── Uptime ledger + SQLite history ─────────────────────────────────────────
# Moved to system/db.py (fold_uptime, _query_events, init_db,
# record_service_event, record_metrics, _daily_uptime, get_uptime_bars,
# get_service_uptime_pct, get_metrics_history, get_service_uptime_str,
# get_docker_uptime_str). Reaches DB_FILE/INIT/HOST via `import app`.
# ───────────────────────────────────────────────────────────────────────────
from system.db import (  # noqa: E402
    fold_uptime, _query_events, init_db, record_service_event, record_metrics,
    _daily_uptime, get_uptime_bars, get_service_uptime_pct, get_metrics_history,
    get_service_uptime_str, get_docker_uptime_str,
)

# ── Feeder last-seen ───────────────────────────────────────────────────────
# Moved to system/feeders.py (FEEDER_STATUS_FILES, get_feeder_last_seen,
# get_fr24_last_seen, get_piaware_last_seen). Reaches HOST via `import app`.
# ───────────────────────────────────────────────────────────────────────────
from system.feeders import (  # noqa: E402
    FEEDER_STATUS_FILES, get_feeder_last_seen, get_fr24_last_seen, get_piaware_last_seen,
    FeederHealth, feeder_status, _feeder_last_seen, _feeder_running_for, probe, readsb_metrics,
)

# ── Stats + version checking ───────────────────────────────────────────────
# Moved to system/stats.py (get_airspy_stats, gain_recommendation,
# get_system_metrics, get_readsb_deep_stats, _uat_msg_cache, _uat_msg_rate,
# get_band_stats) and system/versions.py (VERSION_SOURCES, _fetch_url,
# _get_installed_version, _get_latest_version, _is_outdated,
# refresh_versions, get_versions). Reach HOST/*_DEFAULT/*_JSON via `import app`.
# ───────────────────────────────────────────────────────────────────────────
from system.stats import (  # noqa: E402
    get_airspy_stats, gain_recommendation, get_system_metrics, get_readsb_deep_stats,
    _uat_msg_cache, _uat_msg_rate, get_band_stats,
)
import system.versions as _versions_mod  # noqa: E402
from system.versions import (  # noqa: E402
    VERSION_SOURCES, _fetch_url, _get_installed_version, _get_latest_version, _is_outdated,
    refresh_versions, get_versions,
)

# ── Feeder configs + config store ─────────────────────────────────────────
# Moved to system/config_io.py (FEEDER_CONFIGS, read_*/write_* adapters,
# CONFIG_ADAPTERS, _config_adapter, get_/set_feeder_settings, load_config,
# get_config_map, save_feeders). Reaches HOST/CONFIG_FILE via `import app`.
# ───────────────────────────────────────────────────────────────────────────
from system.config_io import (  # noqa: E402
    FEEDER_CONFIGS, read_flat_ini, write_flat_ini, read_shell_vars, write_shell_vars,
    read_piaware_config, read_docker_env, Adapter, _writable_only, _write_ini_flat,
    _write_shell_vars, _write_piaware, _write_docker, CONFIG_ADAPTERS, _config_adapter,
    get_feeder_settings, set_feeder_settings, load_config, get_config_map, save_feeders,
)

# ── Status ─────────────────────────────────────────────────────────────────
# Moved to system/services.py (systemd_status, docker_status, service_action).
# Reaches HOST/INIT via `import app`.
# ───────────────────────────────────────────────────────────────────────────
from system.services import systemd_status, docker_status, service_action  # noqa: E402

# ── Airspy / receiver / per-SDR settings + SDR presence guard ──────────────
# Moved to system/sdr.py (parse_airspy_options, write_airspy_options,
# parse_receiver_options, write_receiver_options, BIASTEE_1090_CONF,
# _opt_in_receiver, parse_sdr1090, _set_receiver_flag, write_sdr1090,
# parse_sdr978, write_sdr978, _sdr_autostopped, _sdr1090_serial,
# _sdr978_serial, _rtl_present, _airspy_present, _enforce_sdr,
# enforce_sdr_presence, MINI_IDS, R2_IDS, detect_airspy_model,
# airspy_live_hint). Reaches HOST/AIRSPY_DEFAULT/READSB_DEFAULT/
# DUMP978_DEFAULT via `import app`.
# ───────────────────────────────────────────────────────────────────────────
from system.sdr import (  # noqa: E402
    parse_airspy_options, write_airspy_options, parse_receiver_options, write_receiver_options,
    BIASTEE_1090_CONF, _opt_in_receiver, parse_sdr1090, _set_receiver_flag, write_sdr1090,
    parse_sdr978, write_sdr978, _sdr_autostopped, _sdr1090_serial, _sdr978_serial, _rtl_present,
    _airspy_present, _enforce_sdr, enforce_sdr_presence, MINI_IDS, R2_IDS, detect_airspy_model,
    airspy_live_hint,
)

# ── Log streaming + background poller ──────────────────────────────────────
# Moved to system/logstream.py (_sse, _journalctl, _log_command, stream_logs)
# and system/poll.py (background_poll). Reach LOG_LINES via `import app`.
# ───────────────────────────────────────────────────────────────────────────
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
def index():
    return send_from_directory('static', 'index.html')

# ── Server ─────────────────────────────────────────────────────────────────
# Boots the app. Runs two ThreadedWSGIServers over the same Flask app — one per
# port — so each request can be tagged with the port it arrived on (admin vs
# read-only, see Port tagging). __main__ initializes the DB, starts the
# version-refresh and background-poller daemon threads, then serves both ports.
# ───────────────────────────────────────────────────────────────────────────

def make_tagged_app(port):
    def tagged_app(environ, start_response):
        _request_port.port = port
        return app(environ, start_response)
    return tagged_app

def run_server(port):
    from werkzeug.serving import ThreadedWSGIServer
    server = ThreadedWSGIServer('0.0.0.0', port, make_tagged_app(port))
    server.serve_forever()

if __name__ == '__main__':
    init_db()
    # Start background threads — version refresh is fully async, never blocks startup
    threading.Thread(target=refresh_versions, daemon=True).start()
    threading.Thread(target=background_poll,  daemon=True).start()
    threading.Thread(target=run_server, args=(READONLY_PORT,), daemon=True).start()
    run_server(ADMIN_PORT)
