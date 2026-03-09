from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath


def find_repo_root(start: Path | None = None) -> Path:
    cur = (start or Path.cwd()).resolve()
    for candidate in [cur, *cur.parents]:
        if (candidate / "tools" / "scaffold" / "monorepo.toml").exists():
            return candidate
    raise FileNotFoundError(
        "Could not find monorepo root "
        "(expected tools/scaffold/monorepo.toml in a parent directory)."
    )


_SLUG_RE = re.compile(r"[^a-zA-Z0-9._-]+")


def slugify(value: str) -> str:
    s = value.strip()
    s = s.replace("\\", "/")
    s = s.rsplit("/", maxsplit=1)[-1]
    s = s.removesuffix(".git")
    s = _SLUG_RE.sub("-", s)
    s = s.strip("-._")
    return s or "target"


def utc_timestamp_compact() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def normalize_agent_path(path: str | Path) -> str:
    """
    Produces a canonical agent-visible POSIX path.
    Separators are always '/', and trailing separators are stripped.
    """
    if isinstance(path, Path):
        s = path.as_posix()
    else:
        s = str(path).replace("\\", "/")

    # PurePosixPath handles basic normalization like stripping trailing slashes
    # and collapsing // while preserving POSIX-style paths.
    return PurePosixPath(s).as_posix()


def agent_path_join(root: str, *leaves: str) -> str:
    """
    Join path components into a canonical agent-visible POSIX path.
    """
    # We use string replacement for the initial root to handle potential backslashes
    # before passing to PurePosixPath, which is strictly POSIX.
    root_norm = str(root).replace("\\", "/")
    leaves_norm = [str(leaf).replace("\\", "/") for leaf in leaves]
    return PurePosixPath(root_norm, *leaves_norm).as_posix()
