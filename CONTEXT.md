# ADS-B Stack Monitor — Domain & Architecture Glossary

Shared language for this codebase. Domain terms describe *what the app monitors*;
architecture terms describe *how the code is shaped* (seams, adapters, modules).
For the physical module/route layout (which file each of these lives in), see
[ARCHITECTURE.md](ARCHITECTURE.md).

## Domain

- **Feeder** — one monitored unit declared in `feeders.ini`. Either a `service:`
  (systemd unit) or a `docker:` (container). The app's core job is reporting each
  Feeder's health (alive, last-seen, uptime).
- **Feeder kind** — `service` or `docker`; selects how status/logs/config are read.
- **Uptime** — fraction of a time window a Feeder was `ok`, folded from recorded
  state-change events in SQLite.
- **Gain recommendation** — SNR/noise/RSSI thresholds → a suggested ±dB airspy gain
  change.
- **Band** — a receive frequency the station decodes: **1090** MHz ADS-B (readsb) or
  **978** MHz UAT (dump978-fa). `get_band_stats()` returns one block per present band
  for the sidebar; presence is detected from config, not hardcoded.
- **Dual-band** — running both bands at once on two RTL-SDRs, each pinned by USB
  serial. dump978-fa's output is merged into readsb via its `uat_in` net-connector so
  one tar1090 map shows both. 978 exposes RSSI + aircraft only (no SNR/noise floor).
- **Bias-tee** — DC fed up the coax to power an inline LNA. Per-SDR toggle, off by
  default, confirmed on enable: 1090 via an `rtl_biast` `ExecStartPre`, 978 via
  SoapySDR's `biastee=true`.
- **Feeder health** — the live-host answer to "what is this Feeder doing right now":
  `FeederHealth{status, detail, last_seen, running_for}`. Distinct from historical
  **Uptime** (a SQLite aggregation). Produced by the **Feeder probe**.

## Architecture (cont.)

- **Feeder probe** — the deep module that answers Feeder health. `probe(feeder) →
  FeederHealth` composes a cheap `feeder_status(feeder) → (status, detail)` (the single
  kind-dispatch over systemd/docker, used directly by the background poller and alerts)
  with the `last_seen` and `running_for` resolvers. Resolver selection is implicit inside
  the probe (keyed off the Feeder's key/kind); callers never branch on Feeder kind. Sits
  on the **Host** seam, so it is fully testable via `FakeHost`.

## Architecture

- **Host** — the seam between the `system/*.py` business logic and the Linux host it runs on.
  A thin interface — `run(cmd, timeout) → Result`, `read_text(path)`, `read_json(path)`,
  `write_text(path, data)` — with two adapters: `LinuxHost` (real `subprocess`/`open`)
  in production and `FakeHost` (canned commands/files, records calls) in tests. The Host
  **never raises**: failures degrade to `Result{ok: False}` or `None`, matching the app's
  monitor-everything-degrades-gracefully behavior. Injected as a swappable module-level
  singleton `HOST`. Out of scope: log streaming (`Popen`) and HTTP version fetch.

- **Config store** — the deep module behind Feeder settings. `_config_adapter(cfg) →
  Adapter(read, write)` selects a format adapter (`ini_flat`, `shell_vars`, `piaware`,
  `docker`) **once**, used by both `get_feeder_settings` and `set_feeder_settings` — no
  more twin format-dispatch ladders. Adapters own only the format-specific raw read/write
  (delegating to the low-level `read_flat_ini`/`read_docker_env`/… functions on the **Host**
  seam); the format-independent tails (extra-files overlay, field projection, writable
  filtering) stay in the orchestration. A `None` adapter means read-only-via-extra-files.

- **Uptime ledger** — the SQLite history of Feeder state-change events, plus the queries
  over it. The historical **Uptime** math is one pure function, `fold_uptime(rows, start,
  end) → pct`, shared by the daily-bars view, the uptime-history route, and the aggregate
  `get_service_uptime_pct` (which keeps a `None`-on-empty guard). `_query_events(service,
  start, end)` centralises the `service_events` SELECT. The write side (`record_service_event`,
  `record_metrics`, pruning) is separate. The pure fold is unit-testable with fixture
  row-lists — no database.
