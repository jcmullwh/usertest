from __future__ import annotations

from pathlib import Path

from runner_core import find_repo_root

from usertest.cli import _load_runner_config


def test_default_runs_dir_is_runs_usertest() -> None:
    repo_root = find_repo_root(Path(__file__).resolve())
    cfg = _load_runner_config(repo_root)
    assert cfg.runs_dir == repo_root / "runs" / "usertest"
