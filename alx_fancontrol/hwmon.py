"""Sysfs hwmon access + nvidia-smi GPU temps + sensors.d labels.

Everything takes an injectable root (default /sys/class/hwmon) and the GPU
call takes an injectable runner so tests run hermetically against a fake
tree. hwmon indices are UNSTABLE across reboots (today it8686=hwmon1,
it8792=hwmon2) — discovery is always by `name` prefix, never by hwmonN.

pwmN_enable semantics on this board:
    0 = disabled, 1 = manual (software control), 2 = firmware/BIOS SmartFan.
The running gpu-fanctl service owns it8792 pwm1/pwm3 (enable=1); see
scripts/protect.py and the `protected` config section.
"""
from __future__ import annotations

import glob
import os
import re
import subprocess
from pathlib import Path

HWMON_ROOT = "/sys/class/hwmon"
SENSORS_D_DIR = "/etc/sensors.d"

ENABLE_DISABLED = 0
ENABLE_MANUAL = 1
ENABLE_FIRMWARE = 2

FAN_ID_RE = re.compile(r"^(\w+):pwm(\d+)$")
TACH_ID_RE = re.compile(r"^(\w+):fan(\d+)$")

_CHIP_RE = re.compile(r'^chip\s+"([^"]+)"')
_LABEL_RE = re.compile(r'^label\s+(temp|fan|pwm|in)(\d+)\s+"([^"]*)"')


def chip_prefix(raw_name: str) -> str:
    """'it8686_2008090d' -> 'it8686'; 'k10temp' -> 'k10temp'; 'iwlwifi_1' -> 'iwlwifi'."""
    return re.split(r"[_-]", raw_name, 1)[0]


def find_all_chips(root=HWMON_ROOT) -> list[tuple[str, Path]]:
    """[(name_prefix, dir)] for every hwmon* that has tempN_input or pwmN
    files, in sorted glob order. Multiple chips may share a prefix (two
    k10temp packages) — callers disambiguate with chip_index."""
    out: list[tuple[str, Path]] = []
    for d in sorted(glob.glob(os.path.join(str(root), "hwmon*"))):
        d = Path(d)
        try:
            raw = (d / "name").read_text().strip()
            entries = [p.name for p in d.iterdir()]
        except OSError:
            continue
        if not raw:
            continue
        if not any(re.fullmatch(r"temp\d+_input", n) or re.fullmatch(r"pwm\d+", n)
                   for n in entries):
            continue
        out.append((chip_prefix(raw), d))
    return out


def find_chips(root=HWMON_ROOT) -> dict[str, Path]:
    """{name_prefix: first dir in sorted order}. Use find_all_chips() when a
    prefix can occur more than once (e.g. k10temp x2)."""
    chips: dict[str, Path] = {}
    for prefix, d in find_all_chips(root):
        chips.setdefault(prefix, d)
    return chips


def chip_dir_for(prefix: str, index: int = 0, chips=None,
                 root=HWMON_ROOT) -> Path | None:
    """Dir of the `index`-th chip (sorted glob order) with name prefix
    `prefix`, or None."""
    if chips is None:
        chips = find_all_chips(root)
    matches = [d for p, d in chips if p == prefix]
    if index < 0 or index >= len(matches):
        return None
    return matches[index]


# ---------------------------------------------------------------- reads ---

def _read_int(path: Path) -> int | None:
    try:
        raw = path.read_text().strip()
        return int(raw) if raw else None
    except (OSError, ValueError):
        return None


def read_temp_c(chip_dir, idx: int) -> float | None:
    """tempN_input is millidegree-C -> /1000.0; None if missing/empty."""
    try:
        raw = Path(chip_dir) / f"temp{idx}_input"
        text = raw.read_text().strip()
        return float(text) / 1000.0 if text else None
    except (OSError, ValueError):
        return None


def read_rpm(chip_dir, fan_idx: int) -> int | None:
    return _read_int(Path(chip_dir) / f"fan{fan_idx}_input")


def read_duty_raw(chip_dir, pwm_idx: int) -> int | None:
    """pwmN, 0-255."""
    return _read_int(Path(chip_dir) / f"pwm{pwm_idx}")


def get_enable(chip_dir, pwm_idx: int) -> int | None:
    """pwmN_enable: 0/1/2."""
    return _read_int(Path(chip_dir) / f"pwm{pwm_idx}_enable")


# -------------------------------------------------------------- writes ----

def set_enable(chip_dir, pwm_idx: int, val: int) -> None:
    (Path(chip_dir) / f"pwm{pwm_idx}_enable").write_text(f"{int(val)}\n")


def write_duty(chip_dir, pwm_idx: int, raw255: int) -> None:
    raw255 = max(0, min(255, int(raw255)))
    (Path(chip_dir) / f"pwm{pwm_idx}").write_text(f"{raw255}\n")


# ------------------------------------------------- fan/tach id helpers ----

def parse_fan_id(fan_id: str) -> tuple[str, int] | None:
    m = FAN_ID_RE.match(fan_id)
    return (m.group(1), int(m.group(2))) if m else None


def parse_tach_id(tach_id: str) -> tuple[str, int] | None:
    m = TACH_ID_RE.match(tach_id)
    return (m.group(1), int(m.group(2))) if m else None


def resolve_fan(fan_id: str, chips=None, root=HWMON_ROOT) -> tuple[Path, int] | None:
    """'it8686:pwm3' -> (chip_dir, 3) or None (bad id or chip absent)."""
    parsed = parse_fan_id(fan_id)
    if parsed is None:
        return None
    prefix, idx = parsed
    d = chip_dir_for(prefix, 0, chips, root)
    return (d, idx) if d is not None else None


def resolve_tach(tach_id: str, chips=None, root=HWMON_ROOT) -> tuple[Path, int] | None:
    parsed = parse_tach_id(tach_id)
    if parsed is None:
        return None
    prefix, idx = parsed
    d = chip_dir_for(prefix, 0, chips, root)
    return (d, idx) if d is not None else None


def list_pwm_channels(chips=None, root=HWMON_ROOT) -> list[tuple[str, Path, int]]:
    """All ('prefix:pwmN', chip_dir, N) channels that expose pwmN_enable,
    sorted. Used by the TUI to enumerate assignable fans."""
    out: list[tuple[str, Path, int]] = []
    for prefix, d in (chips if chips is not None else find_all_chips(root)):
        try:
            entries = [p.name for p in d.iterdir()]
        except OSError:
            continue
        idxs = sorted({int(m.group(1)) for n in entries
                       if (m := re.fullmatch(r"pwm(\d+)_enable", n))})
        for i in idxs:
            out.append((f"{prefix}:pwm{i}", d, i))
    return out


# ---------------------------------------------------------- nvidia-smi ----

def norm_bus(busid: str) -> str:
    # "00000000:44:00.0" -> "44:00.0"
    parts = busid.strip().split(":")
    return ":".join(parts[-2:])


def _nvidia_smi_csv(*fields: str) -> str:
    out = subprocess.run(
        ["nvidia-smi", "--query-gpu=" + ",".join(fields),
         "--format=csv,noheader,nounits"],
        capture_output=True, text=True, check=True).stdout
    return out


def gpu_temps(runner=None) -> dict[str, int]:
    """{normalized pci bus: temp_c}, e.g. {"08:00.0": 68, "44:00.0": 69}.

    Port of gpu-fanctl's gpu_temps(); `runner` (a stand-in for the
    nvidia-smi subprocess returning the CSV string) is injectable for
    tests. Raises (subprocess.CalledProcessError) if nvidia-smi fails —
    callers mark GPU sources stale and hold duties.
    """
    out = _nvidia_smi_csv("pci.bus_id", "temperature.gpu") if runner is None \
        else runner("pci.bus_id", "temperature.gpu")
    temps: dict[str, int] = {}
    for line in out.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        bus, t = line.split(",")
        temps[norm_bus(bus)] = int(t.strip())
    return temps


def gpu_info(runner=None) -> dict[str, dict]:
    """{normalized pci bus: {"temp": int, "name": str}} — TUI only (shows
    e.g. 'Tesla V100' on GPU source rows)."""
    out = _nvidia_smi_csv("pci.bus_id", "temperature.gpu", "name") if runner is None \
        else runner("pci.bus_id", "temperature.gpu", "name")
    info: dict[str, dict] = {}
    for line in out.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 3:
            continue
        info[norm_bus(parts[0])] = {
            "temp": int(parts[1]),
            "name": ",".join(parts[2:]),
        }
    return info


# --------------------------------------------------------- sensors.d ------

def load_sensorsd_labels(conf_dir=SENSORS_D_DIR) -> dict[str, dict[str, str]]:
    """Parse /etc/sensors.d/*.conf: chip "prefix-*" blocks + label
    tempN/fanN/pwmN "Name". Returns {chip_prefix: {"temp1": "System 1",
    "pwm3": "SYS_FAN2", ...}}; {} if the dir is absent (read-only use)."""
    labels: dict[str, dict[str, str]] = {}
    try:
        files = sorted(Path(conf_dir).glob("*.conf"))
    except OSError:
        return labels
    chip = None
    for f in files:
        try:
            text = f.read_text()
        except OSError:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            m = _CHIP_RE.match(line)
            if m:
                chip = chip_prefix(m.group(1))
                labels.setdefault(chip, {})
                continue
            m = _LABEL_RE.match(line)
            if m and chip is not None and m.group(1) in ("temp", "fan", "pwm"):
                labels[chip][f"{m.group(1)}{m.group(2)}"] = m.group(3)
    return labels


def sensor_label(chip: str, kind: str, idx: int,
                 labels: dict | None = None) -> str:
    """Human label for chip temp/fan/pwm idx; falls back to the generic
    'temp1 (k10temp)' style when sensors.d has no label."""
    if labels:
        lab = labels.get(chip, {}).get(f"{kind}{idx}")
        if lab:
            return lab
    return f"{kind}{idx} ({chip})"
