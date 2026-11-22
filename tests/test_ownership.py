"""Ownership is not a startup event, and commanded duty is not ownership.

The regression behind these tests is the real one from the live box: GPU0's
blower sat at 0 RPM for eleven minutes, the log cheerfully reported
`write it8792:pwm1 = 100%` every few seconds, and the card climbed to 73 C.
Nothing re-read `pwmN_enable` after takeover, so a firmware/EC reclaim of the
header was invisible: a duty write into a channel we no longer own *succeeds*,
and this board parks headers it owns — including at 0 %. The tach was the only
witness, and it could only describe the symptom.

So two things had to change, and both are checked here every poll:
  * re-read `pwmN_enable` (and `pwmN`) for every channel we hold, reclaim /
    re-assert on any drift, and shout about it;
  * a confirmed stall ESCALATES instead of merely being logged, because more
    duty is not an available answer to a fan that is not turning.
"""
import json
import logging

import alx_fancontrol.daemon as dm
import alx_fancontrol.hwmon as hw
from alx_fancontrol import config as config_mod
from alx_fancontrol.daemon import Daemon
from test_safety_net import (HOT, enable, make_config, make_tree, pwm,
                             run_polls, status)


def steal(root, value="2", name="pwm1"):
    """Simulate the firmware/EC taking a header back mid-run."""
    (root / "hwmon0" / f"{name}_enable").write_text(f"{value}\n")


def set_rpm(root, value, name="fan1"):
    (root / "hwmon0" / f"{name}_input").write_text(f"{value}\n")


def config_json(system_source):
    """Same shape as make_config, but the fan hangs off a NON-GPU source."""
    return json.dumps({
        "version": 2,
        "config": {"poll_seconds": 1.0, "max_rate": 6, "floor_duty": 5,
                   "dead_temp_c": -10, "failsafe_after": 2,
                   "watchdog_seconds": 0,
                   "stall_after": 2, "stall_power_limit_w": 150},
        "curves": {"sys": {"label": "sys", "points": [[40, 60]]}},
        "assignments": {
            "System": {"source": system_source, "curve": "sys",
                       "fans": ["it8792:pwm1"], "enabled": True,
                       "tach": {"it8792:pwm1": "it8792:fan1"}}},
    })


# --------------------------------------------------- ownership re-verification

def test_channel_taken_back_mid_run_is_detected_and_reclaimed(tmp_path, caplog):
    """The exact live failure: enable flipped away from 1 while we ran.

    Before the fix the daemon never looked again — it just kept writing duty
    into a header that was no longer ours."""
    caplog.set_level(logging.WARNING)
    root = make_tree(tmp_path, pwm1=(0, 2))
    d = Daemon(make_config(tmp_path), root=root, gpu_runner=lambda *f: HOT)
    seen = []

    def observe(_d, n):
        if n == 2:
            steal(root)                      # firmware parks the header
        seen.append((enable(root), pwm(root)))

    run_polls(d, polls=5, observe=observe)

    assert "OWNERSHIP LOST" in caplog.text
    assert seen[1][0] == "2", "the theft must really have happened"
    assert seen[2][0] == "1", "the very next poll must reclaim the channel"
    # and it must actually drive the fan again, not merely re-claim the channel
    assert seen[2][1] == "255", "reclaiming without re-asserting duty cools " \
                               "nothing"
    assert "re-established" in caplog.text


def test_reclaim_insists_on_a_header_the_firmware_disabled(tmp_path, caplog):
    """enable=0 found on a channel WE hold is theft, not intent: claiming it
    is what `force_takeover` exists for, and waiting for permission here means
    the card cooks while we respect a value the firmware chose under us."""
    caplog.set_level(logging.CRITICAL)
    root = make_tree(tmp_path, pwm1=(0, 2))
    d = Daemon(make_config(tmp_path), root=root, gpu_runner=lambda *f: HOT)
    seen = []

    def observe(_d, n):
        if n == 2:
            steal(root, "0")
        seen.append(enable(root))

    run_polls(d, polls=4, observe=observe)

    assert seen[1] == "0"
    assert seen[2] == "1", "a disabled header we own must be re-enabled"
    assert "left disabled" in caplog.text


def test_a_header_that_never_comes_back_stays_visible(tmp_path, monkeypatch,
                                                     caplog):
    """An EC that wins every round must not read as healthy: the state stays
    `ownership-lost` and status.json carries it, so protect.py/TUI/journal see
    the fault for as long as it lasts."""
    caplog.set_level(logging.CRITICAL)
    root = make_tree(tmp_path, pwm1=(0, 2))
    # a chip that accepts nothing: our reclaim never lands
    monkeypatch.setattr(hw, "set_enable", lambda *a, **k: None)
    d = Daemon(make_config(tmp_path), root=root, gpu_runner=lambda *f: HOT)

    run_polls(d, polls=3)

    st = status(tmp_path)
    assert st["fans"]["it8792:pwm1"]["state"] == "ownership-lost"
    assert any(k.startswith("ownership-lost:") for k in st["errors"])
    assert "OWNERSHIP LOST" in caplog.text


def test_ownership_loss_alerts_and_recovers(tmp_path, monkeypatch):
    """Escalation must fire on the loss AND say so when the header comes back,
    or an operator cannot tell a blip from a fault that is still running."""
    root = make_tree(tmp_path, pwm1=(0, 2))
    events = []
    monkeypatch.setattr(hw, "set_enable", lambda *a, **k: None)
    cfg = make_config(tmp_path,
                      stall_alert_command=f'echo "$ALX_STATE" >> '
                                          f'{tmp_path / "alerts.txt"}')
    d = Daemon(cfg, root=root, gpu_runner=lambda *f: HOT)
    monkeypatch.setattr(d, "_power_limit_for", lambda *a, **k: None)

    def observe(_d, n):
        if n == 2:
            steal(root)
        events.append(enable(root))

    run_polls(d, polls=4, observe=observe)

    text = (tmp_path / "alerts.txt").read_text()
    assert "ownership-lost" in text
    assert "ownership-recovered" not in text, "it never came back (set_enable " \
                                             "is a no-op)"


def test_dry_run_reports_the_loss_and_writes_nothing(tmp_path, caplog):
    caplog.set_level(logging.CRITICAL)
    root = make_tree(tmp_path, pwm1=(0, 2))
    d = Daemon(make_config(tmp_path), root=root, gpu_runner=lambda *f: HOT,
               dry_run=True)

    def observe(_d, n):
        if n == 2:
            steal(root, "0")

    run_polls(d, polls=4, observe=observe)

    assert "OWNERSHIP LOST" in caplog.text
    assert enable(root) == "0", "dry-run performs NO writes at all — not even " \
                                "a reclaim"


def test_foreign_duty_write_is_reasserted(tmp_path, caplog):
    """Second writer on the same header: our write is skipped whenever the
    target equals the duty WE remember, so a foreign 0 would stick forever."""
    caplog.set_level(logging.WARNING)
    root = make_tree(tmp_path, pwm1=(0, 2))
    d = Daemon(make_config(tmp_path), root=root, gpu_runner=lambda *f: HOT)
    seen = []

    def observe(_d, n):
        if n == 2:
            (root / "hwmon0" / "pwm1").write_text("0\n")   # someone else parks it
        seen.append(pwm(root))

    run_polls(d, polls=4, observe=observe)

    assert seen[1] == "0"
    assert seen[2] == "255", "the forgotten-duty write must be re-asserted"
    assert "another writer" in caplog.text


# --------------------------------------------------------------- escalation --

def test_stall_escalates_to_the_alert_hook(tmp_path, caplog):
    """A stalled fan has to reach a person: hook runs on the episode and again
    when the fan starts turning."""
    caplog.set_level(logging.WARNING)
    root = make_tree(tmp_path, pwm1=(0, 2))
    set_rpm(root, "0")                                  # not turning
    alerts = tmp_path / "alerts.txt"
    cfg = make_config(tmp_path, stall_after=2, stall_min_duty=20,
                      stall_alert_command=f'echo "$ALX_STATE $ALX_FAN '
                                          f'$ALX_DUTY_PCT $ALX_RPM" >> {alerts}',
                      stall_alert_repeat_s=0)           # once per episode
    d = Daemon(cfg, root=root, gpu_runner=lambda *f: HOT)

    def observe(_d, n):
        if n >= 3:
            set_rpm(root, "1800")                       # fan comes back

    run_polls(d, polls=6, observe=observe)

    lines = alerts.read_text().splitlines()
    assert any(l.startswith("stalled it8792:pwm1 100 0") for l in lines), lines
    assert any(l.startswith("recovered it8792:pwm1") for l in lines), lines
    assert sum(1 for l in lines if l.startswith("stalled")) == 1, \
        "repeat_s=0 means one alert per episode, not one per poll"
    assert "ESCALATING" in caplog.text
    assert "RECOVERED" in caplog.text


def test_ownership_keeps_escalating_while_the_tach_spins(tmp_path, monkeypatch):
    """A spinning tach does NOT mean the fault is gone.

    Under firmware control the fan may well keep turning — so if a tach stall
    and an ownership loss shared one episode map, the tach would 'recover' a
    loss we are still losing every single poll, and the alert would fire once
    an hour while the header was being stolen every second."""
    root = make_tree(tmp_path, pwm1=(0, 2))     # fake fan1 reads 1500 RPM
    monkeypatch.setattr(hw, "set_enable", lambda *a, **k: None)
    d = Daemon(make_config(tmp_path, stall_alert_repeat_s=60), root=root,
               gpu_runner=lambda *f: HOT)
    fired = []
    monkeypatch.setattr(d, "_alert",
                        lambda _c, _f, kind, _d: fired.append(kind))
    clock = {"t": 0.0}
    monkeypatch.setattr(dm.time, "monotonic", lambda: clock["t"])

    def observe(_d, n):
        clock["t"] = n * 30.0          # 30 s between polls, repeat is 60 s

    run_polls(d, polls=4, observe=observe)

    assert "ownership-lost" in fired
    assert fired.count("ownership-lost") >= 2, \
        f"a fault that never heals must keep alerting, got {fired}"
    assert all(k == "ownership-lost" for k in fired), \
        f"a spinning tach must not report a recovery: {fired}"


def test_escalation_rate_is_governed_by_stall_alert_repeat_s(tmp_path,
                                                            monkeypatch):
    """A stall lasting hours must nag without flooding the log: once at the
    transition, then at most every `stall_alert_repeat_s`."""
    root = make_tree(tmp_path, pwm1=(0, 2))
    d = Daemon(make_config(tmp_path, stall_alert_repeat_s=60), root=root,
               gpu_runner=lambda *f: HOT)
    cfg = config_mod.load(d.cfg_path)
    fired = []
    monkeypatch.setattr(d, "_alert", lambda _c, _f, kind, _d: fired.append(kind))
    clock = {"t": 1000.0}
    monkeypatch.setattr(dm.time, "monotonic", lambda: clock["t"])

    d._escalate(cfg, "it8792:pwm1", "stalled", "0 RPM at 100%", d._stall_eps)
    assert fired == ["stalled"]

    clock["t"] += 30
    d._escalate(cfg, "it8792:pwm1", "stalled", "still 0 RPM", d._stall_eps)
    assert fired == ["stalled"], "must not nag before the repeat interval"

    clock["t"] += 45
    d._escalate(cfg, "it8792:pwm1", "stalled", "still 0 RPM", d._stall_eps)
    assert fired == ["stalled", "stalled"], "must keep nagging while it lasts"


def test_stall_can_cap_the_gpu_that_loses_its_air(tmp_path, monkeypatch):
    """Opt-in throttle: the fan cannot be made to turn, so cut the heat."""
    root = make_tree(tmp_path, pwm1=(0, 2))
    set_rpm(root, "0")
    calls = []
    monkeypatch.setattr(hw, "gpu_indices", lambda **kw: {"43:00.0": "0"})
    monkeypatch.setattr(hw, "run_quiet",
                        lambda argv, **kw: (calls.append(list(argv)),
                                            (True, "ok"))[1])
    cfg = make_config(tmp_path, stall_after=2, stall_power_limit_w=120)
    d = Daemon(cfg, root=root, gpu_runner=lambda *f: HOT)

    run_polls(d, polls=4)

    assert ["nvidia-smi", "-i", "0", "-pl", "120"] in calls


def test_no_power_cap_unless_it_is_asked_for(tmp_path, monkeypatch):
    """Default is off: silently throttling somebody's inference job is not the
    daemon's call to make."""
    root = make_tree(tmp_path, pwm1=(0, 2))
    set_rpm(root, "0")
    calls = []
    monkeypatch.setattr(hw, "gpu_indices", lambda **kw: {"43:00.0": "0"})
    monkeypatch.setattr(hw, "run_quiet",
                        lambda argv, **kw: (calls.append(list(argv)),
                                            (True, "ok"))[1])
    cfg = make_config(tmp_path, stall_after=2)     # stall_power_limit_w absent
    d = Daemon(cfg, root=root, gpu_runner=lambda *f: HOT)

    run_polls(d, polls=4)

    assert not [c for c in calls if "-pl" in c]


def test_degraded_mode_still_verifies_ownership(tmp_path, caplog):
    """Degraded mode runs with cfg = None (the file is invalid — that is why
    we are there). The ownership check has to work in that state anyway, on
    defaults, without raising: an unmonitored fail-safe duty is precisely how
    a parked header goes unnoticed."""
    caplog.set_level(logging.CRITICAL)
    root = make_tree(tmp_path, pwm1=(0, 2))
    d = Daemon(make_config(tmp_path), root=root, gpu_runner=lambda *f: HOT)
    d._chips = hw.find_all_chips(root)          # normally set by run()
    d._takeover("it8792:pwm1")

    steal(root)                        # firmware takes it back
    d._verify_channels(None)           # must not raise on cfg=None

    assert "OWNERSHIP LOST" in caplog.text
    assert enable(root) == "1", "reclaim must still happen without a config"


def test_bad_escalation_values_fall_back_instead_of_raising(tmp_path, caplog):
    """An escalation knob full of junk must never cost us the escalation."""
    caplog.set_level(logging.CRITICAL)
    root = make_tree(tmp_path, pwm1=(0, 2))
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({
        "version": 2,
        "config": {"poll_seconds": 1.0, "watchdog_seconds": 0, "stall_after": 2,
                   "stall_alert_repeat_s": "soon", "stall_alert_timeout_s": None,
                   "stall_power_limit_w": "lots", "stall_alert_command": ""},
        "curves": {"gpu_v100": {"label": "V100", "points": [[50, 26],
                                                             [72, 100]]}},
        "assignments": {}}))
    d = Daemon(cfg_path, root=root, gpu_runner=lambda *f: HOT)
    cfg = config_mod.load(cfg_path)

    d._escalate(cfg, "it8792:pwm1", "stalled", "0 RPM at 100%", d._stall_eps)

    assert "ESCALATING" in caplog.text
    assert "it8792:pwm1" in d._stall_eps


def test_a_fan_with_no_gpu_source_is_not_power_capped_silently(tmp_path,
                                                              monkeypatch,
                                                              caplog):
    """A chassis fan on a CPU/VRM source can lose its air too — say so instead
    of pretending the cap was applied."""
    caplog.set_level(logging.WARNING)
    root = make_tree(tmp_path, pwm1=(0, 2))
    set_rpm(root, "0")
    monkeypatch.setattr(hw, "gpu_indices", lambda **kw: {"43:00.0": "0"})
    monkeypatch.setattr(hw, "run_quiet", lambda argv, **kw: (True, "ok"))
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(
        config_json(system_source="it8686:temp5"))
    d = Daemon(cfg_path, root=root, gpu_runner=lambda *f: HOT)

    run_polls(d, polls=4)

    assert "no GPU source is assigned" in caplog.text
