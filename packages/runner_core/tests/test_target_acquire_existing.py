from __future__ import annotations

import subprocess
from pathlib import Path

from runner_core.target_acquire import acquire_existing_target


def _git(workspace: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(workspace), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def test_existing_target_preserves_head_and_dirty_work_for_nonexistent_recorded_ref(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _git(workspace, "init")
    _git(workspace, "config", "user.email", "usertest@local")
    _git(workspace, "config", "user.name", "usertest")

    tracked = workspace / "tracked.txt"
    tracked.write_text("committed\n", encoding="utf-8")
    _git(workspace, "add", "tracked.txt")
    _git(workspace, "commit", "-m", "initial")

    original_head = _git(workspace, "rev-parse", "HEAD")
    original_branch = _git(workspace, "branch", "--show-current")
    tracked.write_text("uncommitted implementation\n", encoding="utf-8")
    untracked = workspace / "new-implementation.txt"
    untracked.write_text("untracked implementation\n", encoding="utf-8")
    original_status = _git(workspace, "status", "--porcelain=v1")

    intended_ref = "backlog/03f0d43eb78e"
    acquired = acquire_existing_target(
        repo=str(workspace),
        workspace_dir=workspace,
        ref=intended_ref,
    )

    assert acquired.workspace_dir == workspace.resolve()
    assert acquired.mode == "existing"
    assert acquired.ref == intended_ref
    assert acquired.commit_sha == original_head
    assert _git(workspace, "rev-parse", "HEAD") == original_head
    assert _git(workspace, "branch", "--show-current") == original_branch
    assert _git(workspace, "status", "--porcelain=v1") == original_status
    assert tracked.read_text(encoding="utf-8") == "uncommitted implementation\n"
    assert untracked.read_text(encoding="utf-8") == "untracked implementation\n"
    assert _git(workspace, "branch", "--list", intended_ref) == ""
