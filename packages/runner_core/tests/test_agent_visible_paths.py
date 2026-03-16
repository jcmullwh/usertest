from __future__ import annotations

import json
import sys
from pathlib import Path

import runner_core.runner as runner_mod
from runner_core.agent_visible_paths import agent_visible_run_subpath


def test_agent_visible_run_subpath_uses_run_dir_mount_for_staged_file(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    workspace_dir = tmp_path / "workspace"

    layout = agent_visible_run_subpath(
        run_dir=run_dir,
        subpath=Path("agent_prompts") / "system_prompt.md",
        run_dir_mount="/run_dir",
        workspace_dir=workspace_dir,
    )

    assert layout.host_path == run_dir / "agent_prompts" / "system_prompt.md"
    assert layout.agent_path == "/run_dir/agent_prompts/system_prompt.md"


def test_agent_visible_run_subpath_uses_workspace_relative_mirror_without_run_dir_mount(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "run"
    workspace_dir = tmp_path / "workspace"

    layout = agent_visible_run_subpath(
        run_dir=run_dir,
        subpath=Path("verification") / "attempt1" / "verification.json",
        run_dir_mount=None,
        workspace_dir=workspace_dir,
    )

    assert layout.host_path == workspace_dir / "verification" / "attempt1" / "verification.json"
    assert layout.agent_path == "verification/attempt1/verification.json"


def test_run_verification_commands_mirrors_agent_visible_artifacts_into_workspace(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "run"
    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir()
    layout = agent_visible_run_subpath(
        run_dir=run_dir,
        subpath=Path("verification") / "attempt1",
        run_dir_mount=None,
        workspace_dir=workspace_dir,
    )

    summary = runner_mod._run_verification_commands(
        run_dir=run_dir,
        attempt_number=1,
        commands=[
            (
                f'{sys.executable} -c "import sys; '
                "print('visible stdout'); "
                "print('visible stderr', file=sys.stderr)\""
            )
        ],
        command_prefix=[],
        cwd=workspace_dir,
        timeout_seconds=10.0,
        python_executable=sys.executable,
        agent_visible_artifacts_dir=layout.agent_path,
        agent_visible_artifacts_host_dir=layout.host_path,
        agent_visible_workspace_dir=workspace_dir,
    )

    assert summary["artifacts_dir"] == "verification/attempt1"
    mirrored_summary_path = workspace_dir / "verification" / "attempt1" / "verification.json"
    assert mirrored_summary_path.exists()
    mirrored_summary = json.loads(mirrored_summary_path.read_text(encoding="utf-8"))
    assert mirrored_summary["artifacts_dir"] == "verification/attempt1"
    assert (workspace_dir / "verification" / "attempt1" / "cmd_01.stdout.txt").exists()
    assert (workspace_dir / "verification" / "attempt1" / "cmd_01.stderr.txt").exists()
    assert (run_dir / "verification" / "attempt1" / "verification.json").exists()
