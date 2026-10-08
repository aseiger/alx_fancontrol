"""FanControlApp: textual shell — sidebar nav, live-refresh worker,
single save path with stale-write guard.

The TUI never writes to hwmon (read-only); the only file it writes is the
config (atomically, with .bak). `config_path` is injectable so tests use a
tmp copy and never touch the real user config.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from textual.app import App
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Label

from alx_fancontrol import config as config_mod
from alx_fancontrol import hwmon
from alx_fancontrol import sources as sources_mod
from alx_fancontrol.tui.reader import LiveData, LiveReader, LiveUpdate
from alx_fancontrol.tui.screens.assign import AssignScreen
from alx_fancontrol.tui.screens.curves import CurveEditorScreen
from alx_fancontrol.tui.screens.overview import OverviewScreen
from alx_fancontrol.tui.screens.sources import SourcesScreen


class StaleConfigScreen(ModalScreen[bool]):
    """The config file changed on disk since the TUI loaded it (or since
    the last save): Reload (discard in-TUI edits) or Overwrite."""

    DEFAULT_CSS = """
    StaleConfigScreen {
        align: center middle;
    }
    StaleConfigScreen #stale-box {
        width: 58;
        height: auto;
        border: tall red;
        padding: 1 2;
    }
    StaleConfigScreen #stale-box Button {
        margin-top: 1;
        width: 22;
    }
    """

    def compose(self):
        with Vertical(id="stale-box"):
            yield Label("Config changed externally since the TUI loaded it.")
            yield Label("Reload (discard in-TUI edits) or Overwrite it?")
            yield Button("Reload", variant="primary", id="stale-reload")
            yield Button("Overwrite", variant="error", id="stale-overwrite")

    def on_button_pressed(self, event):
        if event.button.id == "stale-reload":
            self.dismiss(False)
        elif event.button.id == "stale-overwrite":
            self.dismiss(True)


class FanControlApp(App):
    """TUI shell. Keys 1-4 switch screens; a worker thread feeds live data
    every ~1 s (live=False in tests for speed/hermeticity)."""

    TITLE = "alx_fancontrol"
    BINDINGS = [
        Binding("1", "show_overview", "Overview"),
        Binding("2", "show_assign", "Assign"),
        Binding("3", "show_curves", "Curves"),
        Binding("4", "show_sources", "Sources"),
    ]
    CSS = """
    #shell {
        height: 1fr;
    }
    #sidebar {
        width: 26;
        height: 100%;
        border-right: tall;
        background: $surface;
        padding: 0 1;
    }
    /* Nav items are non-focusable NavButtons: selection is the `active`
       class (set per screen), never a focus ring. */
    #sidebar Button {
        width: 100%;
        margin-bottom: 1;
        border: none;
        background: transparent;
        color: $text-muted;
        text-style: none;
        text-align: left;
        padding: 0 1;
    }
    #sidebar Button:hover {
        background: $surface-darken-1;
        color: $text;
    }
    #sidebar Button.active {
        background: $primary-muted;
        color: $text-primary;
        text-style: bold;
    }
    #sidebar Button.active:hover {
        background: $primary;
        color: $text;
    }
    #sidebar #prot-hint {
        color: $text-muted;
        margin-top: 2;
    }
    #sidebar #root-warn {
        color: $error;
        text-style: bold;
        margin-bottom: 1;
    }
    #sidebar #prot-hint .prot {
        color: $text-disabled;
    }
    #content {
        height: 100%;
        width: 1fr;
        padding: 0 1;
        /* a pane taller than the terminal scrolls instead of clipping
           (short terminals) */
        overflow: auto auto;
    }
    .title {
        text-style: bold;
        margin: 0 0 1 0;
    }
    .hint {
        color: $text-muted;
        margin-bottom: 1;
    }
    """

    def __init__(self, config_path=None, live: bool = True):
        super().__init__()
        self.cfg_path = (Path(config_path) if config_path
                         else config_mod.default_path())
        self.live_enabled = live
        # Non-root cannot take over fans (pwm writes are root-only) and
        # edits the home config instead of /etc — the user must be told.
        self.is_root = os.geteuid() == 0
        self.cfg: config_mod.Config | None = None
        self._cfg_mtime: float | None = None
        self.labels = hwmon.load_sensorsd_labels()
        self.chips = hwmon.find_all_chips()
        # Sources are discovered at runtime (machine property, not config);
        # the Assign picker and Sources screen show these.
        self.sources = sources_mod.discover(self.chips, labels=self.labels)
        self.live: dict = {}
        self._reader = LiveReader(self)

    # -------------------------------------------------------------- mount
    def on_mount(self):
        if not self.cfg_path.exists():
            self.cfg = config_mod.seed_if_missing(self.cfg_path)
        else:
            self.cfg = config_mod.load(self.cfg_path)
        self._maybe_upgrade_file()
        self._refresh_mtime()
        self.push_screen(OverviewScreen())
        if self.live_enabled:
            self._reader.start()

    def _maybe_upgrade_file(self):
        """One-time in-place upgrade: a v1 file (with a 'sources' section)
        is re-saved as v2 on first TUI open (assignment source ids already
        rewritten to canonical ids by load; the v1 file is kept as .bak).
        Sources are detected at runtime from now on."""
        try:
            raw = json.loads(self.cfg_path.read_text())
        except (OSError, ValueError):
            return
        if isinstance(raw, dict) and int(raw.get("version", 1)) \
                < config_mod.CONFIG_VERSION:
            config_mod.save(self.cfg, self.cfg_path)
            self._refresh_mtime()
            self.notify(f"config upgraded to v{config_mod.CONFIG_VERSION} — "
                        "sources are now detected at runtime", timeout=6)

    def on_unmount(self):
        self._reader.stop()

    def _refresh_mtime(self):
        try:
            self._cfg_mtime = self.cfg_path.stat().st_mtime
        except OSError:
            self._cfg_mtime = None

    # ---------------------------------------------------------------- nav
    def show_overview(self):
        self.switch_screen(OverviewScreen())

    def show_assign(self):
        self.switch_screen(AssignScreen())

    def show_curves(self):
        self.switch_screen(CurveEditorScreen())

    def show_sources(self):
        self.switch_screen(SourcesScreen())

    def action_show_overview(self):
        self.show_overview()

    def action_show_assign(self):
        self.show_assign()

    def action_show_curves(self):
        self.show_curves()

    def action_show_sources(self):
        self.show_sources()

    # --------------------------------------------------------------- live
    def on_live_data(self, msg: LiveData):
        self.live = msg.data
        screen = self.screen
        if screen is not self:
            screen.post_message(LiveUpdate(msg.data))

    # ------------------------------------------------------------ helpers
    def fan_label(self, fan: str) -> str:
        parsed = hwmon.parse_fan_id(fan)
        if parsed is None:
            return fan
        return hwmon.sensor_label(parsed[0], "pwm", parsed[1], self.labels)

    def tach_for(self, fan: str) -> str | None:
        for a in self.cfg.assignments.values():
            tach = a.get("tach") or {}
            if tach.get(fan):
                return tach[fan]
        return None

    def guess_tach(self, fan: str) -> str | None:
        """chip:pwmN → chip:fanN when sensors.d labels both the same
        (true for this board's SYS_FAN headers). Used to show live RPMs
        for fans that have no assignment (and thus no explicit tach) yet."""
        parsed = hwmon.parse_fan_id(fan)
        if parsed is None:
            return None
        chip, pwm = parsed
        lab = self.labels
        pwm_lab = lab.get(chip, {}).get(f"pwm{pwm}")
        fan_lab = lab.get(chip, {}).get(f"fan{pwm}")
        if pwm_lab and pwm_lab == fan_lab:
            return f"{chip}:fan{pwm}"
        return None

    # --------------------------------------------------------------- save
    def save_config(
            self,
            message: str = "Saved — running daemon reloads within ~1 s",
    ) -> bool:
        """THE single save path: stale guard (mtime) → config.save()
        (atomic + .bak) → notification. Returns False when a stale-write
        modal is shown instead of saving directly (textual 8 requires
        push_screen_wait to run in a worker, so the modal flow is one)."""
        try:
            m = self.cfg_path.stat().st_mtime
        except OSError:
            m = None
        if m != self._cfg_mtime:
            self.run_worker(self._stale_modal_flow(), exclusive=False)
            return False
        config_mod.save(self.cfg, self.cfg_path)
        self._refresh_mtime()
        self.notify(message)
        return True

    async def _stale_modal_flow(self):
        result = await self.push_screen_wait(StaleConfigScreen())
        if result:  # Overwrite: write the in-memory config anyway
            config_mod.save(self.cfg, self.cfg_path)
            self.notify("Config overwritten")
        else:  # Reload: discard in-TUI edits, take the file back
            try:
                self.cfg = config_mod.load(self.cfg_path)
            except (OSError, ValueError) as e:
                self.notify(f"Reload failed: {e}", severity="error")
                return
            self.notify("Config reloaded from disk")
        self._refresh_mtime()
