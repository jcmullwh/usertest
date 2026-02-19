from __future__ import annotations

import importlib.util
import os
import zipfile
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


def test_publish_snapshots_validate_dists_does_not_require_confirmation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    publish_snapshots = _publish_snapshots_module()
    _write_pyproject(
        path=tmp_path / "packages" / "pkg_a" / "pyproject.toml",
        name="pkg-a",
        version="0.1.0",
        status="stable",
    )

    def fake_build_dist(package_dir: Path) -> Path:
        dist_dir = package_dir / "dist"
        dist_dir.mkdir(parents=True, exist_ok=True)
        wheel_path = dist_dir / "pkg_a-0.1.0-py3-none-any.whl"
        with zipfile.ZipFile(wheel_path, mode="w") as zf:
            zf.writestr("pkg_a/__init__.py", "# ok\n")
        return dist_dir

    monkeypatch.setattr(publish_snapshots, "build_dist", fake_build_dist)

    assert publish_snapshots.main(["--repo-root", str(tmp_path), "--validate-dists"]) == 0


def test_publish_snapshots_validate_dists_fails_on_forbidden_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    publish_snapshots = _publish_snapshots_module()
    _write_pyproject(
        path=tmp_path / "packages" / "pkg_a" / "pyproject.toml",
        name="pkg-a",
        version="0.1.0",
        status="stable",
    )

    def fake_build_dist(package_dir: Path) -> Path:
        dist_dir = package_dir / "dist"
        dist_dir.mkdir(parents=True, exist_ok=True)
        wheel_path = dist_dir / "pkg_a-0.1.0-py3-none-any.whl"
        with zipfile.ZipFile(wheel_path, mode="w") as zf:
            zf.writestr(".env", "SECRET=should-not-be-here\n")
        return dist_dir

    monkeypatch.setattr(publish_snapshots, "build_dist", fake_build_dist)

    with pytest.raises(publish_snapshots.PublishSnapshotsError, match=r"forbidden|\\.env"):
        publish_snapshots.main(["--repo-root", str(tmp_path), "--validate-dists"])


def test_publish_snapshots_live_publish_runs_dist_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    publish_snapshots = _publish_snapshots_module()
    _write_pyproject(
        path=tmp_path / "packages" / "pkg_a" / "pyproject.toml",
        name="pkg-a",
        version="0.1.0",
        status="stable",
    )

    monkeypatch.setenv("GITLAB_BASE_URL", "https://gitlab.example.invalid")
    monkeypatch.setenv("GITLAB_PYPI_PROJECT_ID", "123")
    monkeypatch.setenv("GITLAB_PYPI_USERNAME", "user")
    monkeypatch.setenv("GITLAB_PYPI_PASSWORD", "pass")

    def fake_build_dist(package_dir: Path) -> Path:
        dist_dir = package_dir / "dist"
        dist_dir.mkdir(parents=True, exist_ok=True)
        wheel_path = dist_dir / "pkg_a-0.1.0-py3-none-any.whl"
        with zipfile.ZipFile(wheel_path, mode="w") as zf:
            zf.writestr(".env", "SECRET=should-not-be-here\n")
        return dist_dir

    twine_called = False

    def fake_twine_upload(*_args: object, **_kwargs: object) -> None:
        nonlocal twine_called
        twine_called = True

    monkeypatch.setattr(publish_snapshots, "build_dist", fake_build_dist)
    monkeypatch.setattr(publish_snapshots, "twine_upload", fake_twine_upload)

    with pytest.raises(publish_snapshots.PublishSnapshotsError, match=r"forbidden|\\.env"):
        publish_snapshots.main(["--repo-root", str(tmp_path), "--confirm-live-publish"])

    assert twine_called is False
