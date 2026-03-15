from __future__ import annotations

from pathlib import Path

import pytest

import runner_core.runner as runner_mod
from runner_core.shell_command_normalization import (
    normalize_command_for_shell,
    render_shell_command_guidance_md,
)


def test_render_shell_command_guidance_mentions_shared_windows_rewrites() -> None:
    guidance = render_shell_command_guidance_md(shell_family="powershell")

    assert "bash -lc" in guidance
    assert "Get-Content -LiteralPath" in guidance
    assert 'rg -n -- "--skip-install" README.md' in guidance
    assert "portability issue" in guidance


def test_render_shell_command_guidance_mentions_bash_passthrough() -> None:
    guidance = render_shell_command_guidance_md(shell_family="bash")

    assert "bash/sh" in guidance
    assert "avoid redundant `bash -lc` wrappers" in guidance
    assert "Ripgrep" in guidance


def test_normalize_command_for_shell_unwraps_shell_neutral_bash_wrapper_for_powershell() -> None:
    decision = normalize_command_for_shell(
        'bash -lc "python -m pytest -q"',
        shell_family="powershell",
    )

    assert decision.action == "rewrite"
    assert decision.command == "python -m pytest -q"
    assert decision.kind == "bash_wrapper_unwrapped_for_host_shell"


def test_normalize_command_for_shell_rewrites_unix_line_inspection_for_powershell() -> None:
    decision = normalize_command_for_shell(
        "nl -ba README.md | sed -n '5,12p'",
        shell_family="powershell",
    )

    assert decision.action == "rewrite"
    assert "Get-Content -LiteralPath 'README.md'" in decision.command
    assert "$i -ge 5 -and $i -le 12" in decision.command
    assert decision.kind == "unix_line_inspection_to_powershell"


def test_normalize_command_for_shell_blocks_unsupported_bash_wrapper_for_powershell() -> None:
    decision = normalize_command_for_shell(
        'bash -lc "python -m pytest && pytest -q"',
        shell_family="powershell",
    )

    assert decision.action == "blocked"
    assert decision.kind == "powershell_unsupported_bash_wrapper"
    assert "not safely portable" in (decision.reason or "")


def test_run_verification_commands_unwraps_bash_lc_before_dispatching_on_windows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runner_mod, "_is_windows", lambda: True)

    calls: list[list[str]] = []

    class _Proc:
        def __init__(self, argv: list[str]) -> None:
            self.returncode = 0
            self.stdout = "ok\n"
            self.stderr = ""
            self.argv = argv

    def _fake_run(argv: list[str], **_kwargs: object) -> _Proc:
        calls.append(list(argv))
        return _Proc(list(argv))

    monkeypatch.setattr(runner_mod.subprocess, "run", _fake_run)

    summary = runner_mod._run_verification_commands(
        run_dir=tmp_path / "run",
        attempt_number=1,
        commands=['bash -lc "python -m pytest -q"'],
        command_prefix=[],
        cwd=tmp_path,
        timeout_seconds=None,
        python_executable=None,
    )

    assert summary["passed"] is True
    assert len(calls) == 1
    assert calls[0][:4] == ["powershell", "-NoProfile", "-NonInteractive", "-Command"]
    assert calls[0][4] == "python -m pytest -q"

    command_result = summary["commands"][0]
    assert command_result["effective_command"] == "python -m pytest -q"
    assert command_result["rewritten"] is True
    assert isinstance(command_result.get("rewrite"), dict)
    assert command_result["rewrite"]["kind"] == "bash_wrapper_unwrapped_for_host_shell"


def test_run_verification_commands_rewrites_line_inspection_before_dispatching_on_windows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runner_mod, "_is_windows", lambda: True)

    calls: list[list[str]] = []

    class _Proc:
        def __init__(self, argv: list[str]) -> None:
            self.returncode = 0
            self.stdout = "ok\n"
            self.stderr = ""
            self.argv = argv

    def _fake_run(argv: list[str], **_kwargs: object) -> _Proc:
        calls.append(list(argv))
        return _Proc(list(argv))

    monkeypatch.setattr(runner_mod.subprocess, "run", _fake_run)

    summary = runner_mod._run_verification_commands(
        run_dir=tmp_path / "run",
        attempt_number=1,
        commands=["nl -ba README.md | sed -n '2,4p'"],
        command_prefix=[],
        cwd=tmp_path,
        timeout_seconds=None,
        python_executable=None,
    )

    assert summary["passed"] is True
    assert len(calls) == 1
    assert calls[0][:4] == ["powershell", "-NoProfile", "-NonInteractive", "-Command"]
    assert "Get-Content -LiteralPath 'README.md'" in calls[0][4]
    assert "$i -ge 2 -and $i -le 4" in calls[0][4]

    command_result = summary["commands"][0]
    assert command_result["rewritten"] is True
    assert command_result["rewrite"]["kind"] == "unix_line_inspection_to_powershell"


def test_run_verification_commands_blocks_unsupported_bash_wrapper_on_windows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runner_mod, "_is_windows", lambda: True)

    def _unexpected_run(*_args: object, **_kwargs: object) -> object:  # pragma: no cover
        raise AssertionError("subprocess.run should not be called for blocked portability cases")

    monkeypatch.setattr(runner_mod.subprocess, "run", _unexpected_run)

    summary = runner_mod._run_verification_commands(
        run_dir=tmp_path / "run",
        attempt_number=1,
        commands=['bash -lc "python -m pytest && pytest -q"'],
        command_prefix=[],
        cwd=tmp_path,
        timeout_seconds=None,
        python_executable=None,
    )

    assert summary["passed"] is False
    command_result = summary["commands"][0]
    assert command_result["exit_code"] == 126
    assert command_result["dispatch_blocked"] is True
    assert command_result["dispatch_validation"]["kind"] == "powershell_unsupported_bash_wrapper"
    assert "not safely portable" in (command_result["stderr_tail"] or "")
