from __future__ import annotations

import hashlib
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

HashMode = Literal["git", "filesystem"]


@dataclass(frozen=True)
class WorkspaceStateHash:
    sha256: str
    mode: HashMode
    file_count: int
    deleted_count: int

    def to_dict(self) -> dict[str, object]:
        return {
            "sha256": self.sha256,
            "mode": self.mode,
            "file_count": self.file_count,
            "deleted_count": self.deleted_count,
        }


def compute_workspace_state_hash(workspace_dir: Path) -> WorkspaceStateHash:
    try:
        return _compute_git_workspace_state_hash(workspace_dir)
    except Exception:
        return _compute_filesystem_workspace_state_hash(workspace_dir)


def _compute_git_workspace_state_hash(workspace_dir: Path) -> WorkspaceStateHash:
    tracked_and_untracked = _git_zlist(
        workspace_dir,
        [
            "git",
            "-C",
            str(workspace_dir),
            "ls-files",
            "--cached",
            "--others",
            "--exclude-standard",
            "-z",
        ],
    )
    deleted = _git_zlist(
        workspace_dir,
        ["git", "-C", str(workspace_dir), "ls-files", "--deleted", "-z"],
    )
    entries: list[tuple[str, bytes]] = []
    deleted_entries: list[str] = []
    for rel in tracked_and_untracked:
        if not rel or rel.startswith(".git/"):
            continue
        rel_norm = rel.replace("\\", "/")
        path = workspace_dir / Path(rel_norm)
        if not path.exists() and not path.is_symlink():
            continue
        if path.is_symlink():
            kind = "symlink"
            data = os.readlink(path).encode("utf-8", "surrogateescape")
        elif path.is_file():
            kind = "file"
            data = path.read_bytes()
        else:
            continue
        entries.append((rel_norm, _encode_entry(rel_norm, kind, data)))
    for rel in deleted:
        if not rel or rel.startswith(".git/"):
            continue
        rel_norm = rel.replace("\\", "/")
        deleted_entries.append(rel_norm)
    digest = hashlib.sha256()
    for _rel, encoded in sorted(entries, key=lambda item: item[0]):
        digest.update(encoded)
    for deleted_rel in sorted(set(deleted_entries)):
        digest.update(_encode_deleted_entry(deleted_rel))
    return WorkspaceStateHash(
        sha256=digest.hexdigest(),
        mode="git",
        file_count=len(entries),
        deleted_count=len(set(deleted_entries)),
    )


def _compute_filesystem_workspace_state_hash(workspace_dir: Path) -> WorkspaceStateHash:
    entries: list[tuple[str, bytes]] = []
    for path in sorted(workspace_dir.rglob("*")):
        try:
            rel = path.relative_to(workspace_dir).as_posix()
        except ValueError:
            continue
        if not rel or rel == ".git" or rel.startswith(".git/"):
            continue
        if path.is_symlink():
            kind = "symlink"
            data = os.readlink(path).encode("utf-8", "surrogateescape")
        elif path.is_file():
            kind = "file"
            data = path.read_bytes()
        else:
            continue
        entries.append((rel, _encode_entry(rel, kind, data)))
    digest = hashlib.sha256()
    for _, encoded in entries:
        digest.update(encoded)
    return WorkspaceStateHash(
        sha256=digest.hexdigest(),
        mode="filesystem",
        file_count=len(entries),
        deleted_count=0,
    )


def _encode_entry(rel: str, kind: str, data: bytes) -> bytes:
    rel_b = rel.encode("utf-8", "surrogateescape")
    kind_b = kind.encode("ascii")
    size_b = str(len(data)).encode("ascii")
    return b"\0".join((b"path", rel_b, b"kind", kind_b, b"size", size_b, b"data", data, b""))


def _encode_deleted_entry(rel: str) -> bytes:
    rel_b = rel.encode("utf-8", "surrogateescape")
    return b"\0".join((b"path", rel_b, b"kind", b"deleted", b""))


def _git_zlist(workspace_dir: Path, argv: list[str]) -> list[str]:
    proc = subprocess.run(
        argv,
        cwd=str(workspace_dir),
        capture_output=True,
        text=False,
        check=False,
    )
    if proc.returncode != 0:
        stderr_text = (proc.stderr or b"").decode("utf-8", "replace").strip()
        raise RuntimeError(stderr_text or f"git command failed: {' '.join(argv)}")
    raw = proc.stdout or b""
    if not raw:
        return []
    return [item.decode("utf-8", "surrogateescape") for item in raw.split(b"\0") if item]
