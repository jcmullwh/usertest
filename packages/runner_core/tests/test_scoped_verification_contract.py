from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from runner_core.runner import (
    _normalize_verification_summary,
    _resolve_effective_verification_categories,
    _run_verification_commands,
)


def test_resolve_effective_verification_categories_heuristic() -> None:
    commands = ["smoke", "install", "lint", "test"]
    resolved = _resolve_effective_verification_categories(commands, [])
    assert resolved == ["repo_health", "prerequisite", "repo_health", "change_validation"]


def test_resolve_effective_verification_categories_padding() -> None:
    commands = ["cmd1", "cmd2"]
    resolved = _resolve_effective_verification_categories(commands, [])
    assert resolved == ["change_validation", "change_validation"]


def test_resolve_effective_verification_categories_explicit() -> None:
    commands = ["cmd1", "cmd2"]
    resolved = _resolve_effective_verification_categories(commands, ["repo_health", "prerequisite"])
    assert resolved == ["repo_health", "prerequisite"]


def test_resolve_effective_verification_categories_explicit_padding() -> None:
    commands = ["cmd1", "cmd2", "cmd3"]
    resolved = _resolve_effective_verification_categories(commands, ["repo_health"])
    assert resolved == ["repo_health", "change_validation", "change_validation"]


def test_run_verification_commands_categorizes_results(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    commands = ["echo smoke", "echo install", "echo lint", "echo test"]
    categories = ["repo_health", "prerequisite", "repo_health", "change_validation"]

    summary = _run_verification_commands(
        run_dir=run_dir,
        attempt_number=1,
        commands=commands,
        categories=categories,
        command_prefix=[],
        cwd=tmp_path,
        timeout_seconds=None,
        python_executable=sys.executable,
    )

    assert summary["passed"] is True
    assert summary["change_validation_passed"] is True
    assert len(summary["commands"]) == 4
    for idx, cmd_res in enumerate(summary["commands"]):
        assert cmd_res["category"] == categories[idx]


def test_run_verification_commands_non_blocking_failure(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    # repo_health fails, but loop continues and final passed is True
    # but change_validation_passed is True
    commands = ["exit 1", "echo ok"]
    categories = ["repo_health", "change_validation"]

    summary = _run_verification_commands(
        run_dir=run_dir,
        attempt_number=1,
        commands=commands,
        categories=categories,
        command_prefix=[],
        cwd=tmp_path,
        timeout_seconds=None,
        python_executable=sys.executable,
    )

    assert summary["passed"] is True
    assert summary["all_passed"] is False
    assert summary["change_validation_passed"] is True
    assert len(summary["commands"]) == 2
    assert summary["commands"][0]["exit_code"] == 1
    assert summary["commands"][1]["exit_code"] == 0


def test_run_verification_commands_blocking_failure_breaks_loop(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    # prerequisite fails, loop breaks
    commands = ["exit 1", "echo should-not-run"]
    categories = ["prerequisite", "change_validation"]

    summary = _run_verification_commands(
        run_dir=run_dir,
        attempt_number=1,
        commands=commands,
        categories=categories,
        command_prefix=[],
        cwd=tmp_path,
        timeout_seconds=None,
        python_executable=sys.executable,
    )

    assert summary["passed"] is False
    assert summary["change_validation_passed"] is False
    assert len(summary["commands"]) == 1
    assert summary["commands"][0]["exit_code"] == 1


def test_normalize_verification_summary_backwards_compat() -> None:
    summary: dict[str, Any] = {
        "passed": True,
        "terminal_reason": "passed",
        "commands": [],
    }
    normalized = _normalize_verification_summary(summary)
    assert normalized["change_validation_passed"] is True

    summary_fail: dict[str, Any] = {
        "passed": False,
        "terminal_reason": "failed",
        "commands": [],
    }
    normalized_fail = _normalize_verification_summary(summary_fail)
    assert normalized_fail["change_validation_passed"] is False
