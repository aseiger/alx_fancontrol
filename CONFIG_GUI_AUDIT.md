# Config ↔ GUI audit — alx-fancontrol

**Scope:** every option the daemon honours (`config.*`, `assignments.*`,
`curves.*`) vs. what the TUI lets you **view** and **edit**.
**Method:** read of `config.py` (the model), `daemon.py` (what is actually
read at runtime), all four TUI screens plus `base.py`/`app.py`, plus 7
throwaway probes that drove
the real TUI against the synthetic machine (`tests/fake_machine.py`) and
asserted on the file written to disk. Probes were deleted after running;
the findings below cite the evidence each one produced.
**Tree:** `6209746` (2026-11-23).
**Status:** steps 1–4 landed — **F1, F2, F3, F4, F7, F8, F9, F14 fixed** (see §D,
`IMPLEMENTATION_NOTES.md` §22–§25). F5–F6, F10–F13 still open.

---

## Verdict

The GUI covers **the mapping** (source → curve → fans) well and **the
policy** (everything that decides *how hard* fans work, and what happens
when something fails) not at all.

| surface | options | editable in TUI | view-only | unreachable |
|---|---|---|---|---|
| `config.*` globals | 22 | **0** | 0 | **22** |
| `assignments.*` | 12 | 6 (+`tach` auto) | `enabled` | `failsafe`, `failsafe_duty`, `failsafe_after`, `force_takeover`, `tach` (manual) |
| `curves.*` | 3 fields | `points`, `label` (step 2) | `id` | delete of an **in-use** curve (refused by design); id rename (out of scope) |

The GUI surface has not moved since the v2 redesign, while the config
surface has grown from 4 globals (`poll_seconds`, `max_rate`, `floor_duty`,
`dead_temp_c` at commit `ededbdb`) to 22: §18–§21 of
`IMPLEMENTATION_NOTES.md` added the fail-safe, liveness, escalation and
stall-drive knobs, and every one of them landed as JSON-only. Every one of them is a *safety* knob, and
tuning any of them today means `sudo $EDITOR /etc/alx_fancontrol/config.json`
— no validation, no defaults shown, no units, no idea whether the running
daemon accepted it.

Four findings are **bugs** (proven, not stylistic): F2, F3, F4, F7, F9.

---

## A. Coverage matrix

### `config.*` — 22 globals (JSON-only at audit time; now screen 5, §25)

| key | default | GUI |
|---|---|---|
| `poll_seconds` | 1.0 | ✅ Settings |
| `max_rate` | 6 | ✅ Settings + per-assignment override |
| `floor_duty` | 5 | ✅ Settings + per-assignment override |
| `dead_temp_c` | −10 | ✅ Settings |
| `failsafe_duty` | 100 | ✅ Settings |
| `failsafe_after` | 5 | ✅ Settings |
| `failsafe_max_rate` | 25 | ✅ Settings |
| `gpu_temp_min_valid_c` | 15 | ✅ Settings |
| `smi_timeout_s` | 3.0 | ✅ Settings |
| `watchdog_seconds` | 30 | ✅ Settings |
| `stall_min_duty` | 20 | ✅ Settings |
| `stall_after` | 10 | ✅ Settings |
| `stall_full_duty` | 100 | ✅ Settings |
| `stall_kick` | true | ✅ Settings |
| `stall_kick_attempts` | 3 | ✅ Settings |
| `stall_kick_stop_s` | 0.5 | ✅ Settings |
| `stall_kick_run_s` | 3.0 | ✅ Settings |
| `stall_kick_backoff_s` | 30 | ✅ Settings |
| `stall_kick_cooldown_s` | 60 | ✅ Settings |
| `stall_alert_command` | `""` | ✅ Settings |
| `stall_alert_timeout_s` | 5.0 | ✅ Settings |
| `stall_alert_repeat_s` | 300 | ✅ Settings |

(Verified two ways **at audit time**: zero references to any of these names
anywhere in `alx_fancontrol/tui/`, and a probe that set `poll_seconds=7.0` +
`stall_alert_command="notify-send 'fan trouble'"` and found neither string
rendered on any of the four screens.)

### `assignments.*` — 12 keys

| key | GUI | notes |
|---|---|---|
| `source` | ✅ edit | `Select`, live °C in labels — good |
| `curve` | ✅ edit | `Select` |
| `fans` | ✅ edit | click-to-toggle table — good |
| `floor_duty` | ⚠️ edit | can set, **cannot clear** (F2) |
| `max_duty` | ⚠️ edit | same |
| `max_rate` | ⚠️ edit | same |
| `enabled` | 👁 view | list shows `on`/`off`; **no control at all** (F3) |
| `tach` | ✅ editor | tach table per picked fan (`#tach-pick`) + `TachPickerModal`; guess is a pre-fill, "(no tach)" selectable; unmapped warns — §24 |
| `failsafe` | ✗ | silently preserved on save |
| `failsafe_duty` | ✗ | silently preserved |
| `failsafe_after` | ✗ | silently preserved |
| `force_takeover` | ✗ | referenced in the screen's own docstring, no control (F5) |

### `curves.*`

| field / action | GUI |
|---|---|
| `points` | ✅ excellent — table + drag plot + nudges + midpoint insert |
| `label` | 👁 view only — cannot rename |
| curve `id` | 👁 view only |
| **new curve** | ✅ `n` / New (step 2) |
| **duplicate curve** | ✅ `c` / Copy — copies unsaved edits too (step 2) |
| **delete curve** | ✅ `shift+d`, unused only, refuses by name (step 2) |
| **first point of an empty curve** | ✅ `a` prompts (step 2) |

---

## B. Findings

### F1 · High (**fixed** §25) — there is no Settings screen (22/22 globals unreachable)

`base.py:107-110` defines exactly four nav items. `app.py:69-74` binds
exactly four keys. There is no command palette, no "raw JSON" view, no
read-only display of the effective values either — the TUI cannot even
*tell you what `poll_seconds` is right now*.

Impact: the knobs a user actually needs after reading
`README → Troubleshooting` (`poll_seconds`, `failsafe_after`,
`stall_alert_command`, `dead_temp_c`, `gpu_temp_min_valid_c`) are all
edit-the-file-by-hand. The daemon hot-reloads, so a typo is *silent*: the
daemon keeps the old config, sets `errors.config`
(`daemon.py:1143-1148, 1236`) and the TUI shows it as one fragment of the
Overview status line (`overview.py:133-147`).

**Fixed (§25):** screen 5 · Settings, generated from `config.CONFIG_FIELDS` —
one description per option driving validation *and* the form (bounds, unit,
group, help), plus a test that every `DEFAULTS["config"]` key has a Field so a
new knob cannot quietly be invisible. Edits are held on the screen until Save,
blank removes the key, an out-of-range value is refused where it is typed.

### F2 · High (bug, **fixed**) — blanking floor / ceiling / rate cannot clear an override

`assign.py:453-459`:

```python
if floor is not None:
    assignment["floor_duty"] = floor
```

`assignment` starts as `dict(existing)` (`:445-446`), so an **empty field means
"leave whatever was there"**. Proven: load `CPU` (floor 30, ceiling 90,
rate 4), blank all three inputs, Save → file still contains
`{"floor_duty": 30, "max_duty": 90, "max_rate": 4}`.

Impact: "put this fan back on the global floor" is not expressible in the
GUI. The user types `5`, which *pins* it to 5 per-assignment — a different
thing that stops following the global default. This is the one operation
every override UI needs and it silently does nothing.

### F3 · High (bug, **fixed**) — `enabled` is displayed but there is no way to change it

The Assign list has an `on`/`off` column (`assign.py:248`) and the save path
deliberately preserves it (`:451`), but no button, no binding, no checkbox
touches it (probe: buttons on the screen are
`new, delete, save, nav-1..4`; bindings are `s`, `d`).

Impact: disabling an assignment — the *recommended* safe action in two
places (`config.py:50-53` "prefer deleting/disabling such an assignment",
and the fail-safe-disabled warning) — requires hand-editing JSON. Deleting
is available and is the wrong tool: it also gives up the tach mapping, the
floor, and firmware control of the header.

### F4 · High (bug, **fixed** §24) — no tach editor; a fan without a tach is silently unmonitored

`app.guess_tach()` (`app.py:235-249`) maps `chip:pwmN → chip:fanN` **only
when `sensors.d` labels both identically**. `save_assignment` writes tach
from that guess and nothing else (`assign.py:459-465`).

Proven: with the matching `fan5` label removed from the synthetic machine,
assigning `it8686:pwm5` saves an assignment with **no `tach` key**, and no
GUI control can add one. Downstream (`daemon.py:1178-1183`, `_check_fans`
from `:491`) `rpm` stays `None`, `self._rpm[fan] == 0` is never true, so the
stall detector — §20/§21, the reason this project exists — **never fires for
that fan**, with no warning anywhere. The Overview RPM column just shows `—`.

Also: stale tach entries are never cleaned. Remove a fan from an assignment
and its `tach` entry survives in the map.

**Fixed (§24):** a tach table per picked fan with a picker modal (same-chip
inputs first, sensors.d labels, "matches this header" flag, explicit
*no tach*), `guess_tach` demoted to a pre-fill, stale entries dropped on
save, and the fact made visible in three places — save notification, daemon
warning, and `no tach` in the Overview RPM column (`status.json` now carries
`tach` per fan).

### F5 · High — takeover refused → `force_takeover` is unreachable, and the picker hides ownership

`daemon._takeover()` refuses a channel that reads `enable=1` held by someone
else (`manual-conflict`, `daemon.py:972-979`) or `enable=0`
(`disabled`, `:990-994`) unless the assignment sets `force_takeover`. The
Assign docstring says this out loud (`assign.py:13-16`) and then offers
nothing. Worse, the fan picker's columns are
`✓ / fan / label / rpm` (`:155-157`) — no enable/owner column — so at pick
time you cannot see that the header is foreign or parked; you find out one
screen later as the bare word `manual-conflict` in the Overview state cell.

This is the failure mode with real history on this board (§18, §20: BIOS
parking headers at 0 %). The GUI currently turns it into a JSON editing
exercise *while a card is hot*.

### F6 · Medium-high — per-assignment fail-safe overrides are invisible

`failsafe`, `failsafe_duty`, `failsafe_after` (`daemon._failsafe()`,
`:360-372`) are read per assignment, validated (`config.py:341-359`), and
warned about — but the TUI neither shows them nor edits them. A hand-set
`"failsafe": false` (fans hold last duty instead of ramping to 100 %) is a
one-character way to disable a safety net on one fan, and the GUI will not
show it on screen. Note the asymmetry: the *global* fail-safe is also
invisible (F1), so there is no place at all where "what happens if this
source dies" can be read.

### F7 · Medium-high (bug, **fixed in step 2**) — curves cannot be created, renamed, deleted; an empty curve is unrecoverable

> Step 1 stopped the *saving* half of this (an empty curve is refused by
> `save_config`); step 2 closed the rest: New / Copy / Delete with a usage
> guard and `used by: …` hint, label editing, and `a` prompting for the
> first point of an empty curve. Renaming a curve **id** remains out of scope
> on purpose — it means rewriting every assignment that references it.

`action_add_point` (`curves.py:346-352`) requires a **selected existing
point**. Proven: with `points: []`, pressing `a` warns
"Select a point row first" and the only button on the screen other than nav
is `save-curve`. So:

* a curve can never get its **first** point;
* delete all points of a curve → save (accepted, see F8) → that curve is
  dead and the only repair path is editing JSON;
* you cannot add a curve for a new fan type (`n` / "New" / duplicate), so
  "copy the V100 curve and shift it 5 °C" — the normal way to tune — is a
  JSON job;
* `label` is display-only, so a curve id can't be renamed either.

Nothing shows which assignments use a curve, which makes even a future
delete button dangerous without a usage count.

### F8 · Medium (**fixed**) — the TUI writes configs the daemon rejects, and says "Saved"

`save_config` (`app.py:251-268`) never calls `config.validate()`, which is
sitting right there with per-field bounds. Proven: delete every point of a
curve, Save → notification *"Curve 'default' saved"*, file now invalid
(`validate()` → `curve default: needs >= 1 point`), daemon keeps the **old**
config and the TUI never indicates that nothing changed.

The screen re-implements two checks locally (`curves.py:514-523`) and misses
the rest; `assign.py` checks only floor ≤ ceiling. Anything else — a duty
> 100 from a drag, a curve with one point at a nonsense temp, the
`stall_full_duty ≥ stall_min_duty` cross-check (`config.py:246-256`, which
exists precisely because that pairing disarms the alarm) — is accepted
silently by the GUI and refused by the daemon.

### F9 · Medium (bug, **fixed**) — renaming an assignment silently duplicates it

Click `CPU`, change the name to `CPU-box`, Save → **both** exist, both drive
`it8686:pwm1`. Proven. The only signal is a generic "Shared fan(s) are
driven to the HIGHEST requested duty" notice; nothing says *the old one is
still there*, and the user now has two assignments whose floors disagree.

### F10 · Medium — the Overview shows less than `status.json` already contains

The daemon publishes (`daemon.py:1185-1262`) and the TUI drops:

* `degraded: true` — the daemon is holding fans at fail-safe duty because
  its config was rejected at startup. No banner anywhere.
* per-fan `kick: {phase, attempts}` — the comment at `:1221` says *"an
  operator (and the TUI) should see that the daemon is actively fighting for
  this fan"*. The Overview state cell shows `stalled`, not "kicking,
  attempt 2/3, lunge".
* `duty_raw` / `orig_enable` — what the chip actually holds vs. what we
  asked for (the §20 "commanded duty is not ownership" lesson).
* the structured `errors` map (`config`, `startup_config`, `nvidia_smi`,
  `stalled:*`, `ownership-lost:*`, `duty-drift:*`) — flattened into one
  joined line, so on a box with four GPUs it scrolls past unreadable.

Also `_refresh_fans` only lists fans of **enabled** assignments (`:94`), so
a disabled assignment's header disappears from the Overview entirely — right
when you want to see "this header is now firmware's".

Small thing spotted while reading: `overview.py:74-85` is dead code (a
duplicate of the loop above it that computes values and discards them).

### F11 · Low-medium — you are never told *which* config you are editing

`cfg_path` is used for load/save and shown nowhere. Root edits
`/etc/alx_fancontrol/config.json`, non-root edits
`~/.config/alx_fancontrol/config.json` (`config.py:104-118`). The non-root
banner says "no fan control — use sudo alx-fancontrol" (`base.py:107-109`)
but not "and your saves are going to the home file, which the running daemon
will never read". A `sudo`-less session can produce a perfectly valid config
that changes nothing.

### F12 · Low — input affordances are thin

`#floor`, `#maxduty`, `#rate` are `restrict="[0-9]" max_length=3`
(`assign.py:134-139`): integers only (the model accepts floats — the live
`/etc` config contains `59.5`), no units in the label, and the defaults are
hard-coded in placeholder text (`"floor % (default 5)"`) which will lie the
moment a global default changes. There is no help text anywhere in the GUI;
all of the excellent explanation lives in the README.

### F13 · Low — doc drift found on the way

`README.md` config table still documents `sources.*.kind` / `.chip` /
`.temp` / `.chip_index` / `.pci_bus`, which §14 removed from the model
(sources are discovered at runtime). The GUI is not wrong here, but anyone
reading the table to learn what to edit will look for a screen that cannot
exist.

### F14 · Medium (**fixed** §25) — `config.load()` re-injects the built-in curves, so some edits cannot stick *(found in step 2)*

`load()` returns `_deep_merge(DEFAULTS, doc)`. For `config.*` that is the
feature (a key added in §21 has a value even in a v1 file), but it applies to
`curves` too: a file with `{"curves": {"mine": …}}` loads as
`[default, gpu_v100, mine]`, and any save writes both built-ins back.

Two consequences beyond curves:

* **Delete curve** cannot remove `default` / `gpu_v100` — they return on the
  next load. Step 2 refuses those two with the reason rather than offering a
  button that silently does nothing.
* Any future "remove this setting" edit has the same trap: absent-from-file
  and set-to-default are the same state to the loader, so the GUI cannot
  express "I deleted it".

Fix belongs with step 4's schema: seed defaults **once** (`seed_if_missing`
already does exactly that) and have `load()` merge only what the schema marks
as additive. Not done in step 2 on purpose — a hand-written config that
references `default` without defining it works today, and replace-semantics
would turn that into a validation error whose runtime consequence is a skipped
assignment (fans holding their last duty).

**Fixed (§25):** `_merge_defaults()` merges `config` per key (still additive,
so a release can add a knob) and takes `curves`/`assignments` **as written
when present**, seeding DEFAULTS only when the section is absent entirely. The
step-2 worry therefore does not arise: a file with no `curves` key still gets
the stock pair, while a file that writes one is taken as complete — and it
also stops built-in labels being invented for curves that have none. Step 2's
Delete refusal was removed and its test inverted (delete `default` → gone from
the file and from a fresh `load()`).

---

## C. Recommended design

### C1 · One schema, three consumers (do this first)

The bounds already exist — in a tuple list inside `validate()`
(`config.py:216-238`). Promote them to a declarative table in `config.py`:

```python
SETTING = namedtuple("SETTING", "key kind default lo hi unit help")
SCHEMA = [
    Group("Polling and liveness", [
        SETTING("poll_seconds", "float", 1.0, 0.001, None, "s",
                "poll interval; the watchdog must be > 3x this"),
        SETTING("watchdog_seconds", "float", 30, 0, None, "s",
                "0 = off; on expiry the daemon exits 70 so systemd restarts it"),
        SETTING("smi_timeout_s", "float", 3.0, 0, None, "s",
                "nvidia-smi timeout; 0 = no timeout"),
    ]),
    Group("Curve control", [...poll/max_rate/floor/dead_temp/gpu_temp_min...]),
    Group("Fail-safe (source unreadable)", [...3 keys...]),
    Group("Stalled fan: pin and kick", [...9 keys...]),
    Group("Escalation (reaching a person)", [...3 keys...]),
]
```

Then: `validate()` iterates it (no behaviour change, one less hand-maintained
list), the **Settings screen renders itself from it**, and `check` / README
can print the same table. This is what makes F1 cheap to build *and* keeps
F1 from coming back next time a knob is added — which, given §18→§21 added
13 knobs in five days, it will.

### C2 · Screen 5 — **Settings** (`config.*`)

* Table of rows grouped by `SCHEMA` group: `key · value · default · unit ·
  help`. Cursor on a row → help line in the footer strip; `Enter`/click edits
  the value (typed input for numbers, `Select` yes/no for `stall_kick`).
* Show **effective vs default**: `poll_seconds 1.0 (default)` vs
  `stall_after 30` highlighted as changed — answers "did I already change
  this?" without opening the file.
* Range-check on commit using the same lo/hi as `validate()`, so an
  out-of-range value is refused *in the field*, not by the daemon.
* Enforce the one cross-key rule that matters in the UI while editing:
  `stall_full_duty ≥ stall_min_duty` (`config.py:253-265`) — name the reason
  in the warning ("pinning a stalled fan under the alarm threshold disarms
  the detector that pinned it").
* **Escalation group gets a Test button**: run `stall_alert_command` with the
  same env (`ALX_FAN=TEST ALX_STATE=test …`) and a timeout, show exit status.
  A hook you have never run is a hook that fails the first real fault.
* Footer: the config path being edited (F11) + `Ctrl+S`-equivalent "Save"
  through `app.save_config()` (keeping the single save path and stale guard).

### C3 · Assign screen — finish the assignment model

1. **Clear = remove the key** (fixes F2): blank input →
   `assignment.pop("floor_duty", None)`. Placeholder becomes
   `(global: 5)` read from the live config instead of hard-coded text.
2. **Enable/disable**: a `ToggleSwitch` in the form + `e` binding on a list
   row; disabled rows dimmed in the list. Deleting stays as-is.
3. **Tach per fan**: make the fan picker rows expandable (or a small
   second table) with a `Select` of `chip:fanN` candidates per picked fan,
   pre-filled by `guess_tach`, and an explicit **"no tach"** option. Warn on
   save if any picked fan has no tach: *"this header will have no RPM
   reading and stall detection cannot watch it"* (fixes F4). Drop tach
   entries for fans no longer in the assignment.
4. **Ownership column** in the fan picker (`firmware / manual(other) /
   disabled / ours`, from `pwmN_enable` + `owned.json`) so F5 is visible at
   pick time.
5. **Advanced group** (collapsed by default): `failsafe` switch,
   `failsafe_duty`, `failsafe_after`, `force_takeover` — each with the
   README's wording. `force_takeover` needs a confirmation naming the channel
   and what gets restored on exit.
6. **Rename** (F9): keep the form-name-means-key behaviour but detect
   "editing X, name changed to Y" → confirm *"rename X → Y"* (delete old key,
   carry everything over) instead of creating a second assignment.
7. Validate through `config.validate()` before writing; on errors show them
   in a modal listing exactly what is wrong (F8).

### C4 · Curves screen

* **New** (id + label prompt, seeded with the selected curve's points),
  **Duplicate**, **Delete** (with "used by N assignments: …" from a
  reverse index; refuse or offer to re-point), rename **label** inline.
* Allow adding the **first point** to an empty curve: `a` with no rows opens
  the prompt with sane defaults instead of warning (fixes F7).
* Refuse to save fewer than 1 point, and run the curve through
  `validate()`'s rules before writing (fixes F8 at the source).

### C5 · Overview screen

* **DEGRADED** banner when `status.degraded` — it should be the loudest thing
  on the screen.
* Fan table: show disabled assignments too (state `disabled (firmware)`);
  add a column for `kick.phase + attempts` when present; show
  commanded-vs-actual duty when `duty_raw` disagrees with what we wrote.
* Move `errors` from the one-line status strip into its own small table
  (`key | detail`) so four GPU faults don't collide, and keep a compact
  count in the status line.
* Status line: add the config path and "reloaded OK / rejected" derived from
  `config_mtime == mtime(config)` + absence of `errors.config` — closing the
  loop between "I pressed Save" and "the running daemon is using it".

---

## D. Suggested order

| step | delivers | touches | risk |
|---|---|---|---|
| 1 ✅ | F2 clear-override, F3 enable toggle, F9 rename-confirm, F8 validate-before-save | `assign.py`, `app.save_config` | small, local |
| 2 ✅ | F7 curve new/duplicate/delete/label + first point | `curves.py`, `base.py` (`ConfirmScreen`) | small |
| 3 ✅ | F4 tach editor + no-tach warning + stale-entry cleanup | `assign.py`, `hwmon.py`, `daemon.py`, `overview.py` | medium (safety-relevant) |
| 4 ✅ | F1 Settings screen + `SCHEMA` refactor of `validate()` **+ F14 (seed defaults once, don't re-merge on load)** | `config.py`, new `tui/screens/settings.py`, `base.py`, `app.py` | medium, mostly mechanical |
| 5 | F5 ownership column + `force_takeover` control | `assign.py`, `hwmon.py` (read enable per channel) | medium |
| 6 | F10 Overview upgrades, F11 config path, F12 input polish, F13 README | `overview.py`, screens, README | low |

Each step should land with UX tests in `tests/test_tui_ux.py` (that file's
`no_swallowed_exceptions` + `notices` fixtures are exactly what these need) —
in particular, the probes used for this audit are worth keeping as
regressions: *blank field removes the key*, *empty curve cannot be saved*,
*fan without a matching tach label still gets a tach via the picker*,
*disabled assignment is togglable from the list*, *rename does not duplicate*.

## E. Explicit non-goals

* Editing `sources` — correctly removed in v2; runtime discovery, and the
  Sources screen's read-only stance (`sources.py:1-5`) is right.
* Writing to hwmon from the TUI — keep it read-only; every control here
  writes config and lets the daemon act.
* Daemon start/stop/restart buttons in the TUI — systemd's job; showing
  state (F10) is enough, and a TUI that restarts the controller while a fan
  is mid-kick is a bad idea.
