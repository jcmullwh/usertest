from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from usertest_implement.cli import build_parser


def test_parser_smoke() -> None:
    parser = build_parser()
    args = parser.parse_args(["run", "--ticket-path", "C:\\tmp\\ticket.md", "--dry-run"])
    assert args.ticket_path == Path("C:\\tmp\\ticket.md")
    assert args.dry_run is True


def test_help_smoke() -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "usertest_implement.cli", "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0
    assert "usertest-implement" in proc.stdout

