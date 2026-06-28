#!/bin/bash

# ─────────────────────────────────────────────────────────────────────────────
# generate-feeders-ini.sh — emit a feeders.ini for the monitor's sidebar.
#
# Called by install-stack.sh with the chosen decoder and the list of selected
# feeders; prints the .ini to stdout (the installer redirects it to the file).
# Maps each selection to a [service:<unit>] or [docker:<container>] section whose
# unit name MUST match the real installed service so status/logs line up. The
# decoder card and readsb+tar1090 are always emitted; feeders are appended per choice.
#   Usage: generate-feeders-ini.sh "flightaware adsbexchange" <decoder>
#   decoder: airspy_adsb | readsb | sdrplay
# ─────────────────────────────────────────────────────────────────────────────

SELECTED="$1"
DECODER="${2:-airspy_adsb}"
DUAL_BAND="${3:-0}"   # 1 → also emit the dump978 (978 UAT) card

# Header
cat << 'EOF'
# ADS-B Stack Monitor - Feeder Configuration
# Section types: [service:<unit>] or [docker:<container>]

EOF

# Core decoder card — depends on SDR type
case "$DECODER" in
  airspy_adsb)
    cat << 'EOF'
[service:airspy_adsb]
label = airspy_adsb
hint = Beast out → :30004
icon = antenna

EOF
    ;;
  sdrplay)
    cat << 'EOF'
[service:dump1090-sdrplay]
label = dump1090 (SDRplay)
hint = Beast out → :30005
icon = antenna

EOF
    ;;
  # readsb (RTL-SDR) — no separate decoder service, readsb drives the dongle
esac

# readsb + tar1090 always present
cat << 'EOF'
[service:readsb]
label = readsb
hint = Beast out → :30005
icon = broadcast

[service:tar1090]
label = tar1090
hint = Web map → :8504
icon = map

EOF

# dump978 (978 UAT) — only on dual-band installs
if [ "$DUAL_BAND" = "1" ]; then
  cat << 'EOF'
[service:dump978-fa]
label = dump978 (978 UAT)
hint = UAT → readsb :30978
icon = antenna

EOF
fi

for feeder in $SELECTED; do
  case "$feeder" in
    flightaware) printf '[service:piaware]\nlabel = FlightAware\nhint = Beast in ← :30005\nicon = plane-departure\n\n' ;;
    flightradar24) printf '[service:fr24feed]\nlabel = FlightRadar24\nhint = Config: /etc/fr24feed.ini\nicon = plane\n\n' ;;
    adsbexchange) printf '[service:adsbexchange-feed]\nlabel = ADSBExchange feed\nhint = Beast in ← :30005\nicon = world\n\n[service:adsbexchange-mlat]\nlabel = ADSBExchange MLAT\nhint = MLAT → feed.adsbexchange.com:31090\nicon = world\n\n' ;;
    adsbfi) printf '[service:adsbfi-feed]\nlabel = adsb.fi\nhint = Beast in ← :30005\nicon = globe\n\n' ;;
    adsblol) printf '[service:adsblol-feed]\nlabel = adsb.lol\nhint = Beast in ← :30005\nicon = globe\n\n' ;;
    airnav) printf '[docker:airnavradar]\nlabel = AirNav Radar\nhint = Docker container\nicon = radar-2\n\n' ;;
    opensky) printf '[service:opensky-feeder]\nlabel = OpenSky Network\nhint = Custom feeder client\nicon = satellite\n\n' ;;
    planefinder) printf '[service:pfclient]\nlabel = Plane Finder\nhint = http://<pi>:30053\nicon = plane\n\n' ;;
    theairtraffic) printf '[service:theairtraffic-feed]\nlabel = TheAirTraffic\nhint = Beast in ← :30005\nicon = world\n\n' ;;
  esac
done
