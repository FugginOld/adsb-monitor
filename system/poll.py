"""Background poller.

A daemon thread (started in run.py) that runs forever: every 30s it records
each service's status into the events ledger and samples readsb metrics
into the metrics table. This is what populates the uptime history and
sparklines over time, independent of whether anyone has the dashboard open.

Deepened into three parts so the orchestration itself is testable, not just
each step in isolation (see ARCHITECTURE.md's background-poller candidate):
  - the three real steps (SDR presence, feeder status recording, metrics
    sampling) are plain zero-arg callables — no class hierarchy, since
    nothing ever swaps in a different *kind* of step, only a different HOST
    underneath one (which FakeHost already covers);
  - `_poll_once(steps)` runs a list of steps once, isolating each step's
    failure from the others, and reports how many failed;
  - `background_poll(steps, sleep_fn)` is the thin infinite driver — steps
    and the sleep function are both injectable, so a test can hand it a
    `sleep_fn` that raises after N calls to end the loop on command and
    assert the exact backoff sequence it was called with.

All three steps are treated uniformly for backoff purposes: previously SDR
presence and per-feeder failures were silently swallowed and never affected
backoff (only `load_config()`/metrics failures did) — that was an inconsistency,
not a deliberate design, so this now backs off on any step failing.
"""
import time

from system.config_io import load_config
from system.db import record_metrics, record_service_event
from system.feeders import feeder_status, readsb_metrics
from system.sdr_presence import enforce_sdr_presence


def _enforce_sdr_presence_step():
    enforce_sdr_presence()

def _record_feeder_statuses_step():
    """Record every feeder's status. One feeder failing doesn't stop the
    others from being recorded, but is still surfaced once so the poller
    backs off like any other step failure."""
    any_failed = False
    for f in load_config():
        try:
            status, _ = feeder_status(f)
            record_service_event(f['key'], status)
        except Exception:
            any_failed = True
    if any_failed:
        raise RuntimeError('one or more feeders failed to record status')

def _sample_metrics_step():
    m = readsb_metrics()
    record_metrics(m['aircraft'], m['msg_rate'], m['max_range_nm'])

def _default_steps():
    return [_enforce_sdr_presence_step, _record_feeder_statuses_step, _sample_metrics_step]

def _poll_once(steps):
    """Run each step once, isolating failures from each other.

    Returns how many steps raised.
    """
    error_count = 0
    for step in steps:
        try:
            step()
        except Exception:
            error_count += 1
    return error_count

def _backoff_seconds(error_count):
    """30s normal, doubling per consecutive bad cycle, capped at 5min."""
    return min(30 * (2 ** min(error_count, 4)), 300)

def background_poll(steps=None, sleep_fn=time.sleep):
    """Record service states and metrics to SQLite every 30s (real steps by default)."""
    if steps is None:
        steps = _default_steps()
    consecutive_errors = 0
    while True:
        consecutive_errors = consecutive_errors + 1 if _poll_once(steps) else 0
        sleep_fn(_backoff_seconds(consecutive_errors))
