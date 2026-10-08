"""Hermetic daemon smoke tests: fake hwmon tree + fake GPU runner.

These NEVER touch real hardware. The fake tree deliberately uses a
different hwmon index order than the live box, and models the live safety
situation: it8792 pwm1/pwm3 already in manual mode (enable=1, owned by
gpu-fanctl).
"""
import json
import logging
import os
import time

import alx_fancontrol.hwmon as hwmon
import pytest
from alx_fancontrol.daemon import Daemon

FAKE_GPUS = "00000000:08:00.0, 68\n00000000:44:00.0, 69\n"


def make_tree(tmp_path):
    root = tmp_path / "hwmon"

    def chip(idx, name, **files):
        d = root / f"hwmon{idx}"
        d.mkdir(parents=True)
        (d / "name").write_text(name + "\n")
        for k, v in files.items():
            (d / k).write_text(str(v) + "\n")

    chip(0, "it8686_2008090d",
         temp1_input=45000,
         fan3_input=3229,
         pwm3=70, pwm3_enable=2, pwm4=70, pwm4_enable=2)
    chip(1, "it8792_2008090d",
         temp1_input=60000,
         pwm1=140, pwm1_enable=1, pwm3=140, pwm3_enable=1)
    chip(2, "k10temp", temp1_input=40000)
    return root


def make_config(tmp_path, assignments):
    # v2: no "sources" section — the daemon discovers sources at runtime
    # from the fake tree (k10temp:temp1, it8686:temp1, it8792:temp1) +
    # the injected GPU runner (gpu:08:00.0, gpu:44:00.0).
    doc = {
        "version": 2,
        "config": {"poll_seconds": 1.0, "max_rate": 6,
                   "floor_duty": 20, "dead_temp_c": -10},
        "protected": ["it8792:pwm1", "it8792:pwm3"],
        "curves": {
            "default": {"label": "Default",
                        "points": [[40, 20], [55, 30], [65, 70], [75, 100]]},
            "gpu_v100": {"label": "V100 blower",
                         "points": [[50, 26], [60, 40], [66, 75], [72, 100]]},
        },
        "assignments": assignments,
    }
    p = tmp_path / "config.json"
    p.write_text(json.dumps(doc))
    return p


_BUMP = {"n": 0}


def _bump_mtime(path):
    """Force the mtime forward, monotonically: this system quantizes mtime
    updates to the coarse clock (~4 ms), so fast back-to-back writes can
    share a timestamp — set an explicit future mtime instead."""
    _BUMP["n"] += 1
    atime_ns = os.stat(path).st_atime_ns
    mtime_ns = int((time.time() + 1000 + _BUMP["n"] * 100) * 1e9)
    os.utime(path, ns=(atime_ns, mtime_ns))


def snapshot_tree(root):
    out = {}
    for d in sorted(root.rglob("*")):
        if d.is_file():
            out[str(d.relative_to(root))] = d.read_text()
    return out


def run_daemon(cfg_path, root, dry_run, **kw):
    d = Daemon(cfg_path, dry_run=dry_run, once=True, root=root,
               gpu_runner=lambda *f: FAKE_GPUS,
               log_path=kw.pop("log_path", None), **kw)
    return d.run()


# ------------------------------------------------------------ dry run -----

def test_dry_run_once_changes_nothing_and_logs_guardrails(caplog, tmp_path):
    caplog.set_level(logging.INFO)
    root = make_tree(tmp_path)
    cfg_path = make_config(tmp_path, {
        "free": {"source": "k10temp:temp1", "curve": "default", "fans": ["it8686:pwm3"],
                 "tach": {"it8686:pwm3": "it8686:fan3"}, "enabled": True},
        "prot1": {"source": "gpu:08:00.0", "curve": "gpu_v100",
                  "fans": ["it8792:pwm1"], "enabled": True},
        "prot3": {"source": "gpu:08:00.0", "curve": "gpu_v100",
                  "fans": ["it8792:pwm3"], "enabled": True},
    })
    before = snapshot_tree(root)
    rc = run_daemon(cfg_path, root, dry_run=True)
    after = snapshot_tree(root)
    assert rc == 0
    assert before == after, "dry-run must not change any file"
    text = caplog.text
    assert "would take over it8686:pwm3" in text
    # k10temp reads 40.0 C -> default curve -> exactly 20% (floor), raw 51
    assert "would write it8686:pwm3 =  20% (raw 51)" in text
    assert text.count("refusing protected channel it8792") == 2
    assert "would return it8686:pwm3 to firmware" in text

    st = json.loads((cfg_path.parent / "status.json").read_text())
    assert st["dry_run"] is True
    assert st["fans"]["it8792:pwm1"]["state"] == "protected"
    assert st["fans"]["it8792:pwm3"]["state"] == "protected"
    assert st["fans"]["it8686:pwm3"]["state"] == "ok"
    assert st["fans"]["it8686:pwm3"]["rpm"] == 3229
    assert st["fans"]["it8686:pwm3"]["label"] == "SYS_FAN2" or \
        st["fans"]["it8686:pwm3"]["label"] == "pwm3 (it8686)"
    assert st["sources"]["k10temp:temp1"]["temp_c"] == 40.0
    assert st["sources"]["gpu:08:00.0"]["temp_c"] == 68


# --------------------------------------------------------- live (fake) ----

def test_live_once_takeover_write_and_restore(caplog, tmp_path, monkeypatch):
    caplog.set_level(logging.INFO)
    root = make_tree(tmp_path)
    cfg_path = make_config(tmp_path, {
        "free": {"source": "k10temp:temp1", "curve": "default", "fans": ["it8686:pwm3"],
                 "enabled": True},
    })
    enable_writes = []
    real_set_enable = hwmon.set_enable

    def spy(chip_dir, pwm, val):
        enable_writes.append((str(chip_dir).split("/")[-1], pwm, val))
        return real_set_enable(chip_dir, pwm, val)

    monkeypatch.setattr(hwmon, "set_enable", spy)
    rc = run_daemon(cfg_path, root, dry_run=False)
    assert rc == 0
    # takeover (2->1) then exact restore (->2), on hwmon0 (it8686)
    assert ("hwmon0", 3, 1) in enable_writes
    assert ("hwmon0", 3, 2) in enable_writes
    # duty written: 40 C -> 20% -> raw 51 (interp/clamp expectation)
    assert (root / "hwmon0" / "pwm3").read_text().strip() == "51"
    # enable back to the ORIGINAL value (2)
    assert (root / "hwmon0" / "pwm3_enable").read_text().strip() == "2"
    text = caplog.text
    assert "took over it8686:pwm3" in text
    assert "write it8686:pwm3 =  20% (raw 51)" in text
    assert "returned it8686:pwm3 to firmware" in text
    assert "refusing protected" not in text  # no protected fans assigned


def test_curve_interpolation_midpoint(tmp_path):
    """k10temp 60 C -> default curve midpoint 55->65: (30+70)/2 = 50%, raw 128."""
    root = make_tree(tmp_path)
    (root / "hwmon2" / "temp1_input").write_text("60000\n")
    cfg_path = make_config(tmp_path, {
        "free": {"source": "k10temp:temp1", "curve": "default", "fans": ["it8686:pwm3"],
                 "enabled": True},
    })
    rc = run_daemon(cfg_path, root, dry_run=False)
    assert rc == 0
    assert (root / "hwmon0" / "pwm3").read_text().strip() == "128"
    assert (root / "hwmon0" / "pwm3_enable").read_text().strip() == "2"


def test_protected_only_changes_nothing(caplog, tmp_path):
    """non-dry-run with only protected fans assigned: takes over nothing,
    exits 0, changes nothing (the M1 proof)."""
    caplog.set_level(logging.INFO)
    root = make_tree(tmp_path)
    cfg_path = make_config(tmp_path, {
        "prot1": {"source": "k10temp:temp1", "curve": "default",
                  "fans": ["it8792:pwm1"], "enabled": True},
        "prot3": {"source": "k10temp:temp1", "curve": "default",
                  "fans": ["it8792:pwm3"], "enabled": True},
    })
    before = snapshot_tree(root)
    rc = run_daemon(cfg_path, root, dry_run=False)
    after = snapshot_tree(root)
    assert rc == 0
    assert before == after, "protected-only run must not change any file"
    assert caplog.text.count("refusing protected channel it8792") == 2
    st = json.loads((cfg_path.parent / "status.json").read_text())
    assert st["fans"]["it8792:pwm1"]["state"] == "protected"
    assert st["fans"]["it8792:pwm1"]["enable"] == 1  # still manual/owned


def test_manual_conflict_refused(caplog, tmp_path):
    """a NON-protected channel already in manual mode (enable=1) is
    refused at takeover — the runtime backstop."""
    caplog.set_level(logging.INFO)
    root = make_tree(tmp_path)
    (root / "hwmon0" / "pwm4_enable").write_text("1\n")
    cfg_path = make_config(tmp_path, {
        "m": {"source": "k10temp:temp1", "curve": "default", "fans": ["it8686:pwm4"],
              "enabled": True},
    })
    before = snapshot_tree(root)
    rc = run_daemon(cfg_path, root, dry_run=False)
    after = snapshot_tree(root)
    assert rc == 0
    assert before == after
    assert "already in manual mode" in caplog.text
    st = json.loads((cfg_path.parent / "status.json").read_text())
    assert st["fans"]["it8686:pwm4"]["state"] == "manual-conflict"


def test_unknown_source_id_skips_assignment(caplog, tmp_path):
    """An assignment whose source id runtime discovery did not produce
    (stale after a hardware change) never takes over its fans."""
    caplog.set_level(logging.INFO)
    root = make_tree(tmp_path)
    cfg_path = make_config(
        tmp_path,
        {"dead": {"source": "k10temp:temp99", "curve": "default",
                  "fans": ["it8686:pwm3"], "enabled": True}})
    rc = run_daemon(cfg_path, root, dry_run=False)
    assert rc == 0
    # fan never taken over: duty and enable untouched
    assert (root / "hwmon0" / "pwm3").read_text().strip() == "70"
    assert (root / "hwmon0" / "pwm3_enable").read_text().strip() == "2"
    st = json.loads((cfg_path.parent / "status.json").read_text())
    assert st["fans"]["it8686:pwm3"]["state"] == "source-missing"


def test_dead_temp_filtered(caplog, tmp_path):
    """-55 C dead thermistor below dead_temp_c=-10 counts as no reading."""
    caplog.set_level(logging.INFO)
    root = make_tree(tmp_path)
    (root / "hwmon2" / "temp1_input").write_text("-55000\n")
    cfg_path = make_config(tmp_path, {
        "free": {"source": "k10temp:temp1", "curve": "default", "fans": ["it8686:pwm3"],
                 "enabled": True},
    })
    rc = run_daemon(cfg_path, root, dry_run=False)
    assert rc == 0
    assert (root / "hwmon0" / "pwm3_enable").read_text().strip() == "2"
    st = json.loads((cfg_path.parent / "status.json").read_text())
    assert st["sources"]["k10temp:temp1"]["stale"] is True


def test_gpu_source_drives_assignment(tmp_path):
    """GPU source: 68 C on gpu_v100 curve -> 75 + (100-75)*(68-66)/(72-66)
    = 83.33 -> first poll jumps to 83%, raw 212."""
    root = make_tree(tmp_path)
    cfg_path = make_config(tmp_path, {
        "g": {"source": "gpu:08:00.0", "curve": "gpu_v100",
              "fans": ["it8686:pwm3"], "enabled": True},
    })
    rc = run_daemon(cfg_path, root, dry_run=False)
    assert rc == 0
    assert (root / "hwmon0" / "pwm3").read_text().strip() == "212"
    assert (root / "hwmon0" / "pwm3_enable").read_text().strip() == "2"


def test_nvidia_smi_failure_at_start_no_gpu_sources(caplog, tmp_path):
    """GPU query broken at daemon start: no GPU sources are discovered,
    so the GPU assignment is skipped (fan stays with firmware) while the
    hwmon assignment still works — with a clear warning."""
    caplog.set_level(logging.INFO)
    root = make_tree(tmp_path)

    def broken(*f):
        raise RuntimeError("nvidia-smi: command not found")

    cfg_path = make_config(tmp_path, {
        "g": {"source": "gpu:08:00.0", "curve": "gpu_v100",
              "fans": ["it8686:pwm4"], "enabled": True},
        "c": {"source": "k10temp:temp1", "curve": "default",
              "fans": ["it8686:pwm3"], "enabled": True},
    })
    d = Daemon(cfg_path, dry_run=False, once=True, root=root,
               gpu_runner=broken)
    rc = d.run()
    assert rc == 0
    assert "nvidia-smi failed at source discovery" in caplog.text
    # hwmon-driven fan still written, gpu-driven fan untouched
    assert (root / "hwmon0" / "pwm3").read_text().strip() == "51"
    assert (root / "hwmon0" / "pwm4").read_text().strip() == "70"
    st = json.loads((cfg_path.parent / "status.json").read_text())
    assert "gpu:08:00.0" not in st["sources"]
    assert st["fans"]["it8686:pwm4"]["state"] == "source-missing"


# ------------------------------------------------------- hot reload -------

def test_hot_reload_bad_config_keeps_old(caplog, tmp_path):
    caplog.set_level(logging.INFO)
    root = make_tree(tmp_path)
    cfg_path = make_config(tmp_path, {
        "free": {"source": "k10temp:temp1", "curve": "default", "fans": ["it8686:pwm3"],
                 "enabled": True},
    })
    d = Daemon(cfg_path, dry_run=True, once=False, root=root,
               gpu_runner=lambda *f: FAKE_GPUS)
    cfg = d._initial_load()
    cfg_path.write_text("{this is not json")
    _bump_mtime(cfg_path)  # mtime watch: force a detectable change
    new = d._maybe_reload(cfg)
    assert new is cfg, "bad reload must keep the old in-memory config"
    assert "config reload failed" in caplog.text
    assert d._reload_error is not None

    # a VALID reload replaces the config
    doc = {
        "config": {"poll_seconds": 1.0, "max_rate": 6,
                   "floor_duty": 20, "dead_temp_c": -10},
        "assignments": {"free2": {"source": "k10temp:temp1", "curve": "default",
                                  "fans": ["it8686:pwm4"], "enabled": True}},
    }
    cfg_path.write_text(json.dumps(doc))
    _bump_mtime(cfg_path)
    new = d._maybe_reload(cfg)
    assert new is not cfg
    assert set(new.assignments) == {"free2"}
    assert d._reload_error is None


def test_hard_config_error_exits_2(caplog, tmp_path):
    caplog.set_level(logging.INFO)
    root = make_tree(tmp_path)
    cfg_path = make_config(tmp_path, {
        "bad": {"source": "k10temp:temp1", "curve": "default",
                "fans": ["it8686/pwm3"], "enabled": True},
    })
    rc = run_daemon(cfg_path, root, dry_run=True)
    assert rc == 2
    assert "hard config error" in caplog.text


def test_corrupt_config_file_exits_2(tmp_path):
    root = make_tree(tmp_path)
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text("{{{")
    rc = run_daemon(cfg_path, root, dry_run=True)
    assert rc == 2


# ---------------------------------------------------- per-fan floor/rate --

def _two_poll_run(tmp_path, assignments):
    """Run one daemon instance for exactly two polls, raising the source
    temp between them (40 C -> 75 C on the default curve: 20% -> 100%).
    Poll 1 jumps to the target; poll 2's step size is what's under test."""
    root = make_tree(tmp_path)
    cfg_path = make_config(tmp_path, assignments)  # global max_rate = 6
    d = Daemon(cfg_path, dry_run=False, once=True, root=root,
               gpu_runner=lambda *f: FAKE_GPUS)
    cfg = d._initial_load()
    assert cfg is not None
    d._initial_takeover(cfg)
    d._poll(cfg)                                   # 40 C -> 20% (jump)
    (root / "hwmon2" / "temp1_input").write_text("75000\n")
    d._poll(cfg)                                   # 75 C -> 100% (rate-limited)
    raw = (root / "hwmon0" / "pwm3").read_text().strip()
    return raw


def test_assignment_max_rate_overrides_global(tmp_path):
    """Per-fan rate: the assignment's max_rate (40 %/poll) overrides the
    global 6 — poll 2 steps 20% -> 60%, not 20% -> 26%."""
    raw = _two_poll_run(tmp_path, {
        "free": {"source": "k10temp:temp1", "curve": "default",
                 "fans": ["it8686:pwm3"], "enabled": True, "max_rate": 40},
    })
    assert raw == str(round(0.60 * 255))  # 153


def test_without_assignment_rate_global_applies(tmp_path):
    """No per-fan max_rate -> the global 6 %/poll applies: 20% -> 26%."""
    raw = _two_poll_run(tmp_path, {
        "free": {"source": "k10temp:temp1", "curve": "default",
                 "fans": ["it8686:pwm3"], "enabled": True},
    })
    assert raw == str(round(0.26 * 255))  # 66


def test_assignment_floor_overrides_global(tmp_path):
    """Per-fan floor: the assignment's floor_duty (45) overrides the
    global 20 — at 40 C the curve target is 20%, so the floor decides:
    45% here, 20% with the global. (The make_config global floor is
    20, mirroring a pre-upgrade user file.)"""
    root = make_tree(tmp_path)
    cfg_path = make_config(tmp_path, {
        "free": {"source": "k10temp:temp1", "curve": "default",
                 "fans": ["it8686:pwm3"], "enabled": True,
                 "floor_duty": 45},
    })
    rc = run_daemon(cfg_path, root, dry_run=False)
    assert rc == 0
    assert (root / "hwmon0" / "pwm3").read_text().strip() \
        == str(round(0.45 * 255))  # 115
