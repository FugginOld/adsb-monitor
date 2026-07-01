"""Airspy / Receiver settings, per-SDR gain + bias-tee, SDR presence guard.

Read/modify the two core SDR config files (/etc/default/airspy_adsb and
/etc/default/readsb). These parse the existing options string, merge in the
user's changes (gain, sample rate, lat/lon, max range) and write it back,
preserving any flags the UI doesn't manage. The routes restart the affected
service afterwards so changes take effect.

Gain and bias-tee for each RTL stick. 1090 gain is readsb's --gain (auto or
a dB number); 978 gain is dump978's --sdr-gain (a number, or 'max' = no
flag). Bias-tee differs: readsb has no flag, so 1090 uses an rtl_biast
ExecStartPre drop-in (resolves the stick by serial, powers the LNA before
readsb opens it); 978 uses SoapySDR's biastee=true device arg. Bias-tee
feeds DC up the coax — only enable it with an inline LNA that expects it.

A decoder crash-loops (or grabs the wrong stick) when its RTL dongle is
gone. If the configured serial isn't on USB, stop the decoder gracefully;
when the stick returns, resume the decoder *we* stopped (a manual stop is
never undone). Runs at startup (covers reboots) and every poll (covers live
un/replugs). Airspy / net-only 1090 has no local stick, so it's left alone.

Identifies which Airspy model is plugged in (Mini vs R2) from its USB id, so
the UI can suggest the correct sample rate, and surfaces a live one-line hint.

`HOST` and the `*_DEFAULT` config paths stay defined in app.py, reached via
`import app`.
"""
import logging
import os
import re

import app
from system.services import service_action, systemd_status

logger = logging.getLogger(__name__)


def parse_airspy_options(text):
    s = {'gain': '21', 'sample_rate': '6', 'options': ''}
    for line in text.splitlines():
        line = line.strip()
        if line.startswith('#') or '=' not in line: continue
        k, _, v = line.partition('=')
        k, v = k.strip(), v.strip().strip('"')
        if k == 'GAIN': s['gain'] = v
        elif k == 'SAMPLE_RATE': s['sample_rate'] = v
        elif k == 'OPTIONS': s['options'] = v
    return s

def write_airspy_options(settings):
    existing = app.HOST.read_text(app.AIRSPY_DEFAULT) or ''
    updates = {
        'GAIN':        settings.get('gain', '21'),
        'SAMPLE_RATE': settings.get('sample_rate', '6'),
        'OPTIONS':     f'"{settings.get("options", "-v -f 1 -w 5 -C 95 -E 60")}"',
    }
    updated = set()
    out = []
    for line in existing.splitlines():
        stripped = line.strip()
        if stripped.startswith('#') or '=' not in stripped:
            out.append(line); continue
        key = stripped.split('=')[0].strip()
        if key in updates:
            out.append(f'{key}={updates[key]}')
            updated.add(key)
        else:
            out.append(line)
    for key, val in updates.items():
        if key not in updated:
            out.append(f'{key}={val}')
    existing_keys = {l.split('=')[0].strip() for l in out if '=' in l and not l.strip().startswith('#')}
    required = {'AFFINITY': '0', 'NET': '-l 47787:beast -c 127.0.0.1:30004:beast',
                'STATS': '-S /run/airspy_adsb/stats.json', 'G': '', 'M': ''}
    for key, default in required.items():
        if key not in existing_keys:
            out.append(f'{key}={default}')
    app.HOST.write_text(app.AIRSPY_DEFAULT, '\n'.join(out) + '\n')

def parse_receiver_options(text):
    s = {'lat': '', 'lon': '', 'max_range': '500'}
    for line in text.splitlines():
        if 'RECEIVER_OPTIONS' in line:
            for flag, key in [('--lat','lat'),('--lon','lon'),('--max-range','max_range')]:
                m = re.search(rf'{flag}\s+([\d.-]+)', line)
                if m: s[key] = m.group(1)
    return s

def write_receiver_options(text, new_settings):
    lines = text.splitlines()
    out = []
    for line in lines:
        if 'RECEIVER_OPTIONS' in line:
            for flag, key in [('--lat','lat'),('--lon','lon'),('--max-range','max_range')]:
                val = new_settings.get(key, '')
                if not val: continue
                if re.search(rf'{flag}\s+[\d.-]+', line):
                    line = re.sub(rf'{flag}\s+[\d.-]+', f'{flag} {val}', line)
                else:
                    line = line.rstrip('"').rstrip() + f' {flag} {val}"'
        out.append(line)
    return '\n'.join(out) + '\n'

BIASTEE_1090_CONF = '/etc/systemd/system/readsb.service.d/biastee.conf'

def _opt_in_receiver(text, flag):
    for line in text.splitlines():
        if 'RECEIVER_OPTIONS' in line:
            m = re.search(rf'{flag}[ =]([^\s"]+)', line)
            if m: return m.group(1)
    return None

def parse_sdr1090():
    text = app.HOST.read_text(app.READSB_DEFAULT) or ''
    return {'gain': _opt_in_receiver(text, '--gain') or 'auto',
            'biastee': os.path.exists(BIASTEE_1090_CONF)}

def _set_receiver_flag(text, flag, value):
    """Set/replace `flag value` inside RECEIVER_OPTIONS="...". value None removes it."""
    out = []
    for line in text.splitlines():
        if 'RECEIVER_OPTIONS' in line:
            line = re.sub(rf'\s*{flag}[ =][^\s"]+', '', line)
            if value is not None:
                line = line.rstrip('"').rstrip() + f' {flag} {value}"'
        out.append(line)
    return '\n'.join(out) + '\n'

def write_sdr1090(gain, biastee):
    text = app.HOST.read_text(app.READSB_DEFAULT) or ''
    app.HOST.write_text(app.READSB_DEFAULT, _set_receiver_flag(text, '--gain', gain or 'auto'))
    serial = _opt_in_receiver(text, '--device') or ''
    if biastee:
        if serial:
            pre = (f'/bin/sh -c "for i in 0 1 2 3; do rtl_eeprom -d $i 2>/dev/null '
                   f'| grep -q {serial} && {{ rtl_biast -d $i -b 1; break; }}; done; true"')
        else:
            pre = '/bin/sh -c "rtl_biast -d 0 -b 1; true"'
        app.HOST.run(['mkdir', '-p', os.path.dirname(BIASTEE_1090_CONF)])
        app.HOST.write_text(BIASTEE_1090_CONF, f'[Service]\nExecStartPre={pre}\n')
    else:
        app.HOST.run(['rm', '-f', BIASTEE_1090_CONF])
    app.HOST.run(['systemctl', 'daemon-reload'])

def parse_sdr978():
    text = app.HOST.read_text(app.DUMP978_DEFAULT) or ''
    gain, biastee = 'max', False
    for line in text.splitlines():
        if 'RECEIVER_OPTIONS' in line:
            m = re.search(r'--sdr-gain[ =]([^\s"]+)', line)
            if m: gain = m.group(1)
            if 'biastee=true' in line: biastee = True
    return {'gain': gain, 'biastee': biastee}

def write_sdr978(gain, biastee):
    text = app.HOST.read_text(app.DUMP978_DEFAULT) or ''
    out = []
    for line in text.splitlines():
        if 'RECEIVER_OPTIONS' in line:
            line = re.sub(r'\s*--sdr-gain[ =][^\s"]+', '', line)
            if gain and gain not in ('max', 'auto'):
                line = line.rstrip('"').rstrip() + f' --sdr-gain {gain}"'
            line = line.replace(',biastee=true', '')
            if biastee:
                line = re.sub(r'(--sdr driver=rtlsdr,serial=[^\s",]+)', r'\1,biastee=true', line)
        out.append(line)
    app.HOST.write_text(app.DUMP978_DEFAULT, '\n'.join(out) + '\n')

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

MINI_IDS = {'60a1'}
R2_IDS   = {'60a8', '0002'}

def detect_airspy_model():
    for line in app.HOST.run(['lsusb'], timeout=5).out.splitlines():
        if '1d50' not in line.lower() and 'airspy' not in line.lower():
            continue
        m = re.search(r'1d50:([0-9a-f]{4})', line.lower())
        if m:
            pid = m.group(1)
            if pid in MINI_IDS: return 'mini'
            elif pid in R2_IDS: return 'r2'
        if 'mini' in line.lower(): return 'mini'
    info = app.HOST.run(['airspy_info'], timeout=5)
    text = (info.out + info.err).lower()
    if 'mini' in text: return 'mini'
    elif 'r2' in text or 'r820' in text: return 'r2'
    return 'unknown'

def airspy_live_hint():
    try:
        settings = parse_airspy_options(app.HOST.read_text(app.AIRSPY_DEFAULT) or '')
        model = detect_airspy_model()
        rate  = settings.get('sample_rate', '?')
        gain  = settings.get('gain', '?')
        label = 'Mini' if model == 'mini' else 'R2' if model == 'r2' else ''
        return f'{rate} MSPS{" ("+label+")" if label else ""} · gain {gain} · Beast → :30004'
    except Exception:
        return '6 MSPS · gain 21 · Beast → :30004'
