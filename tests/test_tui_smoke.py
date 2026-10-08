"""TUI smoke: textual run_test pilot over all 4 screens + save plumbing.

The app is constructed with a throwaway tmp config (seeded from a fake
hwmon tree) so the pilot never touches the real user config or writes to
real hwmon. live=False keeps it fast/hermetic (the manual smoke script
alx_fancontrol.tui.smoke uses live=True).
"""
import asyncio
import json
import os
import time

import pytest

from alx_fancontrol import config as config_mod
from alx_fancontrol.tui.app import FanControlApp, StaleConfigScreen
from alx_fancontrol.tui.screens.assign import AssignScreen
from alx_fancontrol.tui.screens.curves import CurveEditorScreen, \
    render_curve_plot
from alx_fancontrol.tui.screens.overview import OverviewScreen
from alx_fancontrol.tui.screens.sources import SourcesScreen


@pytest.fixture(autouse=True)
def _machine(machine):
    """Hermetic: run the app against the shared synthetic machine
    (tests/conftest.py) instead of the real hwmon — the suite passes on
    any machine, with or without GPUs."""
    return machine


@pytest.fixture
def temp_config(machine):
    """Empty config next to the synthetic machine, so seeding/validation
    never touches real state."""
    cfg_path = machine.root.parent / "config.json"
    config_mod.seed_if_missing(cfg_path)
    return cfg_path


def _bump_mtime(path):
    atime_ns = os.stat(path).st_atime_ns
    mtime_ns = int((time.time() + 1000) * 1e9)
    os.utime(path, ns=(atime_ns, mtime_ns))


def test_all_screens_mount_and_nav(temp_config):
    async def run():
        async with FanControlApp(config_path=temp_config,
                                 live=False).run_test(size=(110, 32)) as pilot:
            await pilot.pause()
            app = pilot.app
            assert isinstance(app.screen, OverviewScreen)
            for key, cls in [("2", AssignScreen), ("3", CurveEditorScreen),
                             ("4", SourcesScreen), ("1", OverviewScreen)]:
                await pilot.press(key)
                await pilot.pause()
                assert isinstance(app.screen, cls), \
                    f"key {key}: expected {cls.__name__}, got {app.screen!r}"
            # sidebar buttons work too
            await pilot.click("#nav-3")
            await pilot.pause()
            assert isinstance(app.screen, CurveEditorScreen)

    asyncio.run(run())


def test_save_roundtrip_and_bak(temp_config):
    async def run():
        async with FanControlApp(config_path=temp_config,
                                 live=False).run_test(size=(110, 32)) as pilot:
            await pilot.pause()
            app = pilot.app
            before = temp_config.read_text()
            app.cfg.assignments["smoke-assign"] = {
                "source": "k10temp[0]:temp1", "curve": "default",
                "fans": ["it8686:pwm3"], "enabled": True,
            }
            assert app.save_config()
            after = temp_config.read_text()
            assert after != before
            doc = json.loads(after)
            assert doc["assignments"]["smoke-assign"]["fans"] == \
                ["it8686:pwm3"]
            bak = temp_config.with_name(temp_config.name + ".bak")
            assert bak.exists()
            assert bak.read_text() == before  # .bak = PREVIOUS content
            app.cfg.config["poll_seconds"] = 2.0
            assert app.save_config()
            assert bak.read_text() == after
            assert json.loads(temp_config.read_text())["config"][
                "poll_seconds"] == 2.0

    asyncio.run(run())


def test_stale_write_guard_overwrite(temp_config):
    async def run():
        async with FanControlApp(config_path=temp_config,
                                 live=False).run_test(size=(110, 32)) as pilot:
            await pilot.pause()
            app = pilot.app
            app.cfg.assignments["new-one"] = {
                "source": "k10temp[0]:temp1", "curve": "default",
                "fans": ["it8686:pwm3"], "enabled": True,
            }
            _bump_mtime(temp_config)  # simulate an external edit
            assert app.save_config() is False  # modal path, not a direct save
            await pilot.pause()
            assert isinstance(app.screen, StaleConfigScreen)
            await app.screen.dismiss(True)  # Overwrite
            await pilot.pause()  # let the modal worker finish
            doc = json.loads(temp_config.read_text())
            assert "new-one" in doc["assignments"]  # overwrite won

    asyncio.run(run())


def test_stale_write_guard_reload(temp_config):
    async def run():
        async with FanControlApp(config_path=temp_config,
                                 live=False).run_test(size=(110, 32)) as pilot:
            await pilot.pause()
            app = pilot.app
            external = json.loads(temp_config.read_text())
            external["config"]["poll_seconds"] = 9.9
            temp_config.write_text(json.dumps(external))
            _bump_mtime(temp_config)
            app.cfg.assignments["in-memory-only"] = {
                "source": "k10temp[0]:temp1", "curve": "default",
                "fans": ["it8686:pwm3"], "enabled": True,
            }
            assert app.save_config() is False  # modal path
            await pilot.pause()
            assert isinstance(app.screen, StaleConfigScreen)
            await app.screen.dismiss(False)  # Reload
            await pilot.pause()  # let the modal worker finish
            # in-memory edit discarded, file content authoritative
            assert "in-memory-only" not in app.cfg.assignments
            assert app.cfg.config["poll_seconds"] == 9.9

    asyncio.run(run())


def test_render_curve_plot():
    pts = [[40, 20], [55, 30], [65, 70], [75, 100]]
    t = render_curve_plot(pts, 60.0)
    s = str(t)
    assert "█" in s           # curve columns
    assert "◄" in s           # live temp marker
    assert s.count("\n") == 16  # 16 plot rows (axis row has no trailing \n)

    t0 = render_curve_plot(pts, None)
    assert "◄" not in str(t0)

    tempty = render_curve_plot([], None)
    assert "no points" in str(tempty)

    # single-point curve doesn't crash the axis math
    t1 = render_curve_plot([[50, 42]], 50.0)
    assert "█" in str(t1)


def test_curve_editor_bindings_nudge(temp_config):
    """a/d and nudge bindings mutate the working points (no save)."""
    async def run():
        async with FanControlApp(config_path=temp_config,
                                 live=False).run_test(size=(110, 32)) as pilot:
            await pilot.pause()
            app = pilot.app
            await pilot.press("3")
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, CurveEditorScreen)
            n0 = len(screen.pts)
            screen.pts.append([90.0, 50.0])  # out-of-range temp, distinct
            screen._refresh()
            assert len(screen.pts) == n0 + 1
            # nudge duty of the selected (first) point
            first = screen._sorted_pts()[0]
            expected = first[1] + 5  # capture BEFORE the nudge mutates `first`
            screen._sel_point = list(first)
            screen.action_nudge_duty_up5()
            assert screen._selected()[1] == expected
            screen.action_delete_point()
            assert len(screen.pts) == n0

    asyncio.run(run())
