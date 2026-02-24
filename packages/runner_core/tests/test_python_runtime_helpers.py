from __future__ import annotations

import json
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

