from __future__ import annotations

import subprocess
from pathlib import Path

from runner_core.provenance import capture_runner_implementation_provenance


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def test_runner_implementation_provenance_distinguishes_clean_and_dirty_source(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    source = repo / "runner.py"
    source.write_text("print('one')\n", encoding="utf-8")
    _git(repo, "add", "runner.py")
    _git(repo, "commit", "-m", "initial")

    clean = capture_runner_implementation_provenance(repo)
    assert clean["available"] is True
    assert clean["dirty"] is False
    assert clean["untracked_file_count"] == 0

    source.write_text("print('two')\n", encoding="utf-8")
    (repo / "new.py").write_text("print('new')\n", encoding="utf-8")
    dirty = capture_runner_implementation_provenance(repo)

    assert dirty["available"] is True
    assert dirty["dirty"] is True
    assert dirty["tracked_diff_size_bytes"] > 0
    assert dirty["untracked_file_count"] == 1
    assert dirty["implementation_identity_sha256"] != clean["implementation_identity_sha256"]


def test_runner_implementation_provenance_reports_non_git_root(tmp_path: Path) -> None:
    result = capture_runner_implementation_provenance(tmp_path)

    assert result["available"] is False
    assert result["reason"] == "runner_repo_revision_unavailable"
