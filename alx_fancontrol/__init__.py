"""alx_fancontrol — TUI-first fan control daemon + TUI.

Pure-stdlib daemon (hwmon + nvidia-smi sources → piecewise-linear curves →
rate-limited PWM writes with firmware-restore-on-exit). The TUI (textual,
project venv) is read-only against hwmon and is the primary UX.
"""

__version__ = "0.1.0"
