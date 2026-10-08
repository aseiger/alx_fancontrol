"""Shared screen shell: Header + Footer + left sidebar (nav 1-4) + content.

Each of the four screens is a real textual Screen (so the app's key
bindings 1-4 switch between them); the sidebar is re-composed by every
screen through this base class.

Nav items are NON-FOCUSABLE on purpose: a focused button shows the
white focus tint, and after every screen switch focus would land on the
first focusable widget (the top nav button) — so the highlight appeared
to "jump back to the top". The selection is shown by the `active`
class instead (set on_mount by the screen that owns it)."""
from __future__ import annotations

from collections.abc import Iterator

from textual.containers import Horizontal, Vertical
from textual.coordinate import Coordinate
from textual.screen import Screen
from textual.widgets import Button, DataTable, Footer, Header, Select, Static


def _row_key_at(table: DataTable, row: int):
    try:
        return table.coordinate_to_cell_key((row, 0))[0]
    except Exception:
        return None


def _find_row_by_key(table: DataTable, key):
    if key is None:
        return None
    for r in range(table.row_count):
        if _row_key_at(table, r) == key:
            return r
    return None


def table_cursor_key(table: DataTable):
    """Row key under the cursor (call BEFORE table.clear()) — the cursor
    AND the mouse-hover highlight are both reset to (0,0) by clear(), so
    per-second live rebuilds must restore both by key or the user's
    selection yanks back to the top of the list."""
    try:
        row = table.cursor_row
    except Exception:
        return None
    if row is None or row >= table.row_count:
        return None
    return _row_key_at(table, row)


def table_hover_key(table: DataTable):
    """Row key under the mouse, or None (call BEFORE table.clear())."""
    try:
        row = table.hover_row
    except Exception:
        return None
    if row is None or row >= table.row_count:
        return None
    return _row_key_at(table, row)


def restore_table_cursor(table: DataTable, key, hover_key=None) -> None:
    """After clear()+rebuild: put the cursor back on the row with `key`
    (row 0 if that key no longer exists) and the hover highlight back on
    `hover_key` (clear() drops it to row 0 until the pointer moves)."""
    if table.row_count == 0:
        return
    table.move_cursor(row=_find_row_by_key(table, key) or 0)
    hrow = _find_row_by_key(table, hover_key)
    if hrow is not None:
        table.hover_coordinate = Coordinate(hrow, 0)


def sel_value(widget: Select):
    """Select.value, mapping textual 8's Select.NULL sentinel to None."""
    v = widget.value
    return None if v is Select.NULL else v


class NavButton(Button):
    """A sidebar nav item: clickable, but never takes keyboard focus —
    the current screen is shown with the `active` class, not a focus
    ring (a focus ring would land on the topmost button after every
    screen switch)."""
    can_focus = False


class BaseScreen(Screen):
    #: the sidebar button for this screen, e.g. "nav-1"
    NAV_ID: str | None = None

    #: No auto-focus on screen activation: a focused Input would swallow
    #: the 1-4 navigation keys (typing "3" into the name field instead of
    #: switching to Curves), and a focused table steals arrow keys. Keys
    #: navigate, clicks focus — focus is None until the user clicks.
    AUTO_FOCUS = ""

    def compose(self) -> Iterator:
        yield Header()
        yield Footer()
        with Horizontal(id="shell"):
            with Vertical(id="sidebar"):
                if not self.app.is_root:
                    yield Static("⚠ non-root: no fan control — "
                                 "use sudo alx-fancontrol", id="root-warn")
                yield NavButton("1 · Overview", id="nav-1")
                yield NavButton("2 · Assign", id="nav-2")
                yield NavButton("3 · Curves", id="nav-3")
                yield NavButton("4 · Sources", id="nav-4")
                yield Static("", id="prot-hint")
            with Vertical(id="content"):
                yield from self.content()

    def content(self) -> Iterator:
        """Overridden by each screen."""
        return iter(())

    def on_mount(self):
        app = self.app
        if self.NAV_ID:
            self.query_one(f"#{self.NAV_ID}", NavButton).add_class("active")
        prot = app.cfg.protected
        if prot:
            lines = "protected (never touched):\n" + "".join(
                f"[prot]  {f} — reserved (gpu-fanctl)[/prot]\n" for f in prot)
        else:
            lines = "[prot]protected: (none!) — careful[/prot]"
        self.query_one("#prot-hint", Static).update(lines)

    def on_button_pressed(self, event):
        actions = {"nav-1": "show_overview",
                   "nav-2": "show_assign",
                   "nav-3": "show_curves",
                   "nav-4": "show_sources"}
        if event.button.id in actions:
            getattr(self.app, actions[event.button.id])()
