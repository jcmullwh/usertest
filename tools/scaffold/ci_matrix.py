"""
Emit a GitHub Actions job matrix from tools/scaffold/monorepo.toml.

This script is stdlib-only and prints a single-line JSON object suitable for use as a GitHub Actions matrix.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _load_toml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}

    try:
        import tomllib

        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except ModuleNotFoundError:  # pragma: no cover
        try:
            import tomli
        except ModuleNotFoundError as exc:  # pragma: no cover
            raise RuntimeError("TOML parsing requires Python 3.11+ (tomllib) or an installed 'tomli' package.") from exc
        data = tomli.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise RuntimeError(f"Invalid TOML root in {path}: expected table")
    return data


def main() -> int:
    repo_root = _repo_root()
    manifest_path = repo_root / "tools" / "scaffold" / "monorepo.toml"
    data = _load_toml(manifest_path)
    projects = data.get("projects", []) or []

    include: list[dict[str, Any]] = []
    if isinstance(projects, list):
        for p in projects:
            if not isinstance(p, dict):
                continue
            ci_raw = p.get("ci")
            ci: dict[str, Any] = ci_raw if isinstance(ci_raw, dict) else {}
            include.append(
                {
                    "id": p.get("id"),
                    "path": p.get("path"),
                    "toolchain": p.get("toolchain"),
                    "package_manager": p.get("package_manager"),
                    "ci_lint": bool(ci.get("lint", False)),
                    "ci_test": bool(ci.get("test", False)),
                    "ci_build": bool(ci.get("build", False)),
                }
            )

    if not include:
        include = [
            {
                "id": "__no_projects__",
                "path": ".",
                "toolchain": "none",
                "package_manager": "none",
                "ci_lint": False,
                "ci_test": False,
                "ci_build": False,
            }
        ]

    print(json.dumps({"include": include}, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
