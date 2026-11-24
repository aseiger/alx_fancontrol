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
  `stall_after` polls is reported as `stalled` (log ERROR + status.json) and
  then ESCALATED: CRITICAL log plus the `stall_alert_command` hook, repeated
  every `stall_alert_repeat_s` while it lasts. Extra duty is useless against
  a fan that is not turning, so escalation is about getting the event in
  front of someone. **This daemon controls fans only** — never GPU clocks,
  power limits or anything else on a card. If a fan fault should also slow a
  workload down, that response belongs in the alert command the operator
  writes, not here.
- OWNERSHIP IS RE-VERIFIED EVERY POLL: `pwmN_enable` can be flipped back to
  2/0 by the firmware, the EC or a second writer while we run, and a duty
  write into a channel we no longer own *succeeds* — the log says "100 %" and
  the fan sits parked, because this board parks headers it owns. So each poll
  re-reads `pwmN_enable` (and `pwmN`) for every channel we hold, reclaims /
  re-asserts on any drift, and treats the loss as an escalation.
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
        # fans we drive that have no usable RPM (no tach, or an id that does
        # not resolve) — counted so the warning is logged once, not per poll
        self._no_tach_count: dict[str, int] = {}
        # fan -> active stall-drive episode {phase, attempts, until, pulse}
        self._kick: dict[str, dict] = {}
        # fan -> duty % the episode commands right now; pass 2 defers to it
        self._kick_duty: dict[str, int] = {}
        # fan -> monotonic time of the last escalation for the current episode
        self._stall_eps: dict[str, float] = {}
        self._lost_eps: dict[str, float] = {}
        # fan -> consecutive polls on which we did NOT hold the channel
        self._lost_count: dict[str, int] = {}
        self._drift_count: dict[str, int] = {}
        self._enable_unreadable: dict[str, int] = {}
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
        # Do we still OWN these channels? Checked before any duty is computed
        # so a reclaimed channel gets its real target this same poll.
        self._verify_channels(cfg)
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
            # A stall episode owns this header outright. The curve is what was
            # asking for 80 % while the blower read 0 RPM, and a rate-limited
            # crawl toward full duty costs seconds we do not have: pin it.
            kd = self._kick_duty.get(fan)
            new = int(kd) if kd is not None \
                else curve.rate_limit(self._duty.get(fan), tgt, max_rate)
            if new != self._duty.get(fan):
                self._write_fan(cfg, fan, new, t, src_id)
            # write-error and ownership-lost stay visible: they describe the
            # channel, not this poll's duty decision, and status.json is where
            # an operator looks DURING the fault (a reclaim that keeps failing
            # must not read as "ok")
            if self._fan_state.get(fan) not in ("write-error",
                                                "ownership-lost"):
                self._fan_state[fan] = "failsafe" if failsafe else "ok"
        self._check_fans(cfg)

    def _check_fans(self, cfg):
        """Read one tach per controlled fan: cache it for status.json and
        alarm when we command real duty but the tach says the fan is not
        turning (stalled, tach pin unplugged, or a header the firmware took
        back). Commanded duty alone never proves that anything cools.

        A confirmed stall does two things. It escalates (`_escalate`), because
        a stopped blower has to reach a person. And it acts, on the header and
        nowhere else (`_stall_drive`): pin the channel to `stall_full_duty` and
        try to restart the fan. "More duty cannot fix a fan that is not
        turning" is true of a fan that is jammed and false of one that has
        simply dropped out, and the only way to learn which we have is to ask
        it at full voltage."""
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
            # A fan we drive with no tach is a fan nobody can see: every stall
            # path below compares RPM against commanded duty, so with no tach
            # it stays silent however hot the card gets. Say so once (and every
            # 30 polls) instead of leaving the operator to discover it from a
            # blank RPM column.
            if tach is None:
                n = self._no_tach_count.get(fan, 0) + 1
                self._no_tach_count[fan] = n
                if n == 1 or n % 30 == 0:
                    self.log.warning(
                        "FAN %s is driven with NO tach mapping: RPM stays "
                        "unknown and a stall cannot be detected — set "
                        "assignments.*.tach for it (TUI: screen 2, tach list)",
                        fan)
            elif self._rpm[fan] is None:
                # mapped, but the mapping produces nothing: the chip is gone,
                # or there is no fanN_input behind that number
                n = self._no_tach_count.get(fan, 0) + 1
                self._no_tach_count[fan] = n
                if n == 1 or n % 30 == 0:
                    self.log.warning(
                        "FAN %s: tach %s yields no RPM reading — a stall on "
                        "this header cannot be detected", fan, tach)
            else:
                self._no_tach_count.pop(fan, None)
            duty = self._duty.get(fan) or 0
            # A kick of ours parks the header at 0 for a poll. That 0 RPM is
            # something we did on purpose, so it must not raise the alarm we
            # are pulsing to answer.
            ours = (self._kick.get(fan) or {}).get("phase") == "stop"
            # An episode keeps itself armed whatever the duty: un-arming because
            # our own pin sits below stall_min_duty would stop us watching the
            # one fan we are fighting for. Creating one still needs the full
            # threshold, so a fan idling below it stays nobody's emergency.
            if self._rpm[fan] == 0 and (duty >= min_duty or fan in self._kick):
                n = self._stall_count.get(fan, 0) + 1
                self._stall_count[fan] = n
                if n >= after:
                    self._fan_state[fan] = "stalled"
                    if not ours:
                        self._escalate(cfg, fan, "stalled",
                                       f"tach {tach} reads 0 RPM at {duty}% "
                                       f"duty for {n} polls", self._stall_eps)
                if not ours and (n == after or n % after == 0):
                    self.log.error(
                        "FAN %s STALLED: commanding %d%% but tach %s reads "
                        "0 RPM for %d polls", fan, duty, tach, n)
            else:
                self._stall_count.pop(fan, None)
                if fan in self._stall_eps:
                    self.log.warning("FAN %s RECOVERED: tach %s reads %s RPM "
                                     "again", fan, tach, self._rpm[fan])
                    self._stall_eps.pop(fan, None)
                    self._alert(cfg, fan, "recovered",
                                f"tach {tach} reads {self._rpm[fan]} RPM again")
            if self._rpm[fan] is None:
                seen = "unknown"
            elif self._rpm[fan] > 0:
                seen = "spinning"
            elif ours:
                seen = "pulsing"
            else:
                seen = "stopped"
            self._stall_drive(cfg, fan, seen)

    # ------------------------------------------------- stalled-fan action --

    def _stall_full_duty(self, cfg) -> int:
        """Duty a stalled fan's header is pinned to. Never below
        `stall_min_duty`: pinning a fan under the alarm threshold would disarm
        the detector that started the episode (validate() rejects that
        pairing; this is the runtime belt)."""
        return int(max(self._cfg_num(cfg, "stall_full_duty", 100.0),
                       self._cfg_num(cfg, "stall_min_duty", 20.0)))

    def _can_pulse(self, cfg, fan: str) -> bool:
        """May we drop this header to 0 to wind up a kick?

        The pulse is the one part of this feature that can cost cooling, so it
        runs only where nothing else depends on that header and we actually
        hold it:
          * shared — another assignment wants air out of this fan right now;
            dropping it to help a stalled card is not ours to decide;
          * not ours — `pwmN_enable` is not 1, so every write here is ignored
            and a pulse would be a lie with a 0 in it (`_lose_channel` owns
            that fault, and reclaiming comes first).
        """
        if self._lost_count.get(fan):
            return False
        n = sum(1 for a in (cfg.assignments.values() if cfg else [])
                if a.get("enabled", True) and fan in a.get("fans", []))
        return n <= 1

    def _stall_drive(self, cfg, fan: str, seen: str):
        """Act on a stalled fan, inside the fan header and nowhere else.

        PIN — hold the channel at `stall_full_duty` (default 100 %), past the
        curve's ceiling and past the rate limiter. The curve is what was asking
        a blower reading 0 RPM for 80 %; whatever it decided, the answer to a
        stopped fan is never less than everything the header can give, and
        ramping there at 6 %/poll spends ~15 s on the way.

        KICK — try to restart it: `stall_kick_stop_s` at 0 so a jammed blade
        can swing free, then `stall_kick_run_s` at full duty, judged by the
        tach rather than by hope. `stall_kick_attempts` lunges per round, and
        when a round runs out the header *stays* pinned and rests
        `stall_kick_backoff_s` before the next round, so a seized winding does
        not cook itself or the fan driver on stall current indefinitely. A
        bearing that seizes hot often frees once it stops trying; that is what
        the repeat is for. `stall_kick: false` keeps the pin and skips the
        pulses.

        Then stop. Restarting a fan is a fan-header action; what the box should
        do with a GPU that has no air is still an operator decision in
        `stall_alert_command`, and this daemon still has no path to a GPU at
        all. Escalation is unchanged and keeps running underneath.

        Nothing here sleeps: each phase lasts at least its configured time and
        at most one poll longer, because this loop is what keeps every OTHER fan
        alive — the same reasoning that bounds `stall_alert_command`'s timeout.

        `seen` is what the tach said this poll: "spinning", "stopped", "pulsing"
        (0 RPM we caused ourselves — the phase clock still advances, so a pulse
        can never become a place the fan is left, and only the alarm stays
        quiet), or "unknown" (no tach to judge by).
        """
        ep = self._kick.get(fan)
        now = time.monotonic()

        if seen == "unknown":
            return          # no tach configured: nothing to judge by

        kick_on = bool((cfg.config if cfg else {}).get("stall_kick", True))

        if seen == "spinning":
            if ep is None:
                return
            if ep["phase"] == "cooldown":
                if now >= ep["until"]:
                    self._end_stall_drive(cfg, fan)
                return
            # Turning again — mid-pulse included, which is why a pulse can
            # never leave a spinning fan parked low: go to full and hold it
            # there before the curve gets the wheel back.
            cd = self._cfg_num(cfg, "stall_kick_cooldown_s", 60.0)
            ep["phase"] = "cooldown"
            ep["until"] = now + cd
            self._kick_duty[fan] = self._stall_full_duty(cfg)
            self.log.warning("FAN %s is turning again after %d kick attempt(s): "
                             "holding %d%% for %.0fs before the curve takes "
                             "over", fan, ep["attempts"], self._kick_duty[fan],
                             cd)
            return

        # ------------------------------------------- seen == stopped/pulsing --
        if ep is None:
            if self._fan_state.get(fan) != "stalled":
                return                    # not confirmed yet: the alarm decides
            may = kick_on and self._can_pulse(cfg, fan)
            ep = self._kick[fan] = {
                "phase": "lunge", "attempts": 1, "pulse": may,
                "until": now + self._cfg_num(cfg, "stall_kick_run_s", 3.0)}
            self._kick_duty[fan] = self._stall_full_duty(cfg)
            self.log.critical(
                "DRIVING %s to %d%% (the curve asked for %s%%): a fan reading "
                "0 RPM is not being asked for enough. %s", fan,
                self._kick_duty[fan], self._duty.get(fan),
                "Kicking: drop to 0 so a jammed blade can swing free, then full "
                "voltage, and let the tach decide."
                if may else
                "No kick (header shared or not ours): holding full duty.")
            return

        if now < ep["until"]:
            return                        # the current phase is still running

        run_s = self._cfg_num(cfg, "stall_kick_run_s", 3.0)
        tries = int(self._cfg_num(cfg, "stall_kick_attempts", 3))
        full = self._stall_full_duty(cfg)
        # Re-decided every transition, not remembered from the start: the
        # firmware can take the header back mid-episode, and a pulse into a
        # channel we no longer own is theatre with a 0 in it.
        ep["pulse"] = kick_on and self._can_pulse(cfg, fan)

        if ep["phase"] == "pin":
            return                        # kicking is off: the pin IS the action

        if ep["phase"] == "lunge":
            if not ep["pulse"]:
                ep["phase"] = "pin"
                ep["until"] = now + 1e12
                return
            if ep["attempts"] >= tries:
                backoff = self._cfg_num(cfg, "stall_kick_backoff_s", 30.0)
                ep["phase"] = "backoff"
                ep["until"] = now + backoff
                ep["attempts"] = 0
                self._kick_duty[fan] = full
                self.log.warning(
                    "KICK %s: %d attempts and still 0 RPM — holding %d%% and "
                    "resting %.0fs before the next round. This fan needs a "
                    "hand, not more voltage.", fan, tries, full, backoff)
                return
            stop_s = self._cfg_num(cfg, "stall_kick_stop_s", 0.5)
            ep["phase"] = "stop"
            ep["until"] = now + stop_s
            self._kick_duty[fan] = 0
            self.log.warning("KICK %s: attempt %d/%d — dropping to 0%% for "
                             "%.1fs so a jammed blade can swing free",
                             fan, ep["attempts"] + 1, tries, stop_s)
            return

        if ep["phase"] in ("stop", "backoff"):
            ep["phase"] = "lunge"
            ep["until"] = now + run_s
            ep["attempts"] += 1
            self._kick_duty[fan] = full
            self.log.warning("KICK %s: attempt %d/%d — full duty for %.1fs, "
                             "the tach decides", fan, ep["attempts"], tries,
                             run_s)

    def _end_stall_drive(self, cfg, fan: str):
        """Hand the header back to the curve. `self._duty` is still full, so
        it walks down at the normal rate rather than falling off a cliff."""
        ep = self._kick.pop(fan, None)
        self._kick_duty.pop(fan, None)
        if ep is not None:
            self.log.info("FAN %s: stall drive over — the curve takes its duty "
                          "back from %d%%", fan, self._duty.get(fan) or 0)

    # ------------------------------------------- ownership re-verification --

    def _verify_channels(self, cfg):
        """Re-check EVERY poll that each channel we took over is still ours.

        `pwmN` is a plain sysfs file: the firmware/EC can flip `pwmN_enable`
        back to 2 (or 0) at any moment — a BIOS polling loop, an EC watchdog,
        a hot-replug, another controller — and this board then parks the
        header wherever its policy wants, including 0 %. A duty write into a
        channel we no longer own *succeeds*: the log cheerfully reports
        "write it8792:pwm1 = 100%" while the fan stays stopped and the card
        heats, which is precisely how a GPU blower ends up stuck at 0 RPM with
        a daemon that looks healthy. Commanded duty is not ownership.

        So: re-read `pwmN_enable`, reclaim on any loss, and re-assert the duty
        too (with a second writer our remembered duty can differ from what the
        chip holds, and `new != self._duty[fan]` would then skip the write
        forever, pinning the fan at someone else's value).

        Re-asserting every poll is deliberate — an EC that steals the header
        every second has to be beaten every second. Only the LOG is
        rate-limited. `--dry-run` still detects and reports, but writes
        nothing.
        """
        for fan in sorted(self._taken_over):
            res = hwmon.resolve_fan(fan, self._chips, self.root)
            if res is None:
                continue          # _write_fan already reports unresolved channels
            chip_dir, pwm = res
            en = hwmon.get_enable(chip_dir, pwm)
            if en == hwmon.ENABLE_MANUAL:
                if fan in self._lost_count:
                    self.log.warning("ownership of %s re-established "
                                     "(pwm%d_enable is 1 again)", fan, pwm)
                    self._lost_count.pop(fan, None)
                    # cleared here, since pass 2 deliberately leaves
                    # ownership-lost alone; pass 2 re-decides ok/failsafe below
                    self._fan_state[fan] = "ok"
                    if fan in self._lost_eps:
                        self._lost_eps.pop(fan, None)
                        self._alert(cfg, fan, "ownership-recovered",
                                    f"pwm{pwm}_enable reads 1 again")
                self._enable_unreadable.pop(fan, None)
                self._reassert_duty(fan, chip_dir, pwm)
                continue
            if en is None:
                # Cannot read enable. Do NOT write enable blind: we do not
                # know who holds it. The tach alarm still covers the fan.
                n = self._enable_unreadable.get(fan, 0) + 1
                self._enable_unreadable[fan] = n
                if n == 1 or n % 30 == 0:
                    self.log.warning("cannot read pwm%d_enable for %s (%d "
                                     "polls in a row) — ownership unconfirmed",
                                     pwm, fan, n)
                continue
            self._lose_channel(cfg, fan, chip_dir, pwm, en)

    def _lose_channel(self, cfg, fan: str, chip_dir, pwm: int, en: int):
        """The firmware/EC took a channel back while we were running."""
        n = self._lost_count.get(fan, 0) + 1
        self._lost_count[fan] = n
        if n == 1 or n % 30 == 0:
            self.log.critical(
                "OWNERSHIP LOST on %s: pwm%d_enable reads %d, not 1 — the "
                "firmware took the channel back, so our duty writes are "
                "IGNORED and the fan is now wherever its policy leaves it "
                "(tach last read %s RPM; on this board that can be 0). "
                "Reclaiming every poll until it holds (%d polls in a row).",
                fan, pwm, en, self._rpm.get(fan), n)
        rpm = self._rpm.get(fan)
        # Re-run the full takeover path, insisting on a header the firmware
        # left DISABLED: we held it manual, so a 0 here is theft, not intent.
        # Forget the remembered duty so the first write after reclaiming is
        # the real target, not a rate-limited crawl from a value the chip
        # never received.
        self._taken_over.discard(fan)
        self._duty.pop(fan, None)
        self._rpm.pop(fan, None)
        self._stall_count.pop(fan, None)
        # A stall episode must not leave a 0 sitting in a header we just lost:
        # the pulse is our decision, and ours to take back. Drop it, pin full,
        # and let the reclaim finish before anything else is tried.
        if self._kick.get(fan) is not None and (self._kick_duty.get(fan)
                                                or 0) < self._stall_full_duty(
                                                    cfg):
            self._kick[fan]["phase"] = "backoff"
            self._kick[fan]["until"] = time.monotonic() + self._cfg_num(
                cfg, "stall_kick_backoff_s", 30.0)
            self._kick_duty[fan] = self._stall_full_duty(cfg)
        self._takeover(fan, insist=True)
        # AFTER the takeover attempt: a successful one ends by setting "ok",
        # which would erase the fault we are reporting. A failed one keeps its
        # own write-error state, which is the more precise truth.
        if fan in self._taken_over:
            self._fan_state[fan] = "ownership-lost"
        self._escalate(cfg, fan, "ownership-lost",
                       f"pwm{pwm}_enable reads {en}, not 1 — our duty writes "
                       f"were being ignored (tach last read {rpm} RPM)",
                       self._lost_eps)

    def _reassert_duty(self, fan: str, chip_dir, pwm: int):
        """Re-write `pwmN` if the chip no longer holds what we wrote."""
        want_pct = self._duty.get(fan)
        if want_pct is None:
            return
        want = curve.pct_to_raw255(want_pct)
        have = hwmon.read_duty_raw(chip_dir, pwm)
        if have is None or have == want:
            if fan in self._drift_count:
                self.log.info("duty on %s is back to what we wrote (raw %d)",
                              fan, want)
                self._drift_count.pop(fan, None)
            return
        n = self._drift_count.get(fan, 0) + 1
        self._drift_count[fan] = n
        if n == 1 or n % 30 == 0:
            self.log.warning("duty on %s reads raw %d but we wrote raw %d "
                             "(%d polls in a row) — another writer? "
                             "re-asserting", fan, have, want, n)
        if self.dry_run:
            return
        try:
            hwmon.write_duty(chip_dir, pwm, want)
        except OSError as e:
            self._fan_state[fan] = "write-error"
            self.log.error("re-asserting duty on %s failed: %s — keeping last "
                           "duty, retrying", fan, e)

    @staticmethod
    def _cfg_num(cfg, key: str, default: float) -> float:
        """A config number that cannot break an escalation path.

        `cfg` can legitimately be None (degraded mode, where the file is
        invalid by definition) and any value in it can be any type, so the
        safety path falls back to the default rather than raising out of a
        poll that is trying to save a fan."""
        try:
            return float((cfg.config if cfg else {}).get(key, default))
        except (TypeError, ValueError, AttributeError):
            return float(default)

    # --------------------------------------------------------- escalation --

    def _escalate(self, cfg, fan: str, kind: str, detail: str,
                  eps: dict[str, float]):
        """A fan that cannot cool is not a log line.

        Neither of these faults is fixable by writing duty — the fan is either
        stopped or no longer ours — so escalation does the two things that
        still have an effect: shout at CRITICAL, and hand the event to an
        operator hook (`stall_alert_command`). Fires on the transition into
        the fault and then at most every `stall_alert_repeat_s` seconds
        (0 = once per episode), so a stall that lasts hours nags without
        flooding.

        And it deliberately stops there. This is a fan controller: what the
        box should do with its GPUs when a fan fails is an operator decision,
        expressible in the alert command, not something the daemon does to
        somebody's running job on its own initiative.

        `eps` is the episode map for THIS kind of fault: a tach stall ends
        when the fan spins again, an ownership loss ends when the channel
        comes back to manual mode. Sharing one map would let a spinning tach
        "recover" an ownership fault we are still losing every poll.
        """
        now = time.monotonic()
        last = eps.get(fan)
        repeat = self._cfg_num(cfg, "stall_alert_repeat_s", 300.0)
        if last is not None and (repeat <= 0 or now - last < repeat):
            return
        eps[fan] = now
        if last is None:
            self.log.critical("ESCALATING %s (%s): %s — more duty cannot fix a "
                              "fan that is not turning", fan, kind, detail)
        self._alert(cfg, fan, kind, detail)

    def _alert(self, cfg, fan: str, kind: str, detail: str):
        """Run `stall_alert_command` (a shell one-liner: mail, MQTT publish,
        desktop notification, ...). The event is passed in the environment:
        ALX_FAN, ALX_STATE (stalled | ownership-lost | recovered |
        ownership-recovered), ALX_DETAIL, ALX_DUTY_PCT, ALX_RPM, ALX_CONFIG.

        Best effort and bounded by `stall_alert_timeout_s`: the poll loop is
        what keeps every OTHER fan alive, so a hung alert script has to cost
        at most its timeout, never the loop."""
        cmd = cfg.config.get("stall_alert_command") if cfg else None
        if not isinstance(cmd, str) or not cmd.strip():
            return
        timeout = self._cfg_num(cfg, "stall_alert_timeout_s", 5.0)
        env = dict(os.environ)
        env.update({
            "ALX_FAN": fan,
            "ALX_STATE": kind,
            "ALX_DETAIL": detail,
            "ALX_DUTY_PCT": "" if self._duty.get(fan) is None
                            else str(self._duty[fan]),
            "ALX_RPM": "" if self._rpm.get(fan) is None else str(self._rpm[fan]),
            "ALX_CONFIG": str(self.cfg_path),
        })
        ok, out = hwmon.run_quiet(["/bin/sh", "-c", cmd],
                                  timeout=timeout or None, env=env)
        if ok:
            self.log.info("alert command for %s (%s): %s", fan, kind, out)
        else:
            self.log.error("alert command for %s (%s) failed: %s — the fault "
                           "stands, nothing else was notified", fan, kind, out)

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

    def _takeover(self, fan: str, insist: bool = False):
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
            if fan not in self._force_fans and not insist:
                self._refuse(fan, "disabled", "warning",
                             f"channel {fan} disabled (enable=0) — skipping")
                return
            if insist and fan not in self._force_fans:
                self.log.critical("%s was left disabled (enable=0) while we "
                                  "hold it — enabling it: a header parked at 0 "
                                  "cools nothing", fan)
            else:
                self.log.warning("force_takeover: %s was disabled (enable=0) — "
                                 "enabling it as configured", fan)
        # what to hand back on exit: the value recorded before WE first took
        # it (from the marker), not the 1 we are about to write. Kept across a
        # mid-run reclaim, so an EC that steals and releases the header still
        # gets the board's original setting back, not our 1.
        orig = self._marker_orig.get(fan)
        if orig is None:
            orig = self._orig_enable.get(fan)
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
        # An unresolved ownership fault stays up until _verify_channels sees
        # enable back at 1: a duty write into a header the firmware holds also
        # "succeeds", so a successful write is not evidence that the fault went
        # away (that illusion is what hid the original stuck-at-0 incident).
        if self._fan_state.get(fan) != "ownership-lost":
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
        # Never hand back a header parked at 0 while its fan is the one we
        # could not restart: this board parks headers the firmware owns, and an
        # exiting daemon must not be what stopped the air. Compare the CHIP,
        # not our own idea of the duty — a phase can advance in the same poll
        # that wrote the pulse, and then the 0 is only in the chip.
        ep = self._kick.get(fan)
        full = self._stall_full_duty(self._cfg)
        if ep is not None:
            held = hwmon.read_duty_raw(chip_dir, pwm)
            if held is not None and held < curve.pct_to_raw255(full):
                try:
                    hwmon.write_duty(chip_dir, pwm,
                                     curve.pct_to_raw255(full))
                    self.log.warning("leaving %s pinned at full duty on exit: "
                                     "its fan was stalled at raw %d and a "
                                     "parked header cools nothing", fan, held)
                except OSError as e:
                    self.log.error("could not leave %s at full duty on exit: %s",
                                   fan, e)
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
                # which tach backs that RPM (null = none: the fan is unwatched
                # for stalls) — published so a reader does not have to guess
                "tach": self._tach_for(fan, cfg),
                "orig_enable": self._orig_enable.get(fan),
                "state": self._fan_state.get(fan, "idle"),
            }
            ep = self._kick.get(fan)
            if ep:
                # an operator (and the TUI) should see that the daemon is
                # actively fighting for this fan, not merely reporting on it
                entry["kick"] = {"phase": ep["phase"],
                                 "attempts": ep["attempts"]}
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
                    f"0 RPM for {n} polls"
                    + (" — ESCALATED" if fan in self._stall_eps else ""))
        for fan, n in sorted(self._lost_count.items()):
            data["errors"][f"ownership-lost:{fan}"] = (
                f"pwmN_enable left manual mode ({n} poll(s)) — the firmware "
                f"took it back; duty writes before the reclaim were ignored")
        for fan, n in sorted(self._drift_count.items()):
            data["errors"][f"duty-drift:{fan}"] = (
                f"pwmN does not hold the duty we wrote ({n} polls) — second "
                f"writer? re-asserting every poll")
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
    # boot_id / pid liveness live in status.py, next to the marker they
    # describe: the TUI has to answer the same "is this channel ours or
    # someone else's" question to tell the truth on the Assign screen, and two
    # implementations of that rule would drift apart.
    _boot_id = staticmethod(status_mod.boot_id)

    def _load_marker(self):
        """Read which channels the previous alx_fancontrol run held.

        Trusted only when it is the SAME boot (our own restart after a
        SIGKILL/OOM/power blip) and no other alx_fancontrol daemon is alive.
        Then those channels are RECLAIMABLE: found in manual mode we take
        them back instead of refusing them forever — which is how a killed
        daemon used to leave a GPU fan pinned at its last raw duty (possibly
        0) with the service reporting itself healthy."""
        claim = status_mod.claimed_channels(self._marker_path,
                                            self._boot_id())
        self._marker_orig = dict(claim["channels"])
        if claim["live_pid"]:
            self.log.error(
                "another alx_fancontrol daemon (pid %d) owns %s — refusing to "
                "take those channels over", claim["live_pid"],
                ", ".join(sorted(claim["channels"])) or "nothing")
            self._marker_orig = {}
            self._reclaimable = set()
            return
        self._reclaimable = set(claim["stale"])
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
            # especially here: a header the firmware takes back can no longer
            # be raised by us, and degraded mode is exactly when nobody is
            # watching. cfg is None (that is why we are here), so escalation
            # runs on defaults.
            self._verify_channels(None)
            for fan in sorted(self._taken_over):
                if self._duty.get(fan) != duty:
                    self._write_fan(None, fan, duty, None,
                                    "fail-safe (invalid config)")
                if self._fan_state.get(fan) not in ("write-error",
                                                    "ownership-lost"):
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
        elif any(v == "ownership-lost" for v in self._fan_state.values()):
            state = "OWNERSHIP LOST (firmware took a fan channel back)"
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
