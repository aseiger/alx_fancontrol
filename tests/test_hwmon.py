"""hwmon module against a fake tree in tmp_path — discovery by name,
conversions, reads, writes, nvidia runner injection, sensors.d labels."""
from pathlib import Path

from alx_fancontrol import hwmon


def make_tree(tmp_path) -> Path:
    root = tmp_path / "hwmon"

    def chip(idx, name, **files):
        d = root / f"hwmon{idx}"
        d.mkdir(parents=True)
        (d / "name").write_text(name + "\n")
        for k, v in files.items():
            (d / k).write_text(str(v) + "\n")

    # deliberately NOT the live machine's index order (there it8686=hwmon1,
    # it8792=hwmon2): discovery must be by name, not index
    chip(1, "it8686_2008090d",
         temp1_input=47000, temp6_input=-55000,
         fan1_input=0, fan3_input=3229, fan4_input=3292,
         pwm3=70, pwm3_enable=2, pwm4=70, pwm4_enable=2)
    chip(0, "it8792_2008090d",
         temp1_input=55000,
         pwm1=140, pwm1_enable=1, pwm3=140, pwm3_enable=1)
    chip(2, "k10temp", temp1_input=61750, temp2_input=34750)
    chip(3, "nvme", temp1_input=35000)
    # dead wifi thermistor: temp file present but empty
    chip(4, "iwlwifi_1", temp1_input="")
    return root


def test_find_chips_by_name_not_index(tmp_path):
    root = make_tree(tmp_path)
    chips = hwmon.find_chips(root)
    assert chips["it8686"].name == "hwmon1"
    assert chips["it8792"].name == "hwmon0"
    assert chips["k10temp"].name == "hwmon2"
    assert "nvme" in chips
    assert "iwlwifi" in chips  # has temp1_input (empty = dead, still a chip)


def test_find_ignores_pwmless_and_nameless(tmp_path):
    root = make_tree(tmp_path)
    d = root / "hwmon9"
    d.mkdir()
    (d / "name").write_text("in0only\n")
    (d / "in0_input").write_text("123\n")
    d2 = root / "hwmon8"   # has pwm but no name file -> skipped
    d2.mkdir()
    (d2 / "pwm1").write_text("5\n")
    chips = hwmon.find_chips(root)
    assert "in0only" not in chips
    assert "hwmon8" not in {c.name for c in chips.values()}


def test_find_all_chips_duplicates(tmp_path):
    root = make_tree(tmp_path)
    d = root / "hwmon5"
    d.mkdir()
    (d / "name").write_text("k10temp\n")
    (d / "temp1_input").write_text("10000\n")
    allc = hwmon.find_all_chips(root)
    k10 = [c for p, c in allc if p == "k10temp"]
    assert len(k10) == 2
    assert hwmon.chip_dir_for("k10temp", 0, allc).name == "hwmon2"
    assert hwmon.chip_dir_for("k10temp", 1, allc).name == "hwmon5"
    assert hwmon.chip_dir_for("k10temp", 2, allc) is None
    # first-seen wins in the dict form
    assert hwmon.find_chips(root)["k10temp"].name == "hwmon2"


def test_temp_millidegree_conversion(tmp_path):
    root = make_tree(tmp_path)
    chips = hwmon.find_chips(root)
    assert hwmon.read_temp_c(chips["it8686"], 1) == 47.0
    assert hwmon.read_temp_c(chips["it8686"], 6) == -55.0   # dead thermistor
    assert hwmon.read_temp_c(chips["k10temp"], 2) == 34.75


def test_reads_return_none_when_missing_or_empty(tmp_path):
    root = make_tree(tmp_path)
    chips = hwmon.find_chips(root)
    assert hwmon.read_temp_c(chips["it8686"], 9) is None
    assert hwmon.read_temp_c(chips["iwlwifi"], 1) is None    # empty file
    assert hwmon.read_rpm(chips["it8686"], 9) is None
    assert hwmon.read_duty_raw(chips["it8686"], 1) is None   # no pwm1
    assert hwmon.get_enable(chips["it8686"], 1) is None


def test_rpm_duty_enable_reads(tmp_path):
    root = make_tree(tmp_path)
    chips = hwmon.find_chips(root)
    assert hwmon.read_rpm(chips["it8686"], 3) == 3229
    assert hwmon.read_rpm(chips["it8686"], 4) == 3292
    assert hwmon.read_rpm(chips["it8686"], 1) == 0
    assert hwmon.read_duty_raw(chips["it8686"], 3) == 70
    assert hwmon.get_enable(chips["it8686"], 3) == 2
    assert hwmon.get_enable(chips["it8792"], 1) == 1
    assert hwmon.get_enable(chips["it8792"], 3) == 1


def test_write_duty_and_enable(tmp_path):
    root = make_tree(tmp_path)
    chips = hwmon.find_chips(root)
    hwmon.write_duty(chips["it8686"], 3, 128)
    assert (chips["it8686"] / "pwm3").read_text().strip() == "128"
    hwmon.set_enable(chips["it8686"], 3, 1)
    assert (chips["it8686"] / "pwm3_enable").read_text().strip() == "1"
    # duty is clamped into 0-255
    hwmon.write_duty(chips["it8686"], 3, 999)
    assert (chips["it8686"] / "pwm3").read_text().strip() == "255"
    hwmon.write_duty(chips["it8686"], 3, -5)
    assert (chips["it8686"] / "pwm3").read_text().strip() == "0"


def test_norm_bus():
    assert hwmon.norm_bus("00000000:44:00.0") == "44:00.0"
    assert hwmon.norm_bus("  00000000:08:00.0 ") == "08:00.0"


def test_gpu_temps_with_injected_runner():
    def runner(*fields):
        assert fields == ("pci.bus_id", "temperature.gpu")
        return "00000000:08:00.0, 68\n00000000:44:00.0, 69\n"
    assert hwmon.gpu_temps(runner) == {"08:00.0": 68, "44:00.0": 69}


def test_gpu_info_with_injected_runner():
    def runner(*fields):
        assert fields == ("pci.bus_id", "temperature.gpu", "name")
        return "00000000:08:00.0, 68, Tesla V100 SXM2 32GB\n"
    info = hwmon.gpu_info(runner)
    assert info == {"08:00.0": {"temp": 68, "name": "Tesla V100 SXM2 32GB"}}


def test_resolve_fan_and_tach(tmp_path):
    root = make_tree(tmp_path)
    chips = hwmon.find_all_chips(root)
    d, idx = hwmon.resolve_fan("it8686:pwm3", chips, root)
    assert (d.name, idx) == ("hwmon1", 3)
    d, idx = hwmon.resolve_tach("it8686:fan3", chips, root)
    assert (d.name, idx) == ("hwmon1", 3)
    assert hwmon.resolve_fan("nope:pwm1", chips, root) is None
    assert hwmon.resolve_fan("it8686/fan3", chips, root) is None
    assert hwmon.resolve_fan("it8686:fan3", chips, root) is None  # wrong kind


def test_list_pwm_channels(tmp_path):
    root = make_tree(tmp_path)
    chans = hwmon.list_pwm_channels(None, root)
    ids = [c[0] for c in chans]
    assert "it8686:pwm3" in ids
    assert "it8686:pwm4" in ids
    assert "it8792:pwm1" in ids
    assert "it8686:pwm1" not in ids   # no pwm1_enable file in the fake
    # (fan_id, dir, idx) shape
    fid, d, i = [c for c in chans if c[0] == "it8792:pwm1"][0]
    assert (d.name, i) == ("hwmon0", 1)


def test_sensor_id_parsing():
    assert hwmon.parse_fan_id("it8792:pwm1") == ("it8792", 1)
    assert hwmon.parse_fan_id("bad") is None
    assert hwmon.parse_fan_id("it8792:pwm") is None
    assert hwmon.parse_tach_id("it8686:fan3") == ("it8686", 3)
    assert hwmon.parse_tach_id("it8686:pwm3") is None


def test_sensorsd_labels(tmp_path):
    conf = tmp_path / "sensors.d"
    conf.mkdir()
    (conf / "gigabyte-it87.conf").write_text(
        "# comment\n"
        'chip "it8686_2008090d-*"\n'
        '    label in0     "CPU Vcore"\n'
        '    label temp1   "System 1"\n'
        '    label fan3    "SYS_FAN2"\n'
        '    label pwm3    "SYS_FAN2"\n'
        'chip "it8792_2008090d-*"\n'
        '    label temp1   "PCIEX8"\n'
        '    label pwm1    "SYS_FAN5_PUMP"\n')
    labels = hwmon.load_sensorsd_labels(conf)
    assert labels["it8686"]["temp1"] == "System 1"
    assert labels["it8686"]["fan3"] == "SYS_FAN2"
    assert labels["it8686"]["pwm3"] == "SYS_FAN2"
    assert "in0" not in labels["it8686"]      # in0 labels ignored
    assert labels["it8792"]["pwm1"] == "SYS_FAN5_PUMP"
    assert hwmon.sensor_label("it8686", "temp", 1, labels) == "System 1"
    assert hwmon.sensor_label("it8686", "temp", 9, labels) == "temp9 (it8686)"
    assert hwmon.sensor_label("it8686", "temp", 1) == "temp1 (it8686)"
    assert hwmon.load_sensorsd_labels(tmp_path / "missing-dir") == {}
