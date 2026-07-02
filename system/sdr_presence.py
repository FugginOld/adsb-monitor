"""SDR presence guard (1090 readsb + 978 dump978 + airspy_adsb).

A decoder crash-loops (or grabs the wrong stick) when its RTL dongle is
gone. If the configured serial isn't on USB, stop the decoder gracefully;
when the stick returns, resume the decoder *we* stopped (a manual stop is
never undone). Runs at startup (covers reboots) and every poll (covers live
un/replugs). Airspy / net-only 1090 has no local stick, so it's left alone.

Depends on system/sdr_settings.py (for `_opt_in_receiver`, to read the
configured device serial) and system/sdr_detect.py (to check an Airspy is
actually present) — not the other way round.

`HOST` and the `*_DEFAULT` config paths stay defined in app.py, reached via
`import app`.
"""
import logging
import re

import app
from system.sdr_detect import detect_airspy_model
from system.sdr_settings import _opt_in_receiver
from system.services import service_action, systemd_status

logger = logging.getLogger(__name__)

# Services we stopped for a missing stick — only these are eligible for auto-resume.
# ponytail: in-memory set, fine for one monitor process; resets on restart (re-derived next poll).
_sdr_autostopped = set()

def _sdr1090_serial():
    text = app.HOST.read_text(app.READSB_DEFAULT) or ''
    if '--device-type rtlsdr' not in text:
        return None  # Airspy / net-only — no local 1090 stick
    return _opt_in_receiver(text, '--device')

def _sdr978_serial():
    m = re.search(r'--sdr\s+driver=rtlsdr,serial=([^\s",]+)', app.HOST.read_text(app.DUMP978_DEFAULT) or '')
    return m.group(1) if m else None

def _rtl_present(serial):
    """True/False if an RTL stick with `serial` is on USB; None if no serial to match.

    Reads the USB serial straight from sysfs — no rtlsdr_open(), so it never
    claims or resets a device. The old rtl_eeprom -d 0..3 loop opened every
    stick (incl. the *active* 1090), resetting it mid-stream; and when the four
    opens overran the 10s budget it timed out and falsely reported the 978
    absent, so it never auto-resumed on replug.
    """
    if not serial:
        return None  # auto-device setups have no specific serial — leave alone
    r = app.HOST.run(['/bin/sh', '-c', 'cat /sys/bus/usb/devices/*/serial 2>/dev/null'], timeout=5)
    return serial in r.out.split()

def _airspy_present():
    """True/False if an Airspy is on USB; None if this host has no Airspy decoder."""
    if app.HOST.read_text(app.AIRSPY_DEFAULT) is None:
        return None
    return detect_airspy_model() != 'unknown'

def _enforce_sdr(service, present_fn):
    """Stop `service` when its SDR is gone; resume the one we stopped when it returns.

    present_fn() -> True/False/None (None = not applicable on this host, skip).
    An active decoder is proof its SDR is present, so we skip the probe then and
    just clear any stale auto-stop flag; we probe only once it's inactive/failed.
    """
    state = systemd_status(service)[1]
    if state == 'active':
        _sdr_autostopped.discard(service)  # reading fine = SDR present; clear stale flag
        return
    if state not in ('inactive', 'failed'):
        return  # activating/reloading — let it settle before probing
    present = present_fn()
    if present is None:
        return
    if not present:
        service_action(service, 'stop')
        _sdr_autostopped.add(service)
        logger.warning("SDR not detected — stopped %s", service)
    elif service in _sdr_autostopped:
        service_action(service, 'start')
        _sdr_autostopped.discard(service)
        logger.info("SDR back — started %s", service)

def enforce_sdr_presence():
    _enforce_sdr('readsb',      lambda: _rtl_present(_sdr1090_serial()))
    _enforce_sdr('dump978-fa',  lambda: _rtl_present(_sdr978_serial()))
    _enforce_sdr('airspy_adsb', _airspy_present)
