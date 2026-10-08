"""FakeMachine: synthetic machine for hermetic tests.

Everything the app reads from the system — hwmon chips, temperatures,
fan RPMs, pwm duty/enable, GPUs, sensors.d labels — lives here. Tests
INJECT the values they need (set_temp / set_rpm / set_pwm / add_gpu)
and read back what they injected; nothing assumes a value about the real
machine, and nothing hard-codes an assumption about fixture defaults
either: if a test cares about a value, it sets one.

`machine.apply(monkeypatch)` (done by the conftest fixture) points the
app's hwmon layer at this machine, so daemon, TUI app init, and the
live reader all see the same synthetic data.
"""
from __future__ import annotations

import copy
from pathlib import Path

from alx_fancontrol import hwmon, sources


class FakeMachine:
    def __init__(self, root: Path):
        self.root = Path(root)
        self._ordered: list[tuple[str, Path]] = []
        self._dirs: dict[str, list[Path]] = {}
        self.gpus: dict[str, float] = {}       # pci bus -> temp °C
        self.gpu_names: dict[str, str] = {}    # pci bus -> model name
        self.labels: dict[str, dict[str, str]] = {}

    # ------------------------------------------------------ construction --
    def add_chip(self, name: str, **sensors) -> Path:
        """Add a chip to the fake /sys tree. Sensor values:
            tempN = float °C            (stored as millidegrees)
            fanN  = int RPM
            pwmN  = int duty 0-255 (enable 2) or (duty, enable)
        """
        idx = len(self._ordered)
        d = self.root / f"hwmon{idx}"
        d.mkdir(parents=True)
        (d / "name").write_text(name + "\n")
        prefix = hwmon.chip_prefix(name)
        self._dirs.setdefault(prefix, []).append(d)
        self._ordered.append((prefix, d))
        for k, v in sensors.items():
            self._write_sensor(d, k, v)
        return d

    def _write_sensor(self, d: Path, k: str, v) -> None:
        if k.startswith("temp"):
            (d / f"{k}_input").write_text(f"{int(round(float(v) * 1000))}\n")
        elif k.startswith("fan"):
            (d / f"{k}_input").write_text(f"{int(v)}\n")
        elif k.startswith("pwm"):
            duty, enable = v if isinstance(v, tuple) else (v, 2)
            (d / k).write_text(f"{int(duty)}\n")
            (d / f"{k}_enable").write_text(f"{int(enable)}\n")
        else:
            raise ValueError(f"unknown sensor kind: {k}")

    # -------------------------------------------------------- injection ----
    def set_temp(self, prefix: str, n: int, celsius: float) -> None:
        self._write_sensor(self._chip_dir(prefix), f"temp{n}", celsius)

    def set_rpm(self, prefix: str, n: int, rpm: int) -> None:
        self._write_sensor(self._chip_dir(prefix), f"fan{n}", rpm)

    def set_pwm(self, prefix: str, n: int, duty: int | None = None,
                enable: int | None = None) -> None:
        d = self._chip_dir(prefix)
        if duty is not None:
            (d / f"pwm{n}").write_text(f"{int(duty)}\n")
        if enable is not None:
            (d / f"pwm{n}_enable").write_text(f"{int(enable)}\n")

    def add_gpu(self, bus: str, temp_c: float, name: str = "") -> None:
        self.gpus[bus] = float(temp_c)
        if name:
            self.gpu_names[bus] = name

    def _chip_dir(self, prefix: str) -> Path:
        dirs = self._dirs.get(prefix)
        if not dirs:
            raise KeyError(f"no chip with prefix {prefix!r} on the fake machine")
        return dirs[0]

    # ---------------------------------------------------------- readbacks --
    @property
    def chips(self) -> list[tuple[str, Path]]:
        return list(self._ordered)

    def source_ids(self) -> list[str]:
        return [s.id for s in self._discover()]

    def fan_ids(self) -> list[str]:
        return [fan for fan, _d, _p in
                hwmon.list_pwm_channels(self.chips)]

    def _discover(self):
        return sources.discover(self.chips, gpu_buses=dict(self.gpus),
                                labels=self.labels)

    def live_payload(self) -> dict:
        """A LiveUpdate dict carrying the machine's CURRENT synthetic
        values for every detected source and pwm channel. Tests may
        mutate entries to inject ad-hoc readings:
            p = machine.live_payload(); p["fans"]["it8686:pwm3"]["rpm"] = 999
        """
        data: dict = {"ts": 0.0, "sources": {}, "fans": {}, "status": None}
        for s in self._discover():
            if s.kind == "gpu":
                entry: dict = {"temp_c": self.gpus.get(s.pci_bus)}
                if s.pci_bus in self.gpu_names:
                    entry["gpu_name"] = self.gpu_names[s.pci_bus]
                data["sources"][s.id] = entry
            else:
                data["sources"][s.id] = {
                    "temp_c": hwmon.read_temp_c(self._chip_dir(s.chip), s.temp)
                }
        for fan in self.fan_ids():
            chip, pwm = hwmon.parse_fan_id(fan)
            d = self._chip_dir(chip)
            data["fans"][fan] = {
                "label": hwmon.sensor_label(chip, "pwm", pwm, self.labels),
                "duty_pct": None,
                "rpm": hwmon.read_rpm(d, pwm),
                "enable": hwmon.get_enable(d, pwm),
            }
        return data

    # ------------------------------------------------------ hwmon inject --
    def gpu_runner(self, *fields: str) -> str:
        """Emulates the nvidia-smi CSV output hwmon expects."""
        out = []
        for bus in sorted(self.gpus):
            vals = []
            for f in fields:
                if f == "pci.bus_id":
                    vals.append(f"00000000:{bus}")
                elif f == "temperature.gpu":
                    vals.append(str(int(self.gpus[bus])))
                elif f == "name":
                    vals.append(self.gpu_names.get(bus, ""))
            out.append(", ".join(vals))
        return "\n".join(out) + ("\n" if out else "")

    def apply(self, monkeypatch) -> None:
        """Point the app's hwmon layer at this machine (daemon, TUI app
        init, and live reader all see the same synthetic data)."""
        monkeypatch.setattr(hwmon, "find_all_chips",
                            lambda root=hwmon.HWMON_ROOT: self.chips)
        monkeypatch.setattr(hwmon, "load_sensorsd_labels",
                            lambda conf_dir=None: copy.deepcopy(self.labels))
        monkeypatch.setattr(hwmon, "gpu_temps",
                            lambda runner=None: dict(self.gpus))
        monkeypatch.setattr(
            hwmon, "gpu_info",
            lambda runner=None: {
                bus: {"temp": t, "name": self.gpu_names.get(bus, "")}
                for bus, t in self.gpus.items()})
