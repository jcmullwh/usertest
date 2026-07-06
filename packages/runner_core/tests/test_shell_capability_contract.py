from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from normalized_events import iter_events_jsonl

import runner_core.runner as runner_mod
from runner_core import RunnerConfig, RunRequest, find_repo_root, run_once
from runner_core.runner import _resolve_shell_capability, _shell_probe_result_from_preflight_meta


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
    discovery_only = _resolve_shell_capability(
        agent="claude",
        operating_system="Linux",
        backend="local",
        sandbox_mode=None,
        policy_status="allowed",
        policy_reason="claude.allowed_tools includes Bash",
        allowed_tools=["Bash"],
    ).to_dict()
    assert discovery_only["state"] == "unprobed"
    assert discovery_only["reason_code"] == "shell_command_discovered_without_launchability"

    available = _resolve_shell_capability(
        agent="claude",
        operating_system="Linux",
        backend="local",
        sandbox_mode=None,
        policy_status="allowed",
        policy_reason="claude.allowed_tools includes Bash",
        allowed_tools=["Bash"],
        probe_result={"kind": "backend_shell_payload", "ok": True, "exit_code": 0},
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

    policy_blocked = _resolve_shell_capability(
        agent="codex",
        operating_system="Windows",
        backend="local",
        sandbox_mode="workspace-write",
        policy_status="unknown",
        policy_reason="Codex CLI command execution depends on sandbox policy.",
        allowed_tools=None,
        probe_result={
            "ok": False,
            "stderr_excerpt": "Process launch blocked by policy before payload execution",
        },
    ).to_dict()
    assert policy_blocked["state"] == "blocked"
    assert policy_blocked["reason_code"] == "codex_windows_process_launch_blocked_by_policy"


def test_failed_shell_probe_blocks_non_codex_agents() -> None:
    for agent, allowed_tools in (("claude", ["Bash"]), ("gemini", ["run_shell_command"])):
        blocked = _resolve_shell_capability(
            agent=agent,
            operating_system="Linux",
            backend="docker",
            sandbox_mode=None,
            policy_status="allowed",
            policy_reason="policy permits shell commands",
            allowed_tools=allowed_tools,
            probe_result={
                "ok": False,
                "exit_code": 2,
                "stderr_excerpt": "backend shell probe failed",
            },
        ).to_dict()
        assert blocked["state"] == "blocked"
        assert blocked["probe_status"] == "failed"
        assert blocked["reason_code"] == "shell_probe_failed"


def test_codex_nonlocal_backend_requires_explicit_shell_evidence() -> None:
    unprobed = _resolve_shell_capability(
        agent="codex",
        operating_system="Linux",
        backend="docker",
        sandbox_mode="danger-full-access",
        policy_status="unknown",
        policy_reason="Codex CLI command execution depends on sandbox policy.",
        allowed_tools=None,
    ).to_dict()
    assert unprobed["state"] == "unprobed"
    assert unprobed["probe_status"] == "not_run"

    policy_only = _resolve_shell_capability(
        agent="codex",
        operating_system="Linux",
        backend="docker",
        sandbox_mode="danger-full-access",
        policy_status="allowed",
        policy_reason="Codex sandbox policy is explicitly configured as danger-full-access.",
        allowed_tools=None,
    ).to_dict()
    assert policy_only["state"] == "unprobed"
    assert policy_only["probe_status"] == "not_run"

    available = _resolve_shell_capability(
        agent="codex",
        operating_system="Linux",
        backend="docker",
        sandbox_mode="danger-full-access",
        policy_status="unknown",
        policy_reason="Codex CLI command execution depends on sandbox policy.",
        allowed_tools=None,
        probe_result={"ok": True, "exit_code": 0},
    ).to_dict()
    assert available["state"] == "available"
    assert available["probe_status"] == "passed"


def test_shell_probe_result_uses_existing_backend_probe_meta() -> None:
    generic_probe = _shell_probe_result_from_preflight_meta(
        {
            "exit_code": 0,
            "stderr": "",
            "command_probe_details": {"codex": {"present": True}},
        }
    )
    assert generic_probe is None

    generic_capability = _resolve_shell_capability(
        agent="codex",
        operating_system="Linux",
        backend="docker",
        sandbox_mode="danger-full-access",
        policy_status="unknown",
        policy_reason="Codex CLI command execution depends on sandbox policy.",
        allowed_tools=None,
        probe_result=generic_probe,
    ).to_dict()
    assert generic_capability["state"] == "unprobed"

    passed = _shell_probe_result_from_preflight_meta(
        {"shell_probe": {"exit_code": 0, "stderr": ""}}
    )
    assert passed == {
        "kind": "backend_shell_payload",
        "ok": True,
        "exit_code": 0,
        "stderr_excerpt": "",
        "stdout_excerpt": "",
    }

    failed = _shell_probe_result_from_preflight_meta(
        {"error": "thread panicked in windows-sandbox-rs before payload"}
    )
    assert failed is not None
    assert failed["ok"] is False
    assert "windows-sandbox-rs" in failed["error"]


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

    events = list(iter_events_jsonl(result.run_dir / "normalized_events.jsonl"))
    assert events[-1]["type"] == "preflight_shell_capability"
    assert events[-1]["data"]["shell_capability"] == shell_capability


def test_shell_required_backend_probe_failure_blocks_dispatch_and_classifies(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repo_root = find_repo_root(Path(__file__).resolve())
    target = tmp_path / "target_repo"
    target.mkdir()
    (target / "README.md").write_text("# hi\n", encoding="utf-8")
    _install_task_requires_shell_mission(target)

    monkeypatch.setattr(runner_mod, "_runner_host_os", lambda: "Windows")
    monkeypatch.setattr(
        runner_mod,
        "prepare_execution_backend",
        lambda **_: SimpleNamespace(
            sandbox_instance=SimpleNamespace(close=lambda: None),
            command_prefix=["docker", "exec", "fake-container"],
            workspace_mount=None,
            run_dir_mount=None,
        ),
    )
    monkeypatch.setattr(
        runner_mod,
        "probe_commands_in_container",
        lambda **_: (
            {},
            {
                "error": (
                    "thread panicked in windows-sandbox-rs before shell payload execution"
                )
            },
        ),
    )
    monkeypatch.setattr(
        runner_mod,
        "_validate_python_capability",
        lambda **_: {
            "runtime_selection": SimpleNamespace(selected=None),
            "runtime_summary": {"selected": None},
            "context_probe": None,
            "validation": {
                "required": False,
                "enabled": True,
                "reason_code": None,
                "reason_type": None,
                "reason": None,
                "validated_python_executable": None,
            },
        },
    )

    cfg = RunnerConfig(
        repo_root=repo_root,
        runs_dir=tmp_path / "runs",
        agents={"codex": {"binary": "definitely-not-run-codex"}},
        policies={"safe": {"codex": {"sandbox": "read-only", "allow_edits": False}}},
    )

    result = run_once(
        cfg,
        RunRequest(repo=str(target), agent="codex", policy="safe", exec_backend="docker"),
    )

    assert result.exit_code != 0
    assert not (result.run_dir / "agent_attempts.json").exists()

    error_payload = json.loads((result.run_dir / "error.json").read_text(encoding="utf-8"))
    shell_capability = error_payload["preflight"]["shell_capability"]
    assert shell_capability["state"] == "blocked"
    assert shell_capability["probe_status"] == "failed"
    assert shell_capability["reason_code"] == "codex_windows_sandbox_panic"
