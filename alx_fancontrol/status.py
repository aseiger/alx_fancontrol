"""status.json writer/reader: daemon liveness + last poll data.

The daemon writes status.json atomically into the config directory every
poll; the TUI reads it to render daemon state (running / stale / absent).
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

DEFAULT_STALE_AFTER = 3.0


SYSTEM_STATUS_DIR = Path("/run/alx_fancontrol")


def status_path_for(cfg_path) -> Path:
    """Root (system service): /run/alx_fancontrol/status.json — created by
    the unit's RuntimeDirectory= (a manual sudo run mkdirs it on write).
    Non-root (dev): next to the config file."""
    if os.geteuid() == 0:
        return SYSTEM_STATUS_DIR / "status.json"
    return Path(cfg_path).parent / "status.json"


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
