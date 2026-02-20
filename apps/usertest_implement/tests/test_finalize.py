from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from usertest_implement.finalize import finalize_commit, finalize_push


def _run(argv: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, cwd=str(cwd), capture_output=True, text=True, check=False)


def _write_json(path: Path, obj: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


@pytest.mark.skipif(shutil.which("git") is None, reason="git not available")
def test_finalize_commit_writes_git_ref(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True)
    assert _run(["git", "init"], cwd=workspace).returncode == 0
    assert _run(["git", "config", "user.name", "test"], cwd=workspace).returncode == 0
    assert _run(["git", "config", "user.email", "test@example.com"], cwd=workspace).returncode == 0
    (workspace / "README.md").write_text("hello\n", encoding="utf-8")
    assert _run(["git", "add", "-A"], cwd=workspace).returncode == 0
    assert _run(["git", "commit", "-m", "init"], cwd=workspace).returncode == 0
    base_sha = _run(["git", "rev-parse", "HEAD"], cwd=workspace).stdout.strip()
    assert base_sha

    (workspace / "README.md").write_text("hello world\n", encoding="utf-8")

    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True)
    _write_json(
        run_dir / "workspace_ref.json",
        {"schema_version": 1, "workspace_dir": str(workspace)},
    )
    _write_json(run_dir / "target_ref.json", {"commit_sha": base_sha})

    git_ref = finalize_commit(
        run_dir=run_dir,
        branch="backlog/blg-001-deadbeef",
        commit_message="BLG-001: update",
    )
    assert git_ref["commit_attempted"] is True
    assert git_ref["commit_performed"] is True
    assert isinstance(git_ref["head_commit"], str) and git_ref["head_commit"]

    on_branch = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=workspace).stdout.strip()
    assert on_branch == "backlog/blg-001-deadbeef"
    ref_path = run_dir / "git_ref.json"
    assert ref_path.exists()


@pytest.mark.skipif(shutil.which("git") is None, reason="git not available")
def test_finalize_push_pushes_to_bare_remote(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True)
    assert _run(["git", "init"], cwd=workspace).returncode == 0
    assert _run(["git", "config", "user.name", "test"], cwd=workspace).returncode == 0
    assert _run(["git", "config", "user.email", "test@example.com"], cwd=workspace).returncode == 0
    (workspace / "README.md").write_text("hello\n", encoding="utf-8")
    assert _run(["git", "add", "-A"], cwd=workspace).returncode == 0
    assert _run(["git", "commit", "-m", "init"], cwd=workspace).returncode == 0

    (workspace / "README.md").write_text("hello world\n", encoding="utf-8")

    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True)
    _write_json(
        run_dir / "workspace_ref.json",
        {"schema_version": 1, "workspace_dir": str(workspace)},
    )

    branch = "backlog/blg-002-deadbeef"
    finalize_commit(run_dir=run_dir, branch=branch, commit_message="BLG-002: update")

    remote = tmp_path / "remote.git"
    assert _run(["git", "init", "--bare", str(remote)], cwd=tmp_path).returncode == 0

    push_ref = finalize_push(
        run_dir=run_dir,
        remote_name="origin",
        remote_url=str(remote),
        candidate_repo_dirs=[],
        branch=branch,
        force_with_lease=False,
    )
    assert push_ref["pushed"] is True
    assert (run_dir / "push_ref.json").exists()

    # Verify the branch exists in the bare remote.
    verify = _run(
        ["git", "--git-dir", str(remote), "show-ref", "--verify", f"refs/heads/{branch}"],
        cwd=tmp_path,
    )
    assert verify.returncode == 0, verify.stderr or verify.stdout

