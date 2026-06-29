#!/bin/bash

# ─────────────────────────────────────────────────────────────────────────────
# install-stack.sh — full ADS-B station installer (whiptail TUI).
#
# Walks the user through a complete build: detect the SDR, ask for location and
# gain, pick feeders, then install everything — decoder, readsb + tar1090,
# graphs1090, the chosen feeders, and finally adsb-monitor itself. The heavy
# lifting (readsb, decoders, most feeders) is delegated to wiedehopf's and the
# aggregators' own install scripts; this script orchestrates and configures them.
#
# Flow: distro check → SDR detect → TUI prompts → confirm → install [1/6..6/6].
# Shared helpers (logging, pkg manager, TUI bootstrap, arch) come from lib.sh.
# `set -uo pipefail`: error on unset vars and surface failures inside pipelines.
# ─────────────────────────────────────────────────────────────────────────────

set -uo pipefail

STATE_DIR="/tmp/adsb-install"; mkdir -p "$STATE_DIR"
# Resolve this script's own directory so it can source siblings (lib.sh,
# detect-sdr.sh) and reach repo files (../app.py) no matter where it's run from.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# shellcheck source=installer/lib.sh
source "$SCRIPT_DIR/lib.sh"

if [ "$EUID" -ne 0 ]; then err "Run with sudo: sudo ./install-stack.sh"; exit 1; fi
check_distro_support
ensure_tui

whiptail --title "ADS-B Stack Installer" --msgbox \
"Welcome to the ADS-B full-stack installer.

This will detect your SDR and install:
  - The correct decoder for your SDR
  - readsb + tar1090 (live map)
  - graphs1090 (performance graphs)
  - Your selected feeders
  - adsb-monitor (dashboard)

Supports: RPi OS, Armbian, Ubuntu, Debian, and derivatives.
Architecture detected: $ARCH

Press OK to begin." 20 70

# ── SDR detection ───────────────────────────────────────────────────────────
info "Detecting connected SDR..."
pkg_install usbutils
source "$SCRIPT_DIR/detect-sdr.sh"
# detect_sdr prints KEY=value lines; assign by name (no eval — values contain spaces)
while IFS='=' read -r _k _v; do
  [ -n "$_k" ] && printf -v "$_k" '%s' "$_v"
done < <(detect_sdr)

if [ "$SDR_TYPE" = "unknown" ]; then
  CHOICE=$(whiptail --title "SDR Not Detected" --menu \
"No SDR auto-detected. Plug one in and Retry, or select manually:" 18 70 6 \
"retry"   "Re-scan for SDR" \
"airspy_mini" "Airspy Mini" \
"airspy_r2"   "Airspy R2" \
"rtlsdr"  "RTL-SDR (Blog V4 / generic / FlightAware / Nooelec)" \
"sdrplay" "SDRplay RSP1A / RSPdx" \
"quit"    "Exit installer" \
3>&1 1>&2 2>&3) || exit 1
  case "$CHOICE" in
    retry)       exec "$0" ;;
    airspy_mini) SDR_TYPE=airspy; SDR_MODEL="Airspy Mini"; SDR_DECODER=airspy_adsb; REC_SAMPLE_RATE=6; REC_GAIN=21; REC_NOTES="Airspy Mini" ;;
    airspy_r2)   SDR_TYPE=airspy; SDR_MODEL="Airspy R2";   SDR_DECODER=airspy_adsb; REC_SAMPLE_RATE=12; REC_GAIN=21; REC_NOTES="Airspy R2" ;;
    rtlsdr)      SDR_TYPE=rtlsdr; SDR_MODEL="RTL-SDR";     SDR_DECODER=readsb; REC_SAMPLE_RATE=na; REC_GAIN=auto; REC_NOTES="RTL-SDR" ;;
    sdrplay)     SDR_TYPE=sdrplay; SDR_MODEL="SDRplay";    SDR_DECODER=sdrplay; REC_SAMPLE_RATE=na; REC_GAIN=40; REC_NOTES="SDRplay" ;;
    quit)        exit 0 ;;
  esac
fi

whiptail --title "SDR Detected" --yesno \
"Detected SDR:

  Model:    $SDR_MODEL
  Decoder:  $SDR_DECODER
  Notes:    $REC_NOTES

Recommended:
  Sample rate: ${REC_SAMPLE_RATE}
  Gain:        $REC_GAIN

Is this correct?" 18 70 || { warn "Re-run and select manually"; exit 1; }

# ── Dual-band (1090 + 978 UAT) ───────────────────────────────────────────────
# Two RTL-SDRs → offer 978 UAT alongside 1090. Requires unique USB serials so
# readsb and dump978 each bind the right stick. We pick the 978-serial stick for
# UAT, the other for 1090, and merge 978 into readsb via its uat_in connector so
# tar1090 shows both bands on one map.
DUAL_BAND=0; SDR_SERIAL=""; UAT_SERIAL=""
RTL_COUNT=$(lsusb | grep -ciE '0bda:(2832|2838)')
if [ "$SDR_TYPE" = "rtlsdr" ] && [ "${RTL_COUNT:-0}" -ge 2 ]; then
  if whiptail --title "Two RTL-SDRs Detected" --yesno \
"Two RTL-SDR sticks found.

Enable DUAL-BAND — 1090 MHz ADS-B + 978 MHz UAT (US)?

Each stick needs a unique USB serial. Assign once with:
  rtl_eeprom -d 0 -s 00001090
  rtl_eeprom -d 1 -s 00000978
then unplug/replug both. The stick whose serial contains
'978' is used for UAT; the other for 1090.

Enable 978 UAT?" 19 72; then
    pkg_install rtl-sdr
    while read -r _sn; do
      case "$_sn" in
        *978*) UAT_SERIAL="$_sn" ;;
        ?*)    SDR_SERIAL="$_sn" ;;
      esac
    done < <(rtl_test -t 2>&1 | sed -n 's/.*SN: *\([^ ]*\).*/\1/p')
    if [ -n "$UAT_SERIAL" ] && [ -n "$SDR_SERIAL" ]; then
      DUAL_BAND=1
      ok "Dual-band: 1090=$SDR_SERIAL  978=$UAT_SERIAL"
    else
      whiptail --title "Serials Not Set" --msgbox \
"Couldn't find two sticks with distinct serials (need one containing '978').

Assign serials with rtl_eeprom (see prior screen), replug, then re-run.
Continuing with 1090-only for now." 13 72
    fi
  fi
fi

# ── Location ────────────────────────────────────────────────────────────────
LAT=$(whiptail --title "Receiver Location" --inputbox "LATITUDE (decimal)\n\nExample: 27.88718" 11 60 "" 3>&1 1>&2 2>&3) || exit 1
LON=$(whiptail --title "Receiver Location" --inputbox "LONGITUDE (decimal)\n\nExample: -82.25752" 11 60 "" 3>&1 1>&2 2>&3) || exit 1
ALT_M=$(whiptail --title "Receiver Location" --inputbox "ALTITUDE in METERS\n\nExample: 25" 11 60 "25" 3>&1 1>&2 2>&3) || exit 1

GAIN="$REC_GAIN"; SAMPLE_RATE="$REC_SAMPLE_RATE"
if [ "$SDR_TYPE" = "airspy" ]; then
  GAIN=$(whiptail --title "Airspy Gain" --inputbox "Gain (0-21)\n\n21 = max; reduce near airports." 11 60 "$REC_GAIN" 3>&1 1>&2 2>&3) || exit 1
elif [ "$SDR_TYPE" = "rtlsdr" ]; then
  GAIN=$(whiptail --title "RTL-SDR Gain" --inputbox "Gain (0-49.6 or 'auto')\n\n'auto' lets readsb optimize." 11 60 "auto" 3>&1 1>&2 2>&3) || exit 1
fi

FEEDERS=$(whiptail --title "Select Feeders" --checklist \
"Choose aggregators (SPACE toggles, ENTER confirms):" 22 78 12 \
"flightaware"   "FlightAware - free Enterprise account" OFF \
"flightradar24" "FlightRadar24 - free Business plan" OFF \
"adsbexchange"  "ADS-B Exchange - unfiltered, community" OFF \
"adsbfi"        "adsb.fi - open, no filtering" OFF \
"adsblol"       "adsb.lol - open-source, community" OFF \
"airnav"        "RadarBox / AirNav - free business account" OFF \
"opensky"       "OpenSky Network - research/academic" OFF \
"planefinder"   "Plane Finder - free perks" OFF \
"adsbhub"       "ADSBHub - share-to-receive" OFF \
"planewatch"    "Plane.watch - community-driven" OFF \
"theairtraffic" "TheAirTraffic - open, unfiltered" OFF \
3>&1 1>&2 2>&3) || exit 1
FEEDERS=$(echo "$FEEDERS" | tr -d '"')

whiptail --title "Confirm" --yesno \
"Ready to install:

SDR:       $SDR_MODEL ($SDR_DECODER)$([ "$DUAL_BAND" = "1" ] && printf '\n978 UAT:   dump978 on serial %s' "$UAT_SERIAL")
Location:  $LAT, $LON @ ${ALT_M}m
Gain:      $GAIN
Feeders:   ${FEEDERS:-none}

Takes 10-20 minutes. Continue?" 16 70 || exit 1

clear
info "Installing..."
pkg_update
install_base_deps
ok "Dependencies installed"

info "[1/6] Installing readsb + tar1090..."
bash -c "$(wget -q -O - https://raw.githubusercontent.com/wiedehopf/adsb-scripts/master/readsb-install.sh)" >/dev/null 2>&1
ok "readsb + tar1090 installed"

info "[2/6] Configuring decoder for $SDR_MODEL..."
case "$SDR_DECODER" in
  airspy_adsb)
    bash -c "$(wget -O - https://raw.githubusercontent.com/wiedehopf/airspy-conf/master/install.sh)" >/dev/null 2>&1
    cat > /etc/default/airspy_adsb << AIRSPYEOF
GAIN=$GAIN
SAMPLE_RATE=$SAMPLE_RATE
OPTIONS="-v -f 1 -w 5 -C 95 -E 60"
AFFINITY=0
NET="-l 47787:beast -c 127.0.0.1:30004:beast"
STATS="-S /run/airspy_adsb/stats.json"
G=
M=
AIRSPYEOF
    sed -i "s|RECEIVER_OPTIONS=.*|RECEIVER_OPTIONS=\"--net-only --write-json-every 1 --lat $LAT --lon $LON\"|" /etc/default/readsb
    systemctl daemon-reload; systemctl restart airspy_adsb readsb
    ok "airspy_adsb configured (${SAMPLE_RATE} MSPS, gain $GAIN)" ;;
  readsb)
    # Dual-band: pin readsb to the 1090 stick by serial so it doesn't grab the 978 one.
    DEV_OPT=""; [ "$DUAL_BAND" = "1" ] && DEV_OPT="--device $SDR_SERIAL "
    sed -i "s|RECEIVER_OPTIONS=.*|RECEIVER_OPTIONS=\"--device-type rtlsdr ${DEV_OPT}--gain $GAIN --lat $LAT --lon $LON\"|" /etc/default/readsb
    systemctl restart readsb
    ok "readsb configured for RTL-SDR (gain $GAIN)" ;;
  sdrplay)
    warn "SDRplay needs the proprietary SDRplay API + dump1090."
    info "Installing SDRplay API..."
    wget -q https://www.sdrplay.com/software/SDRplay_RSP_API-Linux-3.15.2.run -O /tmp/sdrplay.run 2>/dev/null
    if [ -f /tmp/sdrplay.run ]; then chmod +x /tmp/sdrplay.run; echo -e "\n\ny\n" | /tmp/sdrplay.run >/dev/null 2>&1 || warn "SDRplay API may need manual completion"; fi
    warn "SDRplay decoder setup is partially manual - see sdrplay.com" ;;
esac

if [ "$DUAL_BAND" = "1" ]; then
  info "[2b] Installing dump978 (978 UAT) on serial $UAT_SERIAL..."
  pkg_install build-essential debhelper git \
    libboost-program-options-dev libboost-regex-dev libboost-filesystem-dev \
    libsoapysdr-dev soapysdr-module-rtlsdr
  rm -rf /tmp/dump978-build
  if git clone --depth 1 https://github.com/flightaware/dump978 /tmp/dump978-build >/dev/null 2>&1 \
     && ( cd /tmp/dump978-build && dpkg-buildpackage -b -uc -us ) >/dev/null 2>&1 \
     && dpkg -i /tmp/dump978-fa_*.deb >/dev/null 2>&1; then
    # skyaware978 (built alongside dump978-fa) writes /run/skyaware978/aircraft.json
    # with per-aircraft RSSI — the monitor's 978 status block reads it. Best-effort.
    dpkg -i /tmp/skyaware978_*.deb >/dev/null 2>&1 || apt-get -fy install >/dev/null 2>&1
    systemctl enable --now skyaware978 >/dev/null 2>&1 || true
    # graphs1090 ([3/6]) enables its 978 graphs only if /run/skyaware978/aircraft.json
    # already exists when it installs — wait for skyaware978 to write it first.
    for _i in $(seq 1 15); do [ -f /run/skyaware978/aircraft.json ] && break; sleep 1; done
    # Bind the UAT stick. The package's NET_OPTIONS already exposes --raw-port 30978
    # (which readsb ingests); don't repeat it here or dump978 binds the port twice.
    sed -i "s|^RECEIVER_OPTIONS=.*|RECEIVER_OPTIONS=\"--sdr driver=rtlsdr,serial=$UAT_SERIAL\"|" /etc/default/dump978-fa
    # Merge 978 into readsb so tar1090 shows both bands on one map.
    if grep -q "30978,uat_in" /etc/default/readsb; then :
    elif grep -q '^NET_OPTIONS="' /etc/default/readsb; then
      sed -i 's|^NET_OPTIONS="|NET_OPTIONS="--net-connector 127.0.0.1,30978,uat_in |' /etc/default/readsb
    else
      echo 'NET_OPTIONS="--net-connector 127.0.0.1,30978,uat_in"' >> /etc/default/readsb
    fi
    systemctl daemon-reload; systemctl enable --now dump978-fa >/dev/null 2>&1; systemctl restart readsb
    ok "dump978 installed — 978 UAT merged into tar1090"
  else
    warn "dump978 build/install failed — 1090 still works; see github.com/flightaware/dump978"
  fi
fi

# In a container (LXC) there's usually no udev to apply the rtl-sdr rules that
# grant the service user USB access, so the decoders run as root or they can't
# open the dongles ("unable to read device details"). Pin the SDR services to root.
if systemd-detect-virt --container >/dev/null 2>&1; then
  for _svc in readsb dump978-fa airspy_adsb; do
    systemctl cat "$_svc" >/dev/null 2>&1 || continue
    mkdir -p "/etc/systemd/system/$_svc.service.d"
    printf '[Service]\nUser=root\nGroup=root\n' > "/etc/systemd/system/$_svc.service.d/root.conf"
  done
  systemctl daemon-reload
  for _svc in readsb dump978-fa airspy_adsb; do systemctl restart "$_svc" 2>/dev/null; done
  ok "Container detected — SDR services pinned to root (no udev)"
fi

info "[3/6] Installing graphs1090..."
bash -c "$(curl -L -o - https://github.com/wiedehopf/graphs1090/raw/master/install.sh)" >/dev/null 2>&1
if [ -f /etc/default/graphs1090 ]; then
  grep -q "^colorscheme=" /etc/default/graphs1090 && sed -i 's/^colorscheme=.*/colorscheme=dark/' /etc/default/graphs1090 || echo "colorscheme=dark" >> /etc/default/graphs1090
fi
systemctl restart graphs1090 2>/dev/null
ok "graphs1090 installed (dark mode)"

info "[4/6] Installing feeders..."
install_feeder() {
  case "$1" in
    adsbexchange) bash -c "$(wget -q -O - https://www.adsbexchange.com/feed.sh)" </dev/null >/dev/null 2>&1; ok "  ADSBExchange installed" ;;
    adsbfi) bash -c "$(wget -q -O - https://raw.githubusercontent.com/adsbfi/adsb-fi-scripts/main/install.sh)" </dev/null >/dev/null 2>&1; ok "  adsb.fi installed" ;;
    adsblol) bash -c "$(wget -q -O - https://raw.githubusercontent.com/adsblol/feed/main/install.sh)" </dev/null >/dev/null 2>&1; ok "  adsb.lol installed" ;;
    flightaware)
      bash -c "$(wget -q -O - https://raw.githubusercontent.com/abcd567a/piaware-ubuntu-debian-amd64/master/install-piaware.sh)" </dev/null >/dev/null 2>&1
      piaware-config receiver-type other 2>/dev/null; piaware-config receiver-host localhost 2>/dev/null; piaware-config receiver-port 30005 2>/dev/null
      systemctl restart piaware 2>/dev/null; ok "  PiAware installed (claim at flightaware.com)" ;;
    flightradar24)
      if [ "$ARCH" = "unsupported" ]; then warn "  FR24: unsupported architecture $(uname -m) — skipping"
      else
        wget -q "https://repo-feed.flightradar24.com/rpi_binaries/fr24feed_1.0.53-0_${ARCH}.deb" -O /tmp/fr24feed.deb 2>/dev/null \
          && dpkg_install "fr24feed" /tmp/fr24feed.deb \
          && ok "  FR24 installed (run 'fr24feed --signup')" \
          || warn "  FR24 .deb download/install failed for arch $ARCH"
      fi ;;
    airnav) command -v docker >/dev/null 2>&1 || curl -fsSL https://get.docker.com | sh >/dev/null 2>&1; ok "  Docker ready - configure AirNav in Settings" ;;
    opensky) warn "  OpenSky needs manual feeder - see opensky-network.org/feed" ;;
    planefinder)
      if [ "$ARCH" = "unsupported" ]; then warn "  Plane Finder: unsupported architecture $(uname -m) — skipping"
      else
        wget -q "https://client.planefinder.net/pfclient_5.0.161_${ARCH}.deb" -O /tmp/pfclient.deb 2>/dev/null \
          && dpkg_install "pfclient" /tmp/pfclient.deb \
          && ok "  Plane Finder installed (:30053)" \
          || warn "  Plane Finder .deb download/install failed for arch $ARCH"
      fi ;;
    adsbhub) warn "  ADSBHub needs manual client - see adsbhub.org/howtofeed.php" ;;
    planewatch) warn "  Plane.watch is Docker-based - see sdr-enthusiasts.gitbook.io" ;;
    theairtraffic) bash -c "$(wget -q -O - https://gitlab.com/adsb/theairtraffic-feeder/-/raw/master/install.sh)" </dev/null >/dev/null 2>&1; ok "  TheAirTraffic installed" ;;
  esac
}
for f in $FEEDERS; do install_feeder "$f"; done
ok "Feeders installed"

info "[5/6] Setting feeder locations..."
command -v adsbexchange-setup >/dev/null 2>&1 && echo -e "$LAT\n$LON\n$ALT_M\n" | adsbexchange-setup >/dev/null 2>&1 || true
ok "Locations set where scriptable"

info "[6/6] Installing adsb-monitor..."
DEST=/opt/adsb-monitor
mkdir -p "$DEST/static"
cp "$SCRIPT_DIR/../app.py" "$DEST/app.py"
cp "$SCRIPT_DIR/../static/index.html" "$DEST/static/index.html"
"$SCRIPT_DIR/generate-feeders-ini.sh" "$FEEDERS" "$SDR_DECODER" "$DUAL_BAND" > "$DEST/feeders.ini"
python3 -m venv "$DEST/venv"
"$DEST/venv/bin/pip" install --quiet flask psutil
PI_IP=$(hostname -I | awk '{print $1}')
cat > /etc/systemd/system/adsb-monitor.service << SVCEOF
[Unit]
Description=ADS-B Stack Health Monitor
After=network.target readsb.service

[Service]
Type=simple
User=root
WorkingDirectory=$DEST
ExecStart=$DEST/venv/bin/python app.py
Restart=always
RestartSec=5
Environment="TAR1090_URL_LOCAL=http://$PI_IP:8504"
Environment="GRAPHS1090_URL_LOCAL=http://$PI_IP/graphs1090"
Environment="TAR1090_URL_REMOTE=http://$PI_IP:8504"
Environment="GRAPHS1090_URL_REMOTE=http://$PI_IP/graphs1090"
Environment="ADMIN_PORT=5000"
Environment="READONLY_PORT=5001"

[Install]
WantedBy=multi-user.target
SVCEOF
systemctl daemon-reload; systemctl enable --now adsb-monitor >/dev/null 2>&1
ok "adsb-monitor installed"

clear
PI_IP=$(hostname -I | awk '{print $1}')
whiptail --title "Installation Complete" --msgbox \
"ADS-B stack installed!

SDR: $SDR_MODEL$([ "$DUAL_BAND" = "1" ] && printf '\n978 UAT: dump978 (serial %s) — merged into the map' "$UAT_SERIAL")

Access:
  Monitor:   http://$PI_IP:5000
  Read-only: http://$PI_IP:5001
  Map:       http://$PI_IP:8504
  Graphs:    http://$PI_IP/graphs1090

Feeders needing keys/claiming:
  FlightAware: claim at flightaware.com/adsb/stats
  FR24: 'sudo fr24feed --signup'
  AirNav + others: enter keys in monitor Settings

Enjoy!" 22 70
echo ""; ok "Done! Monitor: http://$PI_IP:5000"
