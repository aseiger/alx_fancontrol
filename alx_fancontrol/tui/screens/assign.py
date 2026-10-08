"""Assign: existing-assignment list + one form.

Top: a list of the assignments in the config — click a row to load it
into the form (edit/overwrite by name), Delete button or `d` to remove
one (with confirmation; the daemon then restores firmware control of
its fans).

Form: source Select (live °C in the labels), curve Select, fan picker
as a click-to-toggle DataTable (protected fans are visible but
reserved), optional floor/max, an assignment name, and Save →
app.save_config(). Saving with an existing name overwrites it (the
existing `enabled` state is preserved).
"""
from __future__ import annotations

from collections.abc import Iterator

from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Input, Label, Select, Static

from textual.widgets._select import SelectOverlay

from alx_fancontrol import hwmon
from alx_fancontrol.tui.reader import LiveUpdate
from alx_fancontrol.tui.screens.base import (BaseScreen, restore_table_cursor,
                                             sel_value, table_cursor_key,
                                             table_hover_key)


class ConfirmDeleteScreen(ModalScreen[bool]):
    """Delete-assignment confirmation — destructive: the daemon restores
    firmware (BIOS) control of the fans once the assignment is gone."""

    DEFAULT_CSS = """
    ConfirmDeleteScreen {
        align: center middle;
    }
    ConfirmDeleteScreen #confirm-box {
        width: 62;
        height: auto;
        border: tall $error;
        padding: 1 2;
    }
    ConfirmDeleteScreen #confirm-box Button {
        margin-top: 1;
        margin-right: 1;
        width: 16;
    }
    """

    def __init__(self, assignment_name: str):
        super().__init__()
        # 'name' collides with the read-only Widget.name property
        self.assignment_name = assignment_name

    def compose(self):
        with Vertical(id="confirm-box"):
            yield Label(f"Delete assignment '{self.assignment_name}'?")
            yield Label("The daemon stops driving its fans and restores")
            yield Label("firmware (BIOS) control of them.")
            yield Button("Cancel", id="confirm-cancel")
            yield Button("Delete", id="confirm-del", variant="error")

    def on_button_pressed(self, event):
        if event.button.id == "confirm-cancel":
            self.dismiss(False)
        elif event.button.id == "confirm-del":
            self.dismiss(True)


class AssignScreen(BaseScreen):
    NAV_ID = "nav-2"
    BINDINGS = [
        Binding("s", "save_assignment", "Save assignment"),
        Binding("d", "delete_assignment", "Delete assignment"),
    ]
    CSS = """
    /* The container defaults are Horizontal/Vertical { height: 1fr } —
       left alone they would make every box on this screen grab the
       available space and squash the Selects/floor-max row into
       clipped one-row strips (an empty box, no way to see or pick a
       source). Natural heights: the form stacks top-to-bottom and
       #content scrolls on short terminals. */
    #list-actions {
        height: auto;
        margin-bottom: 1;
    }
    #list-actions Button {
        margin-right: 1;
    }
    #form-title {
        margin-top: 2;
    }
    #assign-form {
        height: auto;
    }
    #assign-form Horizontal {
        height: auto;
    }
    /* Inputs default to width: 100% — side-by-side they'd push the
       second one off-screen; share the row instead. */
    #floor, #maxduty, #rate {
        width: 1fr;
    }
    #floor, #maxduty {
        margin-right: 1;
    }
    """

    def content(self) -> Iterator:
        yield Static("Assignments — click a row to edit it",
                     classes="title")
        with Horizontal(id="list-actions"):
            yield Button("New", id="new")
            yield Button("Delete", id="delete", variant="error")
        yield DataTable(id="assign-list")
        yield Static("New assignment", id="form-title", classes="title")
        with Vertical(id="assign-form"):
            yield Input(placeholder="assignment name (e.g. cpu0-cooling)",
                        id="name")
            with Horizontal():
                yield Select([], id="source", prompt="source")
                yield Select([], id="curve", prompt="curve")
            yield Static("fans — click a row to toggle (✓); protected "
                         "channels are reserved:", classes="hint")
            yield DataTable(id="fan-pick")
            with Horizontal():
                yield Input(placeholder="floor % (default 5)",
                            id="floor", restrict=r"[0-9]", max_length=3)
                yield Input(placeholder="ceiling % (default 100)",
                            id="maxduty", restrict=r"[0-9]", max_length=3)
                yield Input(placeholder="rate %/poll (default 6)",
                            id="rate", restrict=r"[0-9]", max_length=3)
            yield Button("Save assignment", id="save", variant="primary")

    def on_mount(self):
        self._picked: set[str] = set()
        self._editing: str | None = None
        self._populate_source(self.app.live)
        self._populate_curves()
        lst = self.query_one("#assign-list", DataTable)
        lst.add_columns(("name", "an"), ("source", "asrc"),
                        ("curve", "acrv"), ("fans", "afan"),
                        ("floor", "afl"), ("max", "amx"),
                        ("rate", "arat"), ("on", "aon"))
        lst.cursor_type = "row"
        self._refresh_list()
        table = self.query_one("#fan-pick", DataTable)
        table.add_columns(("✓", "sel"), ("fan", "fid"),
                          ("label", "flabel"), ("rpm", "frpm"),
                          ("note", "fnote"))
        table.cursor_type = "row"  # row events need this
        self._refresh_fans()

    # ---------------------------------------------------------- selects --
    def _source_options(self, data: dict) -> list[tuple[str, str]]:
        """Source options with live °C from the RUNTIME discovery
        (sources are not in the config)."""
        app = self.app
        opts = []
        for s in sorted(app.sources, key=lambda x: x.id):
            d = data.get("sources", {}).get(s.id, {})
            t = d.get("temp_c")
            tstr = f"{t:.1f} °C" if t is not None else "no reading"
            name = d.get("gpu_name")
            lab = s.label + (f" ({name})" if name else "")
            opts.append((f"{lab} — {tstr}", s.id))
        return opts

    def _populate_source(self, data: dict):
        """Refresh the source picker. When the dropdown is CLOSED this
        runs every live tick (labels carry the °C). While it is OPEN we
        must NOT call set_options() on temp-only changes: set_options
        (and re-asserting the value) resets the overlay's highlight to
        the selected item, which yanks the user's scroll position back
        every second while they browse the list."""
        app = self.app
        src = self.query_one("#source", Select)
        current = sel_value(src)
        opts = self._source_options(data)
        if not opts:
            return
        values = [v for _l, v in opts]

        if src.expanded:
            if getattr(self, "_src_opt_values", None) == values:
                return  # temps only changed — leave the open list alone
            # The option SET changed (a source appeared/disappeared):
            # rebuild, but keep the user looking at the same option.
            overlay = src.query_one(SelectOverlay)
            hl = overlay.highlighted
            keep = opts[hl][1] if (hl is not None and hl < len(opts)) else current
            src.set_options(opts)
            self._src_opt_values = values
            src.value = current if current in values else opts[0][1]
            if keep in values:
                for i, (_l, v) in enumerate(opts):
                    if v == keep:
                        overlay.select(i)  # highlight + scroll, no commit
                        break
            return

        src.set_options(opts)
        self._src_opt_values = values
        src.value = current if current in values else opts[0][1]

    def _populate_curves(self):
        app = self.app
        crv = self.query_one("#curve", Select)
        opts = [(f"{c.get('label', cid)} ({len(c.get('points', []))} pts)", cid)
                for cid, c in sorted(app.cfg.curves.items())]
        crv.set_options(opts)
        if opts:
            crv.value = opts[0][1]

    def on_select_changed(self, event: Select.Changed):
        if event.select.id == "source":
            pass  # labels carry live data; nothing to do on selection

    # ------------------------------------------------------ assignment list
    def _refresh_list(self):
        app = self.app
        table = self.query_one("#assign-list", DataTable)
        prev_key = table_cursor_key(table)
        prev_hover = table_hover_key(table)
        table.clear()
        slabs = {s.id: s.label for s in app.sources}
        for name in sorted(app.cfg.assignments):
            a = app.cfg.assignments[name]
            src = a.get("source", "")
            cur = a.get("curve", "")
            table.add_row(
                name,
                slabs.get(src, f"{src} (not detected)") if src else "—",
                app.cfg.curves.get(cur, {}).get("label", cur) if cur else "—",
                ", ".join(a.get("fans", [])),
                str(a["floor_duty"]) if a.get("floor_duty") is not None
                else "—",
                str(a["max_duty"]) if a.get("max_duty") is not None
                else "—",
                str(a["max_rate"]) if a.get("max_rate") is not None
                else "—",
                "on" if a.get("enabled", True) else "off",
                key=name,
            )
        restore_table_cursor(table, prev_key, prev_hover)

    def _load_assignment(self, name: str):
        """Click-to-edit: load the named assignment into the form.
        Saving with the same name overwrites it (enabled is preserved)."""
        app = self.app
        a = app.cfg.assignments.get(name)
        if a is None:
            self.notify(f"Assignment '{name}' no longer exists",
                        severity="warning")
            self._refresh_list()
            return
        self._editing = name
        self.query_one("#name", Input).value = name
        src_id = a.get("source")
        if src_id in {s.id for s in app.sources}:
            self.query_one("#source", Select).value = src_id
        else:
            self.notify(f"source '{src_id}' not detected — pick another",
                        severity="warning")
        crv_id = a.get("curve")
        if crv_id in app.cfg.curves:
            self.query_one("#curve", Select).value = crv_id
        else:
            self.notify(f"curve '{crv_id}' missing — pick another",
                        severity="warning")
        self._picked = set(a.get("fans", []))
        self.query_one("#floor", Input).value = (
            str(a["floor_duty"]) if a.get("floor_duty") is not None else "")
        self.query_one("#maxduty", Input).value = (
            str(a["max_duty"]) if a.get("max_duty") is not None else "")
        self.query_one("#rate", Input).value = (
            str(a["max_rate"]) if a.get("max_rate") is not None else "")
        self._refresh_fans()
        self.query_one("#form-title", Static).update(
            f"Editing '{name}' — saving overwrites it")

    def _reset_form(self):
        """Back to 'new assignment': blank form, nothing picked."""
        self._editing = None
        self._picked.clear()
        self.query_one("#name", Input).value = ""
        self.query_one("#floor", Input).value = ""
        self.query_one("#maxduty", Input).value = ""
        self.query_one("#rate", Input).value = ""
        self._refresh_fans()
        self.query_one("#form-title", Static).update("New assignment")

    def _selected_list_name(self) -> str | None:
        table = self.query_one("#assign-list", DataTable)
        if table.cursor_row is None or table.cursor_row >= table.row_count:
            return None
        return table.coordinate_to_cell_key((table.cursor_row, 0))[0]

    def _start_delete(self, name: str | None):
        if name is None or name not in self.app.cfg.assignments:
            self.notify("No assignment selected — click or arrow to a row "
                        "first", severity="warning")
            return
        # push_screen_wait is worker-only in textual 8 → run in a worker
        self.run_worker(self._delete_flow(name), exclusive=False)

    async def _delete_flow(self, name: str):
        ok = await self.app.push_screen_wait(ConfirmDeleteScreen(name))
        if not ok:
            return
        app = self.app
        del app.cfg.assignments[name]
        if self._editing == name:
            self._reset_form()
        if app.save_config(f"Assignment '{name}' deleted — the daemon "
                           "restores firmware control of its fans"):
            self._refresh_list()

    def action_delete_assignment(self):
        # 'd' binding (mirrors Curves' d=delete)
        self._start_delete(self._selected_list_name())

    # --------------------------------------------------------------- fans
    def _refresh_fans(self):
        app = self.app
        table = self.query_one("#fan-pick", DataTable)
        prev_key = table_cursor_key(table)
        prev_hover = table_hover_key(table)
        table.clear()
        prot = set(app.cfg.protected)
        data = app.live
        for fan, _d, _pwm in hwmon.list_pwm_channels(app.chips):
            fd = data.get("fans", {}).get(fan, {})
            rpm = fd.get("rpm")
            table.add_row(
                "✓" if fan in self._picked else "",
                fan,
                app.fan_label(fan),
                "—" if rpm is None else str(rpm),
                "reserved (gpu-fanctl)" if fan in prot else "",
                key=fan,
            )
        restore_table_cursor(table, prev_key, prev_hover)

    def on_data_table_row_selected(self, event: DataTable.RowSelected):
        # textual >= 1 API: RowSelected carries .data_table and .row_key.
        # There is no .table attribute — tests/test_tui_ux.py guards this.
        table = event.data_table
        key = event.row_key
        # row_key is a RowKey wrapper: it compares equal to str, but
        # re.match/json.dump choke on it — unwrap to the plain str.
        key = getattr(key, "value", key)
        if key is None:
            key = table.get_cell_at((event.cursor_row, 0))
        if key is None:
            return
        key = str(key)
        if table.id == "assign-list":
            self._load_assignment(key)
            return
        # fan picker: toggle ✓
        fan = key
        if fan in set(self.app.cfg.protected):
            self.notify("reserved (gpu-fanctl) — cannot assign",
                        severity="warning")
            return
        if fan in self._picked:
            self._picked.discard(fan)
            table.update_cell(fan, "sel", "")
        else:
            self._picked.add(fan)
            table.update_cell(fan, "sel", "✓")

    def on_live_update(self, msg: LiveUpdate):
        data = msg.data
        # live RPMs in place
        table = self.query_one("#fan-pick", DataTable)
        for row in range(table.row_count):
            fan = table.get_cell_at((row, 1))
            if fan is None:
                continue
            rpm = data.get("fans", {}).get(fan, {}).get("rpm")
            table.update_cell(fan, "frpm", "—" if rpm is None else str(rpm))
        # live temps in the source picker labels
        self._populate_source(data)

    # --------------------------------------------------------------- save
    def on_button_pressed(self, event):
        # NB: textual 8 dispatches on_* handlers along the WHOLE MRO,
        # so BaseScreen.on_button_pressed (sidebar nav) also runs for
        # this message — no super() call here (that would double-fire
        # nav actions and double-switch screens).
        if event.button.id == "save":
            self.save_assignment()
        elif event.button.id == "new":
            self._reset_form()
        elif event.button.id == "delete":
            self._start_delete(self._selected_list_name())

    def action_save_assignment(self):
        # 's' binding — the Save button sits at the bottom of a long form
        # and can fall below the fold on short terminals.
        self.save_assignment()

    def save_assignment(self):
        app = self.app
        name = self.query_one("#name", Input).value.strip()
        if not name:
            self.notify("Enter an assignment name", severity="warning")
            return
        src_id = sel_value(self.query_one("#source", Select))
        curve_id = sel_value(self.query_one("#curve", Select))
        if not src_id:
            self.notify("Pick a source", severity="warning")
            return
        if not curve_id:
            self.notify("Pick a curve", severity="warning")
            return
        if not self._picked:
            self.notify("Pick at least one fan", severity="warning")
            return
        reserved = self._picked & set(app.cfg.protected)
        if reserved:
            self.notify(f"reserved (gpu-fanctl): "
                        f"{', '.join(sorted(reserved))}", severity="warning")
            return
        floor_txt = self.query_one("#floor", Input).value.strip()
        max_txt = self.query_one("#maxduty", Input).value.strip()
        rate_txt = self.query_one("#rate", Input).value.strip()
        floor = int(floor_txt) if floor_txt else None
        maxd = int(max_txt) if max_txt else None
        rate = int(rate_txt) if rate_txt else None
        if floor is not None and maxd is not None and floor > maxd:
            self.notify("floor % must be <= ceiling %", severity="warning")
            return
        fans = sorted(self._picked)
        # overwriting an existing assignment keeps its enabled state
        existing = app.cfg.assignments.get(name, {})
        assignment: dict = {
            "source": src_id,
            "curve": curve_id,
            "fans": fans,
            "enabled": existing.get("enabled", True),
        }
        if floor is not None:
            assignment["floor_duty"] = floor
        if maxd is not None:
            assignment["max_duty"] = maxd
        if rate is not None:
            assignment["max_rate"] = rate
        tach = {}
        for f in fans:
            guess = app.guess_tach(f)
            if guess:
                tach[f] = guess
        if tach:
            assignment["tach"] = tach
        app.cfg.assignments[name] = assignment
        if app.save_config(f"Assignment '{name}' saved — daemon picks "
                                 f"it up within ~1 s"):
            self._reset_form()
            self._refresh_list()
