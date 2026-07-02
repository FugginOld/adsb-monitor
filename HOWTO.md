# HOWTO — Install and Use ADS-B Stack Monitor

---

## Prerequisites

- A Raspberry Pi, SBC, or x86 Linux box running a supported distro (see [README](README.md))
- An SDR plugged in (Airspy, RTL-SDR, SDRplay, or FlightAware Pro Stick)
- For **dual-band** (1090 + 978 UAT): two RTL-SDR dongles, each with a unique serial (see §1a)
- `sudo` access
- Internet connection for the installer

---

## 1. Install the full stack

The installer is a TUI (terminal UI) wizard. It auto-detects your SDR, asks for your location, lets you pick feeders, and installs everything.

```bash
git clone https://github.com/FugginOld/adsb-monitor.git
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
| --- | --- |
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

## 1a. Dual-band — add 978 MHz UAT (optional)

With **two RTL-SDR dongles** the installer can decode 1090 MHz ADS-B *and* 978 MHz
UAT (US-only band), merged onto one map. The installer offers this automatically
when it sees two RTL-SDRs.

### Step 1 — give each dongle a unique serial (one-time)

Two identical dongles look the same to software until each has its own serial:

```bash
sudo apt-get install -y rtl-sdr
rtl_test -t                       # shows each dongle + serial
sudo rtl_eeprom -d 0 -s 00001090  # 1090 stick
sudo rtl_eeprom -d 1 -s 00000978  # 978 stick
# unplug and replug BOTH so the kernel re-reads the serials
rtl_test -t                       # confirm: SN 00001090 and 00000978
```

The installer assigns the **978**-serial stick to UAT and the other to 1090.

### Step 2 — run the installer and accept dual-band

Run `install-stack.sh` as normal; at **"Two RTL-SDRs Detected"** choose **Yes**.
It builds `dump978-fa`, installs `skyaware978`, and wires 978 into readsb so
tar1090 shows both bands. A `dump978 (978 UAT)` card appears under core services.

### Step 3 — verify

```bash
systemctl is-active readsb dump978-fa     # both: active
ls /run/skyaware978/aircraft.json          # 978 data present
journalctl -u dump978-fa -n 20 --no-pager  # decoding, no "cb transfer status: 5"
```

978 traffic is sparse (US general aviation only) — empty for a while is normal.

> **Containers (Proxmox LXC):** RTL-SDR passthrough needs host-side setup —
> blacklist `dvb_usb_rtl28xxu`, pass `/dev/bus/usb` into the container, and
> disable USB autosuspend (`usbcore.autosuspend=-1`). See
> [installer/README.md](installer/README.md#dual-band--1090--978-uat) for the
> exact commands. The installer pins the SDR services to `root` automatically
> since containers lack `udev`.

---

## 2. Access the dashboard

| URL | Access |
| --- | --- |
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

### Signal panels (per band)

The sidebar shows a signal block only for the bands you actually have:

- **Airspy** — live SNR / noise / RSSI + a gain recommendation (only when an Airspy is installed; apply suggestions in **Settings → Airspy**).
- **1090 MHz ADS-B** — RSSI, SNR, noise floor, plus aircraft count and msg/s (from readsb; shown when readsb drives an RTL dongle).
- **978 MHz UAT** — RSSI, aircraft count, and msg/s (from skyaware978). dump978 doesn't expose SNR/noise, so those are omitted.

Blocks for absent hardware are hidden — an Airspy-only box shows no 1090/978 RTL blocks, and a single-RTL box shows no 978 block.

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
| --- | --- |
| Gain (0–21) | 21 = max; reduce if near a busy airport |
| Sample rate | 6 MSPS (Mini) or 12 MSPS (R2) |
| Options | Extra airspy_adsb flags |

Changes restart `airspy_adsb` automatically.

### Receiver settings

| Field | Notes |
| --- | --- |
| Latitude / Longitude | Decimal degrees |
| Max range (nm) | Default 500 |

Changes restart `readsb` automatically.

### 1090 / 978 SDR settings (RTL-SDR)

On RTL-SDR setups, each band gets its own card (shown only when that SDR exists):

| Field | Notes |
| --- | --- |
| **Gain** | Dropdown of the 29 supported R820T steps. 1090 also offers `auto` (readsb adaptive); 978 offers `max` (full gain, the default) |
| **Bias-tee** | Off by default. Feeds ~4.5 V up the coax to power an inline LNA — **confirmation dialog on enable**. Only turn on if you have an LNA/filter that expects it; DC into gear that doesn't can damage hardware |

Saving the 1090 card restarts `readsb`; the 978 card restarts `dump978-fa`.
1090 bias-tee is applied via `rtl_biast` on each readsb start; 978 bias-tee uses
SoapySDR's `biastee=true`.

### Feeder settings

Each feeder has its own settings card with fields specific to that feeder (sharing key, UUID, host/port, etc.). Changes are written to the feeder's config file and the service is restarted if the feeder supports it.

---

## 5. Logs

Click the **Logs** button on any feeder card to open a live log stream. The monitor tails the systemd journal (for service feeders) or Docker log (for container feeders) and streams it to your browser. Output is line-buffered, so even quiet units stream immediately; if a unit has no journal or doesn't exist, the stream says so instead of sitting blank.

Each log view has a **Copy** button that copies the visible log buffer to your clipboard.

For a station-wide view, **Settings → System log** shows a live terminal of the whole-system journal (all units at once). It connects when you open Settings and disconnects when you leave. Admin port only — both the per-service Logs button and `/api/logs/<key>` are hidden/blocked on the read-only port.

---

## 6. Backup & restore

**Settings → Service control** has four buttons (admin port only):

| Button | What it does |
| --- | --- |
| **Download config** | Zip of config files: `feeders.ini`, `/etc/default/{readsb,airspy_adsb,dump978-fa,skyaware978}`, the 1090 bias-tee drop-in, feeder configs, and the monitor's unit |
| **Download graph data** | Zip of the collectd RRD history (`/var/lib/collectd/rrd`) — the actual graph data behind graphs1090 |
| **Import config** | Upload a config zip to restore. Writes only known filenames back to fixed paths, then restarts readsb/dump978 |
| **Import graph data** | Upload a graph zip to restore. Stops collectd, replaces the RRDs, restarts collectd + graphs1090 |

Equivalent endpoints: `/api/backup`, `/api/backup/graphs`, `/api/restore`, `/api/restore/graphs`.

Download both before running `update-stack.sh` or `uninstall.sh`. RRD graph data
restores cleanly on the **same machine**; cross-architecture restore (ARM ↔ x86)
may not, due to RRD's on-disk format.

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
sudo cp app.py run.py /opt/adsb-monitor/
sudo cp -r system routes /opt/adsb-monitor/
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
pip install flask psutil
cd /opt/adsb-monitor
python run.py
```

Run `run.py`, not `app.py` — `run.py` is the entry point; `app.py` is meant to be imported as a module (`system/*.py` does `import app` internally), and executing it directly will crash with a circular-import error.

Or create a systemd unit:

```ini
# /etc/systemd/system/adsb-monitor.service
[Unit]
Description=ADS-B Stack Monitor
After=network.target

[Service]
ExecStart=/usr/bin/python3 /opt/adsb-monitor/run.py
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

### dump978 keeps restarting

```bash
journalctl -u dump978-fa -n 40 --no-pager
```

- `unable to read device details` / `no device matching '00000978'` — the service can't open the dongle. In a container this means no `udev`; the SDR services must run as `root` (the installer sets this; otherwise add a `User=root` drop-in). Check serials with `rtl_test -t`.
- `bind: Address already in use` on `30978` — a stray dump978 holds the port, or `--raw-port` is set twice. `RECEIVER_OPTIONS` in `/etc/default/dump978-fa` should hold **only** `--sdr driver=rtlsdr,serial=...`; the port lives in `NET_OPTIONS`.
- `cb transfer status: 5` then `TIMEOUT` / `Abnormal exit` — the dongle dropped off USB, almost always **autosuspend** on a low-traffic 978 stick. On the host: `echo on | tee /sys/bus/usb/devices/*/power/control`, and persist with `usbcore.autosuspend=-1` in GRUB. If it persists, suspect power — use a powered USB hub.

### No 978 / UAT graphs in graphs1090

graphs1090 only enables 978 graphs if `/run/skyaware978/aircraft.json` exists **when it installs**. If you added dump978/skyaware978 after graphs1090, re-run its installer so it detects them:

```bash
bash -c "$(wget -qO - https://github.com/wiedehopf/graphs1090/raw/master/install.sh)"
grep URL_978 /etc/collectd/collectd.conf   # should be uncommented
```

### Force install on unsupported distro

```bash
ADSB_FORCE=1 sudo ./install-stack.sh
```
