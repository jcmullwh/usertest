from __future__ import annotations

import importlib.util
import os
from functools import lru_cache
from pathlib import Path
from types import ModuleType

import pytest


@lru_cache(maxsize=1)
def _publish_snapshots_module() -> ModuleType:
    repo_root = Path(__file__).resolve().parents[3]
    module_path = repo_root / "tools" / "monorepo_publish" / "publish_snapshots.py"
    spec = importlib.util.spec_from_file_location("publish_snapshots", module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Failed to load module spec for {module_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_pyproject(*, path: Path, name: str, version: str, status: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            [
                "[project]",
                f'name = "{name}"',
                f'version = "{version}"',
                'requires-python = ">=3.11"',
                "dependencies = []",
                "",
                "[tool.monorepo]",
                f'status = "{status}"',
                "",
            ]
        ),
        encoding="utf-8",
    )


def test_publish_snapshots_refuses_without_explicit_confirmation(tmp_path: Path) -> None:
    publish_snapshots = _publish_snapshots_module()
    _write_pyproject(
        path=tmp_path / "packages" / "pkg_a" / "pyproject.toml",
        name="pkg-a",
        version="0.1.0",
        status="stable",
    )

    with pytest.raises(publish_snapshots.PublishSnapshotsError, match="confirm"):
        publish_snapshots.main(["--repo-root", str(tmp_path)])


def test_publish_snapshots_dry_run_does_not_require_confirmation(tmp_path: Path) -> None:
    publish_snapshots = _publish_snapshots_module()
    _write_pyproject(
        path=tmp_path / "packages" / "pkg_a" / "pyproject.toml",
        name="pkg-a",
        version="0.1.0",
        status="stable",
    )

    assert publish_snapshots.main(["--repo-root", str(tmp_path), "--dry-run"]) == 0


def test_publish_snapshots_live_publish_requires_credentials(tmp_path: Path) -> None:
    publish_snapshots = _publish_snapshots_module()
    _write_pyproject(
        path=tmp_path / "packages" / "pkg_a" / "pyproject.toml",
        name="pkg-a",
        version="0.1.0",
        status="stable",
    )

    # Prevent any accidental live upload if the test environment has credentials.
    for key in (
        "GITLAB_BASE_URL",
        "GITLAB_PYPI_PROJECT_ID",
        "GITLAB_PYPI_USERNAME",
        "GITLAB_PYPI_PASSWORD",
    ):
        os.environ.pop(key, None)

    with pytest.raises(
        publish_snapshots.PublishSnapshotsError,
        match="Missing GitLab publishing env vars",
    ):
        publish_snapshots.main(["--repo-root", str(tmp_path), "--confirm-live-publish"])
