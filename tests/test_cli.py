"""CLI dispatch: bare `alx-fancontrol` (no subcommand) launches the TUI."""
from __future__ import annotations

import pytest

from alx_fancontrol import cli


def test_no_command_defaults_to_tui(monkeypatch):
    seen = []
    monkeypatch.setattr(cli, "_tui", lambda args: seen.append(args) or 0)
    assert cli.main([]) == 0
    assert len(seen) == 1
    assert seen[0].command == "tui"


def test_explicit_subcommands_still_dispatch(monkeypatch):
    seen = []
    monkeypatch.setattr(cli, "_tui", lambda args: seen.append("tui") or 0)
    monkeypatch.setattr(cli, "_check", lambda args: seen.append("check") or 0)
    import alx_fancontrol.daemon as daemon_mod
    monkeypatch.setattr(daemon_mod, "main",
                        lambda args: seen.append("daemon") or 0)
    assert cli.main(["tui"]) == 0
    assert cli.main(["check"]) == 0
    assert cli.main(["daemon"]) == 0
    assert seen == ["tui", "check", "daemon"]


def test_version_exits_cleanly():
    with pytest.raises(SystemExit) as e:
        cli.build_parser().parse_args(["--version"])
    assert e.value.code == 0
