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
    protected: list
    curves: dict
    assignments: dict


DEFAULTS = {
    "config": {
        "poll_seconds": 1.0,
        "max_rate": 6,          # max duty change, percent per poll
        "floor_duty": 5,        # global default per-assignment floor
        "dead_temp_c": -10,     # readings below this count as "no reading"
    },
    # Owned by the running gpu-fanctl service — never take over, never write.
    # Kept in DEFAULTS so even a hand-written minimal config stays safe.
    "protected": ["it8792:pwm1", "it8792:pwm3"],
    "curves": {
        "default":  {"label": "Default",
                     "points": [[40, 20], [55, 30], [65, 70], [75, 100]]},
        "gpu_v100": {"label": "V100 blower",
                     "points": [[50, 26], [60, 40], [66, 75], [72, 100]]},
    },
    "assignments": {},
}


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
        "protected": cfg.protected,
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
    # carry over ONLY the keys the v1 doc actually had — writing explicit
    # empties would clobber the DEFAULTS merge (e.g. a minimal
    # {"version": 1} must still get the protected defaults)
    out = {"version": CONFIG_VERSION}
    for k in ("config", "protected", "curves", "assignments"):
        if k in doc:
            out[k] = doc[k]
    return out


def load(path) -> Config:
    """Parse (+ migrate v1 -> v2) + merge over DEFAULTS. Raises
    OSError/ValueError on unreadable or invalid JSON (callers decide:
    daemon start -> exit 2, hot reload -> keep old config, `check` ->
    exit 1). Does NOT validate — call validate() for that."""
    with open(path) as f:
        doc = json.load(f)
    if not isinstance(doc, dict):
        raise ValueError(f"{path}: config root must be a JSON object")
    if int(doc.get("version", 1)) < CONFIG_VERSION:
        doc = _migrate_v1(doc)
    merged = _deep_merge(DEFAULTS, doc)
    return Config(
        config=merged.get("config", {}),
        protected=list(merged.get("protected", [])),
        curves=merged.get("curves", {}),
        assignments=merged.get("assignments", {}),
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
    for key, lo, hi in (("poll_seconds", 0.001, None),
                        ("max_rate", 0, None),
                        ("floor_duty", 0, 100),
                        ("dead_temp_c", None, None)):
        v = c.get(key)
        if not isinstance(v, (int, float)) or isinstance(v, bool):
            errors.append(f"config.{key} must be a number, got {v!r}")
        elif lo is not None and v < lo:
            errors.append(f"config.{key} must be >= {lo}")
        elif hi is not None and v > hi:
            errors.append(f"config.{key} must be <= {hi}")

    for p in cfg.protected:
        if not isinstance(p, str) or not FAN_ID_RE.match(p):
            errors.append(f"protected entry {p!r} is not a 'chip:pwmN' id")

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

    seen_fans: dict[str, str] = {}
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
                elif f in seen_fans:
                    errors.append(
                        f"fan {f} is assigned to both '{seen_fans[f]}' and '{aid}'")
                else:
                    seen_fans[f] = aid
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
        protected=list(DEFAULTS["protected"]),
        curves=copy.deepcopy(DEFAULTS["curves"]),
        assignments={},
    )
    save(cfg, path)
    return cfg
