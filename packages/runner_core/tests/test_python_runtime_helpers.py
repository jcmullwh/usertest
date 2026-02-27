from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import runner_core.python_runtime as runtime_mod
import runner_core.runner as runner_mod
from runner_core import RunnerConfig, RunRequest, find_repo_root, run_once


def _install_no_requirements_mission(target_repo: Path) -> None:
    usertest_dir = target_repo / ".usertest"
    missions_dir = usertest_dir / "missions"
    missions_dir.mkdir(parents=True, exist_ok=True)

    (usertest_dir / "catalog.yaml").write_text(
        "\n".join(
            [
                "version: 1",
                "missions_dirs:",
                "  - .usertest/missions",
                "defaults:",
                "  mission_id: test_no_requirements_smoke",
                "",
            ]
        ),
        encoding="utf-8",
    )

    (missions_dir / "test_no_requirements_smoke.mission.md").write_text(
        "\n".join(
            [
                "---",
                "id: test_no_requirements_smoke",
                "name: Test No-Requirements Smoke",
                "extends: null",
                "execution_mode: single_pass_inline_report",
                "prompt_template: default_inline_report.prompt.md",
                "report_schema: default_report.schema.json",
                "requires_shell: false",
                "requires_edits: false",
                "---",
                "Mission used by tests that exercise read-only preflight flows.",
                "",
            ]
        ),
        encoding="utf-8",
    )


def test_verification_command_helpers_detect_pytest_and_provisioning() -> None:
    assert runtime_mod.verification_commands_need_pytest(("pytest -q",)) is True
    assert runtime_mod.verification_commands_need_pytest(("python -m pytest -q",)) is True
    assert runtime_mod.verification_commands_need_pytest(("echo hello",)) is False

    assert (
        runtime_mod.verification_commands_may_provision_pytest(
            ("python -m pip install -U pytest", "pytest -q")
        )
        is True
    )
    assert runtime_mod.verification_commands_may_provision_pytest(("pytest -q",)) is False


def test_rewrite_verification_command_for_python_powershell() -> None:
    cmd, rewritten = runner_mod._rewrite_verification_command_for_python(
        "python -m pytest -q",
        python_executable=r"C:\Program Files\Python\python.exe",
        is_powershell=True,
    )
    assert rewritten is True
    assert cmd.startswith("& 'C:\\Program Files\\Python\\python.exe' -m pytest")

    cmd, rewritten = runner_mod._rewrite_verification_command_for_python(
        "pytest -q",
        python_executable=r"C:\Program Files\Python\python.exe",
        is_powershell=True,
    )
    assert rewritten is True
    assert cmd.startswith("& 'C:\\Program Files\\Python\\python.exe' -m pytest")


def test_run_once_fails_fast_when_verification_needs_pytest_but_python_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = find_repo_root(Path(__file__).resolve())
    target = tmp_path / "target_repo"
    target.mkdir()
    (target / "README.md").write_text("# hi\n", encoding="utf-8")
    _install_no_requirements_mission(target)

    monkeypatch.setattr(
        runner_mod,
        "select_python_runtime",
        lambda *args, **kwargs: runtime_mod.PythonRuntimeSelection(
            selected=None, candidates=tuple()
        ),
    )

    cfg = RunnerConfig(
        repo_root=repo_root,
        runs_dir=tmp_path / "runs",
        agents={"codex": {"binary": str(tmp_path / "dummy_codex.exe")}},
        policies={"safe": {"codex": {"sandbox": "read-only", "allow_edits": False}}},
    )

    result = run_once(
        cfg,
        RunRequest(
            repo=str(target),
            agent="codex",
            policy="safe",
            verification_commands=("pytest -q",),
        ),
    )

    assert result.exit_code == 1
    error_obj = json.loads((result.run_dir / "error.json").read_text(encoding="utf-8"))
    assert error_obj.get("type") == "AgentPreflightFailed"
    assert error_obj.get("subtype") == "python_unavailable"


def test_select_python_runtime_skips_present_but_non_executable_interpreter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    When a candidate interpreter file exists but cannot be executed (e.g., access denied or
    sandbox-interdicted), select_python_runtime must skip it and fall back to a working candidate.
    """
    import os

    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir()

    # Create the workspace venv python path matching the current platform.
    if os.name == "nt":
        fake_inaccessible = workspace_dir / ".venv" / "Scripts" / "python.exe"
    else:
        fake_inaccessible = workspace_dir / ".venv" / "bin" / "python"
    fake_inaccessible.parent.mkdir(parents=True)
    fake_inaccessible.write_bytes(b"")

    # Create a fake fallback python that "exists" and succeeds.
    fake_fallback = tmp_path / "fallback_python.exe"
    fake_fallback.write_bytes(b"")
    fallback_payload = json.dumps({"executable": str(fake_fallback), "version": "3.13.0"})

    def _mock_run(
        args: list[str],
        *,
        capture_output: bool = False,
        text: bool = False,
        encoding: str = "utf-8",
        errors: str = "replace",
        timeout: float = 5.0,
        check: bool = False,
        **kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        if args[0] == str(fake_inaccessible):
            raise OSError("Access is denied")
        if args[0] == str(fake_fallback):
            return subprocess.CompletedProcess(
                args=args, returncode=0, stdout=fallback_payload + "\n", stderr=""
            )
        return subprocess.CompletedProcess(args=args, returncode=1, stdout="", stderr="")

    monkeypatch.setattr(runtime_mod.subprocess, "run", _mock_run)
    monkeypatch.setattr(
        runtime_mod.shutil,
        "which",
        lambda cmd: str(fake_fallback) if cmd == "python" else None,
    )
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    monkeypatch.delenv("USERTEST_PYTHON", raising=False)

    result = runtime_mod.select_python_runtime(workspace_dir=workspace_dir, timeout_seconds=1.0)

    # The inaccessible workspace_venv candidate should be rejected with launch_failed.
    rejected = [c for c in result.candidates if not c.usable]
    assert any(
        c.source == "workspace_venv" and c.reason_code == "launch_failed" for c in rejected
    ), f"Expected workspace_venv to be rejected with launch_failed; got: {rejected}"

    # A usable fallback should have been selected.
    assert result.selected is not None, "Expected a usable fallback to be selected"
    assert result.selected.source == "command_python"


def test_select_python_runtime_prefers_sandbox_env_python(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    When USERTEST_PYTHON env var is set to a usable interpreter, it should be selected first,
    even when a workspace venv also exists. Skipped if the feature is not yet present.
    """
    # Detect whether this version of select_python_runtime supports USERTEST_PYTHON.
    import inspect

    src = inspect.getsource(runtime_mod.select_python_runtime)
    if "USERTEST_PYTHON" not in src:
        pytest.skip("select_python_runtime does not yet support USERTEST_PYTHON env var")

    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir()

    # Create a sandbox-provided python.
    sandbox_python = tmp_path / "sandbox_python.exe"
    sandbox_python.write_bytes(b"")
    sandbox_payload = json.dumps({"executable": str(sandbox_python), "version": "3.13.1"})

    def _mock_run(
        args: list[str],
        *,
        capture_output: bool = False,
        text: bool = False,
        encoding: str = "utf-8",
        errors: str = "replace",
        timeout: float = 5.0,
        check: bool = False,
        **kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        if args[0] == str(sandbox_python):
            return subprocess.CompletedProcess(
                args=args, returncode=0, stdout=sandbox_payload + "\n", stderr=""
            )
        # All others fail to simulate they would not be chosen.
        return subprocess.CompletedProcess(args=args, returncode=1, stdout="", stderr="no module")

    monkeypatch.setattr(runtime_mod.subprocess, "run", _mock_run)
    monkeypatch.setattr(runtime_mod.shutil, "which", lambda cmd: None)
    monkeypatch.setenv("USERTEST_PYTHON", str(sandbox_python))
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)

    result = runtime_mod.select_python_runtime(workspace_dir=workspace_dir, timeout_seconds=1.0)

    assert result.selected is not None
    assert result.selected.source == "sandbox_env"
    assert result.selected.path == str(sandbox_python)


def test_select_python_runtime_no_candidates_yields_none(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    When all interpreter candidates are present-but-non-executable and no fallback exists,
    select_python_runtime returns selected=None and all candidates in rejected state.
    """
    import os

    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir()

    # Create a workspace venv python that exists but always raises OSError.
    if os.name == "nt":
        fake_broken = workspace_dir / ".venv" / "Scripts" / "python.exe"
    else:
        fake_broken = workspace_dir / ".venv" / "bin" / "python"
    fake_broken.parent.mkdir(parents=True)
    fake_broken.write_bytes(b"")

    def _mock_run(
        args: list[str],
        *,
        capture_output: bool = False,
        text: bool = False,
        encoding: str = "utf-8",
        errors: str = "replace",
        timeout: float = 5.0,
        check: bool = False,
        **kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        raise OSError("The file cannot be accessed by the system.")

    monkeypatch.setattr(runtime_mod.subprocess, "run", _mock_run)
    monkeypatch.setattr(runtime_mod.shutil, "which", lambda cmd: str(fake_broken))
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    monkeypatch.delenv("USERTEST_PYTHON", raising=False)
    # Prevent sys.executable from saving us.
    monkeypatch.setattr(runtime_mod.sys, "executable", str(fake_broken))

    result = runtime_mod.select_python_runtime(workspace_dir=workspace_dir, timeout_seconds=1.0)

    assert result.selected is None, "Expected no usable interpreter to be found"
    assert all(not c.usable for c in result.candidates if c.present)


# ---------------------------------------------------------------------------
# Regression tests for BLG-012: Windows path backslash preservation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "python_path,input_cmd",
    [
        # python.exe at C:\Python313
        (r"C:\Python313\python.exe", "python -m pytest -q"),
        # python.exe in C:\Users (common user-install location)
        (
            r"C:\Users\jason\AppData\Local\Programs\Python\Python313\python.exe",
            "python -m pytest",
        ),
        # py.exe launcher
        (
            r"C:\Users\jason\AppData\Local\Programs\Python\Launcher\py.exe",
            "python -m pytest --version",
        ),
        # pytest command -> -m pytest rewrite
        (r"C:\Python313\python.exe", "pytest -q"),
        # Path with spaces
        (r"C:\Program Files\Python313\python.exe", "python -m pytest"),
        # WindowsApps/Packages location
        (
            r"C:\Users\jason\AppData\Local\Packages\PythonSoftwareFoundation.Python.3.13_qbz5n2kfra8p0\LocalCache\local-packages\Python313\Scripts\python.exe",
            "python -m pytest",
        ),
    ],
)
def test_rewrite_verification_command_preserves_windows_backslashes_powershell(
    python_path: str, input_cmd: str
) -> None:
    """
    Regression for BLG-012: _rewrite_verification_command_for_python must emit the
    full Windows absolute path with all backslashes intact when is_powershell=True.

    Previously, paths like C:\\Python313\\python.exe could be emitted as
    C:Python313python.exe if escaping/quoting was broken.
    """
    cmd, rewritten = runner_mod._rewrite_verification_command_for_python(
        input_cmd,
        python_executable=python_path,
        is_powershell=True,
    )
    assert rewritten is True, f"Expected command to be rewritten for {input_cmd!r}"
    # The full path must appear in the output with backslashes
    assert python_path in cmd, (
        f"Windows path lost or corrupted in rewritten command.\n"
        f"Python path: {python_path!r}\n"
        f"Input cmd:   {input_cmd!r}\n"
        f"Got:         {cmd!r}"
    )
    # Specifically check that the drive+backslash pattern is not collapsed
    drive_prefix = python_path[:3]  # e.g. "C:\\"
    assert drive_prefix in cmd, (
        f"Drive+backslash prefix {drive_prefix!r} collapsed in rewritten command.\n"
        f"Got: {cmd!r}"
    )

