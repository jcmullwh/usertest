from __future__ import annotations

import json
from pathlib import Path

import pytest

import runner_core.runner as runner_mod
from runner_core import RunnerConfig, RunRequest, find_repo_root, run_once


def _install_requires_shell_mission(target_repo: Path) -> None:
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
                "  mission_id: test_requires_shell_smoke",
                "",
            ]
        ),
        encoding="utf-8",
    )

    (missions_dir / "test_requires_shell_smoke.mission.md").write_text(
        "\n".join(
            [
                "---",
                "id: test_requires_shell_smoke",
                "name: Test Requires-Shell Smoke",
                "extends: null",
                "execution_mode: single_pass_inline_report",
                "prompt_template: default_inline_report.prompt.md",
                "report_schema: default_report.schema.json",
                "requires_shell: true",
                "requires_edits: false",
                "---",
                "Mission used by tests that exercise shell preflight flows.",
                "",
            ]
        ),
        encoding="utf-8",
    )


def test_gemini_requires_shell_reports_backend_mismatch_with_docker_remediation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = find_repo_root(Path(__file__).resolve())
    target = tmp_path / "target_repo"
    target.mkdir()
    (target / "README.md").write_text("# hi\n", encoding="utf-8")
    _install_requires_shell_mission(target)

    monkeypatch.setattr(runner_mod, "_effective_gemini_cli_sandbox", lambda **_: False)
    monkeypatch.setattr(runner_mod, "_docker_exec_backend_available", lambda: False)

    cfg = RunnerConfig(
        repo_root=repo_root,
        runs_dir=tmp_path / "runs",
        agents={"gemini": {}},
        policies={
            "inspect": {
                "gemini": {
                    "allow_edits": False,
                    "allowed_tools": ["run_shell_command"],
                    "sandbox": True,
                }
            }
        },
    )

    result = run_once(
        cfg,
        RunRequest(
            repo=str(target),
            agent="gemini",
            policy="inspect",
            exec_backend="local",
        ),
    )

    assert result.exit_code == 1
    error_obj = json.loads((result.run_dir / "error.json").read_text(encoding="utf-8"))
    assert error_obj.get("type") == "AgentPreflightFailed"
    assert error_obj.get("subtype") == "mission_requires_shell"
    assert "exec-backend docker" in str(error_obj.get("hint", ""))
    assert "--exec-backend docker" in str(error_obj.get("suggested_command", ""))
    assert "policy" not in str(error_obj.get("hint", "")).lower()
    assert "policy" not in str(error_obj.get("message", "")).lower()

