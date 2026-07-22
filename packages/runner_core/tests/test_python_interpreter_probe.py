from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import time
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


def _completed_probe(
    args: list[str],
    *,
    returncode: int,
    stdout: str,
    stderr: str,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.CompletedProcess(
        args=args,
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )
    result.timed_out = False
    result.cleanup_succeeded = True
    result.cleanup_diagnostic = None
    return result


def test_probe_rejects_windowsapps_alias(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("USERTEST_PYTHON", raising=False)
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
        return _completed_probe(
            ["python", "-c", "..."],
            returncode=1,
            stdout="",
            stderr=(
                "Fatal Python error: init_fs_encoding\n"
                "ModuleNotFoundError: No module named 'encodings'"
            ),
        )

    monkeypatch.setattr(probe_mod, "_run_bounded_interpreter_probe", _run)

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

    monkeypatch.setattr(probe_mod, "_run_bounded_interpreter_probe", _run)

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
        return _completed_probe(
            ["python", "-c", "..."],
            returncode=1,
            stdout="",
            stderr=(
                "Unable to create process using 'C:\\Python313\\python.exe -V': "
                "The file cannot be accessed by the system."
            ),
        )

    monkeypatch.setattr(probe_mod, "_run_bounded_interpreter_probe", _run)

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
    monkeypatch.delenv("USERTEST_PYTHON", raising=False)

    def _which(command: str) -> str | None:
        if command == "python":
            return r"C:\Users\tester\AppData\Local\Microsoft\WindowsApps\python.exe"
        if command == "py":
            return r"C:\Python313\py.exe"
        return None

    def _run(
        args: list[str],
        *,
        timeout_seconds: float,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        assert args[0] == r"C:\Python313\py.exe"
        assert timeout_seconds > 0
        assert env is None
        payload = json.dumps(
            {
                "executable": r"C:\Python313\python.exe",
                "version": "3.13.2",
            }
        )
        return _completed_probe(
            args,
            returncode=0,
            stdout=payload + "\n",
            stderr="",
        )

    monkeypatch.setattr(probe_mod.shutil, "which", _which)
    monkeypatch.setattr(probe_mod, "_run_bounded_interpreter_probe", _run)

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
        timeout_seconds: float,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        assert timeout_seconds > 0
        assert env is None or isinstance(env, dict)
        if args[:2] == ["where", "python"]:
            return _completed_probe(
                args,
                returncode=0,
                stdout=r"C:\Users\tester\AppData\Local\Microsoft\WindowsApps\python.exe" + "\n",
                stderr="",
            )
        if args[:2] == ["py", "-0p"]:
            return _completed_probe(
                args,
                returncode=0,
                stdout=f" -V:3.13          {py0p_path}\n",
                stderr="",
            )
        if args[0] == py0p_path:
            payload = json.dumps({"executable": py0p_path, "version": "3.13.2"})
            return _completed_probe(
                args,
                returncode=0,
                stdout=payload + "\n",
                stderr="",
            )
        return _completed_probe(args, returncode=1, stdout="", stderr="")

    monkeypatch.setattr(probe_mod.shutil, "which", _which)
    monkeypatch.setattr(probe_mod, "_run_bounded_interpreter_probe", _run)

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
        timeout_seconds: float,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        assert args[0] == str(venv_python)
        assert timeout_seconds > 0
        assert env is None
        payload = json.dumps(
            {
                "executable": str(venv_python),
                "version": "3.13.2",
            }
        )
        return _completed_probe(
            args,
            returncode=0,
            stdout=payload + "\n",
            stderr="",
        )

    monkeypatch.setattr(probe_mod.shutil, "which", _which)
    monkeypatch.setattr(probe_mod, "_run_bounded_interpreter_probe", _run)
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    monkeypatch.delenv("USERTEST_PYTHON", raising=False)

    resolved = probe_mod.resolve_usable_python_interpreter(
        workspace_dir=workspace_dir,
        candidate_commands=["python"],
        timeout_seconds=1.0,
        force_windows=True,
        include_sys_executable=False,
    )

    assert resolved.selected_command == str(venv_python)
    assert resolved.selected_resolved_path == str(venv_python)


@pytest.mark.skipif(os.name != "nt", reason="Windows inherited-pipe regression")
def test_health_probe_returns_when_exited_interpreter_leaves_inherited_handles_open(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    descendant_pid_path = tmp_path / "interpreter-probe-descendant.pid"
    descendant_program = (
        "from pathlib import Path; import os, sys, time; "
        "Path(sys.argv[1]).write_text(str(os.getpid()), encoding='utf-8'); "
        "print('descendant-stdout-open', flush=True); "
        "print('descendant-stderr-open', file=sys.stderr, flush=True); "
        "time.sleep(120)"
    )
    health_probe = (
        "import json, subprocess, sys, time; from pathlib import Path; "
        f"pid_path = Path({str(descendant_pid_path)!r}); "
        "subprocess.Popen([sys.executable, '-c', "
        f"{descendant_program!r}, str(pid_path)], stdin=subprocess.DEVNULL); "
        "deadline = time.monotonic() + 10.0; "
        'exec("while not pid_path.exists() and time.monotonic() < deadline:\\n '
        '   time.sleep(0.01)"); '
        "print(json.dumps({'executable': sys.executable, "
        "'version': sys.version.split()[0]}), flush=True)"
    )
    monkeypatch.setattr(probe_mod, "_PYTHON_HEALTH_PROBE", health_probe)
    # This test exercises bounded process-tree cleanup, not WindowsApps policy. The
    # pytest interpreter itself may be a fully runnable Store Python whose path still
    # matches the deliberately conservative WindowsApps rejection rule.
    monkeypatch.setattr(probe_mod, "_is_windowsapps_alias", lambda *_args, **_kwargs: False)

    started = time.monotonic()
    result = probe_mod.probe_python_interpreters(
        candidate_commands=[sys.executable],
        timeout_seconds=15.0,
    )
    elapsed = time.monotonic() - started

    assert descendant_pid_path.exists()
    assert elapsed < 30.0
    candidate = result.by_command()[sys.executable]
    assert candidate.usable is True, candidate
    assert result.selected_resolved_path == sys.executable
