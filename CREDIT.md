# Credits

ADS-B Stack Monitor is a thin dashboard layer on top of a stack of excellent open-source tools and community feeder networks. This project would not exist without them.

---

## Core stack — wiedehopf

The heart of the receiver station is three tools by **wiedehopf**:

| Tool | Purpose | Link |
| --- | --- | --- |
| **readsb** | ADS-B / Mode S decoder — decodes raw SDR output into aircraft data | https://github.com/wiedehopf/readsb |
| **tar1090** | Live aircraft map, served locally | https://github.com/wiedehopf/tar1090 |
| **graphs1090** | Performance and signal graphs over time | https://github.com/wiedehopf/graphs1090 |

The installer also uses:
- **adsb-scripts** — readsb install/config scripts: https://github.com/wiedehopf/adsb-scripts
- **airspy-conf** — airspy_adsb configuration helper: https://github.com/wiedehopf/airspy-conf

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
