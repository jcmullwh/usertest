from __future__ import annotations

import json
from pathlib import Path

import runner_core.runner as runner_mod
from runner_core import RunnerConfig, RunRequest, find_repo_root, run_once
from runner_core.runner import _resolve_shell_capability


def _install_task_requires_shell_mission(target_repo: Path) -> None:
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
                "  mission_id: test_task_requires_shell",
                "",
            ]
        ),
        encoding="utf-8",
    )
    (missions_dir / "test_task_requires_shell.mission.md").write_text(
        "\n".join(
            [
                "---",
                "id: test_task_requires_shell",
                "name: Test Task Requires Shell",
                "extends: null",
                "execution_mode: single_pass_inline_report",
                "prompt_template: default_inline_report.prompt.md",
                "report_schema: task_run_v1.schema.json",
                "requires_shell: true",
                "requires_edits: false",
                "---",
                "Mission used by shell capability contract tests.",
                "",
            ]
        ),
        encoding="utf-8",
    )


def test_shell_capability_resolver_available_blocked_and_unprobed() -> None:
    available = _resolve_shell_capability(
        agent="claude",
        operating_system="Linux",
        backend="local",
        sandbox_mode=None,
        policy_status="allowed",
        policy_reason="claude.allowed_tools includes Bash",
        allowed_tools=["Bash"],
    ).to_dict()
    assert available["state"] == "available"
    assert available["reason_code"] is None

    blocked = _resolve_shell_capability(
        agent="gemini",
        operating_system="Linux",
        backend="local",
        sandbox_mode=None,
        policy_status="blocked",
        policy_reason="run_shell_command not enabled",
        allowed_tools=["read_file"],
    ).to_dict()
    assert blocked["state"] == "blocked"
    assert blocked["reason_code"] == "shell_policy_blocked"

    unprobed = _resolve_shell_capability(
        agent="codex",
        operating_system="Windows",
        backend="local",
        sandbox_mode="read-only",
        policy_status="unknown",
        policy_reason="Codex CLI command execution depends on sandbox policy.",
        allowed_tools=None,
    ).to_dict()
    assert unprobed["state"] == "unprobed"
    assert unprobed["reason_code"] == "codex_windows_shell_unprobed"


def test_shell_capability_resolver_classifies_codex_windows_probe_failures() -> None:
    panic = _resolve_shell_capability(
        agent="codex",
        operating_system="Windows",
        backend="local",
        sandbox_mode="read-only",
        policy_status="unknown",
        policy_reason="Codex CLI command execution depends on sandbox policy.",
        allowed_tools=None,
        probe_result={
            "passed": False,
            "stderr_excerpt": (
                "thread panicked in windows-sandbox-rs: called `Option::unwrap()` "
                "on a `None` value"
            ),
        },
    ).to_dict()
    assert panic["state"] == "blocked"
    assert panic["probe_status"] == "failed"
    assert panic["reason_code"] == "codex_windows_sandbox_panic"

    powershell = _resolve_shell_capability(
        agent="codex",
        operating_system="Windows",
        backend="local",
        sandbox_mode="workspace-write",
        policy_status="unknown",
        policy_reason="Codex CLI command execution depends on sandbox policy.",
        allowed_tools=None,
        probe_result={
            "ok": False,
            "stderr_excerpt": "PowerShell command failed before payload execution",
        },
    ).to_dict()
    assert powershell["state"] == "blocked"
    assert powershell["reason_code"] == "codex_windows_powershell_prepayload_failed"


def test_shell_required_unprobed_capability_blocks_dispatch_and_writes_report(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repo_root = find_repo_root(Path(__file__).resolve())
    target = tmp_path / "target_repo"
    target.mkdir()
    (target / "README.md").write_text("# hi\n", encoding="utf-8")
    _install_task_requires_shell_mission(target)

    monkeypatch.setattr(runner_mod, "_runner_host_os", lambda: "Windows")

    cfg = RunnerConfig(
        repo_root=repo_root,
        runs_dir=tmp_path / "runs",
        agents={"codex": {"binary": "definitely-not-run-codex"}},
        policies={"safe": {"codex": {"sandbox": "read-only", "allow_edits": False}}},
    )

    result = run_once(
        cfg,
        RunRequest(repo=str(target), agent="codex", policy="safe", exec_backend="local"),
    )

    assert result.exit_code != 0
    assert not (result.run_dir / "agent_attempts.json").exists()

    preflight = json.loads((result.run_dir / "preflight.json").read_text(encoding="utf-8"))
    shell_capability = preflight["shell_capability"]
    assert shell_capability["state"] == "unprobed"
    assert shell_capability["reason_code"] == "codex_windows_shell_unprobed"
    assert preflight["capabilities"]["shell_commands"]["canonical"] == shell_capability

    error_payload = json.loads((result.run_dir / "error.json").read_text(encoding="utf-8"))
    assert error_payload["type"] == "AgentPreflightFailed"
    assert error_payload["subtype"] == "mission_requires_shell"
    assert error_payload["preflight"]["shell_capability"] == shell_capability

    report = json.loads((result.run_dir / "report.json").read_text(encoding="utf-8"))
    assert report["kind"] == "task_run_v1"
    assert report["status"] == "failure"
    assert report["extensions"]["shell_capability"] == shell_capability

    report_md = (result.run_dir / "report.md").read_text(encoding="utf-8")
    assert "Shell capability: unprobed" in report_md
    assert "codex_windows_shell_unprobed" in report_md
