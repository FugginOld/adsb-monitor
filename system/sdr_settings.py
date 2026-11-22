"""Airspy / receiver / per-SDR settings — parse, write, and a live hint.

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

No dependency on the presence guard (system/sdr_presence.py) — that module
depends on this one for `_opt_in_receiver`, not the other way round.

`HOST` and the `*_DEFAULT` config paths stay defined in app.py, reached via
`import app`.
"""
from __future__ import annotations

import os
import re
import time
from typing import Any

import app
from system.sdr_detect import detect_airspy_model


# The airspy_adsb unit runs `... $G $GAIN $M $SAMPLE_RATE ...`, so GAIN and
# SAMPLE_RATE only mean anything when the flag variable in front of them is set:
#   G=-g  -> `-g <GAIN>`; airspy_adsb takes 0..21 or `auto`. With G empty, GAIN is a
#            stray positional arg the decoder ignores, and it runs on auto gain.
#   M=-m  -> `-m <SAMPLE_RATE>`; in airspy_adsb 2.x this is the MLAT frequency
#            (12/20/24 MHz) and is Airspy R2 only. `-m` on a Mini (or `-m 6`) makes
#            airspy_adsb exit with AIRSPY_ERROR_INVALID_PARAM, so M must stay empty
#            unless the device is an R2.
AIRSPY_GAINS = tuple(str(i) for i in range(22))
R2_RATES = ('12', '20', '24')

def _shell_vars(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if line.startswith('#') or '=' not in line: continue
        k, _, v = line.partition('=')
        out[k.strip()] = v.strip().strip('"')
    return out

def parse_airspy_options(text: str) -> dict[str, str]:
    """The settings airspy_adsb is *actually* started with, not the raw values:
    gain is 'auto' unless G=-g puts a number in front of -g, and sample_rate is ''
    (decoder default) unless M=-m passes it."""
    v = _shell_vars(text)
    gain = v.get('GAIN', '').lower()
    if v.get('G') != '-g' or gain not in AIRSPY_GAINS:
        gain = 'auto'
    rate = v.get('SAMPLE_RATE', '') if v.get('M') == '-m' else ''
    return {'gain': gain, 'sample_rate': rate, 'options': v.get('OPTIONS', '')}

def _set_shell_vars(text: str, updates: dict[str, str]) -> str:
    """Replace KEY=... lines in place (appending missing keys), keep everything else."""
    done: set[str] = set()
    out = []
    for line in text.splitlines():
        stripped = line.strip()
        key = stripped.split('=')[0].strip() if '=' in stripped and not stripped.startswith('#') else None
        if key in updates:
            if key in done: continue            # drop duplicate definitions
            out.append(f'{key}={updates[key]}'); done.add(key)
        else:
            out.append(line)
    out += [f'{k}={v}' for k, v in updates.items() if k not in done]
    return '\n'.join(out) + '\n'

def set_airspy_gain(text: str, gain: str) -> str:
    """Change only the gain (GAIN + its -g flag); every other line stays byte-for-byte."""
    gain = str(gain).strip().lower()
    if gain not in AIRSPY_GAINS and gain != 'auto':
        raise ValueError(f'gain must be 0-21 or auto, not {gain!r}')
    return _set_shell_vars(text, {'G': '-g', 'GAIN': gain})

def airspy_settings_error(settings: dict[str, Any], model: str = 'unknown') -> str | None:
    """Why airspy_adsb would reject these settings, or None. Returned as a fixed
    string (not raised) so routes can show it without echoing exception text."""
    gain = str(settings.get('gain', 'auto')).strip().lower()
    if gain not in AIRSPY_GAINS and gain != 'auto':
        return 'gain must be 0-21 or auto'
    rate = str(settings.get('sample_rate') or '').strip()
    if model == 'r2' and rate and rate not in R2_RATES:
        return f'sample rate must be one of {", ".join(R2_RATES)} MHz'
    options = str(settings.get('options', ''))
    if '"' in options or '\n' in options:
        return 'options may not contain quotes or newlines'
    return None

def write_airspy_options(settings: dict[str, Any], model: str = 'unknown') -> None:
    """Write gain / sample rate / options. Raises ValueError on a value airspy_adsb
    would reject, so a bad form submit can't crash-loop the decoder."""
    err = airspy_settings_error(settings, model)
    if err: raise ValueError(err)
    existing = app.HOST.read_text(app.AIRSPY_DEFAULT) or ''
    gain = str(settings.get('gain', 'auto')).strip().lower()
    rate = str(settings.get('sample_rate') or '').strip()
    if model != 'r2':
        rate = ''                               # -m is R2-only; never pass it otherwise
    options = str(settings.get('options', '-v -f 1 -w 5 -C 95 -E 60'))
    updates = {'G': '-g', 'GAIN': gain,
               'M': '-m' if rate else '', 'SAMPLE_RATE': rate,
               'OPTIONS': f'"{options}"'}
    v = _shell_vars(existing)
    # The unit crash-loops if any of these is undefined; add defaults only when absent.
    for key, default in {'AFFINITY': '0', 'NET': '"-l 47787:beast -c 127.0.0.1:30004:beast"',
                         'STATS': '"-S /run/airspy_adsb/stats.json"'}.items():
        if key not in v: updates[key] = default
    app.HOST.write_text(app.AIRSPY_DEFAULT, _set_shell_vars(existing, updates), backup=True)

def airspy_live_stats(max_age_s: float = 180) -> dict[str, Any] | None:
    """Gain/sample rate airspy_adsb reports in its stats.json, or None if stale/missing."""
    s = app.HOST.read_json(app.AIRSPY_STATS) or {}
    try:
        if time.time() - float(s.get('now', 0)) > max_age_s: return None
    except (TypeError, ValueError):
        return None
    return {'gain': s.get('gain'), 'sample_rate': s.get('samplerate')}

def parse_receiver_options(text: str) -> dict[str, str]:
    s = {'lat': '', 'lon': '', 'max_range': '500'}
    for line in text.splitlines():
        if 'RECEIVER_OPTIONS' in line:
            for flag, key in [('--lat','lat'),('--lon','lon'),('--max-range','max_range')]:
                m = re.search(rf'{flag}\s+([\d.-]+)', line)
                if m: s[key] = m.group(1)
    return s

def write_receiver_options(text: str, new_settings: dict[str, Any]) -> str:
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

def _opt_in_receiver(text: str, flag: str) -> str | None:
    for line in text.splitlines():
        if 'RECEIVER_OPTIONS' in line:
            m = re.search(rf'{flag}[ =]([^\s"]+)', line)
            if m: return str(m.group(1))
    return None

def parse_sdr1090() -> dict[str, Any]:
    text = app.HOST.read_text(app.READSB_DEFAULT) or ''
    return {'gain': _opt_in_receiver(text, '--gain') or 'auto',
            'biastee': os.path.exists(BIASTEE_1090_CONF)}

def _set_receiver_flag(text: str, flag: str, value: str | None) -> str:
    """Set/replace `flag value` inside RECEIVER_OPTIONS="...". value None removes it."""
    out = []
    for line in text.splitlines():
        if 'RECEIVER_OPTIONS' in line:
            line = re.sub(rf'\s*{flag}[ =][^\s"]+', '', line)
            if value is not None:
                line = line.rstrip('"').rstrip() + f' {flag} {value}"'
        out.append(line)
    return '\n'.join(out) + '\n'

def write_sdr1090(gain: str | None, biastee: bool) -> None:
    text = app.HOST.read_text(app.READSB_DEFAULT) or ''
    app.HOST.write_text(app.READSB_DEFAULT, _set_receiver_flag(text, '--gain', gain or 'auto'), backup=True)
    serial = _opt_in_receiver(text, '--device') or ''
    if biastee:
        if serial:
            pre = (f'/bin/sh -c "for i in 0 1 2 3; do rtl_eeprom -d $i 2>/dev/null '
                   f'| grep -q {serial} && {{ rtl_biast -d $i -b 1; break; }}; done; true"')
        else:
            pre = '/bin/sh -c "rtl_biast -d 0 -b 1; true"'
        app.HOST.makedirs(os.path.dirname(BIASTEE_1090_CONF))
        app.HOST.write_text(BIASTEE_1090_CONF, f'[Service]\nExecStartPre={pre}\n', backup=True)
    else:
        app.HOST.elevated('rm', BIASTEE_1090_CONF)
    app.HOST.elevated('systemctl', 'daemon-reload')

def parse_sdr978() -> dict[str, Any]:
    text = app.HOST.read_text(app.DUMP978_DEFAULT) or ''
    gain, biastee = 'max', False
    for line in text.splitlines():
        if 'RECEIVER_OPTIONS' in line:
            m = re.search(r'--sdr-gain[ =]([^\s"]+)', line)
            if m: gain = str(m.group(1))
            if 'biastee=true' in line: biastee = True
    return {'gain': gain, 'biastee': biastee}

def write_sdr978(gain: str | None, biastee: bool) -> None:
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
    app.HOST.write_text(app.DUMP978_DEFAULT, '\n'.join(out) + '\n', backup=True)

def _fmt_gain(g: Any) -> str:
    try:
        f = float(g)
        return str(int(f)) if f.is_integer() else f'{f:.1f}'
    except (TypeError, ValueError):
        return str(g)

def airspy_live_hint() -> str:
    """What the decoder is really doing (stats.json), falling back to the config."""
    try:
        settings = parse_airspy_options(app.HOST.read_text(app.AIRSPY_DEFAULT) or '')
        model = detect_airspy_model()
        label = 'Mini' if model == 'mini' else 'R2' if model == 'r2' else ''
        live = airspy_live_stats()
        auto = settings['gain'] == 'auto'
        if live:
            rate = f'{live["sample_rate"]} MSPS'
            gain = f'gain {_fmt_gain(live["gain"])}{" (auto)" if auto else ""}'
        else:
            rate = f'{settings["sample_rate"]} MSPS' if settings['sample_rate'] else 'default rate'
            gain = f'gain {settings["gain"]}'
        from system.autotune import load_config   # lazy: autotune imports this module
        tuned = ' · auto-tune' if load_config()['enabled'] else ''
        return f'{rate}{" ("+label+")" if label else ""} · {gain}{tuned} · Beast → :30004'
    except Exception:
        return 'Beast → :30004'
