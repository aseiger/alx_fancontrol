"""UX tests: drive the TUI the way a USER drives it.

Unlike test_tui_smoke.py (mount + nav + plumbing), every test here
performs a real user interaction — clicking table rows, pressing
bindings, filling modal inputs, pressing Save — and asserts on the
observable result (cells, notifications, config file on disk).

The autouse `no_swallowed_exceptions` guard is the meta-test: textual
catches exceptions in event handlers and merely logs them, so a broken
handler (e.g. the RowSelected .table regression) looks "fine" to mount
tests but errors out on the user's first click. Any exception reaching
App._handle_exception fails the test.

Hermetic: every test runs against the shared synthetic machine
(`machine` fixture, tests/conftest.py + tests/fake_machine.py) — the app's
hwmon layer is monkeypatched to it. NO test assumes a value about the
real machine: readings are injected explicitly (machine.set_temp /
set_rpm / set_pwm / add_gpu) and payloads built with
machine.live_payload(). live=False everywhere; the live-data path is
exercised by posting LiveUpdate messages.
"""
from __future__ import annotations

import asyncio
import copy
import json
import time
import os
import traceback

import pytest
from textual.coordinate import Coordinate
from textual.screen import ModalScreen
from textual.widgets import DataTable, Input, Label, Select, Static, Switch

from alx_fancontrol import config as config_mod
from alx_fancontrol import hwmon
from alx_fancontrol.tui.app import ConfigErrorsScreen, FanControlApp
from alx_fancontrol.tui.screens.settings import (ConfirmSettingsScreen,
                                                SettingModal)
from alx_fancontrol.tui.reader import LiveUpdate
from alx_fancontrol.tui.screens.assign import (AssignScreen,
                                               ConfirmDeleteScreen,
                                               ConfirmScreen,
                                               TachPickerModal)
from alx_fancontrol.tui.screens.base import sel_value
from alx_fancontrol.tui.screens.curves import (CurveEditorScreen,
                                               NewCurveModal,
                                               PointPromptModal)
from alx_fancontrol.tui.screens.overview import OverviewScreen
from alx_fancontrol.tui.screens.sources import SourcesScreen


# --------------------------------------------------------------- fixtures --

@pytest.fixture(autouse=True)
def _machine(machine):
    """Every test in this file runs against the shared synthetic machine
    (tests/conftest.py). Tests inject the values they need via
    machine.set_temp/set_rpm/set_pwm/add_gpu — none assumes a value."""
    return machine


@pytest.fixture
def temp_config(machine):
    cfg = machine.root.parent / "config.json"
    config_mod.seed_if_missing(cfg)
    return cfg


# ------------------------------------------------------------- meta-test ---

@pytest.fixture(autouse=True)
def no_swallowed_exceptions():
    """Fail a test if ANY exception is swallowed by the TUI.

    textual catches exceptions raised inside event handlers (on_*
    methods, action callbacks) and logs them via App._handle_exception
    instead of propagating — a broken handler therefore looks 'fine' to
    a test that only checks the happy path (e.g. the RowSelected .table
    regression). This guard records every swallowed exception and fails
    the test at teardown.
    """
    from alx_fancontrol.tui.app import FanControlApp

    swallowed = []
    orig = FanControlApp._handle_exception

    def spy(app, exception_info):
        tb = traceback.format_exception(exception_info)
        swallowed.append("".join(tb))
        orig(app, exception_info)

    FanControlApp._handle_exception = spy
    try:
        yield
    finally:
        FanControlApp._handle_exception = orig
    if swallowed:
        pytest.fail("TUI swallowed exceptions (handlers raised):\n\n" +
                    "\n\n".join(swallowed))


@pytest.fixture
def notices():
    """Collect (severity, message) of every notification the app posts,
    so tests can assert on what the USER was told instead of parsing
    internal state."""
    from alx_fancontrol.tui.app import FanControlApp

    seen = []
    orig = FanControlApp.notify

    def spy(app, message, severity="information", **kw):
        seen.append((severity, message))
        return orig(app, message, severity=severity, **kw)

    FanControlApp.notify = spy
    try:
        yield seen
    finally:
        FanControlApp.notify = orig


# -------------------------------------------------------------- helpers ----

def row_key(table: DataTable, row: int):
    """textual 8 has no get_row_key(); coordinate_to_cell_key is public."""
    return table.coordinate_to_cell_key((row, 0))[0]


def select_options(sel: Select) -> list[tuple]:
    """(label, value) pairs. textual 8 exposes no public accessor for the
    options; _options (set by set_options) is the documented internal."""
    return list(sel._options)


def post_row_selected(pilot, table: DataTable, row: int):
    """Post exactly what a real mouse click posts."""
    table.post_message(DataTable.RowSelected(table, row, row_key(table, row)))
    return pilot.pause()


def post_row_highlighted(pilot, table: DataTable, row: int):
    table.post_message(DataTable.RowHighlighted(table, row, row_key(table, row)))
    return pilot.pause()


async def edit_assignment_row(pilot, scr, row: int = 0):
    """Click-to-edit: post the RowSelected a real click on the assignment
    list posts (returns the table so callers can assert on it)."""
    lst = scr.query_one("#assign-list", DataTable)
    await post_row_selected(pilot, lst, row)
    return lst


def read_cfg(path) -> dict:
    return json.loads(path.read_text())


async def wait_not_modal(pilot, what: str = "modal") -> None:
    """Wait until the top screen is no longer a modal (8.2.8's Pilot has
    no wait(predicate)); the point-add worker applies its result right
    after dismiss, so a few idle cycles are enough."""
    for _ in range(50):
        await pilot.pause()
        if not isinstance(pilot.app.screen, ModalScreen):
            return
    raise AssertionError(f"{what} still open after 50 idle cycles")


# ----------------------------------------------------------- assign screen --

def test_assign_lists_all_pwm_channels(temp_config, machine):
    async def run():
        async with FanControlApp(config_path=temp_config,
                                 live=False).run_test(size=(120, 40)) \
                as pilot:
            await pilot.pause()
            await pilot.press("2")
            await pilot.pause()
            fans = machine.fan_ids()
            table = pilot.app.screen.query_one("#fan-pick", DataTable)
            assert table.row_count == len(fans)
            for row, fan in enumerate(fans):
                assert table.get_cell_at((row, 1)) == fan, \
                    f"row {row} fan column"
            # labels come from the machine's sensors.d labels
            labels = {table.get_cell_at((r, 1)): table.get_cell_at((r, 2))
                      for r in range(table.row_count)}
            assert labels["it8686:pwm3"] == machine.labels["it8686"]["pwm3"]
            assert labels["it8792:pwm3"] == machine.labels["it8792"]["pwm3"]

    asyncio.run(run())


def test_click_fan_row_selects_it(machine, temp_config):
    """THE regression test: a real row click must toggle the fan, not
    raise. (Was: AttributeError on RowSelected.table -> click dead.)"""
    async def run():
        async with FanControlApp(config_path=temp_config,
                                 live=False).run_test(size=(120, 40)) \
                as pilot:
            await pilot.pause()
            await pilot.press("2")
            await pilot.pause()
            screen = pilot.app.screen
            table = screen.query_one("#fan-pick", DataTable)
            row = machine.fan_ids().index("it8686:pwm1")
            await post_row_selected(pilot, table, row)
            assert table.get_cell_at((row, 0)) == "✓"
            assert screen._picked == {"it8686:pwm1"}

    asyncio.run(run())


def test_click_same_row_again_deselects(machine, temp_config):
    async def run():
        async with FanControlApp(config_path=temp_config,
                                 live=False).run_test(size=(120, 40)) \
                as pilot:
            await pilot.pause()
            await pilot.press("2")
            await pilot.pause()
            screen = pilot.app.screen
            table = screen.query_one("#fan-pick", DataTable)
            row = machine.fan_ids().index("it8686:pwm1")
            await post_row_selected(pilot, table, row)
            await post_row_selected(pilot, table, row)
            assert table.get_cell_at((row, 0)) == ""
            assert screen._picked == set()

    asyncio.run(run())


def test_click_gpu_blower_row_selects_it(temp_config, machine):
    """it8792:pwm1 used to be hard 'protected' in the config; after the
    redesign it is an ordinary selectable row. (The daemon still refuses
    takeover of any channel another process holds in manual mode.)"""
    async def run():
        async with FanControlApp(config_path=temp_config,
                                 live=False).run_test(size=(120, 40)) \
                as pilot:
            await pilot.pause()
            await pilot.press("2")
            await pilot.pause()
            screen = pilot.app.screen
            table = screen.query_one("#fan-pick", DataTable)
            row = machine.fan_ids().index("it8792:pwm1")
            await post_row_selected(pilot, table, row)
            assert screen._picked == {"it8792:pwm1"}
            assert table.get_cell_at((row, 0)) == "✓"

    asyncio.run(run())


def test_select_multiple_fans(machine, temp_config):
    async def run():
        async with FanControlApp(config_path=temp_config,
                                 live=False).run_test(size=(120, 40)) \
                as pilot:
            await pilot.pause()
            await pilot.press("2")
            await pilot.pause()
            screen = pilot.app.screen
            table = screen.query_one("#fan-pick", DataTable)
            fans = machine.fan_ids()
            r1, r3 = fans.index("it8686:pwm1"), fans.index("it8686:pwm3")
            await post_row_selected(pilot, table, r1)
            await post_row_selected(pilot, table, r3)
            assert screen._picked == {"it8686:pwm1", "it8686:pwm3"}
            assert table.get_cell_at((r1, 0)) == "✓"
            assert table.get_cell_at((r3, 0)) == "✓"
            assert table.get_cell_at((r1 + 1, 0)) == ""

    asyncio.run(run())


def test_real_mouse_click_on_table_does_not_crash(temp_config):
    """A literal pilot mouse click (pixel path, not a posted event) must
    not raise whatever row it lands on."""
    async def run():
        async with FanControlApp(config_path=temp_config,
                                 live=False).run_test(size=(120, 40)) \
                as pilot:
            await pilot.pause()
            await pilot.press("2")
            await pilot.pause()
            table = pilot.app.screen.query_one("#fan-pick", DataTable)
            await pilot.click("#fan-pick")
            await pilot.pause()
            assert table.row_count == 8  # still alive
            assert table.cursor_row is not None

    asyncio.run(run())


def test_full_assignment_flow_via_ui(temp_config, machine, notices):
    """Golden path: name + source + curve + fan click + Save -> config."""
    async def run():
        async with FanControlApp(config_path=temp_config,
                                 live=False).run_test(size=(120, 45)) \
                as pilot:
            await pilot.pause()
            await pilot.press("2")
            await pilot.pause()
            scr = pilot.app.screen

            scr.query_one("#name", Input).value = "ui-cool"
            scr.query_one("#floor", Input).value = "30"
            src_id = machine.source_ids()[0]
            scr.query_one("#source", Select).value = src_id
            scr.query_one("#curve", Select).value = "default"

            table = scr.query_one("#fan-pick", DataTable)
            row = machine.fan_ids().index("it8686:pwm3")
            table.move_cursor(row=row)
            await post_row_selected(pilot, table, row)
            assert "it8686:pwm3" in scr._picked

            await pilot.click("#save")
            await pilot.pause()

            a = read_cfg(temp_config)["assignments"]["ui-cool"]
            assert a["source"] == src_id
            assert a["curve"] == "default"
            assert a["fans"] == ["it8686:pwm3"]
            assert a["enabled"] is True
            assert a["floor_duty"] == 30
            # tach auto-guessed from matching sensors.d labels
            assert a["tach"] == {"it8686:pwm3": "it8686:fan3"}
            # UI reset after save
            assert table.get_cell_at((2, 0)) == ""
            assert scr.query_one("#name", Input).value == ""
            assert scr._picked == set()
            assert any(sev == "information" and "saved" in msg.lower()
                       for sev, msg in notices), notices
            # .bak holds the PREVIOUS content (no assignments)
            bak = read_cfg(temp_config.with_name(
                temp_config.name + ".bak"))
            assert bak["assignments"] == {}

    asyncio.run(run())


def test_save_requires_name(temp_config, machine, notices):
    async def run():
        async with FanControlApp(config_path=temp_config,
                                 live=False).run_test(size=(120, 45)) \
                as pilot:
            await pilot.pause()
            await pilot.press("2")
            await pilot.pause()
            scr = pilot.app.screen
            scr.query_one("#source", Select).value = "k10temp:temp1"
            scr.query_one("#curve", Select).value = "default"
            table = scr.query_one("#fan-pick", DataTable)
            row = machine.fan_ids().index("it8686:pwm1")
            await post_row_selected(pilot, table, row)
            await pilot.click("#save")
            await pilot.pause()
            assert read_cfg(temp_config)["assignments"] == {}
            assert any("name" in msg.lower() for _s, msg in notices)

    asyncio.run(run())


def test_save_requires_fan(temp_config, notices):
    async def run():
        async with FanControlApp(config_path=temp_config,
                                 live=False).run_test(size=(120, 45)) \
                as pilot:
            await pilot.pause()
            await pilot.press("2")
            await pilot.pause()
            scr = pilot.app.screen
            scr.query_one("#name", Input).value = "no-fans"
            scr.query_one("#source", Select).value = "k10temp:temp1"
            scr.query_one("#curve", Select).value = "default"
            await pilot.click("#save")
            await pilot.pause()
            assert read_cfg(temp_config)["assignments"] == {}
            assert any("fan" in msg.lower() for _s, msg in notices)

    asyncio.run(run())


def test_save_rejects_floor_above_max(temp_config, machine, notices):
    async def run():
        async with FanControlApp(config_path=temp_config,
                                 live=False).run_test(size=(120, 45)) \
                as pilot:
            await pilot.pause()
            await pilot.press("2")
            await pilot.pause()
            scr = pilot.app.screen
            scr.query_one("#name", Input).value = "bad-bounds"
            scr.query_one("#source", Select).value = "k10temp:temp1"
            scr.query_one("#curve", Select).value = "default"
            scr.query_one("#floor", Input).value = "50"
            scr.query_one("#maxduty", Input).value = "20"
            table = scr.query_one("#fan-pick", DataTable)
            row = machine.fan_ids().index("it8686:pwm1")
            await post_row_selected(pilot, table, row)
            await pilot.click("#save")
            await pilot.pause()
            assert read_cfg(temp_config)["assignments"] == {}
            assert any("floor" in msg.lower() for _s, msg in notices)

    asyncio.run(run())


def test_assign_save_via_s_key(temp_config, machine, notices):
    """'s' triggers the same save path as the Save button (which can be
    below the fold on short terminals)."""
    async def run():
        async with FanControlApp(config_path=temp_config,
                                 live=False).run_test(size=(120, 40)) \
                as pilot:
            await pilot.pause()
            await pilot.press("2")
            await pilot.pause()
            scr = pilot.app.screen
            scr.query_one("#name", Input).value = "s-key-save"
            scr.query_one("#source", Select).value = "k10temp:temp1"
            scr.query_one("#curve", Select).value = "default"
            table = scr.query_one("#fan-pick", DataTable)
            table.focus()  # 's' must not land in a focused Input/Select
            row = machine.fan_ids().index("it8686:pwm5")
            await post_row_selected(pilot, table, row)
            await pilot.press("s")
            await pilot.pause()
            a = read_cfg(temp_config)["assignments"]["s-key-save"]
            assert a["fans"] == ["it8686:pwm5"]
            assert scr._picked == set()  # reset after save

    asyncio.run(run())


def test_reader_reports_rpm_for_unassigned_fans(temp_config, machine):
    """The Assign pane shows '—' unless the live reader resolves a tach.
    Before the fix, tach resolution required an existing assignment's
    'tach' mapping, so every fan read '—' on a fresh config. The reader
    must fall back to the sensors.d label match (pwmN label == fanN
    label) for unassigned fans. Readings are injected explicitly."""
    async def run():
        async with FanControlApp(config_path=temp_config,
                                 live=False).run_test(size=(120, 40)) \
                as pilot:
            await pilot.pause()
            app = pilot.app
            assert app.cfg.assignments == {}  # fresh config: nothing assigned
            # inject the readings this test asserts on
            machine.set_rpm("it8686", 1, 1650)
            machine.set_rpm("it8686", 3, 4680)
            machine.set_rpm("it8686", 4, 4890)
            machine.set_rpm("it8792", 1, 250)
            machine.set_rpm("it8792", 3, 3000)
            machine.add_gpu("08:00.0", 68.0, "Tesla V100")
            data = app._reader.snapshot()
            fans = data["fans"]
            # every pwm channel on the machine is reported
            for fan in machine.fan_ids():
                assert fan in fans, fan
            # ...with the injected live RPMs
            assert fans["it8686:pwm1"]["rpm"] == 1650
            assert fans["it8686:pwm3"]["rpm"] == 4680
            assert fans["it8686:pwm4"]["rpm"] == 4890
            assert fans["it8792:pwm1"]["rpm"] == 250
            assert fans["it8792:pwm3"]["rpm"] == 3000
            # ...and None where no matching tach file exists
            assert fans["it8686:pwm2"]["rpm"] is None
            assert fans["it8792:pwm2"]["rpm"] is None
            # GPU source temp also present (canonical bus id)
            assert data["sources"]["gpu:08:00.0"]["temp_c"] == 68

    asyncio.run(run())


def test_live_update_fills_rpm_and_source_labels(temp_config, machine):
    """The per-second LiveUpdate path must not raise and must refresh the
    rpm column + source picker labels (exercises get_cell_at/update_cell
    on every row). All readings come from machine.live_payload() with
    explicit overrides."""
    async def run():
        async with FanControlApp(config_path=temp_config,
                                 live=False).run_test(size=(120, 40)) \
                as pilot:
            await pilot.pause()
            await pilot.press("2")
            await pilot.pause()
            scr = pilot.app.screen
            data = machine.live_payload()
            data["fans"]["it8686:pwm1"]["rpm"] = 1650
            data["fans"]["it8686:pwm3"]["rpm"] = 4680
            data["fans"]["it8792:pwm1"]["rpm"] = 250
            data["fans"]["it8792:pwm3"]["rpm"] = 3000
            data["fans"]["it8686:pwm2"]["rpm"] = None  # no reading
            machine.set_temp("k10temp", 1, 55.0)
            data["sources"]["k10temp:temp1"]["temp_c"] = 55.0
            scr.post_message(LiveUpdate(data))
            await pilot.pause()
            fans = machine.fan_ids()
            table = scr.query_one("#fan-pick", DataTable)
            # by column id, not index: this table has gained columns twice
            # now and a positional assert breaks silently each time
            rpm = _col(table, "frpm")
            own = _col(table, "fown")
            assert table.get_cell_at((fans.index("it8686:pwm1"), rpm)) == "1650"
            assert table.get_cell_at((fans.index("it8686:pwm3"), rpm)) == "4680"
            assert table.get_cell_at((fans.index("it8686:pwm2"), rpm)) == "—"
            # owner column (F5): the fake machine leaves headers in firmware
            # mode, which is what a free header reads as
            assert table.get_cell_at((fans.index("it8686:pwm1"), own)) \
                == "firmware"
            opts = {v: l for l, v in select_options(
                scr.query_one("#source", Select))}
            assert "55.0 °C" in str(opts["k10temp:temp1"])

    asyncio.run(run())


# ------------------------------------------------------------ curves screen --

def test_curve_picker_lists_and_switches(temp_config):
    async def run():
        async with FanControlApp(config_path=temp_config,
                                 live=False).run_test(size=(120, 40)) \
                as pilot:
            await pilot.pause()
            await pilot.press("3")
            await pilot.pause()
            pick = pilot.app.screen.query_one("#curve-pick", Select)
            values = [v for _l, v in select_options(pick)
                      if v is not Select.NULL]
            assert values == ["default", "gpu_v100"]
            table = pilot.app.screen.query_one("#points", DataTable)
            assert table.row_count == 4  # default curve
            pick.value = "gpu_v100"
            await pilot.pause()
            assert table.row_count == 4  # gpu_v100 curve
            assert table.get_cell_at((0, 0)) == 50.0
            assert table.get_cell_at((0, 1)) == 26.0

    asyncio.run(run())


def test_row_highlight_tracks_selected_point(temp_config):
    async def run():
        async with FanControlApp(config_path=temp_config,
                                 live=False).run_test(size=(120, 40)) \
                as pilot:
            await pilot.pause()
            await pilot.press("3")
            await pilot.pause()
            screen = pilot.app.screen
            table = screen.query_one("#points", DataTable)
            await post_row_highlighted(pilot, table, 1)
            assert screen._selected() == [55.0, 30.0]  # 2nd default point

    asyncio.run(run())


def test_add_point_via_modal_flow(temp_config, notices):
    async def run():
        async with FanControlApp(config_path=temp_config,
                                 live=False).run_test(size=(120, 40)) \
                as pilot:
            await pilot.pause()
            await pilot.press("3")
            await pilot.pause()
            editor = pilot.app.screen
            n0 = len(editor.pts)
            # real keypress: 'a' is bound to add_point, which opens the
            # prompt in a worker (push_screen_wait is worker-only in
            # textual >= 8; an async action would also block this
            # screen's message pump while the modal is open)
            await pilot.press("a")
            await pilot.pause()
            assert isinstance(pilot.app.screen, PointPromptModal)
            modal = pilot.app.screen
            modal.query_one("#pp-temp", Input).value = "80"
            modal.query_one("#pp-duty", Input).value = "90"
            await pilot.click("#pp-ok")
            await wait_not_modal(pilot)
            assert isinstance(pilot.app.screen, CurveEditorScreen)
            assert [80.0, 90.0] in editor.pts
            assert len(editor.pts) == n0 + 1
            table = editor.query_one("#points", DataTable)
            assert table.row_count == n0 + 1  # table refreshed too

    asyncio.run(run())


def test_add_point_modal_cancel(temp_config):
    async def run():
        async with FanControlApp(config_path=temp_config,
                                 live=False).run_test(size=(120, 40)) \
                as pilot:
            await pilot.pause()
            await pilot.press("3")
            await pilot.pause()
            editor = pilot.app.screen
            n0 = len(editor.pts)
            await pilot.press("a")
            await pilot.pause()
            assert isinstance(pilot.app.screen, PointPromptModal)
            await pilot.click("#pp-cancel")
            await wait_not_modal(pilot)
            assert isinstance(pilot.app.screen, CurveEditorScreen)
            assert len(editor.pts) == n0

    asyncio.run(run())


def test_add_duplicate_temp_warns(temp_config, notices):
    async def run():
        async with FanControlApp(config_path=temp_config,
                                 live=False).run_test(size=(120, 40)) \
                as pilot:
            await pilot.pause()
            await pilot.press("3")
            await pilot.pause()
            editor = pilot.app.screen
            n0 = len(editor.pts)
            await pilot.press("a")
            await pilot.pause()
            assert isinstance(pilot.app.screen, PointPromptModal)
            modal = pilot.app.screen
            modal.query_one("#pp-temp", Input).value = "40"  # exists
            modal.query_one("#pp-duty", Input).value = "55"
            await pilot.click("#pp-ok")
            await wait_not_modal(pilot)
            assert len(editor.pts) == n0
            assert any(sev == "warning" and "already exists" in msg
                       for sev, msg in notices), notices

    asyncio.run(run())


def test_delete_selected_point(temp_config):
    async def run():
        async with FanControlApp(config_path=temp_config,
                                 live=False).run_test(size=(120, 40)) \
                as pilot:
            await pilot.pause()
            await pilot.press("3")
            await pilot.pause()
            screen = pilot.app.screen
            n0 = len(screen.pts)
            table = screen.query_one("#points", DataTable)
            await post_row_highlighted(pilot, table, 0)
            await pilot.press("d")
            await pilot.pause()
            assert len(screen.pts) == n0 - 1
            assert [40.0, 20.0] not in screen.pts

    asyncio.run(run())


def test_nudge_duty_via_keyboard(temp_config):
    async def run():
        async with FanControlApp(config_path=temp_config,
                                 live=False).run_test(size=(120, 40)) \
                as pilot:
            await pilot.pause()
            await pilot.press("3")
            await pilot.pause()
            screen = pilot.app.screen
            table = screen.query_one("#points", DataTable)
            await post_row_highlighted(pilot, table, 0)  # [40, 20]
            await pilot.press("shift+up")                # duty +5
            await pilot.pause()
            assert screen._selected() == [40.0, 25.0]
            await pilot.press("shift+down")              # duty -5
            await pilot.pause()
            assert screen._selected() == [40.0, 20.0]

    asyncio.run(run())


def test_nudge_temp_via_keyboard(temp_config):
    async def run():
        async with FanControlApp(config_path=temp_config,
                                 live=False).run_test(size=(120, 40)) \
                as pilot:
            await pilot.pause()
            await pilot.press("3")
            await pilot.pause()
            screen = pilot.app.screen
            table = screen.query_one("#points", DataTable)
            await post_row_highlighted(pilot, table, 0)  # [40, 20]
            await pilot.press("shift+right")             # temp +1
            await pilot.pause()
            assert screen._selected() == [41.0, 20.0]

    asyncio.run(run())


def test_save_curve_persists_points(temp_config, notices):
    """Real mouse click on the Save curve button. Needs a 45-row terminal:
    the plot is 16 rows, so on shorter terminals the button is below the
    fold (keyboard path 's' is covered in the next test)."""
    async def run():
        async with FanControlApp(config_path=temp_config,
                                 live=False).run_test(size=(120, 45)) \
                as pilot:
            await pilot.pause()
            await pilot.press("3")
            await pilot.pause()
            screen = pilot.app.screen
            table = screen.query_one("#points", DataTable)
            await post_row_highlighted(pilot, table, 0)
            await pilot.press("shift+up")  # [40,20] -> [40,25]
            await pilot.pause()
            await pilot.click("#save-curve")
            await pilot.pause()
            pts = read_cfg(temp_config)["curves"]["default"]["points"]
            assert pts[0] == [40.0, 25.0]
            bak = read_cfg(temp_config.with_name(
                temp_config.name + ".bak"))["curves"]["default"]["points"]
            assert bak[0] == [40.0, 20.0]
            assert any(sev == "information" for sev, _m in notices)

    asyncio.run(run())


def test_save_curve_rejects_duplicate_temps(temp_config, notices):
    async def run():
        async with FanControlApp(config_path=temp_config,
                                 live=False).run_test(size=(120, 40)) \
                as pilot:
            await pilot.pause()
            await pilot.press("3")
            await pilot.pause()
            screen = pilot.app.screen
            before = read_cfg(temp_config)
            screen.pts = [[50.0, 50.0], [50.0, 60.0]]
            screen._sel_point = None
            screen._refresh()
            table = screen.query_one("#points", DataTable)
            table.focus()  # 's' must not land in a focused Input/Select
            # 's' saves via the key binding (works even when the button
            # is below the fold on short terminals)
            await pilot.press("s")
            await pilot.pause()
            assert read_cfg(temp_config) == before  # untouched
            assert any(sev == "error" and "increasing" in msg
                       for sev, msg in notices), notices

    asyncio.run(run())


# ------------------------------------------------------------ sources screen --

def test_sources_screen_keeps_cursor_across_live_updates(temp_config, machine):
    """THE cursor-regression test: LiveUpdate arrives every second and
    rebuilds the table; clear() silently drops BOTH the cursor and the
    mouse-hover highlight back to row 0, so the user's clicked row must
    be restored by key across every rebuild — not snap to the top."""
    async def run():
        async with FanControlApp(config_path=temp_config,
                                 live=False).run_test(size=(120, 40)) \
                as pilot:
            await pilot.pause()
            await pilot.press("4")
            await pilot.pause()
            table = pilot.app.screen.query_one("#srcs", DataTable)
            last = table.row_count - 1
            table.move_cursor(row=last)          # the user clicks a lower row
            table.hover_coordinate = Coordinate(last, 0)  # ...mouse over it
            key = table.coordinate_to_cell_key((last, 0))[0]
            machine.set_temp("k10temp", 1, 50.0)
            for _ in range(3):                   # several live ticks
                pilot.app.screen.post_message(LiveUpdate(machine.live_payload()))
                await pilot.pause()
            assert table.cursor_row == last
            assert table.coordinate_to_cell_key((table.cursor_row, 0))[0] == key
            assert table.hover_row == last  # hover highlight didn't jump back

    asyncio.run(run())


def test_overview_keeps_cursor_across_live_updates(temp_config, machine):
    async def run():
        async with FanControlApp(config_path=temp_config,
                                 live=False).run_test(size=(120, 40)) \
                as pilot:
            await pilot.pause()
            table = pilot.app.screen.query_one("#src-table", DataTable)
            assert table.row_count >= 3
            last = table.row_count - 1
            table.move_cursor(row=last)
            key = table.coordinate_to_cell_key((last, 0))[0]
            machine.set_rpm("it8686", 1, 1650)
            for _ in range(3):
                pilot.app.screen.post_message(LiveUpdate(machine.live_payload()))
                await pilot.pause()
            assert table.coordinate_to_cell_key((table.cursor_row, 0))[0] == key

    asyncio.run(run())


def test_assign_keeps_clicked_row_across_live_updates(temp_config, machine):
    async def run():
        async with FanControlApp(config_path=temp_config,
                                 live=False).run_test(size=(120, 40)) \
                as pilot:
            await pilot.pause()
            await pilot.press("2")
            await pilot.pause()
            screen = pilot.app.screen
            table = screen.query_one("#fan-pick", DataTable)
            row = machine.fan_ids().index("it8686:pwm3")
            table.move_cursor(row=row)   # a real click moves the cursor AND
            await post_row_selected(pilot, table, row)   # posts the selection
            machine.set_rpm("it8686", 3, 4680)
            for _ in range(3):
                screen.post_message(LiveUpdate(machine.live_payload()))
                await pilot.pause()
            # selection survived and the cursor stayed on the clicked row
            assert "it8686:pwm3" in screen._picked
            assert table.get_cell_at((row, 0)) == "✓"
            assert table.coordinate_to_cell_key((table.cursor_row, 0))[0] \
                == "it8686:pwm3"

    asyncio.run(run())


def test_sources_screen_lists_detected_live(temp_config, machine):
    """Sources are runtime discovery (not config): the screen lists what
    the machine has, with live temps from LiveUpdate — nothing to toggle."""
    async def run():
        async with FanControlApp(config_path=temp_config,
                                 live=False).run_test(size=(120, 40)) \
                as pilot:
            await pilot.pause()
            await pilot.press("4")
            await pilot.pause()
            screen = pilot.app.screen
            table = screen.query_one("#srcs", DataTable)
            ids = {table.get_cell_at((r, 0))
                   for r in range(table.row_count)}
            assert ids == set(machine.source_ids())
            machine.set_temp("k10temp", 1, 50.0)
            machine.add_gpu("08:00.0", 68.0, "Tesla V100")
            screen.post_message(LiveUpdate(machine.live_payload()))
            await pilot.pause()
            row = {table.get_cell_at((r, 0)): r
                   for r in range(table.row_count)}
            assert table.get_cell_at((row["k10temp:temp1"], 4)) == "50.0 °C"
            gpu_row = row["gpu:08:00.0"]
            assert "Tesla V100" in str(table.get_cell_at((gpu_row, 1)))

    asyncio.run(run())


# ------------------------------------------------------------------ app ----

def test_live_update_reaches_every_screen(temp_config, machine):
    """Post a LiveUpdate while on each screen; none may raise (the
    overview/curves/sources handlers all differ in what they touch)."""
    async def run():
        async with FanControlApp(config_path=temp_config,
                                 live=False).run_test(size=(120, 40)) \
                as pilot:
            await pilot.pause()
            machine.set_temp("k10temp", 1, 51.0)
            machine.set_rpm("it8686", 1, 1650)
            machine.set_rpm("it8792", 1, 250)
            machine.set_rpm("it8792", 3, 3000)
            data = machine.live_payload()
            for key, cls in (("1", OverviewScreen), ("2", AssignScreen),
                             ("3", CurveEditorScreen),
                             ("4", SourcesScreen)):
                await pilot.press(key)
                await pilot.pause()
                assert isinstance(pilot.app.screen, cls)
                pilot.app.screen.post_message(LiveUpdate(copy.deepcopy(data)))
                await pilot.pause()

    asyncio.run(run())


# ------------------------------------------------------------------- nav ----

def _active_nav(app) -> str:
    """Which sidebar button carries the `active` class right now."""
    for nav in ("nav-1", "nav-2", "nav-3", "nav-4"):
        btn = app.screen.query_one(f"#{nav}")
        if btn.has_class("active"):
            return nav
    return ""


def test_nav_active_tracks_current_screen(temp_config, machine):
    """The sidebar must show which screen you're ON. Before the fix the
    four nav buttons were re-composed on every switch with no active
    state, and the (focus) highlight snapped to the topmost button."""
    async def run():
        async with FanControlApp(config_path=temp_config,
                                 live=False).run_test(size=(120, 40)) \
                as pilot:
            await pilot.pause()
            app = pilot.app
            assert _active_nav(app) == "nav-1"          # Overview by default
            await pilot.press("2")
            await pilot.pause()
            assert _active_nav(app) == "nav-2"          # Assign
            await pilot.press("3")
            await pilot.pause()
            assert _active_nav(app) == "nav-3"          # Curves
            await pilot.press("4")
            await pilot.pause()
            assert _active_nav(app) == "nav-4"          # Sources
            # ...and only ONE is ever active
            for nav in ("nav-1", "nav-2", "nav-3", "nav-4"):
                assert app.screen.query_one(f"#{nav}").has_class("active") \
                    == (nav == "nav-4")

    asyncio.run(run())


def test_nav_buttons_are_not_focusable(temp_config, machine):
    """Nav items are NavButton(can_focus=False): they must never hold
    keyboard focus, so a screen switch can't leave a focus highlight on
    the topmost button. Focus should end up in the content (or nowhere),
    never on a nav item."""
    async def run():
        async with FanControlApp(config_path=temp_config,
                                 live=False).run_test(size=(120, 40)) \
                as pilot:
            await pilot.pause()
            app = pilot.app
            for nav in ("nav-1", "nav-2", "nav-3", "nav-4"):
                assert app.screen.query_one(f"#{nav}").can_focus is False
            for key in ("1", "2", "3", "4"):
                await pilot.press(key)
                await pilot.pause()
                focused = app.focused
                assert focused is None or not focused.id or \
                    not str(focused.id).startswith("nav-"), \
                    f"focus stuck on nav after pressing {key}: {focused!r}"

    asyncio.run(run())


def test_nav_click_switches_and_stays_active(temp_config, machine):
    """Clicking a nav button (real mouse path) switches screens AND the
    clicked item becomes/keeps the active one."""
    async def run():
        async with FanControlApp(config_path=temp_config,
                                 live=False).run_test(size=(120, 40)) \
                as pilot:
            await pilot.pause()
            app = pilot.app
            await pilot.click("#nav-3")
            await pilot.pause()
            assert isinstance(app.screen, CurveEditorScreen)
            assert _active_nav(app) == "nav-3"
            await pilot.click("#nav-2")
            await pilot.pause()
            assert isinstance(app.screen, AssignScreen)
            assert _active_nav(app) == "nav-2"

    asyncio.run(run())


# ------------------------------------------------------ assignment list ----

def _seed_assignments(cfg_path, **names):
    """Write assignments into the config file BEFORE the app starts."""
    from alx_fancontrol import config as _cfg_mod
    cfg = _cfg_mod.load(cfg_path)
    for name, a in names.items():
        cfg.assignments[name] = a
    _cfg_mod.save(cfg, cfg_path)
    return cfg


async def wait_modal(pilot, cls):
    for _ in range(50):
        await pilot.pause()
        if isinstance(pilot.app.screen, cls):
            return pilot.app.screen
    raise AssertionError(f"{cls.__name__} did not open")


def test_assign_lists_existing_assignments(temp_config, machine):
    """Existing assignments are listed (name, source, curve, fans,
    floor, max, on/off) — no need to remember names to edit them."""
    _seed_assignments(temp_config,
                      CPU={"source": "k10temp:temp1", "curve": "default",
                           "fans": ["it8686:pwm1"], "enabled": True},
                      System={"source": "it8686:temp1", "curve": "default",
                              "fans": ["it8686:pwm3", "it8686:pwm4"],
                              "enabled": False, "floor_duty": 20,
                              "max_rate": 12})
    async def run():
        async with FanControlApp(config_path=temp_config,
                                 live=False).run_test(size=(120, 45)) \
                as pilot:
            await pilot.pause()
            await pilot.press("2")
            await pilot.pause()
            table = pilot.app.screen.query_one("#assign-list", DataTable)
            # columns: name source curve fans floor max rate on
            assert table.row_count == 2
            assert table.get_cell_at((0, 0)) == "CPU"
            assert table.get_cell_at((0, 3)) == "it8686:pwm1"
            assert table.get_cell_at((0, 6)) == "—"   # rate unset
            assert table.get_cell_at((0, 7)) == "on"
            assert table.get_cell_at((1, 0)) == "System"
            assert table.get_cell_at((1, 3)) == "it8686:pwm3, it8686:pwm4"
            assert table.get_cell_at((1, 4)) == "20"
            assert table.get_cell_at((1, 6)) == "12"  # per-fan rate
            assert table.get_cell_at((1, 7)) == "off"

    asyncio.run(run())


def test_click_assignment_row_loads_form(temp_config, machine):
    """Click-to-edit: clicking a list row fills the form below (name,
    source, curve, fans, floor/max) and switches the title to Editing."""
    _seed_assignments(temp_config,
                      CPU={"source": "k10temp:temp1", "curve": "default",
                           "fans": ["it8686:pwm1", "it8686:pwm3"],
                           "enabled": True, "floor_duty": 25,
                           "max_rate": 8})
    async def run():
        async with FanControlApp(config_path=temp_config,
                                 live=False).run_test(size=(120, 45)) \
                as pilot:
            await pilot.pause()
            await pilot.press("2")
            await pilot.pause()
            scr = pilot.app.screen
            lst = scr.query_one("#assign-list", DataTable)
            await post_row_selected(pilot, lst, 0)
            # form is filled from the assignment
            assert scr.query_one("#name", Input).value == "CPU"
            assert sel_value(scr.query_one("#source", Select)) \
                == "k10temp:temp1"
            assert sel_value(scr.query_one("#curve", Select)) == "default"
            assert scr.query_one("#floor", Input).value == "25"
            assert scr.query_one("#rate", Input).value == "8"
            assert scr._picked == {"it8686:pwm1", "it8686:pwm3"}
            fans = machine.fan_ids()
            pick = scr.query_one("#fan-pick", DataTable)
            assert pick.get_cell_at((fans.index("it8686:pwm1"), 0)) == "✓"
            assert pick.get_cell_at((fans.index("it8686:pwm3"), 0)) == "✓"
            assert pick.get_cell_at((fans.index("it8686:pwm4"), 0)) == ""
            # ...and the form title says it's an edit
            title = str(scr.query_one("#form-title", Static).render())
            assert "CPU" in title and "Editing" in title

    asyncio.run(run())


def test_edit_save_overwrites_and_preserves_enabled(temp_config, machine):
    """Saving an edited assignment overwrites it in place — and keeps its
    enabled state (the form has no enabled toggle; flipping it silently
    would surprise the daemon)."""
    _seed_assignments(temp_config,
                      CPU={"source": "k10temp:temp1", "curve": "default",
                           "fans": ["it8686:pwm1"], "enabled": False,
                           "floor_duty": 25})
    async def run():
        async with FanControlApp(config_path=temp_config,
                                 live=False).run_test(size=(120, 45)) \
                as pilot:
            await pilot.pause()
            await pilot.press("2")
            await pilot.pause()
            scr = pilot.app.screen
            lst = scr.query_one("#assign-list", DataTable)
            await post_row_selected(pilot, lst, 0)
            scr.query_one("#rate", Input).value = "15"
            await pilot.click("#save")
            await pilot.pause()
            saved = read_cfg(temp_config)["assignments"]
            assert set(saved) == {"CPU"}          # overwritten, not duplicated
            assert saved["CPU"]["enabled"] is False
            assert saved["CPU"]["floor_duty"] == 25
            assert saved["CPU"]["max_rate"] == 15
            assert saved["CPU"]["fans"] == ["it8686:pwm1"]
            # form reset to 'new' after the save
            assert scr.query_one("#name", Input).value == ""
            assert "Editing" not in str(
                scr.query_one("#form-title", Static).render())

    asyncio.run(run())


def test_new_button_resets_form(temp_config, machine):
    _seed_assignments(temp_config,
                      CPU={"source": "k10temp:temp1", "curve": "default",
                           "fans": ["it8686:pwm1"], "enabled": True,
                           "floor_duty": 25})
    async def run():
        async with FanControlApp(config_path=temp_config,
                                 live=False).run_test(size=(120, 45)) \
                as pilot:
            await pilot.pause()
            await pilot.press("2")
            await pilot.pause()
            scr = pilot.app.screen
            lst = scr.query_one("#assign-list", DataTable)
            await post_row_selected(pilot, lst, 0)
            assert scr.query_one("#name", Input).value == "CPU"
            await pilot.click("#new")
            await pilot.pause()
            assert scr.query_one("#name", Input).value == ""
            assert scr.query_one("#floor", Input).value == ""
            assert scr.query_one("#rate", Input).value == ""
            assert scr._picked == set()
            assert str(scr.query_one("#form-title", Static).render()) \
                == "New assignment"

    asyncio.run(run())


def test_delete_assignment_via_key(temp_config, machine):
    """'d' on a list row opens the confirm modal; Delete removes the
    assignment from the config and the list (and resets the form when
    the deleted one was being edited)."""
    _seed_assignments(temp_config,
                      CPU={"source": "k10temp:temp1", "curve": "default",
                           "fans": ["it8686:pwm1"], "enabled": True},
                      System={"source": "it8686:temp1", "curve": "default",
                              "fans": ["it8686:pwm3"], "enabled": True})
    async def run():
        async with FanControlApp(config_path=temp_config,
                                 live=False).run_test(size=(120, 45)) \
                as pilot:
            await pilot.pause()
            await pilot.press("2")
            await pilot.pause()
            scr = pilot.app.screen
            lst = scr.query_one("#assign-list", DataTable)
            await post_row_selected(pilot, lst, 0)   # click 'CPU' (loads it)
            await pilot.pause()
            await pilot.press("d")
            modal = await wait_modal(pilot, ConfirmDeleteScreen)
            assert modal.assignment_name == "CPU"
            await pilot.click("#confirm-del")
            await pilot.pause()
            await wait_not_modal(pilot)
            saved = read_cfg(temp_config)["assignments"]
            assert set(saved) == {"System"}
            assert scr.query_one("#assign-list", DataTable).row_count == 1
            # the deleted assignment was being edited -> form reset
            assert scr.query_one("#name", Input).value == ""

    asyncio.run(run())


def test_delete_cancel_keeps_assignment(temp_config, machine, notices):
    _seed_assignments(temp_config,
                      CPU={"source": "k10temp:temp1", "curve": "default",
                           "fans": ["it8686:pwm1"], "enabled": True})
    async def run():
        async with FanControlApp(config_path=temp_config,
                                 live=False).run_test(size=(120, 45)) \
                as pilot:
            await pilot.pause()
            await pilot.press("2")
            await pilot.pause()
            scr = pilot.app.screen
            lst = scr.query_one("#assign-list", DataTable)
            lst.move_cursor(row=0)
            await pilot.press("d")
            await wait_modal(pilot, ConfirmDeleteScreen)
            await pilot.click("#confirm-cancel")
            await pilot.pause()
            await wait_not_modal(pilot)
            assert set(read_cfg(temp_config)["assignments"]) == {"CPU"}
            assert lst.row_count == 1

    asyncio.run(run())


def test_delete_without_selection_warns(temp_config, machine, notices):
    async def run():
        async with FanControlApp(config_path=temp_config,
                                 live=False).run_test(size=(120, 45)) \
                as pilot:
            await pilot.pause()
            await pilot.press("2")
            await pilot.pause()
            await pilot.press("d")
            await pilot.pause()
            assert read_cfg(temp_config)["assignments"] == {}
            assert any("selected" in msg.lower() for _s, msg in notices), \
                notices

    asyncio.run(run())


def test_nav_click_from_screen_with_overrides_works(temp_config, machine):
    """Regression: Assign/Curves override on_button_pressed; textual 8
    ALSO dispatches BaseScreen's handler for the same message (MRO
    walk), so the sidebar nav must work from those screens — and must
    NOT double-switch (no super() call)."""
    async def run():
        async with FanControlApp(config_path=temp_config,
                                 live=False).run_test(size=(120, 45)) \
                as pilot:
            await pilot.pause()
            await pilot.press("2")
            await pilot.pause()
            assert isinstance(pilot.app.screen, AssignScreen)
            await pilot.click("#nav-1")
            await pilot.pause()
            assert isinstance(pilot.app.screen, OverviewScreen)
            await pilot.press("3")
            await pilot.pause()
            assert isinstance(pilot.app.screen, CurveEditorScreen)
            await pilot.click("#nav-4")
            await pilot.pause()
            assert isinstance(pilot.app.screen, SourcesScreen)
            assert _active_nav(pilot.app) == "nav-4"

    asyncio.run(run())


# ------------------------------------------------------------- geometry ----

def test_assign_widgets_are_not_squashed(temp_config, machine):
    """Regression: textual's container defaults are Horizontal/Vertical
    { height: 1fr } — without the explicit `height: auto` overrides the
    1fr boxes grab the pane's space and squash the source/curve Selects
    into clipped one-row strips (an empty box: looked like there was no
    way to change the source) and push the maxduty input off-screen
    (Input defaults to width: 100%)."""
    _seed_assignments(temp_config,
                      CPU={"source": "k10temp:temp1", "curve": "default",
                           "fans": ["it8686:pwm1"], "enabled": True})
    async def run():
        async with FanControlApp(config_path=temp_config,
                                 live=False).run_test(size=(120, 45)) \
                as pilot:
            await pilot.pause()
            await pilot.press("2")
            await pilot.pause()
            scr = pilot.app.screen
            W, H = 120, 45
            # the list table must show every row (auto = header + rows)
            lst = scr.query_one("#assign-list")
            assert lst.region.height == 1 + lst.row_count
            for wid in ("#source", "#curve", "#name", "#floor", "#maxduty",
                        "#rate", "#save", "#fan-pick"):
                r = scr.query_one(wid).region
                assert r.height >= 3, f"{wid} squashed to {r.height} rows"
                assert r.x >= 0 and r.x + r.width <= W, \
                    f"{wid} off-screen (x={r.x} w={r.width})"
                assert r.y >= 0 and r.y + r.height <= H, \
                    f"{wid} below the fold (y={r.y} h={r.height})"

    asyncio.run(run())


def test_curves_widgets_are_not_squashed(temp_config, machine):
    """Same trap on Curves: the plot must stay PLOT_H tall (it renders
    16 rows of text — stretching the widget only adds blank space and
    pushes the preview inputs + Save below short terminals), and the
    two preview inputs must share the row (Input defaults to 100%)."""
    async def run():
        async with FanControlApp(config_path=temp_config,
                                 live=False).run_test(size=(120, 45)) \
                as pilot:
            await pilot.pause()
            await pilot.press("3")
            await pilot.pause()
            scr = pilot.app.screen
            W, H = 120, 45
            plot = scr.query_one("#plot")
            assert plot.region.height == 17  # 16 plot rows + axis
            for wid in ("#save-curve", "#curve-pick", "#preview-src"):
                r = scr.query_one(wid).region
                assert r.height >= 3, f"{wid} squashed to {r.height} rows"
                assert r.x >= 0 and r.x + r.width <= W, \
                    f"{wid} off-screen (x={r.x} w={r.width})"
                assert r.y >= 0 and r.y + r.height <= H, \
                    f"{wid} below the fold (y={r.y} h={r.height})"

    asyncio.run(run())


# ------------------------------------------------- source dropdown scroll --

def test_open_source_dropdown_keeps_scroll_position(temp_config, machine):
    """THE dropdown-jump regression: while the user scrolls the OPEN
    source dropdown, live ticks must not yank the view back to the
    selected item. set_options() (and re-asserting the value) resets
    the overlay's highlight — so on temp-only changes the open list
    must be left alone."""
    from textual.widgets._select import SelectOverlay
    async def run():
        async with FanControlApp(config_path=temp_config,
                                 live=False).run_test(size=(120, 45)) \
                as pilot:
            await pilot.pause()
            await pilot.press("2")
            await pilot.pause()
            scr = pilot.app.screen
            src = scr.query_one("#source", Select)
            ov = scr.query_one(SelectOverlay)
            chosen = sel_value(src)
            # open it the way the user does, then scroll away
            await pilot.click("#source")
            await pilot.pause()
            assert src.expanded
            target = len(ov.options) - 1
            ov.highlighted = target
            ov.scroll_to_highlight()
            looking = ov.options[target].prompt
            # ...and let several live ticks arrive with changing temps
            for _ in range(3):
                machine.set_temp("k10temp", 1, 66.6)
                scr.post_message(LiveUpdate(machine.live_payload()))
                await pilot.pause()
            assert ov.highlighted == target, "scroll position was yanked"
            assert ov.options[target].prompt == looking  # same option
            assert sel_value(src) == chosen  # no accidental commit
            assert src.expanded  # dropdown still open

    asyncio.run(run())


def test_open_source_dropdown_survives_source_set_change(temp_config, machine):
    """The one case where the open list MUST be rebuilt (a source
    disappears) — even then the user stays looking at the SAME option
    (tracked by value), not snapped to the selection."""
    from alx_fancontrol import sources as sources_mod
    from textual.widgets._select import SelectOverlay
    async def run():
        async with FanControlApp(config_path=temp_config,
                                 live=False).run_test(size=(120, 45)) \
                as pilot:
            await pilot.pause()
            await pilot.press("2")
            await pilot.pause()
            scr = pilot.app.screen
            app = pilot.app
            src = scr.query_one("#source", Select)
            ov = scr.query_one(SelectOverlay)
            await pilot.click("#source")
            await pilot.pause()
            assert src.expanded
            keep_value = "it8686:temp1"
            values = [s.id for s in sorted(app.sources, key=lambda x: x.id)]
            ov.highlighted = values.index(keep_value)
            ov.scroll_to_highlight()
            # a source vanishes while the dropdown is open
            app.sources = [s for s in app.sources
                           if s.id != "k10temp:temp1"]
            scr.post_message(LiveUpdate(machine.live_payload()))
            await pilot.pause()
            new_values = [s.id for s in sorted(app.sources,
                                               key=lambda x: x.id)]
            assert "k10temp:temp1" not in new_values
            assert ov.highlighted == new_values.index(keep_value), \
                "user was moved away from the option they were viewing"
            assert src.expanded

    asyncio.run(run())


# ----------------------------------------------------- curve editor UX ----

def test_plot_shows_point_markers(temp_config, machine):
    """Every point gets a ● marker at its mapped grid position and the
    selected point a ◆ — the click/drag targets must be visible, not
    hidden inside the █ fill."""
    from alx_fancontrol.tui.screens.curves import (PLOT_H, PLOT_W,
                                                   render_curve_plot)
    pts = [[40, 20], [55, 50]]
    # expected grid positions per the plot's own mapping
    # (t-range = [min-5, max+5]; col/row formulas mirror point_to_grid)
    t_lo, t_hi = 35.0, 60.0

    def cell(t, d):
        col = int(round((t - t_lo) / (t_hi - t_lo) * (PLOT_W - 1)))
        row = PLOT_H - 1 - int(round(max(0.0, min(100.0, d)) / 100
                                     * (PLOT_H - 1)))
        return col, row

    txt = render_curve_plot(pts, sel_point=[55, 50])
    lines = txt.plain.split("\n")
    assert txt.plain.count("●") == 1
    assert txt.plain.count("◆") == 1
    c, r = cell(40, 20)
    assert lines[r][c] == "●"
    c, r = cell(55, 50)
    assert lines[r][c] == "◆"
    # no selection → plain markers for all points
    txt = render_curve_plot(pts)
    assert txt.plain.count("●") == 2
    assert "◆" not in txt.plain


def test_plot_click_does_not_add_point(temp_config, machine):
    """Clicking empty plot area must NOT add a point anymore — adding is
    table-only ('a'). Dragging (click ON a point) is unaffected."""
    from alx_fancontrol.tui.screens.curves import CurvePlot
    async def run():
        async with FanControlApp(config_path=temp_config,
                                 live=False).run_test(size=(120, 45)) \
                as pilot:
            await pilot.pause()
            await pilot.press("3")
            await pilot.pause()
            editor = pilot.app.screen
            n0 = len(editor.pts)
            # far from any point (default curve: markers near cols
            # 6/25/41/51) — an empty area
            await pilot.click("#plot", offset=(2, 8))
            await pilot.pause()
            assert len(editor.pts) == n0, "plot click added a point"
            # ...but a mousedown ON a point still starts a drag (the
            # marker is what makes the target discoverable): mouse down
            # on the first marker, move, release — the point must move.
            from types import SimpleNamespace
            plot = editor.query_one("#plot", CurvePlot)
            # capture the mapping BEFORE the drag — the axes re-scale
            # once the point has moved
            expected = list(editor.grid_to_point(10, 10))
            plot.on_mouse_down(SimpleNamespace(button=1, x=6, y=12,
                                               stop=lambda: None))
            assert plot._dragging == 0  # drag started on the marker
            plot.on_mouse_move(SimpleNamespace(x=10, y=10,
                                               stop=lambda: None))
            plot.on_mouse_up(SimpleNamespace(button=1, x=10, y=10))
            await pilot.pause()
            first = editor._sorted_pts()[0]
            assert first == expected, "drag did not move the point: %r" % first

    asyncio.run(run())


def test_add_point_prefills_midpoint(temp_config, machine):
    """'a' on the selected table row pre-fills the prompt with the
    MIDPOINT of the selected and the next point (default curve: row 1
    is (40,20), next is (55,30) → 47.5 / 25)."""
    async def run():
        async with FanControlApp(config_path=temp_config,
                                 live=False).run_test(size=(120, 45)) \
                as pilot:
            await pilot.pause()
            await pilot.press("3")
            await pilot.pause()
            await pilot.press("a")
            await pilot.pause()
            modal = pilot.app.screen
            assert isinstance(modal, PointPromptModal)
            assert modal.query_one("#pp-temp", Input).value == "47.5"
            assert modal.query_one("#pp-duty", Input).value == "25"
            await pilot.click("#pp-cancel")
            await wait_not_modal(pilot)

    asyncio.run(run())


def test_add_point_at_end_tacks_on(temp_config, machine):
    """'a' with the LAST row selected pre-fills last + 5 °C and the
    last duty (default curve: last is (75,100) → 80 / 100)."""
    async def run():
        async with FanControlApp(config_path=temp_config,
                                 live=False).run_test(size=(120, 45)) \
                as pilot:
            await pilot.pause()
            await pilot.press("3")
            await pilot.pause()
            editor = pilot.app.screen
            table = editor.query_one("#points", DataTable)
            last = table.row_count - 1
            table.move_cursor(row=last)
            await post_row_highlighted(pilot, table, last)
            await pilot.press("a")
            await pilot.pause()
            modal = pilot.app.screen
            assert isinstance(modal, PointPromptModal)
            assert modal.query_one("#pp-temp", Input).value == "80"
            assert modal.query_one("#pp-duty", Input).value == "100"
            await pilot.click("#pp-cancel")
            await wait_not_modal(pilot)

    asyncio.run(run())


def test_add_point_on_an_empty_curve_asks_for_the_first_point(temp_config,
                                                              machine):
    """'a' on an EMPTY curve opens the prompt instead of refusing. Deleting
    every point of a curve used to be a dead end: 'a' demanded a selected
    row and there was no way to create a curve, so the only repair path was
    editing JSON."""
    cfg = config_mod.load(temp_config)
    cfg.curves["empty"] = {"label": "Empty", "points": []}
    config_mod.save(cfg, temp_config)

    async def run():
        async with FanControlApp(config_path=temp_config,
                                 live=False).run_test(size=(120, 45)) \
                as pilot:
            await pilot.pause()
            await pilot.press("3")
            await pilot.pause()
            editor = pilot.app.screen
            editor.query_one("#curve-pick", Select).value = "empty"
            await pilot.pause()
            assert editor.pts == []
            await pilot.press("a")
            modal = await wait_modal(pilot, PointPromptModal)
            assert modal.defaults == (40.0, 20)
            await pilot.click("#pp-ok")
            await wait_not_modal(pilot)
            assert editor.pts == [[40.0, 20]], editor.pts
            # and the curve is recoverable all the way to a valid save
            editor.save_curve()
            await pilot.pause()
            assert read_cfg(temp_config)["curves"]["empty"]["points"] == \
                [[40.0, 20]]

    asyncio.run(run())


def test_delete_every_point_then_rebuild_the_curve(temp_config, machine):
    """The F7 dead end, end to end: delete every point of a live curve and
    it is still recoverable in the GUI — 'a' offers the first point and the
    curve saves again. (Deleting a point from a NON-empty curve re-selects a
    row, so this is the only state with nothing to insert between.)"""

    async def run():
        async with FanControlApp(config_path=temp_config,
                                 live=False).run_test(size=(120, 45)) \
                as pilot:
            await pilot.pause()
            editor = await _goto_curves(pilot)
            while editor.pts:
                editor.action_delete_point()
                await pilot.pause()
            assert editor.pts == [] and editor._sel_point is None
            await pilot.press("a")
            await wait_modal(pilot, PointPromptModal)
            await pilot.click("#pp-ok")
            await wait_not_modal(pilot)
            editor.save_curve()
            await pilot.pause()
            assert read_cfg(temp_config)["curves"]["default"]["points"] == \
                [[40.0, 20]]

    asyncio.run(run())


# ------------------------------------------------------------ curve CRUD --

def _goto_curves(pilot):
    async def _go():
        await pilot.press("3")
        await pilot.pause()
        return pilot.app.screen
    return _go()


def _goto_assign(pilot):
    async def _go():
        await pilot.press("2")
        await pilot.pause()
        return pilot.app.screen
    return _go()


def test_new_curve_creates_selects_and_persists_it(temp_config, machine):
    """`n` / New: a curve that did not exist before — the id is asked for
    (assignments reference it), and it lands in the config immediately."""

    async def run():
        async with FanControlApp(config_path=temp_config,
                                 live=False).run_test(size=(120, 45)) \
                as pilot:
            await pilot.pause()
            scr = await _goto_curves(pilot)
            await pilot.click("#new-curve")
            modal = await wait_modal(pilot, NewCurveModal)
            modal.query_one("#nc-id", Input).value = "case-fans"
            modal.query_one("#nc-label", Input).value = "Case fans"
            await pilot.click("#nc-ok")
            await wait_not_modal(pilot)
            doc = read_cfg(temp_config)
            assert "case-fans" in doc["curves"]
            assert doc["curves"]["case-fans"]["label"] == "Case fans"
            # seeded from the built-in default shape, so it is valid at once
            assert doc["curves"]["case-fans"]["points"] == \
                config_mod.DEFAULTS["curves"]["default"]["points"]
            assert sel_value(scr.query_one("#curve-pick", Select)) == \
                "case-fans"
            assert len(scr.pts) == 4

    asyncio.run(run())


def test_new_curve_refuses_an_id_that_is_taken(temp_config, machine, notices):
    """Overwriting an existing curve by typo-ing its id would silently move
    another fan's cooling — the modal refuses and stays open."""

    async def run():
        async with FanControlApp(config_path=temp_config,
                                 live=False).run_test(size=(120, 45)) \
                as pilot:
            await pilot.pause()
            await _goto_curves(pilot)
            await pilot.click("#new-curve")
            modal = await wait_modal(pilot, NewCurveModal)
            modal.query_one("#nc-id", Input).value = "default"
            before = read_cfg(temp_config)
            await pilot.click("#nc-ok")
            await pilot.pause()
            assert isinstance(pilot.app.screen, NewCurveModal)
            assert any("already exists" in m.lower() for _s, m in notices)
            assert read_cfg(temp_config) == before
            await pilot.click("#nc-cancel")
            await wait_not_modal(pilot)

    asyncio.run(run())


def test_copy_duplicates_the_curve_as_currently_edited(temp_config, machine):
    """`c` / Copy is the normal way to tune: same curve, shifted — including
    point moves that have not been saved yet."""

    async def run():
        async with FanControlApp(config_path=temp_config,
                                 live=False).run_test(size=(120, 45)) \
                as pilot:
            await pilot.pause()
            scr = await _goto_curves(pilot)
            scr.action_nudge_duty_up5()      # unsaved edit
            nudge = list(scr.pts[0])
            await pilot.click("#dup-curve")
            modal = await wait_modal(pilot, NewCurveModal)
            assert modal.suggested_id == "default-copy"
            await pilot.click("#nc-ok")
            await wait_not_modal(pilot)
            doc = read_cfg(temp_config)
            assert "default-copy" in doc["curves"]
            assert doc["curves"]["default-copy"]["points"][0] == nudge
            # the original was NOT changed by the copy
            assert doc["curves"]["default"]["points"][0] != nudge

    asyncio.run(run())


def test_delete_curve_refuses_one_that_assignments_use(temp_config, machine,
                                                       notices):
    """Deleting a curve in use leaves those assignments with no curve — the
    daemon skips them and their fans stay at their last duty. Refused, and
    the assignments are named."""
    _seed_assignments(temp_config,
                      CPU={"source": "k10temp:temp1", "curve": "default",
                           "fans": ["it8686:pwm1"], "enabled": True})

    async def run():
        async with FanControlApp(config_path=temp_config,
                                 live=False).run_test(size=(120, 45)) \
                as pilot:
            await pilot.pause()
            scr = await _goto_curves(pilot)
            assert "used by: CPU" in str(
                scr.query_one("#curve-used", Static).render())
            await pilot.click("#del-curve")
            await pilot.pause()
            assert not isinstance(pilot.app.screen, ConfirmScreen)
            assert any("used by 1 assignment" in m.lower()
                       and "cpu" in m.lower() for _s, m in notices), notices
            assert "default" in read_cfg(temp_config)["curves"]

    asyncio.run(run())


def test_delete_unused_curve_after_confirm(temp_config, machine):
    cfg = config_mod.load(temp_config)
    cfg.curves["spare"] = {"label": "Spare", "points": [[50, 30]]}
    config_mod.save(cfg, temp_config)

    async def run():
        async with FanControlApp(config_path=temp_config,
                                 live=False).run_test(size=(120, 45)) \
                as pilot:
            await pilot.pause()
            scr = await _goto_curves(pilot)
            sel = scr.query_one("#curve-pick", Select)
            sel.value = "spare"
            await pilot.pause()
            assert "not used by any assignment" in str(
                scr.query_one("#curve-used", Static).render())
            await pilot.click("#del-curve")
            modal = await wait_modal(pilot, ConfirmScreen)
            assert "Spare" in modal.title_text
            await pilot.click("#confirm-ok")
            await wait_not_modal(pilot)
            assert "spare" not in read_cfg(temp_config)["curves"]
            # the picker moved to a curve that still exists
            assert sel_value(scr.query_one("#curve-pick", Select)) != "spare"

    asyncio.run(run())


def test_curve_label_rename_shows_up_in_the_pickers(temp_config, machine):
    """The label is what every picker displays, so it is part of the same
    Save as the points."""

    async def run():
        async with FanControlApp(config_path=temp_config,
                                 live=False).run_test(size=(120, 45)) \
                as pilot:
            await pilot.pause()
            scr = await _goto_curves(pilot)
            assert scr.query_one("#curve-label", Input).value == "Default"
            scr.query_one("#curve-label", Input).value = "CPU ramp"
            scr.save_curve()
            await pilot.pause()
            assert read_cfg(temp_config)["curves"]["default"]["label"] == \
                "CPU ramp"
            labels = [lab for lab, _v in select_options(
                scr.query_one("#curve-pick", Select))]
            assert any("CPU ramp" in str(lab) for lab in labels)

    asyncio.run(run())


# ----------------------------------------------------------- root warning --

def test_nonroot_tui_shows_warning_on_every_screen(temp_config, machine):
    """Non-root cannot take over fans (pwm writes are root-only) and edits
    the home config, not /etc — a red banner in the sidebar must say so on
    EVERY screen, so nobody wonders why the fans don't move."""
    async def run():
        async with FanControlApp(config_path=temp_config,
                                 live=False).run_test(size=(120, 40)) \
                as pilot:
            await pilot.pause()
            assert pilot.app.is_root is False   # tests run as the dev user
            for key in ("1", "2", "3", "4"):
                await pilot.press(key)
                await pilot.pause()
                warn = pilot.app.screen.query_one("#root-warn", Static)
                assert "non-root" in str(warn.render())

    asyncio.run(run())


def test_root_tui_has_no_warning(temp_config, machine, monkeypatch):
    """Root is the normal case (the service, sudo) — no warning clutter."""
    monkeypatch.setattr(os, "geteuid", lambda: 0)

    async def run():
        async with FanControlApp(config_path=temp_config,
                                 live=False).run_test(size=(120, 40)) \
                as pilot:
            await pilot.pause()
            assert pilot.app.is_root is True
            assert len(pilot.app.screen.query("#root-warn")) == 0

    asyncio.run(run())


# --------------------------------------------- overrides, enable, rename -

def test_blank_floor_ceiling_rate_clears_the_override(temp_config, machine):
    """A blank field means "no override" — saving must REMOVE the key so the
    assignment follows the global default again. Typing the global's number
    instead would pin it per assignment, which is a different setting (and
    stops tracking the global when it changes)."""
    _seed_assignments(temp_config,
                      CPU={"source": "k10temp:temp1", "curve": "default",
                           "fans": ["it8686:pwm1"], "enabled": True,
                           "floor_duty": 30, "max_duty": 90, "max_rate": 4})

    async def run():
        async with FanControlApp(config_path=temp_config,
                                 live=False).run_test(size=(120, 45)) \
                as pilot:
            await pilot.pause()
            await pilot.press("2")
            await pilot.pause()
            scr = pilot.app.screen
            await edit_assignment_row(pilot, scr)
            assert scr.query_one("#floor", Input).value == "30"
            for wid in ("#floor", "#maxduty", "#rate"):
                scr.query_one(wid, Input).value = ""
            scr.save_assignment()
            await pilot.pause()
            saved = read_cfg(temp_config)["assignments"]["CPU"]
            for key in ("floor_duty", "max_duty", "max_rate"):
                assert key not in saved, f"{key}={saved[key]!r} still set"
            # and the keys the form did have are untouched
            assert saved["source"] == "k10temp:temp1"
            assert saved["fans"] == ["it8686:pwm1"]

    asyncio.run(run())


def test_blank_fields_keep_unmanaged_keys(temp_config, machine):
    """Clearing the overrides must not touch what the form does not manage
    (here: a hand-set fail-safe opt-out and a tach mapping)."""
    _seed_assignments(temp_config,
                      CPU={"source": "k10temp:temp1", "curve": "default",
                           "fans": ["it8686:pwm1"], "enabled": True,
                           "floor_duty": 30, "failsafe": False,
                           "tach": {"it8686:pwm1": "it8686:fan1"}})

    async def run():
        async with FanControlApp(config_path=temp_config,
                                 live=False).run_test(size=(120, 45)) \
                as pilot:
            await pilot.pause()
            await pilot.press("2")
            await pilot.pause()
            scr = pilot.app.screen
            await edit_assignment_row(pilot, scr)
            scr.query_one("#floor", Input).value = ""
            scr.save_assignment()
            await pilot.pause()
            # the assignment has `failsafe: false` — saving it asks first,
            # because that switch is one flip away from removing a safety net
            confirm = await wait_modal(pilot, ConfirmScreen)
            assert "fail-safe" in confirm.title_text
            await pilot.click("#confirm-ok")
            await wait_not_modal(pilot)
            saved = read_cfg(temp_config)["assignments"]["CPU"]
            assert "floor_duty" not in saved
            assert saved["failsafe"] is False
            assert saved["tach"] == {"it8686:pwm1": "it8686:fan1"}

    asyncio.run(run())


def test_placeholders_show_the_live_global_default(temp_config, machine):
    """'(default 5)' written into the widget would start lying as soon as
    the global changes — the placeholder reads the config."""
    cfg = config_mod.load(temp_config)
    cfg.config["floor_duty"] = 17
    cfg.config["max_rate"] = 2.5
    config_mod.save(cfg, temp_config)

    async def run():
        async with FanControlApp(config_path=temp_config,
                                 live=False).run_test(size=(120, 45)) \
                as pilot:
            await pilot.pause()
            await pilot.press("2")
            await pilot.pause()
            scr = pilot.app.screen
            assert "17" in scr.query_one("#floor", Input).placeholder
            assert "2.5" in scr.query_one("#rate", Input).placeholder

    asyncio.run(run())


def test_enabled_switch_releases_fans(temp_config, machine, notices):
    """The list shows on/off, so the form must be able to change it: off
    writes enabled=false (the daemon restores firmware control)."""
    _seed_assignments(temp_config,
                      CPU={"source": "k10temp:temp1", "curve": "default",
                           "fans": ["it8686:pwm1"], "enabled": True})

    async def run():
        async with FanControlApp(config_path=temp_config,
                                 live=False).run_test(size=(120, 45)) \
                as pilot:
            await pilot.pause()
            await pilot.press("2")
            await pilot.pause()
            scr = pilot.app.screen
            await edit_assignment_row(pilot, scr)
            assert scr.query_one("#enabled", Switch).value is True
            scr.query_one("#enabled", Switch).value = False
            scr.save_assignment()
            await pilot.pause()
            assert read_cfg(temp_config)["assignments"]["CPU"]["enabled"] \
                is False
            lst = scr.query_one("#assign-list", DataTable)
            assert lst.get_cell_at((0, 7)) == "off"

    asyncio.run(run())


def test_e_key_disables_after_confirm_then_enables_directly(temp_config,
                                                            machine):
    """`e` on a selected row is the quick path. Releasing fans to the
    firmware is the destructive direction, so it asks; taking them back
    does not."""
    _seed_assignments(temp_config,
                      CPU={"source": "k10temp:temp1", "curve": "default",
                           "fans": ["it8686:pwm1"], "enabled": True})

    async def run():
        async with FanControlApp(config_path=temp_config,
                                 live=False).run_test(size=(120, 45)) \
                as pilot:
            await pilot.pause()
            await pilot.press("2")
            await pilot.pause()
            scr = pilot.app.screen
            lst = scr.query_one("#assign-list", DataTable)
            lst.focus()
            await post_row_selected(pilot, lst, 0)
            await pilot.press("e")
            modal = await wait_modal(pilot, ConfirmScreen)
            assert "Disable" in modal.title_text
            await pilot.click("#confirm-ok")
            await wait_not_modal(pilot)
            assert read_cfg(temp_config)["assignments"]["CPU"]["enabled"] \
                is False
            assert lst.get_cell_at((0, 7)) == "off"
            # enabling again: no confirmation, it only restores control
            await pilot.press("e")
            await pilot.pause()
            assert not isinstance(pilot.app.screen, ConfirmScreen)
            assert read_cfg(temp_config)["assignments"]["CPU"]["enabled"] \
                is True

    asyncio.run(run())


def test_disable_cancel_keeps_assignment_enabled(temp_config, machine):
    _seed_assignments(temp_config,
                      CPU={"source": "k10temp:temp1", "curve": "default",
                           "fans": ["it8686:pwm1"], "enabled": True})

    async def run():
        async with FanControlApp(config_path=temp_config,
                                 live=False).run_test(size=(120, 45)) \
                as pilot:
            await pilot.pause()
            await pilot.press("2")
            await pilot.pause()
            scr = pilot.app.screen
            lst = scr.query_one("#assign-list", DataTable)
            lst.focus()
            await post_row_selected(pilot, lst, 0)
            await pilot.press("e")
            await wait_modal(pilot, ConfirmScreen)
            await pilot.click("#confirm-cancel")
            await wait_not_modal(pilot)
            assert read_cfg(temp_config)["assignments"]["CPU"]["enabled"] \
                is True

    asyncio.run(run())


def test_toggle_button_without_selection_warns(temp_config, machine, notices):
    async def run():
        async with FanControlApp(config_path=temp_config,
                                 live=False).run_test(size=(120, 45)) \
                as pilot:
            await pilot.pause()
            await pilot.press("2")
            await pilot.pause()
            scr = pilot.app.screen
            lst = scr.query_one("#assign-list", DataTable)
            assert lst.row_count == 0
            await pilot.click("#toggle-enabled")
            await pilot.pause()
            assert any("no assignment selected" in m.lower()
                       for _s, m in notices), notices

    asyncio.run(run())


def test_rename_moves_assignment_instead_of_duplicating(temp_config, machine):
    """Editing the name of a loaded assignment is a RENAME. Silently writing
    a second key left both driving the same fan, with nothing but a generic
    shared-fan notice to hint that anything had happened."""
    _seed_assignments(temp_config,
                      CPU={"source": "k10temp:temp1", "curve": "default",
                           "fans": ["it8686:pwm1"], "enabled": True,
                           "floor_duty": 20})

    async def run():
        async with FanControlApp(config_path=temp_config,
                                 live=False).run_test(size=(120, 45)) \
                as pilot:
            await pilot.pause()
            await pilot.press("2")
            await pilot.pause()
            scr = pilot.app.screen
            await edit_assignment_row(pilot, scr)
            scr.query_one("#name", Input).value = "CPU-box"
            scr.save_assignment()
            modal = await wait_modal(pilot, ConfirmScreen)
            assert "Rename" in modal.title_text
            await pilot.click("#confirm-ok")
            await wait_not_modal(pilot)
            saved = read_cfg(temp_config)["assignments"]
            assert set(saved) == {"CPU-box"}, saved
            assert saved["CPU-box"]["floor_duty"] == 20
            assert scr.query_one("#assign-list", DataTable).row_count == 1

    asyncio.run(run())


def test_rename_cancel_keeps_the_original_name(temp_config, machine):
    _seed_assignments(temp_config,
                      CPU={"source": "k10temp:temp1", "curve": "default",
                           "fans": ["it8686:pwm1"], "enabled": True})

    async def run():
        async with FanControlApp(config_path=temp_config,
                                 live=False).run_test(size=(120, 45)) \
                as pilot:
            await pilot.pause()
            await pilot.press("2")
            await pilot.pause()
            scr = pilot.app.screen
            await edit_assignment_row(pilot, scr)
            scr.query_one("#name", Input).value = "CPU-box"
            scr.save_assignment()
            await wait_modal(pilot, ConfirmScreen)
            await pilot.click("#confirm-cancel")
            await wait_not_modal(pilot)
            assert set(read_cfg(temp_config)["assignments"]) == {"CPU"}

    asyncio.run(run())


def test_rename_onto_an_existing_name_overwrites_after_confirm(temp_config,
                                                               machine):
    """Renaming onto a name that is taken replaces THAT assignment (and
    removes the source of the rename) rather than leaving three entries."""
    _seed_assignments(temp_config,
                      CPU={"source": "k10temp:temp1", "curve": "default",
                           "fans": ["it8686:pwm1"], "enabled": True},
                      System={"source": "it8686:temp1", "curve": "default",
                              "fans": ["it8686:pwm2"], "enabled": True})

    async def run():
        async with FanControlApp(config_path=temp_config,
                                 live=False).run_test(size=(120, 45)) \
                as pilot:
            await pilot.pause()
            await pilot.press("2")
            await pilot.pause()
            scr = pilot.app.screen
            await edit_assignment_row(pilot, scr)
            scr.query_one("#name", Input).value = "System"
            scr.save_assignment()
            modal = await wait_modal(pilot, ConfirmScreen)
            assert "overwrite" in modal.title_text.lower()
            await pilot.click("#confirm-ok")
            await wait_not_modal(pilot)
            saved = read_cfg(temp_config)["assignments"]
            assert set(saved) == {"System"}, saved
            assert saved["System"]["fans"] == ["it8686:pwm1"]

    asyncio.run(run())


# --------------------------------------------------- validate before saving --

def test_save_blocked_when_config_is_invalid(temp_config, machine, notices):
    """The daemon keeps its PREVIOUS config when validation fails, so a save
    that writes an invalid file does nothing at all — the TUI must say so
    instead of reporting Saved."""
    cfg = _seed_assignments(temp_config,
                            CPU={"source": "k10temp:temp1",
                                 "curve": "default",
                                 "fans": ["it8686:pwm1"], "enabled": True})
    cfg.curves["default"]["points"] = []      # validate(): needs >= 1 point
    config_mod.save(cfg, temp_config)

    async def run():
        async with FanControlApp(config_path=temp_config,
                                 live=False).run_test(size=(120, 45)) \
                as pilot:
            await pilot.pause()
            await pilot.press("2")
            await pilot.pause()
            scr = pilot.app.screen
            await edit_assignment_row(pilot, scr)
            scr.query_one("#floor", Input).value = "12"
            scr.save_assignment()
            modal = await wait_modal(pilot, ConfigErrorsScreen)
            assert any("needs >= 1 point" in e for e in modal.errors)
            await pilot.press("escape")
            await wait_not_modal(pilot)
            # nothing was written, and no "Saved" lie was told
            assert "floor_duty" not in \
                read_cfg(temp_config)["assignments"]["CPU"]
            assert not any("saved" in m.lower() for _s, m in notices), notices

    asyncio.run(run())


def test_curve_with_no_points_cannot_be_saved(temp_config, machine, notices):
    """Deleting every point and saving used to write an invalid curve with a
    cheerful "Curve saved"; the daemon then silently ignored the file."""
    original = copy.deepcopy(
        config_mod.load(temp_config).curves["default"]["points"])

    async def run():
        async with FanControlApp(config_path=temp_config,
                                 live=False).run_test(size=(120, 45)) \
                as pilot:
            await pilot.pause()
            await pilot.press("3")
            await pilot.pause()
            scr = pilot.app.screen
            while scr.pts:
                scr.action_delete_point()
                await pilot.pause()
            scr.save_curve()
            await wait_modal(pilot, ConfigErrorsScreen)
            await pilot.press("escape")
            await wait_not_modal(pilot)
            on_disk = read_cfg(temp_config)["curves"]["default"]["points"]
            assert on_disk == original
            assert not any("saved" in m.lower() for _s, m in notices), notices

    asyncio.run(run())


def test_global_config_error_blocks_and_says_where_to_fix_it(temp_config,
                                                             machine):
    """A bad global value cannot be fixed in the TUI yet, so the error modal
    has to point at the file instead of leaving the user stuck."""
    cfg = config_mod.load(temp_config)
    cfg.config["floor_duty"] = 140        # out of range (0-100)
    config_mod.save(cfg, temp_config)

    async def run():
        async with FanControlApp(config_path=temp_config,
                                 live=False).run_test(size=(120, 45)) \
                as pilot:
            await pilot.pause()
            await pilot.press("2")
            await pilot.pause()
            scr = pilot.app.screen
            scr.query_one("#name", Input).value = "later"
            scr.query_one("#source", Select).value = "k10temp:temp1"
            scr.query_one("#curve", Select).value = "default"
            table = scr.query_one("#fan-pick", DataTable)
            await post_row_selected(pilot, table,
                                    machine.fan_ids().index("it8686:pwm5"))
            scr.save_assignment()
            modal = await wait_modal(pilot, ConfigErrorsScreen)
            text = " ".join(str(l.render()) for l in modal.query(Label))
            assert "floor_duty" in text
            assert str(temp_config) in text
            await pilot.press("escape")
            await wait_not_modal(pilot)

    asyncio.run(run())


def test_deleting_a_builtin_curve_sticks(temp_config, machine):
    """F14. `default` used to be un-deletable: config.load() merged DEFAULTS
    back in, so the file lost the curve while every reader (daemon hot-reload,
    TUI restart) kept seeing it — a Delete button that did nothing. What the
    file says is what loads now."""

    async def run():
        async with FanControlApp(config_path=temp_config,
                                 live=False).run_test(size=(120, 45)) \
                as pilot:
            await pilot.pause()
            scr = await _goto_curves(pilot)
            assert sel_value(scr.query_one("#curve-pick", Select)) == "default"
            await pilot.click("#del-curve")
            await pilot.pause()
            confirm = pilot.app.screen
            assert isinstance(confirm, ConfirmScreen)
            await pilot.click("#confirm-ok")
            await wait_not_modal(pilot)
            assert "default" not in read_cfg(temp_config)["curves"]
            # and a fresh load does not resurrect it
            assert "default" not in config_mod.load(temp_config).curves
            assert "gpu_v100" in read_cfg(temp_config)["curves"]
            assert sel_value(scr.query_one("#curve-pick", Select)) == "gpu_v100"

    asyncio.run(run())


# ------------------------------------------------------------- tach mapping --

def _pick_fan(pilot, scr, fan):
    """Click a row of the fan picker (the click-to-toggle entry point)."""
    table = scr.query_one("#fan-pick", DataTable)
    rows = {table.get_cell_at((r, 1)): r for r in range(table.row_count)}
    return post_row_selected(pilot, table, rows[fan])


def _col(table: DataTable, key: str) -> int:
    """Index of a column by the key given to add_columns — positional
    asserts on these tables rot every time one is added."""
    try:
        return table.get_column_index(key)
    except Exception:
        raise AssertionError(f"no column {key!r} in {table.id}")


def _cell(table: DataTable, row_key, col: int):
    """Cell of the row whose key is `row_key` (keys are RowKey wrappers that
    compare equal to the plain str we passed to add_row)."""
    for r in range(table.row_count):
        if table.coordinate_to_cell_key((r, 0))[0] == row_key:
            return table.get_cell_at((r, col))
    raise AssertionError(f"no row {row_key!s} in {table.id}")


def test_picking_a_fan_shows_the_tach_it_guessed(temp_config, machine):
    """The tach table pre-fills from matching sensors.d labels (pwm5 label ==
    fan5 label), which is the common case and must not need a click."""

    async def run():
        async with FanControlApp(config_path=temp_config,
                                 live=False).run_test(size=(140, 50)) \
                as pilot:
            await pilot.pause()
            scr = await _goto_assign(pilot)
            await _pick_fan(pilot, scr, "it8686:pwm5")
            tachs = scr.query_one("#tach-pick", DataTable)
            assert tachs.row_count == 1
            assert _cell(tachs, "it8686:pwm5", 1) == "it8686:fan5"

    asyncio.run(run())


def test_unmapped_header_saves_without_tach_and_warns(temp_config, machine,
                                                     notices):
    """Where the labels do not pair there is no guess: the row says so, and
    saving warns that a stall on that header cannot be detected (F4 — before
    this, an unmapped fan was simply unwatched, with no signal anywhere)."""
    machine.labels["it8686"].pop("fan5", None)

    async def run():
        async with FanControlApp(config_path=temp_config,
                                 live=False).run_test(size=(140, 50)) \
                as pilot:
            await pilot.pause()
            scr = await _goto_assign(pilot)
            await _pick_fan(pilot, scr, "it8686:pwm5")
            tachs = scr.query_one("#tach-pick", DataTable)
            assert _cell(tachs, "it8686:pwm5", 1) == "(none)"
            assert _cell(tachs, "it8686:pwm5", 3) == "⚠"
            scr.query_one("#name", Input).value = "unwatched"
            scr.query_one("#source", Select).value = "k10temp:temp1"
            scr.query_one("#curve", Select).value = "default"
            scr.save_assignment()
            await pilot.pause()
            saved = read_cfg(temp_config)["assignments"]["unwatched"]
            assert "tach" not in saved, saved
            assert any("cannot be detected" in m.lower() and "pwm5" in m
                       for _s, m in notices), notices

    asyncio.run(run())


def test_tach_picker_maps_a_header_the_guess_missed(temp_config, machine,
                                                   notices):
    """The fix for F4: with no label match the user can still say 'this fanN
    is its RPM source', and that lands in the config."""
    machine.labels["it8686"].pop("fan5", None)

    async def run():
        async with FanControlApp(config_path=temp_config,
                                 live=False).run_test(size=(140, 50)) \
                as pilot:
            await pilot.pause()
            scr = await _goto_assign(pilot)
            await _pick_fan(pilot, scr, "it8686:pwm5")
            tachs = scr.query_one("#tach-pick", DataTable)
            await post_row_selected(pilot, tachs, 0)
            modal = await wait_modal(pilot, TachPickerModal)
            # the cursor opens on the current mapping (here: no tach), so
            # move to the first candidate and select it
            table = modal.query_one("#tp-list", DataTable)
            await post_row_selected(pilot, table, 0)
            await wait_not_modal(pilot)
            assert _cell(tachs, "it8686:pwm5", 1) == "it8686:fan1"
            scr.query_one("#name", Input).value = "mapped"
            scr.query_one("#source", Select).value = "k10temp:temp1"
            scr.query_one("#curve", Select).value = "default"
            scr.save_assignment()
            await pilot.pause()
            assert read_cfg(temp_config)["assignments"]["mapped"]["tach"] \
                == {"it8686:pwm5": "it8686:fan1"}

    asyncio.run(run())


def test_tach_picker_lists_same_chip_inputs_first_with_their_labels(
        temp_config, machine):
    """Ordering and notes are how a human identifies the right tach: this
    chip's inputs first, the sensors.d label beside each, and a flag on the
    one whose label matches the header being mapped."""

    async def run():
        async with FanControlApp(config_path=temp_config,
                                 live=False).run_test(size=(140, 50)) \
                as pilot:
            await pilot.pause()
            scr = await _goto_assign(pilot)
            opts = scr._tach_candidates("it8686:pwm1")
            ids = [t for t, _n in opts]
            # the machine exposes fan1/3/4 on it8686 and fan1/3 on it8792:
            # this chip's inputs come first, then the other chip's
            assert ids == ["it8686:fan1", "it8686:fan3", "it8686:fan4",
                           "it8792:fan1", "it8792:fan3"]
            notes = dict(opts)
            assert "matches this header" in notes["it8686:fan1"]
            assert notes["it8686:fan3"] == "SYS_FAN2"
            assert notes["it8792:fan1"] == "SYS_FAN5_PUMP"

    asyncio.run(run())


def test_no_tach_choice_removes_an_existing_mapping(temp_config, machine):
    """Explicitly unmapping must be possible (a tach wire that was moved to
    another header is worse than no reading at all: it lies)."""
    _seed_assignments(temp_config,
                      CPU={"source": "k10temp:temp1", "curve": "default",
                           "fans": ["it8686:pwm1"], "enabled": True,
                           "tach": {"it8686:pwm1": "it8686:fan1"}})

    async def run():
        async with FanControlApp(config_path=temp_config,
                                 live=False).run_test(size=(140, 50)) \
                as pilot:
            await pilot.pause()
            scr = await _goto_assign(pilot)
            await edit_assignment_row(pilot, scr)
            tachs = scr.query_one("#tach-pick", DataTable)
            assert _cell(tachs, "it8686:pwm1", 1) == "it8686:fan1"
            await post_row_selected(pilot, tachs, 0)
            modal = await wait_modal(pilot, TachPickerModal)
            table = modal.query_one("#tp-list", DataTable)
            await post_row_selected(pilot, table, table.row_count - 1)
            await wait_not_modal(pilot)
            assert _cell(tachs, "it8686:pwm1", 1) == "(none)"
            scr.save_assignment()
            await pilot.pause()
            assert "tach" not in \
                read_cfg(temp_config)["assignments"]["CPU"]

    asyncio.run(run())


def test_manual_tach_choice_beats_the_label_guess_on_later_saves(
        temp_config, machine):
    """A hand-set mapping must not be re-guessed away by an unrelated edit."""
    _seed_assignments(temp_config,
                      CPU={"source": "k10temp:temp1", "curve": "default",
                           "fans": ["it8686:pwm5"], "enabled": True,
                           "tach": {"it8686:pwm5": "it8686:fan2"}})

    async def run():
        async with FanControlApp(config_path=temp_config,
                                 live=False).run_test(size=(140, 50)) \
                as pilot:
            await pilot.pause()
            scr = await _goto_assign(pilot)
            await edit_assignment_row(pilot, scr)
            # guess_tach would say it8686:fan5 (the labels pair); the config
            # says fan2 and that is what the form must show and keep
            assert app_guess(scr, "it8686:pwm5") == "it8686:fan5"
            assert scr._tach_map["it8686:pwm5"] == "it8686:fan2"
            scr.query_one("#floor", Input).value = "15"
            scr.save_assignment()
            await pilot.pause()
            assert read_cfg(temp_config)["assignments"]["CPU"]["tach"] == \
                {"it8686:pwm5": "it8686:fan2"}

    asyncio.run(run())


def app_guess(scr, fan):
    return scr.app.guess_tach(fan)


def test_unlabeled_tach_is_listed_without_a_redundant_note(temp_config,
                                                           machine):
    """sensors.d has no label for every input. The generic 'fan3 (it8686)'
    fallback would only restate the id in the column beside it, so the note
    stays blank — and an unlabeled input must still be mappable."""
    machine.labels["it8686"].pop("fan4")

    async def run():
        async with FanControlApp(config_path=temp_config,
                                 live=False).run_test(size=(140, 50)) \
                as pilot:
            await pilot.pause()
            scr = await _goto_assign(pilot)
            notes = dict(scr._tach_candidates("it8686:pwm1"))
            assert notes["it8686:fan4"] == ""
            assert notes["it8686:fan3"] == "SYS_FAN2"
            # and it can actually be chosen
            await _pick_fan(pilot, scr, "it8686:pwm5")
            tachs = scr.query_one("#tach-pick", DataTable)
            await post_row_selected(pilot, tachs, 0)
            modal = await wait_modal(pilot, TachPickerModal)
            table = modal.query_one("#tp-list", DataTable)
            rows = {table.get_cell_at((r, 0)): r
                    for r in range(table.row_count)}
            await post_row_selected(pilot, table, rows["it8686:fan4"])
            await wait_not_modal(pilot)
            assert _cell(tachs, "it8686:pwm5", 1) == "it8686:fan4"

    asyncio.run(run())


# ---------------------------------------------------------------- settings --

def _goto_settings(pilot):
    async def _go():
        await pilot.press("5")
        await pilot.pause()
        return pilot.app.screen
    return _go()


def _settings_row(table: DataTable, key: str) -> int:
    for r in range(table.row_count):
        if row_key(table, r) == key:
            return r
    raise AssertionError(f"no setting row {key}")


def _setting_cell(table: DataTable, key: str, col: int):
    return table.get_cell_at((_settings_row(table, key), col))


async def _edit_setting(pilot, scr, key, value):
    """Click a setting row, type into the modal, press Set."""
    table = scr.query_one("#settings", DataTable)
    await post_row_selected(pilot, table, _settings_row(table, key))
    modal = await wait_modal(pilot, SettingModal)
    inp = modal.query_one("#set-input", Input)
    inp.value = str(value)
    await pilot.click("#set-ok")
    await wait_not_modal(pilot)
    return modal


def test_settings_screen_lists_every_option(temp_config, machine):
    """F1: the 22 global options were readable only in config.py. Now every
    one is listed with its current value, unit and built-in default — and the
    list is generated from the same table validate() uses, so a knob cannot be
    added to the daemon and forgotten here."""

    async def run():
        async with FanControlApp(config_path=temp_config,
                                 live=False).run_test(size=(140, 50)) \
                as pilot:
            await pilot.pause()
            scr = await _goto_settings(pilot)
            table = scr.query_one("#settings", DataTable)
            keys = {row_key(table, r) for r in range(table.row_count)}
            assert keys == set(config_mod.BY_KEY)
            assert _setting_cell(table, "poll_seconds", 2) == "1 s"
            assert _setting_cell(table, "max_rate", 2) == "6 % per poll"
            # a value that differs from the default is marked as such
            assert _setting_cell(table, "stall_alert_command", 2) == "(empty)"

    asyncio.run(run())


def test_settings_edit_saves_after_confirmation(temp_config, machine):
    async def run():
        async with FanControlApp(config_path=temp_config,
                                 live=False).run_test(size=(140, 50)) \
                as pilot:
            await pilot.pause()
            scr = await _goto_settings(pilot)
            await _edit_setting(pilot, scr, "poll_seconds", "2.5")
            table = scr.query_one("#settings", DataTable)
            assert _setting_cell(table, "poll_seconds", 2) == "2.5 s  *"
            assert _setting_cell(table, "poll_seconds", 3) == "→ save"
            # nothing written yet
            assert read_cfg(temp_config)["config"]["poll_seconds"] == 1.0
            await pilot.click("#save-settings")
            confirm = await wait_modal(pilot, ConfirmSettingsScreen)
            assert any("poll_seconds: 1 → 2.5" in l
                       for l in confirm.lines), confirm.lines
            await pilot.click("#confirm-ok")
            await wait_not_modal(pilot)
            assert read_cfg(temp_config)["config"]["poll_seconds"] == 2.5

    asyncio.run(run())


def test_settings_blank_value_removes_the_key(temp_config, machine):
    """Blank = 'I never set this': the key leaves the file, which is what the
    built-in default means on disk (load() merges config.* per key)."""

    async def run():
        async with FanControlApp(config_path=temp_config,
                                 live=False).run_test(size=(140, 50)) \
                as pilot:
            await pilot.pause()
            scr = await _goto_settings(pilot)
            await _edit_setting(pilot, scr, "max_rate", "9")
            await pilot.click("#save-settings")
            await wait_modal(pilot, ConfirmSettingsScreen)
            await pilot.click("#confirm-ok")
            await wait_not_modal(pilot)
            assert read_cfg(temp_config)["config"]["max_rate"] == 9
            await _edit_setting(pilot, scr, "max_rate", "")
            table = scr.query_one("#settings", DataTable)
            assert _setting_cell(table, "max_rate", 3) == "→ save"
            await pilot.click("#save-settings")
            await wait_modal(pilot, ConfirmSettingsScreen)
            await pilot.click("#confirm-ok")
            await wait_not_modal(pilot)
            assert "max_rate" not in read_cfg(temp_config)["config"]
            # and a reload still behaves: the default applies
            assert config_mod.load(temp_config).config["max_rate"] == 6

    asyncio.run(run())


def test_settings_refuses_an_out_of_range_number(temp_config, machine):
    """The modal's bounds are validate()'s bounds, so a mistake is caught
    where it was typed instead of at save time."""

    async def run():
        async with FanControlApp(config_path=temp_config,
                                 live=False).run_test(size=(140, 50)) \
                as pilot:
            await pilot.pause()
            scr = await _goto_settings(pilot)
            table = scr.query_one("#settings", DataTable)
            await post_row_selected(pilot, table,
                                    _settings_row(table, "floor_duty"))
            modal = await wait_modal(pilot, SettingModal)
            modal.query_one("#set-input", Input).value = "150"
            await pilot.click("#set-ok")
            await pilot.pause()
            assert isinstance(pilot.app.screen, SettingModal), \
                "out-of-range value must not close the modal"
            assert "must be <= 100" in str(
                modal.query_one("#set-err", Static).render())
            assert "floor_duty" not in scr._edits
            await pilot.click("#set-cancel")
            await wait_not_modal(pilot)
            assert read_cfg(temp_config)["config"]["floor_duty"] == 5

    asyncio.run(run())


def test_settings_save_refuses_a_self_contradictory_pair(temp_config, machine):
    """stall_full_duty below stall_min_duty would disarm the stall detector
    that pins the fan — validate() rejects the pairing and the save is
    refused with the reason rather than written and ignored."""

    async def run():
        async with FanControlApp(config_path=temp_config,
                                 live=False).run_test(size=(140, 50)) \
                as pilot:
            await pilot.pause()
            scr = await _goto_settings(pilot)
            await _edit_setting(pilot, scr, "stall_full_duty", "5")
            await pilot.click("#save-settings")
            await wait_modal(pilot, ConfirmSettingsScreen)
            await pilot.click("#confirm-ok")
            errs = await wait_modal(pilot, ConfigErrorsScreen)
            assert any("stall_full_duty" in e and "stall_min_duty" in e
                       for e in errs.errors), errs.errors
            # the file is untouched
            assert read_cfg(temp_config)["config"]["stall_full_duty"] == 100

    asyncio.run(run())


def test_unsaved_settings_do_not_leak_into_another_screens_save(
        temp_config, machine):
    """Edits live on this screen until Save. If they were written into the
    shared config object as they were typed, saving an assignment on screen 2
    would also publish a poll interval the user never saved."""
    _seed_assignments(temp_config,
                      CPU={"source": "k10temp:temp1", "curve": "default",
                           "fans": ["it8686:pwm1"], "enabled": True})

    async def run():
        async with FanControlApp(config_path=temp_config,
                                 live=False).run_test(size=(140, 50)) \
                as pilot:
            await pilot.pause()
            scr = await _goto_settings(pilot)
            await _edit_setting(pilot, scr, "poll_seconds", "7")
            assign = await _goto_assign(pilot)
            await edit_assignment_row(pilot, assign)
            assign.save_assignment()
            await pilot.pause()
            saved = read_cfg(temp_config)
            assert saved["config"]["poll_seconds"] == 1.0, saved["config"]
            assert saved["assignments"], saved
            # and coming back, the pending edit is still there, unsaved
            back = await _goto_settings(pilot)
            table = back.query_one("#settings", DataTable)
            assert _setting_cell(table, "poll_seconds", 3) == "→ save"

    asyncio.run(run())


def test_settings_discard_changes(temp_config, machine):
    async def run():
        async with FanControlApp(config_path=temp_config,
                                 live=False).run_test(size=(140, 50)) \
                as pilot:
            await pilot.pause()
            scr = await _goto_settings(pilot)
            await _edit_setting(pilot, scr, "watchdog_seconds", "0")
            table = scr.query_one("#settings", DataTable)
            assert _setting_cell(table, "watchdog_seconds", 3) == "→ save"
            await pilot.click("#revert-settings")
            confirm = await wait_modal(pilot, ConfirmScreen)
            await pilot.click("#confirm-ok")
            await wait_not_modal(pilot)
            assert "watchdog_seconds" not in scr._edits
            assert _setting_cell(table, "watchdog_seconds", 2) == "30 s"
            assert read_cfg(temp_config)["config"]["watchdog_seconds"] == 30

    asyncio.run(run())


def test_settings_modal_explains_the_option(temp_config, machine):
    """The reason this is a modal and not 22 bare inputs: nobody should have
    to read daemon.py to know what `stall_kick_backoff_s` is for."""

    async def run():
        async with FanControlApp(config_path=temp_config,
                                 live=False).run_test(size=(140, 50)) \
                as pilot:
            await pilot.pause()
            scr = await _goto_settings(pilot)
            table = scr.query_one("#settings", DataTable)
            await post_row_selected(pilot, table,
                                    _settings_row(table,
                                                  "stall_kick_backoff_s"))
            modal = await wait_modal(pilot, SettingModal)
            help_text = str(modal.query_one("#set-help", Static).render())
            assert "rounds" in help_text and "heat" in help_text.lower()
            labels = [str(l.render()) for l in modal.query(Label)]
            assert any("built-in default: 30" in t for t in labels), labels

    asyncio.run(run())


# ------------------------------------------------------- ownership (F5) ----

def _live_with_owners(machine, **owners):
    """A live payload where named headers read a given pwmN_enable:
    `pwm1=1` etc. — how a test says 'another process holds this header'."""
    data = machine.live_payload()
    for fan, en in owners.items():
        data["fans"][fan]["enable"] = en
    return data


def test_fan_picker_shows_who_holds_each_header(temp_config, machine):
    """F5: the picker used to be ✓/fan/label/rpm, so a header held by
    fancontrol or parked at enable 0 looked exactly like a free one — and the
    refusal only appeared later, as the bare word `manual-conflict`."""

    async def run():
        async with FanControlApp(config_path=temp_config,
                                 live=False).run_test(size=(140, 50)) \
                as pilot:
            await pilot.pause()
            scr = await _goto_assign(pilot)
            app = pilot.app
            app.live = _live_with_owners(machine, **{"it8686:pwm3": 1,
                                                     "it8686:pwm4": 0})
            scr._refresh_fans()
            table = scr.query_one("#fan-pick", DataTable)
            own = _col(table, "fown")
            fans = machine.fan_ids()

            def owner_of(fan):
                return table.get_cell_at((fans.index(fan), own))

            assert owner_of("it8686:pwm1") == "firmware"
            assert owner_of("it8686:pwm3") == "other"
            assert owner_of("it8686:pwm4") == "parked"

    asyncio.run(run())


async def _save_named_assignment(pilot, scr, name):
    scr.query_one("#name", Input).value = name
    scr.query_one("#source", Select).value = "k10temp:temp1"
    scr.query_one("#curve", Select).value = "default"
    scr.save_assignment()
    await pilot.pause()


def test_force_takeover_switch_writes_and_clears_the_key(temp_config, machine):
    """Off REMOVES the key rather than writing `false`: absent is how this file
    says no, and a saved false is noise that makes the entry harder to read."""

    async def run():
        async with FanControlApp(config_path=temp_config,
                                 live=False).run_test(size=(140, 50)) \
                as pilot:
            await pilot.pause()
            scr = await _goto_assign(pilot)
            await _pick_fan(pilot, scr, "it8686:pwm3")
            scr.query_one("#force-takeover", Switch).value = True
            await _save_named_assignment(pilot, scr, "hot")
            saved = read_cfg(temp_config)["assignments"]["hot"]
            assert saved["force_takeover"] is True

            # edit it back off: the key must disappear, not become false
            assign = await _goto_assign(pilot)
            await edit_assignment_row(pilot, assign)
            assert assign.query_one("#force-takeover", Switch).value is True
            assign.query_one("#force-takeover", Switch).value = False
            assign.save_assignment()
            await pilot.pause()
            assert "force_takeover" not in \
                read_cfg(temp_config)["assignments"]["hot"]

    asyncio.run(run())


def test_saving_a_foreign_header_warns_it_will_be_refused(temp_config, machine,
                                                          notices):
    """The refusal is a daemon decision the GUI cannot prevent — but it can
    say so at the moment of saving, with the way out."""

    async def run():
        async with FanControlApp(config_path=temp_config,
                                 live=False).run_test(size=(140, 50)) \
                as pilot:
            await pilot.pause()
            scr = await _goto_assign(pilot)
            pilot.app.live = _live_with_owners(machine, **{"it8686:pwm3": 1})
            await _pick_fan(pilot, scr, "it8686:pwm3")
            await _save_named_assignment(pilot, scr, "fight")
            assert read_cfg(temp_config)["assignments"]["fight"]["fans"] == \
                ["it8686:pwm3"]
            assert any("will NOT control" in m and "it8686:pwm3" in m
                       and "Force takeover" in m for _s, m in notices), notices

    asyncio.run(run())


def test_force_takeover_on_a_foreign_header_warns_about_the_fight(
        temp_config, machine, notices):
    """Taking a header someone else holds is allowed and sometimes right (a
    fancontrol that is gone for good) — but two controllers then write the same
    register, and the duty the Overview shows stops being explainable."""

    async def run():
        async with FanControlApp(config_path=temp_config,
                                 live=False).run_test(size=(140, 50)) \
                as pilot:
            await pilot.pause()
            scr = await _goto_assign(pilot)
            pilot.app.live = _live_with_owners(machine, **{"it8686:pwm3": 1})
            await _pick_fan(pilot, scr, "it8686:pwm3")
            scr.query_one("#force-takeover", Switch).value = True
            await _save_named_assignment(pilot, scr, "insist")
            msgs = [m for _s, m in notices]
            assert not any("will NOT control" in m for m in msgs), msgs
            assert any("fought" in m and "it8686:pwm3" in m
                       for m in msgs), msgs

    asyncio.run(run())


def test_parked_header_is_flagged_as_refused(temp_config, machine):
    """enable=0 is §18/§20's shape: a header nobody drives, cooling nothing.
    The daemon refuses it without force_takeover, so the picker says 'parked'
    and the save says it will be refused."""

    async def run():
        async with FanControlApp(config_path=temp_config,
                                 live=False).run_test(size=(140, 50)) \
                as pilot:
            await pilot.pause()
            scr = await _goto_assign(pilot)
            pilot.app.live = _live_with_owners(machine, **{"it8686:pwm4": 0})
            assert pilot.app.fan_owner("it8686:pwm4") == "parked"
            assert pilot.app.needs_force_takeover(["it8686:pwm4"]) == \
                ["it8686:pwm4"]
            assert pilot.app.needs_force_takeover(["it8686:pwm1"]) == []

    asyncio.run(run())


def test_overview_says_why_a_fan_is_not_being_controlled(temp_config, machine):
    """The state cell used to show the daemon's internal word verbatim. Same
    information, but phrased as what happened and what to do about it."""
    _seed_assignments(temp_config,
                      CPU={"source": "k10temp:temp1", "curve": "default",
                           "fans": ["it8686:pwm1"], "enabled": True})

    async def run():
        async with FanControlApp(config_path=temp_config,
                                 live=False).run_test(size=(140, 50)) \
                as pilot:
            await pilot.pause()
            scr = pilot.app.screen
            data = _live_with_owners(machine, **{"it8686:pwm1": 1})
            data["status"] = {"ts": time.time(),
                              "fans": {"it8686:pwm1": {"state":
                                                       "manual-conflict"}}}
            scr.post_message(LiveUpdate(data))
            await pilot.pause()
            table = scr.query_one("#fan-table", DataTable)
            ncols = len(table.columns)
            cells = [str(table.get_cell_at((r, c)))
                     for r in range(table.row_count)
                     for c in range(ncols)]
            assert any("refused: held by another controller" in c
                       for c in cells), cells

    asyncio.run(run())


# ------------------------------------------- per-assignment fail-safe (F6) --

def test_failsafe_overrides_are_shown_and_edited(temp_config, machine):
    """F6: `failsafe`, `failsafe_duty`, `failsafe_after` were read by the
    daemon and validated, but no screen could show them — so 'what happens if
    this source dies' could not be read on screen for a single fan, including
    the answer 'nothing, it holds its last duty'."""
    _seed_assignments(temp_config,
                      CPU={"source": "k10temp:temp1", "curve": "default",
                           "fans": ["it8686:pwm1"], "enabled": True,
                           "failsafe_duty": 80, "failsafe_after": 2})

    async def run():
        async with FanControlApp(config_path=temp_config,
                                 live=False).run_test(size=(120, 45)) \
                as pilot:
            await pilot.pause()
            scr = await _goto_assign(pilot)
            await edit_assignment_row(pilot, scr)
            assert scr.query_one("#fs-duty", Input).value == "80"
            assert scr.query_one("#fs-after", Input).value == "2"
            assert scr.query_one("#fs-rate", Input).value == ""   # global 25
            assert scr.query_one("#failsafe", Switch).value is True
            scr.query_one("#fs-duty", Input).value = "70"
            scr.query_one("#fs-rate", Input).value = "40"
            scr.save_assignment()
            await pilot.pause()
            saved = read_cfg(temp_config)["assignments"]["CPU"]
            assert saved["failsafe_duty"] == 70
            assert saved["failsafe_after"] == 2
            assert saved["failsafe_max_rate"] == 40

    asyncio.run(run())


def test_blank_failsafe_fields_inherit_the_global(temp_config, machine):
    """Blank = no override, same rule as floor/ceiling/rate: the key leaves
    the file so the global (editable on screen 5) applies again."""
    _seed_assignments(temp_config,
                      CPU={"source": "k10temp:temp1", "curve": "default",
                           "fans": ["it8686:pwm1"], "enabled": True,
                           "failsafe_duty": 80})

    async def run():
        async with FanControlApp(config_path=temp_config,
                                 live=False).run_test(size=(120, 45)) \
                as pilot:
            await pilot.pause()
            scr = await _goto_assign(pilot)
            await edit_assignment_row(pilot, scr)
            scr.query_one("#fs-duty", Input).value = ""
            scr.save_assignment()
            await pilot.pause()
            assert "failsafe_duty" not in \
                read_cfg(temp_config)["assignments"]["CPU"]

    asyncio.run(run())


def test_turning_the_failsafe_off_asks_then_writes_false(temp_config, machine):
    """The opt-out is a one-switch way to remove a safety net from one fan
    (§18 is what that looks like on this board), so it is confirmed and stored
    explicitly as false rather than as an absent key."""

    async def run():
        async with FanControlApp(config_path=temp_config,
                                 live=False).run_test(size=(120, 45)) \
                as pilot:
            await pilot.pause()
            scr = await _goto_assign(pilot)
            await _pick_fan(pilot, scr, "it8686:pwm3")
            scr.query_one("#failsafe", Switch).value = False
            await _save_named_assignment(pilot, scr, "no-net")
            confirm = await wait_modal(pilot, ConfirmScreen)
            assert "HOLD their last duty" in " ".join(confirm.lines)
            await pilot.click("#confirm-ok")
            await wait_not_modal(pilot)
            assert read_cfg(temp_config)["assignments"]["no-net"][
                "failsafe"] is False

    asyncio.run(run())


def test_cancelling_keeps_the_failsafe_alone(temp_config, machine):
    async def run():
        async with FanControlApp(config_path=temp_config,
                                 live=False).run_test(size=(120, 45)) \
                as pilot:
            await pilot.pause()
            scr = await _goto_assign(pilot)
            await _pick_fan(pilot, scr, "it8686:pwm3")
            scr.query_one("#failsafe", Switch).value = False
            await _save_named_assignment(pilot, scr, "no-net")
            await wait_modal(pilot, ConfirmScreen)
            await pilot.click("#confirm-cancel")
            await wait_not_modal(pilot)
            assert "no-net" not in read_cfg(temp_config)["assignments"]

    asyncio.run(run())


def test_turning_it_back_on_removes_the_key(temp_config, machine):
    """On means inherit: the explicit false must not linger in the file."""
    _seed_assignments(temp_config,
                      CPU={"source": "k10temp:temp1", "curve": "default",
                           "fans": ["it8686:pwm1"], "enabled": True,
                           "failsafe": False})

    async def run():
        async with FanControlApp(config_path=temp_config,
                                 live=False).run_test(size=(120, 45)) \
                as pilot:
            await pilot.pause()
            scr = await _goto_assign(pilot)
            await edit_assignment_row(pilot, scr)
            assert scr.query_one("#failsafe", Switch).value is False
            scr.query_one("#failsafe", Switch).value = True
            scr.save_assignment()
            await pilot.pause()
            assert "failsafe" not in \
                read_cfg(temp_config)["assignments"]["CPU"]

    asyncio.run(run())


def test_fractional_duty_survives_the_form(temp_config, machine):
    """F12: the fields used to be int-only with max_length=3, so a config
    containing 59.5 (which the live one does) could not even be displayed
    without rounding it away."""
    _seed_assignments(temp_config,
                      CPU={"source": "k10temp:temp1", "curve": "default",
                           "fans": ["it8686:pwm1"], "enabled": True,
                           "floor_duty": 59.5})

    async def run():
        async with FanControlApp(config_path=temp_config,
                                 live=False).run_test(size=(120, 45)) \
                as pilot:
            await pilot.pause()
            scr = await _goto_assign(pilot)
            await edit_assignment_row(pilot, scr)
            assert scr.query_one("#floor", Input).value == "59.5"
            scr.query_one("#maxduty", Input).value = "97.5"
            scr.save_assignment()
            await pilot.pause()
            saved = read_cfg(temp_config)["assignments"]["CPU"]
            assert saved["floor_duty"] == 59.5
            assert saved["max_duty"] == 97.5

    asyncio.run(run())


# ------------------------------------------------- Overview depth (F10/F11) --

def _fan_row_cells(table, fan):
    for r in range(table.row_count):
        if row_key(table, r) == fan:
            return [str(table.get_cell_at((r, c)))
                    for c in range(len(table.columns))]
    raise AssertionError(f"no fan row {fan}")


def test_overview_lists_a_disabled_assignments_fans(temp_config, machine):
    """Disabling is the recommended safe action, and listing only enabled
    assignments made the header vanish at exactly the moment you wanted to
    watch it hand control back."""
    _seed_assignments(temp_config,
                      CPU={"source": "k10temp:temp1", "curve": "default",
                           "fans": ["it8686:pwm1"], "enabled": False})

    async def run():
        async with FanControlApp(config_path=temp_config,
                                 live=False).run_test(size=(140, 50)) \
                as pilot:
            await pilot.pause()
            scr = pilot.app.screen
            scr._refresh(machine.live_payload())
            table = scr.query_one("#fan-table", DataTable)
            cells = _fan_row_cells(table, "it8686:pwm1")
            assert any("CPU (disabled)" in c for c in cells), cells
            assert any("disabled — firmware holds it" in c
                       for c in cells), cells

    asyncio.run(run())


def test_overview_shows_the_degraded_flag_as_a_banner(temp_config, machine):
    """`degraded: true` means the daemon is holding fans at fail-safe duty
    because its config was rejected — the most important state on the box, and
    it used to be dropped on the floor."""

    async def run():
        async with FanControlApp(config_path=temp_config,
                                 live=False).run_test(size=(140, 50)) \
                as pilot:
            await pilot.pause()
            scr = pilot.app.screen
            data = machine.live_payload()
            data["status"] = {"ts": time.time(), "degraded": True,
                              "errors": {"startup_config": "bad json"}}
            scr._refresh(data)
            line = scr.query_one("#degraded-line", Static)
            assert "DEGRADED" in str(line.render())
            assert line.display
            # errors get their own lines, keyed
            errs = str(scr.query_one("#errors-box", Static).render())
            assert "startup_config: bad json" in errs

    asyncio.run(run())


def test_overview_error_lines_are_separate_and_hidden_when_clean(
        temp_config, machine):
    """One joined line scrolled past unreadable on a box with four GPUs; each
    problem now gets a line, and nothing shows when there are none."""

    async def run():
        async with FanControlApp(config_path=temp_config,
                                 live=False).run_test(size=(140, 50)) \
                as pilot:
            await pilot.pause()
            scr = pilot.app.screen
            data = machine.live_payload()
            data["status"] = {"ts": time.time(), "errors": {
                "nvidia_smi": "3 consecutive failure(s)",
                "stalled:it8686:pwm1": "commanding 80% but 0 RPM",
                "duty-drift:it8686:pwm3": "register says 12, we asked 60"}}
            scr._refresh(data)
            errs = str(scr.query_one("#errors-box", Static).render())
            for frag in ("nvidia_smi:", "stalled:it8686:pwm1:",
                         "duty-drift:it8686:pwm3:"):
                assert frag in errs, errs
            assert "\n" in errs
            # clean status hides it again
            data["status"] = {"ts": time.time(), "errors": {}}
            scr._refresh(data)
            assert not scr.query_one("#errors-box", Static).display

    asyncio.run(run())


def test_overview_state_shows_the_kick_the_daemon_is_running(
        temp_config, machine):
    """§21's comment asks for exactly this on screen: fighting for a fan is
    not the same as reporting that it is stalled."""
    _seed_assignments(temp_config,
                      CPU={"source": "k10temp:temp1", "curve": "default",
                           "fans": ["it8686:pwm1"], "enabled": True})

    async def run():
        async with FanControlApp(config_path=temp_config,
                                 live=False).run_test(size=(140, 50)) \
                as pilot:
            await pilot.pause()
            scr = pilot.app.screen
            data = machine.live_payload()
            data["status"] = {"ts": time.time(), "fans": {
                "it8686:pwm1": {"state": "stalled",
                                "kick": {"phase": "stop", "attempts": 2}}}}
            scr._refresh(data)
            cells = _fan_row_cells(scr.query_one("#fan-table", DataTable),
                                   "it8686:pwm1")
            assert any("STALLED" in c and "kicking stop attempt 2" in c
                       for c in cells), cells

    asyncio.run(run())


def test_duty_cell_shows_the_register_when_it_disagrees():
    """§20: commanded duty is not ownership. The raw value is published on
    every row; showing it only on drift keeps the table readable."""
    from alx_fancontrol.tui.screens.overview import OverviewScreen as O

    assert O._duty_cell({"duty_pct": 20, "duty_raw": 51}) == "20"
    assert O._duty_cell({"duty_pct": 20, "duty_raw": 120}) == "20 (raw 120!)"
    assert O._duty_cell({}) == "—"


def test_sidebar_says_which_config_this_session_edits(temp_config, machine):
    """F11: root edits /etc and a dev shell edits ~/.config; until now the
    TUI never said which one it was writing, so a sudo-less session could
    produce a valid config that changes nothing."""

    async def run():
        async with FanControlApp(config_path=temp_config,
                                 live=False).run_test(size=(140, 50)) \
                as pilot:
            await pilot.pause()
            text = str(pilot.app.screen.query_one("#cfg-path",
                                                  Static).render())
            assert "config:" in text and "config.json" in text, text

    asyncio.run(run())
