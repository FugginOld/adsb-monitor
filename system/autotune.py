"""Airspy gain auto-tune.

A background-poller step that keeps airspy_adsb's fixed gain in the range where
strong signals are not clipping. It never touches anything but the GAIN / G lines
of /etc/default/airspy_adsb, and it is off until enabled from the dashboard.

Signal: airspy_adsb writes stats.json every 60 s with `rssi_buckets` — a 1 dB
histogram of DF17 RSSI. When the gain is too high, strong aircraft hit the ADC
ceiling and pile up in the top buckets. The **strong-signal %** is the share of
DF17s at or above `strong_rssi_db` (the same idea as wiedehopf's autogain, which
uses readsb's `strong_signals` count — readsb can't see that for an airspy fed in
over Beast, so the histogram is used instead).

Loop, one decision per window:
  1. Collect `window_min` distinct stats.json samples (deduped on `now`) at the
     current gain, ignoring the `settle_s` after any change.
  2. Sum the histograms; skip quiet windows with fewer than `min_messages` DF17s.
  3. strong % > `strong_high_pct` → lower gain (3 steps if >= 4x over, else 1);
     strong % < `strong_low_pct` → raise gain 1 step. Anything in between is the
     dead band: leave it alone. Bounded by `min_gain`..`max_gain`.
  4. Write the new gain, restart airspy_adsb, record it in history.db, then
     **verify**: within `VERIFY_TIMEOUT_S` the unit must be active and stats.json
     must report the new gain. If not, the previous file is restored, the decoder
     restarted, the rollback recorded, and auto-tune disables itself.

A sliding window (oldest sample dropped after each no-change decision) means
the next decision only needs one more sample. A manual gain change from the UI
resets the window, and a manual edit during verification cancels the
verification instead of rolling the user's change back.

Config lives in AUTOTUNE_FILE (JSON, next to history.db); run-time state is in
memory and resets when the monitor restarts.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any, Callable

import app
from system.db import record_gain_change
from system.sdr_settings import AIRSPY_GAINS, parse_airspy_options, set_airspy_gain
from system.services import service_action

logger = logging.getLogger(__name__)

UNIT = 'airspy_adsb'
STATS_MAX_AGE_S  = 180   # stats.json older than this is treated as missing
VERIFY_AFTER_S   = 75    # first verification attempt after a change
VERIFY_TIMEOUT_S = 240   # give up and roll back after this long

DEFAULTS: dict[str, Any] = {
    'enabled':         False,
    'min_gain':        0,
    'max_gain':        21,
    'strong_rssi_db':  67,    # RSSI bucket (dB) at/above which a DF17 counts as strong
    'strong_high_pct': 5.0,   # above this → lower gain
    'strong_low_pct':  0.5,   # below this → raise gain
    'window_min':      15,    # stats samples (≈ minutes) per decision
    'min_messages':    3000,  # DF17s needed in a window to decide
    'settle_s':        120,   # ignore stats this long after a change
}
_INT_LIMITS = {'min_gain': (0, 21), 'max_gain': (0, 21), 'strong_rssi_db': (1, 120),
               'window_min': (3, 120), 'min_messages': (0, 10_000_000), 'settle_s': (0, 3600)}
_PCT_KEYS = ('strong_high_pct', 'strong_low_pct')


# ── Config ───────────────────────────────────────────────────────────────────

def validate_config(raw: dict[str, Any]) -> dict[str, Any]:
    """Merge `raw` over DEFAULTS and type/range-check it. Raises ValueError."""
    cfg = dict(DEFAULTS)
    for key, val in raw.items():
        if key not in DEFAULTS:
            raise ValueError(f'unknown setting {key!r}')
        if key == 'enabled':
            if not isinstance(val, bool): raise ValueError('enabled must be true/false')
            cfg[key] = val
        elif key in _INT_LIMITS:
            lo, hi = _INT_LIMITS[key]
            try: n = int(val)
            except (TypeError, ValueError): raise ValueError(f'{key} must be a whole number') from None
            if isinstance(val, bool) or not lo <= n <= hi or float(val) != n:
                raise ValueError(f'{key} must be a whole number {lo}-{hi}')
            cfg[key] = n
        else:
            try: f = float(val)
            except (TypeError, ValueError): raise ValueError(f'{key} must be a number') from None
            if isinstance(val, bool) or not 0 <= f <= 100: raise ValueError(f'{key} must be 0-100')
            cfg[key] = f
    if cfg['min_gain'] > cfg['max_gain']:
        raise ValueError('min_gain must not exceed max_gain')
    if cfg['strong_low_pct'] >= cfg['strong_high_pct']:
        raise ValueError('strong_low_pct must be below strong_high_pct')
    return cfg

def load_config() -> dict[str, Any]:
    raw = app.HOST.read_json(app.AUTOTUNE_FILE)
    if not isinstance(raw, dict):
        return dict(DEFAULTS)
    try:
        return validate_config(raw)
    except ValueError:
        logger.warning('ignoring invalid %s; using defaults (auto-tune off)', app.AUTOTUNE_FILE)
        return dict(DEFAULTS)

def save_config(updates: dict[str, Any]) -> dict[str, Any]:
    cfg = validate_config({**load_config(), **updates})
    app.HOST.write_text(app.AUTOTUNE_FILE, json.dumps(cfg, indent=2) + '\n')
    return cfg


# ── Pure decision logic ──────────────────────────────────────────────────────

def strong_pct(buckets: list[int], threshold_db: int) -> tuple[float, int]:
    """(% of DF17s at/above threshold_db, total DF17s) from a 1 dB histogram."""
    total = sum(buckets)
    if total <= 0:
        return 0.0, 0
    return 100.0 * sum(buckets[threshold_db:]) / total, total

def decide(pct: float, total: int, gain: int, cfg: dict[str, Any]) -> tuple[int | None, str]:
    """New gain (or None to leave it) and a one-line reason."""
    if total < cfg['min_messages']:
        return None, f'not enough traffic to judge ({total} DF17 < {cfg["min_messages"]})'
    if pct > cfg['strong_high_pct']:
        step = 3 if pct >= 4 * cfg['strong_high_pct'] else 1
        new = max(cfg['min_gain'], gain - step)
        if new == gain:
            return None, f'{pct:.1f}% strong signals, but already at minimum gain {gain}'
        return new, f'{pct:.1f}% strong signals > {cfg["strong_high_pct"]:g}% — lowering gain {gain} → {new}'
    if pct < cfg['strong_low_pct']:
        new = min(cfg['max_gain'], gain + 1)
        if new == gain:
            return None, f'{pct:.1f}% strong signals, but already at maximum gain {gain}'
        return new, f'{pct:.1f}% strong signals < {cfg["strong_low_pct"]:g}% — raising gain {gain} → {new}'
    return None, (f'{pct:.1f}% strong signals is within '
                  f'{cfg["strong_low_pct"]:g}–{cfg["strong_high_pct"]:g}% — gain {gain} is fine')

def sum_buckets(samples: list[list[int]]) -> list[int]:
    width = max((len(b) for b in samples), default=0)
    out = [0] * width
    for b in samples:
        for i, n in enumerate(b):
            out[i] += int(n)
    return out


# ── Run-time state ───────────────────────────────────────────────────────────

class _State:
    def __init__(self) -> None:
        self.samples: list[tuple[float, list[int]]] = []
        self.window_gain: str | None = None
        self.last_change_ts = 0.0
        self.pending: dict[str, Any] | None = None   # change awaiting verification
        self.status = 'disabled'
        self.last_decision: dict[str, Any] | None = None

    def reset_window(self, gain: str | None) -> None:
        self.samples = []
        self.window_gain = gain

_lock = threading.Lock()
_state = _State()

def snapshot() -> dict[str, Any]:
    with _lock:
        return {'status': _state.status,
                'samples': len(_state.samples),
                'window_gain': _state.window_gain,
                'last_decision': _state.last_decision,
                'verifying': _state.pending is not None}

def _reset_state_for_tests() -> None:
    global _state
    with _lock:
        _state = _State()


# ── The poll step ────────────────────────────────────────────────────────────

def _read_stats() -> dict[str, Any] | None:
    s = app.HOST.read_json(app.AIRSPY_STATS)
    return s if isinstance(s, dict) else None

def _stats_gain(stats: dict[str, Any]) -> str | None:
    try:
        g = float(stats.get('gain'))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return str(int(g)) if g.is_integer() else None

def _rollback(st: _State, why: str) -> None:
    p = st.pending
    assert p is not None
    app.HOST.write_text(app.AIRSPY_DEFAULT, p['prev_text'])
    ok, out = service_action(UNIT, 'restart')
    reason = f'rollback: {why}' + ('' if ok else f' (restart failed: {out.strip()[:120]})')
    record_gain_change(p['new'], p['old'], reason)
    logger.error('auto-tune %s; restored gain %s and disabled auto-tune', why, p['old'])
    try:
        save_config({'enabled': False})
    except Exception:
        logger.exception('could not persist auto-tune disable')
    st.pending = None
    st.reset_window(None)
    st.status = f'disabled after rollback: {why}'

def _verify(st: _State, now: float) -> None:
    p = st.pending
    assert p is not None
    elapsed = now - p['ts']
    if (app.HOST.read_text(app.AIRSPY_DEFAULT) or '') != p['new_text']:
        st.pending = None                                   # someone else edited it: hands off
        st.status = 'config changed by hand during verification — not verifying'
        return
    if elapsed < VERIFY_AFTER_S:
        st.status = f'verifying gain {p["new"]} ({int(elapsed)} s)'
        return
    unit_ok, unit_state = app.INIT.status(UNIT)
    stats = _read_stats() or {}
    fresh = float(stats.get('now') or 0) > p['ts'] and _stats_gain(stats) == p['new']
    if unit_ok == 'ok' and fresh:
        st.pending = None
        st.status = f'gain {p["old"]} → {p["new"]} verified'
        logger.info('auto-tune: gain %s → %s verified', p['old'], p['new'])
        return
    if elapsed >= VERIFY_TIMEOUT_S:
        why = (f'{UNIT} is {unit_state or "not active"}' if unit_ok != 'ok'
               else f'{UNIT} did not report gain {p["new"]}')
        _rollback(st, f'{why} {int(elapsed)} s after setting gain {p["new"]}')
        return
    st.status = f'verifying gain {p["new"]} ({int(elapsed)} s)'

def _apply(st: _State, text: str, old: str, new: int, reason: str, now: float) -> None:
    new_text = set_airspy_gain(text, str(new))
    app.HOST.write_text(app.AIRSPY_DEFAULT, new_text)
    st.pending = {'ts': now, 'old': old, 'new': str(new), 'prev_text': text, 'new_text': new_text}
    st.last_change_ts = now
    st.reset_window(str(new))
    record_gain_change(old, str(new), reason)
    logger.info('auto-tune: %s', reason)
    ok, out = service_action(UNIT, 'restart')
    if not ok:
        _rollback(st, f'restart failed: {out.strip()[:120]}')
        return
    st.status = f'set gain {new}; verifying'

def autotune_step(now_fn: Callable[[], float] = time.time) -> None:
    now = now_fn()
    with _lock:
        st = _state
        if st.pending is not None:
            _verify(st, now)       # a change in flight is verified even if just disabled
            return
        cfg = load_config()
        if not cfg['enabled']:
            if not st.status.startswith('disabled after rollback'):
                st.status = 'disabled'
            st.reset_window(None)
            return
        text = app.HOST.read_text(app.AIRSPY_DEFAULT)
        if text is None:
            st.status = f'cannot read {app.AIRSPY_DEFAULT}'
            return
        gain = parse_airspy_options(text)['gain']
        if gain not in AIRSPY_GAINS:
            st.status = 'gain is auto — save a fixed gain (0–21) to start auto-tune'
            st.reset_window(None)
            return
        stats = _read_stats()
        buckets = stats.get('rssi_buckets') if stats else None
        try:
            ts = float((stats or {}).get('now') or 0)
        except (TypeError, ValueError):
            ts = 0.0
        if not stats or not isinstance(buckets, list) or now - ts > STATS_MAX_AGE_S:
            st.status = 'waiting for fresh airspy stats.json'
            return
        if _stats_gain(stats) != gain:
            st.status = f'decoder reports gain {stats.get("gain")}, config says {gain} — waiting'
            st.reset_window(gain)
            return
        if st.window_gain != gain:
            st.reset_window(gain)
        if ts - st.last_change_ts < cfg['settle_s']:
            st.status = f'settling after change to gain {gain}'
            return
        if st.samples and st.samples[-1][0] >= ts:
            return                                          # same stats.json as last poll
        st.samples.append((ts, [int(n) for n in buckets]))
        st.samples = st.samples[-cfg['window_min']:]
        if len(st.samples) < cfg['window_min']:
            st.status = f'collecting at gain {gain}: {len(st.samples)}/{cfg["window_min"]} min'
            return
        pct, total = strong_pct(sum_buckets([b for _, b in st.samples]), cfg['strong_rssi_db'])
        new, reason = decide(pct, total, int(gain), cfg)
        st.last_decision = {'ts': now, 'gain': gain, 'strong_pct': round(pct, 2),
                            'messages': total, 'new_gain': new, 'reason': reason}
        if new is None:
            st.status = reason
            st.samples.pop(0)                               # slide: next sample → next decision
            return
        _apply(st, text, gain, new, reason, now)
