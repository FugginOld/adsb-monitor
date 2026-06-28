# Ponytail Audit — adsb-monitor

Whole-repo scan for over-engineering. Ranked biggest cut first. Safe set applied — see status column.

## Findings

| # | Status | Tag | What to cut | Replacement | Location |
|---|--------|-----|-------------|-------------|----------|
| 1 | ⏸️ deferred | `yagni` | `InitAdapter` base + `OpenRCAdapter` + `NullAdapter` — one real impl (systemd). Stack is systemd-only; OpenRC's `running_since` already degrades to `None`. | Module-level `systemd_*` functions + a `detect_init` guard. ~90 → ~30 lines. | [app.py:108-201](app.py#L108-L201) |
| 2 | ⏸️ deferred | `delete` | `FakeHost` + `FakeInitAdapter` test doubles living in the production module. Only imported by tests. | Move to `tests/conftest.py`. -45 lines from app.py. | [app.py:71-100](app.py#L71-L100), [app.py:176-191](app.py#L176-L191) |
| 3 | ✅ done | `delete` | `get_uptime_history` — 0 callers, dead. | Nothing. | [app.py](app.py) |
| 4 | ✅ done | `delete` | `/api/debug-port` route — not referenced by frontend, ships internal port state. | Nothing. | [app.py](app.py) |
| 5 | ✅ done | `shrink` | `api_uptime_history` re-implemented the `get_uptime_bars` day-loop. | Factored `_daily_uptime`; `get_uptime_bars` derives pcts from it, route is a one-line dict comp. Also dropped the leftover in-function `datetime` import (#7). | [app.py](app.py) |
| 6 | ✅ done | `stdlib` | `import threading as _threading` aliases an already-imported module. | Use the existing `threading.local()`. | [app.py](app.py) |
| 7 | ✅ done | `delete` | `from datetime import datetime` re-imported inside functions — already at top (line 14). | Dropped (2 of 3; the one in #5's `api_uptime_history` left with that deferred item). | [app.py](app.py) |
| 8 | ✅ done | `shrink` | `import zipfile, io` inside `api_backup` — `io` already imported at top. | `import zipfile` only. | [app.py](app.py) |
| 9 | ✅ done | `yagni` | `recommended_sample_rate` — one-line function, one caller. | Inlined `'6' if model == 'mini' else '12'`. | [app.py](app.py) |

## Notes

- **#1** is tested and was added in a recent commit ("Implement InitAdapter hierarchy"), so it's deliberate. Flagged as the largest speculative surface, not as a clear mistake — keep if cross-platform is a real near-term goal, collapse if systemd is the only target.

## Bottom line

**net: -160 lines possible, -0 deps.**

## Out of scope (route to a normal review)

- `_write_docker` stop/rm/run is destructive on failure with no rollback — correctness, not bloat. [app.py:742-758](app.py#L742-L758)
