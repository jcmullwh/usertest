from __future__ import annotations

import json
import subprocess

import pytest

import runner_core.python_interpreter_probe as probe_mod


def test_probe_rejects_windowsapps_alias(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        probe_mod.shutil,
        "which",
        lambda _cmd: r"C:\Users\tester\AppData\Local\Microsoft\WindowsApps\python.exe",
    )

    result = probe_mod.probe_python_interpreters(
        candidate_commands=["python"],
        force_windows=True,
    )

    candidate = result.by_command()["python"]
    assert candidate.present is True
    assert candidate.usable is False
    assert candidate.reason_code == "windowsapps_alias"
    assert result.selected_command is None


def test_probe_rejects_incomplete_runtime_missing_encodings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(probe_mod.shutil, "which", lambda _cmd: r"C:\Python313\python.exe")

    def _run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=["python", "-c", "..."],
            returncode=1,
            stdout="",
            stderr=(
                "Fatal Python error: init_fs_encoding\n"
                "ModuleNotFoundError: No module named 'encodings'"
            ),
        )

    monkeypatch.setattr(probe_mod.subprocess, "run", _run)

    result = probe_mod.probe_python_interpreters(
        candidate_commands=["python"],
        force_windows=True,
    )
    candidate = result.by_command()["python"]
    assert candidate.present is True
    assert candidate.usable is False
    assert candidate.reason_code == "missing_stdlib"
    assert "encodings" in (candidate.reason or "")


def test_probe_records_launch_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(probe_mod.shutil, "which", lambda _cmd: r"C:\Python313\python.exe")

    def _run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise OSError("Access is denied")

    monkeypatch.setattr(probe_mod.subprocess, "run", _run)

    result = probe_mod.probe_python_interpreters(
        candidate_commands=["python"],
        force_windows=True,
    )
    candidate = result.by_command()["python"]
    assert candidate.present is True
    assert candidate.usable is False
    assert candidate.reason_code == "launch_failed"
    assert "Access is denied" in (candidate.reason or "")


def test_probe_selects_verified_fallback_candidate(monkeypatch: pytest.MonkeyPatch) -> None:
    def _which(command: str) -> str | None:
        if command == "python":
            return r"C:\Users\tester\AppData\Local\Microsoft\WindowsApps\python.exe"
        if command == "py":
            return r"C:\Python313\py.exe"
        return None

    def _run(
        args: list[str],
        *,
        capture_output: bool,
        text: bool,
        encoding: str,
        errors: str,
        timeout: float,
        check: bool,
    ) -> subprocess.CompletedProcess[str]:
        assert args[0] == r"C:\Python313\py.exe"
        payload = json.dumps({"executable": r"C:\Python313\python.exe", "version": "3.13.2"})
        return subprocess.CompletedProcess(args=args, returncode=0, stdout=payload + "\n", stderr="")

    monkeypatch.setattr(probe_mod.shutil, "which", _which)
    monkeypatch.setattr(probe_mod.subprocess, "run", _run)

    result = probe_mod.probe_python_interpreters(
        candidate_commands=["python", "py"],
        force_windows=True,
    )

    by_command = result.by_command()
    assert by_command["python"].reason_code == "windowsapps_alias"
    assert by_command["py"].usable is True
    assert result.selected_command == "py"
    assert result.selected_resolved_path == r"C:\Python313\py.exe"
