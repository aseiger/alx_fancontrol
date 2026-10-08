"""Shared pytest fixtures: the synthetic machine.

`machine` builds a default fake machine (mirroring the dev box's shape,
but with NO temps/RPMs — tests inject the values they need) and points
the app's hwmon layer at it. See fake_machine.py for the API:

    machine.set_temp("k10temp", 1, 61.5)     # inject a reading
    machine.set_rpm("it8686", 3, 4680)
    machine.set_pwm("it8686", 3, duty=128, enable=1)
    machine.add_gpu("44:00.0", 69.0, "Tesla V100")
    machine.source_ids() / machine.fan_ids() # what the app will detect
    machine.live_payload()                   # a LiveUpdate dict of current values
"""
import pytest

from fake_machine import FakeMachine


def default_machine(root) -> FakeMachine:
    """Shape mirrors the dev box: one k10temp package, an it8686 with five
    pwm channels (pwm3/4 firmware-disabled, as found in the wild), an
    it8792 with three pwm channels (manual — gpu-fanctl-owned), one GPU.
    No temp/RPM values are set: tests inject what they need."""
    m = FakeMachine(root)
    m.add_chip("k10temp", temp1=55.0)
    m.add_chip("it8686_2008090d",
               temp1=46.0, fan1=1650, fan3=4680, fan4=4890,
               pwm1=(70, 2), pwm2=(0, 2),
               pwm3=(255, 0), pwm4=(255, 0),
               pwm5=(0, 2))
    m.add_chip("it8792_2008090d",
               temp1=41.0, fan1=250, fan3=3000,
               pwm1=(140, 1), pwm2=(0, 2), pwm3=(140, 1))
    m.add_gpu("08:00.0", 60.0, "Tesla V100")
    m.labels.update({
        "it8686": {"temp1": "System 1",
                   "pwm1": "CPU_FAN", "fan1": "CPU_FAN",
                   "pwm2": "SYS_FAN1", "fan2": "SYS_FAN1",
                   "pwm3": "SYS_FAN2", "fan3": "SYS_FAN2",
                   "pwm4": "SYS_FAN3", "fan4": "SYS_FAN3",
                   "pwm5": "CPU_OPT", "fan5": "CPU_OPT"},
        "it8792": {"pwm1": "SYS_FAN5_PUMP", "fan1": "SYS_FAN5_PUMP",
                   "pwm2": "SYS_FAN6_PUMP", "fan2": "SYS_FAN6_PUMP",
                   "pwm3": "SYS_FAN4", "fan3": "SYS_FAN4"},
    })
    return m


@pytest.fixture
def machine(tmp_path, monkeypatch):
    m = default_machine(tmp_path / "hwmon")
    m.apply(monkeypatch)
    return m
