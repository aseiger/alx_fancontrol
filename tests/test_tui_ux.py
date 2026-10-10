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
import os
import traceback

import pytest
from textual.coordinate import Coordinate
from textual.widgets import DataTable, Input, Select, Static

from alx_fancontrol import config as config_mod
from alx_fancontrol import hwmon
from alx_fancontrol.tui.app import FanControlApp
from alx_fancontrol.tui.reader import LiveUpdate
from alx_fancontrol.tui.screens.assign import (AssignScreen,
                                               ConfirmDeleteScreen)
from alx_fancontrol.tui.screens.base import sel_value
from alx_fancontrol.tui.screens.curves import (CurveEditorScreen,
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


def read_cfg(path) -> dict:
    return json.loads(path.read_text())


async def wait_not_modal(pilot, what: str = "modal") -> None:
    """Wait until the top screen is no longer a modal (8.2.8's Pilot has
    no wait(predicate)); the point-add worker applies its result right
    after dismiss, so a few idle cycles are enough."""
    for _ in range(50):
        await pilot.pause()
        if not isinstance(pilot.app.screen, (PointPromptModal,)):
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
            assert table.get_cell_at((fans.index("it8686:pwm1"), 3)) == "1650"
            assert table.get_cell_at((fans.index("it8686:pwm3"), 3)) == "4680"
            assert table.get_cell_at((fans.index("it8686:pwm2"), 3)) == "—"
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


def test_add_point_requires_selected_row(temp_config, machine, notices):
    """'a' with an empty curve (nothing selected) warns instead of
    opening a prompt."""
    from alx_fancontrol import config as _cfg_mod
    cfg = _cfg_mod.load(temp_config)
    cfg.curves["empty"] = {"label": "Empty", "points": []}
    _cfg_mod.save(cfg, temp_config)

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
            await pilot.pause()
            assert isinstance(pilot.app.screen, CurveEditorScreen)
            assert any("select a point" in m.lower()
                       for _s, m in notices), notices

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
