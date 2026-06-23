# HOWTO — Install and Use ADS-B Stack Monitor

---

## Prerequisites

- A Raspberry Pi, SBC, or x86 Linux box running a supported distro (see [README](README.md))
- An SDR plugged in (Airspy, RTL-SDR, SDRplay, or FlightAware Pro Stick)
- `sudo` access
- Internet connection for the installer

---

## 1. Install the full stack

The installer is a TUI (terminal UI) wizard. It auto-detects your SDR, asks for your location, lets you pick feeders, and installs everything.

```bash
git clone https://github.com/Fuggin/adsb-monitor.git
cd adsb-monitor/installer
chmod +x *.sh
sudo ./install-stack.sh
```

### What the installer does

1. **Detects your SDR** via USB ID and sets recommended gain / sample rate
2. Installs **readsb** (decoder) + **tar1090** (live map)
3. Installs **graphs1090** (performance graphs, dark mode)
4. Installs each **feeder** you select (see list below)
5. Sets your **lat / lon / altitude** across all services
6. Installs **adsb-monitor** to `/opt/adsb-monitor` with a matching `feeders.ini`

If no SDR is detected you can pick the type manually or retry the scan.

### Feeders available

| Feeder | Notes |
|---|---|
| FlightAware (PiAware) | Claim at flightaware.com/adsb/stats |
| Flightradar24 | Run `sudo fr24feed --signup` after install |
| ADSBExchange | Auto-generates UUID; claim at adsbexchange.com |
| adsb.fi | One-line install, no account needed |
| adsb.lol | One-line install, no account needed |
| AirNav (RasPi24) | Requires Docker; enter sharing key in Settings |
| Plane Finder | Configure at `http://<pi>:30053` |
| OpenSky | Manual feeder — see opensky-network.org/feed |
| ADSBHub | Manual client — see adsbhub.org/howtofeed.php |
| Plane.watch | Docker-based — see sdr-enthusiasts.gitbook.io |
| TheAirTraffic | One-line install |

After install, open `http://<pi-ip>:5000` → **Settings** to enter sharing keys that couldn't be automated.

---

## 2. Access the dashboard

| URL | Access |
|---|---|
| `http://<pi-ip>:5000` | Admin — full access (settings, restarts, backup) |
| `http://<pi-ip>:5001` | Read-only — status + stats only |

Use the read-only port (5001) when exposing the dashboard via a reverse proxy (nginx, Caddy, NPM) so viewers can't modify settings.

### Change ports

```bash
# /etc/systemd/system/adsb-monitor.service  (or equivalent)
Environment="ADMIN_PORT=8080"
Environment="READONLY_PORT=8081"
```

---

## 3. Dashboard overview

### Status panel

Each feeder card shows:
- **Active / Down** badge (green / red)
- **Last seen** — time since the feeder last produced output
- **Running for** — how long the service has been up
- **7-day uptime bar** — green/red squares, one per day

### Signal panel (Airspy only)

Shows live SNR, noise floor, RSSI, and a gain recommendation. If the monitor suggests lowering or raising gain, apply it in **Settings → Airspy**.

### readsb stats

Aircraft count, message rate, max range, strong-signal %, and per-type aircraft breakdown.

### Version panel

Current vs latest version for each stack component. Click **Refresh** to re-check.

### Alerts

Any service currently down appears in the Alerts section at the top.

---

## 4. Settings

Open `http://<pi-ip>:5000` (admin port) and click **Settings**.

### Airspy settings

| Field | Notes |
|---|---|
| Gain (0–21) | 21 = max; reduce if near a busy airport |
| Sample rate | 6 MSPS (Mini) or 12 MSPS (R2) |
| Options | Extra airspy_adsb flags |

Changes restart `airspy_adsb` automatically.

### Receiver settings

| Field | Notes |
|---|---|
| Latitude / Longitude | Decimal degrees |
| Max range (nm) | Default 500 |

Changes restart `readsb` automatically.

### Feeder settings

Each feeder has its own settings card with fields specific to that feeder (sharing key, UUID, host/port, etc.). Changes are written to the feeder's config file and the service is restarted if the feeder supports it.

---

## 5. Logs

Click the **Logs** button on any feeder card to open a live log stream. The monitor tails the systemd journal (for service feeders) or Docker log (for container feeders) and streams it to your browser.

---

## 6. Backup

`http://<pi-ip>:5000/api/backup` (or the Backup button in the UI) downloads a `.tar.gz` containing:

- `feeders.ini` — feeder declarations
- `history.db` — uptime + metrics SQLite database
- Each feeder's config file

Keep this backup before running `update-stack.sh` or `uninstall.sh`.

---

## 7. Update

```bash
cd adsb-monitor/installer
sudo ./update-stack.sh
```

Updates all wiedehopf components (readsb, tar1090, graphs1090) and redeploys the monitor from the current directory.

---

## 8. Uninstall

```bash
cd adsb-monitor/installer
sudo ./uninstall.sh
```

TUI checklist — select what to remove. Confirms before deleting anything. System dependencies (python3, sqlite3, etc.) are left in place.

---

## 9. Manual / headless install (no TUI)

If you already have readsb and feeders running and just want the monitor:

```bash
sudo mkdir -p /opt/adsb-monitor/static
sudo cp app.py /opt/adsb-monitor/
sudo cp static/index.html /opt/adsb-monitor/static/
```

Create `/opt/adsb-monitor/feeders.ini` manually:

```ini
# ADS-B Stack Monitor - Feeder Configuration
# Section types: [service:<unit>] or [docker:<container>]

[service:readsb]
label = readsb
icon = radio

[service:piaware]
label = FlightAware
icon = plane

[docker:adsbexchange]
label = ADSBExchange
icon = globe
```

Install Flask and start the app:

```bash
pip install flask
cd /opt/adsb-monitor
python app.py
```

Or create a systemd unit:

```ini
# /etc/systemd/system/adsb-monitor.service
[Unit]
Description=ADS-B Stack Monitor
After=network.target

[Service]
ExecStart=/usr/bin/python3 /opt/adsb-monitor/app.py
WorkingDirectory=/opt/adsb-monitor
Restart=always
Environment="ADMIN_PORT=5000"
Environment="READONLY_PORT=5001"

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now adsb-monitor
```

---

## 10. Troubleshooting

### Dashboard shows all feeders as Down

Check that the service names in `feeders.ini` match actual systemd unit names:

```bash
systemctl list-units --type=service | grep -E 'readsb|piaware|fr24'
```

### Service control unavailable

The monitor logs `service control unavailable — init system not detected` when neither `systemctl` nor `rc-service` is found. The dashboard still works for status polling if the Host has access to the feeder's status files.

### Settings changes not saving

Settings writes require the admin port (5000). Requests on port 5001 are read-only and will return `403`.

### Airspy stats missing

The monitor reads `/run/airspy_adsb/stats.json`. Confirm airspy_adsb is running and `STATS=-S /run/airspy_adsb/stats.json` is in `/etc/default/airspy_adsb`.

### Force install on unsupported distro

```bash
ADSB_FORCE=1 sudo ./install-stack.sh
```
