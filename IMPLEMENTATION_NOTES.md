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

## 16. `protected` config list removed (2026-10-09)

User decision: the `protected` fan list (built-in default
`["it8792:pwm1", "it8792:pwm3"]`) is gone from the design. Rationale: it
only ever existed to keep the *dev* daemon from stomping on the running
gpu-fanctl, and the takeover guardrail that actually does that — refuse
any channel found in manual mode (`pwmN_enable=1`) at takeover, re-checked
every poll — already covers it. `protected` added nothing except
refusing channels in *firmware* mode (e.g. gpu-fanctl stopped), which is
now deliberately assignable.

- config: field, DEFAULTS entry, validation, seed and v1-migration
  carry-over all removed; legacy `protected` keys in old config files are
  ignored on load and dropped on the next save (v1→v2 migration no
  longer carries the key forward).
- daemon: no `protected` state anymore; such fans surface as
  `manual-conflict` (in manual mode) or are taken over normally
  (firmware mode). `_takeover()` no longer needs the config.
- TUI: the fan picker's `note` column, the "reserved (gpu-fanctl)"
  grey-out/refusal and the sidebar `#prot-hint` block are gone — every
  channel is pickable; the refusal (if applicable) happens in the daemon
  and is visible in Overview state + logs.
- install.sh: the it8792 pwm1/pwm3 `enable=1` post-install check stays
  (it verifies gpu-fanctl still owns its channels), reworded.
- tests: `test_protected_only_changes_nothing` →
  `test_manual_owned_fans_refused_changes_nothing` (same fake tree,
  refusal now via the manual-mode guardrail); `test_click_protected_row_*`
  → clicking a former GPU-blower row selects it; orphan fixture files
  (`tests/fixtures/*.json`, referenced by nothing) deleted.

## 17. Shared fans: highest request wins (2026-11-06)

User decision: a fan controlled by several sources (assignments) must
always be driven to the **highest** duty any of them requests.

Before, this combination did not exist in a sane form:

- `validate()` made a fan in two assignments a **hard error**
  (`fan X is assigned to both A and B`) — `check` failed and the daemon
  refused to start, while the TUI fan picker happily allowed saving such
  a config (save → daemon reload failure → stale-config error in
  `status.json`).
- Had a config gotten through anyway, `_poll` processed assignments in
  sorted-id order and **each one** rate-limited + wrote the fan:
  last-writer-wins, the limiter effectively ran twice per poll, and a
  cold source could drag a shared fan **below** a hot one's request —
  a thermal hazard, not a policy.

Now:

- config: a fan in several assignments is a **warning**, not an error
  (lists all owners + the rule), so start/reload proceed.
- daemon `_poll` is two passes: (1) each enabled assignment computes its
  request (own curve, clamped by own floor/ceiling); (2) per fan, the
  **highest** request wins (ties → lowest assignment id) and the rate
  limiter runs **once** per poll, bounded by the winning assignment's
  `max_rate`. A missing/dead source simply stops contributing — it no
  longer "holds" a fan that other assignments still drive (the
  missing-source log line was reworded accordingly).
- TUI: saving an assignment that shares a fan with another notifies the
  rule ("driven to the HIGHEST requested duty", naming the other owner).
- Tests: `test_shared_fan_across_assignments_is_warning` (config);
  `test_shared_fan_driven_to_highest_request` (order-independent max,
  single rate step per poll, winner switching between polls) and
  `test_shared_fan_dead_source_still_driven_by_live_one` (daemon).

## 18. The "GPU fan stuck at 0 %" safety net (2026-11-15)

Reported symptom: a fan controlled against a GPU temperature fails and gets
stuck at ~0 %, and the card overheats — on every card, not one in
particular. `alx_fancontrol.service` read `active (running)` throughout.

### What the logs showed

```
08:57:44 write it8792:pwm1 =  88%  [GPU 43:00.0 = 69.0 C]
08:58:29 write it8792:pwm1 = 100%  [GPU 43:00.0 = 72.0 C]
09:09:31 WARNING assignment GPU 43:00.0: source gpu:43:00.0 missing — holding last duty
   ... every ~31 s (the n%30 rate limit) until shutdown at 09:52:02
```

GPU 43:00.0 disappeared from `nvidia-smi` output for **43 consecutive
minutes** while the other two cards kept reporting; only a reboot cleared it.
So the trigger is a per-GPU dropout, not an nvidia-smi outage — and the
daemon's answer to that was "hold whatever duty we last wrote".

Two facts about this board make holding (or falling back) fatal:

- firmware control is **not** a safe fallback: an unowned header demonstrably
  sits at `pwm=0` (live `it8686 pwm2 = 0, enable=2`), so "leave it to the
  BIOS" means "stop the fan";
- the only cooling path for those cards is a motherboard header — there is no
  card-side fan (`nvidia-smi` reports `Fan N/A`) and no GPU hwmon chip.

### The five ways a fan ended up stuck (all reproduced, all fixed)

1. **Takeover only from `enable=2`.** A channel found at `1` was refused
   forever as "owned by another process". After any unclean exit (SIGKILL,
   OOM, power blip, `systemctl kill`) the channel stays manual with its last
   raw duty and the daemon never touches it again — including a value of 0.
   → Ownership marker `/var/lib/alx_fancontrol/owned.json` (`StateDirectory=`,
   written on takeover/release with boot id + pid). Same boot + no live
   sibling daemon ⇒ **reclaim**; otherwise still refused, so gpu-fanctl keeps
   its channels. Per-assignment `force_takeover` for headers the BIOS parks
   at `1`/`0`.
2. **Sources discovered only at startup and on config reload.** A GPU absent
   from nvidia-smi at that instant was never adopted — its assignment stayed
   `source-missing` even after the card came back, so its header stayed with
   the firmware (0 %) for the life of the process. `_initial_takeover` also
   skipped fans whose source did not read, i.e. it refused to own the very
   channel that needed rescuing. → Sources are re-discovered **every poll**
   (`_read_all_sources` discovers first, then reads, so a returning GPU is
   used in the same poll), and fans are taken over even when their source is
   absent.
3. **Fail-safe direction was inverted.** Dead source ⇒ *hold last duty* —
   idle duty on this box is 26 % (and the global floor is 5 %). → A dead
   source now *requests* `failsafe_duty` (default 100 %) after
   `failsafe_after` polls, ramping at `failsafe_max_rate`; because requests
   are highest-wins (§17), a dead GPU can no longer be dragged down by a live
   cold source sharing the fan. `"failsafe": false` opts out (warned).
   A GPU reading below `gpu_temp_min_valid_c` (default 15 °C) counts as dead:
   a card off the bus reports 0 °C, which must never read as "ice cold".
4. **`subprocess.run(nvidia-smi)` with no timeout.** When a card falls off the
   bus, nvidia-smi blocks in D state; the poll loop wedged, every fan froze at
   its last duty, and systemd still saw a live process. → `hwmon._run_argv`
   runs with `smi_timeout_s` (default 3), SIGKILLs the child's group and does
   **not** wait for it (waiting would block exactly the same way) — the corpse
   is reaped opportunistically by `reap_children()` on a later poll; a short
   backoff stops hung children piling up. Plus a self-watchdog thread: no poll
   within `watchdog_seconds` (default 30) ⇒ log CRITICAL and `os._exit(70)`
   *without* restoring firmware control (fans keep their duty), systemd
   restarts, the marker reclaims.
5. **A config error at startup meant zero fan control.** `_initial_load`
   exited 2 before any takeover; with `Restart=always` that is a flap loop
   with every header left to the firmware. → **Degraded mode**: recover the
   fan ids from the (invalid) assignments, take them over, hold them at
   `failsafe_duty`, retry the config every second and resume normally when it
   validates. Only a config so broken that no channel can be attributed still
   exits hard.

Also: commanded duty is not proof of cooling — tach RPM was read for display
and never used. Now a controlled fan reading ~0 RPM while commanded
≥ `stall_min_duty` for `stall_after` polls becomes `state: "stalled"` with a
log ERROR and an `errors` entry in status.json (page on that).

### Deliberate choices

- **Restore-exact is preserved.** The marker stores the pre-daemon `enable`
  (not the `1` we wrote), so reclaiming + clean exit still hands back what we
  found — including `0` for a `force_takeover` channel the BIOS had disabled.
- **The watchdog does not restore on the way out.** Its whole job is to avoid
  a frozen fan; returning channels to a firmware that parks them at 0 would be
  worse than dying with them at their last duty.
- **systemd `WatchdogSec=` is left commented in the unit.** Enabling it needs
  `Type=notify`, and if our pings were ever dropped systemd would kill a
  working cooling daemon. The in-process watchdog does the same job with no
  such coupling; `READY=1`/`STATUS=`/`WATCHDOG=1` are sent best-effort
  (`NotifyAccess=main`) so it can be switched on later.
- **Fail-safe respects `max_duty`** (an explicit acoustic ceiling the user
  set) and ignores nothing else.
- TUI assignment save now starts from the existing assignment dict, so
  hand-written safety keys (`failsafe`, `force_takeover`, …) survive an edit
  made in the TUI.

### Tests

`tests/test_safety_net.py` (23 tests): absent-at-start GPU is still owned and
ramps to 100 %; stale reading ramps **up** from idle; returning GPU is
re-adopted without a restart; 0 °C is not a temperature; dead source outranks
a live cold one on a shared fan; per-assignment opt-out (+ warning); reclaim
own-crash / refuse foreign / distrust stale-boot marker / refuse a live
sibling; marker tracks takeover and release; `force_takeover` claims a disabled
channel and restores `0`; `_run_argv` timeout returns promptly and parks the
child; one unanswerable GPU does not black out the others; watchdog exits on a
stuck loop and stays quiet while it beats; degraded mode holds fail-safe then
recovers; unrecoverable config still exits 2 without touching channels;
stalled fan reported / turning fan not reported. Config validation for every
new knob. Suite: **165 passed** (was 145).

## 19. gpu-fanctl removed (2026-11-15)

The second fan controller on this box is gone. `gpu-fanctl.service` had been
inactive since alx_fancontrol took over its headers, but it still shaped the
project: half the safety narrative in the README was "coexists with
gpu-fanctl", `scripts/protect.py` existed only to prove we never stomped on
it, and `install.sh` printed its service state after every install. Carrying
that as live documentation while the service itself is dead is how a guardrail
rots into a comment.

**Why removal, not coexistence.** Two writers on one `pwmN` is exactly the
failure mode §18 was about: last write wins, neither controller can see the
other, and a header can be parked anywhere — including 0. gpu-fanctl also has
none of the safety net (no fail-safe duty, no `nvidia-smi` timeout, no
ownership marker, no watchdog), so leaving it installable is a way to
re-introduce the bug.

**Coverage first.** Verified before deleting anything: `it8792:pwm1`
(GPU 43:00.0) and `it8792:pwm3` (GPU 08:00.0) are assigned and enabled in
`/etc/alx_fancontrol/config.json`, held at `enable=1` with the blowers
spinning, by an active service. Nothing loses cooling.

### scripts/remove_gpu_fanctl.sh

- **Refuses to run** unless that coverage is demonstrable: both channels
  assigned+enabled in the config, `pwmN_enable=1` with a tach reading, and
  `alx_fancontrol.service` active. Abort changes nothing.
- **Archives before deleting**: tarball of every target at
  `/var/backups/gpu-fanctl-<timestamp>.tar.gz`; if archiving fails it does not
  delete. The archive matters — `fan-calibration.json` is the only copy of the
  duty→RPM→temperature sweep, and alx_fancontrol never reads it.
- Removes: unit (stop + disable + `daemon-reload` + `reset-failed`),
  `/usr/local/bin/gpu-fanctl.py`, `/usr/local/bin/fan-calibration.json`, the
  original `~/gpu-fanctl` source tree and the `fanctl-test.*` scratch files.
- **Verifies afterwards**: unit unknown to systemd, files gone, service still
  active, both GPU channels *still* owned with RPM. `--dry-run` prints the
  plan (it is how the target list was confirmed).

### Guardrail inverted: scripts/protect.py

It used to assert "we never changed it8792 enable from 2". The guarantee that
matters now is the mirror image: **the daemon must still hold every channel its
config assigns** — a channel that fell out of manual mode can no longer be
raised by the fail-safe, which is precisely how a blower ended up at 0. The
guarded set is the union of the ownership marker and the config's enabled
assignments, so a channel that should be owned but never was (takeover refused)
is caught as well as one that was lost. Duty drift stays a NOTE; `enable != 1`,
a vanished channel or an inactive service is a GUARDRAIL-VIOLATION. Fan states
from `status.json` are echoed so `failsafe`/`stalled` show up in the same
output. Verified live against the running daemon: six channels, `GUARDRAIL-OK`.

`scripts/real_write_smoke.sh` can no longer use `protect.py check` as its
post-check — it runs with the live service deliberately stopped, so "the daemon
owns its channels" is false by design there. It now (a) refuses to start while
`alx_fancontrol.service` is active, since two daemons writing the same header
is not a test, and (b) compares pre/post snapshots and fails if **any** channel
other than `it8686:pwm3` moved.

### Elsewhere

- `install.sh`: dropped the `gpu-fanctl:` status line; the it8792 check is now
  unconditional ("assigned GPU blowers must be enable=1"), plus a warning when
  nothing in the config assigns the blowers at all — they would be left to a
  BIOS curve that parks headers at 0 on this board.
- Prose in `README.md`, `hwmon.py`, `curve.py`, `assign.py` and the test
  docstrings says "another process" instead of naming gpu-fanctl; the takeover
  rule it illustrated is unchanged. Sections 1–18 of these notes keep their
  mentions — they are a dated record, not current docs.
- Not touched by this change: the running system. Removal happens when you run
  the script with sudo.

Observed while verifying (not part of this change): `it8686:pwm3`/`pwm4` report
`fanN_input=0` while commanded to 7 % — below tach resolution for those
chassis fans, and a reminder that `stall_min_duty` (default 20) must stay well
above the idle floor or the stall detector would cry wolf on every cold boot.

*(Addendum, same day: the removal ran clean — unit unknown to systemd, all
seven targets gone, 41 KB archive at
`/var/backups/gpu-fanctl-20261115-111102.tar.gz`, both GPU channels still
`enable=1` with tach RPM afterwards. `scripts/remove_gpu_fanctl.sh` was then
deleted: a one-shot migration tool that outlives its subject is just another
stale reference, and the tarball is the undo. Code, README, install.sh and the
ops scripts are now clean of the name; sections 1–18 above keep their mentions
because they are dated records of what was true then, and this one records what
is true now: single controller, and `protect.py` asserting that we hold what we
assign.)*

## 20. Ownership re-verification + stall escalation (2026-11-22)

§18 built the safety net and then, three days later, the fault it was written
for happened anyway — with the net up. Live incident, this morning:

```
06:22:15 ERROR FAN it8792:pwm1 STALLED: commanding 52% but tach it8792:fan1 reads 0 RPM for 170 polls
06:25:01 INFO  write it8792:pwm1 = 100% (raw 255)  [GPU 47:00.0 = 73.0 C]
06:30:22 ERROR FAN it8792:pwm1 STALLED: commanding 100% but tach ... 610 polls
06:30:27 INFO  received signal 15 — stopping after this poll      ← box rebooted
```

Eleven and a half minutes (the counter extrapolates back to ≈06:19:07 at the
measured 1.11 s/poll), duty ramped 52 %→100 %, GPU0 50 °C→73 °C — and only a
reboot cleared it, because a boot re-inits the Super I/O. The fan is fine: raw
74 → 1056 RPM today against 1028 RPM in the October calibration sweep, so the
blower and its tach are healthy and correctly paired.

**What the net could see and could not act on.** The stall detector fired, exactly
as designed — and that was the whole of it: an ERROR line, a `status.json`
entry, no consequence. And the reason duty 100 % moved nothing is a hole §18
never closed: `get_enable()` was called at takeover (daemon.py:523) and for
`status.json` (746) — **never in the control loop**. `_write_fan()` writes
`pwmN` and logs success unconditionally. A firmware/EC flip of
`pwmN_enable` to 2 or 0 therefore produced a log full of
`write it8792:pwm1 = 100%` pointing at a header we no longer owned, on a board
that demonstrably parks unowned headers. Commanded duty is not ownership, and
we were treating it as if it were.

### Changed

- `Daemon._verify_channels(cfg)` runs at the top of every poll (before duty is
  computed, so a reclaimed channel gets its real target in the same poll):
  re-read `pwmN_enable` for everything in `_taken_over`; on any value ≠ 1 →
  CRITICAL `OWNERSHIP LOST`, drop from `_taken_over`, **forget the remembered
  duty** (so `rate_limit(None, tgt)` jumps to target instead of crawling up
  from a value the chip never received), re-run `_takeover(insist=True)`,
  escalate. Reclaiming every poll is the point — an EC that steals the header
  every second has to be beaten every second; only the *log* is rate-limited
  (first + every 30th, like `_refuse`).
- `_takeover(fan, insist=True)` claims a channel found `enable=0` **when we are
  the ones who held it**: a 0 there under our ownership is theft, not intent,
  and refusing it means the card cooks while we respect a value the firmware
  chose under us. Foreign-manual channels are still refused — `insist` never
  overrides that, so this cannot stomp a second controller.
- `_reassert_duty()`: if `pwmN` no longer holds what we wrote, write it back
  (WARNING first + every 30th). Without this a second writer's value sticks
  forever, because our own write is skipped whenever the target equals the duty
  *we remember* (`new != self._duty[fan]`).
- `_escalate(cfg, fan, kind, detail, eps)` on both faults: CRITICAL, then
  `stall_alert_command` (env: `ALX_FAN`/`ALX_STATE`/`ALX_DETAIL`/`ALX_DUTY_PCT`/
  `ALX_RPM`/`ALX_CONFIG`, bounded by `stall_alert_timeout_s` through
  `hwmon.run_quiet`) and optional `stall_power_limit_w` →
  `nvidia-smi -i <idx> -pl <W>`, bus→index resolved on the spot via
  `hwmon.gpu_indices()`. Fires on the transition, then every
  `stall_alert_repeat_s` (0 = once per episode).
- `_check_fans` now also reports recovery (`FAN … RECOVERED` + a `recovered`
  alert), which it could not do before: nothing remembered that a fan had ever
  been stalled.
- `ownership-lost` is sticky in `_fan_state`, and pass 2 / `_write_fan` /
  degraded mode were changed to stop overwriting it. Three separate places
  clobbered it while writing these tests — which is the strongest argument for
  the feature existing at all: every layer of the loop concluded "the write
  succeeded, so the fan is fine".
- Degraded mode (invalid config, `cfg = None`) now runs `_verify_channels(None)`
  too, and escalation reads its knobs through `_cfg_num()`, which tolerates a
  None config and junk values. A fault must never be able to raise its way out
  of the loop that is trying to save a fan.
- `status.json`: `errors["ownership-lost:…"]`, `errors["duty-drift:…"]`, and
  `ESCALATED` appended to the stalled entry. systemd `STATUS=` now leads with
  `OWNERSHIP LOST …` above `FAN STALLED`.

### Deliberate choices

- **Episode maps are per fault kind** (`_stall_eps` / `_lost_eps`). They were
  one dict while writing this, and a spinning tach then "recovered" an ownership
  fault we were still losing every poll — under firmware control the fan often
  *does* keep turning, so the alert fired once and the header kept getting
  stolen. There is a test for exactly that.
- **`enable` unreadable ≠ enable lost.** `get_enable()` returning None is a
  sysfs hiccup; writing enable blind would mean guessing who holds the channel.
  Warned first + every 30th, tach alarm still covers the fan.
- **Power capping is opt-in and defaults to off.** Stopping a fan is not our
  call to make either, but throttling somebody's running job is a bigger one.
  Verified this box supports it before writing it: V100-PCIE reports Min/Max
  Power Limit 100 W / 250 W (`nvidia-smi -q -d POWER`), so `-pl 120` is a real
  lever here and not a no-op. A rejected value costs a log line, not the alert.
- **No automatic `systemctl restart` on ownership loss.** Rebooting/restarting
  looked like the cure in the incident, but a restart hands every channel back
  to a firmware that parks them (`_restore_all` → `enable→2`) — trading a
  stalled fan for a window where *all* of them are unowned. Reclaim-in-place is
  strictly safer, and the operator gets told either way.
- **Log retention was NOT changed here.** The four rotating files at
  256 KB × `backupCount=3` hold ~10 hours, which is why "yesterday" could not be
  reconstructed at all: journal unreadable without sudo, `dmesg` locked, no
  metrics store. Worth doing next (`maxBytes`/`backupCount`, or log to the
  journal and read it with `journalctl -u alx_fancontrol`).

### Tests

`tests/test_ownership.py` (14 tests): theft detected + reclaimed + duty
re-asserted in the same poll; reclaim insists on a disabled header we hold; a
header that never comes back keeps `state=ownership-lost` and stays in
`status.json`; loss alerts without falsely reporting recovery; `--dry-run`
detects and writes nothing; foreign duty write re-asserted; stall escalates to
the hook once per episode and reports recovery; repeat interval governs nagging;
ownership fault keeps alerting while a tach spins; power cap fires when asked
and never by default; a fan with no GPU source says so instead of pretending;
degraded mode (`cfg=None`) still verifies; junk escalation values fall back.
Suite: **179 passed** (was 165).

*(Addendum, same day: **the GPU power-cap was removed** — `stall_power_limit_w`,
`Daemon._power_limit_for()`, its call in `_escalate()`, and `hwmon.gpu_indices()`
(which existed only to translate bus→index for `-pl`). It shipped in this PR as
opt-in, default off, guarded by an early return, and never once run on this box
(all four cards still at their 250 W default) — and that was still the wrong
answer. "Off by default" is a promise about the config; it is not the same as
*unable*, and a fan daemon holding a lever into the GPUs' power budget is a
surface that some future config edit, copied config, or well-meant `--set 150`
walks straight into. A fan error should be a fan error: detect it, reclaim the
channel, shout, reach the operator hook — stop. If a fault should also slow a
workload, that response lives in `stall_alert_command`, where whoever wrote it
chose the blast radius and it leaves a log line of its own. The capability is
now asserted *absent*: `test_a_fan_fault_never_reaches_for_the_gpu` runs a full
stall episode and fails if the daemon shells out to anything with no hook
configured, if the knob reappears in `DEFAULTS`, or if `-pl`/`power.limit` shows
up anywhere in `daemon.py`. Suite: **177 passed** (179 − 3 cap tests + 1
capability-absence test). The paragraphs above describe what shipped earlier on
2026-11-22 and are kept as a dated record; this addendum is what is true now.)*

## 21. A stalled fan gets driven, not just described (2026-11-23)

§20 shipped detection and deliberately stopped at the log line: *"more duty
cannot fix a fan that is not turning"*. On this box that sentence cost a card.

### The live failure

2026-11-23, card 0 (V100-PCIE-32GB, PCI `47:00.0`). Its blower's tach went to
**0 RPM at 12:00:19** while commanded 60 %. The daemon escalated **within ten
seconds** — `CRITICAL ESCALATING it8792:pwm1 (stalled)` — and then spent the
next **nineteen minutes** logging `FAN … STALLED` (1,180 polls) at a card it was
asking for 80 % of, because asking was all `_check_fans` did. `stall_alert_command`
was empty, so nobody was told either. GPU 47 stopped answering `nvidia-smi` at
~12:19 (`FAIL-SAFE: … source unreadable for 180 polls`) and the host went down at
12:22–12:23 with it — unclean, no `shutdown` record in `wtmp`.

Detection was never the weak part. The response was: §20 had removed the one
automatic response (the power cap) on the grounds that a response belongs in the
operator hook, and the hook was never configured. What is left when a fan stops
is *the header itself*, and the daemon had stopped touching it.

### Changed

- `Daemon._stall_drive(cfg, fan, seen)` — a poll-driven phase machine over the
  stalled channel: **pin** (`stall_full_duty`, default 100 %) → **kick** (a brief
  `stall_kick_stop_s` at 0 so a jammed blade can swing free, then
  `stall_kick_run_s` at full duty, judged by the tach) → `stall_kick_attempts`
  lunges per round, rounds spaced by `stall_kick_backoff_s` → **cooldown**
  (`stall_kick_cooldown_s` at full after the tach spins) → hand back to the curve.
- Pass 2 of `_poll` defers to `self._kick_duty[fan]` instead of the rate limiter
  while an episode owns the header. The override lives there rather than in
  `_check_fans`, so highest-wins, takeover and `_write_fan`'s logging/ownership
  semantics all still apply to a pinned write.
- `_check_fans` now classifies the tach read (`spinning` / `stopped` / `pulsing`
  / `unknown`) and calls `_stall_drive` every poll; keeps an episode armed
  whatever the duty, and stays quiet about a 0 RPM it caused itself.
- `_lose_channel` drops a pending pulse instead of leaving a 0 in a header it has
  just lost; `_restore_fan` refuses to hand back a header parked below full while
  an episode is live.
- `status.json` gains `fans.<fan>.kick = {phase, attempts}`, so an operator
  looking *during* the fault sees the daemon working on it, not only recording
  that it is not turning.
- `config.py`: seven validated knobs (`stall_full_duty`, `stall_kick`,
  `stall_kick_attempts|stop_s|run_s|backoff_s|cooldown_s`). `validate()` rejects
  a non-bool `stall_kick` (a `"false"` string is truthy, and this is the one
  setting whose default does something physical) and rejects
  `stall_full_duty < stall_min_duty`.

### Deliberate choices

- **The header is the whole blast radius.** This is not §20's power cap coming
  back through a side door: pinning and kicking touch `pwmN` and nothing else.
  `test_a_fan_fault_never_reaches_for_the_gpu` still passes untouched, and
  `test_the_stall_drive_reaches_the_chip_not_the_gpu` re-asserts the invariant
  with the new action in the path — a fault shells out to nothing but the hook,
  and `-pl`/`power.limit` appear nowhere in `daemon.py`. Escalation is unchanged
  and keeps running underneath; what the box does with an airless GPU is still
  `stall_alert_command`'s business.
- **Never parked.** The pulse is the only part of this that can *cause* an
  overheat, so it is fenced three ways: never on a shared header (dropping a fan
  two cards share steals air from the other one — helping a stalled card is not
  licence to hurt a working one), never on a header the firmware currently holds
  (every write there is ignored, so a pulse would be theatre with a 0 in it), and
  never left behind on exit. `stall_kick: false` keeps the pin — which cannot
  cost anything — and turns the pulses off.
- **Nothing sleeps.** Phases advance on the poll loop, each lasting at least its
  configured time and at most one poll more. A kick must never cost the other
  fans more than a poll, for exactly the reason `_alert` bounds its hook.
- **Bounded attempts, repeating rounds.** A seized winding drawing stall current
  forever cooks itself and possibly the fan driver, so a round is capped; but a
  bearing that seizes hot often frees once it stops trying, so the header stays
  pinned and another round starts after `stall_kick_backoff_s`. Giving up
  permanently would mean the next dropout is met with the log line that killed
  card 0.

### Two bugs the tests caught (both real, both pre-fix-of-the-fix)

1. Suppressing the alarm during our own pulse also suppressed **phase
   advancement** — a fan would sit at 0 forever, which is precisely the failure
   mode the feature exists to cure. Fixed by classifying the tach read instead of
   conflating "ours" with "not stopped": `pulsing` advances the clock and only the
   *alarm* stays quiet.
2. `_restore_fan` originally compared **its own** `kick_duty` against full, but a
   phase can advance in the same poll that wrote the pulse — so the 0 was only
   ever in the chip, and an exit at that instant handed firmware a parked header.
   Fixed by reading `pwmN` back and comparing the chip.

### Tests

`tests/test_stall_kick.py` (15 tests): pinned past what the curve asked; the pin
jumps rather than crawling at `max_rate`; the runtime clamp under
`stall_min_duty`; a pulse that really lands on the chip in the right order;
attempts bounded and rounds spaced; kick off still pins and never touches 0;
restart holds full then hands back without a cliff; an exit inside a pulse never
leaves the header parked (and asserts the test really landed inside one); a
shared header pinned but never pulsed; no theatre on a firmware-held header;
validate rejects `full < min` and a non-bool `stall_kick`; `cfg=None` junk still
pins; `status.json` reports the fight.

New helper `run_clocked()`: kick phases are timed in seconds, so these tests drive
a virtual clock (each sleep advances `time.monotonic()` by `step`) instead of
racing the runner — same hermetic contract as `test_safety_net.run_polls`.

Suite: **192 passed** (177 + 15).

---

## 22. Config↔GUI audit, step 1: the four things the TUI got wrong (2026-11-23)

An audit of every option the daemon honours against what the TUI can show and
change (`CONFIG_GUI_AUDIT.md`) found the mapping side (source → curve → fans)
in good shape and the policy side absent: **22 global `config.*` keys, none of
them viewable or editable in the GUI** — the fail-safe, liveness, escalation
and stall-drive knobs added by §18–§21 all landed JSON-only. Four of the
findings were not taste but bugs, reproduced by driving the real TUI against
the synthetic machine and reading the file back. This step fixes those four;
the Settings screen (`config.*`) and the tach/ownership work are deliberately
left for their own steps.

### The four bugs

1. **A blank override field did nothing.** `save_assignment` started from
   `dict(existing)` and only ever *set* `floor_duty` / `max_duty` / `max_rate`
   when the field was filled, so clearing the field left the old override in
   the file: "follow the global default again" was not expressible, and typing
   the global's number instead pins it per assignment — a different setting
   that stops following the global.
2. **`enabled` was displayed and never editable.** The list has had an on/off
   column since the beginning and the save path carefully preserved it, but no
   button or key ever changed it — so the action `config.py`'s own comments
   recommend ("prefer deleting/disabling such an assignment") required hand
   editing JSON, while the wrong tool (Delete) was the one on screen.
3. **Renaming duplicated the assignment.** The name field is the key, so
   loading `CPU`, retyping it as `CPU-box` and saving wrote a second entry:
   both then drove `it8686:pwm1`, and the only signal was a generic "shared
   fan" notice that never said the old one still existed.
4. **Invalid configs were saved with a smile.** `save_config` never called
   `config.validate()`. Delete every point of a curve, press save: notification
   *"Curve 'default' saved"*, file now invalid, and because
   `Daemon._maybe_reload` keeps the **previous** config on any error, nothing
   at all happened — silently. For a tool that moves fans, "Saved" followed by
   silence is the worst failure mode it can have.

### Changed

- `AssignScreen._commit(name, form, base=None, drop=None, message=None)` — the
  single write path for the form, with the merge rules written down: a blank
  floor/ceiling/rate **removes** the key; keys the form does not manage
  (`failsafe`, `failsafe_duty`, `failsafe_after`, `force_takeover`) are carried
  over; `tach` keeps explicit mappings for fans still picked and drops entries
  whose fan left the assignment (they used to linger forever).
- `AssignScreen._rename_flow` — a name change while editing asks (or, if the
  target name is taken, asks about *overwriting*), then moves the entry
  (`drop=old`) instead of adding one. Cancel leaves the config untouched.
- `Switch(id="enabled")` in the form, plus an **Enable/disable** button and the
  `e` binding acting on the selected list row. Disabling confirms — it hands
  fans to the firmware mid-run; enabling does not, it only reclaims.
- `FanControlApp.save_config` validates before touching the file and shows
  `ConfigErrorsScreen` with the reasons. The screen that created the problem is
  usually one keystroke away, so this replaces a round trip through
  `status.json`.
- `ConfirmScreen` — the delete confirmation generalised (title, body lines,
  button label/variant/id, Enter/Escape); `ConfirmDeleteScreen` is now a thin
  subclass so its tests and ids are unchanged.
- Placeholders read the live globals (`floor % (global 5, blank = use it)`)
  instead of hard-coding "default 5", which would start lying the first time
  someone changed `config.floor_duty`.

### Deliberate choices

- **Validation blocks, including for global keys.** An out-of-range
  `config.*` value can only be fixed in JSON until the Settings screen exists,
  so there is a temptation to let assignment edits through. Not so: the daemon
  rejects the *whole file* for any error, so saving "just my fan edit" would
  write a file that changes nothing — exactly the lie this fix exists to stop.
  The modal therefore names the offending key and points at the file.
- **Blank means remove, not zero.** `floor_duty: 0` is a real setting (a fan
  allowed to stop); an absent key means "inherit". Collapsing the two would
  lose the distinction the daemon makes (`a.get("floor_duty", cfg.config…)`).
- **Disable keeps everything.** Only `enabled` flips. That is the point of
  offering it as the alternative to Delete: the curve, floor and tach mapping
  are what make re-enabling safe later.
- **Confirm one direction only.** Every confirm on this screen costs a
  keystroke, so only the direction that gives up control (disable, delete) and
  the one that can silently clobber another assignment (rename onto an existing
  name) ask.

### Tests

13 new UX tests in `tests/test_tui_ux.py`, all driving widgets rather than
internals: blank fields remove all three overrides and leave unmanaged keys
alone; placeholders track the globals; the form switch writes
`enabled=false` (list cell flips to `off`); `e` disables behind a confirm and
enables without one; cancel keeps `enabled=true`; toggle with no selection
warns; rename moves the entry (and preserves its floor), cancel keeps the old
name, rename onto a taken name overwrites and removes the source; an invalid
config blocks a fan edit with the reason and no "Saved" notice; an empty curve
cannot be saved; a bad *global* blocks too and points at the config path.

`wait_not_modal` now waits on `ModalScreen` generally instead of a hard-coded
tuple of modal classes — it had to be taught about every new modal before.

Suite: **205 passed** (192 + 13).

---

## 23. Config↔GUI audit, step 2: curves become manageable (2026-11-23)

The curve was the one object the TUI could edit but not create, copy, rename
or delete — and, as step 1 showed, it could be bricked in three keystrokes.

### Why this mattered

- **No New.** Adding a curve for new hardware meant editing JSON. Curves are
  tuned by copying one and shifting it ("same as the V100 blower, 5 °C
  later"), and that path did not exist in the GUI at all.
- **No first point.** `action_add_point` demanded a *selected existing*
  point, so an empty curve could never be filled — the dead end the audit
  proved with `points: []` + `a` → "Select a point row first", the only
  button on the screen being Save.
- **No Delete, and no idea whether a curve was in use.** That missing
  information is exactly what makes deletion dangerous: `_poll` skips an
  assignment whose curve is gone (`fan_state = "curve-missing"`) and its
  fans sit at their last duty. A delete button without a usage count would
  have been worse than no button.

### Changed

- `NewCurveModal` asks for **id + label**. The id is asked rather than
  invented because assignments reference curves by it, and an id already in
  use is refused *in the modal* — silently overwriting a curve another fan
  depends on is not an acceptable outcome of a typo. Allowed ids are
  `[A-Za-z0-9_-]+` (they are config keys and Select values).
- **New** seeds the built-in default shape
  (`config.DEFAULTS["curves"]["default"]["points"]`) and saves immediately:
  a curve with no points is invalid, so seeding keeps the config valid at
  every moment, and it gives `a` something to insert between right away.
- **Copy** duplicates the curve *as currently edited*, unsaved point moves
  included — that is the point of copying. Suggested id `<id>-copy` (then
  `-copy-2`, … via `_free_id`), label `<label> copy`.
- **Delete** removes an unused curve after confirmation; a curve with users
  is refused with the assignment names in the message. A new hint line
  (`#curve-used`) shows `used by: CPU, System` / `not used by any
  assignment` for the selected curve, so the rule is visible before anyone
  reaches for the button. The two **built-in** curves are refused too — see
  F14 below.
- **Label** is now editable (`#curve-label`) and written by the same Save as
  the points; the picker options are rebuilt afterwards, since they display
  labels and a rename that did not appear there would look like it failed.
- `a` on an **empty** curve opens the prompt for its first point (40 °C /
  20 %) instead of refusing. The "Select a point row first" warning stays as
  the fallback for the state where points exist but none is selected.
- `_refresh_curve_options(keep=…)` replaces the inline option building in
  `on_mount`, so create/copy/delete/save all rebuild the picker and re-assert
  a selection that still exists (`set_options()` resets it to blank).
- `ConfirmScreen` moved from `assign.py` to `base.py` — it is now used by two
  screens, and screens importing each other is not a dependency worth
  creating. `assign.py` re-imports it, so `ConfirmDeleteScreen` and every
  existing import path are unchanged.

### F14, found on the way: deleting a built-in curve cannot stick

While testing Delete: `config.load()` does `_deep_merge(DEFAULTS, doc)`, so
`default` and `gpu_v100` are injected into every config that does not carry
them — a file with `{"curves": {"mine": …}}` loads as
`[default, gpu_v100, mine]`, and the next save writes them back. Deleting one
in the TUI would therefore vanish on the next load (daemon hot-reload or TUI
restart): a button that quietly does nothing.

Refusing it, with the reason, is the honest step-2 behaviour. The real fix
belongs with step 4's schema work: `DEFAULTS` should seed a config **once**
(`seed_if_missing`, which already does exactly that) instead of being merged
on every load — at which point Delete can allow the built-ins. Changing the
merge now was rejected: a hand-written config that references `default`
without defining it works today (implicitly, and arguably wrongly), and under
replace-semantics that assignment would start failing validation and be
skipped at runtime — fans holding their last duty is not a regression to take
for the sake of a tidier curve list.

### Deliberate choices

- **Create writes immediately.** The alternative (a pending curve that only
  exists in memory until Save) means the config and the screen disagree, and
  a curve is harmless once seeded. Everything else on this screen still needs
  an explicit Save, because points are an *edit*.
- **Delete refuses instead of re-pointing.** "Delete and move its assignments
  to another curve" sounds convenient but silently changes how several fans
  cool at once. Re-pointing is one visible edit per assignment on the Assign
  screen — which is the right cost for a change that moves air.
- **Renaming an id is not offered.** A label rename is display-only; an id
  rename would have to rewrite every assignment referencing it. That is a
  different operation (and step 3's territory: it belongs next to the tach
  and ownership edits), not a text field on a curve screen.
- **The action row is its own line.** Sharing it with the picker collapsed the
  Select to 1 cell (`width: 1fr` next to auto-width buttons does not behave as
  one expects), so the picker kept its full-width row and New/Copy/Delete got
  their own.

### Tests

9 new tests (net +8: `test_add_point_requires_selected_row` asserted the
behaviour this step deliberately reverses, so it became the empty-curve
prompt test): `a` on an empty curve prompts and the curve then
saves; delete-every-point-then-rebuild end to end (the F7 regression);
New creates, selects and persists with the seeded shape; a taken id is
refused with the file untouched; Copy carries unsaved moves and leaves the
original alone; Delete refuses an in-use curve by name (and the hint line
says `used by: CPU`); Delete removes an unused one after confirmation and the
picker lands on a curve that still exists; label rename persists and shows up
in the picker options; Delete refuses a built-in curve without touching the
file (F14).

Suite: **213 passed** (205 + 8).
