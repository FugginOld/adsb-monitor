# ADS-B Stack Monitor — Code Architecture

Physical, file-by-file layout of the program. For *what* the domain terms
mean (Feeder, Uptime, Host, Config store, ...), see [CONTEXT.md](CONTEXT.md).
For the history of how this got split out of a single 1,841-line `app.py`,
see [route-module-split.md](route-module-split.md).

## The shape

```
run.py            Process entry point. `import app` + init_db() + start
                  background_poll/refresh_versions threads + run_server(port).
                  The ONLY thing that may execute this codebase as __main__.

app.py            Flask() instance, ProxyFix, true global config (CONFIG_FILE,
                  DB_FILE, HOST, INIT, port config, per-request port tagging),
                  register_blueprints(app), make_tagged_app/run_server.
                  Re-exports ~100 names moved into system/*.py so the existing
                  test suite (`import app as appmod; appmod.X(...)`) keeps
                  working unchanged — see "Why app.py re-exports everything"
                  below.

system/           Business logic. No Flask imports — every function here is
                  plain Python, testable without a request context.

routes/           Flask Blueprints — thin HTTP glue. Parse the request, call
                  into system/*.py, shape the JSON response. No business logic.
```

Two-layer shape: `system/` holds logic, `routes/` holds HTTP glue — same
pattern used elsewhere (see `fugginnas`).

## Request flow

```
systemd  →  run.py  →  app.py (Flask instance)  →  routes/*.py (Blueprint)
                                                          │
                                                          ▼
                                                    system/*.py (logic)
                                                          │
                                                          ▼
                                              system/init_adapter.py's HOST
                                              (subprocess / file IO seam)
```

## `system/` — business logic (no Flask imports)

| Module | Responsibility | Key names |
|---|---|---|
| `init_adapter.py` | Host + init-system adapters — the seam to the OS | `Result`, `LinuxHost`, `InitAdapter`, `SystemdAdapter`, `OpenRCAdapter`, `NullAdapter`, `detect_init` |
| `auth.py` | Read-only vs admin port enforcement | `is_readonly`, `admin_required` |
| `db.py` | Uptime ledger + SQLite history (service_events, metrics) | `fold_uptime`, `_query_events`, `init_db`, `record_service_event`, `record_metrics`, `get_uptime_bars`, `get_service_uptime_pct`, `get_metrics_history`, `get_service_uptime_str`, `get_docker_uptime_str` |
| `config_io.py` | Feeder config table + per-format read/write adapters | `FEEDER_CONFIGS`, `read_flat_ini`/`write_flat_ini`, `read_shell_vars`/`write_shell_vars`, `read_piaware_config`, `read_docker_env`, `CONFIG_ADAPTERS`, `get_feeder_settings`, `set_feeder_settings`, `load_config`, `get_config_map`, `save_feeders` |
| `services.py` | Thin status/action wrappers over `INIT`/docker | `systemd_status`, `docker_status`, `service_action` |
| `feeders.py` | Last-seen + feeder health composition | `get_feeder_last_seen`, `get_fr24_last_seen`, `get_piaware_last_seen`, `FeederHealth`, `feeder_status`, `probe`, `readsb_metrics` |
| `sdr.py` | Airspy/receiver/per-band SDR settings + presence guard | `parse_airspy_options`/`write_airspy_options`, `parse_receiver_options`/`write_receiver_options`, `parse_sdr1090`/`write_sdr1090`, `parse_sdr978`/`write_sdr978`, `enforce_sdr_presence`, `detect_airspy_model`, `airspy_live_hint` |
| `stats.py` | Airspy signal analysis, system metrics, readsb/band stats | `get_airspy_stats`, `gain_recommendation`, `get_system_metrics`, `get_readsb_deep_stats`, `get_band_stats` |
| `versions.py` | Installed-vs-latest version checking, self-contained cache | `VERSION_SOURCES`, `refresh_versions`, `get_versions` |
| `logstream.py` | SSE log-follow framing (journalctl / docker logs) | `_sse`, `_journalctl`, `_log_command`, `stream_logs` |
| `poll.py` | Background poller — records status + metrics every 30s | `background_poll` |

## `routes/` — Flask Blueprints (thin HTTP glue)

`routes/__init__.py:register_blueprints(app)` registers all seven; `app.py`
calls it once at import time. `GET /` (serves `static/index.html`) stays in
`app.py` since it isn't part of the JSON API.

| Blueprint | Routes |
|---|---|
| `dashboard.py` | `GET /api/alerts`, `/api/mode`, `/api/ui-config`, `/api/status` |
| `stats.py` | `GET /api/stats/airspy`, `/readsb`, `/bands`, `/aircraft_types`, `/system`, `/history`, `/uptime/history`, `/uptime/<service>` |
| `versions.py` | `POST /api/versions/refresh` |
| `settings.py` | `GET`+`POST /api/settings/airspy`, `/receiver`, `/sdr/<band>`, `/feeders`, `/feeder/<key>` |
| `services.py` | `POST /api/service/<name>/restart` |
| `logs.py` | `GET /api/logs/<key>`, `/api/syslog` |
| `backup.py` | `GET /api/backup`, `/backup/graphs`; `POST /api/restore`, `/restore/graphs` |

Read routes work on both ports; write routes carry `@admin_required` and
403 on the read-only port (see **Two ports** in [README.md](README.md)).

## Why `app.py` re-exports everything

The existing test suite (119 tests, unchanged by this refactor) does
`import app as appmod` and calls `appmod.get_feeder_last_seen(...)`,
`appmod.Result`, etc. — one file, one flat namespace. Rather than touch
15+ test files, `app.py` re-imports every moved name from `system/*.py`
(`from system.db import fold_uptime, ...`), so `appmod.X` still resolves
for anything tests reference. `ruff.toml` carries a scoped
`per-file-ignores` F401 exemption for `app.py` for exactly this reason —
those "unused" imports are the point.

**The one sharp edge:** `HOST`, `INIT`, and `DB_FILE` are monkeypatched by
*reassignment* in `tests/conftest.py` (`appmod.HOST = fake`). They're
defined in `app.py` and stay there — never moved into `system/init_adapter.py`
or `system/db.py` — because a plain `from system.x import HOST` would give
each importing module its own independent binding that the monkeypatch
can't reach. Instead, every `system/*.py`/`routes/*.py` module that needs
them does `import app` (the module, not the names) and dereferences
`app.HOST` / `app.INIT` / `app.DB_FILE` at the call site, so a test
reassigning `appmod.HOST` is immediately visible everywhere.

## Why `run.py` exists instead of `python app.py`

`system/*.py` modules do `import app` (see above), which requires `app.py`
to always be imported as the module `app` — never executed directly as
`__main__`. Running `app.py` directly makes Python execute it twice, once
as `__main__` and again as `app` when those imports fire mid-script,
colliding with itself (`ImportError: cannot import name 'X' from partially
initialized module`). `run.py` is the only file systemd/CLI should ever
invoke: it does a normal `import app`, then calls `app.init_db()`,
`app.background_poll`, `app.refresh_versions`, and `app.run_server(port)`.
`app.py` itself has no `if __name__ == '__main__':` block.

## Two-port tagging

`app.py` defines `_request_port` (a `threading.local()`), mutated per
request by `make_tagged_app(port)` (a WSGI wrapper), and reads it back via
`system/auth.py:is_readonly()`. Two `ThreadedWSGIServer`s run the same
Flask app — one per port — so a single process serves both the full-access
admin port and the read-only port.

## Deployment

`install.sh`/`update.sh` (monitor-only) and `installer/install-stack.sh`/
`installer/update-stack.sh` (full SDR stack) all copy `app.py`, `run.py`,
`system/`, and `routes/` into `/opt/adsb-monitor`, and point the generated
systemd unit's `ExecStart` at `run.py`. All `.sh` scripts in this repo are
tracked as executable in git (`100755`) — no manual `chmod +x` needed after
a fresh clone.

## Testing

`tests/` is unchanged by this refactor (Option A in
[route-module-split.md](route-module-split.md) §5): every test still does
`import app as appmod` and exercises functions through that flat namespace,
using `tests/fakes.py`'s `FakeHost` swapped in via the `fake_host`/`fake_init`
fixtures in `conftest.py`. Run with:

```bash
pytest -q       # 119 tests
ruff check .    # lint
```

There is currently no test that subprocesses `run.py` itself (i.e. nothing
catches an entry-point-level regression like the `__main__`/`import app`
circular import bug found during the first real deploy of this refactor —
see route-module-split.md §10). Worth adding if this class of bug recurs.
