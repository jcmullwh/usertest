from __future__ import annotations

import subprocess
import sys
import zipfile
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


def test_snapshot_repo_out_requires_zip_suffix(tmp_path: Path) -> None:
    repo_root = find_repo_root(Path(__file__).resolve())
    out_path = tmp_path / "snapshot"

    proc = _run_snapshot_repo(
        repo_root=repo_root,
        args=["--repo-root", str(repo_root), "--out", str(out_path)],
    )

    assert proc.returncode == 2
    assert "ERROR:" in proc.stderr
    assert "zip" in proc.stderr.lower()
    assert "Traceback" not in proc.stderr
    assert "SNAPSHOT PLAN" not in proc.stdout


def test_snapshot_repo_parent_collision_has_no_traceback(tmp_path: Path) -> None:
    repo_root = find_repo_root(Path(__file__).resolve())
    parent_file = tmp_path / "parent_file"
    parent_file.write_text("not a dir", encoding="utf-8")

    out_path = parent_file / "snapshot.zip"

    proc = _run_snapshot_repo(
        repo_root=repo_root,
        args=["--repo-root", str(repo_root), "--out", str(out_path)],
    )

    assert proc.returncode == 2
    assert "ERROR:" in proc.stderr
    assert "not a directory" in proc.stderr.lower()
    assert "Traceback" not in proc.stderr
    assert "SNAPSHOT PLAN" not in proc.stdout


def test_snapshot_repo_excludes_tracked_but_ignored_files_by_default(tmp_path: Path) -> None:
    repo_root = find_repo_root(Path(__file__).resolve())

    target_repo = tmp_path / "repo"
    target_repo.mkdir(parents=True, exist_ok=True)

    subprocess.run(
        ["git", "-C", str(target_repo), "init"],
        capture_output=True,
        text=True,
        check=True,
    )

    (target_repo / "normal.txt").write_text("ok", encoding="utf-8")
    (target_repo / "ignored.txt").write_text("ignore-me", encoding="utf-8")
    (target_repo / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")

    subprocess.run(
        ["git", "-C", str(target_repo), "add", "normal.txt", ".gitignore"],
        capture_output=True,
        text=True,
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(target_repo), "add", "-f", "ignored.txt"],
        capture_output=True,
        text=True,
        check=True,
    )

    out_zip = tmp_path / "snapshot.zip"
    proc = _run_snapshot_repo(
        repo_root=repo_root,
        args=["--repo-root", str(target_repo), "--out", str(out_zip)],
    )
    assert proc.returncode == 0, proc.stderr

    with zipfile.ZipFile(out_zip) as zf:
        names = set(zf.namelist())
    assert "normal.txt" in names
    assert "ignored.txt" not in names

    out_zip_ignored = tmp_path / "snapshot_include_ignored.zip"
    proc2 = _run_snapshot_repo(
        repo_root=repo_root,
        args=[
            "--repo-root",
            str(target_repo),
            "--out",
            str(out_zip_ignored),
            "--include-ignored",
        ],
    )
    assert proc2.returncode == 0, proc2.stderr

    with zipfile.ZipFile(out_zip_ignored) as zf:
        names2 = set(zf.namelist())
    assert "normal.txt" in names2
    assert "ignored.txt" in names2
