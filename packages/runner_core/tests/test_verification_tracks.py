from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

import runner_core.runner as runner_mod
from runner_core.verification_plan import VerificationCommandSpec, VerificationTrack


def test_run_verification_commands_repo_health_failure_does_not_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []

    class _Proc:
        def __init__(self, argv: list[str], returncode: int) -> None:
            self.returncode = returncode
            self.stdout = "ok\n" if returncode == 0 else "fail\n"
            self.stderr = ""
            self.args = list(argv)
        
        def poll(self): return self.returncode
        def wait(self): return self.returncode

    def _fake_run(argv: list[str], **kwargs: Any) -> _Proc:
        calls.append(list(argv))
        # Fail the second command (repo_health)
        if "fail-me" in " ".join(argv):
            return _Proc(argv, returncode=1)
        return _Proc(argv, returncode=0)

    monkeypatch.setattr(runner_mod.subprocess, "run", _fake_run)

    summary = runner_mod._run_verification_commands(
        run_dir=tmp_path / "run",
        attempt_number=1,
        commands=[
            VerificationCommandSpec(command="echo scoped-pass", track=VerificationTrack.CHANGE_VALIDATION),
            VerificationCommandSpec(command="echo fail-me-repo-health", track=VerificationTrack.REPO_HEALTH),
            VerificationCommandSpec(command="echo repo-health-2", track=VerificationTrack.REPO_HEALTH),
        ],
        command_prefix=[],
        cwd=tmp_path,
        timeout_seconds=None,
        python_executable=None,
    )

    # passed should be True because only repo_health failed
    assert summary["passed"] is True
    # Should run all commands because repo_health failure does not break
    assert len(summary["commands"]) == 3
    assert summary["commands"][0]["exit_code"] == 0
    assert summary["commands"][1]["exit_code"] == 1
    assert summary["commands"][2]["exit_code"] == 0
    assert summary["commands"][1]["track"] == VerificationTrack.REPO_HEALTH


def test_run_verification_commands_change_validation_failure_blocks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _fake_run(argv: list[str], **kwargs: Any):
        class _Proc:
            def __init__(self, returncode: int):
                self.returncode = returncode
                self.stdout = ""
                self.stderr = ""
        if "fail-me" in " ".join(argv):
            return _Proc(1)
        return _Proc(0)

    monkeypatch.setattr(runner_mod.subprocess, "run", _fake_run)

    summary = runner_mod._run_verification_commands(
        run_dir=tmp_path / "run",
        attempt_number=1,
        commands=[
            VerificationCommandSpec(command="echo fail-me-scoped", track=VerificationTrack.CHANGE_VALIDATION),
            VerificationCommandSpec(command="echo repo-health", track=VerificationTrack.REPO_HEALTH),
        ],
        command_prefix=[],
        cwd=tmp_path,
        timeout_seconds=None,
        python_executable=None,
    )

    # passed should be False because change_validation failed
    assert summary["passed"] is False
    # Should stop after first failure
    assert len(summary["commands"]) == 1
    assert summary["commands"][0]["exit_code"] == 1


def test_run_verification_commands_bootstrap_failure_blocks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _fake_run(argv: list[str], **kwargs: Any):
        class _Proc:
            def __init__(self, returncode: int):
                self.returncode = returncode
                self.stdout = ""
                self.stderr = ""
        if "fail-me" in " ".join(argv):
            return _Proc(1)
        return _Proc(0)

    monkeypatch.setattr(runner_mod.subprocess, "run", _fake_run)

    summary = runner_mod._run_verification_commands(
        run_dir=tmp_path / "run",
        attempt_number=1,
        commands=[
            VerificationCommandSpec(command="echo fail-me-bootstrap", track=VerificationTrack.BOOTSTRAP),
            VerificationCommandSpec(command="echo scoped", track=VerificationTrack.CHANGE_VALIDATION),
        ],
        command_prefix=[],
        cwd=tmp_path,
        timeout_seconds=None,
        python_executable=None,
    )

    # passed should be False because bootstrap failed
    assert summary["passed"] is False
    assert len(summary["commands"]) == 1
    assert summary["commands"][0]["exit_code"] == 1
