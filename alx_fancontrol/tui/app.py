"""FanControlApp: textual shell — sidebar nav, live-refresh worker,
single save path with validation and a stale-write guard.

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
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Label

from alx_fancontrol import config as config_mod
from alx_fancontrol import hwmon
from alx_fancontrol import sources as sources_mod
from alx_fancontrol import status as status_mod
from alx_fancontrol.tui.reader import LiveData, LiveReader, LiveUpdate
from alx_fancontrol.tui.screens.assign import AssignScreen
from alx_fancontrol.tui.screens.curves import CurveEditorScreen
from alx_fancontrol.tui.screens.overview import OverviewScreen
from alx_fancontrol.tui.screens.settings import SettingsScreen
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


class ConfigErrorsScreen(ModalScreen[None]):
    """A save that would write a config the running daemon refuses is
    blocked here, with the reason from `config.validate()` on screen.

    Without this the file lands anyway, the daemon's hot reload rejects it,
    keeps its previous config, and the only trace is one fragment of the
    Overview status line — "Saved" followed by silence is the worst failure
    mode a tool that drives fans can have."""

    DEFAULT_CSS = """
    ConfigErrorsScreen {
        align: center middle;
    }
    ConfigErrorsScreen #cfgerr-box {
        width: 76;
        height: auto;
        max-height: 85%;
        border: tall red;
        padding: 1 2;
    }
    ConfigErrorsScreen #cfgerr-box Button {
        margin-top: 1;
        width: 24;
    }
    """
    BINDINGS = [Binding("enter", "close", "OK"),
                Binding("escape", "close", "OK")]

    def __init__(self, errors):
        super().__init__()
        self.errors = list(errors)

    def compose(self):
        with VerticalScroll(id="cfgerr-box"):
            yield Label(f"Not saved — the daemon would refuse this config "
                        f"({len(self.errors)} problem(s)):")
            for e in self.errors[:15]:
                yield Label(f"  • {e}")
            if len(self.errors) > 15:
                yield Label(f"  … and {len(self.errors) - 15} more "
                            "(run: alx-fancontrol check)")
            if any(e.startswith("config.") for e in self.errors):
                yield Label("Global settings cannot be edited in the TUI "
                            f"yet — fix them in {self.app.cfg_path} and save "
                            "again (the previous file is kept as .bak).")
            yield Button("Back to editing", id="cfgerr-ok", variant="primary")

    def on_button_pressed(self, event):
        if event.button.id == "cfgerr-ok":
            self.dismiss(None)

    def action_close(self):
        self.dismiss(None)


class FanControlApp(App):
    """TUI shell. Keys 1-5 switch screens; a worker thread feeds live data
    every ~1 s (live=False in tests for speed/hermeticity)."""

    TITLE = "alx_fancontrol"
    BINDINGS = [
        Binding("1", "show_overview", "Overview"),
        Binding("2", "show_assign", "Assign"),
        Binding("3", "show_curves", "Curves"),
        Binding("4", "show_sources", "Sources"),
        Binding("5", "show_settings", "Settings"),
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
    #sidebar #root-warn {
        color: $error;
        text-style: bold;
        margin-bottom: 1;
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
        #: Settings screen's unsaved edits, held HERE rather than on the
        #: screen: switching screens builds a new one, and numbers typed for
        #: `stall_kick_backoff_s` must not evaporate because someone pressed
        #: 2 to look at an assignment. Deliberately separate from `cfg`, so
        #: nothing gets written until Save on that screen (see settings.py).
        self.settings_edits: dict = {}
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

    def show_settings(self):
        self.switch_screen(SettingsScreen())

    def action_show_overview(self):
        self.show_overview()

    def action_show_assign(self):
        self.show_assign()

    def action_show_curves(self):
        self.show_curves()

    def action_show_sources(self):
        self.show_sources()

    def action_show_settings(self):
        self.show_settings()

    # ------------------------------------------------------------- reload
    def reload_from_disk(self) -> bool:
        """Throw away in-memory edits and take the file back (Settings'
        Revert, and the stale-config 'Reload' path). False with a notification
        when the file cannot be read — the in-memory config is then left
        alone, because there is nothing better to replace it with."""
        try:
            self.cfg = config_mod.load(self.cfg_path)
        except (OSError, ValueError) as e:
            self.notify(f"Reload failed: {e}", severity="error")
            return False
        self._refresh_mtime()
        self.notify("Config reloaded from disk")
        return True

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

    # Owner states, shortest honest words that fit a table column. The point
    # of showing them at pick time (audit F5) is that `force_takeover` is not
    # a guess-able setting: whether the daemon will accept a header depends on
    # who holds it NOW, and §18/§20 are what happens when that is invisible.
    OWNER_FIRMWARE = "firmware"    # enable=2: free, takeover is clean
    OWNER_MINE = "ours"            # enable=1 and the running daemon holds it
    OWNER_STALE = "left by us"     # enable=1, marker says our dead run left it
    OWNER_OTHER = "other"          # enable=1 held by someone else: fancontrol,
                                   # EC, BIOS — it will keep taking the fan back
    OWNER_PARKED = "parked"        # enable=0: disabled — cools nothing (§20)
    OWNER_UNKNOWN = "?"

    def fan_owner(self, fan: str, data: dict | None = None) -> str:
        """Who holds `pwmN_enable` on this header right now.

        Read from the live sweep (`enable`) plus the two ownership sources
        `status.claimed_channels` reconciles. `data` takes the snapshot to
        read (a screen refreshing itself from a LiveUpdate message must use
        that message, not whatever the app happened to store last).

        This is the same question `daemon._takeover()` asks before it writes:
        `other` and `parked` are exactly the channels it REFUSES unless the
        assignment sets `force_takeover`, so the screen can warn before a save
        instead of one screen later showing the bare word `manual-conflict`.
        """
        live = data if data is not None else (self.live or {})
        entry = (live.get("fans") or {}).get(fan) or {}
        en = entry.get("enable")
        if en is None:
            return self.OWNER_UNKNOWN
        if en == hwmon.ENABLE_FIRMWARE:
            return self.OWNER_FIRMWARE
        if en == hwmon.ENABLE_DISABLED:
            return self.OWNER_PARKED
        if en != hwmon.ENABLE_MANUAL:
            return self.OWNER_UNKNOWN
        # manual mode: ours (running daemon), ours-but-dead (reclaimable),
        # or somebody else's
        st = live.get("status") or {}
        held = (st.get("fans") or {}) if status_mod.daemon_state(st) \
            != "absent" else {}
        if fan in held:
            return self.OWNER_MINE
        if fan in ((live.get("owned") or {}).get("stale") or []):
            return self.OWNER_STALE
        return self.OWNER_OTHER

    def needs_force_takeover(self, fans, data: dict | None = None) -> list[str]:
        """Picked headers the daemon will refuse to take over as configured."""
        return [f for f in sorted(fans)
                if self.fan_owner(f, data) in (self.OWNER_OTHER,
                                               self.OWNER_PARKED)]

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
        """THE single save path: validate → stale guard (mtime) →
        config.save() (atomic + .bak) → notification. Returns False when
        nothing was written — either a validation failure (modal with the
        reason) or a stale-write modal is shown instead of saving directly
        (textual 8 requires push_screen_wait to run in a worker, so the
        modal flow is one).

        Validating here matters because of how the daemon consumes the file:
        ANY error makes its hot reload keep the previous config
        (`daemon._maybe_reload`), so writing an invalid one is not "a save
        that has not taken effect yet" — it is a save that does nothing,
        silently, while the UI says Saved."""
        errors, _warnings = config_mod.validate(self.cfg)
        if errors:
            # warnings are deliberately not shown here: they are conditions
            # the daemon handles at runtime (unknown curve, shared fan) and
            # the screens that create them already explain them
            self.push_screen(ConfigErrorsScreen(errors))
            return False
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
