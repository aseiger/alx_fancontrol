"""alx-fancontrol CLI: daemon | tui | check (no command = tui).

The daemon is pure stdlib and runs under system python3 (no venv needed);
the TUI needs the project venv (scripts/bootstrap_venv.sh).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from alx_fancontrol import __version__, config as config_mod, hwmon


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="alx-fancontrol",
        description="TUI-first fan control: hwmon + nvidia-smi sources, "
                    "piecewise-linear curves, rate-limited PWM writes. "
                    "With no subcommand, launches the TUI.")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="command", required=False)

    d = sub.add_parser("daemon", help="run the fan control daemon")
    d.add_argument("--config", default=None,
                   help="config path (default: $ALX_FANCONTROL_CONFIG or "
                        "~/.config/alx_fancontrol/config.json)")
    d.add_argument("--dry-run", action="store_true",
                   help="no enable/duty writes at all; log intended actions")
    d.add_argument("--once", action="store_true",
                   help="run a single poll then exit (restore still runs)")
    d.add_argument("--duration", type=float, default=None, metavar="SECS",
                   help="exit after this many seconds (restore still runs)")
    d.add_argument("-v", "--verbose", action="store_true", help="DEBUG logging")

    t = sub.add_parser("tui", help="launch the TUI (needs the project venv)")
    t.add_argument("--config", default=None, help="config path (default: as above)")

    c = sub.add_parser("check", help="load + validate the config, print a summary")
    c.add_argument("--config", default=None, help="config path (default: as above)")
    return p


def _resolve_config_path(args) -> Path:
    return Path(args.config).expanduser() if getattr(args, "config", None) \
        else config_mod.default_path()


def _check(args) -> int:
    path = _resolve_config_path(args)
    seeded = False
    try:
        if not path.exists():
            config_mod.seed_if_missing(path)
            seeded = True
        cfg = config_mod.load(path)
    except (OSError, ValueError) as e:
        print(f"ERROR: cannot load {path}: {e}", file=sys.stderr)
        return 1
    errors, warnings = config_mod.validate(cfg)

    print(f"config: {path}" + ("  (seeded on first run)" if seeded else ""))
    c = cfg.config
    print(f"  poll_seconds={c.get('poll_seconds')}  max_rate={c.get('max_rate')}% "
          f"floor_duty={c.get('floor_duty')}%  dead_temp_c={c.get('dead_temp_c')}")
    print(f"protected: {', '.join(cfg.protected) or '(none!)'}")

    # sources are discovered at runtime (not stored in the config)
    from alx_fancontrol import sources as sources_mod
    srcs = sources_mod.discover()
    gtemps = None
    print(f"sources ({len(srcs)} — detected at runtime, not in config):")
    for s in srcs:
        if s.kind == "gpu":
            if gtemps is None:
                try:
                    gtemps = hwmon.gpu_temps()
                except Exception:
                    gtemps = {}
            t = gtemps.get(s.pci_bus)
        else:
            d = hwmon.chip_dir_for(s.chip, s.chip_index)
            t = hwmon.read_temp_c(d, s.temp) if d is not None else None
        state = "no reading" if t is None else f"{t:.1f} C"
        print(f"  {s.id:<20} {s.label:<24} {s.detail:<16} {state}")

    print(f"curves ({len(cfg.curves)}):")
    for cid, crv in sorted(cfg.curves.items()):
        pts = crv.get("points", [])
        print(f"  {cid:<10} {crv.get('label', cid):<16} {len(pts)} points: "
              + " -> ".join(f"{t:.0f}C={d:.0f}%" for t, d in pts))

    print(f"assignments ({len(cfg.assignments)}):")
    if not cfg.assignments:
        print("  (none — the daemon controls nothing until you assign in the TUI)")
    for aid, a in sorted(cfg.assignments.items()):
        on = "" if a.get("enabled", True) else "  [disabled]"
        print(f"  {aid:<12} source={a.get('source')} curve={a.get('curve')} "
              f"fans={', '.join(a.get('fans', []))}{on}")

    for w in warnings:
        print(f"warning: {w}")
    for e in errors:
        print(f"ERROR: {e}", file=sys.stderr)
    if errors:
        print(f"\n{len(errors)} hard error(s) — daemon will refuse to start.",
              file=sys.stderr)
        return 1
    print("\nOK" if not warnings else f"\nOK ({len(warnings)} warning(s))")
    return 0


def _tui(args) -> int:
    try:
        from alx_fancontrol.tui.app import FanControlApp
    except ModuleNotFoundError:
        print("TUI needs the project venv: run scripts/bootstrap_venv.sh")
        return 1
    path = _resolve_config_path(args)
    if not path.exists():
        config_mod.seed_if_missing(path)
    FanControlApp(config_path=path).run()
    return 0


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.command is None:
        args.command = "tui"   # bare `alx-fancontrol` opens the TUI
    if args.command == "daemon":
        from alx_fancontrol.daemon import main as daemon_main
        return daemon_main(args)
    if args.command == "tui":
        return _tui(args)
    if args.command == "check":
        return _check(args)
    return 2  # pragma: no cover


if __name__ == "__main__":
    sys.exit(main())
