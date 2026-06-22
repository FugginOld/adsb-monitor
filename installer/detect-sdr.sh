#!/bin/bash
#
# SDR detection — identifies connected SDR via lsusb and returns:
#   SDR_TYPE   : airspy | rtlsdr | sdrplay | unknown
#   SDR_MODEL  : human-readable model name
#   SDR_DECODER: airspy_adsb | readsb | sdrplay
#   REC_SAMPLE_RATE, REC_GAIN, REC_NOTES
#
# Outputs shell-sourceable variable assignments.
#
# USB IDs reference:
#   Airspy:        1d50:60a1 (Mini), 1d50:60a8/0002 (R2)
#   RTL-SDR:       0bda:2838 (generic R820T2), 0bda:2832
#   RTL-SDR Blog V4 uses 0bda:2838 but reports via eeprom string
#   FlightAware:   0bda:2832 / 0bda:2838 with "FlightAware" string
#   Nooelec:       0bda:2838 with "NESDR" string
#   SDRplay:       1df7:3000 (RSP1A), 1df7:3020 (RSPdx), 1df7:3010 (RSPduo)

detect_sdr() {
  local lsusb_out
  lsusb_out=$(lsusb 2>/dev/null)

  # ── Airspy ────────────────────────────────────────────────────────────────
  if echo "$lsusb_out" | grep -qiE '1d50:60a1'; then
    echo "SDR_TYPE=airspy"
    echo "SDR_MODEL=Airspy Mini"
    echo "SDR_DECODER=airspy_adsb"
    echo "REC_SAMPLE_RATE=6"
    echo "REC_GAIN=21"
    echo "REC_NOTES=12-bit, 6 MSPS — excellent for 1090 MHz"
    return
  fi
  if echo "$lsusb_out" | grep -qiE '1d50:(60a8|0002)'; then
    echo "SDR_TYPE=airspy"
    echo "SDR_MODEL=Airspy R2"
    echo "SDR_DECODER=airspy_adsb"
    echo "REC_SAMPLE_RATE=12"
    echo "REC_GAIN=21"
    echo "REC_NOTES=10 MSPS, more bandwidth headroom"
    return
  fi

  # ── SDRplay ───────────────────────────────────────────────────────────────
  if echo "$lsusb_out" | grep -qiE '1df7:3000'; then
    echo "SDR_TYPE=sdrplay"
    echo "SDR_MODEL=SDRplay RSP1A"
    echo "SDR_DECODER=sdrplay"
    echo "REC_SAMPLE_RATE=na"
    echo "REC_GAIN=40"
    echo "REC_NOTES=14-bit ADC, wide coverage — needs SDRplay API + dump1090"
    return
  fi
  if echo "$lsusb_out" | grep -qiE '1df7:3020'; then
    echo "SDR_TYPE=sdrplay"
    echo "SDR_MODEL=SDRplay RSPdx"
    echo "SDR_DECODER=sdrplay"
    echo "REC_SAMPLE_RATE=na"
    echo "REC_GAIN=40"
    echo "REC_NOTES=14-bit ADC, excellent dynamic range"
    return
  fi
  if echo "$lsusb_out" | grep -qiE '1df7:'; then
    echo "SDR_TYPE=sdrplay"
    echo "SDR_MODEL=SDRplay (other)"
    echo "SDR_DECODER=sdrplay"
    echo "REC_SAMPLE_RATE=na"
    echo "REC_GAIN=40"
    echo "REC_NOTES=SDRplay device — needs SDRplay API + dump1090"
    return
  fi

  # ── RTL-SDR family (0bda:2832 / 0bda:2838) ────────────────────────────────
  if echo "$lsusb_out" | grep -qiE '0bda:(2832|2838)'; then
    # Try to distinguish variant via rtl_eeprom or kernel string
    local rtl_string=""
    if command -v rtl_eeprom >/dev/null 2>&1; then
      rtl_string=$(timeout 5 rtl_eeprom 2>&1 | grep -iE 'Product|Manufacturer' || true)
    fi
    # Also check dmesg / usb product string
    local usb_string
    usb_string=$(echo "$lsusb_out" | grep -iE '0bda:(2832|2838)')

    if echo "$rtl_string $usb_string" | grep -qi 'flightaware'; then
      # FlightAware Pro Stick or Plus
      if echo "$rtl_string" | grep -qi 'plus'; then
        echo "SDR_TYPE=rtlsdr"
        echo "SDR_MODEL=FlightAware Pro Stick Plus"
        echo "SDR_DECODER=readsb"
        echo "REC_SAMPLE_RATE=na"
        echo "REC_GAIN=auto"
        echo "REC_NOTES=Built-in 1090 SAW filter + amp — use gain auto or ~max"
      else
        echo "SDR_TYPE=rtlsdr"
        echo "SDR_MODEL=FlightAware Pro Stick"
        echo "SDR_DECODER=readsb"
        echo "REC_SAMPLE_RATE=na"
        echo "REC_GAIN=auto"
        echo "REC_NOTES=Built-in amp — use gain auto"
      fi
      return
    fi
    if echo "$rtl_string $usb_string" | grep -qi 'nesdr\|nooelec'; then
      echo "SDR_TYPE=rtlsdr"
      echo "SDR_MODEL=Nooelec NESDR"
      echo "SDR_DECODER=readsb"
      echo "REC_SAMPLE_RATE=na"
      echo "REC_GAIN=auto"
      echo "REC_NOTES=SMArTee variant has bias-tee for LNA power"
      return
    fi
    if echo "$rtl_string $usb_string" | grep -qi 'blog\|rtlsdrblog'; then
      echo "SDR_TYPE=rtlsdr"
      echo "SDR_MODEL=RTL-SDR Blog V4"
      echo "SDR_DECODER=readsb"
      echo "REC_SAMPLE_RATE=na"
      echo "REC_GAIN=auto"
      echo "REC_NOTES=De facto standard — bias-tee capable, good clocking"
      return
    fi
    # Generic RTL2832U
    echo "SDR_TYPE=rtlsdr"
    echo "SDR_MODEL=Generic RTL2832U + R820T2"
    echo "SDR_DECODER=readsb"
    echo "REC_SAMPLE_RATE=na"
    echo "REC_GAIN=auto"
    echo "REC_NOTES=Works but inferior shielding/clocking vs Blog V4"
    return
  fi

  # ── Nothing found ─────────────────────────────────────────────────────────
  echo "SDR_TYPE=unknown"
  echo "SDR_MODEL=No SDR detected"
  echo "SDR_DECODER=none"
  echo "REC_SAMPLE_RATE=na"
  echo "REC_GAIN=auto"
  echo "REC_NOTES=Plug in your SDR and try again, or select manually"
}

# If run directly, output detection
if [ "${BASH_SOURCE[0]}" = "${0}" ]; then
  detect_sdr
fi
