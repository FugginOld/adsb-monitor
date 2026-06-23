"""Pure unit tests for fold_uptime — no DB, no Host, just row-lists in / pct out.

A row is (ts, status). Uptime % over [start, end] is the fraction of the span
the service was 'ok', given the events (including any pre-window event that seeds
the starting status).
"""
import app as appmod


DAY = 86400


def test_all_ok_whole_window():
    # one 'ok' event at the very start -> 100%
    rows = [(0, 'ok')]
    assert appmod.fold_uptime(rows, 0, DAY) == 100.0


def test_all_down_whole_window():
    rows = [(0, 'error')]
    assert appmod.fold_uptime(rows, 0, DAY) == 0.0


def test_no_events_is_zero_not_none():
    # empty window with no prior status -> 'unknown' -> counts as down -> 0.0
    assert appmod.fold_uptime([], 0, DAY) == 0.0


def test_half_up_half_down():
    # ok from start, goes error at the midpoint -> 50%
    rows = [(0, 'ok'), (DAY // 2, 'error')]
    assert appmod.fold_uptime(rows, 0, DAY) == 50.0


def test_pre_window_event_seeds_starting_status():
    # the only event is BEFORE the window and is 'ok' -> up the whole window
    rows = [(-100, 'ok')]
    assert appmod.fold_uptime(rows, 0, DAY) == 100.0


def test_event_before_window_then_flip_inside():
    # 'ok' before window, flips to error a quarter in -> 25% up
    rows = [(-100, 'ok'), (DAY // 4, 'error')]
    assert appmod.fold_uptime(rows, 0, DAY) == 25.0


def test_caps_at_100():
    # degenerate rows can't push above 100
    rows = [(-100, 'ok'), (0, 'ok')]
    assert appmod.fold_uptime(rows, 0, DAY) == 100.0


def test_zero_span_returns_none():
    assert appmod.fold_uptime([(0, 'ok')], 5, 5) is None
