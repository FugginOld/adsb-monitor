# Credits

ADS-B Stack Monitor is a thin dashboard layer on top of a stack of existing open-source tools and community feeder networks.

---

## Core stack — wiedehopf

The core of the receiver station are three tools by **wiedehopf**:

| Tool | Purpose | Link |
| --- | --- | --- |
| **readsb** | ADS-B / Mode S decoder — decodes raw SDR output into aircraft data | https://github.com/wiedehopf/readsb |
| **tar1090** | Live aircraft map, served locally | https://github.com/wiedehopf/tar1090 |
| **graphs1090** | Performance and signal graphs over time | https://github.com/wiedehopf/graphs1090 |

The installer also uses:
- **adsb-scripts** — readsb install/config scripts: https://github.com/wiedehopf/adsb-scripts
- **airspy-conf** — airspy_adsb configuration helper: https://github.com/wiedehopf/airspy-conf

---

## 978 MHz UAT (dual-band)

When two RTL-SDRs are present, the installer adds 978 MHz UAT decoding alongside
1090 MHz ADS-B and merges both onto one map.

| Tool | Purpose | Link |
| --- | --- | --- |
| **dump978-fa** | FlightAware's 978 MHz UAT decoder (built from source) | https://github.com/flightaware/dump978 |
| **skyaware978** | Companion to dump978-fa; writes the UAT `aircraft.json` the dashboard and graphs1090 read | https://github.com/flightaware/dump978 |

dump978-fa's 978 output is fed into readsb via its `uat_in` net-connector, so
tar1090 shows 1090 + 978 traffic on a single map.

---

## SDR hardware and decoders

| Device | Decoder | Notes |
| --- | --- | --- |
| **Airspy Mini / R2** | airspy_adsb | Best sensitivity; 6 or 12 MSPS |
| **RTL-SDR Blog V4** | readsb | Widely available, solid performance |
| **FlightAware Pro Stick / Plus** | readsb | Plus adds SAW filter and LNA |
| **Nooelec NESDR SMArt / SMArTee** | readsb | SMArTee has built-in bias-T |
| **SDRplay RSP1A / RSPdx** | SDRplay API + dump1090 | Requires SDRplay proprietary API |

- **Airspy**: https://airspy.com
- **RTL-SDR Blog**: https://www.rtl-sdr.com
- **FlightAware**: https://flightaware.com/adsb/prostick
- **Nooelec**: https://www.nooelec.com
- **SDRplay**: https://www.sdrplay.com

---

## Build & SDR tooling

Used by the installer to build dump978-fa, address dongles by serial, and drive
bias-tee — most are pulled in automatically on dual-band installs.

| Tool / library | Purpose | Link |
| --- | --- | --- |
| **rtl-sdr** (`rtl_test`, `rtl_eeprom`, `rtl_biast`) | Probe dongles, assign serials, toggle bias-tee | https://github.com/osmocom/rtl-sdr |
| **SoapySDR** + **soapysdr-module-rtlsdr** | SDR abstraction dump978-fa uses to open the 978 dongle | https://github.com/pothosware/SoapySDR |
| **Boost** (program-options, regex, filesystem) | C++ libraries dump978-fa builds against | https://www.boost.org |
| **collectd** | Samples readsb/skyaware978 stats into RRD time series | https://www.collectd.org |
| **rrdtool** | Round-robin database + graph rendering behind graphs1090 | https://oss.oetiker.ch/rrdtool |

---

## Feeder networks

The installer can set up accounts with any of these aggregator networks. Each receives your decoded ADS-B data and provides coverage maps, statistics, or account perks in return.

| Network | Link |
| --- | --- |
| **FlightAware** (PiAware) | https://flightaware.com |
| **Flightradar24** | https://www.flightradar24.com |
| **ADSBExchange** | https://www.adsbexchange.com |
| **adsb.fi** | https://adsb.fi |
| **adsb.lol** | https://adsb.lol |
| **AirNav RasPi24** | https://www.airnavsystems.com |
| **Plane Finder** | https://planefinder.net |
| **OpenSky Network** | https://opensky-network.org |
| **ADSBHub** | https://www.adsbhub.org |
| **Plane.watch** | https://plane.watch |
| **TheAirTraffic** | https://theairtraffic.com |

---

## Application runtime

| Component | Purpose | Link |
| --- | --- | --- |
| **Python 3** | Application runtime | https://www.python.org |
| **Flask** | Lightweight web framework powering the API and SSE log streaming | https://flask.palletsprojects.com |
| **SQLite** | Embedded database for uptime history and metrics (stdlib via `sqlite3`) | https://www.sqlite.org |
| **whiptail / newt** | TUI installer dialogs | https://sourceforge.net/projects/newt |

---

## Community installer scripts

The PiAware install script used in the installer is maintained by the community:
- **abcd567a** — piaware-ubuntu-debian-amd64: https://github.com/abcd567a/piaware-ubuntu-debian-amd64

---

## License

This project is open source. See [LICENSE](LICENSE) if present, or the repository for terms.
All credited projects retain their own licenses and trademarks.
