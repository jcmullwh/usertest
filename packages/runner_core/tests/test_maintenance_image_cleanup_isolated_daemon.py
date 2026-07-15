"""Opt-in end-to-end cleanup coverage for a caller-provided isolated Docker daemon.

These tests never select a local/default daemon: a maintainer must explicitly
opt in and identify an isolated daemon before pytest will collect the scenario.
They are intentionally not part of ordinary unit or replay execution.
"""

import os
import subprocess
from pathlib import Path
from uuid import uuid4

import pytest

import runner_core.execution_backend as backend_mod

_OPT_IN = os.environ.get("USERTEST_RUN_ISOLATED_DOCKER_TESTS") == "1"
_ISOLATED = os.environ.get("USERTEST_ISOLATED_DOCKER_DAEMON") == "1"
_DOCKER_HOST = os.environ.get("DOCKER_HOST")

pytestmark = pytest.mark.skipif(
    not (_OPT_IN and _ISOLATED and _DOCKER_HOST),
    reason=(
        "requires USERTEST_RUN_ISOLATED_DOCKER_TESTS=1, "
        "USERTEST_ISOLATED_DOCKER_DAEMON=1, and an explicit DOCKER_HOST"
    ),
)


def _docker(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    """Run Docker only inside an explicitly opted-in isolated-daemon test."""

    return subprocess.run(
        ["docker", *args],
        cwd=str(cwd),
        capture_output=True,
        check=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def _build_identity(*, repo: str, tag: str, label: str, work_dir: Path) -> str:
    context_dir = work_dir / tag
    context_dir.mkdir()
    (context_dir / "Dockerfile").write_text(
        f"FROM scratch\nLABEL isolated_cleanup_test={label!r}\n",
        encoding="utf-8",
    )
    ref = f"{repo}:{tag}"
    _docker("build", "--no-cache", "-t", ref, str(context_dir), cwd=work_dir)
    return _docker("image", "inspect", "--format", "{{.Id}}", ref, cwd=work_dir).stdout.strip()


def test_cleanup_on_isolated_daemon_bounds_managed_identities_and_reports_external_blocker(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Exercise aliases, safety identities, actual inventory, and external retention."""

    # The marker above is intentionally redundant with this assertion: it prevents
    # a future marker edit from silently allowing Docker's default daemon.
    assert _OPT_IN and _ISOLATED and _DOCKER_HOST
    _docker("version", cwd=tmp_path)

    namespace = f"usertest-isolated-{uuid4().hex[:12]}"
    local_repo = namespace
    published_repo = f"{namespace}-published"
    external_repo = f"{namespace}-external"
    cfg = backend_mod.MaintenanceDockerConfig(
        local_image_repo=local_repo,
        published_image_repo=published_repo,
        pull_policy="never",
        seed_root="/unused",
        cache_root_subdir="unused",
        publish_branches=(),
        cleanup_enabled=True,
        keep_local_count=1,
        protect_tags=("protected-latest", "running-latest", "required-latest"),
        cleanup_on_prepare=True,
        cleanup_dry_run_default=False,
    )
    monkeypatch.setattr(backend_mod, "_load_maintenance_docker_config", lambda **_kwargs: cfg)

    refs_to_remove: list[str] = []
    try:
        # A burst of ordinary paired aliases exceeds the single-identity budget.
        ordinary_ids: dict[str, str] = {}
        for number in range(3):
            tag = f"{number:016x}"
            image_id = _build_identity(
                repo=local_repo,
                tag=tag,
                label=f"ordinary-{number}",
                work_dir=tmp_path,
            )
            local_ref = f"{local_repo}:{tag}"
            published_ref = f"{published_repo}:{tag}"
            _docker("tag", local_ref, published_ref, cwd=tmp_path)
            refs_to_remove.extend([local_ref, published_ref])
            ordinary_ids[tag] = image_id

        # The oldest overflow candidate remains physically present through a tag
        # outside the managed repositories, which must be reported as a blocker.
        external_ref = f"{external_repo}:keep"
        _docker("tag", f"{local_repo}:{'0' * 16}", external_ref, cwd=tmp_path)
        refs_to_remove.append(external_ref)

        protected_ids: dict[str, str] = {}
        for name, alias in (
            ("current", "dev-latest"),
            ("protected", "protected-latest"),
            ("running", "running-latest"),
            ("required", "required-latest"),
        ):
            tag = f"{len(protected_ids) + 16:016x}"
            image_id = _build_identity(
                repo=local_repo,
                tag=tag,
                label=name,
                work_dir=tmp_path,
            )
            local_ref = f"{local_repo}:{tag}"
            published_ref = f"{published_repo}:{tag}"
            _docker("tag", local_ref, published_ref, cwd=tmp_path)
            _docker("tag", local_ref, f"{local_repo}:{alias}", cwd=tmp_path)
            refs_to_remove.extend([local_ref, published_ref, f"{local_repo}:{alias}"])
            protected_ids[name] = image_id

        before = backend_mod.list_local_maintenance_images(repo_root=tmp_path)
        summary = backend_mod.cleanup_local_maintenance_images(
            repo_root=tmp_path,
            dry_run=False,
            protected_refs=(
                f"{local_repo}:{16:016x}",
                f"{published_repo}:{16:016x}",
                f"{local_repo}:{19:016x}",
                f"{published_repo}:{19:016x}",
            ),
        )

        assert len(before["entries"]) > 2 * cfg.keep_local_count
        assert summary["after_inventory"] is not None
        assert summary["managed_tag_bounded"] is True
        assert len(summary["remaining_ordinary_image_ids"]) <= cfg.keep_local_count
        assert set(protected_ids.values()) <= set(summary["remaining_protected_image_ids"])
        assert ordinary_ids["0" * 16] in summary["retained_candidate_image_ids"]
        assert summary["externally_retained_refs"][ordinary_ids["0" * 16]] == [external_ref]
        assert summary["physical_identity_bounded"] is False
        assert summary["bounded"] is False
    finally:
        # This runs only after explicit opt-in against the caller's isolated daemon.
        for ref in reversed(refs_to_remove):
            subprocess.run(
                ["docker", "image", "rm", "--force", ref],
                cwd=str(tmp_path),
                capture_output=True,
                check=False,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
