from __future__ import annotations

from pathlib import Path

from run_artifacts.path_normalization import (
    agent_path_for_staged_file,
    agent_path_join,
    ensure_runs_usertest_exists,
    normalize_agent_path,
)


def test_normalize_agent_path() -> None:
    assert normalize_agent_path("foo/bar") == "foo/bar"
    assert normalize_agent_path("foo\\bar") == "foo/bar"
    assert normalize_agent_path("C:\\foo\\bar") == "C:/foo/bar"
    assert normalize_agent_path("foo//bar") == "foo/bar"
    assert normalize_agent_path("") == ""

def test_agent_path_join() -> None:
    assert agent_path_join("foo", "bar") == "foo/bar"
    assert agent_path_join("foo/", "bar") == "foo/bar"
    assert agent_path_join("foo", "/bar") == "foo/bar"
    assert agent_path_join("foo/", "/bar") == "foo/bar"
    assert agent_path_join("C:\\foo", "bar\\baz") == "C:/foo/bar/baz"
    assert agent_path_join("/", "workspace") == "/workspace"
    assert agent_path_join("\\", "workspace") == "/workspace"

def test_ensure_runs_usertest_exists(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    
    runs_usertest = ensure_runs_usertest_exists(workspace)
    assert runs_usertest == workspace / "runs" / "usertest"
    assert runs_usertest.is_dir()
    assert (workspace / "runs").is_dir()

def test_agent_path_for_staged_file(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    staged = run_dir / "subdir" / "file.txt"
    staged.parent.mkdir()
    staged.touch()
    
    # With mount
    assert agent_path_for_staged_file(
        staged, run_dir=run_dir, run_dir_mount="/run_dir"
    ) == "/run_dir/subdir/file.txt"
    
    # Without mount
    normalized_abs = normalize_agent_path(str(staged.resolve()))
    assert agent_path_for_staged_file(
        staged, run_dir=run_dir, run_dir_mount=None
    ) == normalized_abs

    # Empty mount
    assert agent_path_for_staged_file(
        staged, run_dir=run_dir, run_dir_mount=""
    ) == "/run_dir/subdir/file.txt"
