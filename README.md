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
- Safety is in code, not in your memory: channels already in manual
  mode by another process (e.g. the GPU blowers owned by the existing
  `gpu-fanctl` service) are refused at takeover, and every exit path
  restores firmware control. `--dry-run` demonstrates all of it without
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
source list. `1` → Overview → watch duties/RPMs move. (You *can* pick
the gpu-fanctl blower headers — but the daemon refuses to take over any
channel another process holds in manual mode, so they stay with
`gpu-fanctl` while that service runs.)

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
changed), the daemon skips it with a `source-missing` warning and the TUI
lets you re-pick it. v1 configs (with a `sources` section) are migrated
automatically on load — the TUI re-saves them as v2 on first open.

```json
{
  "version": 2,
  "config": {
    "poll_seconds": 1.0,
    "max_rate": 6,
    "floor_duty": 5,
    "dead_temp_c": -10
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
| `config.max_rate` | fallback max duty change **per poll**, percent (6 ≈ gpu-fanctl's proven 16/0–255) — overridden per assignment |
| `config.floor_duty` | fallback per-assignment floor, **default 5%** (quiet idle; stall protection) — overridden per assignment |
| `config.dead_temp_c` | readings below this count as "no reading" (kills the −55 °C dead thermistors) |
| `sources.*.kind` | `hwmon` or `nvidia` |
| `sources.*.chip` / `.temp` / `.chip_index` | hwmon source: chip **name prefix** (never `hwmonN` — indices reshuffle across reboots), temp index, and which instance of a duplicated prefix (two `k10temp` packages on this box → `chip_index` 0/1) |
| `sources.*.pci_bus` | nvidia source: normalized PCI bus, e.g. `08:00.0` |
| `curves.*.points` | `[temp_c, duty_pct]` pairs, ≥1, temps strictly increasing, duty 0–100. Eval clamps flat beyond the ends. |
| `assignments.*.fans` | list of `chip:pwmN` |
| `assignments.*.tach` | optional `chip:pwmN → chip:fanN` RPM display map (the TUI auto-guesses it from sensors.d labels) |
| `assignments.*.floor_duty` / `.max_duty` / `.max_rate` | **per-fan** floor / ceiling / max duty change per poll — override the global fallbacks (floor ≤ max enforced) |

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
  A failing call marks GPU sources *stale* and **holds** their duties —
  CPU-side assignments keep running (unlike gpu-fanctl, which slept 5 s
  and skipped the whole loop).
- Source disappears mid-run → that assignment's fans **hold last duty**
  (warn in log + `status.json`). Source reappears → resume. Source never
  appeared at startup → fans left to firmware, re-checked each poll
  (safe against "dead source pins a fan low").
- Fan write fails → keep last duty, log first + every 10th failure,
  `state=write-error` in status, retry every poll.
- **Takeover rules** (per channel, every run):
  - `pwmN_enable=1` (already manual — someone else owns it, e.g.
    gpu-fanctl on the GPU blowers) → refused (ERROR). Re-checked every
    poll: a fan becomes eligible the moment the other process releases
    it;
  - `pwmN_enable=0` (disabled) → skipped (WARN);
  - `pwmN_enable=2` (firmware) → taken over (`enable→1`), original value
    recorded and **restored exactly** in `finally` (clean SIGTERM/SIGINT
    exit via signal handlers).
- Per-fan bounds and slew, in this order: curve temp → duty, then
  **floor** (assignment `floor_duty`, else global `config.floor_duty`,
  default **5%**), then **ceiling** (assignment `max_duty`, else 100),
  then the **rate limiter** (assignment `max_rate`, else global
  `config.max_rate`, default 6 %/poll) steps the duty toward the target
  — a fan can't jump 0→100 in one poll.

### status.json

Written by the daemon each poll; read by the TUI:

```json
{ "pid": 1234, "ts": 1719e9, "config_mtime": 1719e8, "dry_run": false,
  "sources": { "it8686:temp1": {"label": "System 1", "temp_c": 46.0, "stale": false} },
  "fans":    { "it8686:pwm3": {"label": "SYS_FAN2", "duty_pct": 27, "rpm": 3229,
                                "orig_enable": 2, "state": "ok"} },
  "errors": {} }
```

`state`: `ok | manual-conflict | disabled | unresolved |
source-missing | write-error | curve-missing | idle`.

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
   `RuntimeDirectory=`/`LogsDirectory=` create `/run/alx_fancontrol`
   (status.json) and `/var/log/alx_fancontrol` (daemon.log); as root the
   daemon finds its config in `/etc` on its own (no path pinning).
6. **Verifies**: unit active, `gpu-fanctl` still active, and the
   GPU-blower channels still at `enable=1` (i.e. still owned by
   gpu-fanctl — the daemon only takes over channels in firmware mode).

`Restart=always` covers the hwmon-reshuffle case (see troubleshooting).

`uninstall.sh` reverses it: `systemctl disable --now` (SIGTERM → the
daemon returns every fan it drove to firmware control), removes the unit,
the launcher and `/opt/alx_fancontrol`; `--purge` also removes
`/etc/alx_fancontrol`
(your assignments/curves) and the log dir. The source tree is never
touched.

## Safety notes

**The running `gpu-fanctl.service` owns it8792 pwm1/pwm3** (the V100
GPU blower headers; `pwmN_enable=1`, actively duty-cycled). alx_fancontrol
coexists with it under three independent guardrails:

1. Any channel with `pwmN_enable=1` at takeover is refused (someone
   else is in manual mode — that's exactly the state gpu-fanctl leaves
   its channels in). It is re-checked every poll, so a channel becomes
   eligible only while it actually sits in firmware mode.
2. `--dry-run` writes nothing at all, and the acceptance path for new
   configs is a dry run.
3. `scripts/protect.py` snapshots/checks it8792 pwm1/pwm3 duty+enable
   and the service state:

   ```bash
   python3 scripts/protect.py snapshot /tmp/fc.snap
   # ... do stuff ...
   python3 scripts/protect.py check /tmp/fc.snap   # → GUARDRAIL-OK
   ```

   `check` treats an **enable** change or an inactive service as a
   violation (exit 1); duty *drift* between checks is reported as a
   NOTE, not a failure — gpu-fanctl is a live service and legitimately
   moves duty with GPU temperature between two checks. (The dry runs
   provably change nothing: the hermetic fake-tree tests assert byte-
   identical trees, and it8686 was verified unchanged after live runs.)

**Design note:** an earlier revision had a `protected` config list
(built-in default `["it8792:pwm1", "it8792:pwm3"]`) that refused those
channels even in firmware mode. It was removed — it only ever existed
to keep the dev daemon from stomping on the running gpu-fanctl, which
guardrail 1 already covers. Consequence to know: if you *stop*
gpu-fanctl and its channels return to firmware mode, the blower headers
become assignable like any other fan (the daemon will take them over if
assigned). Old configs with a `protected` key still load; the key is
ignored and dropped on the next save.

Additional rules honored everywhere:

- **Nothing is ever written outside** `/home/alex/alx_fancontrol` and
  `~/.config/alx_fancontrol/`. No systemctl start/stop/modify anywhere
  in the project (`protect.py` only *reads* `is-active`).
- Real duty writes only happen when you explicitly assign a fan (TUI or
  JSON) and run the daemon non-dry. The optional
  `scripts/real_write_smoke.sh` uses **it8686 pwm3 (SYS_FAN2)** only —
  a spinning chassis fan with a tach, never the it8792 GPU blowers —
  pre-checks `Tctl < 85 °C`, and self-restores (`enable→2`) on clean
  exit. Run it only when you're happy to spin that fan for ~20 s.
- On this board `pwmN_enable`: `1` = manual (software), `2` = firmware/
  BIOS SmartFan, `0` = disabled. The daemon only takes over from `2`
  and restores exactly the value it found.
- `pwmN` duty is 0–255 in sysfs; the config uses 0–100 %.

### Definition of done (verified 2026-10-01)

1. `bash scripts/bootstrap_venv.sh` prints `textual 8.2.8`. ✅
2. `.venv/bin/pytest -q` — 97 passed (73 hermetic unit/daemon/smoke + 24 UX tests) as of 2026-10-01; the suite has since grown to 134 (see [Tests](#tests)). ✅
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
5. `scripts/protect.py check` passes and `gpu-fanctl` was never
   stopped. ✅
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
  another process has the channel in manual mode (e.g. you have
  gpu-fanctl running on a non-default channel); `disabled` means
  `pwmN_enable=0` (BIOS disabled the header); `unresolved` means the
  chip is gone (reboot reshuffle).

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
│   ├── protect.py               # gpu-fanctl guardrail snapshot/check (read-only)
│   ├── real_write_smoke.sh      # OPTIONAL it8686-pwm3 real-write test (opt-in)
│   └── verify_install.sh        # full install-path check in a throwaway venv (no sudo)
├── tests/                       # 134 tests, hermetic via the FakeMachine (see below)
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

134 tests, all hermetic. The app's hwmon layer is monkeypatched to a
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
