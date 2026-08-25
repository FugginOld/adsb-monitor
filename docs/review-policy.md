# Review tooling policy

Read before running any agent review against this repo, and before acting on
its findings. Global `~/.claude/CLAUDE.md` carries the arbitration order; this
file carries what is specific to this repo. Where they conflict, this wins.

Every item below replaced a simpler version that shipped and broke. A finding
that proposes the simpler version back is a defect in the review, not a
finding about the code.

## Scope

**Superpowers** — process default, all code.

**ui-ux-pro-max** — this repo owns its whole frontend, so `--design-system` is
permitted here, unlike a plugin hosted inside another app's shell. Derive it
from `static/index.html` and the existing CSS rather than generating over
them; persist to `design-system/MASTER.md` (not yet generated).

**Ponytail** — `/ponytail-review` on a diff, scoped to `static/` and the
frontend. Not `installer/`, not `system/`, not the deploy scripts, not
`app.py`. Do not run `/ponytail-audit` at all: the deepening pass already
happened here, and what remains in `system/` and `app.py` is precisely the
code a YAGNI scan reads as excess — a compatibility shim full of unused
imports, a three-module split, and a dozen guards that each cost a real
failure to learn.

## Rejected on sight

### Hardware probing

- `system/sdr_presence.py` reads `/sys/bus/usb/devices/*/serial` (`:54`,
  timeout 5). The **presence probe** must never open the device: an
  `rtl_eeprom` loop reset the live 1090 decoder mid-stream, and four opens
  overran the 10s budget, so a replugged 978 stick never auto-resumed.
  (882d6cb, docstring at `sdr_presence.py:47`)

  **`rtl_eeprom` is not banned repo-wide.** It is correct where the decoder
  is stopped: serial assignment at `installer/install-stack.sh:102-103`, and
  the bias-tee `ExecStartPre=` drop-in generated at
  `system/sdr_settings.py:129`, which runs before the unit starts. A finding
  against either of those is reading the scope of this item too broadly.
- Airspy saturation is judged on **p95** RSSI, with max as a fallback only
  when p95 is absent. One aircraft overhead pegs max to ~68 dB at any sane
  gain, making the "decrease gain" nag permanent and unclearable. (6773a3e,
  comment at `system/stats.py:54-57`)
- `RECEIVER_OPTIONS` does **not** set `--raw-port 30978`. The package's own
  `NET_OPTIONS` already binds it; setting it again kills a dual-band install
  with "address already in use". (96f2204, comment at
  `installer/install-stack.sh:237`)
- Container detection pinning `readsb` / `dump978-fa` / `airspy_adsb` to root,
  and the oneshot boot unit reapplying `power/control=on`. LXC has no
  systemd-udevd, so udev rules silently never fire. This broke twice.
  (96f2204, 6ad33f4, pinning at `installer/install-stack.sh:256-264`, the
  boot-unit comment at `:265-267`)
- The 15s wait loop before graphs1090 step [3/6]. graphs1090 enables 978
  graphs only if `/run/skyaware978/aircraft.json` exists *at install time*;
  without the wait, UAT graphs are silently never configured. (058745e)

### Deploy

- `run.py` exists solely as an entry point, and `app.py` has no
  `if __name__ == '__main__'` block. `system/*.py` import `app`, so running
  `app.py` directly executes it twice under two names —
  `ImportError: partially initialized module`. The unit rewrite in `update.sh`
  is part of this fix, not incidental. (e12be32, the "on purpose" comment at
  `app.py:147` and the docstring at `run.py:3`)
- `rm -rf` before `cp -r system`. A plain recursive copy nests a second copy
  inside the existing destination on every deploy after the first: new files
  never land where Python looks, and deleted modules stay behind and get
  imported by accident. Repeated in `install.sh`, `update.sh`,
  `installer/install-stack.sh` and `installer/update-stack.sh`
  deliberately — all four deploy paths. (84791dd)
- `adsb-monitor.service` is generated at install time from the detected IP and
  stays untracked and gitignored. The checked-in version was machine-specific
  and leaked a LAN IP and a personal domain into the repo. (5d7088f)
- No `eval "$(detect_sdr)"`. Values contain spaces; the `printf -v` read loop
  is the fix. (64b6f2d)

### Correctness

- The shared `_journalctl` helper: `stdbuf -oL -eL`, `bufsize=1`, the
  `[connected]` marker, and surfacing a non-zero exit rather than sitting
  idle. Bare `journalctl -f` block-buffers when stdout is not a tty, so quiet
  units' log windows look permanently dead while busy ones work. (06bc660)
- The uptime window seeds with the last event **before** `start`. Without it a
  service that went `ok` 30 days ago and never changed reads 0% on every day
  but the one adjacent to the event. The caller-side `day_start - 86400` hacks
  were part of the bug — do not reintroduce them. Pinned by
  `test_uptime_bars_seed_fills_old_days` in `tests/test_ledger.py`. (69a2efc)
- `_write_docker` renames the container aside, runs, and drops the backup only
  on success — restoring and restarting on failure. stop → rm → run leaves the
  feeder gone with no recovery when `docker run` fails on a bad env or missing
  image. Pinned by `test_docker_recreate_rolls_back_on_run_failure`. (6b1a046)
- Graph backup copies the whole RRD tree. Hardcoding
  `/var/lib/collectd/rrd/localhost` fails wherever collectd stored under the
  real hostname — "no graph data found" on backup. (2ccdb1a)

### Module structure

This section has no fix commits behind it; it is argued in prose in
`ARCHITECTURE.md`, which makes it the easiest thing here to "clean up" and the
most expensive to get wrong.

- `app.py`'s re-export shim. Those F401 imports are the interface the existing
  test suite reaches through (`import app as appmod`), and the scoped
  `per-file-ignores` in `ruff.toml` exists to say so. The shim has already
  been audited once against real references, per ARCHITECTURE.md (13 `from
  system.…` imports remain), so a finding proposing to remove more must name
  the call sites it checked — not cite the lint rule. The 24-name figure is
  stated in ARCHITECTURE.md but no commit backs it.
- A grouped or namespaced shim (`app.db.fold_uptime`) was considered and
  rejected. The flat names cannot be removed, so a namespace layer could only
  be additive: a second access path, not a smaller interface.
- `HOST`, `INIT` and `DB_FILE` stay defined in `app.py`, and every consumer
  does `import app` and dereferences at the call site. `from system.x import
  HOST` gives each importing module its own binding that
  `tests/conftest.py`'s reassignment monkeypatch cannot reach. Moving them into
  `system/init_adapter.py` or `system/db.py` "where they belong" breaks the
  whole fixture layer.
- The three `sdr_*` modules stay separate. The imports form a one-way DAG,
  not a chain: `sdr_presence` imports both `sdr_settings` and `sdr_detect`
  directly (`:23-24`), and `sdr_detect` imports neither. Routing the
  `sdr_detect` edge through `sdr_settings` to make it look like a chain adds
  a hop rather than removing one. Consolidating the three also re-creates a
  module the deploy must delete — see the `cp -r` item above.
- `routes/*.py` is thin HTTP glue and `system/*.py` holds the logic. A finding
  that moves logic into a route is moving it out of the layer that can be
  tested without a request context.

## Do not re-add

- A leaf-name regex in `routes/backup.py`. `[A-Za-z0-9_.-]+\.rrd` backtracked
  polynomially and was redundant against the structural checks that replaced
  it. Hardening this path with a pattern re-opens the scanner alert.
  (3546f9f, #19)

## Documented boundaries

Stated in comments, weaker evidence than the above, but read the comment
before proposing a change:

- The version pill's two-layer gating — feeders with no version source
  otherwise sit on a permanent "checking…". Server-side on `VERSION_SOURCES`
  (`routes/dashboard.py:69`, 08fb138); client-side on `CORE_KEYS`
  (`static/index.html:677`, gated at `:692`, 77af67d). Both layers, not one.
- `get_versions()` never blocks; it returns a possibly-empty cache and
  refreshes in a thread (`system/versions.py:118`)
- `FEEDER_CONFIGS` is deliberately separate from `feeders.ini`
  (`system/config_io.py:40-47`)
- The gain `<select>` replacing a `<datalist>` (2b1f37a) — stated reason is
  finicky autocomplete. UX judgment, not a failure; the weakest item here.

## Not evidence

The ~10 "Potential fix for code scanning alert no. N" commits are
scanner-driven, with nothing showing anything broke in the field. Several are
repeat attempts at the same alert number, which indicates the earlier fixes
didn't satisfy the scanner rather than that anything failed. Do not cite them
as scar tissue, in either direction.

## Cadence and disposal

Review on trigger: installer changes, a new SDR or decoder path, changes under
`system/`, changes to the `app.py` shim, and each release.

Findings become issues or are rejected in the same session. Review reports are
not committed — a retained report is re-read as context by the next pass and
its contents re-reported.

**Known gap:** nothing subprocesses `run.py`, so the entry-point class of bug
(the `__main__` / `import app` collision) has no automated guard. Until that
exists, treat any change to `run.py`, `app.py`'s top level, or a deploy
script's `ExecStart` as needing a real deploy to verify.