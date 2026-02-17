from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path


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
