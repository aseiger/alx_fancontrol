"""Unit tests for runtime source discovery (sources.py).

Sources are a machine property: discovered from hwmon chips + nvidia-smi,
referenced by canonical ids. These tests prove the id scheme (unique vs
repeated chip prefixes, gpu bus ids), the label fallbacks, and that a
broken nvidia-smi degrades gracefully (no GPU sources, hwmon sources
intact).
"""
from alx_fancontrol import sources as S


def make_tree(tmp_path):
    """hwmon0=k10temp, hwmon1=k10temp (2nd package), hwmon2=it8686,
    hwmon3=iwlwifi (pwm-less, must be ignored)."""
    root = tmp_path / "hwmon"

    def chip(idx, name, **files):
        d = root / f"hwmon{idx}"
        d.mkdir(parents=True)
        (d / "name").write_text(name + "\n")
        for k, v in files.items():
            (d / k).write_text(str(v) + "\n")

    chip(0, "k10temp", temp1_input="40000", temp2_input="42000")
    chip(1, "k10temp", temp1_input="41000")
    chip(2, "it8686_2008090d", temp1_input="45000", temp5_input="37000")
    chip(3, "iwlwifi_1")  # no tempN_input/pwmN -> ignored by discovery
    return root


def test_canonical_ids_unique_vs_repeated_prefix(tmp_path):
    root = make_tree(tmp_path)
    srcs = S.discover(root=root, gpu_buses={},
                      labels={"it8686": {"temp1": "System 1"}})
    ids = [s.id for s in srcs]
    # repeated k10temp prefix -> indexed ids, in chip order
    assert "k10temp[0]:temp1" in ids and "k10temp[1]:temp1" in ids
    assert "k10temp[0]:temp2" in ids          # every tempN is a source
    # unique prefix -> bare id
    assert "it8686:temp1" in ids and "it8686:temp5" in ids
    # pwm-less chip contributes nothing
    assert not any(i.startswith("iwlwifi") for i in ids)
    # discovery order = chip (glob) order, then temp number
    assert ids == ["k10temp[0]:temp1", "k10temp[0]:temp2", "k10temp[1]:temp1",
                   "it8686:temp1", "it8686:temp5"]


def test_labels_from_sensorsd_with_fallbacks(tmp_path):
    root = make_tree(tmp_path)
    srcs = S.discover(root=root, gpu_buses={},
                      labels={"it8686": {"temp1": "System 1"}})
    by = S.by_id(srcs)
    assert by["it8686:temp1"].label == "System 1"      # sensors.d wins
    assert by["k10temp[0]:temp1"].label == "CPU0 Tctl"  # familiar fallback
    assert by["k10temp[1]:temp1"].label == "CPU1 Tctl"
    assert by["it8686:temp5"].label == "it8686 temp5"   # bare fallback


def test_gpu_sources_from_bus_map(tmp_path):
    root = make_tree(tmp_path)
    srcs = S.discover(root=root, gpu_buses={"44:00.0": 69,
                                            "08:00.0": 68})
    by = S.by_id(srcs)
    assert "gpu:08:00.0" in by and "gpu:44:00.0" in by
    assert by["gpu:08:00.0"].kind == "gpu"
    assert by["gpu:08:00.0"].pci_bus == "08:00.0"
    assert by["gpu:08:00.0"].label == "GPU 08:00.0"
    # bus-sorted
    gpu_ids = [s.id for s in srcs if s.kind == "gpu"]
    assert gpu_ids == ["gpu:08:00.0", "gpu:44:00.0"]


def test_gpu_buses_none_queries_runner(tmp_path):
    root = make_tree(tmp_path)
    calls = []

    def runner(*f):
        calls.append(f)
        return "00000000:08:00.0, 68\n"

    srcs = S.discover(root=root, gpu_runner=runner)
    assert any(s.id == "gpu:08:00.0" for s in srcs)
    assert calls, "runner must be used when gpu_buses is None"


def test_gpu_runner_failure_degrades_to_no_gpus(tmp_path):
    root = make_tree(tmp_path)

    def broken(*f):
        raise RuntimeError("nvidia-smi: command not found")

    srcs = S.discover(root=root, gpu_runner=broken)
    assert not any(s.kind == "gpu" for s in srcs)
    # hwmon sources unaffected
    assert "k10temp[0]:temp1" in S.by_id(srcs)


def test_detail_strings():
    h = S.SourceInfo(id="it8686:temp1", kind="hwmon", label="System 1",
                     chip="it8686", chip_index=0, temp=1)
    assert h.detail == "it8686:temp1"
    h2 = S.SourceInfo(id="k10temp[1]:temp1", kind="hwmon", label="CPU1 Tctl",
                      chip="k10temp", chip_index=1, temp=1)
    assert h2.detail == "k10temp:temp1 (chip_index 1)"
    g = S.SourceInfo(id="gpu:08:00.0", kind="gpu", label="GPU 08:00.0",
                     pci_bus="08:00.0")
    assert g.detail == "pci 08:00.0"
