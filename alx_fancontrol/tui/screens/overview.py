"""Overview: two live DataTables (sources, fans) + daemon status line.

Live-updating is the core differentiator vs fancontrol: temps, duties and
RPMs refresh every second, and the daemon state (running/stale/absent,
last error) comes from status.json.
"""
from __future__ import annotations

import time
from collections.abc import Iterator

from textual.widgets import DataTable, Static

from alx_fancontrol.tui.reader import LiveUpdate
from alx_fancontrol.tui.screens.base import (BaseScreen, restore_table_cursor,
                                             table_cursor_key, table_hover_key)


class OverviewScreen(BaseScreen):
    NAV_ID = "nav-1"

    def content(self) -> Iterator:
        yield Static("Sources", classes="title")
        yield DataTable(id="src-table")
        yield Static("Fans", classes="title")
        yield DataTable(id="fan-table")
        yield Static("daemon: —", id="status-line", classes="hint")

    def on_mount(self):
        src = self.query_one("#src-table", DataTable)
        src.add_columns(("id", "sid"), ("label", "slabel"),
                        ("kind", "skind"), ("detail", "sdetail"),
                        ("temp", "stemp"), ("state", "sstate"))
        fan = self.query_one("#fan-table", DataTable)
        fan.add_columns(("fan", "fid"), ("label", "flabel"),
                        ("assignment", "fassign"), ("source", "fsrc"),
                        ("duty %", "fduty"), ("rpm", "frpm"),
                        ("state", "fstate"))
        self._refresh()

    def on_live_update(self, msg: LiveUpdate):
        self._refresh()

    # ------------------------------------------------------------ render
    def _refresh(self):
        app = self.app
        if app.cfg is None:
            return
        data = app.live
        st = (data.get("status") or {})
        self._refresh_sources(data)
        self._refresh_fans(data)
        self._refresh_status_line(st)

    def _refresh_sources(self, data):
        app = self.app
        src = self.query_one("#src-table", DataTable)
        prev_key = table_cursor_key(src)   # keep the user's row + hover
        prev_hover = table_hover_key(src)  # across the per-second rebuild
        src.clear()
        for s in sorted(app.sources, key=lambda x: x.id):
            d = data.get("sources", {}).get(s.id, {})
            t = d.get("temp_c")
            if s.kind == "gpu":
                detail = s.detail
                if d.get("gpu_name"):
                    detail += f"  ({d['gpu_name']})"
            else:
                detail = s.detail
            tstr = "no reading" if t is None else f"{t:.1f} °C"
            state = "stale" if t is None else "ok"
            src.add_row(s.id, s.label, s.kind, detail, tstr, state, key=s.id)
        restore_table_cursor(src, prev_key, prev_hover)
        for s in sorted(app.sources, key=lambda x: x.id):
            d = data.get("sources", {}).get(s.id, {})
            t = d.get("temp_c")
            if s.kind == "gpu":
                detail = s.detail
                if d.get("gpu_name"):
                    detail += f"  ({d['gpu_name']})"
            else:
                detail = s.detail
            tstr = "no reading" if t is None else f"{t:.1f} °C"
            state = "stale" if t is None else "ok"

    def _refresh_fans(self, data):
        app = self.app
        fan = self.query_one("#fan-table", DataTable)
        prev_key = table_cursor_key(fan)
        prev_hover = table_hover_key(fan)
        fan.clear()
        assign_of: dict[str, str] = {}
        for aid, a in app.cfg.assignments.items():
            if a.get("enabled", True):
                for f in a.get("fans", []):
                    assign_of.setdefault(f, aid)
        prot = set(app.cfg.protected)
        for f in sorted(set(assign_of) | prot):
            d = data.get("fans", {}).get(f, {})
            aid = assign_of.get(f)
            if aid is not None:
                a = app.cfg.assignments[aid]
                sid = a.get("source")
                t = (data.get("sources") or {}).get(sid, {}).get("temp_c")
                src_cell = (f"{sid}: {t:.1f} °C" if t is not None
                            else f"{sid}: no reading")
                assign_cell = aid
            else:
                src_cell = "—"
                assign_cell = "reserved"
            duty = d.get("duty_pct")
            rpm = d.get("rpm")
            fan.add_row(
                f, d.get("label", "—"), assign_cell, src_cell,
                "—" if duty is None else f"{duty:.0f}",
                "—" if rpm is None else str(rpm),
                self._fan_state(f, aid, data),
                key=f,
            )
        restore_table_cursor(fan, prev_key, prev_hover)

    def _fan_state(self, fan: str, aid: str | None, data: dict) -> str:
        app = self.app
        if fan in set(app.cfg.protected):
            return "protected (gpu-fanctl)"
        if aid is None:
            return "idle"
        st = (data.get("status") or {}).get("fans", {}).get(fan, {})
        state = st.get("state")
        if state:
            return state
        d = data.get("fans", {}).get(fan, {})
        en = d.get("enable")
        if en == 2:
            return "firmware (not taken over)"
        if en == 1:
            return "manual"
        return "—"

    def _refresh_status_line(self, st: dict):
        if not st:
            line = ("daemon: absent (no status.json — is it running? "
                    "start it with: alx-fancontrol daemon)")
        else:
            age = time.time() - st.get("ts", 0)
            state = "running" if age <= 3 else "stale"
            mode = "DRY-RUN" if st.get("dry_run") else "live"
            extra = ""
            if st.get("errors"):
                extra = "   ⚠ " + " | ".join(str(v) for v
                                              in st["errors"].values())
            mtime = st.get("config_mtime")
            mstr = (time.strftime("%H:%M:%S", time.localtime(mtime))
                    if mtime else "—")
            line = (f"daemon: {state} (pid {st.get('pid')}, {age:.1f}s ago, "
                    f"{mode})   config mtime {mstr}{extra}")
        self.query_one("#status-line", Static).update(line)
