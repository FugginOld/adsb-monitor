# ADS-B Stack Monitor — Domain & Architecture Glossary

Shared language for this codebase. Domain terms describe *what the app monitors*;
architecture terms describe *how the code is shaped* (seams, adapters, modules).

## Domain

- **Feeder** — one monitored unit declared in `feeders.ini`. Either a `service:`
  (systemd unit) or a `docker:` (container). The app's core job is reporting each
  Feeder's health (alive, last-seen, uptime).
- **Feeder kind** — `service` or `docker`; selects how status/logs/config are read.
- **Uptime** — fraction of a time window a Feeder was `ok`, folded from recorded
  state-change events in SQLite.
- **Gain recommendation** — SNR/noise/RSSI thresholds → a suggested ±dB airspy gain
  change.

## Architecture

- **Host** — the seam between `app.py`'s business logic and the Linux host it runs on.
  A thin interface — `run(cmd, timeout) → Result`, `read_text(path)`, `read_json(path)`,
  `write_text(path, data)` — with two adapters: `LinuxHost` (real `subprocess`/`open`)
  in production and `FakeHost` (canned commands/files, records calls) in tests. The Host
  **never raises**: failures degrade to `Result{ok: False}` or `None`, matching the app's
  monitor-everything-degrades-gracefully behavior. Injected as a swappable module-level
  singleton `HOST`. Out of scope: log streaming (`Popen`) and HTTP version fetch.
