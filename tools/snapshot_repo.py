from __future__ import annotations

import argparse
import os
import subprocess
import sys
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


class SnapshotError(RuntimeError):
    pass


PLAN_EXCLUDED_GITIGNORE_PATHS_LIMIT = 20


@dataclass(frozen=True)
class SnapshotPlan:
    """
    A resolved plan for a repo snapshot.

    Parameters
    ----------
    repo_root
        Repository root directory.
    out_path
        Output archive path (or None in preview/listing modes).
    files
        Repo-relative file paths to include in the archive, using forward slashes.
    excluded_gitignores
        Repo-relative `.gitignore` paths excluded from the archive.
    excluded_ignored
        Repo-relative paths excluded because they matched git ignore rules.
    excluded_outputs
        Repo-relative paths excluded because they point at the output archive (to prevent self-inclusion).
    excluded_untracked
        Repo-relative paths excluded because they were untracked and `--tracked-only` was requested.
        Populated only when excluded details are collected (e.g. `--list-excluded`).
    excluded_untracked_count
        Count of repo-relative paths excluded because they were untracked and `--tracked-only` was requested.
    """

    repo_root: Path
    out_path: Path | None
    files: tuple[str, ...]
    excluded_gitignores: tuple[str, ...]
    excluded_ignored: tuple[str, ...]
    excluded_outputs: tuple[str, ...]
    excluded_untracked: tuple[str, ...]
    excluded_untracked_count: int


def _count_git_z_paths(payload: bytes) -> int:
    """
    Count paths in a NUL-delimited (git -z) byte payload.
    """

    if not payload:
        return 0
    count = payload.count(b"\0")
    # Be defensive if a tool ever produces a non-trailing-terminator payload.
    if not payload.endswith(b"\0"):
        count += 1
    return count


def _validate_out_path_shape(out_path: Path) -> None:
    """
    Validate the shape of an `--out` path regardless of whether we will write.

    This is used by preview/listing modes where we may want to resolve output-relative exclusions,
    but must not require `--overwrite` or fail due to an existing output file.
    """

    if out_path.exists() and out_path.is_dir():
        raise SnapshotError(f"Output path is a directory (pass a .zip file path): {out_path}")

    if out_path.suffix.lower() != ".zip":
        raise SnapshotError(f"Output path must be a .zip file path: {out_path}")

    parent = out_path.parent
    if parent.exists() and not parent.is_dir():
        raise SnapshotError(f"Output directory is not a directory: {parent}")


def _validate_out_path(out_path: Path, *, overwrite: bool) -> None:
    """
    Validate the `--out` path before doing any expensive work.

    This is intentionally strict so overwrite-guard failures do not print a full
    "SNAPSHOT PLAN" block that can be mistaken for success in logs.
    """

    if out_path.exists():
        if out_path.is_dir():
            raise SnapshotError(f"Output path is a directory (pass a .zip file path): {out_path}")
        if not overwrite:
            raise SnapshotError(f"Output already exists (pass --overwrite to replace): {out_path}")

    _validate_out_path_shape(out_path)


def _validate_repo_root_arg(repo_root: Path) -> None:
    """
    Validate an explicit `--repo-root` before invoking git.

    This prevents raw git errors like "fatal: cannot change to ..." from leaking
    into user-facing output.
    """

    try:
        if not repo_root.exists():
            raise SnapshotError(
                "Invalid --repo-root (path does not exist): "
                f"{repo_root}\n"
                "Hint: pass --repo-root pointing at a git checkout directory."
            )
        if not repo_root.is_dir():
            raise SnapshotError(
                "Invalid --repo-root (not a directory): "
                f"{repo_root}\n"
                "Hint: pass --repo-root pointing at a git checkout directory."
            )
    except OSError as e:
        raise SnapshotError(
            "Invalid --repo-root (unable to access path): "
            f"{repo_root}\n"
            f"Details: {e}"
        ) from e

    try:
        out = _run_git(["rev-parse", "--is-inside-work-tree"], cwd=repo_root)
    except SnapshotError as e:
        msg = str(e).strip()
        lowered = msg.lower()
        if "git not found on path" in lowered:
            raise
        if "not a git repository" in lowered:
            raise SnapshotError(
                "Invalid --repo-root (not a git repository): "
                f"{repo_root}\n"
                "Hint: pass --repo-root pointing at a git checkout (a directory containing `.git`)."
            ) from e
        raise SnapshotError(
            "Invalid --repo-root (failed to run git in that directory): "
            f"{repo_root}\n"
            "Hint: pass --repo-root pointing at a git checkout.\n"
            f"git said: {msg}"
        ) from e

    inside = out.decode("utf-8", errors="replace").strip().lower()
    if inside != "true":
        raise SnapshotError(
            "Invalid --repo-root (not a git work tree): "
            f"{repo_root}\n"
            "Hint: pass --repo-root pointing at a git checkout (not a bare repo)."
        )


def _find_repo_root(start: Path) -> Path:
    """
    Find the monorepo root by walking upward looking for `tools/scaffold/monorepo.toml`.

    Parameters
    ----------
    start
        Directory to start searching from.

    Returns
    -------
    pathlib.Path
        The detected repo root.
    """

    cur = start.resolve()
    for candidate in [cur, *cur.parents]:
        if (candidate / "tools" / "scaffold" / "monorepo.toml").exists():
            return candidate
    raise SnapshotError(
        "Could not find repo root (expected tools/scaffold/monorepo.toml in a parent directory)."
    )


def _run_git(args: list[str], *, cwd: Path) -> bytes:
    """
    Run a `git` command in `cwd` and return stdout (bytes).

    Notes
    -----
    This intentionally does not silently fall back to a non-git walk. If `git` is missing or
    fails, we raise with a clear error so the operator can decide how to proceed.
    """

    try:
        proc = subprocess.run(
            ["git", "-C", str(cwd), *args],
            capture_output=True,
            check=False,
        )
    except FileNotFoundError as e:
        raise SnapshotError("git not found on PATH (required for snapshot_repo).") from e
    except OSError as e:
        raise SnapshotError(f"Failed to run git: {e}") from e
    if proc.returncode == 0:
        return proc.stdout
    msg = (proc.stderr or proc.stdout).decode("utf-8", errors="replace").strip()
    if not msg:
        msg = f"git failed (exit {proc.returncode}): {' '.join(args)}"
    lowered = msg.lower()
    if "not a git repository" in lowered:
        raise SnapshotError(
            "Not a git repository.\n"
            f"- repo_root: {cwd}\n"
            "Hint: pass --repo-root pointing at a git checkout (a directory containing `.git`)."
        )
    raise SnapshotError(msg)


def _posix_basename(path: str) -> str:
    """
    Return the final path component for a git-style relative path.

    Notes
    -----
    Git paths are normally POSIX (forward slashes), but this helper is defensive and
    treats backslashes as separators too.
    """

    return path.replace("\\", "/").rsplit("/", maxsplit=1)[-1]


def _git_ls_files(*, repo_root: Path, include_untracked: bool, include_ignored: bool) -> list[str]:
    """
    Enumerate repo files via `git ls-files`.

    Parameters
    ----------
    repo_root
        Repository root directory.
    include_untracked
        If True, include untracked files that are not ignored by standard excludes.
    include_ignored
        If True, include ignored (gitignored) untracked files too.

    Returns
    -------
    list[str]
        Repo-relative file paths using forward slashes.
    """

    files: set[str] = set()

    out_tracked = _run_git(["ls-files", "-z", "--cached"], cwd=repo_root)
    files.update(filter(None, out_tracked.decode("utf-8", errors="strict").split("\0")))

    if include_untracked:
        # Untracked-but-not-ignored files (respects .gitignore + standard excludes).
        out_untracked = _run_git(["ls-files", "-z", "--others", "--exclude-standard"], cwd=repo_root)
        files.update(filter(None, out_untracked.decode("utf-8", errors="strict").split("\0")))

        if include_ignored:
            out_ignored = _run_git(
                ["ls-files", "-z", "--others", "--ignored", "--exclude-standard"], cwd=repo_root
            )
            files.update(filter(None, out_ignored.decode("utf-8", errors="strict").split("\0")))
    else:
        if include_ignored:
            raise SnapshotError("--include-ignored requires including untracked files (omit --tracked-only).")

    return sorted(files)


def _git_ls_files_parts(
    *,
    repo_root: Path,
    include_untracked: bool,
    include_ignored: bool,
    collect_untracked_details: bool,
) -> tuple[list[str], list[str], list[str]]:
    """
    Enumerate repo files via `git ls-files`, returning tracked/untracked parts.

    Returns
    -------
    tuple[list[str], list[str], list[str]]
        (tracked, untracked_not_ignored, untracked_ignored)
    """

    if not include_untracked and include_ignored:
        raise SnapshotError("--include-ignored requires including untracked files (omit --tracked-only).")

    out_tracked = _run_git(["ls-files", "-z", "--cached"], cwd=repo_root)
    tracked = sorted(filter(None, out_tracked.decode("utf-8", errors="strict").split("\0")))

    untracked_not_ignored: list[str] = []
    untracked_ignored: list[str] = []

    if include_untracked or collect_untracked_details:
        out_untracked = _run_git(["ls-files", "-z", "--others", "--exclude-standard"], cwd=repo_root)
        untracked_not_ignored = sorted(
            filter(None, out_untracked.decode("utf-8", errors="strict").split("\0"))
        )

        if include_ignored or collect_untracked_details:
            out_ignored = _run_git(
                ["ls-files", "-z", "--others", "--ignored", "--exclude-standard"], cwd=repo_root
            )
            untracked_ignored = sorted(filter(None, out_ignored.decode("utf-8", errors="strict").split("\0")))

    return tracked, untracked_not_ignored, untracked_ignored


def _git_check_ignore(*, repo_root: Path, paths: list[str]) -> set[str]:
    """
    Return the subset of `paths` that match ignore rules.

    Parameters
    ----------
    repo_root
        Repository root directory.
    paths
        Repo-relative paths (forward slashes).

    Returns
    -------
    set[str]
        The ignored paths.
    """

    if not paths:
        return set()

    payload = ("\0".join(paths) + "\0").encode("utf-8")
    proc = subprocess.run(
        ["git", "-C", str(repo_root), "check-ignore", "--no-index", "-z", "--stdin"],
        input=payload,
        capture_output=True,
        check=False,
    )
    if proc.returncode not in (0, 1):
        msg = (proc.stderr or proc.stdout).decode("utf-8", errors="replace").strip()
        if not msg:
            msg = f"git check-ignore failed (exit {proc.returncode})"
        raise SnapshotError(msg)

    out = proc.stdout.decode("utf-8", errors="strict")
    ignored = {p for p in out.split("\0") if p}
    return ignored


def _plan_snapshot(
    *,
    repo_root: Path,
    out_path: Path | None,
    include_untracked: bool,
    include_ignored: bool,
    include_gitignore_files: bool,
    exclude_ignored: bool,
    collect_excluded_details: bool,
) -> SnapshotPlan:
    """
    Build a snapshot plan (file list + exclusions).
    """

    tracked, untracked_not_ignored, untracked_ignored = _git_ls_files_parts(
        repo_root=repo_root,
        include_untracked=include_untracked,
        include_ignored=include_ignored,
        collect_untracked_details=collect_excluded_details,
    )
    files: set[str] = set(tracked)
    if include_untracked:
        files.update(untracked_not_ignored)
        if include_ignored:
            files.update(untracked_ignored)
    all_files = sorted(files)

    excluded_output_candidates: set[str] = set()
    if out_path is not None:
        try:
            out_rel = out_path.resolve().relative_to(repo_root.resolve()).as_posix()
            excluded_output_candidates.add(out_rel)
            excluded_output_candidates.add(out_rel + ".tmp")
        except Exception:
            pass

    ignored_set = _git_check_ignore(repo_root=repo_root, paths=all_files) if exclude_ignored else set()

    included: list[str] = []
    excluded_gitignores: list[str] = []
    excluded_ignored: list[str] = []
    excluded_outputs: list[str] = []
    for rel in all_files:
        if rel in excluded_output_candidates:
            excluded_outputs.append(rel)
            continue
        if not include_gitignore_files and _posix_basename(rel).lower() == ".gitignore":
            excluded_gitignores.append(rel)
            continue
        if rel in ignored_set:
            excluded_ignored.append(rel)
            continue
        included.append(rel)

    excluded_untracked: tuple[str, ...] = ()
    excluded_untracked_count = 0
    if not include_untracked:
        if untracked_not_ignored:
            excluded_untracked_count = len(untracked_not_ignored)
            if collect_excluded_details:
                excluded_untracked = tuple(sorted(set(untracked_not_ignored)))
        else:
            # We did not enumerate untracked-but-not-ignored paths, but we still want an auditable
            # excluded_untracked count for `--tracked-only` plan output.
            out_untracked = _run_git(["ls-files", "-z", "--others", "--exclude-standard"], cwd=repo_root)
            excluded_untracked_count = _count_git_z_paths(out_untracked)

    if collect_excluded_details and exclude_ignored and untracked_ignored:
        excluded_ignored.extend(untracked_ignored)

    if not included:
        raise SnapshotError("No files selected for snapshot (after exclusions).")

    return SnapshotPlan(
        repo_root=repo_root,
        out_path=out_path,
        files=tuple(included),
        excluded_gitignores=tuple(excluded_gitignores),
        excluded_ignored=tuple(sorted(set(excluded_ignored))),
        excluded_outputs=tuple(sorted(set(excluded_outputs))),
        excluded_untracked=excluded_untracked,
        excluded_untracked_count=int(excluded_untracked_count),
    )


def _write_zip(plan: SnapshotPlan, *, overwrite: bool) -> None:
    """
    Write the snapshot archive described by `plan`.

    Parameters
    ----------
    plan
        Planned snapshot contents.
    overwrite
        If True, overwrite an existing output archive.
    """

    out_path = plan.out_path
    if out_path is None:
        raise SnapshotError("Internal error: missing output path for archive write.")
    _validate_out_path(out_path, overwrite=overwrite)

    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise SnapshotError(f"Failed to create output directory: {out_path.parent}: {e}") from e

    compression = zipfile.ZIP_DEFLATED
    compresslevel = 9

    tmp_out = out_path.with_suffix(out_path.suffix + ".tmp")
    try:
        if tmp_out.exists():
            tmp_out.unlink()
    except OSError as e:
        raise SnapshotError(f"Failed to remove pre-existing temp file: {tmp_out}: {e}") from e

    try:
        with zipfile.ZipFile(
            tmp_out,
            mode="w",
            compression=compression,
            compresslevel=compresslevel,
        ) as zf:
            for rel in plan.files:
                abs_path = plan.repo_root / rel
                if not abs_path.is_file():
                    raise SnapshotError(f"Missing expected file: {abs_path}")
                zf.write(abs_path, arcname=rel)
    except SnapshotError:
        try:
            if tmp_out.exists():
                tmp_out.unlink()
        except OSError:
            pass
        raise
    except OSError as e:
        try:
            if tmp_out.exists():
                tmp_out.unlink()
        except OSError:
            pass
        raise SnapshotError(f"Failed to write archive: {tmp_out}: {e}") from e
    except Exception as e:
        try:
            if tmp_out.exists():
                tmp_out.unlink()
        except OSError:
            pass
        raise SnapshotError(f"Failed to write archive: {tmp_out}: {e}") from e

    try:
        os.replace(tmp_out, out_path)
    except OSError as e:
        try:
            if tmp_out.exists():
                tmp_out.unlink()
        except OSError:
            pass
        raise SnapshotError(f"Failed to finalize archive: {out_path}: {e}") from e


def _verify_zip(
    *,
    repo_root: Path,
    zip_path: Path,
    allow_gitignore_files: bool,
    allow_ignored_files: bool,
) -> None:
    """
    Verify the written archive respects the tool's invariants.

    Invariants
    ----------
    - If `allow_gitignore_files` is False, the archive must not contain any `.gitignore` entries.
    - No entries in the archive may match git ignore rules.
    """

    try:
        with zipfile.ZipFile(zip_path) as zf:
            entries = [n for n in zf.namelist() if n and not n.endswith("/")]
    except Exception as e:
        raise SnapshotError(f"Failed to read output archive for verification: {e}") from e

    if not allow_gitignore_files:
        bad = [n for n in entries if _posix_basename(n).lower() == ".gitignore"]
        if bad:
            sample = "\n".join(f"- {p}" for p in sorted(bad)[:20])
            raise SnapshotError(
                "Output archive unexpectedly contains `.gitignore` files.\n"
                "This is a bug; please report it with the sample below:\n"
                f"{sample}"
            )

    if not allow_ignored_files:
        ignored = _git_check_ignore(repo_root=repo_root, paths=entries)
        if ignored:
            sample = "\n".join(f"- {p}" for p in sorted(ignored)[:20])
            raise SnapshotError(
                "Output archive unexpectedly contains git-ignored files.\n"
                "This is a bug; please report it with the sample below:\n"
                f"{sample}"
            )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python tools/snapshot_repo.py",
        description=(
            "Create a zip snapshot of the repo using git's standard excludes (.gitignore, etc). "
            "Archive entries use repo-relative paths. "
            "Note: by default, `.gitignore` files themselves are excluded from the archive "
            "(use --include-gitignore-files to include them)."
        ),
        formatter_class=argparse.RawTextHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python tools/snapshot_repo.py --out repo_snapshot.zip\n"
            "  python tools/snapshot_repo.py --out repo_snapshot.zip --overwrite\n"
            "  python tools/snapshot_repo.py --out repo_snapshot.zip --include-gitignore-files\n"
            "  python tools/snapshot_repo.py --dry-run\n"
            "  python tools/snapshot_repo.py --list-included\n"
            "  python tools/snapshot_repo.py --list-excluded --list-limit 200\n"
            "\n"
            "Notes:\n"
            "  - `--out` must be a .zip file path.\n"
            "  - `--out` is optional in preview/listing modes.\n"
            "  - Archive entries are repo-relative (no top-level directory prefix).\n"
            "  - By default, untracked files are included if they are not ignored; pass --tracked-only to exclude them.\n"
            "  - `.gitignore` files are excluded by default (avoid sharing ignore rules); pass --include-gitignore-files to include them.\n"
        ),
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        help="Repo root (auto-detected via tools/scaffold/monorepo.toml if omitted).",
    )
    parser.add_argument(
        "--out",
        type=Path,
        help="Output .zip path to write (required unless using --dry-run/--plan-only/--list-*).",
    )
    parser.add_argument(
        "--tracked-only",
        action="store_true",
        help="Only include tracked files (omit untracked files; default includes untracked-but-not-ignored).",
    )
    parser.add_argument(
        "--include-ignored",
        action="store_true",
        help=(
            "Include files that match ignore rules (not recommended). "
            "By default, ignored files are excluded even if they are tracked."
        ),
    )
    parser.add_argument(
        "--include-gitignore-files",
        action="store_true",
        help="Include `.gitignore` files in the snapshot (excluded by default to avoid sharing ignore rules).",
    )
    parser.add_argument(
        "--verify",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "Verify the output archive respects invariants (no ignored files unless --include-ignored; "
            "no `.gitignore` files unless --include-gitignore-files)."
        ),
    )
    parser.add_argument(
        "--plan-only",
        action="store_true",
        help="Print the snapshot plan and exit (do not write the archive).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Alias for --plan-only.",
    )
    list_group = parser.add_mutually_exclusive_group()
    list_group.add_argument(
        "--list-included",
        action="store_true",
        help="Print newline-delimited included paths (repo-relative) and exit.",
    )
    list_group.add_argument(
        "--list-excluded",
        action="store_true",
        help="Print newline-delimited excluded paths (repo-relative) with reason codes and exit.",
    )
    parser.add_argument(
        "--list-limit",
        type=int,
        help="Limit list output to the first N lines (applies to --list-included/--list-excluded).",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite --out if it already exists.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.list_limit is not None and args.list_limit <= 0:
        raise SnapshotError("--list-limit must be a positive integer.")

    if args.repo_root is not None:
        repo_root = args.repo_root.resolve()
        _validate_repo_root_arg(repo_root)
    else:
        repo_root = _find_repo_root(Path.cwd())

    do_list_included = bool(args.list_included)
    do_list_excluded = bool(args.list_excluded)
    do_plan = bool(args.plan_only) or bool(args.dry_run)
    preview_mode = do_plan or do_list_included or do_list_excluded

    if args.list_limit is not None and not (do_list_included or do_list_excluded):
        raise SnapshotError("--list-limit requires --list-included or --list-excluded.")

    out_path: Path | None
    if args.out is not None:
        out_path = args.out.resolve()
        _validate_out_path_shape(out_path)
    else:
        out_path = None

    if not preview_mode:
        if out_path is None:
            raise SnapshotError(
                "Missing required --out.\n"
                "Hint: pass --out PATH.zip to write an archive, or use --dry-run/--plan-only/--list-*."
            )
        _validate_out_path(out_path, overwrite=bool(args.overwrite))

    plan = _plan_snapshot(
        repo_root=repo_root,
        out_path=out_path,
        include_untracked=not bool(args.tracked_only),
        include_ignored=bool(args.include_ignored),
        include_gitignore_files=bool(args.include_gitignore_files),
        exclude_ignored=not bool(args.include_ignored),
        collect_excluded_details=do_list_excluded,
    )

    if do_list_included:
        limit = args.list_limit
        for rel in (plan.files if limit is None else plan.files[:limit]):
            print(rel)
        return 0

    if do_list_excluded:
        excluded: dict[str, str] = {}

        # Priority order matters: earlier reasons win.
        for rel in plan.excluded_outputs:
            excluded.setdefault(rel, "output_path")
        for rel in plan.excluded_gitignores:
            excluded.setdefault(rel, "gitignore_file")
        for rel in plan.excluded_ignored:
            excluded.setdefault(rel, "gitignored")
        for rel in plan.excluded_untracked:
            excluded.setdefault(rel, "untracked_excluded")

        items = sorted(excluded.items(), key=lambda kv: kv[0])
        if args.list_limit is not None:
            items = items[: args.list_limit]
        for rel, reason in items:
            print(f"{rel}\t{reason}")
        return 0

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    print("SNAPSHOT PLAN")
    print(f"- time_utc: {now}")
    print(f"- repo_root: {plan.repo_root}")
    print(f"- out: {plan.out_path if plan.out_path is not None else '<none>'}")
    print("- archive_paths: repo-relative")
    print("- default_untracked: include untracked (not ignored); pass --tracked-only to exclude")
    print("- default_gitignore_files: excluded (avoid sharing ignore rules); pass --include-gitignore-files to include")
    print(f"- tracked_only: {bool(args.tracked_only)}")
    print(f"- include_ignored: {bool(args.include_ignored)}")
    print(f"- include_gitignore_files: {bool(args.include_gitignore_files)}")
    print(f"- verify: {bool(args.verify)}")
    print(f"- plan_only: {bool(args.plan_only)}")
    print(f"- dry_run: {bool(args.dry_run)}")
    print(f"- files: {len(plan.files)}")
    print(f"- excluded_gitignores: {len(plan.excluded_gitignores)}")
    if plan.excluded_gitignores:
        excluded_gitignore_paths = sorted(plan.excluded_gitignores)
        shown = excluded_gitignore_paths[:PLAN_EXCLUDED_GITIGNORE_PATHS_LIMIT]
        remaining = len(excluded_gitignore_paths) - len(shown)
        print("- excluded_gitignore_paths:")
        for rel in shown:
            print(f"  - {rel}")
        if remaining:
            print(f"  - ... (+{remaining} more)")
    print(f"- excluded_ignored: {len(plan.excluded_ignored)}")
    print(f"- excluded_outputs: {len(plan.excluded_outputs)}")
    print(f"- excluded_untracked: {plan.excluded_untracked_count}")
    print("")

    if preview_mode:
        if bool(args.dry_run):
            print("Dry-run: no archive written.")
        else:
            print("Plan-only: no archive written.")
        return 0

    _write_zip(plan, overwrite=bool(args.overwrite))
    if bool(args.verify):
        _verify_zip(
            repo_root=repo_root,
            zip_path=out_path,
            allow_gitignore_files=bool(args.include_gitignore_files),
            allow_ignored_files=bool(args.include_ignored),
        )
        print("Verified archive invariants.")
    print("Done.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SnapshotError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        raise SystemExit(2)
