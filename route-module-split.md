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
                      # register_blueprints(app), make_tagged_app, run_server
                      # (no `if __name__ == '__main__':` — see run.py)

run.py               # Entry point: `import app` + init_db() + start
                      # background_poll/refresh_versions threads + run_server.
                      # Added post-migration — see §10.
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
- **`HOST` / `INIT` / `DB_FILE` are reassigned, not mutated, by tests**
  (`conftest.py`: `appmod.HOST = fake`, `appmod.INIT = ...`,
  `appmod.DB_FILE = str(tmp_path/...)`; `test_init_adapter.py` also does
  `monkeypatch.setattr(appmod, 'INIT', ...)`). A plain re-export shim
  (`from system.x import HOST`) creates an independent binding that the
  monkeypatch never reaches, so moved code would silently keep using the
  real host/DB in tests. Keep these three names defined in `app.py` and have
  every moved function reference them as `app.HOST` / `app.INIT` /
  `app.DB_FILE` (via `import app`) instead of a bare name. See §9's slices
  3-8 for exactly which functions this touches.

## 9. TDD execution slices

Baseline (verified before slice 1): `pytest -q` → **119 passed**. Each slice
below is a red→green cycle: move the code (imports/tests break = red),
add the one-line re-export shim in `app.py` (green), run `pytest -q`, then
check the box and log the result here. No new test files — the existing 119
tests are the regression harness for this refactor (Option A, §5).

**Cross-cutting rule discovered while reading app.py (see §7 addendum):**
`HOST`, `INIT`, `DB_FILE` are monkeypatched by *reassignment* in
`conftest.py` (`appmod.HOST = fake`, `appmod.DB_FILE = str(tmp_path/...)`).
They must stay defined in `app.py` itself. Any moved function that touches
them uses `import app` + `app.HOST` / `app.INIT` / `app.DB_FILE` at the call
site — not a bare name and not `from system.x import HOST` — otherwise the
monkeypatch silently stops applying to the moved code.

- [x] **Slice 1 — `system/init_adapter.py`**: move `Result`, `LinuxHost`,
      `InitAdapter`/`SystemdAdapter`/`OpenRCAdapter`/`NullAdapter`,
      `detect_init`. `HOST = LinuxHost()` / `INIT = detect_init(HOST)` stay in
      `app.py`. Re-export the moved names. Run `pytest -q`.
      → Done: 119 passed.
- [x] **Slice 2 — `system/auth.py`**: move `is_readonly`, `admin_required`
      (needs `_request_port`, `ADMIN_PORT`, `READONLY_PORT` — pass/import from
      app.py). Re-export. Run `pytest -q`.
      → Done: 119 passed.
- [x] **Slice 3 — `system/db.py`**: move `fold_uptime`, `_query_events`,
      `init_db`, `record_service_event`, `record_metrics`, `_daily_uptime`,
      `get_uptime_bars`, `get_service_uptime_pct`, `get_metrics_history`,
      `get_service_uptime_str`, `get_docker_uptime_str`. All `DB_FILE` uses →
      `app.DB_FILE`; `get_service_uptime_str` uses `app.INIT`. `app.py` still
      calls `init_db()` at startup. Re-export. Run `pytest -q`.
      → Done: 119 passed.
- [x] **Slice 4 — `system/config_io.py`**: move `read_flat_ini`,
      `write_flat_ini`, `read_shell_vars`, `write_shell_vars`,
      `read_piaware_config`, `read_docker_env`, `_writable_only`,
      `_write_ini_flat`, `_write_shell_vars`, `_write_piaware`,
      `_write_docker`, `Adapter`, `CONFIG_ADAPTERS`, `_config_adapter`,
      `get_feeder_settings`, `set_feeder_settings`, `load_config`,
      `get_config_map`, `save_feeders`, `FEEDER_CONFIGS`. `CONFIG_FILE` stays
      in `app.py`, imported read-only. All `HOST` uses → `app.HOST`.
      Re-export. Run `pytest -q`.
      → Done: 119 passed.
- [x] **Slice 5 — `system/services.py`**: move `systemd_status`,
      `docker_status`, `service_action`. `INIT` → `app.INIT`, `HOST` →
      `app.HOST`. Re-export. Run `pytest -q`. (Moved ahead of feeders.py in
      execution order since feeders.py depends on `docker_status`.)
      → Done: 119 passed.
- [x] **Slice 6 — `system/feeders.py`**: move `FEEDER_STATUS_FILES`,
      `get_feeder_last_seen`, `get_fr24_last_seen`, `get_piaware_last_seen`,
      `FeederHealth`, `feeder_status`, `_feeder_last_seen`,
      `_feeder_running_for`, `probe`, `readsb_metrics`. Depends on slices 3-5
      (imports `system.db`, `system.config_io`, `system.services`). `HOST` →
      `app.HOST`. Re-export. Run `pytest -q`.
      → Done: 119 passed.
- [x] **Slice 7 — `system/sdr.py`**: move `parse_airspy_options`,
      `write_airspy_options`, `parse_receiver_options`,
      `write_receiver_options`, `_opt_in_receiver`, `parse_sdr1090`,
      `_set_receiver_flag`, `write_sdr1090`, `parse_sdr978`, `write_sdr978`,
      `_sdr1090_serial`, `_sdr978_serial`, `_rtl_present`, `_airspy_present`,
      `_enforce_sdr`, `enforce_sdr_presence`, `BIASTEE_1090_CONF`,
      `_sdr_autostopped`, `MINI_IDS`, `R2_IDS`, `detect_airspy_model`,
      `airspy_live_hint`. `HOST` → `app.HOST`; `systemd_status`/
      `service_action` imported from `system.services`. Re-export.
      Run `pytest -q`.
      → Done: 119 passed.
- [x] **Slice 8 — `system/stats.py`, `system/versions.py`,
      `system/logstream.py`, `system/poll.py`**: same pattern, one at a time.
      `stats.py`: `get_system_metrics`, `get_readsb_deep_stats`,
      `get_band_stats`, `_uat_msg_rate`, `_uat_msg_cache`, `get_airspy_stats`,
      `gain_recommendation` (`HOST`→`app.HOST`; calls `system.services.systemd_status`).
      `versions.py`: `VERSION_SOURCES`, `_fetch_url`, `_get_installed_version`,
      `_get_latest_version`, `_is_outdated`, `refresh_versions`,
      `get_versions`, `_version_cache`/`_version_lock`/`_version_ts`/
      `VERSION_TTL` (self-contained, no app.py globals needed; `HOST`→`app.HOST`).
      `logstream.py`: `_sse`, `_journalctl`, `_log_command`, `stream_logs`,
      `LOG_LINES` stays in app.py (read-only import).
      `poll.py`: `background_poll` (imports `system.sdr.enforce_sdr_presence`,
      `system.config_io.load_config`, `system.services.feeder_status`... via
      `system.feeders`, `system.db.record_service_event/record_metrics`,
      `system.feeders.readsb_metrics`).
      Run `pytest -q` after each file.
      → Done: 119 passed (all four files; also fixed `/api/versions/refresh`'s
      `global _version_ts; _version_ts = 0` to
      `_versions_mod._version_ts = 0` — that route wasn't test-covered but
      would have silently stopped resetting the real cache).
- [x] **Slice 9 — routes extraction**: split `@app.route` handlers into
      `routes/dashboard.py`, `routes/stats.py`, `routes/versions.py`,
      `routes/settings.py`, `routes/services.py`, `routes/logs.py`,
      `routes/backup.py`; `routes/__init__.py:register_blueprints(app)`.
      `GET /` stays in `app.py`. Run `pytest -q` after registering blueprints
      (route tests are indirect — via `appmod.app.test_client()` — so this is
      the one slice where the safety net is thinner; smoke-test each endpoint
      path manually too).
      → Done: 119 passed. Manual smoke test via `test_client()` against all
      19 read routes (with a temp DB so init_db() has a service_events
      table): all 200, `/api/logs/nope` correctly 404s (unknown key). Full
      route inventory (`url_map.iter_rules()`) matches the original 25
      endpoints exactly, same paths, no prefix drift.
- [x] **Slice 10 — cleanup + final verification**: delete now-dead re-export
      lines from `app.py` only after confirming no test/module imports the
      moved names from `app` directly beyond the shim
      (`grep -rn "from app import\|import app as" tests/`), run
      `ruff check .`, `pytest -q`, `shellcheck installer/*.sh`. Manual smoke
      test deferred to a live Pi (not available in this session) — see
      Definition of Done.
      → Done: 25 `import app`/`from app import` references across tests/ —
      all still resolve (nothing to delete; the shims in app.py ARE what
      those 25 references need — Option A means they stay load-bearing, not
      dead code). Pruned the now-genuinely-unused raw imports in app.py
      (`configparser`, `subprocess`, `json`, `io`, `re`, `sqlite3`,
      `urllib.request`, `psutil`, `shutil`, `namedtuple`, `datetime`,
      `timezone`, `jsonify`, `Response`, `stream_with_context`, `request`,
      `time`, and the module-level `logger`/`logging` — none referenced by
      app.py's own code or by tests). Added `[lint.per-file-ignores]
      "app.py" = ["F401"]` to ruff.toml for the re-export shim (unused-import
      is the *point* of those lines) rather than 100+ scattered `# noqa`
      comments. Also fixed 3 call sites in routes/stats.py and
      routes/settings.py that had written `app.logger.exception(...)`
      (meaning the Flask instance's logger in the original single-file code)
      — after the split `app` is the imported *module*, so that would have
      silently resolved to a different logger; changed to `app.app.logger`
      to preserve the original instance. `ruff check .` clean, `pytest -q`
      119 passed, `app.py` now 193 lines (down from 1841) — over the ~150
      aspirational target because Option A's re-export shim genuinely needs
      ~100 names; see Definition of Done for the honest accounting.
      shellcheck not installed in this environment — skipped, unaffected by
      this change (no shell scripts touched).

## 8. Definition of done

- [x] ~~`app.py` under ~150 lines~~ — landed at **193 lines**. Flask app
      creation, blueprint registration, `init_db()` call, background poll
      thread start, `make_tagged_app`, `run_server` are all there, but so is
      the Option A re-export shim (~100 names across 8 `from system.x
      import (...)` blocks) that the 25 `appmod.<name>` references in
      tests/ require. That shim is the deliberate cost of "no test files
      modified" — cutting it below 150 lines means Option B (§5), which is
      an explicit follow-up, not this pass.
- [x] All logic lives in `system/*.py` (13 modules), all HTTP glue lives in
      `routes/*.py` (7 blueprints + `__init__.py:register_blueprints`).
- [x] `ruff check .` clean (`app.py`'s shim imports carry a scoped
      `per-file-ignores` F401 exemption in ruff.toml, not scattered `noqa`).
- [x] `pytest -q` fully green — 119 passed, no test files modified.
- [x] `grep -rn "from app import\|import app as" tests/` still resolves —
      25 matches, all still valid.
- [x] Manual smoke test on rpi5b: dashboard loads, `/api/status`,
      `/api/settings/feeder/<key>` GET, `/api/logs/<key>` stream.
      → Done, on real hardware — see §10 for the two real bugs this surfaced
      (neither was a pure-refactor regression the test suite could have
      caught, both are now fixed and deployed). Final state:
      `adsb-monitor.service` `active (running)` on rpi5b, `/api/status`
      returns all 9 configured feeders/services correctly
      (`airspy_adsb`, `readsb`, `tar1090`, `fr24feed`, `piaware`,
      `adsbexchange-feed`, `adsbexchange-mlat`, `adsbfi-feed`,
      `airnavradar`), matching the live `/opt/adsb-monitor/feeders.ini`
      exactly.

## 10. Deployment findings (post-migration, on real hardware)

Two bugs surfaced deploying to rpi5b that the test suite structurally
could not catch (both are about how the *process* boots, not about any
function's behavior — `pytest` only ever does `import app as appmod`,
never executes `app.py` as `__main__`):

1. **Circular import crash on boot.** systemd's `ExecStart` ran
   `python app.py` directly, which executes `app.py` as `__main__`. But
   `system/*.py` modules do `import app` (required so `conftest.py`'s
   `appmod.HOST = fake`-style monkeypatches reach them — see §7/§9's
   `HOST`/`INIT`/`DB_FILE` rule). Python then imports `app.py` a *second*
   time under the module name `app`, and that second pass collides
   mid-import with the first (`ImportError: cannot import name
   'is_readonly' from partially initialized module 'system.auth'`).
   **Fix:** added `run.py` as the only sanctioned entry point — it does
   `import app` (a normal, single import) and calls `app.init_db()` /
   `app.run_server(...)` etc. `app.py` no longer has an `if __name__ ==
   '__main__':` block at all, so it can never be executed directly again.
   `install.sh` now generates `ExecStart=.../python run.py`; `update.sh`
   deploys `run.py` and self-migrates any existing unit still pointing at
   `app.py` (one-time `sed` + `daemon-reload`).
2. **Shell scripts untracked as executable.** Unrelated to the refactor's
   logic, but it blocked deploying the fix above: every `*.sh` in the repo
   (`install.sh`, `update.sh`, all of `installer/*.sh`) had always been
   committed as mode `100644`, not `100755` — true since the very first
   commit that added them, confirmed via `git log --raw`. Harmless until
   this session, when `install.sh`/`update.sh` started getting real content
   updates and a Pi where they'd been locally `chmod +x`'d hit a genuine
   file-mode conflict on every `git pull` (`Your local changes... would be
   overwritten`). **Fix:** `git update-index --chmod=+x` on all 8 scripts,
   committed as a pure mode change — permanent, no more manual `chmod`
   needed after a fresh clone or pull.

Both fixes are committed and pushed to `origin/main`
(`e12be32` — run.py entry point; `f9374a9` — executable bits) and deployed
to rpi5b. `pytest -q` stayed green throughout (119 passed) since neither
bug was reachable from the test suite's import path — this is a real gap
in the regression net for this specific class of bug (entry-point/process
bootstrapping), not something more unit tests inside `system/*.py` would
have caught. Worth a `tests/test_entrypoint.py` that actually subprocesses
`python run.py` and checks it doesn't exit non-zero in the first second, if
this class of bug is worth guarding against going forward — not done here
since it's a new test infra decision, not a mechanical part of this
refactor.
