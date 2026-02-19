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


@dataclass(frozen=True)
class SnapshotPlan:
    """
    A resolved plan for a repo snapshot.

    Parameters
    ----------
    repo_root
        Repository root directory.
    out_path
        Output archive path.
    files
        Repo-relative file paths to include in the archive, using forward slashes.
    excluded_gitignores
        Repo-relative `.gitignore` paths excluded from the archive.
    excluded_ignored
        Repo-relative paths excluded because they matched git ignore rules.
    excluded_outputs
        Repo-relative paths excluded because they point at the output archive (to prevent self-inclusion).
    """

    repo_root: Path
    out_path: Path
    files: tuple[str, ...]
    excluded_gitignores: tuple[str, ...]
    excluded_ignored: tuple[str, ...]
    excluded_outputs: tuple[str, ...]


def _validate_out_path(out_path: Path, *, overwrite: bool) -> None:
    """
    Validate the `--out` path before doing any expensive work.

    This is intentionally strict so overwrite-guard failures do not print a full
    "SNAPSHOT PLAN" block that can be mistaken for success in logs.
    """

    if not out_path.exists():
        return
    if out_path.is_dir():
        raise SnapshotError(f"Output path is a directory (pass a .zip file path): {out_path}")
    if not overwrite:
        raise SnapshotError(f"Output already exists (pass --overwrite to replace): {out_path}")


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

    proc = subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        check=False,
    )
    if proc.returncode == 0:
        return proc.stdout
    msg = (proc.stderr or proc.stdout).decode("utf-8", errors="replace").strip()
    if not msg:
        msg = f"git failed (exit {proc.returncode}): {' '.join(args)}"
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
        ["git", "-C", str(repo_root), "check-ignore", "-z", "--stdin"],
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
    out_path: Path,
    include_untracked: bool,
    include_ignored: bool,
    include_gitignore_files: bool,
    exclude_ignored: bool,
) -> SnapshotPlan:
    """
    Build a snapshot plan (file list + exclusions).
    """

    all_files = _git_ls_files(
        repo_root=repo_root,
        include_untracked=include_untracked,
        include_ignored=include_ignored,
    )

    excluded_outputs: list[str] = []
    try:
        out_rel = out_path.resolve().relative_to(repo_root.resolve()).as_posix()
        excluded_outputs.append(out_rel)
        excluded_outputs.append(out_rel + ".tmp")
    except Exception:
        pass

    ignored_set = _git_check_ignore(repo_root=repo_root, paths=all_files) if exclude_ignored else set()

    included: list[str] = []
    excluded_gitignores: list[str] = []
    excluded_ignored: list[str] = []
    for rel in all_files:
        if rel in ignored_set:
            excluded_ignored.append(rel)
            continue
        if rel in excluded_outputs:
            continue
        if not include_gitignore_files and _posix_basename(rel).lower() == ".gitignore":
            excluded_gitignores.append(rel)
            continue
        included.append(rel)

    if not included:
        raise SnapshotError("No files selected for snapshot (after exclusions).")

    return SnapshotPlan(
        repo_root=repo_root,
        out_path=out_path,
        files=tuple(included),
        excluded_gitignores=tuple(excluded_gitignores),
        excluded_ignored=tuple(sorted(excluded_ignored)),
        excluded_outputs=tuple(sorted(set(excluded_outputs))),
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
    if out_path.exists():
        if out_path.is_dir():
            raise SnapshotError(f"Output path is a directory (pass a .zip file path): {out_path}")
        if not overwrite:
            raise SnapshotError(f"Output already exists (pass --overwrite to replace): {out_path}")

    out_path.parent.mkdir(parents=True, exist_ok=True)

    compression = zipfile.ZIP_DEFLATED
    compresslevel = 9

    tmp_out = out_path.with_suffix(out_path.suffix + ".tmp")
    if tmp_out.exists():
        tmp_out.unlink()

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
    except Exception:
        try:
            if tmp_out.exists():
                tmp_out.unlink()
        except OSError:
            pass
        raise

    os.replace(tmp_out, out_path)


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
        prog="snapshot_repo",
        description=(
            "Create a zip snapshot of the repo using git's standard excludes (.gitignore, etc). "
            "By default, `.gitignore` files themselves are excluded from the archive."
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
        required=True,
        help="Output .zip path to write.",
    )
    parser.add_argument(
        "--tracked-only",
        action="store_true",
        help="Only include tracked files (omit untracked files).",
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
        help="Include `.gitignore` files in the snapshot (by default they are excluded).",
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
        "--overwrite",
        action="store_true",
        help="Overwrite --out if it already exists.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    repo_root = args.repo_root.resolve() if args.repo_root is not None else _find_repo_root(Path.cwd())
    out_path = args.out.resolve()
    _validate_out_path(out_path, overwrite=bool(args.overwrite))

    plan = _plan_snapshot(
        repo_root=repo_root,
        out_path=out_path,
        include_untracked=not bool(args.tracked_only),
        include_ignored=bool(args.include_ignored),
        include_gitignore_files=bool(args.include_gitignore_files),
        exclude_ignored=not bool(args.include_ignored),
    )

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    print("SNAPSHOT PLAN")
    print(f"- time_utc: {now}")
    print(f"- repo_root: {plan.repo_root}")
    print(f"- out: {plan.out_path}")
    print(f"- tracked_only: {bool(args.tracked_only)}")
    print(f"- include_ignored: {bool(args.include_ignored)}")
    print(f"- include_gitignore_files: {bool(args.include_gitignore_files)}")
    print(f"- verify: {bool(args.verify)}")
    print(f"- files: {len(plan.files)}")
    print(f"- excluded_gitignores: {len(plan.excluded_gitignores)}")
    print(f"- excluded_ignored: {len(plan.excluded_ignored)}")
    print(f"- excluded_outputs: {len(plan.excluded_outputs)}")
    print("")

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
