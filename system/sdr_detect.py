"""Airspy model detection.

Identifies which Airspy model is plugged in (Mini vs R2) from its USB id, so
the UI can suggest the correct sample rate. No dependency on settings I/O or
the presence guard — both of those depend on this, not the other way round.

`HOST` stays defined in app.py, reached via `import app`.
"""
from __future__ import annotations

import re

import app

MINI_IDS = {'60a1'}
R2_IDS   = {'60a8', '0002'}

def detect_airspy_model() -> str:
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
