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
    assert args.base_branch == "dev"
    assert args.exec_backend == "docker"
    assert args.exec_keep_container is True
    assert args.move_on_start is True
    assert args.move_on_commit is True
    assert args.draft_pr_on_ci_failure is True


def test_parser_base_branch_override() -> None:
    parser = build_parser()
    args = parser.parse_args(
        ["run", "--ticket-path", "C:\\tmp\\ticket.md", "--base-branch", "main", "--dry-run"]
    )
    assert args.base_branch == "main"


def test_parser_no_docker_overrides_default() -> None:
    parser = build_parser()
    args = parser.parse_args(
        ["run", "--ticket-path", "C:\\tmp\\ticket.md", "--dry-run", "--no-docker"]
    )
    assert args.exec_backend == "local"


def test_parser_opt_out_flags() -> None:
    parser = build_parser()
    args = parser.parse_args(
        [
            "run",
            "--ticket-path",
            "C:\\tmp\\ticket.md",
            "--dry-run",
            "--no-exec-keep-container",
            "--no-move-on-start",
            "--no-move-on-commit",
            "--no-draft-pr-on-ci-failure",
        ]
    )
    assert args.exec_keep_container is False
    assert args.move_on_start is False
    assert args.move_on_commit is False
    assert args.draft_pr_on_ci_failure is False


def test_help_smoke() -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "usertest_implement.cli", "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0
    assert "usertest-implement" in proc.stdout
