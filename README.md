# alx_fancontrol

TUI-first fan control for the Gigabyte X399 AORUS Gaming 7 (Debian 12,
Python 3.11): a **pure-stdlib daemon** (hwmon + `nvidia-smi` temperature
sources → piecewise-linear curves → rate-limited PWM writes with
firmware-restore-on-exit) plus a **textual TUI** (overview / assign /
curve editor / sources) as the primary UX.

```
┌ alx_fancontrol ────────────────────────────────────────────────┐
│  TUI (venv, textual)          config.json (plain JSON, atomic) │
│  overview / assign / curves  ←────────→  daemon (stdlib)       │
│  sources (1 s live refresh)            hwmon + nvidia-smi      │
│  read-only on hwmon, ever              → pwmN / pwmN_enable    │
└────────────────────────────────────────────────────────────────┘
```

**Why it's less clunky than `fancontrol` (lm-sensors):**

- No `/etc/fancontrol` + `/etc/fancontrol.conf` pair, no
  `fancontrol-configure` wizard, no `fcconfmonitor` helper, no
  `pwmconfig`-style probing. One plain-JSON file in your home dir.
- You **see** what's happening: live temps, live duty, live RPM, daemon
  state — updating every second in the TUI. `fancontrol` is a
  headless CLI (`sensors` tells you what; `fancontrol -l` tells you
  less) and its config is a bespoke ini dialect.
- Curves are edited **visually** in a live text plot (drag points with
  the mouse, nudge with keys, watch the current-temperature marker),
  instead of `tc=… sc=…` line pairs in a text editor.
- Multiple sources (CPU package temps, motherboard thermistors, GPU
  temps) and multiple fans per source, with per-fan floor/ceiling/rate —
  `fancontrol` does one fan per temp with a single global min.
- Safety is in code, not in your memory: channels already in manual mode
  by another process are refused at takeover unless our own ownership
  marker claims them (a restart after a crash), and every exit path
  restores firmware control. An unreadable temperature source ramps the
  fans it drives to `failsafe_duty` instead of parking them where a BIOS
  curve may leave them at 0 %. `--dry-run` demonstrates all of it without
  writing a single sysfs file.
- Hot config reload: edit the JSON (by hand or in the TUI) and the
  daemon picks it up within ~1 s — no restart, no `pwmconfig` re-run.

---

## Install

**One-liner** — build the runtime snapshot, install and start the
systemd service, verify the guarded channels:

```bash
sudo bash install.sh              # install/upgrade
sudo bash install.sh --dry-run    # no sudo: print exactly what it would do
sudo bash uninstall.sh [--purge]  # remove the service (+ /etc config w/ --purge)
```

The install model: this directory is the **source tree**; `install.sh`
`pip install`s the current source **non-editable** into
`/opt/alx_fancontrol/venv` — a frozen snapshot the service runs. The
source tree is never touched at runtime and can be deleted without
affecting the running system. Upgrading = edit source, re-run
`install.sh` (refreshes the snapshot, restarts the service). It also
installs a launcher to `/usr/local/bin/alx-fancontrol`, so
**`sudo alx-fancontrol tui|daemon|check`** works from anywhere (it
prefers the `/opt` snapshot and falls back to the dev venv, so it
stays usable in interim mode too — see
[Running without installing](#running-without-installing-interim-mode)).

The manual/dev path (no service): everything lives inside this project
directory; nothing is installed system-wide (no apt, no `/usr/local`).

```bash
cd /home/alex/alx_fancontrol
bash scripts/bootstrap_venv.sh        # creates .venv, installs textual + pytest
```

The dev venv is bootstrapped **without apt** (`python3 -m venv
--without-pip` + `get-pip.py` + `pip install -e ".[tui,test]"`) because
Debian's PEP 668 refuses system pip installs and `python3-venv` is not
installed. The dev install is *editable* (tests run against the working
tree); the `/opt` install is not (the service runs a snapshot).

After that you have (dev, from the source tree):

- `.venv/bin/alx-fancontrol` — the CLI (daemon / tui / check)
- `python3 -m alx_fancontrol.daemon` — the daemon under **system** python
  (it's pure stdlib; no venv required)
- `python3 -m alx_fancontrol.cli check` — config validation under system
  python

The first `check`/`daemon`/`tui` run **seeds** the config (see
[Config](#config) for locations) from live discovery (hwmon chips +
`nvidia-smi` GPUs + `/etc/sensors.d` labels) with **zero assignments** —
a fresh install controls nothing until you assign fans in the TUI.

## Quick start

```bash
# installed system (what install.sh runs):
sudo alx-fancontrol check        # validate /etc config
sudo alx-fancontrol              # the TUI — bare command = tui (edits /etc config)

# dev, from the source tree (uses ~/.config, can't take over fans):
cd /home/alex/alx_fancontrol
.venv/bin/alx-fancontrol daemon --dry-run --duration 5        # watch intended actions, no writes
.venv/bin/alx-fancontrol tui
```

The daemon is a system daemon: run it with sudo (or as the service) so
it uses the `/etc` config and can write the pwm registers. Without sudo
it runs in dev mode against `~/.config/alx_fancontrol/` — fine for
testing, but it can't take over fans.

In the TUI: `2` → Assign → name it, pick a source (live °C), a curve,
click fans, Save. `3` → Curves → shape the curve (drag points, `a`/`d`,
`+`/`-`, shift+arrows), Save. `4` → Sources → the runtime-detected
source list. `1` → Overview → watch duties/RPMs move. (The GPU blower
headers on `it8792` are assigned here like any other fan — this daemon is
the only fan controller on the machine.)

Then run the daemon for real (it takes over only the fans you assigned,
restores firmware control on exit) — or better, install it as a service:

```bash
sudo alx-fancontrol daemon       # foreground (Ctrl-C exits cleanly)
sudo bash install.sh             # or: survive reboots, auto-restart
```

## Config

This is a **system** daemon (the service runs as root for autonomous
operation), so as root the config lives in `/etc`:

| running as | config | status.json | daemon.log |
|---|---|---|---|
| root (service / sudo) | `/etc/alx_fancontrol/config.json` | `/run/alx_fancontrol/status.json` | `/var/log/alx_fancontrol/daemon.log` |
| non-root (dev/foreground) | `~/.config/alx_fancontrol/config.json` | next to config | next to config |

`$ALX_FANCONTROL_CONFIG` overrides the config location anywhere. Plain
JSON, hand-editable. `config.json.bak` (previous content) sits next to
the config; `daemon.log` rotates at 256 KB × 3; the service's stdout also
goes to the journal (`journalctl -u alx-fancontrol`).

The TUI edits the config, so run it as root (`sudo alx-fancontrol tui`)
to edit the system config.

Duty is **percent 0–100** in the config; raw 0–255 only at the sysfs
boundary.

**What is (and isn't) in the config:** only the *source→fan mappings*
(`assignments`) and the *curves* live in the config, plus daemon
tunables. **Temperature sources are not configured** —
they are *discovered at runtime* (every hwmon `tempN_input` + every
nvidia-smi GPU) by both the daemon and the TUI, and referenced by
canonical ids:

| source | canonical id |
|---|---|
| hwmon chip with a unique name | `it8686:temp1` |
| hwmon chip name occurring N× (e.g. per-CPU-package) | `k10temp[0]:temp1` |
| GPU | `gpu:08:00.0` (pci bus — stable) |

If an assignment references an id discovery doesn't produce (hardware
changed), the daemon still takes over its fans and puts them on the
fail-safe (see *Daemon*), re-checking every poll — and the TUI lets you
re-pick it. v1 configs (with a `sources` section) are migrated
automatically on load — the TUI re-saves them as v2 on first open.

```json
{
  "version": 2,
  "config": {
    "poll_seconds": 1.0,
    "max_rate": 6,
    "floor_duty": 5,
    "dead_temp_c": -10,
    "failsafe_duty": 100,
    "failsafe_after": 5,
    "failsafe_max_rate": 25,
    "gpu_temp_min_valid_c": 15,
    "smi_timeout_s": 3.0,
    "watchdog_seconds": 30,
    "stall_min_duty": 20,
    "stall_after": 10
  },
  "curves": {
    "default":  { "label": "Default",     "points": [[40, 20], [55, 30], [65, 70], [75, 100]] },
    "gpu_v100": { "label": "V100 blower", "points": [[50, 26], [60, 40], [66, 75], [72, 100]] }
  },
  "assignments": {
    "cpu-fan": {
      "source": "k10temp[0]:temp1",
      "curve": "default",
      "fans": ["it8686:pwm1"],
      "tach": { "it8686:pwm1": "it8686:fan1" },
      "enabled": true
    },
    "gpu-low": {
      "source": "gpu:08:00.0",
      "curve": "gpu_v100",
      "fans": ["it8792:pwm2"],
      "floor_duty": 26,
      "max_duty": 100,
      "max_rate": 12,
      "enabled": true
    }
  }
}
// no "sources" key — sources are detected at runtime (see table above)
```

| field | meaning |
|---|---|
| `config.poll_seconds` | poll interval (s) |
| `config.max_rate` | fallback max duty change **per poll**, percent (6 %/poll ≈ 16 raw steps on the 0–255 scale — measured safe for these blowers to spin up from) — overridden per assignment |
| `config.floor_duty` | fallback per-assignment floor, **default 5%** (quiet idle; stall protection) — overridden per assignment |
| `config.dead_temp_c` | readings below this count as "no reading" (kills the −55 °C dead thermistors) |
| `config.failsafe_duty` | **default 100** — duty a fan is driven to when its source goes unreadable. An unreadable temperature may only ever make a fan go FASTER |
| `config.failsafe_after` | consecutive dead polls before the fail-safe engages (default 5 ≈ 5 s: rides out a one-poll nvidia-smi hiccup) |
| `config.failsafe_max_rate` | %/poll used when ramping toward the fail-safe duty (default 25 — faster than normal control) |
| `config.gpu_temp_min_valid_c` | **default 15** — a GPU reading below this is not a temperature (a card that dropped off the bus reports 0 °C), so it counts as "no reading" |
| `config.smi_timeout_s` | nvidia-smi timeout in seconds (default 3, `0` = no timeout). A hung nvidia-smi is killed and NOT waited on; a short backoff keeps hung children from piling up |
| `config.watchdog_seconds` | poll-loop watchdog (default 30, `0` = off). If no poll completes in time the daemon exits with status 70 so systemd restarts it — fans keep their last duty and are reclaimed on startup |
| `config.stall_min_duty` / `config.stall_after` | stalled-fan alarm: tach reads ~0 RPM while we command ≥ this duty for this many polls (default 20 % / 10 polls) → `state: "stalled"` + log ERROR + `status.json` `errors`, then **escalates** (see [Escalation](#escalation-what-happens-when-a-fan-cannot-cool)) |
| `config.stall_alert_command` | shell command run when a fan fault starts and again when it clears (default `""` = off). Event arrives in the environment: `ALX_FAN`, `ALX_STATE`, `ALX_DETAIL`, `ALX_DUTY_PCT`, `ALX_RPM`, `ALX_CONFIG` |
| `config.stall_alert_timeout_s` | how long the alert command may run (default 5). Longer than that and it is killed with its process group — the loop that keeps the *other* fans alive is not negotiable |
| `config.stall_alert_repeat_s` | re-alert every N seconds while a fault persists (default 300; `0` = once per episode) |
| `sources.*.kind` | `hwmon` or `nvidia` |
| `sources.*.chip` / `.temp` / `.chip_index` | hwmon source: chip **name prefix** (never `hwmonN` — indices reshuffle across reboots), temp index, and which instance of a duplicated prefix (two `k10temp` packages on this box → `chip_index` 0/1) |
| `sources.*.pci_bus` | nvidia source: normalized PCI bus, e.g. `08:00.0` |
| `curves.*.points` | `[temp_c, duty_pct]` pairs, ≥1, temps strictly increasing, duty 0–100. Eval clamps flat beyond the ends. |
| `assignments.*.fans` | list of `chip:pwmN` — a fan **may** appear in several assignments; the daemon then drives it to the **highest** requested duty (validation warns) |
| `assignments.*.tach` | optional `chip:pwmN → chip:fanN` RPM display map (the TUI auto-guesses it from sensors.d labels) |
| `assignments.*.floor_duty` / `.max_duty` / `.max_rate` | **per-fan** floor / ceiling / max duty change per poll — override the global fallbacks (floor ≤ max enforced) |
| `assignments.*.failsafe` | `false` opts this assignment out of the fail-safe (it holds last duty instead of ramping). Validation warns — prefer disabling/deleting the assignment |
| `assignments.*.failsafe_duty` / `.failsafe_after` | per-assignment fail-safe overrides |
| `assignments.*.force_takeover` | `true` claims the channel even when `pwmN_enable` is 1 (manual, another owner) or 0 (disabled). Opt-in for headers the BIOS parks where they cannot cool anything; the value found there is still what gets restored on exit |

**Writers:** only the TUI and humans (plain JSON by design). The daemon
never writes the config; it hot-reloads on mtime change every poll
(atomic replace on the writer side means it never reads a torn file; a
bad edit keeps the old in-memory config and shows a red error in
`status.json`). The TUI remembers the mtime it loaded and, if the file
changed on disk, asks **Reload / Overwrite** before saving. Don't run
two TUIs at once.

## Daemon

```
alx-fancontrol daemon [--config PATH] [--dry-run] [--once] [--duration SECS] [-v]
python3 -m alx_fancontrol.daemon  [same flags]     # system python, no venv needed
```

- `--dry-run` — **no writes at all** (no `pwmN_enable`, no `pwmN`), but
  the full read loop runs and every guardrail check executes and logs:
  `would take over …`, `channel … already in manual mode` (refused),
  `would write …`, `would return … to firmware`. Use it to see exactly
  what a config would do.
- `--once` / `--duration N` — bounded runs for smoke tests; the
  firmware-restore `finally` still executes.
- `-v` — DEBUG logging.

Behavior:

- 1 s poll; **one** `nvidia-smi` call per poll serves all GPU sources.
  The call has a timeout (`smi_timeout_s`): a card that fell off the bus
  makes nvidia-smi block forever, so it is killed (not waited on) and the
  loop keeps running. A failing call marks GPU sources *stale* → fail-safe
  below; CPU-side assignments keep running.
- **Fail-safe direction.** A source that is unreadable — GPU gone from
  nvidia-smi, nvidia-smi hung/offline, dead thermistor, hwmon chip gone —
  makes the assignment drive its fans to `failsafe_duty` (default **100 %**)
  after `failsafe_after` consecutive dead polls, ramping at
  `failsafe_max_rate` %/poll. It never "holds a low duty", and it never
  hands the header back to a firmware that parks it at 0: an unreadable
  temperature may only ever make a fan go **faster**. Opt out per
  assignment with `"failsafe": false` (validation warns).
- Sources are **re-discovered every poll**, so a GPU that leaves
  nvidia-smi and comes back is re-adopted automatically — no restart, no
  config edit — and its fan drops back off the fail-safe as soon as it
  reads again. Fans are taken over even when their source is absent at
  startup, because the fail-safe cannot act on a channel we do not own.
- **Ownership is re-verified every poll.** `pwmN_enable` is not ours just
  because we set it once: the firmware/EC can flip it back to `2` (or `0`)
  at any moment, and a duty write into a channel we no longer own
  **succeeds** — the log reports `write it8792:pwm1 = 100%` while the fan
  sits where the firmware left it, which on this board can be 0 %. So each
  poll re-reads `pwmN_enable` *and* `pwmN` for every channel we hold:
  enable drifted → CRITICAL `OWNERSHIP LOST`, reclaimed immediately (a
  header the firmware left disabled is re-enabled: we held it, so a `0`
  there is theft, not intent) and the remembered duty dropped so the next
  write is the real target; duty drifted → WARNING + re-asserted (a second
  writer's value would otherwise stick, because our own write is skipped
  whenever the target equals the duty we remember). `state=ownership-lost`
  stays up until enable really reads `1` again — a successful duty write is
  *not* evidence the fault is gone.
- **A fan that cannot cool escalates** (see
  [Escalation](#escalation-what-happens-when-a-fan-cannot-cool)): a tach
  stall and an ownership loss both shout CRITICAL and reach the operator
  hook, because more duty is not an available answer to either. Nothing more:
  the daemon's reach ends at the fan header.
- A GPU reading below `gpu_temp_min_valid_c` (default 15 °C) counts as "no
  reading", never as "ice cold": a card that dropped off the bus reports 0 °C.
- Fan write fails → keep last duty, log first + every 10th failure,
  `state=write-error` in status, retry every poll.
- **Takeover rules** (per channel, re-checked every poll):
  - `pwmN_enable=1` left by **our own previous run** (same boot, per the
    ownership marker) → **reclaimed** (WARN `reclaiming …`) and driven
    again. This is what stops a SIGKILLed/OOMed daemon from pinning a GPU
    fan at its last raw duty — possibly 0 — forever;
  - `pwmN_enable=1` owned by **another process** (a second controller, or
    a tool that exited without restoring) →
    refused (ERROR, logged first + every 30th poll); eligible the moment
    it is released;
  - `pwmN_enable=0` (disabled) → skipped, unless the assignment sets
    `force_takeover`;
  - `pwmN_enable=2` (firmware) → taken over (`enable→1`), original value
    recorded and **restored exactly** in `finally` (clean SIGTERM/SIGINT
    exit via signal handlers).
  The set of channels we hold is written to the ownership marker
  (`/var/lib/alx_fancontrol/owned.json`, unit's `StateDirectory=`) on every
  takeover/release, together with the boot id and our pid: a marker from a
  previous boot, or one held by a live sibling daemon, is never claimed.
- **Nothing may freeze the loop.** If no poll completes within
  `watchdog_seconds` (default 30) a watchdog thread exits with status 70,
  systemd restarts the unit (`Restart=always`) and the new instance
  reclaims its channels. A hung nvidia-smi can therefore no longer leave
  every fan stuck while the service still reads `active`.
- **Stalled-fan alarm.** Commanding duty is not cooling: if a controlled
  fan's tach reads ~0 RPM while we ask for ≥ `stall_min_duty` for
  `stall_after` polls, it is reported as `state: "stalled"` with a log
  ERROR and an entry in `status.json` `errors`.
- **A bad config does not mean no fan control.** If the config fails to
  load or validate at startup, the daemon used to exit(2) before taking
  anything over (and `Restart=always` turned that into a flap loop with
  zero cooling). Now it enters **degraded mode**: take the channels the
  assignments reference, hold them at `failsafe_duty`, and resume normal
  control the second the file is valid again. Only a config so broken that
  no fan channel can be recovered from it still exits hard.
- Per-fan bounds and slew, in this order: curve temp → duty, then
  **floor** (assignment `floor_duty`, else global `config.floor_duty`,
  default **5%**), then **ceiling** (assignment `max_duty`, else 100),
  then the **rate limiter** (assignment `max_rate`, else global
  `config.max_rate`, default 6 %/poll) steps the duty toward the target
  — a fan can't jump 0→100 in one poll.
- **Shared fans** (a fan listed in several assignments): each assignment
  requests a duty (clamped by its **own** floor/ceiling) and the fan is
  driven to the **highest** request — a cold source can never drag it
  below a hot one's demand. The rate limiter runs **once** per poll,
  bounded by the winning assignment's `max_rate`; ties go to the lowest
  assignment id.

### Escalation: what happens when a fan cannot cool

Two faults mean a card is heating with nothing moving its air, and neither
is fixable by writing more duty:

| fault | detected | ends when |
|---|---|---|
| `stalled` | tach ~0 RPM while commanded ≥ `stall_min_duty` for `stall_after` polls | the tach spins again (`FAN … RECOVERED` + a `recovered` alert) |
| `ownership-lost` | `pwmN_enable` no longer `1` on a channel we hold (checked every poll) | enable reads `1` again (`ownership … re-established`) |

Each one, on the transition into the fault and then at most every
`stall_alert_repeat_s` seconds:

1. **CRITICAL** log line, plus `status.json` `errors` (`stalled:…`,
   `ownership-lost:…`, `duty-drift:…`) and a systemd `STATUS=` that names
   the fault, so `systemctl status alx_fancontrol` shows it without grep.
2. **`stall_alert_command`** runs through `/bin/sh -c` with the event in the
   environment — mail, MQTT publish, a light turning red:

   ```json
   "stall_alert_command": "mosquitto_pub -t home/fan/alert -m \"$ALX_FAN $ALX_STATE: $ALX_DETAIL\""
   ```

   Bounded by `stall_alert_timeout_s` (killed with its process group), and a
   failing hook is logged but never retried into a flood: the fault is still
   reported by the log and status.

**That is all escalation does — this daemon controls fans, and nothing else.**
It has no code path to a GPU power limit, clock, or persistence setting, and
there is deliberately no config knob for one: "off by default" is not the same
as unable. If a fan fault should also slow a workload down, that response
belongs in `stall_alert_command` — a command you write, that logs what it did,
and whose blast radius you chose:

```json
"stall_alert_command": "logger -t alx-fan '$ALX_FAN $ALX_STATE'; nvidia-smi -pl 120"
```

### status.json

Written by the daemon each poll; read by the TUI:

```json
{ "pid": 1234, "ts": 1719e9, "config_mtime": 1719e8, "dry_run": false,
  "degraded": false,
  "sources": { "it8686:temp1": {"label": "System 1", "temp_c": 46.0, "stale": false} },
  "fans":    { "it8686:pwm3": {"label": "SYS_FAN2", "duty_pct": 27, "rpm": 3229,
                                "orig_enable": 2, "state": "ok"} },
  "errors": {} }
```

`state`: `ok | failsafe | stalled | manual-conflict | disabled | unresolved |
source-missing | write-error | curve-missing | idle`.

`errors` carries what a monitor should page on: `config` (failed reload),
`startup_config` (degraded mode), `nvidia_smi` (consecutive
failures/timeouts) and `stalled:<fan>`. A GPU source reads `stale: true`
both when it is missing from nvidia-smi and when it reports an
implausible value such as 0 °C.

## TUI

`.venv/bin/alx-fancontrol tui` — or just **`alx-fancontrol`** with no
subcommand, which opens the TUI (needs the venv; under system python you
get a pointer to `scripts/bootstrap_venv.sh`). The TUI is **read-only on
hwmon** — safe to run while the daemon is live. Run without root and a
red **non-root** banner sits in the sidebar on every screen: fans can't
be controlled (pwm writes are root-only) and the config edited is the
home one, not `/etc`. Keys `1–4` switch
screens (footer shows them; sidebar buttons work too). The current
screen is highlighted in the sidebar — nav items are never *focused*
(focus would swallow the nav keys and leave a stray highlight on the
top item), so: **keys navigate, clicks focus** fields/tables.

1. **Overview** — live tables: *Sources* (label, kind, chip/bus, °C,
   stale?) and *Fans* (assignment, label, source °C, live duty % from
   `pwmN`, live RPM from the tach, state) + a daemon status line
   (running / stale >3 s / absent, config mtime, last error from
   `status.json`).
2. **Assign** — top: a list of the existing assignments (name, source,
   curve, fans, floor, max, rate, on/off). **Click a row to load it into the
   form below and edit it** (no need to remember names); saving with the
   same name overwrites it in place (it keeps its `enabled` state).
   **Delete** button or `d` removes the selected row after a
   confirmation (the daemon then restores firmware control of its
   fans); **New** resets the form. Form: source `Select` (rows show
   live °C), curve `Select`, fan picker (click a row to toggle ✓ —
   every channel is pickable; channels another process holds in manual
   mode are refused by the *daemon* at takeover, not by the UI),
   optional floor % / ceiling % / rate %/poll, name, **Save** (or `s`).
3. **Curves** — points table (cursor-selectable) + live text plot
   (curve as `█` columns, `◄` marker at the current temp of the
   selected preview source, every point marked `●` and the selected one
   `◆`). The curve knows nothing about floor/ceiling — those are
   per-fan settings (Assign screen: floor % / ceiling % / rate %/poll;
   the daemon falls back to the global `config` values). Mouse: **drag a `●` point** to move it —
   clicking empty plot area does nothing. Points are added only from
   the table: `a` inserts a point **between the selected row and the
   next** (prompt pre-filled at their midpoint; if the selected row is
   last it tacks one on at last + 5 °C). Keys: `d` delete, `s` save,
   `+`/`-` duty ±1, shift+←/→ temp ±1, shift+↑/↓ duty ±5. **Save** (button or `s` — the
   button can fall below the fold on short terminals) writes the sorted
   points. On the **Assign** screen `s` likewise saves.
4. **Sources** — read-only live table of the temperature sources
   *detected at runtime* (id, label, kind, chip/bus detail, °C; GPU rows
   show the `nvidia-smi` model). Nothing to configure here: any of them
   is pickable in the Assign screen, and a hardware change is picked up
   on the next daemon start/reload or TUI restart.

## Running without installing (interim mode)

The box is installed via `install.sh` (unit active, config in `/etc`).
This interim mode — running straight from the source tree, no `/opt`
snapshot, no systemd unit — remains for dev work:

```bash
cd /home/alex/alx_fancontrol
bash run-local.sh tui                  # no sudo: edit the home config (read-only on fans)
sudo bash run-local.sh daemon          # root: drive the fans per the home config
```

`run-local.sh` pins **both** to the same config
(`~/.config/alx_fancontrol/config.json`) via `ALX_FANCONTROL_CONFIG`,
because the euid-based defaults would otherwise split them (root →
`/etc/alx_fancontrol`, non-root → `~/.config`). It also restores file
ownership if the TUI ran as root.

> **The install path is the required path for deployment/distribution** —
> interim mode is for this machine in the meantime, not a permanent
> state. Keep it green with:
>
> ```bash
> sudo bash install.sh --dry-run        # light: plan + unit review
> bash scripts/verify_install.sh        # full: throwaway venv, non-editable
>                                        # install, neutral-cwd checks, live
>                                        # dry-run of the INSTALLED package
> ```

## systemd unit

`install.sh` installs `alx-fancontrol.service` for you (see
[Install](#install) for the exact commands). What it does, and why:

1. **Builds the runtime snapshot** at `/opt/alx_fancontrol/venv`:
   `python3 -m venv --without-pip` + `get-pip` (Debian PEP 668), then
   `pip install` of the source — with the `[tui]` extra on first install,
   `--force-reinstall --no-deps` on upgrades (refreshes the package from
   the current source, works offline, never re-downloads deps).
2. **Puts the config in `/etc/alx_fancontrol/`** (root-owned — this is
   system config, not per-user state), migrating a pre-existing
   `~/.config/alx_fancontrol/config.json` once, then seeds-if-missing +
   validates.
3. **Stops any manually-started daemon** so two controllers can't fight
   over the same pwm channel.
4. **Installs the launcher** at `/usr/local/bin/alx-fancontrol`
   (prefer the `/opt` snapshot, else the dev venv) — this is what makes
   plain `sudo alx-fancontrol …` work.
5. **Installs the unit as a root service** — it must be root because it
   writes `/sys/class/hwmon/*/pwmN` (mode 644, root-writable). The unit's
   `RuntimeDirectory=`/`LogsDirectory=`/`StateDirectory=` create
   `/run/alx_fancontrol` (status.json), `/var/log/alx_fancontrol`
   (daemon.log) and `/var/lib/alx_fancontrol` (**owned.json** — the
   ownership marker that lets a restarted daemon reclaim channels its
   predecessor was killed holding in manual mode); as root the daemon
   finds its config in `/etc` on its own (no path pinning).
6. **Verifies**: unit active, the assigned GPU-blower channels at
   `enable=1` (owned by the daemon — anything else means nothing is
   controlling that fan), the contents of the ownership marker, and every
   `errors` entry plus non-`ok` fan state in `status.json`.

`Restart=always` covers the hwmon-reshuffle case (see troubleshooting)
and the daemon's own self-watchdog: if no poll completes within
`watchdog_seconds` the process exits with status 70 and systemd starts a
fresh instance, which reclaims the channels from the ownership marker.

`uninstall.sh` reverses it: `systemctl disable --now` (SIGTERM → the
daemon returns every fan it drove to firmware control), removes the unit,
the launcher and `/opt/alx_fancontrol`; `--purge` also removes
`/etc/alx_fancontrol`
(your assignments/curves) and the log dir. The source tree is never
touched.

## Safety notes

**alx_fancontrol is the only fan controller on this machine.** It holds
every header its config assigns — including the GPU blowers on `it8792`
— and nothing else writes them. Two controllers on one header is exactly
how a fan gets stranded: whoever writes last wins, and neither can see
the other. The three guardrails that keep a single controller honest:

1. Any channel with `pwmN_enable=1` at takeover is refused (something else
   is in manual mode — a second daemon, `fancontrol`, or a tool that exited
   without restoring it). Re-checked every poll, so a channel becomes
   eligible only while it actually sits in firmware mode. **Exception:** a
   channel listed in *our own* ownership marker from *this boot*
   (`/var/lib/alx_fancontrol/owned.json`) is reclaimed — that is the
   leftover of an alx_fancontrol that was killed and could not restore it,
   and refusing it forever would pin the fan at its last duty (possibly 0).
   A marker from another boot, or one whose pid is a live alx_fancontrol,
   is never claimed.
   **The mirror image is checked too:** every channel we *do* hold is
   re-read every poll, and a `pwmN_enable` that left manual mode is
   reclaimed and reported (`OWNERSHIP LOST`). Ownership is not a startup
   event — writing duty into a header the firmware took back succeeds, and
   the fan does not move.
2. `--dry-run` writes nothing at all, and the acceptance path for new
   configs is a dry run.
3. `scripts/protect.py` snapshots/checks the channels *this daemon must
   own* — the union of its enabled assignments and the ownership marker:

   ```bash
   python3 scripts/protect.py snapshot /tmp/fc.snap
   # ... do stuff ...
   python3 scripts/protect.py check /tmp/fc.snap   # → GUARDRAIL-OK
   ```

   `check` fails (exit 1) when a guarded channel is no longer `enable=1`
   — the fail-safe cannot raise a fan we do not own — or when the service
   is inactive; duty *drift* between checks is a NOTE, not a failure,
   because the daemon legitimately moves duty with temperature. Per-fan
   states from `status.json` are echoed, so a `failsafe` or `stalled` fan
   shows up in the same output. (The dry runs provably change nothing: the
   hermetic fake-tree tests assert byte-identical trees, and it8686 was
   verified unchanged after live runs.)

**Design note:** an earlier revision had a `protected` config list
(built-in default `["it8792:pwm1", "it8792:pwm3"]`) that refused those
channels even in firmware mode. It existed only to keep a dev-mode daemon
from stomping on a second controller that ran here, and went away with it
— guardrail 1 covers foreign owners generically. Old configs with a
`protected` key still load; the key is ignored and dropped on the next
save.

Additional rules honored everywhere:

- **Nothing is ever written outside** `/home/alex/alx_fancontrol` and
  `~/.config/alx_fancontrol/`. No systemctl start/stop/modify anywhere
  in the project (`protect.py` only *reads* `is-active`).
- Real duty writes only happen when you explicitly assign a fan (TUI or
  JSON) and run the daemon non-dry. The optional
  `scripts/real_write_smoke.sh` uses **it8686 pwm3 (SYS_FAN2)** only —
  a spinning chassis fan with a tach, never the it8792 GPU blowers —
  pre-checks `Tctl < 85 °C`, refuses to run while the live service is
  active (stop it first, start it again after), fails if any channel other
  than it8686:pwm3 moved, and self-restores (`enable→2`) on clean exit.
  Run it only when you're happy to spin that fan for ~20 s.
- On this board `pwmN_enable`: `1` = manual (software), `2` = firmware/
  BIOS SmartFan, `0` = disabled. The daemon takes over from `2`, reclaims
  channels its own previous run left at `1`, and restores exactly the
  value recorded before it first took the channel (`force_takeover` per
  assignment opts into claiming a channel held by someone else, or one the
  BIOS disabled). Note that firmware control here is **not** a safe
  fallback: unowned headers demonstrably sit at `pwm=0` (e.g. it8686 pwm2),
  which is why an unreadable source ramps a fan up instead of handing the
  channel back.
- `pwmN` duty is 0–255 in sysfs; the config uses 0–100 %.

### Definition of done (verified 2026-10-01)

1. `bash scripts/bootstrap_venv.sh` prints `textual 8.2.8`. ✅
2. `.venv/bin/pytest -q` — 97 passed (73 hermetic unit/daemon/smoke + 24 UX tests) as of 2026-10-01; the suite has since grown to 165 (see [Tests](#tests)). ✅
   The UX suite (`tests/test_tui_ux.py`) drives the TUI like a user
   (row clicks, key bindings, modals, saves) and fails any test in which
   the app swallows an exception — see IMPLEMENTATION_NOTES §12 for the
   three real bugs it caught.
3. `.venv/bin/alx-fancontrol daemon --dry-run --duration 5` logs live
   temps + intended duties and changes zero sysfs files (protect.py
   GUARDRAIL-OK before/after). ✅
4. `.venv/bin/alx-fancontrol tui` shows all four screens with live
   data; an assignment + curve edit persists to
   `~/.config/alx_fancontrol/config.json` with `.bak`; a running daemon
   reflects config changes within ~1 s (mtime hot-reload). ✅
5. `scripts/protect.py check` passes — the daemon still owns every channel
   its config assigns. ✅
6. You take over: `.venv/bin/alx-fancontrol tui` +
   `.venv/bin/alx-fancontrol daemon` (or install the unit yourself).

## Troubleshooting

- **A sensor reads −55 °C** — dead thermistor (it8686 temp6, it8792
  temp2 on this board). `dead_temp_c: -10` filters it to "no reading";
  the assignment holding logic then applies. Don't assign fans to such
  sources.
- **After a reboot, fan/source ids look wrong or missing** — hwmon
  indices reshuffle (today it8686=hwmon1, it8792=hwmon2; yesterday
  possibly different). The project never hardcodes indices; the daemon
  re-discovers by chip *name* at each start. With the systemd unit,
  `Restart=always` handles it; otherwise just restart the daemon and,
  if the TUI is open, close and reopen it (it caches the chip map).
- **`nvidia-smi` hiccups** — GPU sources go *stale* and their fans hold
  last duty; hwmon-driven fans keep moving. The failure is logged first
  + every 30th time.
- **Two k10temp packages** — this box exposes the CPU temperature twice
  (two packages/sockets). Use `chip_index: 0/1` on `k10temp` sources to
  disambiguate (seeded as `cpu0`/`cpu1`).
- **TUI says "Config changed externally"** — someone/something edited
  the JSON since the TUI loaded it: Reload (discard in-TUI edits) or
  Overwrite. Don't run two TUIs at once.
- **A fan won't move** — check Overview state: `manual-conflict` means
  another process has the channel in manual mode (`fancontrol`, a second
  alx_fancontrol, or a tool that exited without restoring it);
  `disabled` means
  `pwmN_enable=0` (BIOS disabled the header); `unresolved` means the
  chip is gone (reboot reshuffle).
- **`OWNERSHIP LOST` in the log / `state: "ownership-lost"`** — the
  firmware took a header back while we ran, so our duty writes were being
  ignored; the daemon reclaims it every poll, but the *cause* is off-box:
  a BIOS/EC fan policy still managing that header (turn Q-Fan/fan control
  off for headers the daemon owns), a second controller, or hot-replug.
  `sudo python3 scripts/protect.py check` names every channel we no longer
  hold. If it recurs on the same header at boot, the BIOS is re-asserting
  control after the daemon takes over.
- **Fan reads 0 RPM but the log says high duty, and nothing recovers it**
  — that is the incident this feature exists for (11 minutes at 100 %,
  GPU0 to 73 °C, only a reboot cleared it). Read `status.json`: `enable`
  ≠ 1 means ownership loss; `enable` = 1 with 0 RPM means the fan or its
  tach lead. Note these blowers need **~8 % duty to start from a stop**
  (raw 20; measured sweep archived in
  `/var/backups/gpu-fanctl-*.tar.gz:fan-calibration.json`) and keep
  spinning down to ~6 %, so a curve whose bottom sits below 8 % can park a
  blower that will never restart on its own.

## Layout

```
alx_fancontrol/
├── pyproject.toml               # packaging; alx-fancontrol entry point; [tui]/[test] extras
├── alx-fancontrol.service       # installed by install.sh (ExecStart -> /opt snapshot)
├── alx-fancontrol               # launcher -> /usr/local/bin (sudo alx-fancontrol …)
├── install.sh                   # build /opt snapshot + install/start service (+ --dry-run)
├── uninstall.sh                 # stop+remove service & /opt (+ --purge for /etc, --dry-run)
├── run-local.sh                 # interim mode: daemon/tui from the source tree, pinned config
├── IMPLEMENTATION_NOTES.md      # design decisions + TUI bug log (see §12)
├── alx_fancontrol/
│   ├── cli.py                   # daemon | tui | check (bare command = tui)
│   ├── config.py                # Config model, load/save (atomic+.bak)/validate/seed
│   ├── hwmon.py                 # chip discovery by name, temp/fan/pwm/enable R/W, nvidia-smi, sensors.d labels
│   ├── sources.py               # runtime source discovery (hwmon + nvidia → canonical ids)
│   ├── curve.py                 # piecewise-linear eval, bounds, rate limit, pct<->255
│   ├── daemon.py                # poll loop, takeover/restore, signals, --dry-run/--once/--duration
│   ├── status.py                # status.json writer/reader
│   └── tui/
│       ├── app.py               # FanControlApp: nav, live worker, save + stale guard
│       ├── reader.py            # read-only 1 s worker thread (hwmon/nvidia/status)
│       ├── smoke.py             # run_test pilot (python -m alx_fancontrol.tui.smoke)
│       └── screens/             # base shell + overview / assign / curves / sources
├── scripts/
│   ├── bootstrap_venv.sh        # venv without apt + get-pip + editable install
│   ├── protect.py               # ownership guardrail snapshot/check (read-only)
│   ├── real_write_smoke.sh      # OPTIONAL it8686-pwm3 real-write test (opt-in)
│   └── verify_install.sh        # full install-path check in a throwaway venv (no sudo)
├── tests/                       # 165 tests, hermetic via the FakeMachine (see below)
└── .venv/                       # dev venv, created by bootstrap (gitignored)
```

Installed by `install.sh` (runtime, root-owned):

```
/opt/alx_fancontrol/venv/        # frozen snapshot: non-editable pip install of the source
/usr/local/bin/alx-fancontrol    # launcher (sudo alx-fancontrol …; /opt first, dev venv fallback)
/etc/alx_fancontrol/config.json  # system config (+ config.json.bak)
/run/alx_fancontrol/status.json  # daemon liveness (RuntimeDirectory=)
/var/log/alx_fancontrol/         # daemon.log (LogsDirectory=)
```

## Tests

165 tests, all hermetic. The app's hwmon layer is monkeypatched to a
**synthetic machine** — the `machine` fixture (`tests/conftest.py` +
`tests/fake_machine.py`): a fake /sys tree, fake sensors.d labels and a
fake GPU, whose values the tests INJECT explicitly:

```python
machine.set_temp("k10temp", 1, 61.5)     # inject a reading
machine.set_rpm("it8686", 3, 4680)
machine.set_pwm("it8686", 3, duty=128, enable=1)
machine.add_gpu("44:00.0", 69.0, "Tesla V100")
machine.source_ids() / machine.fan_ids() # what the app will detect
machine.live_payload()                   # a LiveUpdate dict of current values
```

No test assumes a value about the real machine: if a test cares about a
reading, it injects one and asserts on the value it injected. The TUI
tests additionally drive the app the way a user does (clicks, keys,
modals, saves) with a meta-fixture that fails the test if the TUI
swallows any exception.
