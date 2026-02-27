from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest


def _load_probe_module() -> ModuleType:
    module_path = (
        Path(__file__).resolve().parents[1] / "src" / "runner_core" / "python_interpreter_probe.py"
    )
    spec = importlib.util.spec_from_file_location("runner_core_python_probe_test", module_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


probe_mod = _load_probe_module()


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


def test_probe_classifies_inaccessible_file_as_access_denied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(probe_mod.shutil, "which", lambda _cmd: r"C:\Python313\python.exe")

    def _run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=["python", "-c", "..."],
            returncode=1,
            stdout="",
            stderr=(
                "Unable to create process using 'C:\\Python313\\python.exe -V': "
                "The file cannot be accessed by the system."
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
    assert candidate.reason_code == "access_denied"
    assert "cannot be accessed by the system" in (candidate.reason or "").lower()


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
        payload = json.dumps(
            {
                "executable": r"C:\Python313\python.exe",
                "version": "3.13.2",
            }
        )
        return subprocess.CompletedProcess(
            args=args,
            returncode=0,
            stdout=payload + "\n",
            stderr="",
        )

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


def test_resolve_can_select_py0p_interpreter_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    if sys.platform.startswith("win"):
        py0p_path = str(tmp_path / "py0p_python.exe")
        Path(py0p_path).write_text("", encoding="utf-8")
    else:
        py0p_path = r"C:\Fake\python.exe"
        (tmp_path / py0p_path).write_text("", encoding="utf-8")

    def _which(command: str) -> str | None:
        if command == "python":
            return r"C:\Users\tester\AppData\Local\Microsoft\WindowsApps\python.exe"
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
        **_kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        if args[:2] == ["where", "python"]:
            return subprocess.CompletedProcess(
                args=args,
                returncode=0,
                stdout=r"C:\Users\tester\AppData\Local\Microsoft\WindowsApps\python.exe" + "\n",
                stderr="",
            )
        if args[:2] == ["py", "-0p"]:
            return subprocess.CompletedProcess(
                args=args,
                returncode=0,
                stdout=f" -V:3.13          {py0p_path}\n",
                stderr="",
            )
        if args[0] == py0p_path:
            payload = json.dumps({"executable": py0p_path, "version": "3.13.2"})
            return subprocess.CompletedProcess(
                args=args,
                returncode=0,
                stdout=payload + "\n",
                stderr="",
            )
        return subprocess.CompletedProcess(args=args, returncode=1, stdout="", stderr="")

    monkeypatch.setattr(probe_mod.shutil, "which", _which)
    monkeypatch.setattr(probe_mod.subprocess, "run", _run)

    resolved = probe_mod.resolve_usable_python_interpreter(
        workspace_dir=None,
        candidate_commands=["python", "py"],
        timeout_seconds=1.0,
        force_windows=True,
        include_sys_executable=False,
    )

    assert resolved.selected_command == py0p_path
    assert resolved.selected_resolved_path == py0p_path


def test_resolve_prefers_workspace_venv_python(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    workspace_dir = tmp_path / "workspace"
    venv_python = workspace_dir / ".venv" / "Scripts" / "python.exe"
    venv_python.parent.mkdir(parents=True, exist_ok=True)
    venv_python.write_text("", encoding="utf-8")

    def _which(command: str) -> str | None:
        if command == str(venv_python):
            return str(venv_python)
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
        assert args[0] == str(venv_python)
        payload = json.dumps(
            {
                "executable": str(venv_python),
                "version": "3.13.2",
            }
        )
        return subprocess.CompletedProcess(
            args=args,
            returncode=0,
            stdout=payload + "\n",
            stderr="",
        )

    monkeypatch.setattr(probe_mod.shutil, "which", _which)
    monkeypatch.setattr(probe_mod.subprocess, "run", _run)

    resolved = probe_mod.resolve_usable_python_interpreter(
        workspace_dir=workspace_dir,
        candidate_commands=["python"],
        timeout_seconds=1.0,
        force_windows=True,
        include_sys_executable=False,
    )

    assert resolved.selected_command == str(venv_python)
    assert resolved.selected_resolved_path == str(venv_python)
