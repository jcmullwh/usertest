from __future__ import annotations

from pathlib import Path

__all__ = []


def _extend_package_path_for_monorepo_source_run() -> None:
    """
    Dev-UX shim for running from a monorepo checkout.

    When a developer runs `python -m usertest.cli` from the repo root without
    sourcing `scripts/set_pythonpath.*`, Python can’t discover the real package
    under `apps/usertest/src/`.

    This shim keeps installed usage unaffected (it only applies when importing
    from a monorepo checkout), but makes the first failure mode an actionable
    in-app hint instead of `No module named usertest`.
    """

    repo_root = Path(__file__).resolve().parents[1]
    real_pkg = repo_root / "apps" / "usertest" / "src" / "usertest"
    if not real_pkg.is_dir():
        return
    real_pkg_str = str(real_pkg)
    if real_pkg_str in __path__:
        return
    __path__.append(real_pkg_str)


_extend_package_path_for_monorepo_source_run()

