"""Unit tests for the config model (v2): round-trip, .bak, validation,
seeding, and the v1 -> v2 migration (sources moved out of the config to
runtime discovery)."""
import json

import pytest

from alx_fancontrol import config as C


def make_fake_root(tmp_path):
    """Fake hwmon tree: hwmon0=k10temp, hwmon1=k10temp (2nd pkg),
    hwmon2=it8686, hwmon3=it8792 — deliberately NOT the real machine's
    index order (there it8686=hwmon1, it8792=hwmon2)."""
    root = tmp_path / "hwmon"

    def chip(idx, name, temps=(), pwms=(), fans=()):
        d = root / f"hwmon{idx}"
        d.mkdir(parents=True)
        (d / "name").write_text(name + "\n")
        for t in temps:
            (d / f"temp{t}_input").write_text("45000\n")
        for p in pwms:
            (d / f"pwm{p}").write_text("70\n")
            (d / f"pwm{p}_enable").write_text("2\n")
        for f in fans:
            (d / f"fan{f}_input").write_text("1234\n")

    chip(0, "k10temp", temps=(1, 2))
    chip(1, "k10temp", temps=(1, 2))
    chip(2, "it8686_2008090d", temps=(1, 2, 3), pwms=(1, 2, 3, 4, 5),
         fans=(1, 2, 3, 4, 5))
    chip(3, "it8792_2008090d", temps=(1, 2, 3), pwms=(1, 2, 3),
         fans=(1, 2, 3))
    # a pwm-less chip that find_chips must ignore
    d = root / "hwmon4"
    d.mkdir()
    (d / "name").write_text("iwlwifi_1\n")
    return root


def test_roundtrip_load_save_load(tmp_path):
    p = tmp_path / "c.json"
    cfg = C.seed_if_missing(p)
    cfg.assignments["test"] = {
        "source": "k10temp[0]:temp1", "curve": "default",
        "fans": ["it8686:pwm3"], "floor_duty": 15, "max_duty": 95,
        "enabled": True,
    }
    C.save(cfg, p)
    cfg2 = C.load(p)
    assert cfg2.assignments == cfg.assignments
    assert cfg2.curves == cfg.curves
    doc = json.loads(p.read_text())
    assert doc["version"] == C.CONFIG_VERSION
    assert "sources" not in doc  # v2: sources live at runtime, not here
    assert "protected" not in doc  # removed from the design


def test_bak_holds_previous_content(tmp_path):
    p = tmp_path / "c.json"
    cfg = C.seed_if_missing(p)
    v1_text = p.read_text()
    cfg.config["poll_seconds"] = 2.0
    C.save(cfg, p)
    v2_text = p.read_text()
    bak = p.parent / (p.name + ".bak")
    assert bak.exists()
    assert bak.read_text() == v1_text
    assert p.read_text() == v2_text
    assert json.loads(v2_text)["config"]["poll_seconds"] == 2.0


def test_save_is_atomic_no_tmp_left(tmp_path):
    p = tmp_path / "c.json"
    cfg = C.seed_if_missing(p)
    cfg.config["poll_seconds"] = 1.5
    C.save(cfg, p)
    assert not (p.parent / (p.name + ".tmp")).exists()
    json.loads(p.read_text())  # valid JSON


def test_corrupt_json_load_raises(tmp_path):
    p = tmp_path / "c.json"
    p.write_text("{not json at all")
    with pytest.raises(ValueError):
        C.load(p)


def test_non_object_root_raises(tmp_path):
    p = tmp_path / "c.json"
    p.write_text("[1, 2, 3]")
    with pytest.raises(ValueError):
        C.load(p)


def test_minimal_config_keeps_defaults(tmp_path):
    p = tmp_path / "c.json"
    p.write_text('{"version": 1}\n')
    cfg = C.load(p)
    assert cfg.config["poll_seconds"] == 1.0
    assert "default" in cfg.curves
    errors, warnings = C.validate(cfg)
    assert errors == []
    assert warnings == []


def test_seed_produces_valid_config(tmp_path):
    p = tmp_path / "c.json"
    cfg = C.seed_if_missing(p)
    errors, warnings = C.validate(cfg)
    assert errors == []
    assert warnings == []
    # v2: no sources section (runtime discovery), default curves present
    doc = C.to_dict(cfg)
    assert "sources" not in doc
    assert "protected" not in doc  # removed from the design
    assert "default" in cfg.curves and "gpu_v100" in cfg.curves
    # guardrail: a fresh config controls nothing
    assert cfg.assignments == {}
    # file was written and loads back
    assert p.exists()
    assert C.load(p).assignments == cfg.assignments


def test_seed_returns_existing_config(tmp_path):
    p = tmp_path / "c.json"
    first = C.seed_if_missing(p)
    first.config["poll_seconds"] = 3.3
    C.save(first, p)
    second = C.seed_if_missing(p)
    assert second.config["poll_seconds"] == 3.3  # not re-seeded


# ------------------------------------------------------- v1 -> v2 --------
# The user's real v1 config looked like this: hand-made source ids
# ("cpu0", "sys1", "gpu08"...) plus a sources section describing them.

V1_DOC = {
    "version": 1,
    "config": {"poll_seconds": 1.0, "max_rate": 6, "floor_duty": 20,
               "dead_temp_c": -10},
    "protected": ["it8792:pwm1", "it8792:pwm3"],
    "sources": {
        "cpu0": {"kind": "hwmon", "chip": "k10temp", "chip_index": 0,
                 "temp": 1, "label": "CPU0 Tctl", "enabled": True},
        "cpu1": {"kind": "hwmon", "chip": "k10temp", "chip_index": 1,
                 "temp": 1, "label": "CPU1 Tctl", "enabled": True},
        "sys1": {"kind": "hwmon", "chip": "it8686", "temp": 1,
                 "label": "System 1", "enabled": True},
        "gpu08": {"kind": "nvidia", "pci_bus": "08:00.0",
                  "label": "GPU 08:00.0", "enabled": True},
        "gpu44": {"kind": "nvidia", "pci_bus": "44:00.0",
                  "label": "GPU 44:00.0", "enabled": True},
    },
    "curves": {
        "default": {"label": "Default",
                    "points": [[40, 20], [55, 30], [65, 70], [75, 100]]},
    },
    "assignments": {
        "CPU": {"source": "cpu0", "curve": "default",
                "fans": ["it8686:pwm1"], "enabled": True},
        "System": {"source": "sys1", "curve": "default",
                   "fans": ["it8686:pwm3", "it8686:pwm4"], "enabled": True},
        "GPU-low": {"source": "gpu08", "curve": "default",
                    "fans": ["it8792:pwm1"], "enabled": True},
    },
}


def test_v1_migration_rewrites_source_ids(tmp_path):
    p = tmp_path / "c.json"
    p.write_text(json.dumps(V1_DOC))
    cfg = C.load(p)
    a = cfg.assignments
    # repeated k10temp prefix -> indexed ids; unique chip -> bare; gpu -> bus
    assert a["CPU"]["source"] == "k10temp[0]:temp1"
    assert a["System"]["source"] == "it8686:temp1"
    assert a["GPU-low"]["source"] == "gpu:08:00.0"
    # everything else preserved
    assert a["CPU"]["fans"] == ["it8686:pwm1"]
    assert cfg.curves["default"]["points"] == V1_DOC["curves"][
        "default"]["points"]
    # re-saving produces a clean v2 doc without the sources section — and
    # the legacy 'protected' key is dropped, not carried forward
    C.save(cfg, p)
    doc = json.loads(p.read_text())
    assert doc["version"] == C.CONFIG_VERSION
    assert "sources" not in doc
    assert "protected" not in doc
    assert doc["assignments"]["CPU"]["source"] == "k10temp[0]:temp1"


def test_v1_migration_single_k10temp_gets_no_index(tmp_path):
    """Indexing follows the legacy config's own chip counts: one k10temp
    in the config -> bare id, matching runtime discovery on a 1-package
    box."""
    p = tmp_path / "c.json"
    doc = json.loads(json.dumps(V1_DOC))
    del doc["sources"]["cpu1"]
    p.write_text(json.dumps(doc))
    cfg = C.load(p)
    assert cfg.assignments["CPU"]["source"] == "k10temp:temp1"


def test_v1_migration_unknown_legacy_source_untouched(tmp_path):
    p = tmp_path / "c.json"
    doc = json.loads(json.dumps(V1_DOC))
    doc["assignments"]["Ghost"] = {"source": "ghost", "curve": "default",
                                   "fans": ["it8686:pwm5"], "enabled": True}
    p.write_text(json.dumps(doc))
    cfg = C.load(p)
    assert cfg.assignments["Ghost"]["source"] == "ghost"  # daemon skips it


# ------------------------------------------------------------ validate ----

def _cfg_with(assignments=None, curves=None, config=None):
    return C.Config(
        config=config or dict(C.DEFAULTS["config"]),
        curves=curves if curves is not None
        else dict(C.DEFAULTS["curves"]),
        assignments=assignments or {},
    )


def test_missing_source_id_is_error():
    cfg = _cfg_with(assignments={
        "a": {"curve": "default", "fans": ["it8686:pwm3"]},
    })
    errors, _ = C.validate(cfg)
    assert any("'source'" in e for e in errors)


def test_duplicate_fan_across_assignments_is_error():
    cfg = _cfg_with(assignments={
        "a": {"source": "k10temp:temp1", "curve": "default",
              "fans": ["it8686:pwm3"]},
        "b": {"source": "k10temp:temp1", "curve": "default",
              "fans": ["it8686:pwm3"]},
    })
    errors, _ = C.validate(cfg)
    assert any("it8686:pwm3" in e and "both" in e for e in errors)


def test_unsorted_curve_points_is_error():
    cfg = _cfg_with(curves={"bad": {"label": "bad",
                                    "points": [[50, 10], [40, 90]]}})
    errors, _ = C.validate(cfg)
    assert any("strictly increasing" in e for e in errors)


def test_curve_duty_out_of_range_is_error():
    cfg = _cfg_with(curves={"bad": {"label": "bad",
                                    "points": [[40, 101]]}})
    errors, _ = C.validate(cfg)
    assert any("0-100" in e for e in errors)


def test_unknown_curve_ref_is_warning():
    cfg = _cfg_with(assignments={
        "a": {"source": "k10temp:temp1", "curve": "ghost",
              "fans": ["it8686:pwm3"]},
    })
    errors, warnings = C.validate(cfg)
    assert errors == []
    assert any("unknown curve" in w for w in warnings)


def test_floor_gt_max_is_error():
    cfg = _cfg_with(assignments={
        "a": {"source": "k10temp:temp1", "curve": "default",
              "fans": ["it8686:pwm3"], "floor_duty": 60, "max_duty": 40},
    })
    errors, _ = C.validate(cfg)
    assert any("floor_duty" in e and "max_duty" in e for e in errors)


def test_bad_fan_id_is_error():
    cfg = _cfg_with(assignments={
        "a": {"source": "k10temp:temp1", "curve": "default",
              "fans": ["it8686/fan3"]},
    })
    errors, _ = C.validate(cfg)
    assert any("bad fan id" in e for e in errors)


def test_bad_global_settings_are_errors():
    cfg = _cfg_with(config={"poll_seconds": 0, "max_rate": -1,
                            "floor_duty": 150, "dead_temp_c": "cold"})
    errors, _ = C.validate(cfg)
    assert len(errors) == 4


# ------------------------------------------- per-fan floor/ceiling/rate ----

def test_default_floor_is_5(tmp_path):
    """The global default floor is 5% (quiet idle), not the old 20%."""
    p = tmp_path / "c.json"
    cfg = C.seed_if_missing(p)
    assert cfg.config["floor_duty"] == 5
    doc = json.loads(p.read_text())
    assert doc["config"]["floor_duty"] == 5


def test_assignment_max_rate_roundtrip(tmp_path):
    p = tmp_path / "c.json"
    cfg = C.seed_if_missing(p)
    cfg.assignments["a"] = {"source": "k10temp:temp1", "curve": "default",
                            "fans": ["it8686:pwm3"], "max_rate": 12,
                            "enabled": True}
    C.save(cfg, p)
    cfg2 = C.load(p)
    assert cfg2.assignments["a"]["max_rate"] == 12


def test_assignment_max_rate_validation():
    cfg = _cfg_with(assignments={
        "a": {"source": "k10temp:temp1", "curve": "default",
              "fans": ["it8686:pwm3"], "max_rate": 12},
    })
    errors, _ = C.validate(cfg)
    assert errors == []

    cfg = _cfg_with(assignments={
        "a": {"source": "k10temp:temp1", "curve": "default",
              "fans": ["it8686:pwm3"], "max_rate": -1},
    })
    errors, _ = C.validate(cfg)
    assert any("max_rate" in e for e in errors)

    cfg = _cfg_with(assignments={
        "a": {"source": "k10temp:temp1", "curve": "default",
              "fans": ["it8686:pwm3"], "max_rate": "fast"},
    })
    errors, _ = C.validate(cfg)
    assert any("max_rate" in e for e in errors)
