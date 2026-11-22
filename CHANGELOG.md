# Changelog

All notable changes to this project are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

## [Unreleased]

### Added
- Feeder data-flow alerts: a feeder that is running but has sent no data for 5 minutes
  gets an amber "no data" badge and a "No data:" line in the alert banner. Sources: the
  upload connector of the ADSBExchange / adsb.fi feed clients (`stats.prom`), FR24's
  `monitor.json`, piaware's `status.json`, mlat-client's journal, readsb's decoded
  messages, and Docker health checks. Short reconnects don't alert. Changes are stored
  in `history.db` (`dataflow_events`); `GET /api/dataflow/events` lists them, and
  `/api/alerts` gains `data_alerts` and `any_alert`.
- Airspy gain auto-tune (off by default; Settings → Airspy SDR). Measures the share of
  DF17 signals near clipping from airspy_adsb's RSSI histogram over a ~15 min window and
  steps a fixed gain to keep it between 0.5 % and 5 %. Verifies each change and rolls back
  and disables itself if airspy_adsb doesn't come back at the new gain. Changes are listed
  in the UI and stored in `history.db` (`gain_changes`). Config: `autotune.json`.
- `GET`/`POST /api/autotune`.
- Auto-tune's run state (sample window, last change, last decision, and any change still
  being verified, with the config to roll back to) is saved to `autotune_state.json` and
  restored after a monitor restart, so frequent restarts no longer reset the window or skip
  verifying a change. Samples older than one window, or taken at a different gain, are
  dropped on restore; an unreadable file just starts a fresh window.
- Update readsb, tar1090 and graphs1090 from Settings → Service control. Each runs the
  same wiedehopf installer as `installer/update-stack.sh` (readsb in `binary-only` mode,
  so fr24feed.ini/rbfeeder.ini and `/etc/default/readsb` are left alone) as a transient
  systemd unit `adsb-update-<component>`: one at a time, survives the page closing or the
  monitor restarting, output in the log window, version check re-run when it finishes.
  `GET /api/stack/updates`, `POST /api/stack/update/<component>`,
  `GET /api/stack/update/<component>/logs` (admin port only; needs systemd).
  Every run ends with `systemctl daemon-reload`, since installers replace unit files
  (readsb's `binary-only` copies `readsb.service`) without reloading.
- Auto-tune is visible wherever the gain is: an AUTO-TUNE badge on the signal panel, a
  note in the Airspy card, and "· auto-tune" in the sidebar hint.
- Changing the gain while auto-tune is on asks for confirmation first. The settings API
  refuses with 409 `autotune_enabled` unless `override_autotune` is set; an override turns
  auto-tune off (so it doesn't walk the gain back) and is recorded in the gain history.

### Security
- Content-Security-Policy on HTML pages (`script-src 'self'`, `object-src 'none'`,
  `frame-src` limited to http/https for the map and graphs) plus
  `X-Content-Type-Options: nosniff`. Inline scripts and `on*=` handlers were removed
  to allow it (see Changed).
- Dashboard templates HTML-escape every server-provided string (feeder labels, keys,
  hints and icons from `feeders.ini`, versions, gain-advice reasons, feeder config
  field labels and options) before inserting it.
- Admin port login. Port 5000 now asks for a password and is **locked until one is set**
  with `sudo /opt/adsb-monitor/venv/bin/python /opt/adsb-monitor/run.py set-password` (prompts twice, at least 10 characters, no restart needed).
  The password is stored hashed (Werkzeug) with a random session key in `admin_auth.json`
  (mode 0600; not part of config backups). Login lasts 30 days in an HttpOnly,
  SameSite=Strict cookie; "Log out" in the toolbar ends it; setting a new password logs out
  every session. 5 wrong passwords from one address block it for 15 minutes. The read-only
  port (5001) is unchanged and needs no login. `GET /login`, `GET /api/login/status`,
  `POST /api/login`, `POST /api/logout`.
- Cross-site write guard: every POST/PUT/PATCH/DELETE must carry
  `X-Requested-With: adsb-monitor` and is refused when the browser's `Sec-Fetch-Site`
  is `cross-site` or `same-site`. Before this, any web page opened on the LAN could
  submit a form to the admin port and restart services, start a stack update or upload
  a restore. The dashboard adds the header to its own requests; scripts calling the API
  with curl need `-H 'X-Requested-With: adsb-monitor'`.

### Changed
- Front end split: `static/index.html` keeps the markup; styles moved to
  `static/app.css` and scripts to `static/js/` (`core`, `status`, `settings`,
  `logs`, `init`, and `login.js` for the login page). Inline handlers became
  `data-click` / `data-change` attributes dispatched from `init.js`.
- Minimum Python is now 3.11 (Debian/Raspberry Pi OS bookworm); CI tests 3.11 and 3.13.
  3.9 is end-of-life and no longer available on GitHub's current Ubuntu runners.

### Fixed
- FR24 always showed "live": the `fr24feed-status` parser matched the `0 SYNC` text.
  Last-seen for FR24 and piaware now comes from their status files (see Added).
- Config writes are atomic: every file the monitor writes (`/etc/default/readsb`,
  `/etc/default/airspy_adsb`, `/etc/default/dump978-fa`, feeder configs, `feeders.ini`,
  the bias-tee drop-in, `autotune.json`) goes to a temp file in the same directory, is
  fsynced and renamed over the original. A power cut mid-save can no longer leave a
  truncated config that stops a decoder from starting. Mode and owner are kept, and
  symlinks are written through. Every config save (Settings, auto-tune, config restore)
  also keeps the version it replaced as `<file>.adsb-monitor.bak`. Config restore no
  longer overwrites files in place.
- Tests no longer start background version-refresh threads that could record
  commands on a later test's fake host (an occasional `test_service_route` failure).
- Map blank in the dashboard after a tar1090 update: the browser kept a cached tar1090
  `index.html` whose hashed script/style files no longer exist. `/api/ui-config` now
  returns `tar1090_version`, and the map iframe loads `<tar1090_url>?v=<version>`, so
  each update gets a new URL.
- Max range read about 46 % low: readsb's `r_dst` is already in nautical miles, but it
  was multiplied by 0.539957 as if it were kilometres. Range samples stored in
  `history.db` before this fix are low by that factor (×0.54).
- readsb "latest" version is read from the `stale` branch, the one `readsb-install.sh`
  builds, not `dev`, so the update badge clears after an update.
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
