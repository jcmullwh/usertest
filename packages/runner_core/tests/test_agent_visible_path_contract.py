from __future__ import annotations

import json
import sys
from pathlib import Path

import runner_core.runner as runner_mod
from runner_core.verification_broker import VerificationBrokerAttempt
from runner_core.workspace_state_hash import WorkspaceStateHash


def test_resolve_agent_visible_run_dir_entry_materializes_workspace_copy_for_local_backend(
    tmp_path: Path,
) -> None:
    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir()
    run_dir = tmp_path / "run"
    staged_path = run_dir / "agent_prompts" / "system_prompt.md"
    staged_path.parent.mkdir(parents=True, exist_ok=True)
    staged_path.write_text("system prompt\n", encoding="utf-8")

    entry = runner_mod._resolve_agent_visible_run_dir_entry(
        staged_path,
        run_dir=run_dir,
        workspace_dir=workspace_dir,
        run_dir_mount=None,
    )

    expected_path = (
        workspace_dir
        / ".usertest_hidden"
        / "run_dir"
        / "agent_prompts"
        / "system_prompt.md"
    )
    assert entry.mirrored_into_workspace is True
    assert entry.host_path == expected_path
    assert entry.agent_path == runner_mod.normalize_agent_path(expected_path.resolve())
    assert expected_path.read_text(encoding="utf-8") == "system prompt\n"


def test_resolve_agent_visible_run_dir_entry_uses_host_path_without_workspace_mirror(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "run"
    staged_path = run_dir / "agent_prompts" / "system_prompt.md"
    staged_path.parent.mkdir(parents=True, exist_ok=True)
    staged_path.write_text("system prompt\n", encoding="utf-8")

    entry = runner_mod._resolve_agent_visible_run_dir_entry(
        staged_path,
        run_dir=run_dir,
        workspace_dir=None,
        run_dir_mount=None,
    )

    assert entry.mirrored_into_workspace is False
    assert entry.host_path == staged_path
    assert entry.agent_path == runner_mod.normalize_agent_path(staged_path.resolve())


def test_run_verification_commands_surfaces_workspace_artifacts_dir_for_local_backend(
    tmp_path: Path,
) -> None:
    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir()
    run_dir = tmp_path / "run"

    summary = runner_mod._run_verification_commands(
        run_dir=run_dir,
        workspace_dir=workspace_dir,
        run_dir_mount=None,
        attempt_number=1,
        commands=['python -c "print(\'ok\')"'],
        command_prefix=[],
        cwd=workspace_dir,
        timeout_seconds=None,
        python_executable=sys.executable,
    )

    mirrored_dir = (
        workspace_dir / ".usertest_hidden" / "run_dir" / "verification" / "attempt1"
    )
    mirrored_summary_path = mirrored_dir / "verification.json"
    assert summary["artifacts_dir"] == runner_mod.normalize_agent_path(
        mirrored_dir.resolve()
    )
    assert mirrored_summary_path.exists()
    mirrored_summary = json.loads(mirrored_summary_path.read_text(encoding="utf-8"))
    assert mirrored_summary["artifacts_dir"] == summary["artifacts_dir"]
    assert (run_dir / "verification" / "attempt1" / "verification.json").exists()


def test_verification_broker_mirrors_client_wrapper_into_workspace_for_local_backend(
    tmp_path: Path,
) -> None:
    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir()
    run_dir = tmp_path / "run"
    client_root = run_dir / "verification_broker" / "client"
    attempt_root = run_dir / "verification_broker" / "attempt1"

    client_entry = runner_mod._resolve_agent_visible_run_dir_entry(
        client_root,
        run_dir=run_dir,
        workspace_dir=workspace_dir,
        run_dir_mount=None,
        allow_missing=True,
    )

    broker = VerificationBrokerAttempt(
        run_dir=run_dir,
        attempt_number=1,
        client_root=client_root,
        client_root_host_for_agent=client_entry.host_path,
        client_root_for_agent=client_entry.agent_path,
        attempt_root_for_agent=runner_mod._agent_execution_path_for_run_dir_entry(
            attempt_root,
            run_dir=run_dir,
            run_dir_mount=None,
        ),
        execution_shell="bash",
        python_command=sys.executable,
        verification_timeout_seconds=5.0,
        verification_command_count=1,
        verifier=lambda *_args, **_kwargs: {
            "schema_version": 1,
            "attempt": 1,
            "artifacts_dir": None,
            "passed": True,
            "status": "passed",
            "terminal_reason": "passed",
            "commands": [],
        },
        workspace_hash_fn=lambda: WorkspaceStateHash(
            sha256="abc123",
            mode="filesystem",
            file_count=1,
            deleted_count=0,
        ),
        utc_now_fn=lambda: "2026-03-16T00:00:00Z",
        run_async_verifier=False,
    )

    mirrored_wrapper = client_entry.host_path / "verify_client.sh"
    assert mirrored_wrapper.exists()
    expected_command = runner_mod.render_verification_broker_command(
        client_root_for_agent=client_entry.agent_path,
        launcher=runner_mod.resolve_verification_launcher(command_prefix=[]),
    )
    assert (
        runner_mod._verification_broker_client_command(
            run_dir=run_dir,
            workspace_dir=workspace_dir,
            run_dir_mount=None,
            command_prefix=[],
        )
        == expected_command
    )
    broker.stop()
