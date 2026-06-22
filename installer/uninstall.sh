#!/bin/bash
# ADS-B Stack Uninstaller (TUI)
# Removes adsb-monitor and optionally each stack component
set -uo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
info()  { echo -e "${BLUE}==>${NC} $1"; }
ok()    { echo -e "${GREEN}✓${NC} $1"; }
warn()  { echo -e "${YELLOW}⚠${NC} $1"; }
err()   { echo -e "${RED}✗${NC} $1"; }

if [ "$EUID" -ne 0 ]; then err "Run with sudo: sudo ./uninstall.sh"; exit 1; fi
if ! command -v whiptail >/dev/null 2>&1; then apt-get install -y whiptail >/dev/null 2>&1; fi

whiptail --title "ADS-B Stack Uninstaller" --yesno \
"This will remove components of your ADS-B stack.

You'll choose exactly what to remove on the next screen.
Nothing is removed until you confirm.

Continue?" 14 70 || exit 0

# What to remove
TARGETS=$(whiptail --title "Select Components to Remove" --checklist \
"Choose what to UNINSTALL (SPACE toggles):" 22 78 12 \
"monitor"      "adsb-monitor dashboard" ON \
"airspy"       "airspy_adsb decoder" OFF \
"readsb"       "readsb + tar1090" OFF \
"graphs1090"   "graphs1090" OFF \
"flightaware"  "PiAware (FlightAware)" OFF \
"flightradar24" "fr24feed (FlightRadar24)" OFF \
"adsbexchange" "ADSBExchange feed + mlat" OFF \
"adsbfi"       "adsb.fi" OFF \
"adsblol"      "adsb.lol" OFF \
"airnav"       "AirNav Radar (Docker)" OFF \
"planefinder"  "Plane Finder" OFF \
"theairtraffic" "TheAirTraffic" OFF \
3>&1 1>&2 2>&3) || exit 0
TARGETS=$(echo "$TARGETS" | tr -d '"')

if [ -z "$TARGETS" ]; then info "Nothing selected. Exiting."; exit 0; fi

# Final confirm
whiptail --title "Confirm Removal" --yesno \
"About to remove:

$TARGETS

This cannot be undone. Proceed?" 14 70 || exit 0

clear
info "Uninstalling..."

remove_service() {
  local svc="$1"
  systemctl stop "$svc" 2>/dev/null
  systemctl disable "$svc" 2>/dev/null
}

for target in $TARGETS; do
  case "$target" in
    monitor)
      info "Removing adsb-monitor..."
      remove_service adsb-monitor
      rm -f /etc/systemd/system/adsb-monitor.service
      rm -rf /opt/adsb-monitor
      systemctl daemon-reload
      ok "adsb-monitor removed (history.db deleted)" ;;
    airspy)
      info "Removing airspy_adsb..."
      remove_service airspy_adsb
      rm -f /usr/local/bin/airspy_adsb
      rm -f /etc/default/airspy_adsb
      rm -f /usr/lib/systemd/system/airspy_adsb.service /lib/systemd/system/airspy_adsb.service /etc/systemd/system/airspy_adsb.service
      systemctl daemon-reload
      ok "airspy_adsb removed" ;;
    readsb)
      info "Removing readsb + tar1090..."
      if [ -f /usr/local/share/adsb-scripts/uninstall.sh ]; then
        bash /usr/local/share/adsb-scripts/uninstall.sh >/dev/null 2>&1
      fi
      remove_service readsb
      rm -f /usr/local/bin/readsb
      rm -f /etc/default/readsb
      rm -rf /usr/local/share/tar1090
      rm -f /etc/lighttpd/conf-enabled/*tar1090*
      systemctl daemon-reload
      ok "readsb + tar1090 removed" ;;
    graphs1090)
      info "Removing graphs1090..."
      if [ -f /usr/share/graphs1090/uninstall.sh ]; then
        bash /usr/share/graphs1090/uninstall.sh >/dev/null 2>&1
      fi
      remove_service graphs1090
      rm -rf /usr/share/graphs1090
      rm -f /etc/lighttpd/conf-enabled/*graphs1090*
      systemctl daemon-reload
      ok "graphs1090 removed" ;;
    flightaware)
      info "Removing PiAware..."
      remove_service piaware
      apt-get remove -y piaware >/dev/null 2>&1
      ok "PiAware removed" ;;
    flightradar24)
      info "Removing fr24feed..."
      remove_service fr24feed
      apt-get remove -y fr24feed >/dev/null 2>&1
      rm -f /etc/fr24feed.ini
      ok "fr24feed removed" ;;
    adsbexchange)
      info "Removing ADSBExchange..."
      remove_service adsbexchange-feed
      remove_service adsbexchange-mlat
      if [ -f /usr/local/share/adsbexchange/uninstall.sh ]; then
        bash /usr/local/share/adsbexchange/uninstall.sh >/dev/null 2>&1
      fi
      rm -rf /usr/local/share/adsbexchange
      systemctl daemon-reload
      ok "ADSBExchange removed" ;;
    adsbfi)
      info "Removing adsb.fi..."
      remove_service adsbfi-feed
      rm -rf /usr/local/share/adsbfi
      systemctl daemon-reload
      ok "adsb.fi removed" ;;
    adsblol)
      info "Removing adsb.lol..."
      remove_service adsblol-feed
      rm -rf /usr/local/share/adsblol
      systemctl daemon-reload
      ok "adsb.lol removed" ;;
    airnav)
      info "Removing AirNav Radar..."
      docker stop airnavradar 2>/dev/null
      docker rm airnavradar 2>/dev/null
      ok "AirNav Radar container removed" ;;
    planefinder)
      info "Removing Plane Finder..."
      remove_service pfclient
      apt-get remove -y pfclient >/dev/null 2>&1
      ok "Plane Finder removed" ;;
    theairtraffic)
      info "Removing TheAirTraffic..."
      remove_service theairtraffic-feed
      rm -rf /usr/local/share/theairtraffic
      systemctl daemon-reload
      ok "TheAirTraffic removed" ;;
  esac
done

clear
whiptail --title "Uninstall Complete" --msgbox \
"Selected components have been removed.

Note: system packages installed as dependencies
(python3, sqlite3, etc.) were left in place since
other software may use them.

Done." 14 70
echo ""; ok "Uninstall complete"
