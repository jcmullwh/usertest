from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from runner_core import target_acquire


def _init_git_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "-C", str(path), "init"], check=True, capture_output=True, text=True)
    subprocess.run(
        ["git", "-C", str(path), "config", "user.email", "usertest@local"],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "-C", str(path), "config", "user.name", "usertest"],
        check=True,
        capture_output=True,
        text=True,
    )
    (path / "README.md").write_text("# repo\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(path), "add", "-A"],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "-C", str(path), "commit", "-m", "init"],
        check=True,
        capture_output=True,
        text=True,
    )


def _git(path: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(path), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def test_git_clone_supports_no_local_flag(monkeypatch) -> None:
    recorded: list[list[str]] = []

    class _Proc:
        returncode = 0
        stdout = ""
        stderr = ""

    def _fake_run(argv: list[str], **_kwargs: object) -> _Proc:
        recorded.append(list(argv))
        return _Proc()

    monkeypatch.setattr(target_acquire.subprocess, "run", _fake_run)

    target_acquire._git_clone(repo="C:/src/repo", dest_dir=Path("C:/tmp/dest"), no_local=True)

    assert recorded == [["git", "clone", "--no-local", "C:/src/repo", str(Path("C:/tmp/dest"))]]


def test_acquire_target_plain_directory_with_isolated_home(
    tmp_path: Path, monkeypatch
) -> None:
    src = tmp_path / "target_repo"
    src.mkdir(parents=True)
    (src / "README.md").write_text("# repo\n", encoding="utf-8")

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))

    acquired = target_acquire.acquire_target(
        repo=str(src),
        dest_dir=tmp_path / "workspace",
        ref=None,
    )

    assert acquired.mode == "copy"
    safe_dir = str(acquired.workspace_dir.resolve()).replace("\\", "/")
    proc = subprocess.run(
        [
            "git",
            "-c",
            f"safe.directory={safe_dir}",
            "-C",
            str(acquired.workspace_dir),
            "status",
            "--porcelain",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout


def test_acquire_target_local_git_repo_uses_safe_clone_and_connectivity_check(
    tmp_path: Path, monkeypatch
) -> None:
    src = tmp_path / "src_repo"
    _init_git_repo(src)

    clone_calls: list[bool] = []
    connectivity_checks: list[Path] = []

    original_git_clone = target_acquire._git_clone
    original_verify = target_acquire._verify_git_workspace_connectivity

    def _wrapped_git_clone(*, repo: str, dest_dir: Path, no_local: bool = False) -> None:
        clone_calls.append(no_local)
        original_git_clone(repo=repo, dest_dir=dest_dir, no_local=no_local)

    def _wrapped_verify(*, cwd: Path) -> None:
        connectivity_checks.append(cwd)
        original_verify(cwd=cwd)

    monkeypatch.setattr(target_acquire, "_git_clone", _wrapped_git_clone)
    monkeypatch.setattr(target_acquire, "_verify_git_workspace_connectivity", _wrapped_verify)

    dest = tmp_path / "workspace"
    acquired = target_acquire.acquire_target(repo=str(src), dest_dir=dest, ref=None)
    try:
        assert acquired.mode == "git"
        assert clone_calls == [True]
        assert connectivity_checks == [acquired.workspace_dir]

        proc = subprocess.run(
            [
                "git",
                "-C",
                str(acquired.workspace_dir),
                "fsck",
                "--connectivity-only",
                "--no-dangling",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 0, proc.stderr or proc.stdout
    finally:
        shutil.rmtree(acquired.workspace_dir, ignore_errors=True)


def test_acquire_target_fetches_ref_reachable_only_from_source_remote_tracking_ref(
    tmp_path: Path,
) -> None:
    src = tmp_path / "src_repo"
    _init_git_repo(src)
    primary_branch = _git(src, "branch", "--show-current")

    _git(src, "checkout", "-b", "temporary-frontier")
    (src / "frontier.txt").write_text("remote-tracking frontier\n", encoding="utf-8")
    _git(src, "add", "frontier.txt")
    _git(src, "commit", "-m", "frontier")
    frontier = _git(src, "rev-parse", "HEAD")
    _git(src, "update-ref", "refs/remotes/origin/dev", frontier)
    _git(src, "checkout", primary_branch)
    _git(src, "update-ref", "-d", "refs/heads/temporary-frontier")

    assert _git(src, "rev-parse", "refs/remotes/origin/dev") == frontier
    assert _git(src, "branch", "--contains", frontier) == ""

    dest = tmp_path / "workspace"
    acquired = target_acquire.acquire_target(repo=str(src), dest_dir=dest, ref=frontier)
    try:
        assert acquired.mode == "git"
        assert acquired.commit_sha == frontier
        assert (acquired.workspace_dir / "frontier.txt").read_text(encoding="utf-8") == (
            "remote-tracking frontier\n"
        )
        assert _git(acquired.workspace_dir, "branch", "--show-current") == ""
    finally:
        shutil.rmtree(acquired.workspace_dir, ignore_errors=True)
