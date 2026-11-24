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
        # Degradation and errors get their own lines at the top: they are the
        # two things that mean "the fans are not following their curves", and
        # the daemon already publishes both (F10) — flattening them into one
        # joined status line is what made them unreadable on a box with four
        # GPUs (they scrolled past).
        yield Static("", id="degraded-line", classes="alarm")
        yield Static("", id="errors-box", classes="hint")
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
        # render the payload we were handed rather than re-reading app.live:
        # in the running app they are the same snapshot, but a screen that
        # ignores the message it was sent cannot be tested with one
        self._refresh(msg.data)

    # ------------------------------------------------------------ render
    def _refresh(self, data: dict | None = None):
        app = self.app
        if app.cfg is None:
            return
        data = data if data is not None else app.live
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

    def _refresh_fans(self, data):
        app = self.app
        fan = self.query_one("#fan-table", DataTable)
        prev_key = table_cursor_key(fan)
        prev_hover = table_hover_key(fan)
        fan.clear()
        # Fans of DISABLED assignments are listed too, marked as such: when
        # you disable one what you want to see is the header handing control
        # back (duty, RPM, who owns it now). Listing only enabled ones made
        # the row vanish at exactly that moment (F10).
        assign_of: dict[str, tuple[str, bool]] = {}
        for aid, a in app.cfg.assignments.items():
            on = bool(a.get("enabled", True))
            for f in a.get("fans", []):
                assign_of.setdefault(f, (aid, on))
        for f in sorted(assign_of):
            d = data.get("fans", {}).get(f, {})
            aid, on = assign_of[f]
            a = app.cfg.assignments[aid]
            sid = a.get("source")
            t = (data.get("sources") or {}).get(sid, {}).get("temp_c")
            src_cell = (f"{sid}: {t:.1f} °C" if t is not None
                        else f"{sid}: no reading")
            assign_cell = aid if on else f"{aid} (disabled)"
            rpm = d.get("rpm")
            # A blank RPM means one of two very different things, and the
            # difference decides whether a stall on this header can be seen
            # at all: no tach mapped (nothing to read) vs. a tach that reads
            # nothing right now. Say which.
            if rpm is None:
                rpm_cell = "no tach" if ("tach" in d and d["tach"] is None) \
                    else "—"
            else:
                rpm_cell = str(rpm)
            fan.add_row(
                f, d.get("label", "—"), assign_cell, src_cell,
                self._duty_cell(d),
                rpm_cell,
                self._fan_state(f, aid, data, enabled=on),
                key=f,
            )
        restore_table_cursor(fan, prev_key, prev_hover)

    @staticmethod
    def _duty_cell(d: dict) -> str:
        """Commanded duty, with what the chip actually holds when the two
        disagree (§20: commanded duty is not the same as ownership — a fan can
        read 20 % in the daemon's books while the register says something
        else). Only shown on drift: `raw` is always present, and a second
        number on every row would be noise."""
        duty = d.get("duty_pct")
        if duty is None:
            return "—"
        txt = f"{duty:.0f}"
        raw = d.get("duty_raw")
        if isinstance(raw, (int, float)) and abs(raw - duty * 255 / 100) > 3:
            txt = f"{txt} (raw {raw:g}!)"
        return txt

    #: the daemon's own state strings, said in words a person can act on.
    # `manual-conflict` alone (F5) tells you nothing about who to go argue
    # with, and it arrives one screen after the decision was made.
    STATE_WORDS = {
        "ok": "ok",
        "manual-conflict": "refused: held by another controller",
        "disabled": "refused: header disabled (enable 0)",
        "unresolved": "refused: no such channel",
        "curve-missing": "skipped: curve missing",
        "source-missing": "skipped: source missing",
        "failsafe": "FAIL-SAFE: source unreadable, ramping up",
        "stalled": "STALLED: high duty, ~0 RPM — pinned to full",
        "write-error": "write failed (pwm/pwmN_enable)",
        "ownership-lost": "OWNERSHIP LOST (firmware took it back)",
    }

    def _fan_state(self, fan: str, aid: str | None, data: dict,
                   enabled: bool = True) -> str:
        app = self.app
        if aid is None:
            return "idle"
        if not enabled:
            # the daemon releases these; say who has them now rather than
            # showing whatever state the last enabled poll left behind
            return "disabled — firmware holds it"
        st = (data.get("status") or {}).get("fans", {}).get(fan, {})
        state = st.get("state")
        if state:
            words = self.STATE_WORDS.get(state) or state
            kick = st.get("kick")
            if isinstance(kick, dict):
                # §21: the daemon is not only reporting on this fan, it is
                # writing to it right now — phase and attempt count belong on
                # screen, not only in the log
                words += (f" — kicking {kick.get('phase')}"
                          f" attempt {kick.get('attempts')}")
            return words
        d = data.get("fans", {}).get(fan, {})
        en = d.get("enable")
        if en == 2:
            return "firmware (not taken over)"
        if en == 1:
            # no fresh daemon state for this fan: manual mode means someone
            # is driving it — us (daemon stopped reporting) or somebody else
            if app.fan_owner(fan) == app.OWNER_MINE:
                return "manual (daemon not reporting)"
            return "manual — held by someone else"
        if en == 0:
            return "disabled (enable 0) — cools nothing"
        return "—"

    def _refresh_status_line(self, st: dict):
        # three separate widgets, because they mean three different things:
        # "the daemon is not cooling to your config" (degraded), "here is
        # every problem it reported" (errors, one per line — F10), and the
        # routine liveness line.
        deg = self.query_one("#degraded-line", Static)
        errbox = self.query_one("#errors-box", Static)
        errors = dict(st.get("errors") or {})
        if st.get("degraded"):
            deg.update("⚠ DEGRADED: the daemon is running with a config it "
                       "rejected — fans are held at fail-safe duty, NOT "
                       "following their curves")
            deg.display = True
        elif errors:
            # not degraded, but something is wrong on one path (a source that
            # will not read, one stalled fan)
            deg.update("")
            deg.display = False
        else:
            deg.update("")
            deg.display = False
        if errors:
            lines = [f"⚠ {k}: {v}" for k, v in sorted(errors.items())]
            shown, rest = lines[:4], lines[4:]
            if rest:
                shown.append(f"   … and {len(rest)} more (journalctl -u "
                             "alx_fancontrol)")
            errbox.update("\n".join(shown))
            errbox.display = True
        else:
            errbox.update("")
            errbox.display = False
        if not st:
            line = ("daemon: absent (no status.json — is it running? "
                    "start it with: alx-fancontrol daemon)")
        else:
            age = time.time() - st.get("ts", 0)
            state = "running" if age <= 3 else "stale"
            mode = "DRY-RUN" if st.get("dry_run") else "live"
            mtime = st.get("config_mtime")
            mstr = (time.strftime("%H:%M:%S", time.localtime(mtime))
                    if mtime else "—")
            line = (f"daemon: {state} (pid {st.get('pid')}, {age:.1f}s ago, "
                    f"{mode})   config mtime {mstr}")
        self.query_one("#status-line", Static).update(line)
