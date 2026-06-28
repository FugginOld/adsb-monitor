# Ponytail Audit — adsb-monitor

Whole-repo scan for over-engineering. Ranked biggest cut first. Safe set applied — see status column.

## Findings

| # | Status | Tag | What to cut | Replacement | Location |
|---|--------|-----|-------------|-------------|----------|
| 1 | 🚫 wontfix | `yagni` | `InitAdapter` base + `OpenRCAdapter` + `NullAdapter`. | Kept — three real runtime targets (systemd, OpenRC, monitor-only/Null) wired via `detect_init`, documented + tested + a cross-platform PLAN. The interface earns its keep. | [app.py](app.py) |
| 2 | ✅ done | `delete` | `FakeHost` + `FakeInitAdapter` test doubles living in the production module. Only imported by tests. | Moved to `tests/fakes.py`; conftest + test_init_adapter import from there. -45 lines from app.py. | [tests/fakes.py](tests/fakes.py) |
| 3 | ✅ done | `delete` | `get_uptime_history` — 0 callers, dead. | Nothing. | [app.py](app.py) |
| 4 | ✅ done | `delete` | `/api/debug-port` route — not referenced by frontend, ships internal port state. | Nothing. | [app.py](app.py) |
| 5 | ✅ done | `shrink` | `api_uptime_history` re-implemented the `get_uptime_bars` day-loop. | Factored `_daily_uptime`; `get_uptime_bars` derives pcts from it, route is a one-line dict comp. Also dropped the leftover in-function `datetime` import (#7). | [app.py](app.py) |
| 6 | ✅ done | `stdlib` | `import threading as _threading` aliases an already-imported module. | Use the existing `threading.local()`. | [app.py](app.py) |
| 7 | ✅ done | `delete` | `from datetime import datetime` re-imported inside functions — already at top (line 14). | Dropped (2 of 3; the one in #5's `api_uptime_history` left with that deferred item). | [app.py](app.py) |
| 8 | ✅ done | `shrink` | `import zipfile, io` inside `api_backup` — `io` already imported at top. | `import zipfile` only. | [app.py](app.py) |
| 9 | ✅ done | `yagni` | `recommended_sample_rate` — one-line function, one caller. | Inlined `'6' if model == 'mini' else '12'`. | [app.py](app.py) |

## Notes

- **#1 wontfix** — verified the cross-platform goal is real: README documents all three adapters, `detect_init` selects at runtime, OpenRC/Null are tested, and there's a `PLAN.md` for it. Three real implementations behind one interface is exactly when the abstraction is justified, not speculative.

## Bottom line

**net: -160 lines possible, -0 deps.**

## Out of scope (route to a normal review)

- ✅ fixed — `_write_docker` stop/rm/run was destructive on failure with no rollback. Now renames the old container aside, runs the new one, and only drops the backup on success; a failed `docker run` restores and restarts the original. Covered by `test_docker_recreate_rolls_back_on_run_failure`. [app.py](app.py)
