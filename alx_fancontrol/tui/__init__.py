"""TUI package: textual-based screens (overview / assign / curves / sources).

The TUI is READ-ONLY against hwmon (it never writes pwm/enable files), so
it is safe to run while the daemon is live. All config writes go through
app.save_config() (atomic + .bak + stale-write guard).
"""
