from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from runner_core.pip_target import (
    is_pip_repo_input,
    parse_pip_repo_input,
    write_pip_target_workspace,
)


@dataclass(frozen=True)
class AcquiredTarget:
    workspace_dir: Path
    repo_input: str
    ref: str | None
    commit_sha: str
    mode: str  # "git" | "copy"


COPYTREE_ALWAYS_IGNORE: frozenset[str] = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".bzr",
        ".venv",
        "venv",
        "__pypackages__",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".tox",
        ".nox",
        "node_modules",
        ".pdm-python",
        ".pdm-build",
        ".scaffold",
        ".idea",
        ".vscode",
    }
)

COPYTREE_ROOT_ONLY_IGNORE: frozenset[str] = frozenset({"runs", "dist", "build"})


WINDOWS_MAX_PATH = 260
WINDOWS_MAX_DIR_PATH = 248


def _is_windows() -> bool:
    return os.name == "nt"


def _ignore_names_for_copytree(*, src_root: Path) -> Callable[[str, list[str]], set[str]]:
    try:
        src_root_resolved = src_root.resolve()
    except OSError:
        src_root_resolved = src_root

    def _ignore(dir_path: str, names: list[str]) -> set[str]:
        ignored: set[str] = {name for name in names if name in COPYTREE_ALWAYS_IGNORE}

        try:
            dir_resolved = Path(dir_path).resolve()
        except OSError:
            dir_resolved = Path(dir_path)

        if dir_resolved == src_root_resolved:
            ignored.update({name for name in names if name in COPYTREE_ROOT_ONLY_IGNORE})

        return ignored

    return _ignore


def _run_git(args: list[str], *, cwd: Path) -> str:
    proc = subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, check=False
    )
    if proc.returncode != 0:
        msg = proc.stderr.strip() or proc.stdout.strip()
        if not msg:
            msg = f"git failed: {' '.join(args)}"
        raise RuntimeError(msg)
    return proc.stdout.strip()


def _looks_like_existing_path(repo: str) -> bool:
    try:
        return Path(repo).expanduser().exists()
    except OSError:
        return False


def _relocate_dest_if_within_source(*, src: Path, dest_dir: Path) -> Path:
    try:
        src_resolved = src.resolve()
        dest_resolved = dest_dir.resolve()
    except OSError:
        return dest_dir

    if not dest_resolved.is_relative_to(src_resolved):
        return dest_dir

    base = Path(tempfile.gettempdir()) / "usertest_workspaces"
    return base / dest_dir.name


def _git_clone(*, repo: str, dest_dir: Path) -> None:
    proc = subprocess.run(
        ["git", "clone", repo, str(dest_dir)],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode == 0:
        return
    msg = proc.stderr.strip() or proc.stdout.strip()
    if not msg:
        msg = f"git clone failed (exit {proc.returncode})"
    raise RuntimeError(msg)


def _is_windows_path_too_long_error(msg: str) -> bool:
    lowered = msg.lower()
    return "filename too long" in lowered or "file name too long" in lowered


def _windows_path_lengths_ok(*, dest_dir: Path, max_file_rel: int, max_dir_rel: int) -> bool:
    base = len(str(dest_dir)) + 1
    return (base + max_file_rel) < WINDOWS_MAX_PATH and (base + max_dir_rel) < WINDOWS_MAX_DIR_PATH


def _max_tracked_relpath_lengths(*, src: Path) -> tuple[int, int]:
    """
    Returns (max_file_rel_len, max_dir_rel_len) for tracked files in a git repo.
    """

    try:
        out = _run_git(["ls-files"], cwd=src)
    except Exception:
        return (0, 0)

    max_file = 0
    max_dir = 0
    for line in out.splitlines():
        if not line:
            continue
        max_file = max(max_file, len(line))
        if "/" in line:
            max_dir = max(max_dir, len(line.rsplit("/", maxsplit=1)[0]))
    return (max_file, max_dir)


def _max_copytree_relpath_lengths(*, src_root: Path) -> tuple[int, int]:
    """
    Returns (max_file_rel_len, max_dir_rel_len) for paths that copytree would copy.
    """

    max_file = 0
    max_dir = 0

    try:
        src_root_resolved = src_root.resolve()
    except OSError:
        src_root_resolved = src_root

    for dir_path_str, dirnames, filenames in os.walk(src_root_resolved, topdown=True):
        dir_path = Path(dir_path_str)
        try:
            rel_dir = dir_path.relative_to(src_root_resolved)
        except Exception:
            rel_dir = dir_path.relative_to(src_root)

        # Prune ignored dirs in-place (to avoid walking them).
        keep_dirs: list[str] = []
        for name in dirnames:
            if name in COPYTREE_ALWAYS_IGNORE:
                continue
            if rel_dir == Path(".") and name in COPYTREE_ROOT_ONLY_IGNORE:
                continue
            keep_dirs.append(name)
        dirnames[:] = keep_dirs

        rel_dir_posix = "" if rel_dir == Path(".") else rel_dir.as_posix()
        max_dir = max(max_dir, len(rel_dir_posix))

        for name in filenames:
            if name in COPYTREE_ALWAYS_IGNORE:
                continue
            if rel_dir == Path(".") and name in COPYTREE_ROOT_ONLY_IGNORE:
                continue
            rel_file = name if not rel_dir_posix else f"{rel_dir_posix}/{name}"
            max_file = max(max_file, len(rel_file))

    return (max_file, max_dir)


def _workspace_candidates(*, dest_dir: Path) -> list[Path]:
    tmp = Path(tempfile.gettempdir())
    digest = hashlib.sha1(str(dest_dir).encode("utf-8")).hexdigest()[:12]  # noqa: S324
    return [
        tmp / "usertest_workspaces" / dest_dir.name,
        tmp / "ut" / dest_dir.name,
        tmp / "ut" / f"ws_{digest}",
    ]


def _relocate_dest_for_windows_longpaths(
    *,
    dest_dir: Path,
    max_file_rel: int | None,
    max_dir_rel: int | None,
) -> Path:
    if not _is_windows():
        return dest_dir

    candidates = _workspace_candidates(dest_dir=dest_dir)
    if max_file_rel is None or max_dir_rel is None:
        return candidates[-1]

    for candidate in candidates:
        if _windows_path_lengths_ok(
            dest_dir=candidate, max_file_rel=max_file_rel, max_dir_rel=max_dir_rel
        ):
            return candidate

    return candidates[-1]


def acquire_target(*, repo: str, dest_dir: Path, ref: str | None) -> AcquiredTarget:
    if is_pip_repo_input(repo):
        spec = parse_pip_repo_input(repo)
        dest_dir.parent.mkdir(parents=True, exist_ok=True)
        if dest_dir.exists():
            raise FileExistsError(dest_dir)
        dest_dir.mkdir(parents=True, exist_ok=False)
        try:
            write_pip_target_workspace(workspace_dir=dest_dir, repo_input=repo, spec=spec)
            _run_git(["init"], cwd=dest_dir)
            _run_git(["config", "user.email", "usertest@local"], cwd=dest_dir)
            _run_git(["config", "user.name", "usertest"], cwd=dest_dir)
            _run_git(["add", "-A"], cwd=dest_dir)
            _run_git(
                [
                    "commit",
                    "--allow-empty",
                    "--no-gpg-sign",
                    "--no-verify",
                    "-m",
                    "pip target",
                ],
                cwd=dest_dir,
            )
            sha = _run_git(["rev-parse", "HEAD"], cwd=dest_dir)
            return AcquiredTarget(
                workspace_dir=dest_dir,
                repo_input=repo,
                ref=ref,
                commit_sha=sha,
                mode="pip",
            )
        except Exception:
            shutil.rmtree(dest_dir, ignore_errors=True)
            raise

    is_local_path = _looks_like_existing_path(repo)
    src: Path | None = None

    if is_local_path:
        src = Path(repo).expanduser().resolve()
        if not src.is_dir():
            raise ValueError(f"--repo must be a directory or git URL, got file: {repo}")

        dest_dir = _relocate_dest_if_within_source(src=src, dest_dir=dest_dir)

        if _is_windows():
            git_dir = src / ".git"
            if git_dir.exists():
                max_file, max_dir = _max_tracked_relpath_lengths(src=src)
            else:
                max_file, max_dir = _max_copytree_relpath_lengths(src_root=src)

            if not _windows_path_lengths_ok(
                dest_dir=dest_dir, max_file_rel=max_file, max_dir_rel=max_dir
            ):
                dest_dir = _relocate_dest_for_windows_longpaths(
                    dest_dir=dest_dir, max_file_rel=max_file, max_dir_rel=max_dir
                )

    dest_dir.parent.mkdir(parents=True, exist_ok=True)
    if dest_dir.exists():
        raise FileExistsError(dest_dir)

    try:
        if is_local_path:
            assert src is not None

            # If it's a git repo, clone it. Otherwise, copy it and init git so agents that require
            # git metadata can still run.
            git_dir = src / ".git"
            if git_dir.exists():
                # A freshly-initialized repo can have .git/ but no commits yet; cloning that
                # results in a workspace with no HEAD (rev-parse HEAD fails). In that case,
                # fall back to copy+init so we can produce a usable workspace.
                try:
                    _run_git(["rev-parse", "--verify", "HEAD"], cwd=src)
                except Exception:
                    git_dir = None

            if git_dir is not None and git_dir.exists():
                try:
                    _git_clone(repo=str(src), dest_dir=dest_dir)
                except RuntimeError as e:
                    if _is_windows() and _is_windows_path_too_long_error(str(e)):
                        alt_dest = _relocate_dest_for_windows_longpaths(
                            dest_dir=dest_dir,
                            max_file_rel=None,
                            max_dir_rel=None,
                        )
                        alt_dest.parent.mkdir(parents=True, exist_ok=True)
                        _git_clone(repo=str(src), dest_dir=alt_dest)
                        dest_dir = alt_dest
                    else:
                        raise
                if ref is not None:
                    _run_git(["checkout", ref], cwd=dest_dir)
                sha = _run_git(["rev-parse", "HEAD"], cwd=dest_dir)
                return AcquiredTarget(
                    workspace_dir=dest_dir,
                    repo_input=repo,
                    ref=ref,
                    commit_sha=sha,
                    mode="git",
                )

            shutil.copytree(src, dest_dir, ignore=_ignore_names_for_copytree(src_root=src))
            _run_git(["init"], cwd=dest_dir)
            _run_git(["config", "user.email", "usertest@local"], cwd=dest_dir)
            _run_git(["config", "user.name", "usertest"], cwd=dest_dir)
            _run_git(["add", "-A"], cwd=dest_dir)
            _run_git(
                [
                    "commit",
                    "--allow-empty",
                    "--no-gpg-sign",
                    "--no-verify",
                    "-m",
                    "initial import",
                ],
                cwd=dest_dir,
            )
            sha = _run_git(["rev-parse", "HEAD"], cwd=dest_dir)
            return AcquiredTarget(
                workspace_dir=dest_dir,
                repo_input=repo,
                ref=ref,
                commit_sha=sha,
                mode="copy",
            )

        try:
            _git_clone(repo=repo, dest_dir=dest_dir)
        except RuntimeError as e:
            if _is_windows() and _is_windows_path_too_long_error(str(e)):
                alt_dest = _relocate_dest_for_windows_longpaths(
                    dest_dir=dest_dir,
                    max_file_rel=None,
                    max_dir_rel=None,
                )
                alt_dest.parent.mkdir(parents=True, exist_ok=True)
                _git_clone(repo=repo, dest_dir=alt_dest)
                dest_dir = alt_dest
            else:
                raise
        if ref is not None:
            _run_git(["checkout", ref], cwd=dest_dir)
        sha = _run_git(["rev-parse", "HEAD"], cwd=dest_dir)
        return AcquiredTarget(
            workspace_dir=dest_dir, repo_input=repo, ref=ref, commit_sha=sha, mode="git"
        )
    except Exception:
        shutil.rmtree(dest_dir, ignore_errors=True)
        raise
