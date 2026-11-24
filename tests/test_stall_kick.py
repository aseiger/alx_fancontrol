"""A confirmed stall is an order as well as a log line — inside the header.

The live regression this closes is 2026-11-23, card 0 (V100, PCI 47:00.0):
its blower's tach went to 0 RPM at 12:00:19 while commanded 60 %, the daemon
escalated within ten seconds — correctly — and then spent the next nineteen
minutes logging `FAN it8792:pwm1 STALLED` at a card it was asking for 80 % of,
because that is all `_check_fans` did. The card stopped answering nvidia-smi at
~12:19 and the box went down with it. Detection was never the problem. The
daemon watched a fan die without ever trying to start it, and the curve's
ceiling (80 %) was the last thing it asked for.

So a confirmed stall now also *acts*, on the fan header and nowhere else:

  * PIN   — hold the channel at `stall_full_duty`, past the curve ceiling and
            past the rate limiter;
  * KICK  — a brief 0 so a jammed blade can swing free, then full duty, judged
            by the tach; bounded attempts, spaced rounds, pinned throughout.

Escalation is untouched: reaching a person is still the other half, and this
daemon still has no path to a GPU (see `test_ownership.py`).

Hermetic like the rest of the suite: fake hwmon tree + injected nvidia-smi.
Kick phases are timed in seconds, so these tests drive a virtual clock — see
`run_clocked`, which advances monotonic time by `step` per poll instead of
sleeping.
"""
import inspect
import json
import logging

import alx_fancontrol.daemon as dm
import alx_fancontrol.hwmon as hw
from alx_fancontrol import config as config_mod
from alx_fancontrol.daemon import Daemon
from test_safety_net import (enable, make_config, make_tree, pwm, status)

FAN = "it8792:pwm1"
WARM = "00000000:43:00.0, 55\n"      # curve asks for 33 % (raw 84) — not 100
SPINNING = "1500\n"


def set_rpm(root, value):
    (root / "hwmon0" / "fan1_input").write_text(str(value) + "\n")


def run_clocked(d, polls, step=2.0, observe=None):
    """`run()` for `polls` iterations on a virtual clock.

    Each sleep advances `time.monotonic()` by `step` seconds, so kick phases
    (configured in seconds) become deterministic instead of racing the test
    runner. Same contract as `test_safety_net.run_polls`: `observe(d, n)` runs
    BETWEEN polls, while the daemon still owns the channels."""
    d._watchdog_seconds = 0
    d._stop = False
    clock = {"t": 1000.0}
    n = {"i": 0}
    orig_sleep, orig_mono = dm.time.sleep, dm.time.monotonic

    def fake_sleep(_s):
        clock["t"] += step
        n["i"] += 1
        if observe:
            observe(d, n["i"])
        if n["i"] >= polls:
            d._stop = True

    dm.time.sleep = fake_sleep
    dm.time.monotonic = lambda: clock["t"]
    try:
        rc = d.run()
    finally:
        dm.time.sleep = orig_sleep
        dm.time.monotonic = orig_mono
    return rc


def duty_trace(tmp_path, root, observe):
    """Watch every duty the chip actually holds, poll by poll."""
    seen = []

    def watch(d, n):
        seen.append(pwm(root))
        if observe:
            observe(d, n)
    return seen, watch


# ------------------------------------------------------------------- pin --

def test_a_stalled_fan_is_pinned_past_what_the_curve_asked(tmp_path, caplog):
    """The 12:00 failure, replayed: the curve wanted 33 %, so that is what it
    got for nineteen minutes. A fan reading 0 RPM is now driven to full
    whatever the curve decided."""
    caplog.set_level(logging.DEBUG)
    root = make_tree(tmp_path, pwm1=(0, 2))
    set_rpm(root, "0")
    d = Daemon(make_config(tmp_path, stall_after=2), root=root,
               gpu_runner=lambda *f: WARM)

    run_clocked(d, polls=5)

    assert "DRIVING it8792:pwm1 to 100%" in caplog.text
    assert pwm(root) == "255", "the header must hold everything it can give"


def test_the_pin_jumps_rather_than_crawling_there(tmp_path, caplog):
    """max_rate exists to keep a fan's inrush off the rail during normal
    control. A fan that just stopped is not normal control: waiting ~11 polls
    at 6 %/poll for full duty is the difference between a kick and a funeral."""
    caplog.set_level(logging.WARNING)
    root = make_tree(tmp_path, pwm1=(0, 2))
    set_rpm(root, SPINNING)
    d = Daemon(make_config(tmp_path, stall_after=1, max_rate=6), root=root,
               gpu_runner=lambda *f: WARM)

    seen, watch = duty_trace(tmp_path, root, None)

    def stall_at_three(_d, n):
        if n == 3:
            set_rpm(root, "0")
        watch(_d, n)

    run_clocked(d, polls=6, observe=stall_at_three)

    assert "84" in seen, "before the stall it sits where the curve puts it"
    # 84 -> 255 in one move: no 90, no 96, no crawl through the middle
    assert set(seen[seen.index("84"):]) <= {"84", "255"}, \
        f"the pin must not ramp: {seen}"
    assert seen[-1] == "255"


class _Cfg:
    """Just enough Config for the arithmetic helpers."""
    assignments: dict = {}

    def __init__(self, **kw):
        self.config = kw


def test_the_pin_never_sits_below_the_alarm_threshold():
    """validate() refuses `stall_full_duty` < `stall_min_duty`, because a pin
    under the alarm threshold parks the fan where the detector that pinned it
    can no longer see it. If such values arrive anyway — hand-edited file,
    degraded defaults, cfg=None — the runtime belt clamps them anyway."""
    d = Daemon.__new__(Daemon)          # pure arithmetic, no logging needed
    assert d._stall_full_duty(_Cfg(stall_full_duty=10, stall_min_duty=60)) == 60
    assert d._stall_full_duty(_Cfg(stall_full_duty=80, stall_min_duty=20)) == 80
    assert d._stall_full_duty(None) == 100


# ------------------------------------------------------------------ kick --

def test_a_kick_drops_the_header_and_then_lunges(tmp_path, caplog):
    """A stalled blade often frees if it is allowed to stop swinging and then
    hit full voltage. The pulse has to really land on the chip, not just be
    described in the log."""
    caplog.set_level(logging.WARNING)
    root = make_tree(tmp_path, pwm1=(0, 2))
    set_rpm(root, "0")
    d = Daemon(make_config(tmp_path, stall_after=1), root=root,
               gpu_runner=lambda *f: WARM)

    seen, watch = duty_trace(tmp_path, root, None)
    run_clocked(d, polls=8, step=2.0, observe=watch)

    assert "attempt 2/3" in caplog.text, "the second lunge should be announced"
    # pinned, dropped to 0 by us, then driven again — in that order
    assert "255" in seen and "0" in seen
    assert seen.index("0") > seen.index("255")
    assert seen[-1] == "255", "after a failed pulse the header goes back to full"


def test_attempts_are_bounded_and_the_rounds_are_spaced(tmp_path, caplog):
    """A seized winding drawing stall current forever cooks itself and the fan
    driver. Attempts per round are capped; when a round runs out we rest
    `stall_kick_backoff_s`, still pinned, then try again."""
    caplog.set_level(logging.WARNING)
    root = make_tree(tmp_path, pwm1=(0, 2))
    set_rpm(root, "0")
    d = Daemon(make_config(tmp_path, stall_after=1, stall_kick_attempts=2,
                           stall_kick_backoff_s=30), root=root,
               gpu_runner=lambda *f: WARM)

    run_clocked(d, polls=12, step=2.0)

    assert "still 0 RPM" in caplog.text
    assert "resting 30s before the next round" in caplog.text
    lunges = [r for r in caplog.records if "attempt 3/" in r.getMessage()]
    assert not lunges, "attempts beyond the cap must not fire"


def test_kick_disabled_still_pins_and_never_touches_zero(tmp_path, caplog):
    """The pulse is the part that can cost cooling, so it has a switch. With it
    off, the pin — which cannot cost anything — still happens."""
    caplog.set_level(logging.WARNING)
    root = make_tree(tmp_path, pwm1=(0, 2))
    set_rpm(root, "0")
    d = Daemon(make_config(tmp_path, stall_after=1, stall_kick=False),
               root=root, gpu_runner=lambda *f: WARM)

    seen, watch = duty_trace(tmp_path, root, None)
    run_clocked(d, polls=8, step=2.0, observe=watch)

    assert "DRIVING it8792:pwm1 to 100%" in caplog.text
    assert "KICK" not in caplog.text
    assert "0" not in seen[1:], f"never pulse with the kick off: {seen}"


# ------------------------------------------------------- restarting it wins --

def test_a_fan_that_starts_is_held_at_full_then_handed_back(tmp_path, caplog):
    """Recovery must not be a cliff. Once the tach spins we hold full duty for
    `stall_kick_cooldown_s` — a bearing that just freed is not yet trustworthy —
    and only then let the curve walk it back down at its own rate."""
    caplog.set_level(logging.DEBUG)
    root = make_tree(tmp_path, pwm1=(0, 2))
    set_rpm(root, "0")
    d = Daemon(make_config(tmp_path, stall_after=1, max_rate=6,
                           stall_kick_cooldown_s=10), root=root,
               gpu_runner=lambda *f: WARM)

    def spin_at_four(_d, n):
        if n == 4:
            set_rpm(root, SPINNING)

    seen, watch = duty_trace(tmp_path, root, spin_at_four)
    run_clocked(d, polls=8, step=20.0, observe=watch)

    assert "is turning again" in caplog.text
    assert "stall drive over" in caplog.text
    assert seen[-1] != "255", "the curve has the wheel back"
    assert int(seen[-1]) < 255, f"walking down, not falling: {seen}"


def test_a_pulse_never_leaves_the_header_parked_on_exit(tmp_path, caplog):
    """The one moment this feature could strand a fan at 0 is an exit landing
    inside the drop — SIGTERM during a kick, say. Handing a parked header to
    firmware that parks it right back is how the card finishes the job we were
    trying to undo."""
    caplog.set_level(logging.WARNING)
    root = make_tree(tmp_path, pwm1=(0, 2))
    set_rpm(root, "0")
    d = Daemon(make_config(tmp_path, stall_after=1), root=root,
               gpu_runner=lambda *f: WARM)
    caught = {"poll": 0}

    def stop_on_a_pulse_write(_d, n):
        # The window that matters: the pulse has reached the chip, and this is
        # where a SIGTERM would land.
        if pwm(root) == "0":
            caught["poll"] = n
            _d._stop = True

    run_clocked(d, polls=20, step=2.0, observe=stop_on_a_pulse_write)

    assert caught["poll"], "the exit must really have landed inside a pulse"
    assert enable(root) == "2", "the channel really is handed back"
    assert pwm(root) == "255", "and never while parked at 0"


# --------------------------------------------------------- where we may not --

def test_a_shared_header_is_pinned_but_never_pulsed(tmp_path, caplog):
    """Two cards on one fan: dropping that header to 0 to kick a stalled
    blower steals air from the other card. Helping one is not licence to
    hurt the other, so a shared header only ever gets the pin."""
    caplog.set_level(logging.WARNING)
    root = make_tree(tmp_path, pwm1=(0, 2))
    set_rpm(root, "0")
    cfg = json.loads(make_config(tmp_path, stall_after=1).read_text())
    cfg["assignments"]["GPU 44:00.0"] = {
        "source": "gpu:44:00.0", "curve": "cool", "fans": [FAN],
        "enabled": True}
    (tmp_path / "config.json").write_text(json.dumps(cfg))
    d = Daemon(tmp_path / "config.json", root=root,
               gpu_runner=lambda *f: WARM + "00000000:44:00.0, 45\n")

    seen, watch = duty_trace(tmp_path, root, None)
    run_clocked(d, polls=8, step=2.0, observe=watch)

    assert "No kick" in caplog.text
    assert "0" not in seen[1:], f"a shared fan keeps its air: {seen}"
    assert pwm(root) == "255"


def test_no_pulse_on_a_header_the_firmware_holds(tmp_path, caplog):
    """While the EC owns the channel every write here is ignored — so a pulse
    would be theatre with a 0 in it. Reclaim first (test_ownership.py owns that
    fault); pin, and do not pretend to kick."""
    caplog.set_level(logging.WARNING)
    root = make_tree(tmp_path, pwm1=(0, 2))
    set_rpm(root, "0")
    d = Daemon(make_config(tmp_path, stall_after=1), root=root,
               gpu_runner=lambda *f: WARM)

    def hold_it(_d, n):
        (root / "hwmon0" / "pwm1_enable").write_text("2\n")   # EC never lets go

    seen, watch = duty_trace(tmp_path, root, hold_it)
    run_clocked(d, polls=8, step=2.0, observe=watch)

    assert "OWNERSHIP LOST" in caplog.text
    assert "KICK" not in caplog.text
    assert "0" not in seen[1:], f"no theatre: {seen}"


# ------------------------------------------------------------- config --

def test_validate_rejects_a_pin_below_the_alarm_threshold(tmp_path):
    """`stall_full_duty` under `stall_min_duty` is self-defeating: the pin
    would move the fan below the duty at which a stall counts as a stall, and
    the detector that pinned it would go quiet. Refuse the pairing."""
    cfg = config_mod.load(make_config(tmp_path, stall_full_duty=10,
                                      stall_min_duty=20))
    errors, _warnings = config_mod.validate(cfg)

    assert any("stall_full_duty" in e and "stall_min_duty" in e
               for e in errors), errors


def test_validate_requires_a_real_bool_for_stall_kick(tmp_path):
    """`"false"` would be truthy: the pulses would run for someone who means
    them off, which is the one setting here that can cost cooling."""
    cfg = config_mod.load(make_config(tmp_path, stall_kick="false"))
    errors, _warnings = config_mod.validate(cfg)

    assert any("stall_kick must be true or false" in e for e in errors), errors


def test_junk_knob_values_still_drive_the_fan(tmp_path, caplog):
    """No knob may cost us the action. validate() blocks junk from a loaded
    config, but degraded mode calls this with cfg=None — and a poll that is
    trying to save a fan cannot afford a TypeError."""
    caplog.set_level(logging.WARNING)
    root = make_tree(tmp_path, pwm1=(0, 2))
    set_rpm(root, "0")
    d = Daemon(make_config(tmp_path, stall_after=1), root=root,
               gpu_runner=lambda *f: WARM)
    d._chips = hw.find_all_chips(root)          # normally set by run()
    d._takeover(FAN)
    d._fan_state[FAN] = "stalled"

    d._stall_drive(None, FAN, "stopped")        # must not raise

    assert d._kick_duty[FAN] == 100, "defaults still pin it"


def test_the_stall_drive_reaches_the_chip_not_the_gpu(tmp_path, monkeypatch):
    """PR #6's invariant, re-checked with the new action in the path: a fan
    fault may shell out to NOTHING but the alert hook, and pinning a header
    must not become a lever into the card."""
    root = make_tree(tmp_path, pwm1=(0, 2))
    set_rpm(root, "0")
    calls = []
    monkeypatch.setattr(hw, "run_quiet",
                        lambda argv, **kw: (calls.append(list(argv)),
                                            (True, "ok"))[1])
    d = Daemon(make_config(tmp_path, stall_after=1, stall_alert_repeat_s=0),
               root=root, gpu_runner=lambda *f: WARM)

    run_clocked(d, polls=8, step=2.0)

    assert pwm(root) == "255", "the fan action still happens"
    assert not calls, f"a fan fault runs nothing but the alert hook: {calls}"
    src = inspect.getsource(dm)
    assert "-pl" not in src and "power.limit" not in src


def test_status_reports_the_fight(tmp_path):
    """An operator looking during the fault should see the daemon working on
    it, not just recording that it is not turning."""
    root = make_tree(tmp_path, pwm1=(0, 2))
    set_rpm(root, "0")
    d = Daemon(make_config(tmp_path, stall_after=1), root=root,
               gpu_runner=lambda *f: WARM)

    run_clocked(d, polls=4, step=2.0)

    entry = status(tmp_path)["fans"][FAN]
    assert entry["state"] == "stalled"
    assert entry["kick"]["phase"] in ("lunge", "stop", "backoff")
    assert entry["kick"]["attempts"] >= 1
    assert entry["duty_pct"] != 33, "not under curve control any more"
