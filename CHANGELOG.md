# Changelog

All notable changes to this project are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

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
