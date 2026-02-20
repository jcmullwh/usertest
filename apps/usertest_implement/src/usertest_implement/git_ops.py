from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class CommandResult:
    argv: list[str]
    returncode: int
    stdout: str
    stderr: str


def _run(argv: list[str], *, cwd: Path, check: bool) -> CommandResult:
    proc = subprocess.run(
        argv,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
    )
    result = CommandResult(
        argv=argv,
        returncode=proc.returncode,
        stdout=proc.stdout,
        stderr=proc.stderr,
    )
    if check and result.returncode != 0:
        msg = result.stderr.strip() or result.stdout.strip() or "command failed"
        raise RuntimeError(f"{' '.join(argv)}: {msg}")
    return result


def ensure_git_identity(workspace_dir: Path) -> None:
    _run(["git", "config", "user.name", "usertest-implement"], cwd=workspace_dir, check=True)
    _run(
        ["git", "config", "user.email", "usertest-implement@local"],
        cwd=workspace_dir,
        check=True,
    )


def branch_exists(workspace_dir: Path, branch: str) -> bool:
    result = _run(
        ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
        cwd=workspace_dir,
        check=False,
    )
    return result.returncode == 0


def checkout_branch(workspace_dir: Path, branch: str) -> None:
    if branch_exists(workspace_dir, branch):
        _run(["git", "checkout", branch], cwd=workspace_dir, check=True)
        return
    _run(["git", "checkout", "-b", branch], cwd=workspace_dir, check=True)


def status_porcelain(workspace_dir: Path) -> str:
    result = _run(["git", "status", "--porcelain"], cwd=workspace_dir, check=True)
    return result.stdout


def head_sha(workspace_dir: Path) -> str:
    result = _run(["git", "rev-parse", "HEAD"], cwd=workspace_dir, check=True)
    return result.stdout.strip()


def commit_all(workspace_dir: Path, *, message: str) -> str:
    _run(["git", "add", "-A"], cwd=workspace_dir, check=True)
    _run(["git", "commit", "--no-gpg-sign", "-m", message], cwd=workspace_dir, check=True)
    return head_sha(workspace_dir)


def ensure_remote(workspace_dir: Path, *, remote_name: str, remote_url: str) -> None:
    existing = _run(["git", "remote", "get-url", remote_name], cwd=workspace_dir, check=False)
    if existing.returncode == 0:
        _run(["git", "remote", "set-url", remote_name, remote_url], cwd=workspace_dir, check=True)
        return
    _run(["git", "remote", "add", remote_name, remote_url], cwd=workspace_dir, check=True)


def push_branch(
    workspace_dir: Path,
    *,
    remote_name: str,
    branch: str,
    force_with_lease: bool,
) -> CommandResult:
    argv = ["git", "push", "-u", remote_name, branch]
    if force_with_lease:
        argv.insert(2, "--force-with-lease")
    return _run(argv, cwd=workspace_dir, check=True)

