from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from runner_core import find_repo_root


def _run_snapshot_repo(*, repo_root: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(repo_root / "tools" / "snapshot_repo.py"), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def test_snapshot_repo_existing_out_fails_without_printing_plan(tmp_path: Path) -> None:
    repo_root = find_repo_root(Path(__file__).resolve())
    out_path = tmp_path / "snapshot.zip"
    out_path.write_text("not a zip", encoding="utf-8")

    proc = _run_snapshot_repo(
        repo_root=repo_root,
        args=["--repo-root", str(repo_root), "--out", str(out_path)],
    )

    assert proc.returncode == 2
    assert "ERROR:" in proc.stderr
    assert "SNAPSHOT PLAN" not in proc.stdout


def test_snapshot_repo_out_directory_message_is_specific(tmp_path: Path) -> None:
    repo_root = find_repo_root(Path(__file__).resolve())
    out_dir = tmp_path / "out_dir"
    out_dir.mkdir(parents=True, exist_ok=True)

    proc = _run_snapshot_repo(
        repo_root=repo_root,
        args=["--repo-root", str(repo_root), "--out", str(out_dir)],
    )

    assert proc.returncode == 2
    assert "ERROR:" in proc.stderr
    assert "directory" in proc.stderr.lower()
    assert "--overwrite" not in proc.stderr
    assert "SNAPSHOT PLAN" not in proc.stdout

