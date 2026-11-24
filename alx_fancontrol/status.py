"""status.json writer/reader: daemon liveness + last poll data.

The daemon writes status.json atomically into the config directory every
poll; the TUI reads it to render daemon state (running / stale / absent).

This module also owns the *ownership* question — which pwm channels an
alx_fancontrol run has claimed (`owned.json`) and whether that claim is still
live. It lives here rather than in `daemon.py` because the GUI has to answer
the same question to tell the truth on screen: "this header is held by another
process, so the daemon will refuse it unless the assignment says
force_takeover" is only useful if it agrees with what `_takeover()` will
actually do. Two implementations of that rule drift; one cannot.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

DEFAULT_STALE_AFTER = 3.0


SYSTEM_STATUS_DIR = Path("/run/alx_fancontrol")
SYSTEM_STATE_DIR = Path("/var/lib/alx_fancontrol")


def status_path_for(cfg_path) -> Path:
    """Root (system service): /run/alx_fancontrol/status.json — created by
    the unit's RuntimeDirectory= (a manual sudo run mkdirs it on write).
    Non-root (dev): next to the config file."""
    if os.geteuid() == 0:
        return SYSTEM_STATUS_DIR / "status.json"
    return Path(cfg_path).parent / "status.json"


def owned_path_for(cfg_path) -> Path:
    """The ownership marker: which pwm channels THIS daemon took over, and
    the enable value to restore. It must survive a crash and a restart,
    so it lives in the state dir (persistent), not in /run — that is what
    lets a restarted daemon reclaim a channel its predecessor was killed
    while holding in manual mode (instead of refusing it forever).
    Root: /var/lib/alx_fancontrol/owned.json (unit's StateDirectory=).
    Non-root (dev): next to the config file."""
    if os.geteuid() == 0:
        return SYSTEM_STATE_DIR / "owned.json"
    return Path(cfg_path).parent / "owned.json"


def write_status(path, data: dict) -> None:
    """Atomic replace so the TUI never reads a torn file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = str(path) + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def read_status(path) -> dict | None:
    try:
        with open(path) as f:
            doc = json.load(f)
        return doc if isinstance(doc, dict) else None
    except (OSError, ValueError):
        return None


def boot_id() -> str:
    """The current boot's id. An ownership marker from an earlier boot means
    nothing: whatever held that channel is gone, and the board has been
    re-initialised since."""
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        return "unknown"


def pid_alive(pid: int) -> bool:
    """Is a live alx_fancontrol process running under this pid?

    Pid reuse is real, so liveness alone is not enough — a recycled pid that
    now belongs to, say, `nginx` must not make us treat another daemon's
    channels as ours (PermissionError does count as alive: a process we cannot
    signal is definitely there)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    try:
        cmd = Path(f"/proc/{pid}/cmdline").read_bytes().decode("utf-8",
                                                               "ignore")
    except OSError:
        return False
    return "alx_fancontrol" in cmd


def claimed_channels(path, now_boot_id: str | None = None) -> dict:
    """What the ownership marker says, reduced to what a reader needs:

        {"channels": {fan: orig_enable},   # claimed by an alx_fancontrol run
         "live_pid": int | None,           # a daemon still holding them
         "stale": [fan, ...]}              # claimed this boot, holder is gone

    `stale` is what the daemon reclaims instead of refusing: manual mode left
    behind by a run of ours that was killed without restoring it. Anything
    from another boot is reported as no claim at all.

    Read-only and forgiving: an unreadable or malformed marker is simply "no
    claim", which is the safe reading for both callers (the daemon then refuses
    a manual-mode channel rather than assuming it is ours; the GUI says so).
    """
    doc = read_status(path)
    if not isinstance(doc, dict):
        return {"channels": {}, "live_pid": None, "stale": []}
    if doc.get("boot_id") != (now_boot_id or boot_id()):
        return {"channels": {}, "live_pid": None, "stale": []}
    chans = doc.get("channels")
    if not isinstance(chans, dict):
        return {"channels": {}, "live_pid": None, "stale": []}
    channels = {f: orig for f, orig in chans.items()
                if isinstance(f, str) and isinstance(orig, int)
                and not isinstance(orig, bool)}
    pid = doc.get("pid")
    live = pid if isinstance(pid, int) and pid != os.getpid() \
        and pid_alive(pid) else None
    return {"channels": channels,
            "live_pid": live,
            "stale": [] if live else sorted(channels)}


def daemon_state(status: dict | None, now: float | None = None,
                 stale_after: float = DEFAULT_STALE_AFTER) -> str:
    """'running' | 'stale' | 'absent' based on the status file's ts."""
    if not status:
        return "absent"
    ts = status.get("ts")
    if not isinstance(ts, (int, float)):
        return "absent"
    now = time.time() if now is None else now
    return "running" if (now - ts) <= stale_after else "stale"
