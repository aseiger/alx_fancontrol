"""Config model: load/save/validate/seed.

Location: $ALX_FANCONTROL_CONFIG or ~/.config/alx_fancontrol/config.json
(honoring $XDG_CONFIG_HOME) — deliberately NOT /etc. Runtime side files
live next to it: config.json.bak, status.json, daemon.log.

Writers: only the TUI (and humans — it's plain JSON). The daemon never
writes the config; it hot-reloads on mtime change every poll.

Duty is percent 0-100 in the config; raw 0-255 only at the sysfs boundary.
"""
from __future__ import annotations

import copy
import json
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from alx_fancontrol import hwmon

CONFIG_VERSION = 2
# v1 -> v2: sources moved OUT of the config (they are a machine property,
# discovered at runtime — see sources.py). v1 assignments referenced
# hand-made source ids ("cpu0", "sys1", ...); the migration rewrites them
# to the canonical ids the runtime discovery produces.

FAN_ID_RE = re.compile(r"^\w+:pwm\d+$")
PCI_BUS_RE = re.compile(r"^\d{2}:\d{2}\.\d$")


@dataclass
class Config:
    config: dict
    curves: dict
    assignments: dict


DEFAULTS = {
    "config": {
        "poll_seconds": 1.0,
        "max_rate": 6,          # max duty change, percent per poll
        "floor_duty": 5,        # global default per-assignment floor
        "dead_temp_c": -10,     # readings below this count as "no reading"
        # ---- fail-safe: what a fan does when its source goes unreadable --
        # An unreadable source must never mean "fan keeps idling": after
        # `failsafe_after` consecutive dead polls the assignment drives its
        # fans to `failsafe_duty` (ramping at failsafe_max_rate %/poll).
        # Set `"failsafe": false` on an assignment to opt out (keeps the
        # old hold-last-duty behaviour), e.g. for a sensor that is gone for
        # good — but prefer deleting/disabling such an assignment.
        "failsafe_duty": 100,
        "failsafe_after": 5,
        "failsafe_max_rate": 25,
        # A GPU reading below this is not a real temperature (a card that
        # dropped off the bus reports 0), so it counts as "no reading".
        "gpu_temp_min_valid_c": 15,
        # ---- liveness: nothing may block the poll loop -----------------
        "smi_timeout_s": 3.0,   # nvidia-smi call timeout (0 = no timeout)
        "watchdog_seconds": 30,  # poll-loop watchdog, 0 = off; exits so
                                 # systemd restarts us (fans keep their duty)
        # ---- stalled-fan alarm: duty high but tach reads ~0 RPM --------
        "stall_min_duty": 20,
        "stall_after": 10,
        # ---- a confirmed stall is also an order, not only a log line ------
        # Escalation reaches a person; this reaches the header, and only the
        # header. A fan reading 0 RPM is pinned to `stall_full_duty` — past
        # the curve's ceiling and past the rate limiter, because the curve is
        # what left it under-powered and a stalled fan needs no soft start.
        # Then we try to restart it: a brief drop to 0 so a jammed blade can
        # swing free, then full voltage, judged by the tach. Attempts per
        # round are bounded and the rounds are spaced, so a seized winding
        # does not cook itself (or the fan driver) on stall current; between
        # rounds and after them the header stays pinned, never parked.
        "stall_full_duty": 100,
        "stall_kick": True,          # false = pin only, never pulse to 0
        "stall_kick_attempts": 3,
        "stall_kick_stop_s": 0.5,    # at 0 before each lunge
        "stall_kick_run_s": 3.0,     # full duty before judging the tach
        "stall_kick_backoff_s": 30,  # rest between rounds (fan + driver heat)
        "stall_kick_cooldown_s": 60,  # spinning again: hold full, then hand
                                      # the duty back to the curve
        # ---- escalation once a fan is confirmed not cooling -------------
        # A stalled blower is not something we can fix by writing duty, so the
        # alarm has to reach a person. Fans only: there is deliberately no
        # knob here for touching the GPUs (clocks, power limits, ...).
        "stall_alert_command": "",   # shell command; see README "Escalation"
        "stall_alert_timeout_s": 5.0,
        "stall_alert_repeat_s": 300,  # re-alert while it lasts (0 = once only)
    },
    "curves": {
        "default":  {"label": "Default",
                     "points": [[40, 20], [55, 30], [65, 70], [75, 100]]},
        "gpu_v100": {"label": "V100 blower",
                     "points": [[50, 26], [60, 40], [66, 75], [72, 100]]},
    },
    "assignments": {},
}


@dataclass(frozen=True)
class Field:
    """One `config.*` option, described once for validation *and* the GUI.

    Before this the ranges lived inside `validate()` and nowhere else, so the
    TUI could not say what a knob means, where its default comes from, or what
    a legal value is without re-deriving all three — and did not, which is how
    F1 (22 global options, no editor anywhere) survived this long.

    `lo`/`hi` are the bounds validate() enforces (None = unbounded). `kind`
    shapes the input widget only: numbers still accept int or float either
    way, as they always have.
    """

    key: str
    label: str
    kind: str            # "number" | "bool" | "str"
    group: str
    lo: float | None = None
    hi: float | None = None
    unit: str = ""
    help: str = ""


# Order is the order the Settings screen shows them in; the numeric bounds
# are applied in this order, which is the order validate() always used.
CONFIG_FIELDS: tuple[Field, ...] = (
    Field("poll_seconds", "Poll interval", "number", "Cooling loop",
          lo=0.001, unit="s",
          help="How often temperatures are read and duty recomputed."),
    Field("max_rate", "Max duty change", "number", "Cooling loop",
          lo=0, hi=100, unit="% per poll",
          help="Limits how fast duty may move per poll, so a spike in one "
               "reading cannot slam the fans. Per-assignment override: "
               "max_rate on the Assign screen."),
    Field("floor_duty", "Minimum duty", "number", "Cooling loop",
          lo=0, hi=100, unit="%",
          help="Duty never falls below this, whatever the curve says. The "
               "default for assignments that do not set their own floor."),
    Field("dead_temp_c", "Dead reading below", "number", "Cooling loop",
          unit="\u00b0C",
          help="A temperature at or below this counts as \"no reading\" "
               "rather than as very cold, and starts the fail-safe count."),

    Field("failsafe_duty", "Fail-safe duty", "number",
          "Fail-safe (source unreadable)", lo=0, hi=100, unit="%",
          help="Where a fan goes when its source stops answering: ramp to "
               "this duty and stay. 100 = fans full on."),
    Field("failsafe_after", "Fail-safe after", "number",
          "Fail-safe (source unreadable)", lo=0, unit="polls",
          help="Consecutive dead polls before the fail-safe engages \u2014 "
               "one bad reading should not spin the fans up."),
    Field("failsafe_max_rate", "Fail-safe ramp", "number",
          "Fail-safe (source unreadable)", lo=0, hi=100, unit="% per poll",
          help="How fast the fail-safe ramps, deliberately faster than "
               "max_rate: a sensor that is gone is not a reason to be gentle."),

    Field("gpu_temp_min_valid_c", "GPU reading valid above", "number",
          "Readings and liveness", unit="\u00b0C",
          help="A GPU reporting below this dropped off the bus; it counts as "
               "no reading instead of as a cold card."),
    Field("smi_timeout_s", "nvidia-smi timeout", "number",
          "Readings and liveness", lo=0, unit="s",
          help="Give up on an nvidia-smi call after this (0 = no timeout). "
               "Nothing may block the poll loop \u2014 a hung call would stop "
               "every fan being controlled."),
    Field("watchdog_seconds", "Watchdog", "number", "Readings and liveness",
          lo=0, unit="s",
          help="If a poll takes longer than this the daemon exits, so "
               "systemd restarts it (fans keep their last duty). 0 = off."),

    Field("stall_min_duty", "Stall alarm above", "number", "Stalled fan",
          lo=0, hi=100, unit="%",
          help="Ask for at least this much duty before ~0 RPM counts as a "
               "stall \u2014 below it, 0 RPM is just an idle fan."),
    Field("stall_after", "Stall confirmed after", "number", "Stalled fan",
          lo=1, unit="polls",
          help="How long high duty with no RPM must last before the stall is "
               "believed."),
    Field("stall_full_duty", "Pin a stalled fan to", "number",
          "Stalled fan", lo=0, hi=100, unit="%",
          help="A stalled header is pinned here \u2014 past the curve's "
               "ceiling and the rate limit, because the curve is what left it "
               "under-powered. Must be >= stall_min_duty, or pinning would "
               "silence the detector that pinned it."),
    Field("stall_kick", "Try to restart it", "bool", "Stalled fan",
          help="Off means: pin to full and leave it. On also pulses the "
               "header to 0 so a jammed blade can swing free, then back to "
               "full, judging the result by the tach."),
    Field("stall_kick_attempts", "Kick attempts per round", "number",
          "Stalled fan", lo=0,
          help="How many stop-and-lunge tries in one round, so a seized "
               "winding does not cook itself (or the fan driver) on stall "
               "current."),
    Field("stall_kick_stop_s", "Hold at 0 for", "number", "Stalled fan",
          lo=0, unit="s",
          help="Time parked at 0 duty before each lunge, so the blade can "
               "swing free."),
    Field("stall_kick_run_s", "Run at full for", "number", "Stalled fan",
          lo=0, unit="s",
          help="Time at full duty before judging whether it restarted."),
    Field("stall_kick_backoff_s", "Rest between rounds", "number",
          "Stalled fan", lo=0, unit="s",
          help="Rest between rounds of attempts \u2014 the fan and the driver "
               "both heat up on stall current."),
    Field("stall_kick_cooldown_s", "Hold after restart", "number",
          "Stalled fan", lo=0, unit="s",
          help="Once it is spinning again: hold full duty this long, then "
               "hand control back to the curve."),

    Field("stall_alert_command", "Alert command", "str", "Escalation",
          help="Shell command run when a fan is confirmed stalled \u2014 the "
               "part that reaches a person. Empty = no alert. See README "
               "\"Escalation\"."),
    Field("stall_alert_timeout_s", "Alert timeout", "number", "Escalation",
          lo=0, unit="s",
          help="Kill the alert command after this; an alert must never be "
               "what blocks the poll loop."),
    Field("stall_alert_repeat_s", "Repeat alert every", "number",
          "Escalation", lo=0, unit="s",
          help="Keep re-alerting while the stall lasts (0 = alert once)."),
)

#: key -> Field, for the GUI and for error messages
BY_KEY: dict[str, Field] = {f.key: f for f in CONFIG_FIELDS}


SYSTEM_CONFIG_DIR = Path("/etc/alx_fancontrol")


def default_path() -> Path:
    """$ALX_FANCONTROL_CONFIG, else /etc/alx_fancontrol/config.json as
    root, else ~/.config/alx_fancontrol/config.json.

    This is a SYSTEM daemon (the service runs as root for autonomous
    operation), so as root the config lives in /etc. A non-root
    invocation (foreground dev/testing) uses the home dir, where it can
    actually save without sudo."""
    env = os.environ.get("ALX_FANCONTROL_CONFIG")
    if env:
        return Path(env).expanduser()
    if os.geteuid() == 0:
        return SYSTEM_CONFIG_DIR / "config.json"
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".config"
    return base / "alx_fancontrol" / "config.json"


def _deep_merge(base: dict, over: dict) -> dict:
    """Merge `over` onto `base` (dicts recursively, other values replace)."""
    out = copy.deepcopy(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def to_dict(cfg: Config) -> dict:
    return {
        "version": CONFIG_VERSION,
        "config": cfg.config,
        "curves": cfg.curves,
        "assignments": cfg.assignments,
    }


def _migrate_v1(doc: dict) -> dict:
    """v1 doc (with a 'sources' section and hand-made source ids) -> v2.
    Assignment source references are rewritten from the legacy id to the
    canonical id the legacy source descriptor would produce at runtime."""
    from alx_fancontrol import sources as sources_mod
    legacy = doc.get("sources") or {}
    counts: dict[str, int] = {}
    for s in legacy.values():
        if isinstance(s, dict) and s.get("kind") == "hwmon":
            chip = s.get("chip")
            if chip:
                counts[chip] = counts.get(chip, 0) + 1
    remap: dict[str, str] = {}
    for sid, s in legacy.items():
        if not isinstance(s, dict):
            continue
        if s.get("kind") == "nvidia" or s.get("kind") == "gpu":
            if s.get("pci_bus"):
                remap[sid] = sources_mod.canonical_id(
                    "gpu", pci_bus=s["pci_bus"])
        elif s.get("kind") == "hwmon" and s.get("chip"):
            remap[sid] = sources_mod.canonical_id(
                "hwmon", s["chip"], s.get("chip_index", 0),
                s.get("temp", 1),
                repeated=counts.get(s["chip"], 0) > 1)
    assignments = doc.get("assignments")
    if isinstance(assignments, dict):
        for a in assignments.values():
            if isinstance(a, dict) and a.get("source") in remap:
                a["source"] = remap[a["source"]]
    # carry over ONLY the keys the v2 model actually has — writing explicit
    # empties would clobber the DEFAULTS merge (e.g. a minimal
    # {"version": 1} must still get the defaults). The legacy v1
    # "protected" key is dropped: channels owned by another process are
    # refused at takeover via the manual-mode (enable=1) check instead.
    out = {"version": CONFIG_VERSION}
    for k in ("config", "curves", "assignments"):
        if k in doc:
            out[k] = doc[k]
    return out


def _merge_defaults(doc: dict) -> dict:
    """Fill in what the file does not say, without overruling what it does.

    Two different meanings of "absent", treated differently on purpose:

      * **`config.*` keys merge one by one.** A release that adds a knob must
        not require every existing config file to be edited, and deleting a
        key from the file genuinely means "use the built-in default" — which
        is exactly what the GUI writes for a blank field.
      * **`curves` and `assignments` are taken as written when present.**
        Merging them entry by entry made a curve un-deletable: DEFAULTS
        re-injected `default` and `gpu_v100` on every load, so the operator's
        Delete did not stick, the TUI showed the curve again after a reload,
        and the daemon kept resolving assignments against a curve nobody had
        asked for (F14). A file with NO `curves` key at all still gets the
        stock pair — that is a minimal or freshly migrated file, not a
        decision to have no curves.
    """
    merged = {"config": _deep_merge(DEFAULTS["config"],
                                     doc.get("config") or {})}
    for section in ("curves", "assignments"):
        merged[section] = copy.deepcopy(
            doc[section] if section in doc else DEFAULTS[section])
    return merged


def load(path) -> Config:
    """Parse (+ migrate v1 -> v2) + fill in defaults. Raises
    OSError/ValueError on unreadable or invalid JSON (callers decide:
    daemon start -> exit 2, hot reload -> keep old config, `check` ->
    exit 1). Does NOT validate — call validate() for that.

    See `_merge_defaults` for what counts as absent: per-key for `config`,
    per-section for curves and assignments.
    """
    with open(path) as f:
        doc = json.load(f)
    if not isinstance(doc, dict):
        raise ValueError(f"{path}: config root must be a JSON object")
    if int(doc.get("version", 1)) < CONFIG_VERSION:
        doc = _migrate_v1(doc)
    merged = _merge_defaults(doc)
    return Config(
        config=merged["config"],
        curves=merged["curves"],
        assignments=merged["assignments"],
    )


def validate(cfg: Config) -> tuple[list[str], list[str]]:
    """(errors, warnings).

    Hard errors block `check`/daemon start. Warnings mean the daemon skips
    the offending assignment but keeps running — this is what makes
    hot-reload resilient when the user deletes a source mid-run.
    """
    errors: list[str] = []
    warnings: list[str] = []

    c = cfg.config
    for f in CONFIG_FIELDS:
        if f.kind != "number":
            continue          # str/bool checked on their own below
        key = f.key
        if key not in c:
            continue          # absent = the DEFAULTS value applies (load merges)
        v = c.get(key)
        if not isinstance(v, (int, float)) or isinstance(v, bool):
            errors.append(f"config.{key} must be a number, got {v!r}")
        elif f.lo is not None and v < f.lo:
            errors.append(f"config.{key} must be >= {f.lo}")
        elif f.hi is not None and v > f.hi:
            errors.append(f"config.{key} must be <= {f.hi}")

    cmd = c.get("stall_alert_command", "")
    if cmd is not None and not isinstance(cmd, str):
        errors.append(f"config.stall_alert_command must be a string, got {cmd!r}")

    kick = c.get("stall_kick", True)
    if not isinstance(kick, bool):
        errors.append(f"config.stall_kick must be true or false, got {kick!r}")

    # Pinning a stalled fan BELOW the threshold that defines "we are asking
    # for real duty" would disarm the very detector that started the episode:
    # the fan would sit at, say, 10 %, un-alarmed and un-kicked, which is the
    # failure this whole path exists to catch. Refuse the pairing outright.
    full, mind = c.get("stall_full_duty"), c.get("stall_min_duty")
    if all(isinstance(v, (int, float)) and not isinstance(v, bool)
           for v in (full, mind)) and full < mind:
        errors.append(
            f"config.stall_full_duty ({full}) must be >= stall_min_duty "
            f"({mind}): pinning a stalled fan under the alarm threshold would "
            "disarm the detector that pinned it")

    # (sources are not in the config anymore — they are discovered at
    # runtime; an assignment whose source is not detected is skipped with
    # a warning by the daemon, not a config error)

    for cid, crv in cfg.curves.items():
        if not isinstance(crv, dict):
            errors.append(f"curve {cid}: not an object")
            continue
        pts = crv.get("points")
        if not isinstance(pts, list) or len(pts) < 1:
            errors.append(f"curve {cid}: needs >= 1 point")
            continue
        prev = None
        for i, p in enumerate(pts):
            if not isinstance(p, (list, tuple)) or len(p) != 2:
                errors.append(f"curve {cid}: point {i} must be [temp, duty]")
                break
            t, d = p
            if not isinstance(t, (int, float)) or isinstance(t, bool):
                errors.append(f"curve {cid}: point {i} temp must be a number")
                break
            if not isinstance(d, (int, float)) or isinstance(d, bool) \
                    or not (0 <= d <= 100):
                errors.append(f"curve {cid}: point {i} duty must be 0-100")
                break
            if prev is not None and t <= prev:
                errors.append(
                    f"curve {cid}: temps must be strictly increasing (point {i})")
                break
            prev = t

    seen_fans: dict[str, list[str]] = {}
    for aid, a in cfg.assignments.items():
        if not isinstance(a, dict):
            errors.append(f"assignment {aid}: not an object")
            continue
        src = a.get("source")
        if not isinstance(src, str) or not src:
            errors.append(f"assignment {aid}: 'source' must be a source id "
                          f"string (see `alx-fancontrol check` for the "
                          f"detected ids)")
        if a.get("curve") not in cfg.curves:
            warnings.append(f"assignment {aid}: unknown curve "
                            f"{a.get('curve')!r} (skipped at runtime)")
        fans = a.get("fans")
        if not isinstance(fans, list) or not fans:
            errors.append(f"assignment {aid}: 'fans' must be a non-empty list")
        else:
            for f in fans:
                if not isinstance(f, str) or not FAN_ID_RE.match(f):
                    errors.append(f"assignment {aid}: bad fan id {f!r}")
                else:
                    seen_fans.setdefault(f, []).append(aid)
        fl, mx = a.get("floor_duty"), a.get("max_duty")
        for name, v in (("floor_duty", fl), ("max_duty", mx)):
            if v is not None and (not isinstance(v, (int, float))
                                  or isinstance(v, bool) or not (0 <= v <= 100)):
                errors.append(f"assignment {aid}: {name} must be 0-100")
        mr = a.get("max_rate")
        if mr is not None and (not isinstance(mr, (int, float))
                               or isinstance(mr, bool) or mr < 0):
            errors.append(f"assignment {aid}: max_rate must be >= 0 "
                          f"(percent per poll)")
        if isinstance(fl, (int, float)) and isinstance(mx, (int, float)) \
                and not isinstance(fl, bool) and fl > mx:
            errors.append(f"assignment {aid}: floor_duty {fl} > max_duty {mx}")
        tach = a.get("tach")
        if tach is not None:
            if not isinstance(tach, dict):
                errors.append(f"assignment {aid}: 'tach' must be a mapping")
            else:
                for f, t in tach.items():
                    if not isinstance(t, str) or not hwmon.TACH_ID_RE.match(t):
                        errors.append(f"assignment {aid}: bad tach id {t!r} "
                                      f"for fan {f!r}")
        # ---- per-assignment fail-safe / takeover overrides --------------
        fs = a.get("failsafe")
        if fs is not None and not isinstance(fs, bool):
            errors.append(f"assignment {aid}: 'failsafe' must be true/false")
        for name, lo, hi in (("failsafe_duty", 0, 100),
                             ("failsafe_after", 0, None)):
            v = a.get(name)
            if v is not None and (not isinstance(v, (int, float))
                                  or isinstance(v, bool)):
                errors.append(f"assignment {aid}: {name} must be a number")
            elif v is not None and lo is not None and v < lo:
                errors.append(f"assignment {aid}: {name} must be >= {lo}")
            elif v is not None and hi is not None and v > hi:
                errors.append(f"assignment {aid}: {name} must be <= {hi}")
        ft = a.get("force_takeover")
        if ft is not None and not isinstance(ft, bool):
            errors.append(f"assignment {aid}: 'force_takeover' must be "
                          f"true/false")
        if fs is False:
            warnings.append(
                f"assignment {aid}: fail-safe disabled — its fans keep "
                f"their last duty (they do NOT ramp up) if "
                f"{a.get('source')!r} becomes unreadable")
    # a fan shared by several assignments is LEGAL: the daemon drives it
    # to the highest duty any of them requests (see daemon._poll) — warn
    # so the user knows the rule when they save such a config
    for f, owners in sorted(seen_fans.items()):
        if len(owners) > 1:
            warnings.append(
                f"fan {f} is controlled by {len(owners)} assignments "
                f"({', '.join(owners)}) — driven to the highest requested "
                f"duty")
    return errors, warnings


def save(cfg: Config, path) -> None:
    """Atomic write + .bak of the PREVIOUS content.

    Ordering (replicates fan-calibrate.py's atomic-write pattern, with the
    .bak taken before the replace so .bak always holds the previous content):
      1. if path exists: shutil.copy2(path, path + ".bak")
      2. write path + ".tmp", flush + fsync
      3. os.replace(tmp, path)   <- readers never see partial JSON
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        shutil.copy2(path, path.with_name(path.name + ".bak"))
    tmp = str(path) + ".tmp"
    with open(tmp, "w") as f:
        json.dump(to_dict(cfg), f, indent=2)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def seed_if_missing(path) -> Config:
    """First-run seeding: if the config file is missing, write a v2 config
    with default curves but ZERO assignments (a fresh daemon controls
    nothing until the user assigns in the TUI). Sources are NOT seeded —
    they are discovered at runtime (sources.discover). If the file exists,
    just load it (migrating v1)."""
    path = Path(path)
    if path.exists():
        return load(path)
    cfg = Config(
        config=copy.deepcopy(DEFAULTS["config"]),
        curves=copy.deepcopy(DEFAULTS["curves"]),
        assignments={},
    )
    save(cfg, path)
    return cfg
