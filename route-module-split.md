# adsb-monitor: route-module-split plan

**Goal:** break `app.py` (1,840 lines, Flask) into a `system/` package (business
logic) + `routes/` package (thin Flask Blueprints), mirroring the pattern
already used in `fugginnas` (`system/*.py` + `routes/*.py`).

**Non-goal:** no behavior changes. This is a pure structural refactor — same
endpoints, same responses, same DB schema, same config files.

---

## 1. Current state (baseline, verified)

- `ruff check .` → clean, 0 issues
- `shellcheck` on all installer scripts → clean (info-level notes only)
- No `shell=True` subprocess calls
- Framework: **Flask** (`@app.route(...)`), not FastAPI
- All tests import directly from the module: `import app as appmod`,
  `from app import InitAdapter, Result` (see `tests/conftest.py`,
  `tests/fakes.py`, and 15+ other test files). **This is the main migration
  risk** — see §5.

## 2. Inventory of app.py (by responsibility)

| Lines (approx) | Contents |
|---|---|
| 1–83 | imports, `app = Flask(...)`, `CONFIG_FILE`, module-level constants |
| 84–195 | `Result`, `LinuxHost`, `InitAdapter`, `SystemdAdapter`, `OpenRCAdapter`, `NullAdapter`, `detect_init()` |
| 196–238 | `is_readonly()`, `admin_required` decorator |
| 239–367 | uptime/event DB layer: `fold_uptime`, `_query_events`, `init_db`, `record_service_event`, `record_metrics`, `_daily_uptime`, `get_uptime_bars`, `get_service_uptime_pct`, `get_metrics_history`, `get_service_uptime_str`, `get_docker_uptime_str` |
| 374–540 | feeder last-seen + airspy/system/readsb stats: `get_feeder_last_seen`, `get_fr24_last_seen`, `get_piaware_last_seen`, `get_airspy_stats`, `gain_recommendation`, `get_system_metrics`, `get_readsb_deep_stats` |
| 540–635 | `_uat_msg_rate`, `get_band_stats` |
| 635–735 | version checking: `_fetch_url`, `_get_installed_version`, `_get_latest_version`, `_is_outdated`, `refresh_versions`, `get_versions` |
| 703–797 | `detect_airspy_model`, `airspy_live_hint`, `FEEDER_CONFIGS` table |
| 797–999 | config read/write adapters: `read_flat_ini`, `write_flat_ini`, `read_shell_vars`, `write_shell_vars`, `read_piaware_config`, `read_docker_env`, `_writable_only`, `_write_ini_flat`, `_write_shell_vars`, `_write_piaware`, `_write_docker`, `CONFIG_ADAPTERS`, `_config_adapter`, `get_feeder_settings`, `set_feeder_settings`, `load_config`, `get_config_map`, `save_feeders` |
| 999–1074 | service control: `systemd_status`, `docker_status`, `service_action`, `FeederHealth`, `feeder_status`, `_feeder_last_seen`, `_feeder_running_for`, `probe`, `readsb_metrics` |
| 1074–1295 | SDR settings parse/write: `parse_airspy_options`, `write_airspy_options`, `parse_receiver_options`, `write_receiver_options`, `_opt_in_receiver`, `parse_sdr1090`, `_set_receiver_flag`, `write_sdr1090`, `parse_sdr978`, `write_sdr978`, `_sdr1090_serial`, `_sdr978_serial`, `_rtl_present`, `_airspy_present`, `_enforce_sdr`, `enforce_sdr_presence` |
| 1295–1376 | log streaming: `_sse`, `_journalctl`, `_log_command`, `stream_logs`, `background_poll` |
| 1376–1822 | **all `@app.route` handlers** (see §3) |
| 1823–end | `make_tagged_app`, `run_server` |

## 3. Route inventory → target blueprint

| Route(s) | Blueprint module |
|---|---|
| `GET /api/alerts`, `GET /api/mode`, `GET /api/ui-config`, `GET /api/status` | `routes/dashboard.py` |
| `GET /api/stats/airspy`, `/readsb`, `/bands`, `/aircraft_types`, `/system`, `/history`, `/uptime/history`, `/uptime/<service>` | `routes/stats.py` |
| `POST /api/versions/refresh` | `routes/versions.py` |
| `GET/POST /api/settings/airspy`, `/receiver`, `/sdr/<band>`, `/feeders`, `/feeder/<key>` | `routes/settings.py` |
| `POST /api/service/<name>/restart` | `routes/services.py` |
| `GET /api/logs/<key>`, `GET /api/syslog` | `routes/logs.py` |
| `GET /api/backup`, `/backup/graphs`, `POST /api/restore`, `POST /api/restore/graphs` (+ `_safe_path_under_base`) | `routes/backup.py` |
| `GET /` | stays in `app.py` (or `routes/dashboard.py`) |

## 4. Target file map

```
system/
  __init__.py
  init_adapter.py    # Result, LinuxHost, InitAdapter + subclasses, detect_init
  auth.py            # is_readonly, admin_required
  db.py              # init_db, record_service_event, record_metrics,
                      # fold_uptime, _query_events, _daily_uptime,
                      # get_uptime_bars, get_service_uptime_pct,
                      # get_metrics_history, get_*_uptime_str
  feeders.py         # FEEDER_CONFIGS, get_feeder_last_seen + per-feeder
                      # variants, feeder_status, probe, _feeder_running_for
  config_io.py        # read/write adapters (ini/shell/piaware/docker),
                      # CONFIG_ADAPTERS, get/set_feeder_settings,
                      # load_config, save_feeders
  sdr.py             # airspy/receiver/band parse+write, SDR presence
                      # enforcement, detect_airspy_model
  stats.py           # get_system_metrics, get_readsb_deep_stats,
                      # get_band_stats, get_airspy_stats, gain_recommendation
  versions.py        # _fetch_url, _get_installed_version,
                      # _get_latest_version, _is_outdated, refresh_versions,
                      # get_versions
  services.py        # systemd_status, docker_status, service_action
  logstream.py       # _sse, _journalctl, _log_command, stream_logs
  backup.py          # _safe_path_under_base, backup/restore helpers
  poll.py            # background_poll

routes/
  __init__.py        # register_blueprints(app) helper
  dashboard.py
  stats.py
  versions.py
  settings.py
  services.py
  logs.py
  backup.py

app.py               # Flask() init, CONFIG_FILE + other true globals,
                      # register_blueprints(app), init_db(), start
                      # background_poll thread, make_tagged_app, run_server
```

This is the same two-layer shape `fugginnas` already uses: `system/` holds
logic with no Flask/FastAPI imports, `routes/` holds thin HTTP glue.

## 5. Test-compatibility strategy (the actual hard part)

Every test does `import app as appmod` and calls things like
`appmod.get_feeder_last_seen(...)`, `appmod.Result`, `appmod.InitAdapter`.
Two options:

**Option A — re-export shim (recommended, lowest risk):**
After the split, `app.py` re-imports everything tests reference, e.g.:
```python
from system.init_adapter import Result, LinuxHost, InitAdapter, SystemdAdapter, OpenRCAdapter, NullAdapter, detect_init
from system.db import init_db, record_service_event, record_metrics, fold_uptime, ...
from system.feeders import get_feeder_last_seen, feeder_status, ...
# etc — one line per moved symbol
```
No test files change. This is mechanical and safe to do first; verify with
`pytest -q` after the shim is in place and before deleting anything from
`app.py`.

**Option B — update tests to import from `system.*` directly.**
Cleaner long-term, but touches 15+ test files and is unnecessary churn for
this pass. Defer to a later cleanup once the split has proven stable.

**Recommendation:** do Option A now. Leave Option B as a follow-up issue.

## 6. Suggested migration order (small, verifiable steps)

1. Create `system/init_adapter.py`, move `Result`/`LinuxHost`/`InitAdapter`
   family + `detect_init`. Re-export from `app.py`. Run tests.
2. Create `system/auth.py` (`is_readonly`, `admin_required`). Re-export. Run tests.
3. Create `system/db.py` (uptime/event/metrics layer). Re-export. Run tests.
   — Watch for the module-level SQLite connection/`init_db()` call order;
   `app.py` must still call `init_db()` at startup.
4. Create `system/config_io.py` (ini/shell/piaware/docker read-write +
   `FEEDER_CONFIGS`/`CONFIG_ADAPTERS`). Re-export. Run tests.
5. Create `system/feeders.py` (last-seen, status, probe). Depends on #3, #4.
   Re-export. Run tests.
6. Create `system/sdr.py` (airspy/receiver/band parse+write, presence
   enforcement). Re-export. Run tests.
7. Create `system/stats.py`, `system/versions.py`, `system/services.py`,
   `system/logstream.py`, `system/backup.py`, `system/poll.py` — same
   pattern, one at a time, tests green after each.
8. Once `system/` is fully populated and all tests pass with `app.py` as a
   pure re-export shim, extract the `@app.route` handlers into
   `routes/*.py` Blueprints. Register them via
   `routes/__init__.py:register_blueprints(app)`.
9. Delete now-dead re-export lines from `app.py` **only after** confirming
   nothing outside `system/`/`routes/`/tests imports from `app` directly
   (`grep -rn "from app import\|import app" --include="*.py" .`).
10. Final pass: `ruff check .`, `pytest -q`, `shellcheck installer/*.sh`
    (unaffected but cheap to re-verify), manual smoke test against a live
    Pi 5 instance (dashboard load, one settings GET/POST, one log stream).

## 7. Things to watch for

- **Global mutable state**: `_version_ts` (used with `global` in
  `refresh_versions`/`get_versions`) needs a single owner module
  (`system/versions.py`) — don't let both `app.py` and the new module hold
  copies.
- **`CONFIG_FILE` path**: computed via `os.path.dirname(__file__)` relative
  to `app.py`'s location. If moved into `system/config_io.py`, this must be
  passed in or recomputed relative to the *package root*, not the new
  file's own `__file__>`, or it'll break on the installed Pi (`/opt/...`)
  path layout.
- **`background_poll` thread startup**: currently started somewhere near
  `run_server`/app init — confirm exact call site before moving, so the
  polling loop still starts exactly once.
- **`FEEDER_CONFIGS` / `CONFIG_ADAPTERS` are shared dicts** referenced from
  both config I/O and feeder-status code — keep them in one place
  (`system/config_io.py`) and import, don't duplicate.
- **Blueprint URL prefixes**: register each blueprint with
  `url_prefix='/api'` or keep full paths in each route — pick one
  convention and apply consistently so `/api/status` etc. don't shift.
- **`tests/fakes.py`** imports `InitAdapter, Result` from `app` — this file
  is a shared test fixture, so get it passing early (step 1) since other
  tests likely depend on it transitively.

## 8. Definition of done

- [ ] `app.py` under ~150 lines: Flask app creation, blueprint registration,
      `init_db()` call, background poll thread start, `make_tagged_app`,
      `run_server`.
- [ ] All logic lives in `system/*.py`, all HTTP glue lives in `routes/*.py`.
- [ ] `ruff check .` clean.
- [ ] `pytest -q` fully green, no test files modified (Option A shim).
- [ ] `grep -rn "from app import\|import app as" tests/` still resolves —
      confirms shim compatibility.
- [ ] Manual smoke test on rpi5b: dashboard loads, `/api/status`,
      `/api/settings/feeder/<key>` GET, `/api/logs/<key>` stream.
