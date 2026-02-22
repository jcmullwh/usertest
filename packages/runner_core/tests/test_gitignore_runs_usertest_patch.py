from __future__ import annotations

from pathlib import Path

from runner_core.runner import (
    _RUNS_USERTEST_GITIGNORE_MARKER,
    _maybe_patch_workspace_gitignore_for_runs_usertest,
)


def test_gitignore_patch_appends_unignore_block_when_runs_ignored(tmp_path: Path) -> None:
    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir(parents=True, exist_ok=True)
    gitignore_path = workspace_dir / ".gitignore"
    gitignore_path.write_text("runs/\n", encoding="utf-8", newline="\n")

    _maybe_patch_workspace_gitignore_for_runs_usertest(workspace_dir=workspace_dir)

    patched = gitignore_path.read_text(encoding="utf-8")
    assert _RUNS_USERTEST_GITIGNORE_MARKER in patched
    assert "!runs/" in patched
    assert "runs/*" in patched
    assert "!runs/usertest/" in patched
    assert "!runs/usertest/**" in patched


def test_gitignore_patch_is_idempotent(tmp_path: Path) -> None:
    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir(parents=True, exist_ok=True)
    gitignore_path = workspace_dir / ".gitignore"
    gitignore_path.write_text("runs/\n", encoding="utf-8", newline="\n")

    _maybe_patch_workspace_gitignore_for_runs_usertest(workspace_dir=workspace_dir)
    _maybe_patch_workspace_gitignore_for_runs_usertest(workspace_dir=workspace_dir)

    patched = gitignore_path.read_text(encoding="utf-8")
    assert patched.count(_RUNS_USERTEST_GITIGNORE_MARKER) == 1


def test_gitignore_patch_does_not_modify_when_runs_not_ignored(tmp_path: Path) -> None:
    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir(parents=True, exist_ok=True)
    gitignore_path = workspace_dir / ".gitignore"
    original = "dist/\n"
    gitignore_path.write_text(original, encoding="utf-8", newline="\n")

    _maybe_patch_workspace_gitignore_for_runs_usertest(workspace_dir=workspace_dir)

    assert gitignore_path.read_text(encoding="utf-8") == original
