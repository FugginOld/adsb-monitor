"""Airspy signal analysis, system metrics, readsb deep stats, per-band SDR stats.

Reads airspy_adsb's live stats.json and turns the raw SNR / noise / RSSI
numbers into a plain-English gain recommendation (raise, lower, or leave
it). Saturation is judged on the 95th-percentile RSSI rather than the
single loudest sample, because one aircraft directly overhead pegs the max
at any sane gain and would otherwise nag forever.

Host vitals for the dashboard's system panel — CPU %, memory, disk, load,
temperature, uptime — read via psutil (with a fallback for CPU temp, which
psutil can't always see on a Pi).

Parses readsb's own stats.json (aircraft seen, message rate, peak range,
strong-signal %, per-type breakdown) for the detailed receiver-stats panel.

One status block per receiver band for the sidebar. 1090 comes from
readsb's stats.json local block (signal/noise in dBFS -> RSSI/SNR/noise)
and aircraft.json; 978 comes from skyaware978's aircraft.json (per-aircraft
RSSI, message count). dump978 exposes no noise floor, so the 978 block has
RSSI but no SNR/noise.

`HOST` and the `*_DEFAULT`/`*_JSON` config paths stay defined in app.py,
reached via `import app`.
"""
import os

import psutil
import shutil

import app
from system.services import systemd_status


def get_airspy_stats():
    return app.HOST.read_json(app.AIRSPY_STATS) or {}

def gain_recommendation(stats):
    """Analyze airspy stats and suggest gain adjustment."""
    if not stats:
        return None
    snr = stats.get('snr', {})
    noise = stats.get('noise', {})
    rssi = stats.get('rssi', {})
    gain = stats.get('gain', 0)

    median_snr   = snr.get('median', 0)
    median_noise = noise.get('median', 0)
    p95_rssi     = rssi.get('p95', 0)
    max_rssi     = rssi.get('max', 0)

    # Judge saturation on the 95th-percentile RSSI, not the single loudest sample:
    # one aircraft overhead pegs max RSSI to ~68 dB at any sane gain, so a max-based
    # check nags forever and lowering gain never clears it. Fall back to max only if
    # the feed doesn't report p95.
    sat_rssi = p95_rssi or max_rssi
    if sat_rssi >= 67:
        return {'action': 'decrease', 'reason': f'Strong signals near saturation (RSSI {sat_rssi:.0f} dB) — try gain {max(0, int(gain)-2)}'}
    elif median_snr < 8:
        return {'action': 'increase', 'reason': f'Low median SNR ({median_snr:.1f} dB) — try gain {min(21, int(gain)+2)}'}
    elif median_noise > 45:
        return {'action': 'decrease', 'reason': f'High noise floor ({median_noise:.0f} dB) — try gain {max(0, int(gain)-2)}'}
    else:
        return {'action': 'ok', 'reason': f'Signal levels look good (SNR {median_snr:.1f} dB, noise {median_noise:.0f} dB)'}

def get_system_metrics():
    metrics = {}
    try:
        metrics['cpu_pct']  = psutil.cpu_percent(interval=0.5)
        metrics['mem_pct']  = psutil.virtual_memory().percent
        metrics['mem_used'] = round(psutil.virtual_memory().used / 1024 / 1024)
        metrics['mem_total']= round(psutil.virtual_memory().total / 1024 / 1024)
        temp = psutil.sensors_temperatures() or {}
        for _key in ('cpu_thermal', 'coretemp', 'k10temp', 'acpitz', 'cpu-thermal', 'soc-thermal'):
            if _key in temp:
                metrics['cpu_temp'] = round(temp[_key][0].current, 1)
                break
        du = shutil.disk_usage('/')
        metrics['disk_pct']  = round(du.used / du.total * 100, 1)
        metrics['disk_used'] = round(du.used / 1024 / 1024 / 1024, 1)
        metrics['disk_total']= round(du.total / 1024 / 1024 / 1024, 1)
    except Exception as e:
        metrics['error'] = str(e)
    return metrics

def get_readsb_deep_stats():
    try:
        stats = app.HOST.read_json(os.path.join(app.READSB_JSON, 'stats.json'))
        if not stats:
            return {}
        last  = stats.get('last1min', {})
        total = stats.get('total', {})
        return {
            'msgs_last_min':     last.get('messages_valid', 0),
            'positions_last_min':last.get('position_count_total', 0),
            'msgs_total':        total.get('messages_valid', 0),
            'positions_total':   total.get('position_count_total', 0),
            'adsb_pct': round(
                last.get('position_count_by_type', {}).get('adsb_icao', 0) /
                max(last.get('position_count_total', 1), 1) * 100, 1
            ),
            'uptime_hrs': round((total.get('end', 0) - total.get('start', 0)) / 3600, 1),
        }
    except Exception:
        return {}

# Cumulative UAT message count from the last poll, to derive a per-second rate.
# ponytail: single-value cache, fine for one monitor process; no locking needed.
_uat_msg_cache = {'msgs': None, 'now': None}

def _uat_msg_rate(msgs, now):
    if msgs is None or now is None:
        return None
    prev_m, prev_t = _uat_msg_cache['msgs'], _uat_msg_cache['now']
    _uat_msg_cache['msgs'], _uat_msg_cache['now'] = msgs, now
    if prev_m is None or prev_t is None or now <= prev_t:
        return None
    return round((msgs - prev_m) / (now - prev_t))

def get_band_stats():
    # has_rtl1090: readsb is driving an RTL dongle, not just aggregating an Airspy
    # feed (the Airspy path runs readsb with --net-only and has no local SDR/signal).
    readsb_text = app.HOST.read_text(app.READSB_DEFAULT) or ''
    has_rtl1090 = '--device-type rtlsdr' in readsb_text
    out = {
        'has_airspy':  os.path.exists(app.AIRSPY_DEFAULT),
        'has_rtl1090': has_rtl1090,
        'has_978':     os.path.exists(app.DUMP978_DEFAULT),
        'b1090': None, 'b978': None,
    }
    uat_count = 0
    if out['has_978']:
        sky = app.HOST.read_json(app.SKYAWARE978_JSON) or {}
        acs = [a for a in sky.get('aircraft', []) if a.get('seen', 999) < 60]
        uat_count = len(acs)
        rssis = [a['rssi'] for a in acs
                 if isinstance(a.get('rssi'), (int, float)) and a['rssi'] > -49.4]
        out['b978'] = {
            'active':   systemd_status('dump978-fa')[0] == 'ok',
            'aircraft': uat_count,
            'rssi':     round(sum(rssis) / len(rssis), 1) if rssis else None,
            'msg_rate': _uat_msg_rate(sky.get('messages'), sky.get('now')),
        }
    if has_rtl1090:
        stats = app.HOST.read_json(os.path.join(app.READSB_JSON, 'stats.json')) or {}
        local = stats.get('last1min', {}).get('local', {})
        signal, noise = local.get('signal'), local.get('noise')
        ac = app.HOST.read_json(os.path.join(app.READSB_JSON, 'aircraft.json')) or {}
        total_ac = len([a for a in ac.get('aircraft', []) if a.get('seen', 999) < 60])
        out['b1090'] = {
            'active':   systemd_status('readsb')[0] == 'ok',
            'aircraft': max(0, total_ac - uat_count),  # readsb json merges 978 in
            'msg_rate': round(stats.get('last1min', {}).get('messages_valid', 0) / 60),
            'rssi':     round(signal, 1) if isinstance(signal, (int, float)) else None,
            'noise':    round(noise, 1) if isinstance(noise, (int, float)) else None,
            'snr':      round(signal - noise, 1)
                        if isinstance(signal, (int, float)) and isinstance(noise, (int, float)) else None,
        }
    return out
