"""textual run_test pilot over all 4 screens.

Run:  .venv/bin/python -m alx_fancontrol.tui.smoke

Mounts Overview, navigates 1-4 across all screens, and exercises one save
round-trip against a THROWAWAY temp config (seeded from a tiny fake hwmon
tree — the real user config is never touched). Uses live data by default
(read-only hwmon/nvidia reads); pass --no-live for a hermetic pilot.
"""
from __future__ import annotations

import asyncio
import json
import shutil
import sys
import tempfile
from pathlib import Path

from alx_fancontrol import config as config_mod


def make_temp_config(tmp: Path) -> Path:
    """Seed a throwaway config from a fake hwmon tree (hermetic sources)."""
    root = tmp / "hwmon"

    def chip(idx, name, **files):
        d = root / f"hwmon{idx}"
        d.mkdir(parents=True)
        (d / "name").write_text(name + "\n")
        for k, v in files.items():
            (d / k).write_text(str(v) + "\n")

    chip(0, "k10temp", temp1_input="50000")
    chip(1, "it8686_2008090d", temp1_input="45000", fan3_input="3000",
         pwm3="70", pwm3_enable="2")
    chip(2, "it8792_2008090d", pwm1="140", pwm1_enable="1",
         pwm3="140", pwm3_enable="1")
    cfg_path = tmp / "config.json"
    config_mod.seed_if_missing(cfg_path)
    return cfg_path


async def run_pilot(live: bool) -> None:
    from alx_fancontrol.tui.app import FanControlApp
    from alx_fancontrol.tui.screens.assign import AssignScreen
    from alx_fancontrol.tui.screens.curves import CurveEditorScreen
    from alx_fancontrol.tui.screens.overview import OverviewScreen
    from alx_fancontrol.tui.screens.sources import SourcesScreen

    tmp = Path(tempfile.mkdtemp(prefix="alx-fc-smoke-"))
    try:
        cfg_path = make_temp_config(tmp)
        async with FanControlApp(config_path=cfg_path,
                                 live=live).run_test(size=(110, 32)) as pilot:
            await pilot.pause()
            app = pilot.app
            assert isinstance(app.screen, OverviewScreen), \
                f"expected OverviewScreen, got {app.screen!r}"
            print("ok: OverviewScreen mounted")
            for key, cls in [("2", AssignScreen), ("3", CurveEditorScreen),
                             ("4", SourcesScreen), ("1", OverviewScreen)]:
                await pilot.press(key)
                await pilot.pause()
                assert isinstance(app.screen, cls), \
                    f"key {key}: expected {cls.__name__}, got {app.screen!r}"
                print(f"ok: key {key} -> {cls.__name__}")

            # one save round-trip on the temp config
            before = cfg_path.read_text()
            app.cfg.assignments["smoke-assign"] = {
                "source": "cpu0", "curve": "default",
                "fans": ["it8686:pwm3"],
                "tach": {"it8686:pwm3": "it8686:fan3"},
                "enabled": True,
            }
            assert app.save_config()
            after = cfg_path.read_text()
            assert after != before and "smoke-assign" in after
            doc = json.loads(after)
            assert doc["assignments"]["smoke-assign"]["fans"] == \
                ["it8686:pwm3"]
            bak = cfg_path.with_name(cfg_path.name + ".bak")
            assert bak.exists() and bak.read_text() == before
            # second save: .bak now holds the first save's content
            app.cfg.config["poll_seconds"] = 2.0
            assert app.save_config()
            assert bak.read_text() == after
            assert json.loads(cfg_path.read_text())["config"]["poll_seconds"] \
                == 2.0
            print("ok: save round-trip + .bak on temp config")
        print("SMOKE-OK" + (" (live data)" if live else " (no live reader)"))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main(argv) -> int:
    asyncio.run(run_pilot(live="--no-live" not in argv))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
