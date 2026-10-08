"""Runtime source discovery.

Temperature sources are a property of the MACHINE, not of the config:
they are discovered at runtime (hwmon chips + nvidia-smi) by both the
daemon and the TUI. The config only stores curves and source→fan
mappings (assignments), and references sources by their canonical id.

Canonical ids (stable across reboots / hwmon index reshuffles):
    hwmon, unique chip prefix:   "it8686:temp1"
    hwmon, repeated prefix:      "k10temp[0]:temp1", "k10temp[1]:temp1"
    nvidia-smi GPU (pci bus):    "gpu:08:00.0"

If a source an assignment references is not detected (chip gone, prefix
started repeating, GPU removed), the daemon/TUI skip it with a warning —
the user re-picks it in the TUI.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from alx_fancontrol import hwmon

_TEMP_RE = re.compile(r"temp(\d+)_input")


@dataclass(frozen=True)
class SourceInfo:
    id: str
    kind: str                # "hwmon" | "gpu"
    label: str
    chip: str | None = None
    chip_index: int = 0
    temp: int | None = None
    pci_bus: str | None = None

    @property
    def detail(self) -> str:
        if self.kind == "gpu":
            return f"pci {self.pci_bus}"
        base = f"{self.chip}:temp{self.temp}"
        return base + (f" (chip_index {self.chip_index})"
                       if self.chip_index else "")


def canonical_id(kind: str, chip: str | None = None, chip_index: int = 0,
                 temp: int | None = None, pci_bus: str | None = None,
                 repeated: bool = False) -> str:
    if kind == "gpu":
        return f"gpu:{pci_bus}"
    base = f"{chip}[{chip_index}]" if repeated else chip
    return f"{base}:temp{temp}"


def _hwmon_label(chip: str, idx: int, temp: int,
                 labels: dict) -> str:
    lab = (labels.get(chip) or {}).get(f"temp{temp}")
    if lab:
        return lab
    if chip == "k10temp" and temp == 1:
        return f"CPU{idx} Tctl"        # familiar name for Tdie/Tctl pkg temps
    if idx:
        return f"{chip}[{idx}] temp{temp}"
    return f"{chip} temp{temp}"


def discover(chips=None, root=hwmon.HWMON_ROOT, gpu_buses: dict | None = None,
             gpu_runner=None, labels: dict | None = None) -> list[SourceInfo]:
    """All temperature sources on this machine, in stable order: hwmon
    sources (chip-prefix order, then temp number), then GPUs (pci bus
    order). Everything takes injectables so tests run hermetically."""
    if chips is None:
        chips = hwmon.find_all_chips(root)
    if labels is None:
        labels = hwmon.load_sensorsd_labels()

    counts: dict[str, int] = {}
    for prefix, _d in chips:
        counts[prefix] = counts.get(prefix, 0) + 1

    out: list[SourceInfo] = []
    seen: dict[str, int] = {}
    for prefix, d in chips:
        try:
            entries = [p.name for p in d.iterdir()]
        except OSError:
            continue
        temps = sorted({int(m.group(1)) for n in entries
                        if (m := _TEMP_RE.fullmatch(n))})
        idx = seen.get(prefix, 0)
        seen[prefix] = idx + 1
        repeated = counts.get(prefix, 0) > 1
        for t in temps:
            out.append(SourceInfo(
                id=canonical_id("hwmon", prefix, idx, t, repeated=repeated),
                kind="hwmon",
                label=_hwmon_label(prefix, idx, t, labels),
                chip=prefix, chip_index=idx, temp=t))

    if gpu_buses is None:
        try:
            gpu_buses = hwmon.gpu_temps(gpu_runner)
        except Exception:
            gpu_buses = {}
    for bus in sorted(gpu_buses):
        out.append(SourceInfo(id=canonical_id("gpu", pci_bus=bus),
                              kind="gpu", label=f"GPU {bus}", pci_bus=bus))
    return out


def by_id(sources: list[SourceInfo]) -> dict[str, SourceInfo]:
    return {s.id: s for s in sources}
