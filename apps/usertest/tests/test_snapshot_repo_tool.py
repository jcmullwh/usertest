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


def _normalize_snapshot_plan(stdout: str) -> str:
    stdout = stdout.replace("\r\n", "\n")
    lines: list[str] = []
    for line in stdout.splitlines():
        if line.startswith("- time_utc: "):
            lines.append("- time_utc: <TIME>")
        elif line.startswith("- repo_root: "):
            lines.append("- repo_root: <REPO_ROOT>")
        else:
            lines.append(line)
    return "\n".join(lines).strip() + "\n"


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


def test_snapshot_repo_plan_only_does_not_write_archive(tmp_path: Path) -> None:
    repo_root = find_repo_root(Path(__file__).resolve())

    target_repo = tmp_path / "repo_plan_only"
    target_repo.mkdir(parents=True, exist_ok=True)

    subprocess.run(
        ["git", "-C", str(target_repo), "init"],
        capture_output=True,
        text=True,
        check=True,
    )

    (target_repo / "file.txt").write_text("ok", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(target_repo), "add", "file.txt"],
        capture_output=True,
        text=True,
        check=True,
    )

    out_zip = tmp_path / "plan_only.zip"
    proc = _run_snapshot_repo(
        repo_root=repo_root,
        args=[
            "--repo-root",
            str(target_repo),
            "--out",
            str(out_zip),
            "--plan-only",
        ],
    )

    assert proc.returncode == 0, proc.stderr
    assert "SNAPSHOT PLAN" in proc.stdout
    assert "Plan-only" in proc.stdout
    assert not out_zip.exists()


def test_snapshot_repo_dry_run_does_not_require_out(tmp_path: Path) -> None:
    repo_root = find_repo_root(Path(__file__).resolve())

    target_repo = tmp_path / "repo_dry_run"
    target_repo.mkdir(parents=True, exist_ok=True)

    subprocess.run(
        ["git", "-C", str(target_repo), "init"],
        capture_output=True,
        text=True,
        check=True,
    )

    (target_repo / "file.txt").write_text("ok", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(target_repo), "add", "file.txt"],
        capture_output=True,
        text=True,
        check=True,
    )

    proc = _run_snapshot_repo(
        repo_root=repo_root,
        args=[
            "--repo-root",
            str(target_repo),
            "--dry-run",
        ],
    )

    assert proc.returncode == 0, proc.stderr
    assert "SNAPSHOT PLAN" in proc.stdout
    assert "Dry-run" in proc.stdout
    assert not list(tmp_path.glob("*.zip"))


def test_snapshot_repo_plan_output_tracked_only_prints_excluded_untracked_zero(tmp_path: Path) -> None:
    repo_root = find_repo_root(Path(__file__).resolve())

    target_repo = tmp_path / "repo_plan_tracked_only_clean"
    target_repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "-C", str(target_repo), "init"],
        capture_output=True,
        text=True,
        check=True,
    )

    (target_repo / "file.txt").write_text("ok", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(target_repo), "add", "file.txt"],
        capture_output=True,
        text=True,
        check=True,
    )

    proc = _run_snapshot_repo(
        repo_root=repo_root,
        args=["--repo-root", str(target_repo), "--tracked-only", "--plan-only"],
    )
    assert proc.returncode == 0, proc.stderr

    normalized = _normalize_snapshot_plan(proc.stdout)
    assert normalized == (
        "SNAPSHOT PLAN\n"
        "- time_utc: <TIME>\n"
        "- repo_root: <REPO_ROOT>\n"
        "- out: <none>\n"
        "- archive_paths: repo-relative\n"
        "- default_untracked: include untracked (not ignored); pass --tracked-only to exclude\n"
        "- default_gitignore_files: excluded (avoid sharing ignore rules); pass --include-gitignore-files to include\n"
        "- tracked_only: True\n"
        "- include_ignored: False\n"
        "- include_gitignore_files: False\n"
        "- verify: True\n"
        "- plan_only: True\n"
        "- dry_run: False\n"
        "- files: 1\n"
        "- excluded_gitignores: 0\n"
        "- excluded_ignored: 0\n"
        "- excluded_outputs: 0\n"
        "- excluded_untracked: 0\n"
        "\n"
        "Plan-only: no archive written.\n"
    )


def test_snapshot_repo_plan_output_tracked_only_counts_untracked_excluded(tmp_path: Path) -> None:
    repo_root = find_repo_root(Path(__file__).resolve())

    target_repo = tmp_path / "repo_plan_tracked_only_untracked"
    target_repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "-C", str(target_repo), "init"],
        capture_output=True,
        text=True,
        check=True,
    )

    (target_repo / "file.txt").write_text("ok", encoding="utf-8")
    (target_repo / "untracked.txt").write_text("untracked", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(target_repo), "add", "file.txt"],
        capture_output=True,
        text=True,
        check=True,
    )

    proc = _run_snapshot_repo(
        repo_root=repo_root,
        args=["--repo-root", str(target_repo), "--tracked-only", "--plan-only"],
    )
    assert proc.returncode == 0, proc.stderr

    normalized = _normalize_snapshot_plan(proc.stdout)
    assert normalized == (
        "SNAPSHOT PLAN\n"
        "- time_utc: <TIME>\n"
        "- repo_root: <REPO_ROOT>\n"
        "- out: <none>\n"
        "- archive_paths: repo-relative\n"
        "- default_untracked: include untracked (not ignored); pass --tracked-only to exclude\n"
        "- default_gitignore_files: excluded (avoid sharing ignore rules); pass --include-gitignore-files to include\n"
        "- tracked_only: True\n"
        "- include_ignored: False\n"
        "- include_gitignore_files: False\n"
        "- verify: True\n"
        "- plan_only: True\n"
        "- dry_run: False\n"
        "- files: 1\n"
        "- excluded_gitignores: 0\n"
        "- excluded_ignored: 0\n"
        "- excluded_outputs: 0\n"
        "- excluded_untracked: 1\n"
        "\n"
        "Plan-only: no archive written.\n"
    )


def test_snapshot_repo_list_included_is_deterministic_and_sorted(tmp_path: Path) -> None:
    repo_root = find_repo_root(Path(__file__).resolve())

    target_repo = tmp_path / "repo_list_included"
    target_repo.mkdir(parents=True, exist_ok=True)

    subprocess.run(
        ["git", "-C", str(target_repo), "init"],
        capture_output=True,
        text=True,
        check=True,
    )

    (target_repo / "normal.txt").write_text("ok", encoding="utf-8")
    (target_repo / "untracked.txt").write_text("untracked", encoding="utf-8")
    (target_repo / "ignored.txt").write_text("ignored-tracked", encoding="utf-8")
    (target_repo / "ignored_untracked.txt").write_text("ignored-untracked", encoding="utf-8")
    (target_repo / ".gitignore").write_text("ignored*.txt\n", encoding="utf-8")

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

    proc = _run_snapshot_repo(
        repo_root=repo_root,
        args=[
            "--repo-root",
            str(target_repo),
            "--list-included",
        ],
    )
    assert proc.returncode == 0, proc.stderr

    included = [line for line in proc.stdout.splitlines() if line.strip()]
    assert included == ["normal.txt", "untracked.txt"]


def test_snapshot_repo_list_excluded_has_reason_codes(tmp_path: Path) -> None:
    repo_root = find_repo_root(Path(__file__).resolve())

    target_repo = tmp_path / "repo_list_excluded"
    target_repo.mkdir(parents=True, exist_ok=True)

    subprocess.run(
        ["git", "-C", str(target_repo), "init"],
        capture_output=True,
        text=True,
        check=True,
    )

    (target_repo / "normal.txt").write_text("ok", encoding="utf-8")
    (target_repo / "untracked.txt").write_text("untracked", encoding="utf-8")
    (target_repo / "ignored.txt").write_text("ignored-tracked", encoding="utf-8")
    (target_repo / "ignored_untracked.txt").write_text("ignored-untracked", encoding="utf-8")
    (target_repo / ".gitignore").write_text("ignored*.txt\n", encoding="utf-8")

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

    proc = _run_snapshot_repo(
        repo_root=repo_root,
        args=[
            "--repo-root",
            str(target_repo),
            "--tracked-only",
            "--list-excluded",
        ],
    )
    assert proc.returncode == 0, proc.stderr

    excluded = [line for line in proc.stdout.splitlines() if line.strip()]
    assert excluded == [
        ".gitignore\tgitignore_file",
        "ignored.txt\tgitignored",
        "ignored_untracked.txt\tgitignored",
        "untracked.txt\tuntracked_excluded",
    ]


def test_snapshot_repo_list_limit_applies_to_listing_output(tmp_path: Path) -> None:
    repo_root = find_repo_root(Path(__file__).resolve())

    target_repo = tmp_path / "repo_list_limit"
    target_repo.mkdir(parents=True, exist_ok=True)

    subprocess.run(
        ["git", "-C", str(target_repo), "init"],
        capture_output=True,
        text=True,
        check=True,
    )

    (target_repo / "b.txt").write_text("b", encoding="utf-8")
    (target_repo / "a.txt").write_text("a", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(target_repo), "add", "a.txt", "b.txt"],
        capture_output=True,
        text=True,
        check=True,
    )

    proc = _run_snapshot_repo(
        repo_root=repo_root,
        args=[
            "--repo-root",
            str(target_repo),
            "--list-included",
            "--list-limit",
            "1",
        ],
    )
    assert proc.returncode == 0, proc.stderr

    lines = [line for line in proc.stdout.splitlines() if line.strip()]
    assert lines == ["a.txt"]


def test_snapshot_repo_non_git_repo_has_actionable_hint(tmp_path: Path) -> None:
    repo_root = find_repo_root(Path(__file__).resolve())

    non_repo = tmp_path / "not_a_repo"
    non_repo.mkdir(parents=True, exist_ok=True)
    (non_repo / "file.txt").write_text("ok", encoding="utf-8")

    out_zip = tmp_path / "out.zip"
    proc = _run_snapshot_repo(
        repo_root=repo_root,
        args=[
            "--repo-root",
            str(non_repo),
            "--out",
            str(out_zip),
        ],
    )

    assert proc.returncode == 2
    assert "ERROR:" in proc.stderr
    assert "not a git repository" in proc.stderr.lower()
    assert "--repo-root" in proc.stderr
    assert "Traceback" not in proc.stderr


def test_snapshot_repo_missing_repo_root_is_flag_aware(tmp_path: Path) -> None:
    repo_root = find_repo_root(Path(__file__).resolve())

    missing_repo_root = tmp_path / "missing_repo_root"
    out_zip = tmp_path / "out.zip"
    proc = _run_snapshot_repo(
        repo_root=repo_root,
        args=[
            "--repo-root",
            str(missing_repo_root),
            "--out",
            str(out_zip),
        ],
    )

    assert proc.returncode == 2
    assert "ERROR:" in proc.stderr
    assert "--repo-root" in proc.stderr
    assert "does not exist" in proc.stderr.lower()
    assert "fatal:" not in proc.stderr.lower()
    assert "Traceback" not in proc.stderr
    assert "SNAPSHOT PLAN" not in proc.stdout


def test_snapshot_repo_repo_root_file_is_flag_aware(tmp_path: Path) -> None:
    repo_root = find_repo_root(Path(__file__).resolve())

    repo_root_file = tmp_path / "repo_root_file"
    repo_root_file.write_text("not a repo", encoding="utf-8")

    out_zip = tmp_path / "out.zip"
    proc = _run_snapshot_repo(
        repo_root=repo_root,
        args=[
            "--repo-root",
            str(repo_root_file),
            "--out",
            str(out_zip),
        ],
    )

    assert proc.returncode == 2
    assert "ERROR:" in proc.stderr
    assert "--repo-root" in proc.stderr
    assert "not a directory" in proc.stderr.lower()
    assert "fatal:" not in proc.stderr.lower()
    assert "Traceback" not in proc.stderr
    assert "SNAPSHOT PLAN" not in proc.stdout
