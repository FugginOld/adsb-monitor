# ADS-B Full Stack Installer

A TUI installer suite for a complete ADS-B receiving/feeding station. Detects
your architecture, package manager, and init system at runtime, so it runs on
most Raspberry Pis, SBCs, and major Linux distros — not just RPi 5 / Debian.

## Supported Platforms

| Platform | Arch | Distro | Init | Status |
|---|---|---|---|---|
| Raspberry Pi 5 | ARM64 | RPi OS / Armbian | systemd | supported |
| Raspberry Pi 4 | ARM64 / ARMhf | RPi OS / Armbian | systemd | supported |
| Raspberry Pi 3 / Zero 2 W | ARMhf | RPi OS Lite | systemd | supported |
| Orange Pi 5 / Rock Pi | ARM64 | Armbian (Rockchip) | systemd | supported |
| Orange Pi / Banana Pi | ARMhf / ARM64 | Armbian (Allwinner) | systemd | supported |
| Beelink / generic x86 mini-PC | x86_64 | Ubuntu / Debian | systemd | supported |
| Generic x86 server | x86_64 | Fedora / Arch | systemd | experimental |
| Generic SBC (Alpine-based) | ARM64 / ARMhf | Alpine Linux | OpenRC | experimental |

The installer reads `/etc/os-release` and warns before continuing on an
untested distro. **Experimental** means the install logic handles it (package
manager + init system are auto-detected) but it has had less real-world testing.

## Scripts

| Script | Purpose |
|--------|---------|
| `install-stack.sh` | Full install: detects SDR, installs decoder + stack + feeders + monitor |
| `update-stack.sh` | Update wiedehopf components + re-deploy monitor |
| `uninstall.sh` | Remove monitor and/or selected stack components |
| `detect-sdr.sh` | SDR detection helper (sourced by installer; can run standalone) |
| `generate-feeders-ini.sh` | Builds monitor's feeders.ini from selections |

## Install

```bash
cd installer
chmod +x *.sh
sudo ./install-stack.sh
```

### What it does

1. **Detects your SDR** via USB ID and sets recommended defaults:
   - **Airspy Mini** → airspy_adsb, 6 MSPS, gain 21
   - **Airspy R2** → airspy_adsb, 12 MSPS, gain 21
   - **RTL-SDR Blog V4** → readsb direct, gain auto
   - **Generic RTL2832U + R820T2** → readsb direct, gain auto
   - **FlightAware Pro Stick / Plus** → readsb direct (Plus has SAW filter+amp)
   - **Nooelec NESDR SMArt / SMArTee** → readsb direct
   - **SDRplay RSP1A / RSPdx** → SDRplay API + dump1090 (partly manual)
2. Installs readsb + tar1090, the SDR decoder, graphs1090
3. Installs your selected feeders
4. Installs adsb-monitor with a matching feeders.ini

If no SDR is detected, you can pick the type manually or retry the scan.

## Update

```bash
sudo ./update-stack.sh         # TUI picker
sudo ./update-stack.sh --all   # update everything non-interactively
```

Updates airspy_adsb, readsb+tar1090, graphs1090 from upstream, and re-deploys
the monitor from this folder. **Preserves** your `feeders.ini` and `history.db`.
Prints current versions when done.

## Uninstall

```bash
sudo ./uninstall.sh
```

TUI checklist to remove the monitor and/or any stack components. System
dependency packages (python3, sqlite3, etc.) are left in place. Confirms
before removing anything.

## After install — feeders needing manual steps

| Feeder | Action |
|--------|--------|
| FlightAware | Claim at flightaware.com/adsb/stats |
| FR24 | `sudo fr24feed --signup` |
| ADSBExchange | Auto-generates UUID; claim at adsbexchange.com |
| AirNav | Enter sharing key in monitor Settings |
| Plane Finder | Configure at http://&lt;pi&gt;:30053 |
| OpenSky | Custom feeder — see opensky-network.org/feed |
| ADSBHub | Custom client — see adsbhub.org/howtofeed.php |
| Plane.watch | Docker-based — see sdr-enthusiasts.gitbook.io |

Open the monitor at `http://<pi-ip>:5000` → **Settings** to enter sharing keys.

## Access points after install

- Monitor (admin): `http://<pi-ip>:5000`
- Monitor (read-only): `http://<pi-ip>:5001`
- Live map: `http://<pi-ip>:8504`
- Graphs: `http://<pi-ip>/graphs1090`

## SDR detection standalone

```bash
./detect-sdr.sh
```

Outputs the detected SDR type, model, decoder, and recommended settings —
useful for debugging detection before a full install.

## Platform Notes

Per-arch feeder and decoder limitations the installer handles by warning and
skipping rather than failing:

| Component | arm64 | armhf | amd64 | Note |
|---|---|---|---|---|
| `airspy_adsb` | ✅ | ✅ | ✅ | Upstream ships all three |
| `readsb` / `tar1090` | ✅ | ✅ | ✅ | Built from source |
| pfclient (Plane Finder) | ✅ | ❌ | ❌ | arm64-only `.deb`; skipped with a warning elsewhere |
| FR24 feed | ✅ | ✅ | ✅ | `.deb` URL may drift — see Notes below |
| SDRplay API | ✅ | ✅ | ✅ | Partly manual on all arches |

- **Init system:** systemd hosts get `systemctl enable/start`; OpenRC (Alpine)
  hosts get the `rc-update add` / `rc-service start` equivalents. Unknown init
  systems disable service control in the monitor with a clear API error.
- **Package manager:** auto-detected (`apt`/`dnf`/`yum`/`pacman`/`zypper`/`apk`).
  Logical package names are mapped per distro (e.g. `whiptail`→`newt` on
  dnf/apk). If `whiptail` is unavailable the TUI falls back to `dialog`, then
  plain prompts.
- **CPU temperature:** read from the first available kernel sensor across RPi,
  Intel, AMD, Rockchip, and Allwinner keys; absent on some kernels/permissions.

## Notes

- The read-only port (5001) hides Settings — use it for public/NPM exposure.
- Feeder install URLs (FR24 .deb, community PiAware build) may drift over time;
  those are the most likely thing needing a version bump later.
- ADSBExchange-codebase feeders (adsb.fi, adsb.lol, TheAirTraffic) install via
  their own one-line scripts and connect to readsb on :30005.
