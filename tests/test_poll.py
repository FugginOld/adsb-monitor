"""Tests for the background poller (system/poll.py): the orchestration engine
deepened out of a single untested while-True loop — see ARCHITECTURE.md
candidate 4 (route-module-split.md-style TDD slices, grilled interactively).
"""
import pytest

import system.poll as poll_mod
from system.poll import (
    _backoff_seconds, _enforce_sdr_presence_step, _poll_once,
    _record_feeder_statuses_step, _sample_metrics_step, background_poll,
)


class _StopAfter:
    """A fake sleep_fn: records every call, raises StopIteration on the Nth
    so background_poll's 'infinite' loop stops on command."""
    def __init__(self, n):
        self.n = n
        self.calls = []
    def __call__(self, seconds):
        self.calls.append(seconds)
        if len(self.calls) >= self.n:
            raise StopIteration


@pytest.mark.parametrize('error_count,expected', [
    (0, 30),
    (1, 60),
    (2, 120),
    (3, 240),
    (4, 300),
    (5, 300),
    (100, 300),
])
def test_backoff_seconds(error_count, expected):
    assert _backoff_seconds(error_count) == expected


# ── _poll_once: runs every step, isolates failures from each other ──────────
def test_poll_once_runs_every_step():
    calls = []
    steps = [lambda: calls.append('a'), lambda: calls.append('b'), lambda: calls.append('c')]
    assert _poll_once(steps) == 0
    assert calls == ['a', 'b', 'c']


def test_poll_once_isolates_a_failing_step_from_the_others():
    calls = []
    def boom():
        raise ValueError('nope')
    steps = [lambda: calls.append('a'), boom, lambda: calls.append('c')]
    assert _poll_once(steps) == 1
    assert calls == ['a', 'c']


def test_poll_once_counts_every_failing_step():
    def boom():
        raise ValueError('nope')
    assert _poll_once([boom, boom, lambda: None]) == 2


# ── _record_feeder_statuses_step: one bad feeder doesn't stop the others ────
def test_record_feeder_statuses_records_every_feeder(monkeypatch):
    feeders = [{'key': 'a', 'kind': 'service'}, {'key': 'b', 'kind': 'service'}]
    recorded = []
    monkeypatch.setattr(poll_mod, 'load_config', lambda: feeders)
    monkeypatch.setattr(poll_mod, 'feeder_status', lambda f: ('ok', 'active'))
    monkeypatch.setattr(poll_mod, 'record_service_event', lambda key, status: recorded.append((key, status)))
    _record_feeder_statuses_step()
    assert recorded == [('a', 'ok'), ('b', 'ok')]


def test_record_feeder_statuses_isolates_one_bad_feeder_then_raises(monkeypatch):
    feeders = [{'key': 'a', 'kind': 'service'}, {'key': 'b', 'kind': 'service'}]
    recorded = []
    def flaky_status(f):
        if f['key'] == 'a':
            raise RuntimeError('feeder a is unreachable')
        return 'ok', 'active'
    monkeypatch.setattr(poll_mod, 'load_config', lambda: feeders)
    monkeypatch.setattr(poll_mod, 'feeder_status', flaky_status)
    monkeypatch.setattr(poll_mod, 'record_service_event', lambda key, status: recorded.append((key, status)))
    with pytest.raises(RuntimeError):
        _record_feeder_statuses_step()
    # feeder 'b' still got recorded even though 'a' blew up
    assert recorded == [('b', 'ok')]


# ── the two thin steps: call straight through, no extra logic to hide bugs ──
def test_enforce_sdr_presence_step_calls_through(monkeypatch):
    calls = []
    monkeypatch.setattr(poll_mod, 'enforce_sdr_presence', lambda: calls.append('called'))
    _enforce_sdr_presence_step()
    assert calls == ['called']


def test_enforce_sdr_presence_step_propagates_failure(monkeypatch):
    def boom():
        raise RuntimeError('sdr guard blew up')
    monkeypatch.setattr(poll_mod, 'enforce_sdr_presence', boom)
    with pytest.raises(RuntimeError):
        _enforce_sdr_presence_step()


def test_sample_metrics_step_calls_through(monkeypatch):
    recorded = []
    monkeypatch.setattr(poll_mod, 'readsb_metrics', lambda: {'aircraft': 3, 'msg_rate': 12, 'max_range_nm': 40})
    monkeypatch.setattr(poll_mod, 'record_metrics', lambda aircraft, msg_rate, max_range_nm: recorded.append((aircraft, msg_rate, max_range_nm)))
    _sample_metrics_step()
    assert recorded == [(3, 12, 40)]


# ── background_poll: the thin infinite driver, steps + sleep_fn injected ────
def test_background_poll_sleeps_30s_between_clean_cycles():
    sleep_fn = _StopAfter(3)
    with pytest.raises(StopIteration):
        background_poll(steps=[lambda: None], sleep_fn=sleep_fn)
    assert sleep_fn.calls == [30, 30, 30]


def test_background_poll_backs_off_on_consecutive_failing_cycles():
    def boom():
        raise ValueError('nope')
    sleep_fn = _StopAfter(6)
    with pytest.raises(StopIteration):
        background_poll(steps=[boom], sleep_fn=sleep_fn)
    assert sleep_fn.calls == [60, 120, 240, 300, 300, 300]


def test_background_poll_resets_backoff_after_a_clean_cycle():
    # fails, fails, then succeeds forever -> backoff should drop back to 30s
    cycle = {'n': 0}
    def sometimes_fails():
        cycle['n'] += 1
        if cycle['n'] <= 2:
            raise ValueError('nope')
    sleep_fn = _StopAfter(4)
    with pytest.raises(StopIteration):
        background_poll(steps=[sometimes_fails], sleep_fn=sleep_fn)
    assert sleep_fn.calls == [60, 120, 30, 30]


def test_background_poll_defaults_to_the_real_steps_when_none_given(monkeypatch):
    # steps=None must build _default_steps() -- prove it by checking the
    # default list shape rather than running real HOST/DB-backed steps.
    calls = []
    monkeypatch.setattr(poll_mod, '_default_steps', lambda: [lambda: calls.append('ran')])
    sleep_fn = _StopAfter(1)
    with pytest.raises(StopIteration):
        background_poll(sleep_fn=sleep_fn)
    assert calls == ['ran']
