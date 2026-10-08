#!/usr/bin/env python3
"""Guardrail snapshot/check for the gpu-fanctl-owned channels.

Usage:
  python3 scripts/protect.py snapshot FILE
  python3 scripts/protect.py check FILE

Records it8792 pwm1/pwm3 duty+enable and the gpu-fanctl service state.

`check` prints GUARDRAIL-OK (exit 0) when:
  - it8792 pwm1/pwm3 ENABLE values are unchanged (i.e. alx_fancontrol
    never took them over — the hard guarantee), and
  - gpu-fanctl is still active.
Duty drift between snapshot and check is reported as a NOTE, not a
failure: gpu-fanctl is a live service and legitimately moves duty with
GPU temperature between two checks. An enable change or an inactive
service is a GUARDRAIL-VIOLATION (exit 1).

Read-only: never writes to hwmon, never touches the service.
"""
import glob
import json
import os
import subprocess
import sys
import time

CHANNELS = ["pwm1", "pwm3"]


def find_it8792():
    for d in sorted(glob.glob("/sys/class/hwmon/hwmon*")):
        try:
            with open(os.path.join(d, "name")) as f:
                if f.read().strip().startswith("it8792"):
                    return d
        except OSError:
            pass
    return None


def read_channel_state():
    base = find_it8792()
    if base is None:
        return {"it8792_dir": None, "channels": {}}
    ch = {}
    for p in CHANNELS:
        def _read(name):
            try:
                with open(os.path.join(base, name)) as f:
                    return f.read().strip()
            except OSError:
                return None
        ch[p] = {"duty": _read(p), "enable": _read(p + "_enable")}
    return {"it8792_dir": base, "channels": ch}


def gpu_fanctl_active():
    try:
        out = subprocess.run(["systemctl", "is-active", "gpu-fanctl"],
                             capture_output=True, text=True)
        return out.stdout.strip()
    except Exception:
        return "unknown"


def cmd_snapshot(path):
    doc = {"ts": time.time(), "state": read_channel_state(),
           "gpu_fanctl": gpu_fanctl_active()}
    with open(path, "w") as f:
        json.dump(doc, f, indent=2)
    print(f"snapshot written to {path}")
    print(f"  it8792: {doc['state']['it8792_dir']}")
    for p, v in doc["state"]["channels"].items():
        print(f"  {p}: duty={v['duty']} enable={v['enable']}")
    print(f"  gpu-fanctl: {doc['gpu_fanctl']}")
    return 0


def cmd_check(path):
    with open(path) as f:
        snap = json.load(f)
    now = read_channel_state()
    print("  it8792: %s" % now["it8792_dir"])
    ok = True
    for p in CHANNELS:
        s, n = snap["state"]["channels"].get(p, {}), \
            now["channels"].get(p, {})
        duty_note = ""
        if s.get("duty") != n.get("duty"):
            duty_note = (f" NOTE: duty moved {s.get('duty')} -> "
                         f"{n.get('duty')} (gpu-fanctl is live and owns duty)")
        en_note = ""
        if s.get("enable") != n.get("enable"):
            en_note = "  <-- ENABLE CHANGED (we must never do this!)"
            ok = False
        print(f"  {p}: duty {s.get('duty')} -> {n.get('duty')}, "
              f"enable {s.get('enable')} -> {n.get('enable')}"
              f"{duty_note}{en_note}")
    svc = gpu_fanctl_active()
    if svc != "active":
        ok = False
        print(f"  gpu-fanctl: {snap['gpu_fanctl']} -> {svc}  <-- NOT ACTIVE")
    else:
        print(f"  gpu-fanctl: {snap['gpu_fanctl']} -> {svc}")
    if ok:
        print("GUARDRAIL-OK")
        return 0
    print("GUARDRAIL-VIOLATION")
    return 1


def main(argv):
    if len(argv) != 3 or argv[1] not in ("snapshot", "check"):
        print(__doc__)
        return 2
    return {"snapshot": cmd_snapshot, "check": cmd_check}[argv[1]](argv[2])


if __name__ == "__main__":
    sys.exit(main(sys.argv))
