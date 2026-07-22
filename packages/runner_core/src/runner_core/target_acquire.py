from __future__ import annotations

import hashlib
import os
import shutil
import stat
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


@dataclass(frozen=True)
class _GitCloneOutcome:
    destination: Path
    failed_enospc_destination: Path | None = None
    original_enospc_error: str | None = None


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
    safe_dir = str(cwd.resolve()).replace("\\", "/")
    proc = subprocess.run(
        ["git", "-c", f"safe.directory={safe_dir}", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        check=False,
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


def _git_clone(*, repo: str, dest_dir: Path, no_local: bool = False) -> None:
    argv = ["git", "clone"]
    if no_local:
        argv.append("--no-local")
    argv.extend([repo, str(dest_dir)])
    proc = subprocess.run(
        argv,
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


def _verify_git_workspace_connectivity(*, cwd: Path) -> None:
    _run_git(["fsck", "--connectivity-only", "--no-dangling"], cwd=cwd)


def _is_windows_path_too_long_error(msg: str) -> bool:
    lowered = msg.lower()
    return "filename too long" in lowered or "file name too long" in lowered


def _is_windows_checkout_enospc_error(msg: str) -> bool:
    """Return whether Git reported the checkout failure seen on exhausted Windows volumes."""

    return "no space left on device" in msg.casefold()


def _windows_volume_identity(path: Path) -> str:
    """Return a comparable Windows drive or UNC anchor without requiring *path* to exist."""

    try:
        anchor = path.expanduser().resolve(strict=False).anchor
    except (OSError, RuntimeError):
        return ""
    return anchor.rstrip("\\/").casefold()


def _probe_workspace_parent(parent: Path) -> bool:
    """Check that a candidate parent can hold a workspace without touching the candidate."""

    probe_path: Path | None = None
    try:
        parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            dir=parent,
            prefix=".usertest_workspace_probe_",
            delete=False,
        ) as probe:
            probe_path = Path(probe.name)
        probe_path.unlink()
        return True
    except OSError:
        if probe_path is not None:
            try:
                probe_path.unlink(missing_ok=True)
            except OSError:
                pass
        return False


def _select_distinct_windows_workspace_candidate(
    *, failed_dest: Path, protected_source: Path | None
) -> Path | None:
    """Select one absent, writable workspace candidate on a known different volume."""

    failed_volume = _windows_volume_identity(failed_dest)
    if not failed_volume:
        return None

    for candidate in _workspace_candidates(dest_dir=failed_dest):
        candidate_volume = _windows_volume_identity(candidate)
        if not candidate_volume or candidate_volume == failed_volume:
            continue
        if protected_source is not None:
            try:
                if candidate.resolve(strict=False).is_relative_to(
                    protected_source.resolve(strict=False)
                ):
                    continue
            except (OSError, RuntimeError):
                continue
        # lexists rejects broken symlinks as well as ordinary pre-existing paths.
        if os.path.lexists(candidate):
            continue
        if not _probe_workspace_parent(candidate.parent):
            continue
        # The probe and clone are necessarily separate operations. Recheck immediately before
        # returning to narrow the race without ever deleting a path rejected by this selector.
        if os.path.lexists(candidate):
            continue
        return candidate
    return None


def _remove_readonly_path(func: Callable[[str], object], path: str, _exc_info: object) -> None:
    """Retry a failed Windows removal after clearing Git's read-only object-file bit."""

    os.chmod(path, os.stat(path).st_mode | stat.S_IWRITE)
    func(path)


def remove_acquired_workspace(path: Path) -> None:
    """Remove exactly one runner-owned workspace, including read-only Git pack files."""

    if path.is_symlink() or path.is_file():
        try:
            path.unlink(missing_ok=True)
        except PermissionError:
            path.chmod(path.stat().st_mode | stat.S_IWRITE)
            path.unlink(missing_ok=True)
    elif os.path.lexists(path):
        shutil.rmtree(path, onerror=_remove_readonly_path)


def _remove_acquisition_destination(path: Path) -> None:
    """Remove one exact, previously-absent destination created by this acquisition attempt."""

    remove_acquired_workspace(path)


def _enospc_recovery_error(
    *,
    original_error: str,
    failed_dest: Path,
    fallback_dest: Path | None,
    fallback_context: str,
) -> RuntimeError:
    fallback_path = str(fallback_dest) if fallback_dest is not None else "<none>"
    return RuntimeError(
        "Git clone failed on the preferred Windows workspace volume and ENOSPC recovery failed. "
        f"preferred destination: {failed_dest}; fallback destination: {fallback_path}; "
        f"original error: {original_error}; fallback context: {fallback_context}"
    )


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


def _git_clone_with_windows_recovery(
    *,
    repo: str,
    dest_dir: Path,
    no_local: bool,
    protected_source: Path | None,
    enospc_owned_destinations: list[Path],
) -> _GitCloneOutcome:
    """Clone once, retaining long-path recovery and adding one bounded ENOSPC reacquisition."""

    try:
        _git_clone(repo=repo, dest_dir=dest_dir, no_local=no_local)
        return _GitCloneOutcome(destination=dest_dir)
    except RuntimeError as initial_error:
        initial_message = str(initial_error)
        initial_cause = initial_error

    # Preserve the existing long-path recovery priority and behavior. In particular, a failure
    # of that established fallback is not treated as a fresh initial clone failure.
    if _is_windows() and _is_windows_path_too_long_error(initial_message):
        alt_dest = _relocate_dest_for_windows_longpaths(
            dest_dir=dest_dir,
            max_file_rel=None,
            max_dir_rel=None,
        )
        alt_dest.parent.mkdir(parents=True, exist_ok=True)
        _git_clone(repo=repo, dest_dir=alt_dest, no_local=no_local)
        return _GitCloneOutcome(destination=alt_dest)

    if not (_is_windows() and _is_windows_checkout_enospc_error(initial_message)):
        raise RuntimeError(initial_message) from initial_cause

    try:
        _remove_acquisition_destination(dest_dir)
    except OSError as cleanup_error:
        raise _enospc_recovery_error(
            original_error=initial_message,
            failed_dest=dest_dir,
            fallback_dest=None,
            fallback_context=f"could not remove the partial destination: {cleanup_error}",
        ) from cleanup_error
    if os.path.lexists(dest_dir):
        raise _enospc_recovery_error(
            original_error=initial_message,
            failed_dest=dest_dir,
            fallback_dest=None,
            fallback_context="partial destination still exists after removal",
        ) from initial_cause

    fallback_dest = _select_distinct_windows_workspace_candidate(
        failed_dest=dest_dir,
        protected_source=protected_source,
    )
    if fallback_dest is None:
        raise _enospc_recovery_error(
            original_error=initial_message,
            failed_dest=dest_dir,
            fallback_dest=None,
            fallback_context="no absent, writable candidate on a known different volume",
        ) from initial_cause

    # Reserve the exact candidate atomically. Git accepts an existing empty destination, and the
    # reservation distinguishes our cleanup ownership from a path created by another actor after
    # candidate selection.
    try:
        fallback_dest.mkdir(parents=False, exist_ok=False)
    except OSError as reservation_error:
        raise _enospc_recovery_error(
            original_error=initial_message,
            failed_dest=dest_dir,
            fallback_dest=fallback_dest,
            fallback_context=f"could not reserve the fallback destination: {reservation_error}",
        ) from reservation_error
    enospc_owned_destinations.append(fallback_dest)

    try:
        _git_clone(repo=repo, dest_dir=fallback_dest, no_local=no_local)
    except Exception as fallback_error:
        try:
            _remove_acquisition_destination(fallback_dest)
        except OSError as cleanup_error:
            fallback_context = (
                f"clone failed: {fallback_error}; partial fallback cleanup failed: {cleanup_error}"
            )
        else:
            fallback_context = f"clone failed: {fallback_error}"
        raise _enospc_recovery_error(
            original_error=initial_message,
            failed_dest=dest_dir,
            fallback_dest=fallback_dest,
            fallback_context=fallback_context,
        ) from fallback_error

    return _GitCloneOutcome(
        destination=fallback_dest,
        failed_enospc_destination=dest_dir,
        original_enospc_error=initial_message,
    )


def _raise_enospc_validation_error(
    *, clone_outcome: _GitCloneOutcome, validation_error: Exception
) -> None:
    if (
        clone_outcome.failed_enospc_destination is None
        or clone_outcome.original_enospc_error is None
    ):
        raise validation_error
    raise _enospc_recovery_error(
        original_error=clone_outcome.original_enospc_error,
        failed_dest=clone_outcome.failed_enospc_destination,
        fallback_dest=clone_outcome.destination,
        fallback_context=f"fallback workspace validation failed: {validation_error}",
    ) from validation_error


def acquire_target(*, repo: str, dest_dir: Path, ref: str | None) -> AcquiredTarget:
    if is_pip_repo_input(repo):
        spec = parse_pip_repo_input(repo)
        dest_dir.parent.mkdir(parents=True, exist_ok=True)
        if os.path.lexists(dest_dir):
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
            try:
                remove_acquired_workspace(dest_dir)
            except OSError:
                pass
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
    if os.path.lexists(dest_dir):
        raise FileExistsError(dest_dir)

    enospc_owned_destinations: list[Path] = []
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
                resolved_ref: str | None = None
                if ref is not None:
                    # A local clone does not necessarily transfer commits that are reachable only
                    # through the source repository's remote-tracking refs. Resolve the requested
                    # revision while the source has its complete ref namespace, then explicitly
                    # fetch that exact commit into the isolated workspace below. If it does not
                    # resolve, defer the authoritative checkout failure until after acquisition so
                    # an ENOSPC fallback can preserve both the original and validation failures.
                    try:
                        resolved_ref = _run_git(
                            ["rev-parse", "--verify", f"{ref}^{{commit}}"], cwd=src
                        )
                    except RuntimeError:
                        resolved_ref = None
                clone_outcome = _git_clone_with_windows_recovery(
                    repo=str(src),
                    dest_dir=dest_dir,
                    no_local=True,
                    protected_source=src,
                    enospc_owned_destinations=enospc_owned_destinations,
                )
                dest_dir = clone_outcome.destination
                try:
                    if resolved_ref is not None:
                        _run_git(["fetch", "--no-tags", str(src), resolved_ref], cwd=dest_dir)
                        _run_git(["checkout", "--detach", resolved_ref], cwd=dest_dir)
                    elif ref is not None:
                        _run_git(["checkout", ref], cwd=dest_dir)
                    _verify_git_workspace_connectivity(cwd=dest_dir)
                    sha = _run_git(["rev-parse", "HEAD"], cwd=dest_dir)
                except Exception as validation_error:
                    _raise_enospc_validation_error(
                        clone_outcome=clone_outcome,
                        validation_error=validation_error,
                    )
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

        clone_outcome = _git_clone_with_windows_recovery(
            repo=repo,
            dest_dir=dest_dir,
            no_local=False,
            protected_source=None,
            enospc_owned_destinations=enospc_owned_destinations,
        )
        dest_dir = clone_outcome.destination
        try:
            if ref is not None:
                _run_git(["checkout", ref], cwd=dest_dir)
            sha = _run_git(["rev-parse", "HEAD"], cwd=dest_dir)
        except Exception as validation_error:
            _raise_enospc_validation_error(
                clone_outcome=clone_outcome,
                validation_error=validation_error,
            )
        return AcquiredTarget(
            workspace_dir=dest_dir, repo_input=repo, ref=ref, commit_sha=sha, mode="git"
        )
    except Exception:
        cleanup_destinations = [dest_dir, *reversed(enospc_owned_destinations)]
        cleaned: set[Path] = set()
        for cleanup_dest in cleanup_destinations:
            if cleanup_dest in cleaned:
                continue
            cleaned.add(cleanup_dest)
            try:
                remove_acquired_workspace(cleanup_dest)
            except OSError:
                pass
        raise


def acquire_existing_target(*, repo: str, workspace_dir: Path, ref: str | None) -> AcquiredTarget:
    """Return an already materialized workspace as the acquired target.

    This is intentionally narrower than :func:`acquire_target`: callers own the decision that the
    workspace is safe to re-enter and the function must never delete or copy it. It exists for
    durable resume flows where the previous implementation attempt left useful uncommitted changes
    in its kept workspace. ``ref`` is retained as the intended finalize target only; acquisition
    must not switch refs because the retained workspace's current HEAD and working tree are the
    implementation state being resumed.
    """

    resolved = workspace_dir.expanduser().resolve()
    if not resolved.exists():
        raise FileNotFoundError(resolved)
    if not resolved.is_dir():
        raise ValueError(f"resume workspace must be a directory, got: {resolved}")

    if (resolved / ".git").exists():
        sha = _run_git(["rev-parse", "HEAD"], cwd=resolved)
    else:
        sha = ""

    return AcquiredTarget(
        workspace_dir=resolved,
        repo_input=repo,
        ref=ref,
        commit_sha=sha,
        mode="existing",
    )
