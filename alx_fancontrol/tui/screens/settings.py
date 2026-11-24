"""Settings screen: the global `config` block, seen and edited in the TUI.

Audit finding F1: 22 options that the daemon reads, that README documents,
and that the GUI offered no way to view or change — so tuning the poll rate,
the fail-safe ramp or the stall-kick timings meant hand-editing JSON (and
knowing which of five files, since root uses /etc and a dev shell does not).

Built from `config.CONFIG_FIELDS`, one description per option that drives
validation *and* this screen: the ranges, units and explanations here are the
ones `validate()` enforces, so the form cannot drift away from the rules that
decide whether a save is accepted. A knob added to DEFAULTS without a Field
fails a test (tests/test_config.py).

Rows are click/Enter-to-edit (a modal with the option's help text, its bounds
and its built-in default), because 22 inline inputs would not fit a terminal
—
and because the explanation is what makes a number like `stall_kick_backoff_s`
editable without reading daemon.py first.

Semantics that match the rest of the GUI:
  * blank value = remove the key = follow the built-in default (load() merges
    `config.*` per key, so this is exactly the file state "I never set it");
  * **edits stay on this screen until Save** — they are held in an overlay
    rather than written into `app.cfg` as they are made. Editing the shared
    config object directly would mean an unsaved poll interval gets written by
    the next Save on the Assign screen, which is how a GUI ends up saving
    things the user never asked it to save;
  * Save validates first (`app.save_config`) — an invalid number is refused
    with the reason instead of being written and silently ignored by the
    daemon's hot-reload, which keeps the previous config.
"""
from __future__ import annotations

from collections.abc import Iterator

from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Input, Label, Static, Switch

from alx_fancontrol import config as config_mod
from alx_fancontrol.tui.screens.base import (BaseScreen, ConfirmScreen,
                                             restore_table_cursor,
                                             table_cursor_key,
                                             table_hover_key)

# modal results: ("set", value) / ("clear", None); None = cancelled
CLEAR = "clear"


def fmt_value(v) -> str:
    """Display form: numbers without trailing .0 noise, bools as on/off,
    the alert command quoted so an empty one is visibly empty."""
    if isinstance(v, bool):
        return "on" if v else "off"
    if isinstance(v, float):
        return f"{v:g}"
    if isinstance(v, str):
        return v if v else "(empty)"
    return str(v)


def parse_value(field: config_mod.Field, text: str):
    """(value, error) for what was typed.

    Blank means "remove the key" for every kind except a string option, where
    blank IS the value (an empty alert command means no alert). The bounds are
    the Field's — the same ones validate() uses, so the modal cannot let
    through a number the save would then refuse."""
    text = text.strip()
    if field.kind == "str":
        return text, None
    if not text:
        return CLEAR, None
    unit = f" {field.unit}" if field.unit else ""
    if field.kind == "bool":
        low = text.lower()
        if low in ("on", "true", "1", "yes"):
            return True, None
        if low in ("off", "false", "0", "no"):
            return False, None
        return None, "expected on or off"
    try:
        v = float(text)
    except ValueError:
        return None, f"{text!r} is not a number"
    if field.lo is not None and v < field.lo:
        return None, f"must be >= {fmt_value(field.lo)}{unit}"
    if field.hi is not None and v > field.hi:
        return None, f"must be <= {fmt_value(field.hi)}{unit}"
    # keep whole numbers whole, so the file does not fill up with 3.0
    if v.is_integer() and isinstance(
            config_mod.DEFAULTS["config"].get(field.key), int):
        return int(v), None
    return v, None


class SettingModal(ModalScreen):
    """One option: what it does, what it is now, what the bounds are.

    The help text is the reason this is a modal instead of 22 inline inputs —
    `stall_kick_backoff_s` is not editable by someone who has to guess at it,
    and the range shown here is the one validate() enforces.
    """

    DEFAULT_CSS = """
    SettingModal {
        align: center middle;
    }
    SettingModal #set-box {
        width: 76;
        height: auto;
        max-height: 90%;
        border: tall;
        padding: 1 2;
    }
    SettingModal #set-help {
        margin-bottom: 1;
    }
    SettingModal .set-ctx {
        color: $text-muted;
    }
    SettingModal #set-err {
        color: $error;
        height: auto;
        min-height: 0;
    }
    SettingModal #set-actions {
        /* textual's container default is height: 1fr — without this the
           button row swallowed every remaining line and the box was mostly
           empty air */
        height: auto;
    }
    SettingModal #set-actions Button {
        margin-top: 1;
        margin-right: 1;
    }
    """
    AUTO_FOCUS = "#set-input"
    BINDINGS = [Binding("escape", "cancel", "Cancel"),
                Binding("enter", "commit", "Set")]

    def __init__(self, field: config_mod.Field, current):
        super().__init__()
        self.field = field
        self.current = current

    def compose(self):
        f = self.field
        default = config_mod.DEFAULTS["config"].get(f.key)
        with Vertical(id="set-box"):
            yield Label(f"{f.label}  ({f.key})")
            yield Static(f.help, id="set-help")
            bounds = [p for p in (
                f"min {fmt_value(f.lo)}" if f.lo is not None else "",
                f"max {fmt_value(f.hi)}" if f.hi is not None else "",
                f.unit) if p]
            if bounds:
                yield Label(" · ".join(bounds), classes="set-ctx")
            now = ("not set" if self.current is None
                   else fmt_value(self.current))
            yield Label(f"built-in default: {fmt_value(default)}   "
                        f"now: {now}", classes="set-ctx")
            if f.kind == "bool":
                yield Switch(value=bool(self.current), id="set-input")
            else:
                restrict = r"[0-9.+-]" if f.kind == "number" else None
                yield Input(value=("" if self.current is None
                                   else fmt_value(self.current)),
                            placeholder=(
                                "blank = remove it, use the built-in default "
                                f"({fmt_value(default)})"
                                if f.kind == "number" else
                                "the command to run"),
                            restrict=restrict, id="set-input")
            yield Static("", id="set-err")
            with Horizontal(id="set-actions"):
                yield Button("Set", id="set-ok", variant="primary")
                if self.current is not None and f.kind != "str":
                    yield Button("Use default", id="set-default")
                yield Button("Cancel", id="set-cancel")

    def _fail(self, msg: str):
        self.query_one("#set-err", Static).update(msg)

    def _commit(self, raw):
        value, err = parse_value(self.field, raw) if isinstance(raw, str) \
            else (raw, None)
        if err:
            self._fail(err)
            return
        self.dismiss((CLEAR, None) if value is CLEAR else ("set", value))

    def on_button_pressed(self, event):
        if event.button.id == "set-cancel":
            self.dismiss(None)
        elif event.button.id == "set-default":
            self.dismiss((CLEAR, None))
        elif event.button.id == "set-ok":
            w = self.query_one("#set-input")
            if isinstance(w, Switch):
                self.dismiss(("set", w.value))
            else:
                self._commit(w.value)

    def on_input_submitted(self, event):
        self._commit(event.value)

    def action_commit(self):
        w = self.query_one("#set-input")
        if isinstance(w, Switch):
            self.dismiss(("set", w.value))
        else:
            self._commit(w.value)

    def action_cancel(self):
        self.dismiss(None)


class SettingsScreen(BaseScreen):
    NAV_ID = "nav-5"
    #: unlike the other screens, the table takes focus on mount: this screen
    # is one list and nothing else, so arrows moving the row cursor cannot
    # steal input from anything, and without it a keyboard user could not
    # reach the rows at all (every other screen keeps focus off tables because
    # they have Inputs whose digits would be eaten).
    AUTO_FOCUS = "#settings"
    BINDINGS = [Binding("s", "save_settings", "Save settings"),
                Binding("r", "revert_settings", "Revert"),
                Binding("enter", "edit_setting", "Edit setting")]
    CSS = """
    #settings-body {
        height: 1fr;
    }
    #settings {
        height: auto;
    }
    #settings-foot {
        height: auto;
        margin-top: 1;
    }
    #settings-actions {
        height: auto;
        margin-top: 1;
    }
    #settings-actions Button {
        margin-right: 1;
    }
    """

    def content(self) -> Iterator:
        yield Static("Daemon settings", id="settings-title",
                     classes="title")
        with VerticalScroll(id="settings-body"):
            yield DataTable(id="settings")
            yield Static("↑/↓ move · Enter or click to edit · blank value "
                         "= built-in default · the daemon picks a save "
                         "up within ~1 s", classes="hint", id="settings-foot")
        with Horizontal(id="settings-actions"):
            yield Button("Save", id="save-settings", variant="primary")
            yield Button("Discard changes", id="revert-settings")

    def on_mount(self):
        super().on_mount()
        #: pending edits, applied to the config only on Save (see docstring).
        # Lives on the app so a screen switch (which builds a new instance)
        # does not throw them away.
        self._edits: dict[str, object] = self.app.settings_edits
        table = self.query_one("#settings", DataTable)
        table.add_columns(("", "grp"), ("setting", "label"),
                          ("value", "val"), ("unsaved", "pend"))
        table.cursor_type = "row"
        self.query_one("#settings-title", Static).update(
            f"Daemon settings — {self.app.cfg_path}")
        self._refresh()

    # ------------------------------------------------------------- render
    def _effective(self, key: str):
        """What the screen says this option should be: the pending edit if
        there is one (CLEAR = remove the key), else what the config holds."""
        if key in self._edits:
            v = self._edits[key]
            return None if v is CLEAR else v
        return self.app.cfg.config.get(key)


    #: short group headers — the first column is paid for by every row on an
    # 80-column terminal, where "fail-safe (source unreadable)" would push the
    # values off the right edge
    GROUP_SHORT = {"Cooling loop": "loop",
                   "Fail-safe (source unreadable)": "fail-safe",
                   "Readings and liveness": "liveness",
                   "Stalled fan": "stalled fan",
                   "Escalation": "escalation"}

    def _rows(self):
        """Group headers appear in the first column of a group's first row:
        a DataTable has no section headers, and five tables would each want a
        border and a cursor."""
        seen: set[str] = set()
        for f in config_mod.CONFIG_FIELDS:
            grp = "" if f.group in seen else self.GROUP_SHORT.get(
                f.group, f.group.lower())
            seen.add(f.group)
            yield grp, f

    def _refresh(self):
        table = self.query_one("#settings", DataTable)
        prev_key = table_cursor_key(table)
        prev_hover = table_hover_key(table)
        default_of = config_mod.DEFAULTS["config"]
        table.clear()
        for grp, f in self._rows():
            current = self._effective(f.key)
            default = default_of.get(f.key)
            # the unit is folded into the value cell: a column of its own is
            # mostly empty and costs width an 80-column terminal lacks
            shown = ("not set" if current is None
                     else f"{fmt_value(current)} {f.unit}".strip())
            if current is not None and current != default:
                shown = f"{shown}  *"
            table.add_row(grp, f.label, shown,
                          "→ save" if f.key in self._edits else "",
                          key=f.key)
        restore_table_cursor(table, prev_key, prev_hover)

    # --------------------------------------------------------------- edit
    def _selected_key(self) -> str | None:
        table = self.query_one("#settings", DataTable)
        return table_cursor_key(table)

    def _start_edit(self, key: str | None):
        if not key or key not in config_mod.BY_KEY:
            self.notify("Pick a setting to edit first", severity="warning")
            return
        field = config_mod.BY_KEY[key]
        self.run_worker(self._edit_flow(field), exclusive=False)

    async def _edit_flow(self, field):
        current = self._effective(field.key)
        result = await self.app.push_screen_wait(SettingModal(field, current))
        if result is None:
            return                      # cancelled: nothing touched
        kind, value = result
        self._edits[field.key] = CLEAR if kind == CLEAR else value
        if self._edits[field.key] == self.app.cfg.config.get(field.key):
            # back to what the file says: no longer a pending change
            del self._edits[field.key]
        self._refresh()

    def action_edit_setting(self):
        self._start_edit(self._selected_key())

    def on_data_table_row_selected(self, event):
        if event.data_table.id != "settings":
            return
        key = getattr(event.row_key, "value", event.row_key)
        self._start_edit(None if key is None else str(key))

    # ------------------------------------------------------- save / revert
    def on_button_pressed(self, event):
        # NB: textual 8 dispatches on_* along the whole MRO, so
        # BaseScreen.on_button_pressed (sidebar nav) also runs for this
        # message — no super() here (that would double-fire nav).
        if event.button.id == "save-settings":
            self.action_save_settings()
        elif event.button.id == "revert-settings":
            self.action_revert_settings()

    def _pending_changes(self) -> list[str]:
        """What Save would write, for the confirmation — a global change
        moves every fan at once, so it is worth one more look."""
        out = []
        for key, edit in self._edits.items():
            was = self.app.cfg.config.get(key)
            now = self._effective(key)
            if was == now:
                continue
            was_txt = fmt_value(was) if was is not None else "default"
            now_txt = fmt_value(now) if now is not None else "default"
            out.append(f"{key}: {was_txt} → {now_txt}")
        return out

    def action_save_settings(self):
        self.run_worker(self._save_flow(), exclusive=False)

    async def _save_flow(self):
        changed = self._pending_changes()
        if not changed:
            self.notify("Nothing to save — every value is as the file "
                        "has it", severity="warning")
            return
        ok = await self.app.push_screen_wait(ConfirmSettingsScreen(changed))
        if not ok:
            return
        # applied only now, and dropped on failure: an invalid value must not
        # reach the file (the daemon would keep the old config silently), and
        # the edit stays on screen so it can be corrected
        for key, edit in self._edits.items():
            if edit is CLEAR:
                self.app.cfg.config.pop(key, None)
            else:
                self.app.cfg.config[key] = edit
        if self.app.save_config("Global settings saved — daemon picks them "
                                "up within ~1 s"):
            self._edits.clear()
        self._refresh()

    def action_revert_settings(self):
        """Reload the file: unsaved edits here are dropped."""
        self.run_worker(self._revert_flow(), exclusive=False)

    async def _revert_flow(self):
        if not self._edits:
            self.notify("Nothing to discard — no unsaved changes here",
                        severity="warning")
            return
        ok = await self.app.push_screen_wait(ConfirmScreen(
            "Discard unsaved settings?",
            ("These edits were never written. The screen goes back to what "
             "the config file says."),
            ok_label="Discard", ok_variant="error"))
        if not ok:
            return
        self._edits.clear()
        self._refresh()


class ConfirmSettingsScreen(ConfirmScreen):
    """What Save is about to change, before it changes every fan at once."""

    def __init__(self, changed: list[str]):
        super().__init__(
            f"Save {len(changed)} global setting(s)?",
            tuple(changed[:12]) + (("...",) if len(changed) > 12 else ())
            + ("These are the defaults for every assignment — the daemon "
               "reloads within ~1 s.",),
            ok_label="Save", ok_variant="primary")
