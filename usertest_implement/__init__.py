from __future__ import annotations

from pathlib import Path

__all__ = []


def _extend_package_path_for_monorepo_source_run() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    real_pkg = repo_root / "apps" / "usertest_implement" / "src" / "usertest_implement"
    if not real_pkg.is_dir():
        return
    real_pkg_str = str(real_pkg)
    if real_pkg_str in __path__:
        return
    __path__.append(real_pkg_str)


_extend_package_path_for_monorepo_source_run()

