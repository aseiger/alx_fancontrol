"""TUI-side live reads: read-only worker thread over hwmon/nvidia/status.

Reuses alx_fancontrol.hwmon (injectable, read-only here). NEVER writes to
hwmon — safe to run alongside the daemon. Posts LiveData messages to the
app via call_from_thread every ~1 s.
"""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path

from textual.message import Message

from alx_fancontrol import hwmon
from alx_fancontrol import status as status_mod


class LiveData(Message):
    """Latest live snapshot (app-level)."""

    def __init__(self, data: dict):
        self.data = data
        super().__init__()


class LiveUpdate(Message):
    """Forwarded by the app to the active screen."""

    def __init__(self, data: dict):
        self.data = data
        super().__init__()


class LiveReader:
    """Daemon thread: take a snapshot, post LiveData, sleep ~1 s."""

    def __init__(self, app):
        self.app = app
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="alx-live-reader")
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    def _run(self):
        while not self._stop.is_set():
            data = self.snapshot()
            try:
                self.app.call_from_thread(self.app.post_message, LiveData(data))
            except Exception:
                return  # app is shutting down
            self._stop.wait(1.0)

    def snapshot(self) -> dict:
        """One read-only sweep: source temps (+GPU names), live fan
        duty/enable/rpm, and the daemon's status.json."""
        app = self.app
        cfg = app.cfg
        chips = hwmon.find_all_chips()
        data: dict = {"ts": time.time(), "sources": {}, "fans": {},
                      "status": None}
        ginfo: dict = {}
        if any(s.kind == "gpu" for s in app.sources):
            try:
                ginfo = hwmon.gpu_info()
            except Exception:
                ginfo = {}
        for s in app.sources:
            if s.kind == "gpu":
                t = ginfo.get(s.pci_bus, {}).get("temp")
            else:
                d = hwmon.chip_dir_for(s.chip, s.chip_index, chips)
                t = hwmon.read_temp_c(d, s.temp) if d else None
            entry: dict = {"temp_c": t}
            name = ginfo.get(s.pci_bus, {}).get("name") if s.kind == "gpu" \
                else None
            if name:
                entry["gpu_name"] = name
            data["sources"][s.id] = entry

        # Report EVERY pwm channel, not just assigned ones — the Assign
        # screen needs live RPMs for fans that are not assigned yet, or
        # the picker's rpm column shows "—" and the user can't tell which
        # header a physical fan is on.
        fans: set[str] = set()
        for a in cfg.assignments.values():
            fans.update(a.get("fans", []))
        for fan, _d, _pwm in hwmon.list_pwm_channels(chips):
            fans.add(fan)
        for fan in sorted(fans):
            entry = {"label": app.fan_label(fan), "duty_pct": None,
                     "rpm": None, "enable": None}
            res = hwmon.resolve_fan(fan, chips)
            if res is not None:
                d, pwm = res
                raw = hwmon.read_duty_raw(d, pwm)
                entry["duty_pct"] = (round(raw * 100 / 255, 1)
                                     if raw is not None else None)
                entry["enable"] = hwmon.get_enable(d, pwm)
                # Explicit assignment tach wins; otherwise guess from the
                # matching sensors.d pwm/fan labels so unassigned fans still
                # show a live RPM in the Assign pane.
                tach = app.tach_for(fan) or app.guess_tach(fan)
                if tach:
                    tres = hwmon.resolve_tach(tach, chips)
                    if tres is not None:
                        entry["rpm"] = hwmon.read_rpm(*tres)
            data["fans"][fan] = entry
        # The daemon (root) writes status to /run/alx_fancontrol/; a
        # non-root TUI computes the next-to-config path — check both so
        # the Overview still shows the daemon as running.
        st = status_mod.read_status(status_mod.status_path_for(app.cfg_path))
        if st is None and os.geteuid() != 0:
            st = status_mod.read_status(
                Path("/run/alx_fancontrol/status.json"))
        data["status"] = st
        return data
