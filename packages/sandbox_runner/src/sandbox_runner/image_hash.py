from __future__ import annotations

import hashlib
import os
from collections.abc import Iterator
from pathlib import Path

_EXCLUDED_DIR_NAMES = {
    ".git",
    ".hg",
    ".svn",
    ".venv",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".pdm-build",
    "__pycache__",
    "node_modules",
}

_EXCLUDED_FILE_NAMES = {
    ".DS_Store",
}


def _iter_context_files(context_dir: Path) -> Iterator[tuple[str, Path]]:
    context_dir = context_dir.resolve()

    for root, dirnames, filenames in os.walk(context_dir):
        dirnames[:] = sorted([d for d in dirnames if d not in _EXCLUDED_DIR_NAMES])
        for filename in sorted(filenames):
            if filename in _EXCLUDED_FILE_NAMES:
                continue
            abs_path = Path(root) / filename
            rel = abs_path.relative_to(context_dir).as_posix()
            yield rel, abs_path


def _hash_file(hasher: hashlib._Hash, path: Path) -> None:
    with path.open("rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            hasher.update(chunk)


def compute_image_hash(*, context_dir: Path, dockerfile: Path) -> str:
    hasher = hashlib.sha256()

    for rel_path, abs_path in _iter_context_files(context_dir):
        hasher.update(b"file\0")
        hasher.update(rel_path.encode("utf-8"))
        hasher.update(b"\0")
        _hash_file(hasher, abs_path)
        hasher.update(b"\0")

    dockerfile_resolved = dockerfile.resolve()
    context_resolved = context_dir.resolve()
    dockerfile_in_context = False
    try:
        dockerfile_resolved.relative_to(context_resolved)
        dockerfile_in_context = True
    except ValueError:
        dockerfile_in_context = False

    if not dockerfile_in_context:
        hasher.update(b"dockerfile\0")
        _hash_file(hasher, dockerfile_resolved)
        hasher.update(b"\0")

    return hasher.hexdigest()
