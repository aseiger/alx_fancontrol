"""The safety net: an unreadable temperature may only ever make a fan go
FASTER, and nothing may freeze or strand a fan.

These are the regressions behind "a fan controlled against the GPU temp
fails and gets stuck at 0%, and the card overheats" (see
IMPLEMENTATION_NOTES.md §18). All hermetic: fake hwmon tree + injected
nvidia-smi runner. The fake tree models what the real board does — a
header nobody owns sits where the firmware left it (cf. live it8686 pwm2
= 0 under firmware control), so "hand it back to the firmware" is not a
safe answer here.
"""
import json
import logging
import os
import subprocess
import threading
import time

import alx_fancontrol.daemon as dm
import pytest
from alx_fancontrol import hwmon
from alx_fancontrol.daemon import Daemon

HOT = "00000000:43:00.0, 72\n"        # curve top -> 100 %
IDLE = "00000000:43:00.0, 50\n"       # curve start -> 26 % (raw 66)
COOL = "00000000:43:00.0, 45\n"       # below curve start -> 26 %


def make_tree(tmp_path, pwm1=(0, 2)):
    """hwmon0 = it8792 (the chip driving the GPU blowers on the live box)."""
    root = tmp_path / "hwmon"
    d = root / "hwmon0"
    d.mkdir(parents=True)
    (d / "name").write_text("it8792_2008090d\n")
    duty, enable = pwm1
    (d / "pwm1").write_text(f"{duty}\n")
    (d / "pwm1_enable").write_text(f"{enable}\n")
    (d / "fan1_input").write_text("1500\n")
    return root


def make_config(tmp_path, assignment_extra=None, **global_extra):
    cfg = {
        "version": 2,
        "config": {"poll_seconds": 1.0, "max_rate": 6, "floor_duty": 5,
                   "dead_temp_c": -10, "failsafe_after": 2,
                   "failsafe_duty": 100, "failsafe_max_rate": 25,
                   "watchdog_seconds": 0, "stall_after": 3},
        "curves": {"gpu_v100": {"label": "V100 blower",
                                "points": [[50, 26], [60, 40], [66, 75],
                                           [72, 100]]},
                   "cool": {"label": "cold source", "points": [[40, 20]]}},
        "assignments": {
            "GPU 43:00.0": {"source": "gpu:43:00.0", "curve": "gpu_v100",
                            "fans": ["it8792:pwm1"], "enabled": True,
                            "tach": {"it8792:pwm1": "it8792:fan1"},
                            **(assignment_extra or {})},
        },
    }
    cfg["config"].update(global_extra)
    p = tmp_path / "config.json"
    p.write_text(json.dumps(cfg))
    return p


def run_polls(d, polls, observe=None):
    """Run the real run() loop for `polls` iterations, sleeping not at all.

    The watchdog thread is off (it would share the patched sleep) and
    `_stop` is cleared so a daemon can be driven more than once. `observe(d,
    n)` runs BETWEEN polls, i.e. while the daemon still owns the channels —
    run()'s finally hands them back to the firmware."""
    d._watchdog_seconds = 0
    d._stop = False
    n = {"i": 0}
    orig = dm.time.sleep

    def fake_sleep(_s):
        n["i"] += 1
        if observe:
            observe(d, n["i"])
        if n["i"] >= polls:
            d._stop = True
    dm.time.sleep = fake_sleep
    try:
        rc = d.run()
    finally:
        dm.time.sleep = orig
    return rc


def pwm(root, name="pwm1"):
    return (root / "hwmon0" / name).read_text().strip()


def enable(root, name="pwm1"):
    return (root / "hwmon0" / f"{name}_enable").read_text().strip()


def status(tmp_path):
    return json.loads((tmp_path / "status.json").read_text())


# ------------------------------------------------- fail-safe on dead source

def test_absent_gpu_at_start_owns_the_header_and_ramps_to_failsafe(tmp_path,
                                                                    caplog):
    """The old bug: the GPU was not in nvidia-smi when the daemon started,
    so its header was left with the firmware (parked at 0 %) — and never
    taken over again. Now: we take the channel over anyway and the
    fail-safe drives it."""
    caplog.set_level(logging.WARNING)
    root = make_tree(tmp_path, pwm1=(0, 2))
    cfg = make_config(tmp_path)
    seen = []
    d = Daemon(cfg, root=root, gpu_runner=lambda *f: "")
    run_polls(d, polls=4, observe=lambda _d, _n: seen.append((pwm(root),
                                                              enable(root))))

    assert seen[-1] == ("255", "1"), f"expected owned + 100%, got {seen[-1]}"
    assert all(en == "1" for _, en in seen[1:]), "ownership must stick"
    assert status(tmp_path)["fans"]["it8792:pwm1"]["state"] == "failsafe"
    assert "FAIL-SAFE" in caplog.text
    # clean exit hands it back, exactly as configured
    assert (root / "hwmon0" / "pwm1_enable").read_text().strip() == "2"


def test_stale_gpu_ramps_UP_instead_of_holding_idle_duty(tmp_path):
    """Taken over while idle (26 %), then the reading dies: the fan must
    climb to the fail-safe duty, not sit at 26 % while the card heats."""
    root = make_tree(tmp_path, pwm1=(0, 2))
    cfg = make_config(tmp_path)
    out = {"csv": COOL}
    seen = []

    def obs(_d, n):
        if n == 1:
            out["csv"] = ""                 # GPU vanishes from nvidia-smi
        seen.append(pwm(root))

    d = Daemon(cfg, root=root, gpu_runner=lambda *f: out["csv"])
    run_polls(d, polls=6, observe=obs)
    assert seen[0] == "66", f"idle duty expected first, got {seen}"
    assert seen[-1] == "255", f"expected ramp to 100%, got {seen}"


def test_gpu_that_comes_back_is_readopted_without_a_restart(tmp_path):
    """Sources are re-discovered every poll: a GPU that returns is read and
    controlled again in the same poll, no config edit, no restart."""
    root = make_tree(tmp_path, pwm1=(0, 2))
    cfg = make_config(tmp_path)
    out = {"csv": ""}

    def obs(_d, n):
        if n == 3:
            out["csv"] = COOL               # the GPU is back, and cool

    d = Daemon(cfg, root=root, gpu_runner=lambda *f: out["csv"])
    run_polls(d, polls=25, observe=obs)

    st = status(tmp_path)
    assert "gpu:43:00.0" in st["sources"], "GPU never re-appeared"
    assert st["sources"]["gpu:43:00.0"]["stale"] is False
    assert st["fans"]["it8792:pwm1"]["state"] == "ok"
    assert pwm(root) == "66", f"should be back on the curve, got {pwm(root)}"


def test_zero_degree_gpu_reading_is_not_a_temperature(tmp_path):
    """A card that dropped off the bus reports 0 C. That must count as 'no
    reading' (fail-safe), never as 'ice cold, fan may idle'."""
    root = make_tree(tmp_path, pwm1=(66, 2))
    cfg = make_config(tmp_path)
    d = Daemon(cfg, root=root,
               gpu_runner=lambda *f: "00000000:43:00.0, 0\n")
    seen = []
    run_polls(d, polls=4,
              observe=lambda _d, _n: seen.append(pwm(root)))
    st = status(tmp_path)
    assert st["sources"]["gpu:43:00.0"]["stale"] is True
    assert seen[-1] == "255", f"0 C must trigger the fail-safe, got {seen}"


def test_dead_source_still_outranks_a_live_cold_one_on_a_shared_fan(tmp_path):
    """Fail-safe composes with highest-request-wins: a dead GPU source
    requests 100 %, a live cold hwmon source requests 20 % — the fan goes
    to 100 %, it may not be dragged down by the cold one."""
    root = make_tree(tmp_path, pwm1=(0, 2))
    (root / "hwmon0" / "temp1_input").write_text("40000\n")   # cool
    cfg = json.loads(make_config(tmp_path).read_text())
    cfg["assignments"]["Case"] = {"source": "it8792:temp1", "curve": "cool",
                                  "fans": ["it8792:pwm1"], "enabled": True}
    p = tmp_path / "config.json"
    p.write_text(json.dumps(cfg))
    d = Daemon(p, root=root, gpu_runner=lambda *f: "")   # GPU absent
    seen = []
    run_polls(d, polls=6, observe=lambda _d, _n: seen.append(pwm(root)))
    assert seen[0] == "51", f"cold source drives it first, got {seen}"
    assert seen[-1] == "255", f"dead source must win, got {seen}"


def test_failsafe_can_be_disabled_per_assignment(tmp_path, caplog):
    """Opt-out keeps the old hold-last-duty behaviour, and validation warns
    that it is an opt-out."""
    caplog.set_level(logging.WARNING)
    root = make_tree(tmp_path, pwm1=(0, 2))
    cfg = make_config(tmp_path)
    doc = json.loads(cfg.read_text())
    doc["assignments"]["GPU 43:00.0"]["failsafe"] = False
    cfg.write_text(json.dumps(doc))
    from alx_fancontrol import config as C
    _errors, warnings = C.validate(C.load(cfg))
    assert any("fail-safe disabled" in w for w in warnings)

    d = Daemon(cfg, root=root, gpu_runner=lambda *f: "")
    run_polls(d, polls=5)
    assert pwm(root) == "0", "opt-out must not ramp"
    assert "fail-safe DISABLED" in caplog.text


# ------------------------------------------------------- reclaim / takeover

def _write_marker(tmp_path, channels, pid=None):
    marker = {"pid": pid if pid is not None else os.getpid(),
              "boot_id": Daemon._boot_id(), "channels": channels}
    (tmp_path / "owned.json").write_text(json.dumps(marker))


def test_channel_left_manual_by_our_own_crash_is_reclaimed(tmp_path, caplog):
    """SIGKILL/OOM/power blip leaves pwmN_enable=1 and the duty frozen (here
    at 0). The ownership marker from this boot makes it reclaimable instead
    of a permanent 'manual-conflict' — which is exactly how a fan used to
    stay stuck at 0 % with the service reporting itself healthy."""
    caplog.set_level(logging.INFO)
    root = make_tree(tmp_path, pwm1=(0, 1))       # manual, parked at 0
    cfg = make_config(tmp_path)
    _write_marker(tmp_path, {"it8792:pwm1": 2})   # ours, restore target = 2
    d = Daemon(cfg, root=root, gpu_runner=lambda *f: HOT)
    run_polls(d, polls=3)

    assert "reclaiming it8792:pwm1" in caplog.text
    assert pwm(root) == "255", f"must be driven again, got {pwm(root)}"
    assert enable(root) == "2", "restored to the pre-daemon value on exit"


def test_foreign_manual_channel_is_still_refused(tmp_path, caplog):
    """Without a marker from this boot, a manual channel belongs to someone
    else (e.g. gpu-fanctl.service) and stays refused — coexistence still
    holds; only our own leftovers are reclaimed."""
    caplog.set_level(logging.ERROR)
    root = make_tree(tmp_path, pwm1=(0, 1))
    cfg = make_config(tmp_path)
    d = Daemon(cfg, root=root, gpu_runner=lambda *f: HOT)
    run_polls(d, polls=3)
    assert "already in manual mode" in caplog.text
    assert pwm(root) == "0"
    assert status(tmp_path)["fans"]["it8792:pwm1"]["state"] == "manual-conflict"


def test_marker_from_another_boot_is_not_trusted(tmp_path):
    """Across a cold boot the firmware owns the channels again: a stale
    marker must not make us claim whatever it lists."""
    root = make_tree(tmp_path, pwm1=(0, 1))
    cfg = make_config(tmp_path)
    _write_marker(tmp_path, {"it8792:pwm1": 2})
    m = json.loads((tmp_path / "owned.json").read_text())
    m["boot_id"] = "some-previous-boot"
    (tmp_path / "owned.json").write_text(json.dumps(m))
    d = Daemon(cfg, root=root, gpu_runner=lambda *f: HOT)
    run_polls(d, polls=2)
    assert pwm(root) == "0"


def test_marker_of_a_live_other_instance_is_not_reclaimed(tmp_path, caplog):
    """Two daemons must not fight over one channel — a marker whose pid is a
    live alx_fancontrol process is not ours to reclaim."""
    caplog.set_level(logging.ERROR)
    root = make_tree(tmp_path, pwm1=(0, 1))
    cfg = make_config(tmp_path)
    me = os.getpid()
    # pretend a sibling daemon holds it: a live pid whose /proc cmdline we
    # cannot read would be treated as foreign, so use a live `sleep` and
    # patch the identity check to report it as one of ours.
    proc = subprocess.Popen(["sleep", "30"])
    try:
        _write_marker(tmp_path, {"it8792:pwm1": 2}, pid=proc.pid)
        orig = Daemon._pid_alive

        def alive(pid):
            return pid == proc.pid or orig(pid)
        Daemon._pid_alive = staticmethod(alive)
        d = Daemon(cfg, root=root, gpu_runner=lambda *f: HOT)
        run_polls(d, polls=2)
    finally:
        Daemon._pid_alive = orig
        proc.kill()
        proc.wait()
    assert "another alx_fancontrol daemon" in caplog.text
    assert pwm(root) == "0"


def test_marker_tracks_takeover_and_release(tmp_path):
    """The marker lists exactly what we hold, and is emptied on clean exit."""
    root = make_tree(tmp_path, pwm1=(0, 2))
    cfg = make_config(tmp_path)
    d = Daemon(cfg, root=root, gpu_runner=lambda *f: HOT)
    run_polls(d, polls=2)
    marker = json.loads((tmp_path / "owned.json").read_text())
    assert marker["channels"] == {}          # released on exit
    assert enable(root) == "2"


def test_force_takeover_claims_a_disabled_channel(tmp_path, caplog):
    """BIOS parks some headers at enable=0; a fan the user explicitly
    assigned with force_takeover is enabled anyway (opt-in, because that is
    the one case where 'refuse' strands a GPU). The value found there is
    still what gets restored on exit."""
    caplog.set_level(logging.WARNING)
    root = make_tree(tmp_path, pwm1=(0, 0))     # disabled by firmware
    cfg = make_config(tmp_path, assignment_extra={"force_takeover": True})
    seen = []
    d = Daemon(cfg, root=root, gpu_runner=lambda *f: HOT)
    run_polls(d, polls=3,
              observe=lambda _d, _n: seen.append((pwm(root), enable(root))))
    assert seen[-1] == ("255", "1"), f"not claimed: {seen}"
    assert "force_takeover" in caplog.text
    st = status(tmp_path)
    assert st["fans"]["it8792:pwm1"]["orig_enable"] == 0
    assert (root / "hwmon0" / "pwm1_enable").read_text().strip() == "0"

    # and without the opt-in it is still refused
    root2 = make_tree(tmp_path / "b", pwm1=(0, 0))
    cfg2 = make_config(tmp_path / "b")
    d2 = Daemon(cfg2, root=root2, gpu_runner=lambda *f: HOT)
    run_polls(d2, polls=3)
    assert (root2 / "hwmon0" / "pwm1_enable").read_text().strip() == "0"
    assert status(tmp_path / "b")["fans"]["it8792:pwm1"]["state"] == "disabled"


# ------------------------------------------------------------ liveness ----

def test_nvidia_smi_timeout_kills_the_child_and_does_not_wait():
    """The wedge that froze every fan at its last duty: a hung nvidia-smi.
    _run_argv must return promptly, kill the child, and NOT reap it (a
    D-state child would block the reaper too)."""
    t0 = time.time()
    with pytest.raises(subprocess.TimeoutExpired):
        hwmon._run_argv(["sleep", "30"], 0.2)
    assert time.time() - t0 < 3.0, "timeout did not return promptly"
    assert hwmon.pending_children() >= 1, "child was waited on, not parked"


def test_gpu_temps_tolerates_a_card_that_cannot_answer():
    """'[Not Supported]' from one card must not black out the others (that
    used to fail the whole call and stale every GPU source)."""
    out = hwmon.gpu_temps(runner=lambda *f:
                          "00000000:08:00.0, 55\n"
                          "00000000:43:00.0, [Not Supported]\n")
    assert out == {"08:00.0": 55}


def test_watchdog_exits_when_the_poll_loop_stops_beating(tmp_path,
                                                         monkeypatch):
    root = make_tree(tmp_path, pwm1=(66, 1))
    cfg = make_config(tmp_path, watchdog_seconds=1)
    d = Daemon(cfg, root=root, gpu_runner=lambda *f: HOT)
    d._initial_load()
    assert d._watchdog_seconds == 1.0

    exited = []

    def fake_exit(code):
        exited.append(code)
        d._stop = True              # stop the loop instead of killing pytest
    monkeypatch.setattr(dm.os, "_exit", fake_exit)
    d._last_tick = time.time() - 5          # pretend no poll for 5s
    t = threading.Thread(target=d._watchdog_loop, daemon=True)
    t.start()
    t.join(5)
    assert exited == [dm.EXIT_STUCK], "watchdog must exit for systemd"


def test_watchdog_stays_quiet_while_the_loop_beats(tmp_path):
    root = make_tree(tmp_path, pwm1=(66, 1))
    cfg = make_config(tmp_path, watchdog_seconds=2)
    d = Daemon(cfg, root=root, gpu_runner=lambda *f: HOT)
    d._initial_load()
    stop = threading.Event()

    def beater():
        while not stop.is_set():
            d._last_tick = time.time()
            time.sleep(0.2)

    b = threading.Thread(target=beater, daemon=True)
    t = threading.Thread(target=d._watchdog_loop, daemon=True)
    b.start()
    t.start()
    try:
        time.sleep(2.5)                      # longer than one watchdog period
        assert d._stop is False, "watchdog fired while the loop was beating"
    finally:
        stop.set()
        d._stop = True                       # retire the watchdog thread —
        t.join(3)                            # a live one would exit(70) pytest


# ------------------------------------------------------------ degraded ----

def test_invalid_config_holds_failsafe_then_recovers(tmp_path, caplog):
    """A config that fails validation used to exit(2) before taking anything
    over — with Restart=always that is a flap loop with ZERO fan control.
    Now: take the assigned channels, hold them at fail-safe duty, and pick
    up normal control the moment the file is valid again."""
    caplog.set_level(logging.CRITICAL)
    root = make_tree(tmp_path, pwm1=(0, 2))
    cfg = make_config(tmp_path)
    doc = json.loads(cfg.read_text())
    doc["config"]["floor_duty"] = 150          # hard validation error
    cfg.write_text(json.dumps(doc))

    d = Daemon(cfg, root=root, gpu_runner=lambda *f: HOT)
    d._watchdog_seconds = 0
    n = {"i": 0}
    orig = dm.time.sleep

    def fake_sleep(_s):
        n["i"] += 1
        if n["i"] == 2:                        # operator fixes the config
            good = json.loads(cfg.read_text())
            good["config"]["floor_duty"] = 5
            cfg.write_text(json.dumps(good))
        if n["i"] >= 6:
            d._stop = True
    dm.time.sleep = fake_sleep
    try:
        rc = d.run()
    finally:
        dm.time.sleep = orig

    assert "DEGRADED MODE" in caplog.text
    assert rc == 0
    assert pwm(root) == "255"                  # HOT 72 C -> 100 % either way
    assert status(tmp_path)["degraded"] is False


def test_unreadable_config_with_no_recoverable_fans_still_exits(tmp_path):
    """Nothing in the file tells us which channels exist → no safe claim to
    make, so keep the old hard exit (and say so loudly). The watchdog must
    retire with run(), not fire later for a daemon that no longer polls."""
    root = make_tree(tmp_path, pwm1=(0, 2))
    cfg = tmp_path / "config.json"
    cfg.write_text("{ this is not json")
    d = Daemon(cfg, root=root, gpu_runner=lambda *f: HOT)
    assert d.run() == dm.EXIT_HARD_ERROR
    assert enable(root) == "2", "must not touch channels it cannot attribute"
    assert d._retired is True
    n_before = threading.active_count()
    time.sleep(1.2)                      # give any watchdog thread time to die
    assert threading.active_count() <= n_before


# ---------------------------------------------------------- stalled fan ---

def test_stalled_fan_is_reported(tmp_path, caplog):
    """Commanding duty is not cooling: if the tach reads 0 while we ask for
    real duty, that is an alarm (state + status.json errors + log ERROR)."""
    caplog.set_level(logging.ERROR)
    root = make_tree(tmp_path, pwm1=(0, 2))
    (root / "hwmon0" / "fan1_input").write_text("0\n")   # fan not turning
    cfg = make_config(tmp_path, stall_after=3, stall_min_duty=20)
    d = Daemon(cfg, root=root, gpu_runner=lambda *f: HOT)
    run_polls(d, polls=4)

    st = status(tmp_path)
    assert st["fans"]["it8792:pwm1"]["state"] == "stalled"
    assert any("stalled" in k for k in st["errors"])
    assert "STALLED" in caplog.text


def test_fan_turning_is_not_reported_as_stalled(tmp_path):
    root = make_tree(tmp_path, pwm1=(0, 2))
    (root / "hwmon0" / "fan1_input").write_text("1800\n")
    cfg = make_config(tmp_path, stall_after=3)
    d = Daemon(cfg, root=root, gpu_runner=lambda *f: HOT)
    run_polls(d, polls=5)
    st = status(tmp_path)
    assert st["fans"]["it8792:pwm1"]["state"] == "ok"
    assert st["fans"]["it8792:pwm1"]["rpm"] == 1800
    assert not any("stalled" in k for k in st["errors"])
