"""Sources: read-only live view of the temperature sources DETECTED at
runtime (hwmon chips + nvidia-smi) — sources are a machine property, not
config, so there is nothing to edit here. Pick any of them in the
Assign screen (2).
"""
from __future__ import annotations

from collections.abc import Iterator

from textual.containers import Vertical
from textual.widgets import DataTable, Static

from alx_fancontrol.tui.reader import LiveUpdate
from alx_fancontrol.tui.screens.base import (BaseScreen, restore_table_cursor,
                                             table_cursor_key, table_hover_key)


class SourcesScreen(BaseScreen):
    NAV_ID = "nav-4"

    def content(self) -> Iterator:
        with Vertical(id="sources-view"):
            yield Static("Detected temperature sources (runtime discovery — "
                         "not in the config).", classes="hint")
            yield Static("Assign any of them to fans in screen 2.",
                         classes="hint")
            yield DataTable(id="srcs")

    def on_mount(self):
        self._row_ids: list[str] = []
        table = self.query_one("#srcs", DataTable)
        table.add_columns(("id", "sid"), ("label", "slabel"),
                          ("kind", "skind"), ("detail", "sdetail"),
                          ("temp", "stemp"))
        self._refresh()
        if table.row_count:
            table.move_cursor(row=0)

    def on_live_update(self, msg: LiveUpdate):
        self._refresh(msg.data)

    def _refresh(self, data: dict | None = None):
        app = self.app
        table = self.query_one("#srcs", DataTable)
        prev_key = table_cursor_key(table)   # keep the user's row + hover
        prev_hover = table_hover_key(table)  # across the per-second rebuild
        table.clear()
        self._row_ids = []
        if data is None:
            data = app.live
        for s in sorted(app.sources, key=lambda x: x.id):
            self._row_ids.append(s.id)
            d = data.get("sources", {}).get(s.id, {})
            t = d.get("temp_c")
            name = d.get("gpu_name")
            label = s.label + (f" ({name})" if name else "")
            tstr = "—" if t is None else f"{t:.1f} °C"
            table.add_row(s.id, label, s.kind, s.detail, tstr, key=s.id)
        restore_table_cursor(table, prev_key, prev_hover)
