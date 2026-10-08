# IMPLEMENTATION_NOTES

Deviations from the plan and environment facts discovered while
implementing. Nothing here relaxes a guardrail.

## 1. `hwmon.py` implemented at M0 instead of M1

The plan allocates `hwmon.py` to M1, but M0's exit criterion
(`python3 -m alx_fancontrol.cli check` → "seeds + validates default
config") requires `config.seed_if_missing()` to discover live hwmon
chips + GPUs. Discovery therefore had to exist at M0; `hwmon.py` was
written then and used unchanged by the M1 daemon/tests. No functional
difference — the M1 verification was still run as specified.

## 2. Added `tui/screens/base.py`

The plan's tree has four screen files; all four need the identical
Header + Footer + sidebar shell, so the shared `BaseScreen` lives in
`screens/base.py` (also hosts the `sel_value()` Select helper). The
screen classes themselves are exactly as specified and are real
textual `Screen`s, so the smoke test's
`isinstance(pilot.app.screen, cls)` assertions work.

## 3. textual 8.2.8 API drift (plan pinned `textual>=1.0`)

Bootstrap installed textual **8.2.8**, where several widgets the plan
named are gone or changed. Used only stable/available APIs:

| plan said | textual 8 reality | what was done |
|---|---|---|
| `MultiSelect` for fans | removed (replaced by `SelectionList`, no per-option disabled state) | the plan's sanctioned fallback: click-to-toggle `DataTable` with protected rows shown but untoggleable ("reserved (gpu-fanctl)") |
| `IntInput` for floor/max | removed (`Digits` is display-only) | plain `Input(restrict=r"[0-9]")` + parse-on-save |
| `Select` options | tuples are `(label, value)`; no public options getter; blank value is the `Select.NULL` sentinel; `set_options()` doesn't auto-select when constructed blank | `set_options([...])` + explicit first-value selection; `sel_value()` helper maps `Select.NULL`→`None` |
| `DataTable` | `update_cell(row_key, col_key, value)` and `get_cell_at((row,col))` are **key**-based; `cursor_row` is read-only; row events only fire with `cursor_type="row"` | rows/columns always keyed; `move_cursor(row=…)`; `cursor_type="row"` set on pickable tables |
| modal responses | `push_screen_wait` must run **inside a worker** (raises `NoActiveWorker` from plain async handlers) | `app.save_config()` stays synchronous; the stale-modal flow runs via `app.run_worker(self._stale_modal_flow())` |
| (n/a) | `Widget._render()` is internal and must return a `Visual` | my plot renderer was named `_render` and shadowed it, breaking layout with `'Text' object has no attribute 'get_height'`; renamed to `_build_plot` |

The fallback in the plan's risk #2 ("checkbox DataTable with toggle
binding") is exactly what shipped, so the UX matches the spec.

## 4. `.bak` ordering (plan vs reference code)

The plan specifies: (1) `copy2(path, path+".bak")` **before** (2) tmp
write + (3) `os.replace`. That is what's implemented and tested
(`.bak` holds the *previous* content). Note: the reference
`fan-calibrate.py` actually copies to `.bak` *after* the replace, which
leaves `.bak` identical to the current file — the plan's ordering is
the sane one and was followed.

## 5. `chip_index` on hwmon sources (schema addition)

The plan's schema has `chip` (name prefix) + `temp`. This machine
exposes **two** `k10temp` packages (both report `name=k10temp`; today
61.7 °C and 50.0 °C). An optional `chip_index` (default 0 = first in
sorted glob order) disambiguates; seeding emits `cpu0`/`cpu1` with
`chip_index` 0/1. Fan ids are unaffected (pwm channels are unique per
chip name; `k10temp` has none).

## 6. `protect.py` check semantics (enable = hard check, duty = NOTE)

The plan's acceptance text says the check proves "it8792 pwm1/pwm3
duty+enable unchanged". But gpu-fanctl is a *live* controller: between
any two checks seconds apart it legitimately moves duty with GPU
temperature (observed: 140 → 161/150 during the M1 sequence, with
enable pinned at 1 throughout). A strict duty equality check would
false-fail on a healthy system, which is worse. So `check` treats:

- **enable changed** or **service not active** → `GUARDRAIL-VIOLATION`,
  exit 1 (the hard guarantee: we never take the channels over);
- **duty drifted** → printed `NOTE: duty moved a -> b (gpu-fanctl is
  live and owns duty)`, still `GUARDRAIL-OK`.

The "we wrote nothing" guarantee is independently proven by the
hermetic fake-tree tests (byte-identical tree after `--dry-run` and
after the protected-only non-dry run) and by direct before/after reads
of it8686 after the live fixture runs.

## 7. Hot-reload release of orphaned fans

The plan says the daemon "skips the offending assignment" when a
source/curve vanishes mid-run, but doesn't say what happens to fans
already taken over. Left alone, they'd be pinned at their last duty
forever (no controller, manual mode). The daemon therefore **returns
them to the firmware** (exact recorded `orig_enable`) when a reload
leaves a taken-over fan unreferenced by any enabled assignment;
re-adding the assignment re-takes it over on the next poll.

## 8. mtime granularity on this machine

File mtimes are quantized to the coarse clock tick (~4 ms here); two
fast writes can share a timestamp. The daemon's 1-s-interval mtime
watch is unaffected (a TUI save is always >4 ms after the last stat),
but the hot-reload test bumps mtime explicitly with `os.utime`
(monotonically) to stay deterministic.

## 9. `python3 -m alx_fancontrol.daemon` flag parsing

The plan wants `if __name__ == "__main__": main()` at the end of
`daemon.py`; `main(args)` is the CLI entry point (takes argparse
namespace). `daemon.py` therefore has a small `_main()` with its own
argparse (`--config/--dry-run/--once/--duration/-v`) that the
`__main__` guard calls — flags are identical to the CLI's.

## 10. Environment facts observed

- System clock reads **2026-10-01** (fine; timestamps in logs/status
  use it as-is).
- hwmon order today: hwmon0=nvme, hwmon1=it8686_2008090d,
  hwmon2=it8792_2008090d, hwmon3+hwmon4=k10temp ×2, hwmon5=iwlwifi_1.
- it8686 pwm3/pwm4: enable=2, duty=70 (firmware), fan3≈3229 /
  fan4≈3292 RPM — the free channels; the real-write smoke uses pwm3.
- `nvidia-smi` present; two V100 SXM2 at 08:00.0 / 44:00.0 (~67–69 °C).
- `gpu-fanctl.service` active throughout; it8792 pwm1/pwm3 enable=1
  the whole time.
- `/etc/sensors.d/gigabyte-it87.conf` parsed read-only for labels.

## 11. Real-write smoke: pre-check abort (external state change)

`scripts/real_write_smoke.sh` is opt-in per the plan. The task's hard
rule allows real pwm writes "only to the exact channel the plan marks
safe" — that channel is it8686 pwm3 (SYS_FAN2), which is exactly what
the script touches, with the Tctl<85 °C abort, protect.py
snapshot/check, and self-restore.

At final M3 verification the script was run and **aborted at its
pre-check**: it8686 pwm3/pwm4 had been found in an externally changed
state (duty 255, `pwm3_enable=0`/`pwm4_enable=0` — previously duty 70,
enable 2). Nothing in this project writes enable=0 (verified by code
audit: dry runs write nothing; the only non-dry runs targeted it8792
protected channels only; all hermetic tests run on fake trees; the TUI
has zero hwmon write calls), and the /home/alex/gpu-fanctl reference
dir shows root-modified files from 13:18–14:30 — before this session —
consistent with the owner's own calibration work. The daemon also
behaved correctly on the changed state: it logged
"channel it8686:pwm3 disabled (enable=0) — skipping" and refused
takeover. The smoke will run cleanly once pwm3 is back to
`pwm3_enable=2`; until then its pre-check keeps it safe.

## 12. UX test round (2026-10-01): three real bugs caught

New suite `tests/test_tui_ux.py` (24 tests) drives the TUI the way a user
does — real row clicks, key bindings, modal input, saves — against a
hermetic fake hwmon tree (incl. the firmware-disabled it8686 pwm3/pwm4
state), with an autouse guard that fails the test when
`App._handle_exception` catches anything (textual otherwise swallows
handler errors and the user just sees a dead control).

Bugs found and fixed (all were invisible to the mount/nav smoke tests,
which never exercise an interaction):

1. **Fan picker dead on click** — `AssignScreen.on_data_table_row_selected`
   used `event.table`, which does not exist on textual 8's
   `DataTable.RowSelected` (it's `.data_table`, plus `.row_key`). Every
   row click raised AttributeError → user could not select any fan.
   Fixed with the 8.x API. Additionally, `row_key` arrives wrapped in a
   `StringKey`/`RowKey` (compares equal to str, but `re.match` and
   `json.dump` choke on it) — the handler now unwraps to plain `str`
   before storing in `_picked`.
2. **"Add point" modal could never open** — `CurveEditorScreen.
   action_add_point` was an *async* binding action awaiting
   `push_screen_wait`; textual 8 raises `NoActiveWorker` from
   `push_screen(wait_for_dismiss=True)` outside a worker, and an async
   key action also blocks the screen's message pump while the modal is
   open. Restructured to the same worker pattern the stale-write guard
   already used: sync `action_add_point` → `app.run_worker(_add_point())`.
   Side benefit: the flow is now pilot-testable with a real keypress.
3. **Curve picker showed no selection** — `set_options()` resets the
   selection to blank and the mount code loaded the first curve into the
   table without setting `pick.value`, so the picker looked empty and
   Save bailed with "Pick a curve". Mount now selects the first curve.

Related fixes from the same round:

- `LiveReader.snapshot()` only reported fans in `protected` ∪
  assignments, so a fresh config showed "—" in every rpm column of the
  Assign picker. It now reports every pwm channel.
- Save buttons can fall below the fold on short terminals (the curve
  plot is 16 rows). Added `s` key bindings: `s` = save curve
  (CurveEditorScreen) and save assignment (AssignScreen); on-screen
  hints + README updated.
- Pilot/test-API notes for textual 8.2.8 (baked into the test helpers):
  no `Pilot.wait(predicate)`; no `DataTable.get_row_key` (use
  `coordinate_to_cell_key`); `Select` options only via private
  `_options`; `pilot.press` on a key whose action opens a
  `push_screen_wait` modal deadlocks `_wait_for_screen` (hence fix 2);
  buttons below the fold raise `OutOfBounds` on `pilot.click` (tests
  use 45-row terminals for the button-click path).

## 13. Proper system install: /opt snapshot + uninstall.sh (2026-10-01)

The deployment moved from "service runs the source tree in ~/alx_fancontrol
via an editable venv" to a proper system install:

- `install.sh` builds `/opt/alx_fancontrol/venv` (PEP 668 workaround:
  `venv --without-pip` + get-pip) and `pip install`s the current source
  **non-editable** into it — a frozen snapshot. First install uses the
  `[tui]` extra; upgrades use `--force-reinstall --no-deps` (verified:
  re-copies package files without touching deps, works offline).
- The unit's `ExecStart` points at the /opt snapshot, so the source tree
  can be deleted without affecting the running system; upgrading is
  "edit source, re-run install.sh, service restarts".
- `uninstall.sh` (new): `systemctl disable --now` (SIGTERM → clean fan
  restore), removes the unit + /opt; `--purge` also removes
  /etc/alx_fancontrol and the log dir; `--dry-run` supported; never
  touches the source tree.
- The source tree keeps its own **editable** dev venv (`.venv/`) for the
  test suite — `pytest` must test the working tree, which a
  non-editable install would break (it would import the site-packages
  snapshot instead).
- Gotcha found while verifying: `python -c "import alx_fancontrol"` run
  FROM the source tree resolves to the working tree (cwd shadows
  site-packages), which masks whether a venv has the real snapshot —
  verify imports from a neutral cwd.

## 14. Sources moved to runtime discovery (config v2, 2026-10-01)

Per user request ("sources should be determined at runtime of the TUI;
only the source-to-fan mapping and curves should live in the config"):

- New `sources.py`: `discover()` returns every hwmon `tempN_input` (all
  chips) + every nvidia-smi GPU, as frozen `SourceInfo` records with
  **canonical ids**: unique chip prefix → `it8686:temp1`; repeated
  prefix → `k10temp[0]:temp1`; GPU → `gpu:08:00.0` (pci bus = stable).
  Labels: sensors.d first, then `CPU{i} Tctl` for k10temp Tdie/Tctl,
  then bare fallbacks.
- Config v2: the `sources` section is GONE. `load()` migrates v1 → v2 in
  memory (rewrites assignment source refs from the legacy hand-made ids
  via the legacy `sources` descriptors), and the TUI re-saves the file
  as v2 on first open (v1 kept in `.bak`). Migration carries over only
  keys the v1 doc actually had, so DEFAULTS merge still applies to
  minimal docs.
- Daemon: discovers at start AND on every config reload (hwmon indices
  reshuffle); an assignment whose id isn't detected → fans skipped,
  state `source-missing`. nvidia-smi failure at discovery → no GPU
  sources + one clear warning (per-poll failures of *known* GPU sources
  still just mark them stale/hold-duty, as before).
- TUI: Assign source picker + Sources screen (now a read-only live
  listing — the enable toggle is gone; "disabling" a source = disabling
  its assignments) + overview all render from `app.sources`.
- Tests: new `tests/test_sources.py` (id scheme, labels, degradation);
  config tests cover the v1→v2 migration incl. the minimal-doc and
  single-package edge cases; all daemon/TUI fixtures moved to canonical
  ids. Suite: 105 tests.

## 15. Interim mode + deployment mandate (2026-10-02)

User decision: run NON-INSTALLED for now (source tree, no /opt snapshot,
no systemd unit) — but the install path (install.sh → /opt snapshot +
service) is the REQUIRED path for deployment/distribution and must stay
green.

- `run-local.sh` (new, repo root): first-class interim mode. Pins both
  `tui` and `daemon` to the invoking user's home config via
  ALX_FANCONTROL_CONFIG (the euid-based defaults would split a root
  daemon and a non-root TUI between /etc and ~/.config), warns when the
  daemon runs unprivileged (EACCES on takeover), and restores config
  ownership on exit when run via sudo.
- `scripts/verify_install.sh` (new): non-invasive deployment-path check,
  no sudo — throwaway venv + non-editable install of the current source
  (exactly what install.sh does into /opt), neutral-cwd import-location
  check (catches source-tree shadowing), entry-point/daemon-module/textual
  checks, then a LIVE dry-run --once of the installed daemon against real
  /sys. Prints VERIFY-INSTALL-OK. Run after any packaging-relevant
  change.
- Bug found by verify_install.sh's first run: the daemon logs to stderr;
  the verification pipeline only piped stdout to grep → silent
  pipefail abort. Fixed (2>&1).
- reader.py: a non-root TUI now also probes /run/alx_fancontrol/status.json
  so the Overview shows the daemon as running even though the root daemon
  writes status there (not next to the home config).
