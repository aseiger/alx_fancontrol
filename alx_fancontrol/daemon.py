"""alx_fancontrol daemon.

Poll loop: read sources (ONE nvidia-smi call per poll serves all GPU
sources) -> eval piecewise-linear curves -> rate-limit -> rate-limited PWM
writes, with takeover/restore. Hot-reloads the config on mtime change
every poll (atomic replace on the writer side guarantees no torn reads).
Firmware control is restored exactly in `finally` on clean exit
(SIGTERM/SIGINT set the stop flag).

Shared fans (a fan listed in several assignments) are driven to the
HIGHEST duty any of them requests — each assignment's request is clamped
by its OWN floor/ceiling first; ties go to the lowest assignment id and
the rate limiter runs ONCE per poll, bounded by the winning assignment's
max_rate. A cold source can never drag a shared fan below a hot one's
request; a missing source simply stops contributing.

Safety guardrails (enforced in code, see also scripts/protect.py):
- FAIL-SAFE DIRECTION: a source that goes unreadable (GPU dropped off the
  bus, nvidia-smi hung/offline, dead thermistor, hwmon chip gone) makes the
  assignment drive its fans to `failsafe_duty` (default 100 %) after
  `failsafe_after` consecutive dead polls, ramping at `failsafe_max_rate`
  %/poll. It never "holds a low duty" and never hands the header back to a
  firmware that parks it at 0 — an unreadable temperature may only ever
  make a fan go FASTER.
- Sources are re-discovered EVERY poll, so a GPU that vanishes from
  nvidia-smi and comes back is re-adopted automatically (no restart, no
  config edit). Fans are taken over even when their source is absent at
  startup, because the fail-safe needs ownership to be able to act.
- A channel found in manual mode (pwmN_enable=1) owned by ANOTHER process
  is refused — but a channel left manual by a previous alx_fancontrol run
  that died (SIGKILL/OOM/power loss) is RECLAIMED, from the ownership
  marker in the state dir. Without that, a killed daemon would pin a GPU
  fan at its last raw duty (possibly 0) forever.
- Nothing may block the poll loop: nvidia-smi runs with a timeout
  (`smi_timeout_s`) and its child is killed without being waited on, and a
  watchdog thread exits (`watchdog_seconds`) if no poll completes, so
  systemd restarts us instead of a hung nvidia-smi freezing every fan.
- A fan commanded >= `stall_min_duty` whose tach reads 0 RPM for
  `stall_after` polls is reported as `stalled` (log ERROR + status.json).
- Takeover from pwmN_enable=2 records the original value and restores it
  EXACTLY on clean exit. Opt in to taking over a channel whatever its
  state with per-assignment `"force_takeover": true`.
- --dry-run performs NO writes at all (no enable, no duty); it still runs
  every guardrail check and logs the intended actions.

The daemon never writes the config file — only the TUI (and humans) do.
"""
from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

from alx_fancontrol import config as config_mod
from alx_fancontrol import curve, hwmon, sources as sources_mod
from alx_fancontrol import status as status_mod

EXIT_OK = 0
EXIT_HARD_ERROR = 2
EXIT_STUCK = 70          # watchdog: poll loop wedged, let systemd restart us

_QUERY = object()        # discover sources: query nvidia-smi now
_KEEP = object()         # discover sources: keep the GPU list from last time


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
        self._retired = False             # run() finished: retire the watchdog
        self._chips: list[tuple[str, Path]] = []
        self._sources: list = []
        self._source_ids: set[str] = set()
        self._source_labels: dict[str, str] = {}
        self._labels = hwmon.load_sensorsd_labels()
        self._cfg = None
        self._cfg_mtime: float | None = None
        self._reload_error: str | None = None
        self._startup_error: str | None = None
        self._degraded = False
        self._degraded_fans: set[str] = set()

        self._taken_over: set[str] = set()
        self._orig_enable: dict[str, int] = {}
        self._duty: dict[str, int] = {}          # fan -> last successful duty %
        self._fan_state: dict[str, str] = {}
        self._temps: dict[str, float | None] = {}
        self._rpm: dict[str, int | None] = {}
        self._missing_count: dict[str, int] = {}
        self._refuse_count: dict[str, int] = {}
        self._stall_count: dict[str, int] = {}
        self._write_fail_count: dict[str, int] = {}
        self._gpu_fail_count = 0
        self._gpu_warned = False
        self._gpu_backoff_until = 0.0
        self._gpu_queried = False
        self._force_fans: set[str] = set()
        self._t0: float | None = None
        self._last_tick = time.time()
        self._watchdog_seconds = 30.0

        # ownership marker: channels we hold, + the enable value to restore
        self._marker_path = status_mod.owned_path_for(self.cfg_path)
        self._marker_orig: dict[str, int] = {}
        self._reclaimable: set[str] = set()

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
        self._load_marker()
        cfg = self._initial_load()
        self._start_watchdog()   # uses the configured value, or the default
        if cfg is None:
            # An invalid config must not mean "no fan control": keep the
            # assigned fans at fail-safe duty and wait for a valid config.
            cfg = self._degraded_recovery()
            if cfg is None:
                self._retired = True
                self._restore_all()
                self.log.info("daemon stopped")
                return EXIT_HARD_ERROR
        self._log_resolved(cfg)
        self._initial_takeover(cfg)
        self._t0 = time.time()
        self._last_tick = self._t0
        self._notify("READY=1")
        self.log.info(
            "daemon started (pid %d, %s, poll %.2fs, once=%s, duration=%s, "
            "failsafe %d%% after %s dead polls, watchdog %gs)",
            os.getpid(), "DRY-RUN: no writes" if self.dry_run else "LIVE",
            cfg.config.get("poll_seconds", 1.0), self.once,
            f"{self.duration:.0f}s" if self.duration is not None else "none",
            int(cfg.config.get("failsafe_duty", 100)),
            cfg.config.get("failsafe_after", 5),
            cfg.config.get("watchdog_seconds", 30))
        try:
            while not self._stop:
                cfg = self._maybe_reload(cfg)
                self._poll(cfg)
                self._write_status(cfg)
                self._last_tick = time.time()
                self._beat(cfg)
                if self.once:
                    break
                if self.duration is not None \
                        and time.time() - self._t0 >= self.duration:
                    self.log.info("duration %gs reached — stopping", self.duration)
                    break
                time.sleep(cfg.config.get("poll_seconds", 1.0))
        finally:
            self._retired = True
            self._notify("STOPPING=1")
            self._restore_all()
            self.log.info("daemon stopped")
        return EXIT_OK

    def _initial_load(self):
        try:
            cfg = config_mod.load(self.cfg_path)
        except (OSError, ValueError) as e:
            self._startup_error = f"{self.cfg_path}: {e}"
            self._degraded_fans = self._collect_fans()
            self.log.error("cannot load config %s: %s", self.cfg_path, e)
            self.log.critical("config unreadable — %s",
                              f"degraded mode will hold {len(self._degraded_fans)} "
                              f"assigned fan channel(s) at fail-safe duty"
                              if self._degraded_fans else
                              "no assignable fan channel could be recovered "
                              "from it, so nothing can be cooled")
            return None
        errors, warnings = config_mod.validate(cfg)
        for w in warnings:
            self.log.warning("config: %s", w)
        if errors:
            for e in errors:
                self.log.error("config: %s", e)
            self._startup_error = "; ".join(errors)
            self._degraded_fans = self._collect_fans(cfg)
            self.log.critical("%d hard config error(s) — degraded mode "
                              "instead of leaving the fans to the firmware",
                              len(errors))
            return None
        self._startup_error = None
        self._degraded_fans = set()
        self._cfg = cfg
        self._watchdog_seconds = float(cfg.config.get("watchdog_seconds", 30) or 0)
        try:
            self._cfg_mtime = os.stat(self.cfg_path).st_mtime
        except OSError:
            pass
        self._discover_sources()
        return cfg

    def _smi_timeout(self) -> float | None:
        try:
            v = float((self._cfg.config if self._cfg else {})
                      .get("smi_timeout_s", hwmon.SMI_TIMEOUT_S))
        except (TypeError, ValueError):
            v = hwmon.SMI_TIMEOUT_S
        return v if v > 0 else None      # 0 = explicitly no timeout

    def _discover_sources(self, gpu_buses=_QUERY):
        """Sources are a machine property: detect them, don't configure
        them. Runs at start AND EVERY POLL (hwmon indices reshuffle, GPUs
        leave and come back) — re-detecting is what lets a GPU that dropped
        out of nvidia-smi be re-adopted without a restart or a config edit.

        `gpu_buses`: explicit {bus: temp} map (only the keys matter),
        _QUERY to ask nvidia-smi, _KEEP to reuse the previous GPU list (e.g.
        while backing off after a timeout — the ids must not churn)."""
        old_ids = set(self._source_ids)
        self._chips = hwmon.find_all_chips(self.root)
        if gpu_buses is _QUERY:
            try:
                gpu_buses = hwmon.gpu_temps(self.gpu_runner,
                                            timeout=self._smi_timeout())
                self._gpu_warned = False
            except Exception as e:
                gpu_buses = {}
                if not self._gpu_warned:
                    self._gpu_warned = True
                    self.log.warning("nvidia-smi failed at source discovery: "
                                     "%s — no GPU sources until one succeeds", e)
        elif gpu_buses is _KEEP:
            gpu_buses = {s.pci_bus: None for s in self._sources
                         if s.kind == "gpu"}
        self._sources = sources_mod.discover(
            self._chips, self.root, gpu_buses=gpu_buses, labels=self._labels)
        self._source_ids = {s.id for s in self._sources}
        self._source_labels = {s.id: s.label for s in self._sources}
        gained = self._source_ids - old_ids
        lost = old_ids - self._source_ids
        if gained:
            self.log.info("source(s) detected again: %s", ", ".join(sorted(gained)))
        if lost:
            self.log.warning("source(s) no longer detected: %s",
                             ", ".join(sorted(lost)))

    # ------------------------------------------------------------- poll --

    def _read_gpu_temps(self, cfg) -> dict:
        """ONE nvidia-smi call per poll serves all GPU sources — WITH a
        timeout, because a GPU that fell off the bus makes nvidia-smi block
        forever and that must never freeze the loop. A timed-out child is
        killed without being waited on (hwmon._run_argv) and reaped on a
        later poll; a short backoff stops hung nvidia-smi processes piling
        up. Any failure returns {} = every GPU source stale."""
        hwmon.reap_children()
        self._gpu_queried = False
        need_gpu = any(s.kind == "gpu" for s in self._sources) or any(
            str(a.get("source", "")).startswith("gpu:")
            for a in cfg.assignments.values() if a.get("enabled", True))
        if not need_gpu or time.time() < self._gpu_backoff_until:
            return {}
        try:
            gtemps = hwmon.gpu_temps(self.gpu_runner, timeout=self._smi_timeout())
            self._gpu_fail_count = 0
            self._gpu_queried = True
            return gtemps
        except Exception as e:
            self._gpu_fail_count += 1
            timed_out = isinstance(e, subprocess.TimeoutExpired)
            if timed_out:
                self._gpu_backoff_until = time.time() + max(
                    1.0, self._smi_timeout() or 1.0)
            if self._gpu_fail_count == 1 or self._gpu_fail_count % 30 == 0:
                self.log.warning(
                    "nvidia-smi %s (%d times): %s — GPU sources stale; the "
                    "fail-safe ramps their fans instead of holding duty",
                    "timed out" if timed_out else "failed",
                    self._gpu_fail_count, e)
            return {}

    def _read_all_sources(self, cfg) -> tuple[dict[str, float | None], dict]:
        """(live temp per detected source id, gpu temps by pci bus). The
        sources are re-discovered first, so a source that came back is read
        in the SAME poll it reappears. A GPU temp of None means stale — the
        caller decides (fail-safe), it never reads as 'cold'."""
        gtemps = self._read_gpu_temps(cfg)
        self._discover_sources(gtemps if self._gpu_queried else _KEEP)
        temps: dict[str, float | None] = {}
        for s in self._sources:
            if s.kind == "gpu":
                temps[s.id] = gtemps.get(s.pci_bus)
            else:
                d = hwmon.chip_dir_for(s.chip, s.chip_index,
                                       self._chips, self.root)
                temps[s.id] = hwmon.read_temp_c(d, s.temp) if d else None
        return temps, gtemps

    def _failsafe(self, a, cfg) -> dict:
        """Fail-safe parameters for one assignment (global values are the
        fallbacks): drive its fans to `duty` % once its source has been
        unreadable for `after` consecutive polls, ramping at `rate` %/poll.
        `on: False` (per-assignment) restores the old hold-last-duty."""
        c = cfg.config

        def num(key, dflt):
            v = a.get(key, c.get(key, dflt))
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                return float(dflt)
            return float(v)
        return {"on": bool(a.get("failsafe", True)),
                "duty": num("failsafe_duty", 100),
                "after": max(0, int(num("failsafe_after", 5))),
                "rate": num("failsafe_max_rate", 25)}

    def _temp_dead(self, src_id, t, cfg) -> bool:
        """Unreadable reading: absent, or below dead_temp_c, or — for a GPU
        — below gpu_temp_min_valid_c. A card that dropped off the bus reads
        0 C: that is not a temperature and must never look like 'ice cold'
        (that is how a fan ends up idling while the card cooks)."""
        if curve.is_dead_temp(t, cfg.config.get("dead_temp_c", -10)):
            return True
        if t is not None and str(src_id).startswith("gpu:"):
            try:
                lo = float(cfg.config.get("gpu_temp_min_valid_c", 15))
            except (TypeError, ValueError):
                lo = 15.0
            if t < lo:
                return True
        return False

    def _poll(self, cfg):
        self._temps, _gtemps = self._read_all_sources(cfg)
        # channels the user explicitly wants taken over whatever
        # pwmN_enable currently says
        self._force_fans = {
            f for a in cfg.assignments.values()
            if isinstance(a, dict) and a.get("enabled", True)
            and a.get("force_takeover")
            for f in a.get("fans", [])}
        # Pass 1: every enabled assignment computes the duty its source
        # requests (its own curve, clamped by its own floor/ceiling).
        # A fan may be requested by SEVERAL assignments — pass 2 drives
        # it to the highest request, so a cold source never drags a
        # shared fan below a hot one. A DEAD source still requests its
        # fail-safe duty once its counter passes, so an unreadable GPU
        # pushes its fan UP instead of freezing it, and highest-wins keeps
        # the safest request in charge of a shared fan.
        # fan -> [(target %, max rate, source id, temp C, failsafe, aid)]
        requests: dict[str, list[tuple[float, float, str, float | None,
                                       bool, str]]] = {}
        for aid, a in sorted(cfg.assignments.items()):
            if not a.get("enabled", True):
                continue
            src_id = a.get("source")
            fans = list(a.get("fans", []))
            fs = self._failsafe(a, cfg)
            t = self._temps.get(src_id)
            if src_id not in self._source_ids or self._temp_dead(src_id, t, cfg):
                n = self._missing_count.get(aid, 0) + 1
                self._missing_count[aid] = n
                if fs["on"] and n >= fs["after"]:
                    duty = min(float(a.get("max_duty", 100)), fs["duty"])
                    for f in fans:
                        requests.setdefault(f, []).append(
                            (duty, fs["rate"], src_id, t, True, aid))
                    if n == fs["after"] or n % 30 == 0:
                        self.log.warning(
                            "FAIL-SAFE: assignment %s source %s unreadable for "
                            "%d polls — driving %s to %.0f%%", aid, src_id, n,
                            ", ".join(fans) or "(no fans)", duty)
                else:
                    for f in fans:
                        self._fan_state[f] = "source-missing"
                    if n == 1 or n % 30 == 0:
                        self.log.warning(
                            "assignment %s: source %s unreadable (poll %d) — %s",
                            aid, src_id, n,
                            f"fail-safe engages in {max(0, fs['after'] - n)} "
                            f"poll(s)" if fs["on"] else
                            "fail-safe DISABLED: holding last duty")
                continue
            self._missing_count.pop(aid, None)
            crv = cfg.curves.get(a.get("curve"))
            if crv is None:
                for f in fans:
                    self._fan_state[f] = "curve-missing"
                continue
            # floor/ceiling/rate are per-fan (per-assignment) settings;
            # the global config values are only fallbacks
            floor = a.get("floor_duty", cfg.config.get("floor_duty", 5))
            maxd = a.get("max_duty", 100)
            max_rate = a.get("max_rate", cfg.config.get("max_rate", 6))
            tgt = curve.apply_bounds(curve.eval_curve(crv.get("points", []), t),
                                     floor, maxd)
            for f in fans:
                requests.setdefault(f, []).append(
                    (tgt, max_rate, src_id, t, False, aid))
        # Pass 2: one rate-limited step per fan, toward the highest
        # request. Ties go to the lowest assignment id (deterministic);
        # the winning assignment's max_rate bounds the step — the limiter
        # runs ONCE per poll, even for a fan with several assignments.
        for fan, reqs in sorted(requests.items()):
            reqs.sort(key=lambda r: (-r[0], r[5]))
            tgt, max_rate, src_id, t, failsafe, _aid = reqs[0]
            if fan not in self._taken_over:
                self._takeover(fan)
                if fan not in self._taken_over:
                    continue
            new = curve.rate_limit(self._duty.get(fan), tgt, max_rate)
            if new != self._duty.get(fan):
                self._write_fan(cfg, fan, new, t, src_id)
            if self._fan_state.get(fan) != "write-error":
                self._fan_state[fan] = "failsafe" if failsafe else "ok"
        self._check_fans(cfg)

    def _check_fans(self, cfg):
        """Read one tach per controlled fan: cache it for status.json and
        alarm when we command real duty but the tach says the fan is not
        turning (stalled, tach pin unplugged, or a header the firmware took
        back). Commanded duty alone never proves that anything cools."""
        c = cfg.config
        try:
            min_duty = float(c.get("stall_min_duty", 20))
            after = max(1, int(c.get("stall_after", 10)))
        except (TypeError, ValueError):
            min_duty, after = 20.0, 10
        for fan in sorted(self._taken_over):
            tach = self._tach_for(fan, cfg)
            tres = hwmon.resolve_tach(tach, self._chips, self.root) if tach else None
            self._rpm[fan] = hwmon.read_rpm(*tres) if tres else None
            duty = self._duty.get(fan) or 0
            if duty >= min_duty and self._rpm[fan] == 0:
                n = self._stall_count.get(fan, 0) + 1
                self._stall_count[fan] = n
                if n >= after:
                    self._fan_state[fan] = "stalled"
                if n == after or n % after == 0:
                    self.log.error(
                        "FAN %s STALLED: commanding %d%% but tach %s reads "
                        "0 RPM for %d polls", fan, duty, tach, n)
            else:
                self._stall_count.pop(fan, None)

    # -------------------------------------------------------- takeover --

    def _initial_takeover(self, cfg):
        """Take over every fan an enabled assignment references, WHETHER OR
        NOT its source reads now: the fail-safe has to own the channel to be
        able to raise it later. Leaving a header with the firmware because
        its source is absent is exactly how a GPU fan ends up parked at 0%
        while the card heats (and this board does park unowned headers —
        e.g. it8686 pwm2 sits at 0 under firmware control)."""
        for aid, a in sorted(cfg.assignments.items()):
            if not a.get("enabled", True):
                continue
            src_id = a.get("source")
            if src_id not in self._source_ids:
                self.log.info(
                    "assignment %s: source %s absent at start — taking over "
                    "its fans anyway; the fail-safe drives them until it "
                    "appears", aid, src_id)
            for fan in a.get("fans", []):
                self._takeover(fan)

    def _refuse(self, fan: str, state: str, level: str, msg: str):
        """Refusals are re-checked every poll (that is what makes recovery
        automatic), so log the first one and then every 30th instead of
        flooding the journal."""
        n = self._refuse_count.get(fan, 0) + 1
        self._refuse_count[fan] = n
        if n == 1 or n % 30 == 0:
            getattr(self.log, level)("%s (%d polls in a row)", msg, n)
        self._fan_state[fan] = state

    def _takeover(self, fan: str):
        if fan in self._taken_over:
            return
        res = hwmon.resolve_fan(fan, self._chips, self.root)
        if res is None:
            self._refuse(fan, "unresolved", "warning",
                         f"fan {fan} does not resolve to a live hwmon "
                         f"channel — skipping")
            return
        chip_dir, pwm = res
        en = hwmon.get_enable(chip_dir, pwm)
        if en is None:
            self._refuse(fan, "unresolved", "warning",
                         f"cannot read pwm{pwm}_enable on {fan} — skipping")
            return
        if en == hwmon.ENABLE_MANUAL:
            ours = fan in self._reclaimable
            force = fan in self._force_fans
            if not (ours or force):
                self._refuse(fan, "manual-conflict", "error",
                             f"channel {fan} already in manual mode (owned by "
                             f"another process) — skipping")
                return
            if ours:
                self.log.warning(
                    "reclaiming %s: left in manual mode by a previous "
                    "alx_fancontrol run that died without restoring it — "
                    "taking it back instead of pinning it at its last duty",
                    fan)
            else:
                self.log.warning("force_takeover: %s was already in manual "
                                 "mode (foreign owner) — taking it over as "
                                 "configured", fan)
        if en == hwmon.ENABLE_DISABLED:
            if fan not in self._force_fans:
                self._refuse(fan, "disabled", "warning",
                             f"channel {fan} disabled (enable=0) — skipping")
                return
            self.log.warning("force_takeover: %s was disabled (enable=0) — "
                             "enabling it as configured", fan)
        # what to hand back on exit: the value recorded before WE first took
        # it (from the marker), not the 1 we are about to write
        orig = self._marker_orig.get(fan)
        if orig is None:
            orig = hwmon.ENABLE_FIRMWARE if en == hwmon.ENABLE_MANUAL else en
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
        self._orig_enable[fan] = orig
        self._taken_over.add(fan)
        self._fan_state[fan] = "ok"
        self._refuse_count.pop(fan, None)
        self._save_marker()
        self.log.info("took over %s (enable %d→1, restore to %d on exit)",
                      fan, en, orig)

    def _write_fan(self, cfg, fan: str, new_pct: int, t: float | None,
                   src_id: str):
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
        # a fail-safe write has no reading to name — that is the whole point
        tstr = "no reading" if t is None else f"{t:.1f} C"
        if self.dry_run:
            self.log.info("would write %s = %3d%% (raw %d)  [%s = %s]",
                          fan, new_pct, raw, label, tstr)
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
        self.log.info("write %s = %3d%% (raw %d)  [%s = %s]",
                      fan, new_pct, raw, label, tstr)

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
        self._save_marker()

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
        self._discover_sources(_KEEP)   # the next poll refreshes GPUs anyway
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
        for a in (cfg.assignments.values() if cfg else []):
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
            "degraded": self._degraded,
            "sources": {},
            "fans": {},
            "errors": {},
        }
        dead = cfg.config.get("dead_temp_c", -10) if cfg else -10
        for s in self._sources:
            t = self._temps.get(s.id)
            data["sources"][s.id] = {
                "label": s.label,
                "kind": s.kind,
                "temp_c": t,
                # a GPU reading of 0 C is stale, not cold
                "stale": self._temp_dead(s.id, t, cfg) if cfg
                else curve.is_dead_temp(t, dead),
            }
        fan_ids: set[str] = set(self._taken_over)
        for a in (cfg.assignments.values() if cfg else []):
            fan_ids.update(a.get("fans", []))
        for fan in sorted(fan_ids):
            entry = {
                "label": self._fan_label(fan),
                "duty_pct": self._duty.get(fan),
                "rpm": self._rpm.get(fan),
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
        if self._startup_error:
            data["errors"]["startup_config"] = self._startup_error
        if self._gpu_fail_count:
            data["errors"]["nvidia_smi"] = (
                f"{self._gpu_fail_count} consecutive failure(s)/timeout(s) — "
                f"GPU sources stale, fans on fail-safe")
        try:
            after = int((cfg.config.get("stall_after", 10)
                         if cfg else 10))
        except (TypeError, ValueError):
            after = 10
        for fan, n in sorted(self._stall_count.items()):
            if n >= after:
                data["errors"][f"stalled:{fan}"] = (
                    f"commanding {self._duty.get(fan)}%% but tach reads "
                    f"0 RPM for {n} polls")
        try:
            status_mod.write_status(status_mod.status_path_for(self.cfg_path),
                                    data)
        except OSError:
            pass  # status is best-effort; never kill the loop over it

    # -------------------------------------------------------------- misc --

    def _log_resolved(self, cfg):
        self.log.info("config: %s", self.cfg_path)
        temps, _g = self._read_all_sources(cfg)
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

    # ------------------------------------------------- ownership marker --

    @staticmethod
    def _boot_id() -> str:
        try:
            return Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        except OSError:
            return "unknown"

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return False
        try:          # pid reuse: only another alx_fancontrol counts as us
            cmd = Path(f"/proc/{pid}/cmdline").read_bytes().decode("utf-8",
                                                                   "ignore")
        except OSError:
            return False
        return "alx_fancontrol" in cmd

    def _load_marker(self):
        """Read which channels the previous alx_fancontrol run held.

        Trusted only when it is the SAME boot (our own restart after a
        SIGKILL/OOM/power blip) and no other alx_fancontrol daemon is alive.
        Then those channels are RECLAIMABLE: found in manual mode we take
        them back instead of refusing them forever — which is how a killed
        daemon used to leave a GPU fan pinned at its last raw duty (possibly
        0) with the service reporting itself healthy."""
        self._marker_orig = {}
        self._reclaimable = set()
        try:
            doc = json.loads(self._marker_path.read_text())
        except (OSError, ValueError):
            return
        if not isinstance(doc, dict) or doc.get("boot_id") != self._boot_id():
            return                       # different boot: nothing of ours left
        other = doc.get("pid")
        if isinstance(other, int) and other != os.getpid() \
                and self._pid_alive(other):
            self.log.error(
                "another alx_fancontrol daemon (pid %d) owns %s — refusing to "
                "take those channels over", other,
                ", ".join(sorted((doc.get("channels") or {}).keys()))
                or "nothing")
            return
        chans = doc.get("channels")
        if not isinstance(chans, dict):
            return
        for fan, orig in chans.items():
            if isinstance(fan, str) and isinstance(orig, int) \
                    and not isinstance(orig, bool):
                self._marker_orig[fan] = orig
                self._reclaimable.add(fan)
        if self._reclaimable:
            self.log.info("ownership marker from this boot: %d channel(s) "
                          "held by the previous run (%s)",
                          len(self._reclaimable),
                          ", ".join(sorted(self._reclaimable)))

    def _save_marker(self):
        """Record exactly what we hold + what to restore it to. Written on
        takeover/restore only (never per poll)."""
        data = {"pid": os.getpid(),
                "boot_id": self._boot_id(),
                "channels": {f: self._orig_enable.get(f, hwmon.ENABLE_FIRMWARE)
                             for f in sorted(self._taken_over)}}
        try:
            status_mod.write_status(self._marker_path, data)
        except OSError as e:
            self.log.warning("cannot write ownership marker %s: %s",
                             self._marker_path, e)

    # ------------------------------------------------- degraded startup --

    def _collect_fans(self, cfg=None) -> set[str]:
        """Fan ids referenced by enabled assignments, best effort — used
        when the config did not load or did not validate: those fans still
        have to be driven instead of being left to a firmware that may park
        them at 0%."""
        if cfg is not None:
            docs: list = [cfg.assignments]
        else:
            try:
                docs = [json.loads(Path(self.cfg_path).read_text())
                            .get("assignments") or {}]
            except (OSError, ValueError, AttributeError):
                docs = []
        out: set[str] = set()
        for assignments in docs:
            if not isinstance(assignments, dict):
                continue
            for a in assignments.values():
                if not isinstance(a, dict) or not a.get("enabled", True):
                    continue
                for f in a.get("fans") or []:
                    if isinstance(f, str) and config_mod.FAN_ID_RE.match(f):
                        out.add(f)
        return out

    def _failsafe_duty_global(self) -> float:
        try:
            v = json.loads(Path(self.cfg_path).read_text()) \
                .get("config", {}).get("failsafe_duty", 100)
        except (OSError, ValueError, AttributeError):
            return 100.0
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            return 100.0
        return max(0.0, min(100.0, float(v)))

    def _degraded_recovery(self):
        """Startup config unusable: rather than exit and hand every assigned
        header back to the firmware, hold them at fail-safe duty and re-read
        the config every second. Returns a valid Config once the file is
        fixed, or None if we have to give up (no recoverable channel, or a
        one-shot run)."""
        if not self._degraded_fans or self.once:
            return None
        duty = int(self._failsafe_duty_global())
        self.log.critical(
            "DEGRADED MODE: %s is unusable (%s) — driving %s to %d%% and "
            "re-reading the config every 1s; fix it to resume normal control",
            self.cfg_path, self._startup_error,
            ", ".join(sorted(self._degraded_fans)), duty)
        self._degraded = True
        for fan in sorted(self._degraded_fans):
            self._takeover(fan)
        while not self._stop:
            for fan in sorted(self._taken_over):
                if self._duty.get(fan) != duty:
                    self._write_fan(None, fan, duty, None,
                                    "fail-safe (invalid config)")
                self._fan_state[fan] = "failsafe"
            self._write_status(None)
            self._last_tick = time.time()
            self._beat(None)
            time.sleep(1.0)
            cfg = self._initial_load()
            if cfg is not None:
                self._degraded = False
                self.log.info("config valid again — leaving degraded mode")
                return cfg
        self._degraded = False
        return None

    # -------------------------------------------------------- liveness --

    def _start_watchdog(self):
        """A poll loop stuck on anything (D-state nvidia-smi, hung sysfs
        read) freezes every fan at its last duty while the process still
        looks alive to systemd. So beat a drum, and if the beating stops,
        exit NON-ZERO *without* restoring firmware control: the channels
        keep their last duty, systemd restarts us, and the ownership marker
        lets the new instance take them back."""
        if self.once or self._watchdog_seconds <= 0:
            return
        threading.Thread(target=self._watchdog_loop, name="alx-watchdog",
                         daemon=True).start()

    def _watchdog_loop(self):
        while not self._stop and not self._retired:
            time.sleep(1.0)
            if self._stop or self._retired:
                return
            limit = self._watchdog_seconds
            if limit <= 0:
                continue                 # disabled after startup: stay quiet
            idle = time.time() - self._last_tick
            if idle > limit:
                self.log.critical(
                    "WATCHDOG: no poll completed for %.0fs — exiting so "
                    "systemd restarts the daemon; channels keep their last "
                    "duty and are reclaimed on startup", idle)
                for h in self.log.handlers:
                    try:
                        h.flush()
                    except OSError:
                        pass
                os._exit(EXIT_STUCK)

    def _beat(self, cfg):
        """systemd keep-alive + a one-line status (both best effort)."""
        if self._degraded:
            state = "DEGRADED (invalid config, fail-safe duty)"
        elif any(v == "stalled" for v in self._fan_state.values()):
            state = "FAN STALLED"
        elif any(v == "failsafe" for v in self._fan_state.values()):
            state = "FAIL-SAFE (source unreadable)"
        else:
            state = "ok"
        self._notify(f"WATCHDOG=1\nSTATUS={state} — {len(self._taken_over)} "
                     f"channel(s), pid {os.getpid()}")

    def _notify(self, state: str):
        """sd_notify with the stdlib: silently ignored unless the unit asks "
        "for notifications, so it can never break the daemon."""
        sock = os.environ.get("NOTIFY_SOCKET")
        if not sock or not state:
            return
        if sock.startswith("@"):
            sock = "\0" + sock[1:]
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
                s.sendto(state.encode(), sock)
        except OSError:
            pass


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
