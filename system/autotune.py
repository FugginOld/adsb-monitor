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

Config lives in AUTOTUNE_FILE (JSON, next to history.db). Run-time state — the
sample window, the gain it was collected at, the last change time, the last
decision, and a change awaiting verification (with the previous config text
for rollback) — is mirrored to AUTOTUNE_STATE_FILE and restored on the first
poll after a restart, so restarting the monitor, however often, neither throws
away a partly collected window nor forgets to verify (or roll back) a change.
On restore, samples older than one window or collected at a gain the decoder
is no longer running are dropped by the normal checks, never used. A missing
or unreadable state file just means starting a fresh window.
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
WINDOW_SLACK_S   = 120   # extra age a sample may have and still count toward a window

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

def check_config(raw: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
    """Merge `raw` over DEFAULTS and type/range-check it.

    Returns (config, None) or (DEFAULTS copy, error message). Errors are
    returned as fixed strings rather than raised, so routes can show them
    without echoing exception text (CodeQL py/stack-trace-exposure)."""
    cfg = dict(DEFAULTS)
    for key, val in raw.items():
        if key not in DEFAULTS:
            return dict(DEFAULTS), f'unknown setting {key!r}'
        if key == 'enabled':
            if not isinstance(val, bool): return dict(DEFAULTS), 'enabled must be true/false'
            cfg[key] = val
        elif key in _INT_LIMITS:
            lo, hi = _INT_LIMITS[key]
            msg = f'{key} must be a whole number {lo}-{hi}'
            if isinstance(val, bool): return dict(DEFAULTS), msg
            try: n, f = int(val), float(val)
            except (TypeError, ValueError): return dict(DEFAULTS), msg
            if f != n or not lo <= n <= hi: return dict(DEFAULTS), msg
            cfg[key] = n
        else:
            msg = f'{key} must be a number 0-100'
            if isinstance(val, bool): return dict(DEFAULTS), msg
            try: f = float(val)
            except (TypeError, ValueError): return dict(DEFAULTS), msg
            if not 0 <= f <= 100: return dict(DEFAULTS), msg
            cfg[key] = f
    if cfg['min_gain'] > cfg['max_gain']:
        return dict(DEFAULTS), 'min_gain must not exceed max_gain'
    if cfg['strong_low_pct'] >= cfg['strong_high_pct']:
        return dict(DEFAULTS), 'strong_low_pct must be below strong_high_pct'
    return cfg, None

def validate_config(raw: dict[str, Any]) -> dict[str, Any]:
    """check_config, raising ValueError on an invalid config."""
    cfg, err = check_config(raw)
    if err: raise ValueError(err)
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

def update_error(updates: dict[str, Any]) -> str | None:
    """Why `updates` (merged over the saved config) would be rejected, or None."""
    return check_config({**load_config(), **updates})[1]

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

_PENDING_KEYS = {'ts': float, 'old': str, 'new': str, 'prev_text': str, 'new_text': str}

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

    def to_dict(self) -> dict[str, Any]:
        return {'version': 1, 'samples': [[ts, b] for ts, b in self.samples],
                'window_gain': self.window_gain, 'last_change_ts': self.last_change_ts,
                'pending': self.pending, 'last_decision': self.last_decision}

    @classmethod
    def from_dict(cls, d: Any) -> '_State':
        """Rebuild from a saved dict; anything malformed is dropped, not trusted."""
        st = cls()
        if not isinstance(d, dict) or d.get('version') != 1:
            return st
        wg = d.get('window_gain')
        st.window_gain = wg if isinstance(wg, str) and wg in AIRSPY_GAINS else None
        for item in d.get('samples') or []:
            if (isinstance(item, list) and len(item) == 2 and isinstance(item[0], (int, float))
                    and isinstance(item[1], list) and all(isinstance(n, int) for n in item[1])):
                st.samples.append((float(item[0]), list(item[1])))
        if st.window_gain is None:
            st.samples = []
        lc = d.get('last_change_ts')
        st.last_change_ts = float(lc) if isinstance(lc, (int, float)) else 0.0
        p = d.get('pending')
        if isinstance(p, dict) and all(isinstance(p.get(k), t if t is not float else (int, float))
                                       for k, t in _PENDING_KEYS.items()):
            st.pending = {k: (float(p[k]) if t is float else p[k]) for k, t in _PENDING_KEYS.items()}
        ld = d.get('last_decision')
        st.last_decision = ld if isinstance(ld, dict) else None
        return st

_lock = threading.Lock()
_state = _State()
_loaded = False          # state restored from AUTOTUNE_STATE_FILE yet?
_last_saved: str | None = None

def _restore() -> None:
    """Once per process: adopt the saved state. Caller holds _lock."""
    global _state, _loaded, _last_saved
    _loaded = True
    try:
        raw = app.HOST.read_json(app.AUTOTUNE_STATE_FILE)
    except Exception:
        raw = None                              # unreadable state = fresh window
    if raw is None:
        return
    _state = _State.from_dict(raw)
    _last_saved = json.dumps(_state.to_dict(), sort_keys=True)
    if _state.pending:
        logger.info('auto-tune: resuming verification of gain %s after restart', _state.pending['new'])

def _persist() -> None:
    """Write the state file if it changed since the last write. Caller holds _lock.
    Never raises: losing the state file only costs a fresh window."""
    global _last_saved
    text = json.dumps(_state.to_dict(), sort_keys=True)
    if text == _last_saved:
        return
    try:
        app.HOST.write_text(app.AUTOTUNE_STATE_FILE, text + '\n')
        _last_saved = text
    except Exception:
        logger.warning('could not write %s; auto-tune state will not survive a restart',
                       app.AUTOTUNE_STATE_FILE, exc_info=True)

def snapshot() -> dict[str, Any]:
    with _lock:
        if not _loaded:
            _restore()
        return {'status': _state.status,
                'samples': len(_state.samples),
                'window_gain': _state.window_gain,
                'last_decision': _state.last_decision,
                'verifying': _state.pending is not None}

def _reset_state_for_tests(loaded: bool = True) -> None:
    """Fresh in-memory state. loaded=False simulates a monitor restart: the next
    step restores from AUTOTUNE_STATE_FILE."""
    global _state, _loaded, _last_saved
    with _lock:
        _state = _State()
        _loaded = loaded
        _last_saved = None


# ── The poll step ────────────────────────────────────────────────────────────

def _read_stats() -> dict[str, Any] | None:
    s = app.HOST.read_json(app.AIRSPY_STATS)
    return s if isinstance(s, dict) else None

def signal_sample(now: float | None = None) -> dict[str, float] | None:
    """The Airspy's last minute for the metrics table: strong-signal % (the number
    auto-tune acts on), median SNR and noise, and gain. None without fresh stats."""
    s = _read_stats()
    now = time.time() if now is None else now
    try:
        if not s or now - float(s.get('now') or 0) > STATS_MAX_AGE_S:
            return None
        out: dict[str, float] = {}
        buckets = s.get('rssi_buckets')
        if isinstance(buckets, list) and buckets:
            pct, total = strong_pct([int(b) for b in buckets], int(load_config()['strong_rssi_db']))
            if total > 0:
                out['strong_pct'] = round(pct, 2)
        for key in ('snr', 'noise'):
            med = (s.get(key) or {}).get('median')
            if med is not None:
                out[key] = float(med)
        if s.get('gain') is not None:
            out['gain'] = float(s['gain'])
        return out or None
    except (TypeError, ValueError, AttributeError):
        return None

def _stats_gain(stats: dict[str, Any]) -> str | None:
    try:
        g = float(stats.get('gain'))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return str(int(g)) if g.is_integer() else None

def _rollback(st: _State, why: str) -> None:
    p = st.pending
    assert p is not None
    app.HOST.write_text(app.AIRSPY_DEFAULT, p['prev_text'], backup=True)
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
    app.HOST.write_text(app.AIRSPY_DEFAULT, new_text, backup=True)
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
    with _lock:
        if not _loaded:
            _restore()
        try:
            _step(now_fn())
        finally:
            _persist()

def _step(now: float) -> None:
    """One poll's worth of auto-tune. Caller holds _lock."""
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
    # A window is window_min consecutive minutes: drop samples that have aged out
    # (monitor was down, restored from disk, or the poller backed off).
    oldest_ok = ts - (cfg['window_min'] * 60 + WINDOW_SLACK_S)
    st.samples = [(t, b) for t, b in st.samples if t >= oldest_ok]
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
