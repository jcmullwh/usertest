from __future__ import annotations

import argparse
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


@dataclass(frozen=True)
class MoveOp:
    """
    A planned filesystem move from `src` to `dst`.

    Notes
    -----
    This tool only plans/moves within a single repo root. Moves are executed via `shutil.move`,
    which should be a rename on the same filesystem (fast) and a copy+delete if needed.
    """

    src: Path
    dst: Path


class MigrationError(RuntimeError):
    pass


def _find_repo_root(start: Path) -> Path:
    """
    Find the monorepo root by walking upward looking for `tools/scaffold/monorepo.toml`.
    """

    cur = start.resolve()
    for candidate in [cur, *cur.parents]:
        if (candidate / "tools" / "scaffold" / "monorepo.toml").exists():
            return candidate
    raise MigrationError(
        "Could not find repo root (expected tools/scaffold/monorepo.toml in a parent directory)."
    )


def _iter_children_sorted(path: Path) -> list[Path]:
    return sorted(path.iterdir(), key=lambda p: p.name.lower())


def _unique_child_name(dest_dir: Path, name: str, *, reserved_dests: set[Path]) -> str:
    """
    Return a filename under `dest_dir` that does not exist by appending a suffix.
    """

    candidate_path = dest_dir / name
    if not candidate_path.exists() and candidate_path not in reserved_dests:
        return name

    base = name
    for idx in range(1, 10_000):
        candidate = f"{base}__migrated_{idx}"
        candidate_path = dest_dir / candidate
        if not candidate_path.exists() and candidate_path not in reserved_dests:
            return candidate
    raise MigrationError(f"Could not generate a unique destination name for {dest_dir / name}")


def _plan_merge_dir_children(
    *,
    src_dir: Path,
    dest_dir: Path,
    reserved_dests: set[Path],
    rename_on_conflict: bool,
    skip_existing: bool,
) -> tuple[list[MoveOp], list[str]]:
    """
    Plan moving children of `src_dir` into `dest_dir`.

    Returns
    -------
    (moves, warnings)
    """

    if not src_dir.is_dir():
        raise MigrationError(f"Expected directory: {src_dir}")
    dest_dir_exists = dest_dir.exists() or dest_dir in reserved_dests
    if dest_dir_exists and dest_dir.exists() and not dest_dir.is_dir():
        raise MigrationError(f"Destination exists and is not a directory: {dest_dir}")

    moves: list[MoveOp] = []
    warnings: list[str] = []

    for child in _iter_children_sorted(src_dir):
        dst_child = dest_dir / child.name
        if not dst_child.exists() and dst_child not in reserved_dests:
            moves.append(MoveOp(src=child, dst=dst_child))
            continue

        if skip_existing:
            warnings.append(f"skip-existing: {child} (dest exists: {dst_child})")
            continue

        if rename_on_conflict:
            new_name = _unique_child_name(dest_dir, child.name, reserved_dests=reserved_dests)
            moves.append(MoveOp(src=child, dst=dest_dir / new_name))
            warnings.append(
                f"rename-on-conflict: {child.name} -> {new_name} (dest exists: {dst_child})"
            )
            continue

        raise MigrationError(f"Conflict: destination already exists: {dst_child}")

    return moves, warnings


def _collect_migration_roots(repo_root: Path) -> list[Path]:
    """
    Return legacy roots that should be migrated into `runs/usertest/`.
    """

    roots: list[Path] = []

    legacy_app_local = repo_root / "usertest" / "runs"
    if legacy_app_local.exists():
        roots.append(legacy_app_local)

    legacy_runs_root = repo_root / "runs"
    if legacy_runs_root.exists():
        roots.append(legacy_runs_root)

    return roots


def _iter_legacy_entries(root: Path) -> Iterable[Path]:
    """
    Yield top-level entries to migrate from a legacy root.
    """

    if not root.exists():
        return []
    if not root.is_dir():
        raise MigrationError(f"Legacy root is not a directory: {root}")

    # Legacy app-local root: migrate everything under it.
    if root.name == "runs" and root.parent.name == "usertest":
        return _iter_children_sorted(root)

    # Legacy repo-root runs/: migrate everything except canonical subfolders.
    if root.name == "runs" and root.parent.name != "usertest":
        out: list[Path] = []
        for child in _iter_children_sorted(root):
            if child.name in {"usertest", "_cache"}:
                continue
            out.append(child)
        return out

    raise MigrationError(f"Unsupported legacy root: {root}")


def plan_migration(
    *,
    repo_root: Path,
    rename_on_conflict: bool,
    skip_existing: bool,
) -> tuple[list[MoveOp], list[str]]:
    """
    Plan moving legacy run directories into `runs/usertest/`.

    This includes:
    - `usertest/runs/*` -> `runs/usertest/*`
    - legacy `runs/*` (excluding `runs/usertest` and `runs/_cache`) -> `runs/usertest/*`
    """

    dest_root = repo_root / "runs" / "usertest"

    moves: list[MoveOp] = []
    warnings: list[str] = []
    reserved_dests: set[Path] = set()

    def _reserve_dir_children(*, src_dir: Path, dest_dir: Path) -> None:
        reserved_dests.add(dest_dir)
        if not src_dir.exists() or not src_dir.is_dir():
            return
        try:
            for child in src_dir.iterdir():
                reserved_dests.add(dest_dir / child.name)
        except OSError:
            # If we can't enumerate, we can still reserve the directory itself.
            return

    for legacy_root in _collect_migration_roots(repo_root):
        for entry in _iter_legacy_entries(legacy_root):
            if not entry.exists():
                continue
            if not entry.is_dir():
                warnings.append(f"skip non-directory legacy entry: {entry}")
                continue

            dest = dest_root / entry.name
            dest_exists = dest.exists() or dest in reserved_dests
            if not dest_exists:
                moves.append(MoveOp(src=entry, dst=dest))
                _reserve_dir_children(src_dir=entry, dest_dir=dest)
                continue

            # Merge directories by moving children (e.g., timestamps) into the existing dest.
            child_moves, child_warnings = _plan_merge_dir_children(
                src_dir=entry,
                dest_dir=dest,
                reserved_dests=reserved_dests,
                rename_on_conflict=rename_on_conflict,
                skip_existing=skip_existing,
            )
            moves.extend(child_moves)
            for op in child_moves:
                _reserve_dir_children(src_dir=op.src, dest_dir=op.dst)
            warnings.extend(child_warnings)

    # If we planned per-child moves within a directory, the directory itself may remain empty
    # after apply; cleanup is handled separately.
    return moves, warnings


def _apply_moves(moves: list[MoveOp], *, dry_run: bool) -> None:
    for op in moves:
        if dry_run:
            continue
        op.dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(op.src), str(op.dst))


def _rmdir_if_empty(path: Path, *, dry_run: bool) -> bool:
    """
    Attempt to remove `path` if it is an empty directory.

    Returns True if removed.
    """

    if not path.exists() or not path.is_dir():
        return False
    try:
        next(path.iterdir())
    except StopIteration:
        if not dry_run:
            path.rmdir()
        return True
    return False


def _cleanup_legacy_dirs(repo_root: Path, *, dry_run: bool) -> list[str]:
    """
    Best-effort cleanup of empty legacy directories after migration.
    """

    cleaned: list[str] = []

    # Remove empty legacy children first (if we did child-level merges).
    legacy_app_local = repo_root / "usertest" / "runs"
    if legacy_app_local.exists() and legacy_app_local.is_dir():
        try:
            for child in _iter_children_sorted(legacy_app_local):
                if _rmdir_if_empty(child, dry_run=dry_run):
                    cleaned.append(str(child))
        except OSError:
            pass

    legacy_runs_root = repo_root / "runs"
    if legacy_runs_root.exists() and legacy_runs_root.is_dir():
        try:
            for child in _iter_children_sorted(legacy_runs_root):
                if not child.is_dir():
                    continue
                if child.name in {"usertest", "_cache"}:
                    continue
                if _rmdir_if_empty(child, dry_run=dry_run):
                    cleaned.append(str(child))
        except OSError:
            pass

    # usertest/runs -> usertest (if empty)
    if _rmdir_if_empty(legacy_app_local, dry_run=dry_run):
        cleaned.append(str(legacy_app_local))
    legacy_usertest = repo_root / "usertest"
    if _rmdir_if_empty(legacy_usertest, dry_run=dry_run):
        cleaned.append(str(legacy_usertest))

    return cleaned


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="migrate_runs_layout",
        description="Migrate legacy usertest run directories into runs/usertest/ (dry-run by default).",
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        help="Repo root (auto-detected via tools/scaffold/monorepo.toml if omitted).",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply filesystem changes. If omitted, this is a dry-run.",
    )
    parser.add_argument(
        "--rename-on-conflict",
        action="store_true",
        help="On path conflicts, rename the incoming directory by appending '__migrated_<N>'.",
    )
    parser.add_argument(
        "--skip-existing",
        action="store_true",
        help="On path conflicts, skip the incoming directory (logs a warning).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    repo_root = args.repo_root.resolve() if args.repo_root is not None else _find_repo_root(Path.cwd())
    dry_run = not bool(args.apply)

    if args.rename_on_conflict and args.skip_existing:
        raise MigrationError("--rename-on-conflict and --skip-existing are mutually exclusive.")

    moves, warnings = plan_migration(
        repo_root=repo_root,
        rename_on_conflict=bool(args.rename_on_conflict),
        skip_existing=bool(args.skip_existing),
    )

    if dry_run:
        print("DRY RUN (no filesystem changes).")
    else:
        print("APPLYING filesystem changes.")
    print(f"repo_root: {repo_root}")
    print(f"dest_root: {repo_root / 'runs' / 'usertest'}")
    print()

    if warnings:
        print("WARNINGS:", file=sys.stderr)
        for w in warnings:
            print(f"- {w}", file=sys.stderr)
        print(file=sys.stderr)

    if not moves:
        print("No moves required.")
        return 0

    print("PLANNED MOVES:")
    for op in moves:
        print(f"- {op.src} -> {op.dst}")
    print()

    _apply_moves(moves, dry_run=dry_run)

    removed = _cleanup_legacy_dirs(repo_root, dry_run=dry_run)
    if removed:
        print("CLEANUP:")
        for p in removed:
            verb = "would remove" if dry_run else "removed"
            print(f"- {verb} empty dir: {p}")
        print()

    print("Done.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except MigrationError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        raise SystemExit(2)
