from __future__ import annotations

from pathlib import Path

import pytest

import runner_core.runner as runner_mod


def test_verification_rewrites_bash_smoke_to_powershell_when_bash_blocked_on_windows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runner_mod.os, "name", "nt")
    monkeypatch.setattr(
        runner_mod,
        "_probe_windows_bash_usable",
        lambda: {
            "present": True,
            "usable": False,
            "resolved_path": r"C:\blocked\bash.exe",
            "reason_code": "blocked",
            "reason": "Access is denied",
        },
    )

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
        commands=["bash ./scripts/smoke.sh --skip-install --use-pythonpath --require-doctor"],
        command_prefix=[],
        cwd=tmp_path,
        timeout_seconds=None,
        python_executable=None,
    )

    assert summary["passed"] is True
    commands = summary["commands"]
    assert isinstance(commands, list)
    assert len(commands) == 1
    cmd0 = commands[0]
    assert cmd0["command"].startswith("bash ")
    assert "smoke.ps1" in cmd0["effective_command"]
    assert "-SkipInstall" in cmd0["effective_command"]
    assert "-UsePythonPath" in cmd0["effective_command"]
    assert "-RequireDoctor" in cmd0["effective_command"]
    assert isinstance(cmd0.get("rewrite"), dict)
    assert cmd0["rewrite"]["kind"] == "bash_smoke_to_powershell_smoke"

    assert len(calls) == 1
    assert calls[0][:4] == ["powershell", "-NoProfile", "-NonInteractive", "-Command"]
    expected = (
        "powershell -NoProfile -ExecutionPolicy Bypass -File "
        ".\\scripts\\smoke.ps1"
    )
    assert expected in calls[0][4]


def test_verification_skips_bash_syntax_check_when_bash_blocked_on_windows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runner_mod.os, "name", "nt")
    monkeypatch.setattr(
        runner_mod,
        "_probe_windows_bash_usable",
        lambda: {
            "present": True,
            "usable": False,
            "resolved_path": r"C:\blocked\bash.exe",
            "reason_code": "blocked",
            "reason": "Access is denied",
        },
    )

    def _boom(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("subprocess.run should not be invoked for skipped checks")

    monkeypatch.setattr(runner_mod.subprocess, "run", _boom)

    summary = runner_mod._run_verification_commands(
        run_dir=tmp_path / "run",
        attempt_number=1,
        commands=["bash -n scripts/smoke.sh"],
        command_prefix=[],
        cwd=tmp_path,
        timeout_seconds=None,
        python_executable=None,
    )

    assert summary["passed"] is True
    commands = summary["commands"]
    assert isinstance(commands, list)
    assert len(commands) == 1
    cmd0 = commands[0]
    assert cmd0.get("skipped") is True
    assert cmd0.get("effective_command") is None
    assert cmd0.get("argv") is None
    assert "Skipping" in (cmd0.get("stderr_tail") or "")
