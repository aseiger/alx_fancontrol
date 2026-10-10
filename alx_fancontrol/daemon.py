"""alx_fancontrol daemon.

Poll loop: read sources (ONE nvidia-smi call per poll serves all GPU
sources) -> eval piecewise-linear curves -> rate-limit -> rate-limited PWM
writes, with takeover/restore. Hot-reloads the config on mtime change
every poll (atomic replace on the writer side guarantees no torn reads).
Firmware control is restored exactly in `finally` on clean exit
(SIGTERM/SIGINT set the stop flag).

Safety guardrails (enforced in code, see also scripts/protect.py):
- A channel with pwmN_enable=1 (already manual — owned by another
  process, e.g. the gpu-fanctl service) is refused at takeover; =0 is
  skipped.
- Takeover only from pwmN_enable=2 (firmware); the original value is
  recorded and restored EXACTLY on exit.
- --dry-run performs NO writes at all (no enable, no duty); it still runs
  every guardrail check and logs the intended actions.

The daemon never writes the config file — only the TUI (and humans) do.
"""
from __future__ import annotations

import argparse
import logging
import logging.handlers
import os
import signal
import sys
import time
from pathlib import Path

from alx_fancontrol import config as config_mod
from alx_fancontrol import curve, hwmon, sources as sources_mod
from alx_fancontrol import status as status_mod

EXIT_OK = 0
EXIT_HARD_ERROR = 2


class Daemon:
    def __init__(self, cfg_path, dry_run: bool = False, once: bool = False,
                 duration: float | None = None, root=hwmon.HWMON_ROOT,
                 gpu_runner=None, log_path=None, verbose: bool = False):
        self.cfg_path = Path(cfg_path)
        self.dry_run = dry_run
        self.once = bool(once)
        self.duration = duration
        self.root = root
        self.gpu_runner = gpu_runner
        self.log_path = Path(log_path) if log_path else None
        self.verbose = verbose

        self._stop = False
        self._chips: list[tuple[str, Path]] = []
        self._sources: list = []
        self._source_ids: set[str] = set()
        self._labels = hwmon.load_sensorsd_labels()
        self._cfg = None
        self._cfg_mtime: float | None = None
        self._reload_error: str | None = None

        self._taken_over: set[str] = set()
        self._orig_enable: dict[str, int] = {}
        self._duty: dict[str, int] = {}          # fan -> last successful duty %
        self._fan_state: dict[str, str] = {}
        self._temps: dict[str, float | None] = {}
        self._missing_count: dict[str, int] = {}
        self._write_fail_count: dict[str, int] = {}
        self._gpu_fail_count = 0
        self._gpu_warned = False
        self._t0: float | None = None

        self.log = self._setup_logging()

    # ------------------------------------------------------------ setup --

    def _setup_logging(self) -> logging.Logger:
        log = logging.getLogger("alx_fancontrol.daemon")
        log.setLevel(logging.DEBUG if self.verbose else logging.INFO)
        log.handlers.clear()
        fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s")
        sh = logging.StreamHandler(sys.stderr)
        sh.setFormatter(fmt)
        log.addHandler(sh)
        if self.log_path is not None:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            fh = logging.handlers.RotatingFileHandler(
                self.log_path, maxBytes=256 * 1024, backupCount=3)
            fh.setFormatter(fmt)
            log.addHandler(fh)
        return log

    def _on_signal(self, signum, _frame):
        self.log.info("received signal %d — stopping after this poll", signum)
        self._stop = True

    # -------------------------------------------------------------- run --

    def run(self) -> int:
        signal.signal(signal.SIGINT, self._on_signal)
        signal.signal(signal.SIGTERM, self._on_signal)
        cfg = self._initial_load()
        if cfg is None:
            return EXIT_HARD_ERROR
        self._log_resolved(cfg)
        self._initial_takeover(cfg)
        self._t0 = time.time()
        self.log.info(
            "daemon started (pid %d, %s, poll %.2fs, once=%s, duration=%s)",
            os.getpid(), "DRY-RUN: no writes" if self.dry_run else "LIVE",
            cfg.config.get("poll_seconds", 1.0), self.once,
            f"{self.duration:.0f}s" if self.duration is not None else "none")
        try:
            while not self._stop:
                cfg = self._maybe_reload(cfg)
                self._poll(cfg)
                self._write_status(cfg)
                if self.once:
                    break
                if self.duration is not None \
                        and time.time() - self._t0 >= self.duration:
                    self.log.info("duration %gs reached — stopping", self.duration)
                    break
                time.sleep(cfg.config.get("poll_seconds", 1.0))
        finally:
            self._restore_all()
            self.log.info("daemon stopped")
        return EXIT_OK

    def _initial_load(self):
        try:
            cfg = config_mod.load(self.cfg_path)
        except (OSError, ValueError) as e:
            self.log.error("cannot load config %s: %s — aborting", self.cfg_path, e)
            return None
        errors, warnings = config_mod.validate(cfg)
        for w in warnings:
            self.log.warning("config: %s", w)
        if errors:
            for e in errors:
                self.log.error("config: %s", e)
            self.log.error("%d hard config error(s) — aborting", len(errors))
            return None
        self._cfg = cfg
        try:
            self._cfg_mtime = os.stat(self.cfg_path).st_mtime
        except OSError:
            pass
        self._discover_sources()
        return cfg

    def _discover_sources(self):
        """Sources are a machine property: detect them, don't configure
        them. Runs at start and on every config reload (hwmon indices can
        reshuffle). If nvidia-smi fails at discovery time there are simply
        no GPU sources (assignments referencing them skip with
        source-missing); a later reload re-detects."""
        self._chips = hwmon.find_all_chips(self.root)
        try:
            gpus = hwmon.gpu_temps(self.gpu_runner)
            self._gpu_warned = False
        except Exception as e:
            gpus = {}
            if not self._gpu_warned:
                self._gpu_warned = True
                self.log.warning("nvidia-smi failed at source discovery: %s "
                                 "— no GPU sources until a reload succeeds",
                                 e)
        self._sources = sources_mod.discover(
            self._chips, self.root, gpu_buses=gpus, labels=self._labels)
        self._source_ids = {s.id for s in self._sources}
        self._source_labels = {s.id: s.label for s in self._sources}

    # ------------------------------------------------------------- poll --

    def _read_all_sources(self, cfg) -> dict[str, float | None]:
        """Live temp per DETECTED source id. ONE nvidia-smi call serves all
        GPU sources; a failure marks them stale (hold duties) without
        stalling the whole loop — hwmon sources keep running."""
        temps: dict[str, float | None] = {}
        need_gpu = any(s.kind == "gpu" for s in self._sources)
        gtemps: dict = {}
        if need_gpu:
            try:
                gtemps = hwmon.gpu_temps(self.gpu_runner)
                self._gpu_fail_count = 0
            except Exception as e:
                self._gpu_fail_count += 1
                if self._gpu_fail_count == 1 \
                        or self._gpu_fail_count % 30 == 0:
                    self.log.warning("nvidia-smi failed (%d times): %s — GPU "
                                     "sources stale, holding duties",
                                     self._gpu_fail_count, e)
                gtemps = {}
        for s in self._sources:
            if s.kind == "gpu":
                temps[s.id] = gtemps.get(s.pci_bus) if gtemps else None
            else:
                d = hwmon.chip_dir_for(s.chip, s.chip_index,
                                       self._chips, self.root)
                temps[s.id] = hwmon.read_temp_c(d, s.temp) if d else None
        return temps

    def _poll(self, cfg):
        self._temps = self._read_all_sources(cfg)
        dead = cfg.config.get("dead_temp_c", -10)
        for aid, a in sorted(cfg.assignments.items()):
            if not a.get("enabled", True):
                continue
            src_id = a.get("source")
            if src_id not in self._source_ids:
                for f in a.get("fans", []):
                    self._fan_state[f] = "source-missing"
                continue
            t = self._temps.get(src_id)
            if curve.is_dead_temp(t, dead):
                taken = [f for f in a.get("fans", []) if f in self._taken_over]
                if taken:
                    n = self._missing_count.get(aid, 0) + 1
                    self._missing_count[aid] = n
                    if n == 1 or n % 30 == 0:
                        self.log.warning("assignment %s: source %s missing — "
                                         "holding last duty on %s", aid, src_id,
                                         ", ".join(taken))
                for f in a.get("fans", []):
                    if f in self._taken_over:
                        self._fan_state[f] = "source-missing"
                # not taken over: fans stay with the firmware, re-checked
                # every poll (safe against "dead source pins a fan low")
                continue
            self._missing_count.pop(aid, None)
            crv = cfg.curves.get(a.get("curve"))
            if crv is None:
                for f in a.get("fans", []):
                    self._fan_state[f] = "curve-missing"
                continue
            # floor/ceiling/rate are per-fan (per-assignment) settings;
            # the global config values are only fallbacks
            floor = a.get("floor_duty", cfg.config.get("floor_duty", 5))
            maxd = a.get("max_duty", 100)
            max_rate = a.get("max_rate", cfg.config.get("max_rate", 6))
            tgt = curve.apply_bounds(curve.eval_curve(crv.get("points", []), t),
                                     floor, maxd)
            for fan in a.get("fans", []):
                if fan not in self._taken_over:
                    self._takeover(fan)
                    if fan not in self._taken_over:
                        continue
                new = curve.rate_limit(self._duty.get(fan), tgt, max_rate)
                if new != self._duty.get(fan):
                    self._write_fan(cfg, fan, new, t, src_id)
                if self._fan_state.get(fan) != "write-error":
                    self._fan_state[fan] = "ok"

    # -------------------------------------------------------- takeover --

    def _initial_takeover(self, cfg):
        temps = self._read_all_sources(cfg)
        dead = cfg.config.get("dead_temp_c", -10)
        for aid, a in sorted(cfg.assignments.items()):
            if not a.get("enabled", True):
                continue
            src_id = a.get("source")
            if src_id not in self._source_ids:
                continue
            if curve.is_dead_temp(temps.get(src_id), dead):
                self.log.info("assignment %s: source %s absent at start — fans "
                              "left to firmware; re-checking each poll", aid,
                              src_id)
                continue
            for fan in a.get("fans", []):
                self._takeover(fan)

    def _takeover(self, fan: str):
        if fan in self._taken_over:
            return
        res = hwmon.resolve_fan(fan, self._chips, self.root)
        if res is None:
            self.log.warning("fan %s does not resolve to a live hwmon "
                             "channel — skipping", fan)
            self._fan_state[fan] = "unresolved"
            return
        chip_dir, pwm = res
        en = hwmon.get_enable(chip_dir, pwm)
        if en is None:
            self.log.warning("cannot read pwm%d_enable on %s — skipping",
                             pwm, fan)
            self._fan_state[fan] = "unresolved"
            return
        if en == hwmon.ENABLE_MANUAL:
            self.log.error("channel %s already in manual mode (owned by "
                           "another process) — skipping", fan)
            self._fan_state[fan] = "manual-conflict"
            return
        if en == hwmon.ENABLE_DISABLED:
            self.log.warning("channel %s disabled (enable=0) — skipping", fan)
            self._fan_state[fan] = "disabled"
            return
        if self.dry_run:
            self.log.info("would take over %s (enable %d→1)", fan, en)
            self._fan_state[fan] = "ok"
            self._taken_over.add(fan)
            return
        try:
            hwmon.set_enable(chip_dir, pwm, hwmon.ENABLE_MANUAL)
        except OSError as e:
            self.log.error("takeover of %s failed: %s", fan, e)
            self._fan_state[fan] = "write-error"
            return
        self._orig_enable[fan] = en
        self._taken_over.add(fan)
        self._fan_state[fan] = "ok"
        self.log.info("took over %s (enable %d→1)", fan, en)

    def _write_fan(self, cfg, fan: str, new_pct: int, t: float, src_id: str):
        res = hwmon.resolve_fan(fan, self._chips, self.root)
        if res is None:
            n = self._write_fail_count.get(fan, 0) + 1
            self._write_fail_count[fan] = n
            if n == 1 or n % 10 == 0:
                self.log.error("fan %s no longer resolves to a hwmon channel "
                               "(%d occurrences)", fan, n)
            self._fan_state[fan] = "unresolved"
            return
        chip_dir, pwm = res
        raw = curve.pct_to_raw255(new_pct)
        label = self._source_labels.get(src_id, src_id)
        if self.dry_run:
            self.log.info("would write %s = %3d%% (raw %d)  [%s = %.1f C]",
                          fan, new_pct, raw, label, t)
            self._duty[fan] = new_pct
            self._fan_state[fan] = "ok"
            return
        try:
            hwmon.write_duty(chip_dir, pwm, raw)
        except OSError as e:
            n = self._write_fail_count.get(fan, 0) + 1
            self._write_fail_count[fan] = n
            if n == 1 or n % 10 == 0:
                self.log.error("write to %s failed (%d times): %s — keeping "
                               "last duty, retrying", fan, n, e)
            self._fan_state[fan] = "write-error"
            return
        self._duty[fan] = new_pct
        self._fan_state[fan] = "ok"
        self.log.info("write %s = %3d%% (raw %d)  [%s = %.1f C]",
                      fan, new_pct, raw, label, t)

    # ----------------------------------------------------- restore/exit --

    def _restore_all(self):
        for fan in sorted(self._taken_over):
            self._restore_fan(fan)

    def _restore_fan(self, fan: str):
        if self.dry_run:
            self.log.info("would return %s to firmware (dry-run: no writes)",
                          fan)
            self._taken_over.discard(fan)
            return
        en = self._orig_enable.get(fan)
        if en is None:
            self._taken_over.discard(fan)
            return
        res = hwmon.resolve_fan(fan, self._chips, self.root)
        if res is None:
            self.log.error("cannot resolve %s to restore it — leaving as is",
                           fan)
            return
        chip_dir, pwm = res
        try:
            hwmon.set_enable(chip_dir, pwm, en)
        except OSError as e:
            self.log.error("FAILED to restore firmware control on %s: %s", fan,
                           e)
            return
        self.log.info("returned %s to firmware (enable → %d)", fan, en)
        self._taken_over.discard(fan)

    # -------------------------------------------------------- hot reload --

    def _maybe_reload(self, cfg):
        """mtime watch: on change, re-load. Parse/validation failure keeps
        the old in-memory config (log + status.json error)."""
        try:
            m = os.stat(self.cfg_path).st_mtime
        except OSError:
            return cfg
        if m == self._cfg_mtime:
            return cfg
        self._cfg_mtime = m
        try:
            new = config_mod.load(self.cfg_path)
            errors, warnings = config_mod.validate(new)
            for w in warnings:
                self.log.warning("config reload: %s", w)
            if errors:
                raise ValueError("; ".join(errors))
        except (OSError, ValueError) as e:
            self._reload_error = str(e)
            self.log.error("config reload failed — keeping previous config: "
                           "%s", e)
            return cfg
        if config_mod.to_dict(new) == config_mod.to_dict(cfg):
            self._reload_error = None
            return cfg
        self._reload_error = None
        self._discover_sources()
        self.log.info("config reloaded (sources detected: %d, "
                      "assignments: %d)", len(self._sources),
                      len(new.assignments))
        self._release_orphaned_fans(new)
        return new

    def _release_orphaned_fans(self, cfg):
        """Fans no longer referenced by any enabled assignment (assignment
        deleted/disabled, or its source removed) go back to the firmware —
        otherwise they would be pinned at their last duty forever."""
        referenced: set[str] = set()
        for a in cfg.assignments.values():
            if a.get("enabled", True):
                referenced.update(a.get("fans", []))
        for fan in sorted(self._taken_over - referenced):
            self.log.info("assignment removed — returning %s to firmware", fan)
            self._restore_fan(fan)

    # ----------------------------------------------------------- status --

    def _fan_label(self, fan: str) -> str:
        parsed = hwmon.parse_fan_id(fan)
        if parsed is None:
            return fan
        return hwmon.sensor_label(parsed[0], "pwm", parsed[1], self._labels)

    def _tach_for(self, fan: str, cfg) -> str | None:
        for a in cfg.assignments.values():
            tach = a.get("tach") or {}
            if tach.get(fan):
                return tach[fan]
        return None

    def _write_status(self, cfg):
        data = {
            "pid": os.getpid(),
            "ts": time.time(),
            "config_mtime": self._cfg_mtime,
            "config_path": str(self.cfg_path),
            "dry_run": self.dry_run,
            "sources": {},
            "fans": {},
            "errors": {},
        }
        dead = cfg.config.get("dead_temp_c", -10)
        for s in self._sources:
            t = self._temps.get(s.id)
            data["sources"][s.id] = {
                "label": s.label,
                "kind": s.kind,
                "temp_c": t,
                "stale": curve.is_dead_temp(t, dead),
            }
        fan_ids: set[str] = set()
        for a in cfg.assignments.values():
            fan_ids.update(a.get("fans", []))
        for fan in sorted(fan_ids):
            entry = {
                "label": self._fan_label(fan),
                "duty_pct": self._duty.get(fan),
                "rpm": None,
                "orig_enable": self._orig_enable.get(fan),
                "state": self._fan_state.get(fan, "idle"),
            }
            res = hwmon.resolve_fan(fan, self._chips, self.root)
            if res is not None:
                chip_dir, pwm = res
                entry["duty_raw"] = hwmon.read_duty_raw(chip_dir, pwm)
                entry["enable"] = hwmon.get_enable(chip_dir, pwm)
                tach = self._tach_for(fan, cfg)
                if tach:
                    tres = hwmon.resolve_tach(tach, self._chips, self.root)
                    if tres:
                        entry["rpm"] = hwmon.read_rpm(*tres)
            data["fans"][fan] = entry
        if self._reload_error:
            data["errors"]["config"] = self._reload_error
        try:
            status_mod.write_status(status_mod.status_path_for(self.cfg_path),
                                    data)
        except OSError:
            pass  # status is best-effort; never kill the loop over it

    # -------------------------------------------------------------- misc --

    def _log_resolved(self, cfg):
        self.log.info("config: %s", self.cfg_path)
        temps = self._read_all_sources(cfg)
        self.log.info("sources (discovered at runtime — not in the config):")
        for s in self._sources:
            t = temps.get(s.id)
            tstr = "no reading" if t is None else f"{t:6.1f} C"
            self.log.info("  %-20s %-24s %-28s %s", s.id, s.label,
                          s.detail, tstr)
        self.log.info("assignments:")
        for aid, a in sorted(cfg.assignments.items()):
            on = "" if a.get("enabled", True) else " [disabled]"
            self.log.info("  %-14s %s -> %s   fans: %s%s", aid, a.get("source"),
                          a.get("curve"), ", ".join(a.get("fans", [])), on)


def main(args) -> int:
    """Entry from cli.py (args: --config/--dry-run/--once/--duration/-v)."""
    path = Path(args.config).expanduser() if getattr(args, "config", None) \
        else config_mod.default_path()
    if not path.exists():
        config_mod.seed_if_missing(path)
    # System service (root): /var/log (unit's LogsDirectory=); non-root
    # dev runs: next to the config file.
    if os.geteuid() == 0:
        log_path = Path("/var/log/alx_fancontrol/daemon.log")
    else:
        log_path = path.parent / "daemon.log"
    d = Daemon(path,
               dry_run=getattr(args, "dry_run", False),
               once=getattr(args, "once", False),
               duration=getattr(args, "duration", None),
               log_path=log_path,
               verbose=getattr(args, "verbose", False))
    return d.run()


def _main() -> int:
    """`python3 -m alx_fancontrol.daemon` — same flags, system python OK
    (the daemon is pure stdlib)."""
    p = argparse.ArgumentParser(prog="alx_fancontrol.daemon")
    p.add_argument("--config", default=None)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--once", action="store_true")
    p.add_argument("--duration", type=float, default=None, metavar="SECS")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()
    return main(args)


if __name__ == "__main__":
    sys.exit(_main())
