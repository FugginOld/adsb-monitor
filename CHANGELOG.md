# Changelog

All notable changes to this project are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

## [Unreleased]

### Added
- Airspy gain auto-tune (off by default; Settings → Airspy SDR). Measures the share of
  DF17 signals near clipping from airspy_adsb's RSSI histogram over a ~15 min window and
  steps a fixed gain to keep it between 0.5 % and 5 %. Verifies each change and rolls back
  and disables itself if airspy_adsb doesn't come back at the new gain. Changes are listed
  in the UI and stored in `history.db` (`gain_changes`). Config: `autotune.json`.
- `GET`/`POST /api/autotune`.

### Fixed
- Gain set from the UI (or via "Apply recommended gain") was silently ignored: the writer
  never set `G=-g`, so airspy_adsb ran auto gain. Now `G=-g` is always written and
  `GAIN=auto` maps to `-g auto`.
- Sample rate: the writer could produce `-m 6`, which makes airspy_adsb 2.x exit
  (`-m` is the R2-only MLAT frequency, 12/20/24). `-m` is now only written for an R2
  with a valid value; the 3/6 MSPS "Mini" choices are gone.
- Airspy settings and sidebar hint showed the config file's raw GAIN/SAMPLE_RATE rather
  than what the decoder uses; they now show the effective values and the live
  gain/sample rate from stats.json.
- Gain recommendation offered "try gain 0"-style numbers when gain is auto.
- Airspy settings POST validates gain (0–21/auto), rate and options (no quotes) and
  returns 400 instead of writing a config that crash-loops the decoder.

## [1.0.0] - 2026-07-01

Initial release.

- Live status dashboard: active/down badge per feeder and service, with last-seen time
- 7-day uptime history (daily bars + aggregate %), backed by SQLite
- Dual-band support (1090 MHz ADS-B + 978 MHz UAT) merged onto one map
- Per-band signal stats (RSSI/SNR/noise) and readsb deep stats (aircraft count, message rate, range)
- Upstream version checking for readsb, tar1090, graphs1090, airspy_adsb, dump978-fa
- In-browser settings: per-SDR gain/bias-tee, sharing keys, lat/lon/altitude, receiver options
- Live log streaming (systemd journal / Docker) with a whole-system journal view
- Service restart control (admin port only)
- Backup/restore for config and graph data (collectd RRDs)
- Two-port model: admin (full access) and read-only (status/stats only)
