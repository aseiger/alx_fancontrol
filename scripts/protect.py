#!/usr/bin/env python3
"""Guardrail snapshot/check for the channels alx_fancontrol must own.

Usage:
  python3 scripts/protect.py snapshot FILE
  python3 scripts/protect.py check FILE

History: this script used to guard the *other* controller — gpu-fanctl's
it8792 pwm1/pwm3 — while the two coexisted. gpu-fanctl has been removed
(`scripts/remove_gpu_fanctl.sh`), so the guarantee that matters now is the
mirror image: **the daemon must hold every channel its config assigns.**
A channel that fell out of manual mode means the fail-safe can no longer
raise that fan, which is exactly how a GPU blower ended up parked at 0%.

The guarded set is the union of the ownership marker
(/var/lib/alx_fancontrol/owned.json) and the enabled assignments in the
config, so a channel that should be owned but never was (takeover refused)
is caught too, not just one that was lost.

`check` prints GUARDRAIL-OK (exit 0) when:
  - every guarded channel is still `pwmN_enable=1` (held by the daemon), and
  - alx_fancontrol.service is active.
Anything else is a GUARDRAIL-VIOLATION (exit 1). Duty drift between the two
runs is a NOTE, not a failure — the daemon moves duty with temperature.
The daemon's own `status.json` is echoed (per-fan state, errors) so a
`failsafe` or `stalled` fan is visible in the same output; those are
reported as WARN and do not by themselves fail the check, because they
describe the source/fan, not ownership.

Read-only: never writes to hwmon, never touches the service (it only reads
`systemctl is-active`).
"""
import glob
import json
import os
import subprocess
import sys
import time

SVC = "alx_fancontrol"
MARKER = f"/var/lib/{SVC}/owned.json"
STATUS = f"/run/{SVC}/status.json"


def config_path():
    env = os.environ.get("ALX_FANCONTROL_CONFIG")
    if env:
        return os.path.expanduser(env)
    if os.geteuid() == 0 or os.path.exists("/etc/alx_fancontrol/config.json"):
        return "/etc/alx_fancontrol/config.json"
    xdg = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(xdg, "alx_fancontrol", "config.json")


def chip_dirs():
    """{chip name prefix: dir} for every hwmon chip (first match per prefix,
    the same rule the daemon's resolve_fan uses)."""
    out = {}
    for d in sorted(glob.glob("/sys/class/hwmon/hwmon*")):
        try:
            with open(os.path.join(d, "name")) as f:
                raw = f.read().strip()
        except OSError:
            continue
        prefix = raw.split("_", 1)[0].split("-", 1)[0]
        out.setdefault(prefix, d)
    return out


def assigned_channels():
    """chip:pwmN from every ENABLED assignment in the config."""
    try:
        with open(config_path()) as f:
            doc = json.load(f)
    except (OSError, ValueError):
        return set()
    out = set()
    for a in (doc.get("assignments") or {}).values():
        if isinstance(a, dict) and a.get("enabled", True):
            for fan in a.get("fans") or []:
                if isinstance(fan, str):
                    out.add(fan)
    return out


def marker_channels():
    try:
        with open(MARKER) as f:
            doc = json.load(f)
    except (OSError, ValueError):
        return set()
    return set((doc.get("channels") or {}).keys())


def read_channel(chips, fan_id):
    """{'duty':…, 'enable':…, 'rpm':…} for 'chip:pwmN'; None values if the
    channel cannot be read (chip gone, file missing)."""
    chip, _, pwm = fan_id.partition(":")
    d = chips.get(chip)
    if d is None or not pwm.startswith("pwm"):
        return {"duty": None, "enable": None, "rpm": None}
    idx = pwm[3:]

    def _read(name):
        try:
            with open(os.path.join(d, name)) as f:
                return f.read().strip()
        except OSError:
            return None
    return {"duty": _read(pwm), "enable": _read(f"{pwm}_enable"),
            "rpm": _read(f"fan{idx}_input")}


def service_state():
    try:
        return subprocess.run(["systemctl", "is-active", SVC],
                              capture_output=True, text=True).stdout.strip()
    except Exception:
        return "unknown"


def daemon_status():
    """The bits of status.json worth comparing/guarding."""
    try:
        with open(STATUS) as f:
            st = json.load(f)
    except (OSError, ValueError):
        return {}
    return {
        "pid": st.get("pid"),
        "degraded": st.get("degraded"),
        "errors": st.get("errors") or {},
        "states": {f: v.get("state") for f, v in (st.get("fans") or {}).items()},
    }


def read_state():
    chips = chip_dirs()
    guarded = sorted(assigned_channels() | marker_channels())
    return {"guarded": {f: read_channel(chips, f) for f in guarded},
            "service": service_state(),
            "daemon": daemon_status()}


def cmd_snapshot(path):
    doc = {"ts": time.time(), "state": read_state()}
    with open(path, "w") as f:
        json.dump(doc, f, indent=2)
    st = doc["state"]
    print(f"snapshot written to {path}")
    print(f"  {SVC}: {st['service']} (pid {st['daemon'].get('pid')})")
    if not st["guarded"]:
        print("  WARNING: nothing guarded — no enabled assignments in the "
              f"config and no marker at {MARKER}")
    for fan, v in st["guarded"].items():
        print(f"  {fan:14s} duty={v['duty']} enable={v['enable']} "
              f"rpm={v['rpm']}")
    return 0


def cmd_check(path):
    with open(path) as f:
        snap = json.load(f)
    now = read_state()
    ok = True

    svc_before = snap["state"].get("service")
    if now["service"] != "active":
        ok = False
        print(f"  {SVC}: {svc_before} -> {now['service']}  <-- NOT ACTIVE, "
              f"nothing is controlling these fans")
    else:
        print(f"  {SVC}: {svc_before} -> active "
              f"(pid {now['daemon'].get('pid')})")

    before = snap["state"].get("guarded", {})
    for fan in sorted(set(before) | set(now["guarded"])):
        b = before.get(fan)
        n = now["guarded"].get(fan)
        if b is None:
            print(f"  {fan:14s} NEW since snapshot: enable={n['enable']} "
                  f"duty={n['duty']}")
            if n["enable"] != "1":
                ok = False
                print("       <-- not held by the daemon")
            continue
        if n is None:
            ok = False
            print(f"  {fan:14s} GONE since snapshot (chip missing?)")
            continue
        note = ""
        if b.get("duty") != n.get("duty"):
            note = f" NOTE: duty moved {b.get('duty')} -> {n['duty']} (normal)"
        if n["enable"] != "1":
            ok = False
            note += "  <-- NOT HELD (enable must be 1; the fail-safe cannot " \
                    "raise this fan now)"
        state = now["daemon"].get("states", {}).get(fan)
        state_note = ""
        if state in ("failsafe", "stalled", "manual-conflict", "disabled",
                     "write-error", "unresolved", "source-missing"):
            state_note = f"  [{state}]"
            if state in ("manual-conflict", "disabled", "write-error",
                         "unresolved"):
                ok = False
        print(f"  {fan:14s} duty {b.get('duty')} -> {n['duty']}, "
              f"enable {b.get('enable')} -> {n['enable']}{state_note}{note}")

    errs = now["daemon"].get("errors") or {}
    if errs:
        print("  daemon errors:")
        for k, v in sorted(errs.items()):
            print(f"    WARN {k}: {v}")

    print("GUARDRAIL-OK" if ok else "GUARDRAIL-VIOLATION")
    return 0 if ok else 1


def main(argv):
    if len(argv) != 3 or argv[1] not in ("snapshot", "check"):
        print(__doc__)
        return 2
    return {"snapshot": cmd_snapshot, "check": cmd_check}[argv[1]](argv[2])


if __name__ == "__main__":
    sys.exit(main(sys.argv))
