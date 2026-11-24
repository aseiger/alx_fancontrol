"""Curves: points table + live text-plot preview + curve management.

Left: DataTable of [temp, duty] points — cursor-selectable, bindings:
  a add point (inserted between the selected row and the next one,
  prompt pre-filled at their midpoint; at the end of the curve:
  last + 5 °C; on an EMPTY curve: the first point), d delete,
  +/− nudge duty ±1,
  shift+left/right nudge temp ±1, shift+up/down nudge duty ±5.
Right: a text plot (curve as █ columns, live temperature marker ◄,
point markers ● with the selected point as ◆) with mouse support:
drag a ● point to move it. Points are added ONLY from the table —
clicking the plot never adds anything.
Save → curves[id].points (sorted) + curves[id].label → app.save_config().

Curve management (buttons, and `n` / `c` / `shift+d`):
  * **New** creates a curve (id + label asked for — the id is what
    assignments reference, so it is never invented and a collision is
    refused rather than silently overwriting), seeded with the built-in
    default shape so the config stays valid and the first `a` has
    something to insert between;
  * **Copy** duplicates the curve AS CURRENTLY EDITED (unsaved point moves
    included) — the normal way to tune "same curve, shifted 5 °C";
  * **Delete** removes an UNUSED curve after confirmation. A curve that
    assignments still reference is refused with the list of them named:
    deleting it would leave those assignments without one (the daemon skips
    them and their fans sit at their last duty), so re-point them on the
    Assign screen first. The two built-in curves can be deleted too — what
    the file says is what loads (`config._merge_defaults`); they are seeded
    into a file that has no `curves` section at all, not re-injected into one
    that does.
A hint line shows which assignments use the selected curve for exactly that
reason.

The curve knows NOTHING about floor/ceiling: those are per-assignment
fan settings on the Assign screen (daemon.py: assignment floor_duty /
max_duty, falling back to the global config floor_duty default).
"""
from __future__ import annotations

import re
from collections.abc import Iterator

from rich.text import Text
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Input, Label, Select, Static

from alx_fancontrol import config as config_mod
from alx_fancontrol import curve as curve_mod
from alx_fancontrol.tui.reader import LiveUpdate
from alx_fancontrol.tui.screens.base import (BaseScreen, ConfirmScreen,
                                             sel_value)

PLOT_W = 58
PLOT_H = 16


# Plot marker glyphs
POINT_CH = "●"      # a curve point (drag to move)
SEL_POINT_CH = "◆"  # the selected point (from the table)
LIVE_CH = "◄"       # live temperature of the preview source


def render_curve_plot(points, temp_now=None,
                      sel_point=None,
                      w: int = PLOT_W, h: int = PLOT_H) -> Text:
    """Render the curve as a filled text plot.

    t-range: [min-5, max+5]; columns = temps, height = duty 0-100.
    Live temp as a ◄ marker on the curve; every point gets a ● marker
    (selected: ◆) drawn LAST so the interactive targets are always
    visible on top of the curve. No floor/ceiling here — those are
    per-assignment fan settings, not curve data.
    """
    pts = sorted((float(t), float(d)) for t, d in points)
    if not pts:
        out = Text()
        out.append("  (no points — press a to add one)", style="dim")
        return out
    t_lo = min(p[0] for p in pts) - 5
    t_hi = max(p[0] for p in pts) + 5
    if t_hi <= t_lo:
        t_hi = t_lo + 1

    def to_grid(t, d):
        col = int(round((t - t_lo) / (t_hi - t_lo) * (w - 1)))
        row = h - 1 - int(round(max(0.0, min(100.0, d)) / 100 * (h - 1)))
        return max(0, min(w - 1, col)), max(0, min(h - 1, row))

    grid = [[" "] * w for _ in range(h)]
    for col in range(w):
        t = t_lo + (t_hi - t_lo) * col / (w - 1)
        y = curve_mod.eval_curve(pts, t)
        height = int(round(max(0.0, min(100.0, y)) / 100 * (h - 1)))
        for r in range(h - 1, h - 1 - height, -1):
            grid[r][col] = "█"
    if temp_now is not None and t_lo <= temp_now <= t_hi:
        col, row = to_grid(temp_now, curve_mod.eval_curve(pts, temp_now))
        grid[row][col] = LIVE_CH
    # point markers last: the click/drag targets must always be visible
    for (t, d) in pts:
        c, r = to_grid(t, d)
        grid[r][c] = POINT_CH
    if sel_point is not None:
        c, r = to_grid(float(sel_point[0]), float(sel_point[1]))
        grid[r][c] = SEL_POINT_CH
    out = Text()
    cell_styles = {POINT_CH: "bold white",
                   SEL_POINT_CH: "bold yellow",
                   LIVE_CH: "bold green"}
    for row in grid:
        buf, style = "", None
        for ch in row:
            ch_style = None if ch == " " else cell_styles.get(ch,
                                                              "bold cyan")
            if ch_style != style:
                if buf:
                    out.append(buf, style=style)
                buf, style = ch, ch_style
            else:
                buf += ch
        if buf:
            out.append(buf, style=style)
        out.append("\n")
    lo, hi = f"{t_lo:.0f}C", f"{t_hi:.0f}C"
    out.append(f" {lo}{' ' * max(1, w - len(lo) - len(hi))}{hi}", style="dim")
    return out


class PointPromptModal(ModalScreen[tuple[float, int] | None]):
    """Prompted (temp, duty) for the 'a' binding — pre-filled with the
    insertion position computed from the selected table row (midpoint
    of the selected and the next point; last + 5 °C at the end)."""

    DEFAULT_CSS = """
    PointPromptModal {
        align: center middle;
    }
    PointPromptModal #pp-box {
        width: 44;
        height: auto;
        border: tall;
        padding: 1 2;
    }
    PointPromptModal .pp-ctx {
        color: $text-muted;
    }
    PointPromptModal #pp-box Button {
        margin-top: 1;
        width: 18;
    }
    """

    def __init__(self, defaults: tuple[float, int] | None = None,
                 context: str = ""):
        super().__init__()
        self.defaults = defaults
        self.context = context

    def compose(self):
        with Vertical(id="pp-box"):
            yield Label("New curve point")
            if self.context:
                yield Label(self.context, classes="pp-ctx")
            yield Input(id="pp-temp", placeholder="temp °C",
                        restrict=r"[0-9.\-]")
            yield Input(id="pp-duty", placeholder="duty % (0-100)",
                        restrict=r"[0-9]", max_length=3)
            yield Button("Add", id="pp-ok", variant="primary")
            yield Button("Cancel", id="pp-cancel")

    def on_mount(self):
        if self.defaults is not None:
            self.query_one("#pp-temp", Input).value = f"{self.defaults[0]:g}"
            self.query_one("#pp-duty", Input).value = str(self.defaults[1])

    def on_button_pressed(self, event):
        if event.button.id == "pp-cancel":
            self.dismiss(None)
        elif event.button.id == "pp-ok":
            try:
                t = float(self.query_one("#pp-temp", Input).value)
                d = float(self.query_one("#pp-duty", Input).value)
            except ValueError:
                self.notify("Enter numbers for both fields",
                            severity="warning")
                return
            if not (0 <= d <= 100):
                self.notify("duty must be 0-100", severity="warning")
                return
            self.dismiss((t, int(round(d))))


class NewCurveModal(ModalScreen[tuple[str, str] | None]):
    """(id, label) for a new or copied curve.

    The id is asked for rather than invented because assignments reference
    curves by it, and a collision is refused here instead of quietly
    overwriting a curve some other fan depends on."""

    ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")

    DEFAULT_CSS = """
    NewCurveModal {
        align: center middle;
    }
    NewCurveModal #nc-box {
        width: 52;
        height: auto;
        border: tall;
        padding: 1 2;
    }
    NewCurveModal .nc-ctx {
        color: $text-muted;
    }
    NewCurveModal #nc-box Button {
        margin-top: 1;
        margin-right: 1;
        width: 18;
    }
    """

    def __init__(self, suggested_id: str = "", label: str = "",
                 context: str = "", taken=()):
        super().__init__()
        self.suggested_id = suggested_id
        self.label = label
        self.context = context
        self.taken = set(taken)

    def compose(self):
        with Vertical(id="nc-box"):
            yield Label("New curve")
            if self.context:
                yield Label(self.context, classes="nc-ctx")
            yield Input(placeholder="id (letters, digits, - and _)",
                        id="nc-id", restrict=r"[A-Za-z0-9_-]+",
                        max_length=24)
            yield Input(placeholder="label (shown in the pickers)",
                        id="nc-label", max_length=32)
            yield Button("Create", id="nc-ok", variant="primary")
            yield Button("Cancel", id="nc-cancel")

    def on_mount(self):
        self.query_one("#nc-id", Input).value = self.suggested_id
        self.query_one("#nc-label", Input).value = self.label

    def on_button_pressed(self, event):
        if event.button.id == "nc-cancel":
            self.dismiss(None)
            return
        if event.button.id != "nc-ok":
            return
        cid = self.query_one("#nc-id", Input).value.strip()
        label = self.query_one("#nc-label", Input).value.strip()
        if not cid:
            self.notify("Give the curve an id", severity="warning")
            return
        if not self.ID_RE.match(cid):
            self.notify("id: letters, digits, - and _ only",
                        severity="warning")
            return
        if cid in self.taken:
            self.notify(f"A curve named '{cid}' already exists",
                        severity="warning")
            return
        self.dismiss((cid, label or cid))


class CurvePlot(Static):
    """Text plot with mouse: drag a ● point (radius 3 cells) → move it.
    Clicking empty plot area does NOTHING — points are added only from
    the table ('a'). All characters used are single-cell wide, so
    pixel→cell mapping is a straight division."""

    def __init__(self, editor: "CurveEditorScreen"):
        super().__init__(id="plot", classes="curve-plot")
        self._editor = editor
        self._dragging: int | None = None
        self.update(self._build_plot())

    def _build_plot(self) -> Text:
        # NOTE: must not be named _render — that shadows Widget._render,
        # which the layout engine calls expecting a Visual, not a rich Text
        return render_curve_plot(self._editor.pts,
                                 self._editor.temp_now(),
                                 sel_point=self._editor._sel_point,
                                 w=self._editor.plot_w(), h=PLOT_H)

    def refresh_plot(self):
        self.update(self._build_plot())

    def on_resize(self, _event):
        # the plot renders at its own width — a resize changes that
        # width, and Static caches its renderable, so rebuild it
        self.refresh_plot()

    def _to_grid(self, e):
        if self.size.width < 1 or self.size.height < 1:
            return None
        cw = self.region.width / self.size.width
        ch = self.region.height / self.size.height
        if cw <= 0 or ch <= 0:
            return None
        col = int(e.x // cw)
        row = int(e.y // ch)
        if not (0 <= col < self._editor.plot_w() and 0 <= row < PLOT_H):
            return None
        return col, row

    def on_mouse_down(self, e):
        if e.button != 1:
            return
        g = self._to_grid(e)
        if g is None:
            return
        idx = self._editor.nearest_point_index(*g, radius=3)
        if idx is not None:
            self._dragging = idx
            e.stop()
        # empty plot area: nothing to do — points are added from the
        # table ('a'), never by clicking the plot

    def on_mouse_move(self, e):
        if self._dragging is None:
            return
        g = self._to_grid(e)
        if g is None:
            return
        t, d = self._editor.grid_to_point(*g)
        self._editor.set_point(self._dragging, t, d)

    def on_mouse_up(self, e):
        self._dragging = None


class CurveEditorScreen(BaseScreen):
    NAV_ID = "nav-3"
    BINDINGS = [
        Binding("a", "add_point", "Add point"),
        Binding("d", "delete_point", "Delete point"),
        Binding("s", "save_curve", "Save curve"),
        Binding("n", "new_curve", "New curve"),
        Binding("c", "duplicate_curve", "Copy curve"),
        Binding("shift+d", "delete_curve", "Delete curve"),
        Binding("+", "nudge_duty_up", "Duty +1"),
        Binding("-", "nudge_duty_down", "Duty -1"),
        Binding("shift+left", "nudge_temp_left", "Temp -1"),
        Binding("shift+right", "nudge_temp_right", "Temp +1"),
        Binding("shift+up", "nudge_duty_up5", "Duty +5"),
        Binding("shift+down", "nudge_duty_down5", "Duty -5"),
    ]
    CSS = """
    /* The plot widget must stay exactly PLOT_H+1 rows (16 plot rows +
       axis row) — left to fill it would stretch the column and push
       the Save button below short terminals. */
    #plot {
        height: 17;
    }
    /* One row of curve actions. Buttons keep their natural width; the row
       is pinned to a single control row so nothing stretches. */
    #curve-manage {
        height: 3;
    }
    #curve-manage Button {
        width: auto;
        margin-right: 1;
    }
    #curve-label-row {
        height: 3;
    }
    #curve-label {
        width: 1fr;
    }
    #curve-used {
        width: auto;
        margin-left: 1;
        color: $text-muted;
    }
    """

    def content(self) -> Iterator:
        # set before compose builds the CurvePlot (its __init__ renders)
        self.pts: list[list[float]] = []
        self._sel_point: list[float] | None = None
        with Horizontal():
            with Vertical(id="curve-left"):
                yield Select([], id="curve-pick", prompt="curve")
                with Horizontal(id="curve-manage"):
                    yield Button("New", id="new-curve")
                    yield Button("Copy", id="dup-curve")
                    yield Button("Delete", id="del-curve", variant="error")
                with Horizontal(id="curve-label-row"):
                    yield Input(placeholder="label (shown in pickers)",
                                id="curve-label", max_length=32)
                    yield Static("", id="curve-used")
                yield DataTable(id="points")
                yield Static("a add (between selected row & next) · "
                             "d delete · s save · +/− duty ±1 · "
                             "shift+←/→ temp ±1 · shift+↑/↓ duty ±5 · "
                             "drag ● points (◆ = selected) · n new · c copy"
                             " · shift+d delete curve", classes="hint")
            with Vertical(id="curve-right"):
                yield Select([], id="preview-src", prompt="preview source")
                yield CurvePlot(self)
                yield Button("Save curve", id="save-curve",
                             variant="primary")

    # --------------------------------------------------------------- init
    def on_mount(self):
        app = self.app
        table = self.query_one("#points", DataTable)
        table.add_columns(("temp °C", "pt"), ("duty %", "pd"))
        table.cursor_type = "row"  # row events need this
        curve_opts = self._refresh_curve_options()
        pv = self.query_one("#preview-src", Select)
        src_opts = [(s.label, s.id)
                    for s in sorted(app.sources, key=lambda x: x.id)]
        pv.set_options(src_opts)
        if src_opts:
            pv.value = src_opts[0][1]
        self._load_curve(curve_opts[0][1] if curve_opts else None)

    def _load_curve(self, cid):
        crv = self.app.cfg.curves.get(cid) if cid else None
        self.pts = [[float(t), float(d)]
                    for t, d in (crv or {}).get("points", [])]
        self._sel_point = self.pts[0] if self.pts else None
        self.query_one("#curve-label", Input).value = \
            (crv or {}).get("label", cid or "")
        self._refresh_used(cid)
        self._refresh()

    def _refresh_curve_options(self, keep: str | None = "") -> list[tuple]:
        """Rebuild the curve picker (after create/copy/delete) and return
        its options. `set_options()` resets the selection to blank — which
        is why first mount has to re-assert it, or Save bails with "Pick a
        curve" while the table is already showing a curve's points.
        keep=None means "whatever is selected now" (falling back to the
        first option when that id is gone)."""
        pick = self.query_one("#curve-pick", Select)
        current = keep if keep is not None else sel_value(pick)
        opts = [(c.get("label", cid), cid)
                for cid, c in sorted(self.app.cfg.curves.items())]
        pick.set_options(opts)
        if opts:
            values = [v for _l, v in opts]
            pick.value = current if current in values else opts[0][1]
        return opts

    def _used_by(self, cid: str) -> list[str]:
        """Assignments referencing this curve — the reason it cannot simply
        be deleted."""
        return sorted(name for name, a in self.app.cfg.assignments.items()
                      if a.get("curve") == cid)

    def _refresh_used(self, cid):
        used = self._used_by(cid) if cid else []
        self.query_one("#curve-used", Static).update(
            "used by: " + ", ".join(used) if used
            else "not used by any assignment")

    def on_select_changed(self, event: Select.Changed):
        if event.select.id == "curve-pick":
            self._load_curve(sel_value(event.select))

    # ------------------------------------------------------- curve CRUD --
    @staticmethod
    def _free_id(base: str, taken) -> str:
        cid, n = base, 2
        while cid in taken:
            cid, n = f"{base}-{n}", n + 1
        return cid

    def action_new_curve(self):
        # 'n' / New. Seeded with the built-in default shape: a curve with no
        # points is invalid (validate() needs >= 1), and seeding also means
        # there is immediately a row to insert between.
        self.run_worker(self._new_curve_flow(
            suggested=self._free_id("newcurve", set(self.app.cfg.curves)),
            label="",
            context="seeded with the default curve's points — then drag or "
                    "nudge them and press s",
            seed=[list(p) for p in
                  config_mod.DEFAULTS["curves"]["default"]["points"]]),
            exclusive=False)

    def action_duplicate_curve(self):
        # 'c' / Copy — the normal way to tune ("same curve, shifted 5 °C").
        # Copies what is on screen, unsaved moves included.
        cid = sel_value(self.query_one("#curve-pick", Select))
        if not cid:
            self.notify("Pick a curve to copy first", severity="warning")
            return
        lab = self.app.cfg.curves[cid].get("label", cid)
        self.run_worker(self._new_curve_flow(
            suggested=self._free_id(f"{cid}-copy", set(self.app.cfg.curves)),
            label=f"{lab} copy",
            context=f"copied from '{lab}' as currently edited (unsaved "
                    "moves included)",
            seed=[list(p) for p in self._sorted_pts()]), exclusive=False)

    async def _new_curve_flow(self, suggested, label, context, seed):
        result = await self.app.push_screen_wait(
            NewCurveModal(suggested_id=suggested, label=label,
                          context=context, taken=set(self.app.cfg.curves)))
        if result is None:
            return
        cid, lab = result
        # persisted immediately: the config is the truth and a seeded curve
        # is valid, so nothing is lost by writing it now
        self.app.cfg.curves[cid] = {"label": lab, "points": seed}
        if self.app.save_config(f"Curve '{lab}' created — set its points "
                                "and press s"):
            self._refresh_curve_options(keep=cid)
            self._load_curve(cid)

    def action_delete_curve(self):
        # shift+d / Delete. A curve assignments still reference is refused by
        # name: the daemon skips an assignment whose curve is missing (its
        # fans stay at their last duty), so deleting one is a silent loss of
        # cooling — re-pointing is the Assign screen's job.
        cid = sel_value(self.query_one("#curve-pick", Select))
        if not cid:
            self.notify("Pick a curve to delete first", severity="warning")
            return
        used = self._used_by(cid)
        if used:
            self.notify(f"Curve '{cid}' is used by {len(used)} assignment(s): "
                        f"{', '.join(used)} — re-point them on the Assign "
                        "screen (2) first", severity="error", timeout=12)
            return
        self.run_worker(self._delete_curve_flow(cid), exclusive=False)

    async def _delete_curve_flow(self, cid):
        lab = self.app.cfg.curves[cid].get("label", cid)
        ok = await self.app.push_screen_wait(ConfirmScreen(
            f"Delete curve '{lab}'?",
            ("No assignment uses it. There is no undo in the TUI —",
             "config.json.bak keeps the previous file."),
            ok_label="Delete", ok_variant="error"))
        if not ok:
            return
        del self.app.cfg.curves[cid]
        if self.app.save_config(f"Curve '{lab}' deleted"):
            opts = self._refresh_curve_options(keep=None)
            self._load_curve(opts[0][1] if opts else None)

    # -------------------------------------------------------------- table
    def _sorted_pts(self):
        return sorted(self.pts, key=lambda p: p[0])

    def _refresh(self):
        table = self.query_one("#points", DataTable)
        table.clear()
        for i, p in enumerate(self._sorted_pts()):
            table.add_row(p[0], p[1], key=i)
        if self.pts:
            table.move_cursor(row=0)
            if self._sel_point is not None:
                for i, p in enumerate(self._sorted_pts()):
                    if p == self._sel_point:
                        table.move_cursor(row=i)
                        break
        self.query_one("#plot", CurvePlot).refresh_plot()

    def on_data_table_row_highlighted(self, event):
        p = self._sorted_pts()
        if 0 <= event.cursor_row < len(p):
            self._sel_point = p[event.cursor_row]
            self.query_one("#plot", CurvePlot).refresh_plot()

    def _selected(self) -> list[float] | None:
        if self._sel_point is None:
            return None
        for p in self.pts:
            if p == self._sel_point:
                return p
        return None

    # ------------------------------------------------------------- edits
    def action_add_point(self):
        # 'a' — insert a point between the SELECTED table row and the
        # next one (prompt pre-filled at their midpoint), or tack one on
        # at the end when the selected row is the last. Points are only
        # ever added from the table.
        sel = self._selected()
        if sel is None:
            if not self.pts:
                # An empty curve used to be a dead end: 'a' demanded a
                # selected row, so the first point could never exist — and
                # deleting every point of a curve bricked it.
                self.run_worker(self._add_point(
                    (40.0, 20), "first point of this curve"),
                    exclusive=False)
                return
            self.notify("Select a point row first", severity="warning")
            return
        sp = self._sorted_pts()
        i = sp.index(sel)
        if i + 1 < len(sp):
            nxt = sp[i + 1]
            defaults = (round((sel[0] + nxt[0]) / 2.0, 1),
                        int(round((sel[1] + nxt[1]) / 2.0)))
            context = (f"inserted between {sel[0]:g} °C and "
                       f"{nxt[0]:g} °C")
        else:
            defaults = (round(sel[0] + 5.0, 1), int(round(sel[1])))
            context = f"after {sel[0]:g} °C (end of curve)"
        # push_screen(wait_for_dismiss=True) is worker-only in textual >= 8,
        # and an async key action would additionally block this screen's
        # message pump while the modal is open. Run the flow in a worker —
        # same pattern as FanControlApp.save_config's stale-write guard.
        self.app.run_worker(self._add_point(defaults, context),
                            exclusive=False)

    async def _add_point(self, defaults, context):
        result = await self.app.push_screen_wait(
            PointPromptModal(defaults=defaults, context=context))
        if result is None:
            return
        t, d = result
        if any(abs(q[0] - t) < 1e-9 for q in self.pts):
            self.notify("A point at that temp already exists",
                        severity="warning")
            return
        self.pts.append([t, d])
        self._sel_point = [t, d]
        self._refresh()

    def action_delete_point(self):
        p = self._selected()
        if p is None:
            self.notify("Select a point row first", severity="warning")
            return
        self.pts.remove(p)
        self._sel_point = None
        self._refresh()

    def _nudge(self, dt: float = 0, dd: float = 0):
        p = self._selected()
        if p is None:
            self.notify("Select a point row first", severity="warning")
            return
        p[0] = round(p[0] + dt, 2)
        p[1] = round(max(0.0, min(100.0, p[1] + dd)), 1)
        self._sel_point = p  # keep tracking the (mutated) point object
        self._refresh()

    def action_nudge_duty_up(self):
        self._nudge(dd=1)

    def action_nudge_duty_down(self):
        self._nudge(dd=-1)

    def action_nudge_duty_up5(self):
        self._nudge(dd=5)

    def action_nudge_duty_down5(self):
        self._nudge(dd=-5)

    def action_nudge_temp_left(self):
        self._nudge(dt=-1)

    def action_nudge_temp_right(self):
        self._nudge(dt=1)

    # ----------------------------------------------------------- plot map
    def plot_w(self) -> int:
        """The plot's current text width — the render AND the mouse
        mapping must agree on it (the plot is only as wide as its
        column, not PLOT_W)."""
        try:
            w = self.query_one("#plot", CurvePlot).size.width
            return w if w > 0 else PLOT_W
        except Exception:
            return PLOT_W

    def _axes(self):
        pts = self._sorted_pts() or [[0.0, 0.0]]
        t_lo = min(p[0] for p in pts) - 5
        t_hi = max(p[0] for p in pts) + 5
        if t_hi <= t_lo:
            t_hi = t_lo + 1
        return t_lo, t_hi

    def point_to_grid(self, t, d):
        t_lo, t_hi = self._axes()
        w = self.plot_w()
        col = int(round((t - t_lo) / (t_hi - t_lo) * (w - 1)))
        row = PLOT_H - 1 - int(round(max(0.0, min(100.0, d)) / 100
                                     * (PLOT_H - 1)))
        return max(0, min(w - 1, col)), max(0, min(PLOT_H - 1, row))

    def grid_to_point(self, col, row):
        t_lo, t_hi = self._axes()
        w = self.plot_w()
        t = t_lo + (t_hi - t_lo) * col / (w - 1)
        d = (PLOT_H - 1 - row) / (PLOT_H - 1) * 100
        return round(t, 1), int(round(max(0, min(100, d))))

    def nearest_point_index(self, col, row, radius: int = 3) -> int | None:
        """Index (into _sorted_pts()) of the point nearest (col,row),
        or None beyond `radius` cells."""
        best_i, best_dist = None, None
        for i, (t, d) in enumerate(self._sorted_pts()):
            c, r = self.point_to_grid(t, d)
            dist = max(abs(c - col), abs(r - row))
            if dist <= radius and (best_dist is None or dist < best_dist):
                best_i, best_dist = i, dist
        return best_i

    def set_point(self, sorted_idx, t, d):
        sp = self._sorted_pts()
        if not (0 <= sorted_idx < len(sp)):
            return
        p = sp[sorted_idx]
        i = self.pts.index(p)
        self.pts[i][0] = t
        self.pts[i][1] = d
        self._sel_point = [t, d]
        self._refresh()

    # -------------------------------------------------------- live plot --
    def temp_now(self) -> float | None:
        try:
            sid = sel_value(self.query_one("#preview-src", Select))
        except Exception:
            return None
        if not sid:
            return None
        return (self.app.live.get("sources") or {}).get(sid, {}).get("temp_c")

    def on_live_update(self, msg: LiveUpdate):
        self.query_one("#plot", CurvePlot).refresh_plot()

    # --------------------------------------------------------------- save
    def on_button_pressed(self, event):
        # NB: textual 8 dispatches on_* handlers along the WHOLE MRO,
        # so BaseScreen.on_button_pressed (sidebar nav) also runs for
        # this message — no super() call here (that would double-fire
        # nav actions and double-switch screens).
        bid = event.button.id
        if bid == "save-curve":
            self.save_curve()
        elif bid == "new-curve":
            self.action_new_curve()
        elif bid == "dup-curve":
            self.action_duplicate_curve()
        elif bid == "del-curve":
            self.action_delete_curve()

    def action_save_curve(self):
        # 's' binding — the Save button can fall below the fold on short
        # terminals (the plot is 16 rows tall), so keep a key path.
        self.save_curve()

    def save_curve(self):
        cid = sel_value(self.query_one("#curve-pick", Select))
        if not cid:
            self.notify("Pick a curve", severity="warning")
            return
        pts = [[p[0], p[1]] for p in self._sorted_pts()]
        for i in range(1, len(pts)):
            if pts[i][0] <= pts[i - 1][0]:
                self.notify("Curve temps must be strictly increasing",
                            severity="error")
                return
        for t, d in pts:
            if not (0 <= d <= 100):
                self.notify("Duty must be 0-100", severity="error")
                return
        self.app.cfg.curves[cid]["points"] = pts
        # the label is part of the same edit: one field, one Save. A blank
        # field is not an error worth interrupting for — it falls back to
        # whatever label the curve has, or its id.
        lab = (self.query_one("#curve-label", Input).value.strip()
               or self.app.cfg.curves[cid].get("label") or cid)
        self.app.cfg.curves[cid]["label"] = lab
        if self.app.save_config(f"Curve '{lab}' saved"):
            # the picker shows labels, so a rename is only visible once the
            # options are rebuilt
            self._refresh_curve_options(keep=cid)
            self._load_curve(cid)
