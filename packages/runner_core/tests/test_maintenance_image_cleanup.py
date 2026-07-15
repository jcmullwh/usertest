from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import runner_core.execution_backend as backend_mod


def _created(days_ago: int) -> str:
    """Return an ISO-8601 timestamp for a relative UTC day offset."""

    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


def _cfg() -> backend_mod.MaintenanceDockerConfig:
    """Build a maintenance config with conservative retention defaults for tests."""

    return backend_mod.MaintenanceDockerConfig(
        local_image_repo="usertest-maintenance",
        published_image_repo="ghcr.io/jcmullwh/usertest-maintenance",
        pull_policy="if_missing",
        seed_root="/opt/usertest_maint_seed",
        cache_root_subdir="usertest_maint_venvs",
        publish_branches=("dev", "main"),
        cleanup_enabled=True,
        keep_local_count=2,
        keep_local_days=7,
        keep_branch_alias_tags=True,
        protect_tags=("bench-dfc31ac",),
        cleanup_on_prepare=True,
        cleanup_dry_run_default=False,
    )


def test_list_local_maintenance_images_filters_and_marks_protected(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Only maintenance repos should be listed, with protected tags annotated."""

    monkeypatch.setattr(backend_mod, "_load_maintenance_docker_config", lambda **_kwargs: _cfg())
    monkeypatch.setattr(
        backend_mod,
        "_docker_image_ls_rows",
        lambda **_kwargs: [
            {"repository": "usertest-maintenance", "tag": "dev-latest", "image_id": "sha256:1"},
            {
                "repository": "ghcr.io/jcmullwh/usertest-maintenance",
                "tag": "aaaaaaaaaaaaaaaa",
                "image_id": "sha256:2",
            },
            {"repository": "python", "tag": "3.11", "image_id": "sha256:3"},
        ],
    )
    monkeypatch.setattr(
        backend_mod,
        "_docker_image_inspect_rows",
        lambda refs_or_ids, **_kwargs: [
            {"Id": "sha256:1", "Created": _created(0)},
            {"Id": "sha256:2", "Created": _created(1)},
            {"Id": "sha256:3", "Created": _created(2)},
        ],
    )

    payload = backend_mod.list_local_maintenance_images(repo_root=tmp_path)

    assert payload["repos_scanned"] == [
        "usertest-maintenance",
        "ghcr.io/jcmullwh/usertest-maintenance",
    ]
    refs = {entry["ref"]: entry for entry in payload["entries"]}
    assert set(refs) == {
        "usertest-maintenance:dev-latest",
        "ghcr.io/jcmullwh/usertest-maintenance:aaaaaaaaaaaaaaaa",
    }
    assert refs["usertest-maintenance:dev-latest"]["protected"] is True
    assert refs["ghcr.io/jcmullwh/usertest-maintenance:aaaaaaaaaaaaaaaa"]["hash_tag"] is True


def test_cleanup_dry_run_bounds_recent_identities_and_keeps_protected_tags(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Dry-run cleanup should cap ordinary identities even when they are recent."""

    monkeypatch.setattr(backend_mod, "_load_maintenance_docker_config", lambda **_kwargs: _cfg())
    monkeypatch.setattr(
        backend_mod,
        "list_local_maintenance_images",
        lambda **_kwargs: {
            "schema_version": 1,
            "repos_scanned": [
                "usertest-maintenance",
                "ghcr.io/jcmullwh/usertest-maintenance",
            ],
            "protected_tags": ["bench-dfc31ac", "dev-latest", "latest", "main-latest"],
            "entries": [
                {
                    "repository": "usertest-maintenance",
                    "tag": "dev-latest",
                    "image_id": "sha256:dev",
                    "created_at": _created(100),
                },
                {
                    "repository": "usertest-maintenance",
                    "tag": "aaaaaaaaaaaaaaaa",
                    "image_id": "sha256:a",
                    "created_at": _created(0),
                },
                {
                    "repository": "usertest-maintenance",
                    "tag": "bbbbbbbbbbbbbbbb",
                    "image_id": "sha256:b",
                    "created_at": _created(1),
                },
                {
                    "repository": "ghcr.io/jcmullwh/usertest-maintenance",
                    "tag": "cccccccccccccccc",
                    "image_id": "sha256:c",
                    "created_at": _created(2),
                },
                {
                    "repository": "usertest-maintenance",
                    "tag": "dddddddddddddddd",
                    "image_id": "sha256:d",
                    "created_at": _created(30),
                },
                {
                    "repository": "usertest-maintenance",
                    "tag": "bench-dfc31ac",
                    "image_id": "sha256:bench",
                    "created_at": _created(365),
                },
            ],
        },
    )

    summary = backend_mod.cleanup_local_maintenance_images(
        repo_root=tmp_path,
        dry_run=True,
    )

    assert "usertest-maintenance:dev-latest" in summary["kept_tags"]
    assert "usertest-maintenance:bench-dfc31ac" in summary["kept_tags"]
    assert "usertest-maintenance:aaaaaaaaaaaaaaaa" in summary["kept_tags"]
    assert "usertest-maintenance:bbbbbbbbbbbbbbbb" in summary["kept_tags"]
    assert (
        "ghcr.io/jcmullwh/usertest-maintenance:cccccccccccccccc"
        in summary["projected_deleted_tags"]
    )
    assert "usertest-maintenance:dddddddddddddddd" in summary["projected_deleted_tags"]
    assert summary["deleted_tags"] == []
    assert summary["deleted_image_ids"] == []


def test_cleanup_retains_all_aliases_for_selected_image_identities(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The count budget applies once per image, rather than once per repository tag."""

    cfg = _cfg()
    cfg = backend_mod.MaintenanceDockerConfig(
        local_image_repo=cfg.local_image_repo,
        published_image_repo=cfg.published_image_repo,
        pull_policy=cfg.pull_policy,
        seed_root=cfg.seed_root,
        cache_root_subdir=cfg.cache_root_subdir,
        publish_branches=cfg.publish_branches,
        cleanup_enabled=cfg.cleanup_enabled,
        keep_local_count=1,
        keep_local_days=cfg.keep_local_days,
        keep_branch_alias_tags=cfg.keep_branch_alias_tags,
        protect_tags=cfg.protect_tags,
        cleanup_on_prepare=cfg.cleanup_on_prepare,
        cleanup_dry_run_default=cfg.cleanup_dry_run_default,
    )
    monkeypatch.setattr(backend_mod, "_load_maintenance_docker_config", lambda **_kwargs: cfg)
    monkeypatch.setattr(
        backend_mod,
        "list_local_maintenance_images",
        lambda **_kwargs: {
            "schema_version": 1,
            "repos_scanned": [cfg.local_image_repo, cfg.published_image_repo],
            "protected_tags": [],
            "entries": [
                {
                    "repository": cfg.local_image_repo,
                    "tag": "aaaaaaaaaaaaaaaa",
                    "image_id": "sha256:new",
                    "created_at": _created(0),
                },
                {
                    "repository": cfg.published_image_repo,
                    "tag": "aaaaaaaaaaaaaaaa",
                    "image_id": "sha256:new",
                    "created_at": _created(0),
                },
                {
                    "repository": cfg.local_image_repo,
                    "tag": "bbbbbbbbbbbbbbbb",
                    "image_id": "sha256:old",
                    "created_at": _created(1),
                },
                {
                    "repository": cfg.published_image_repo,
                    "tag": "bbbbbbbbbbbbbbbb",
                    "image_id": "sha256:old",
                    "created_at": _created(1),
                },
            ],
        },
    )

    summary = backend_mod.cleanup_local_maintenance_images(repo_root=tmp_path, dry_run=True)

    assert summary["kept_image_ids"] == ["sha256:new"]
    assert set(summary["kept_tags"]) == {
        "usertest-maintenance:aaaaaaaaaaaaaaaa",
        "ghcr.io/jcmullwh/usertest-maintenance:aaaaaaaaaaaaaaaa",
    }
    assert set(summary["projected_deleted_tags"]) == {
        "usertest-maintenance:bbbbbbbbbbbbbbbb",
        "ghcr.io/jcmullwh/usertest-maintenance:bbbbbbbbbbbbbbbb",
    }
    assert summary["deleted_tags"] == []


def test_cleanup_does_not_delete_when_ordinary_identities_are_below_budget(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A count budget is a ceiling, not a mandate to delete retained identities."""

    monkeypatch.setattr(backend_mod, "_load_maintenance_docker_config", lambda **_kwargs: _cfg())
    monkeypatch.setattr(
        backend_mod,
        "list_local_maintenance_images",
        lambda **_kwargs: {
            "schema_version": 1,
            "repos_scanned": [],
            "protected_tags": [],
            "entries": [
                {
                    "repository": "usertest-maintenance",
                    "tag": "aaaaaaaaaaaaaaaa",
                    "image_id": "sha256:a",
                    "created_at": _created(0),
                },
                {
                    "repository": "usertest-maintenance",
                    "tag": "bbbbbbbbbbbbbbbb",
                    "image_id": "sha256:b",
                    "created_at": _created(1),
                },
            ],
        },
    )

    summary = backend_mod.cleanup_local_maintenance_images(repo_root=tmp_path, dry_run=True)

    assert summary["kept_image_ids"] == ["sha256:a", "sha256:b"]
    assert summary["projected_deleted_tags"] == []
    assert summary["deleted_tags"] == []


def test_cleanup_protects_every_alias_of_configured_current_identity(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Configured current aliases protect their complete image identity at budget zero."""

    cfg = replace(_cfg(), keep_local_count=0)
    monkeypatch.setattr(backend_mod, "_load_maintenance_docker_config", lambda **_kwargs: cfg)
    monkeypatch.setattr(
        backend_mod,
        "list_local_maintenance_images",
        lambda **_kwargs: {
            "schema_version": 1,
            "repos_scanned": [],
            "protected_tags": [],
            "entries": [
                {
                    "repository": cfg.local_image_repo,
                    "tag": "aaaaaaaaaaaaaaaa",
                    "image_id": "sha256:current",
                    "created_at": _created(1),
                },
                {
                    "repository": cfg.published_image_repo,
                    "tag": "aaaaaaaaaaaaaaaa",
                    "image_id": "sha256:current",
                    "created_at": _created(1),
                },
                {
                    "repository": cfg.local_image_repo,
                    "tag": "bbbbbbbbbbbbbbbb",
                    "image_id": "sha256:ordinary",
                    "created_at": _created(0),
                },
            ],
        },
    )

    summary = backend_mod.cleanup_local_maintenance_images(
        repo_root=tmp_path,
        dry_run=True,
        protected_refs=(
            "usertest-maintenance:aaaaaaaaaaaaaaaa",
            "ghcr.io/jcmullwh/usertest-maintenance:aaaaaaaaaaaaaaaa",
        ),
    )

    assert summary["kept_image_ids"] == ["sha256:current"]
    assert set(summary["kept_tags"]) == {
        "usertest-maintenance:aaaaaaaaaaaaaaaa",
        "ghcr.io/jcmullwh/usertest-maintenance:aaaaaaaaaaaaaaaa",
    }
    assert summary["projected_deleted_tags"] == ["usertest-maintenance:bbbbbbbbbbbbbbbb"]


def test_resolver_cleans_before_a_forced_build(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Automatic cleanup must run before a build writes to Docker's local store."""

    context_dir = tmp_path / "context"
    context_dir.mkdir()
    (context_dir / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
    calls: list[str] = []
    captured_cleanup: dict[str, object] = {}

    monkeypatch.setattr(backend_mod, "_load_maintenance_docker_config", lambda **_kwargs: _cfg())
    monkeypatch.setattr(
        backend_mod,
        "_prepare_maintenance_image_context",
        lambda **_kwargs: (context_dir, {}),
    )
    monkeypatch.setattr(backend_mod, "compute_image_hash", lambda **_kwargs: "a" * 64)
    monkeypatch.setattr(backend_mod, "_git_remote_url", lambda **_kwargs: None)
    monkeypatch.setattr(
        backend_mod,
        "_docker_image_inspect_rows",
        lambda *_args, **_kwargs: [
            {
                "RepoTags": [
                    "usertest-maintenance:" + ("a" * 16),
                    "ghcr.io/jcmullwh/usertest-maintenance:" + ("a" * 16),
                ]
            }
        ],
    )

    def _fake_cleanup(**kwargs):
        calls.append("cleanup")
        captured_cleanup.update(kwargs)
        return {"schema_version": 1, "deleted_tags": [], "deleted_image_ids": [], "errors": []}

    monkeypatch.setattr(backend_mod, "cleanup_local_maintenance_images", _fake_cleanup)
    monkeypatch.setattr(
        backend_mod,
        "_build_maintenance_image",
        lambda **_kwargs: calls.append("build"),
    )

    resolution = backend_mod.resolve_maintenance_docker_image(
        repo_root=tmp_path,
        run_dir=tmp_path / "run",
        force_rebuild=True,
        timeout_seconds=1,
    )

    tag = "a" * 16
    assert calls == ["cleanup", "build", "cleanup"]
    assert set(captured_cleanup["protected_refs"]) == {
        f"usertest-maintenance:{tag}",
        f"ghcr.io/jcmullwh/usertest-maintenance:{tag}",
    }
    assert resolution.metadata["cleanup"]["errors"] == []
    assert resolution.metadata["cleanup"]["prewrite"] is not None
    assert resolution.metadata["cleanup"]["postresolution"] is not None


def test_cleanup_deletes_tags_and_unreferenced_image_ids(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Cleanup should remove old tags and then remove image ids with no remaining tags."""

    cfg = _cfg()
    cfg = backend_mod.MaintenanceDockerConfig(
        local_image_repo=cfg.local_image_repo,
        published_image_repo=cfg.published_image_repo,
        pull_policy=cfg.pull_policy,
        seed_root=cfg.seed_root,
        cache_root_subdir=cfg.cache_root_subdir,
        publish_branches=cfg.publish_branches,
        cleanup_enabled=cfg.cleanup_enabled,
        keep_local_count=0,
        keep_local_days=7,
        keep_branch_alias_tags=cfg.keep_branch_alias_tags,
        protect_tags=cfg.protect_tags,
        cleanup_on_prepare=cfg.cleanup_on_prepare,
        cleanup_dry_run_default=cfg.cleanup_dry_run_default,
    )
    monkeypatch.setattr(backend_mod, "_load_maintenance_docker_config", lambda **_kwargs: cfg)
    monkeypatch.setattr(
        backend_mod,
        "list_local_maintenance_images",
        lambda **_kwargs: {
            "schema_version": 1,
            "repos_scanned": [
                "usertest-maintenance",
                "ghcr.io/jcmullwh/usertest-maintenance",
            ],
            "protected_tags": ["dev-latest", "latest", "main-latest"],
            "entries": [
                {
                    "repository": "usertest-maintenance",
                    "tag": "aaaaaaaaaaaaaaaa",
                    "image_id": "sha256:a",
                    "created_at": _created(30),
                },
                {
                    "repository": "ghcr.io/jcmullwh/usertest-maintenance",
                    "tag": "bbbbbbbbbbbbbbbb",
                    "image_id": "sha256:b",
                    "created_at": _created(31),
                },
            ],
        },
    )

    removed: list[tuple[str, ...]] = []

    def _fake_run(argv: list[str], **_kwargs):
        removed.append(tuple(argv))
        return type("Proc", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(backend_mod, "_run_subprocess", _fake_run)

    def _fake_inspect(refs_or_ids: list[str], **_kwargs):
        ref = refs_or_ids[0]
        if ref == "sha256:a":
            return [{"Id": "sha256:a", "RepoTags": []}]
        if ref == "sha256:b":
            return [{"Id": "sha256:b", "RepoTags": ["other:keep"]}]
        return []

    monkeypatch.setattr(backend_mod, "_docker_image_inspect_rows", _fake_inspect)

    summary = backend_mod.cleanup_local_maintenance_images(repo_root=tmp_path, dry_run=False)

    assert "usertest-maintenance:aaaaaaaaaaaaaaaa" in summary["deleted_tags"]
    assert "ghcr.io/jcmullwh/usertest-maintenance:bbbbbbbbbbbbbbbb" in summary["deleted_tags"]
    assert "sha256:a" in summary["deleted_image_ids"]
    assert "sha256:b" not in summary["deleted_image_ids"]
    assert ("docker", "image", "rm", "usertest-maintenance:aaaaaaaaaaaaaaaa") in removed
    assert (
        "docker",
        "image",
        "rm",
        "ghcr.io/jcmullwh/usertest-maintenance:bbbbbbbbbbbbbbbb",
    ) in removed
    assert ("docker", "image", "rm", "sha256:a") in removed


def test_cleanup_reports_actual_post_inventory_after_partial_tag_deletion_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Actual remaining state must not be inferred from attempted Docker removals."""

    cfg = replace(_cfg(), keep_local_count=0)
    monkeypatch.setattr(backend_mod, "_load_maintenance_docker_config", lambda **_kwargs: cfg)
    before_inventory = {
        "schema_version": 1,
        "repos_scanned": [cfg.local_image_repo, cfg.published_image_repo],
        "protected_tags": [],
        "entries": [
            {
                "repository": cfg.local_image_repo,
                "tag": "aaaaaaaaaaaaaaaa",
                "image_id": "sha256:failed",
                "created_at": _created(0),
            },
            {
                "repository": cfg.local_image_repo,
                "tag": "bbbbbbbbbbbbbbbb",
                "image_id": "sha256:deleted",
                "created_at": _created(1),
            },
        ],
    }
    after_inventory = {
        **before_inventory,
        "entries": [before_inventory["entries"][0]],
    }
    inventories = [before_inventory, after_inventory]
    monkeypatch.setattr(
        backend_mod,
        "list_local_maintenance_images",
        lambda **_kwargs: inventories.pop(0),
    )

    def _fake_run(argv: list[str], **_kwargs):
        if argv[-1] == "usertest-maintenance:aaaaaaaaaaaaaaaa":
            return type("Proc", (), {"returncode": 1, "stdout": "", "stderr": "in use"})()
        return type("Proc", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(backend_mod, "_run_subprocess", _fake_run)
    monkeypatch.setattr(backend_mod, "_docker_image_inspect_rows", lambda *_args, **_kwargs: [])

    summary = backend_mod.cleanup_local_maintenance_images(repo_root=tmp_path, dry_run=False)

    assert set(summary["attempted_deleted_tags"]) == {
        "usertest-maintenance:aaaaaaaaaaaaaaaa",
        "usertest-maintenance:bbbbbbbbbbbbbbbb",
    }
    assert summary["deleted_tags"] == ["usertest-maintenance:bbbbbbbbbbbbbbbb"]
    assert summary["remaining_ordinary_image_ids"] == ["sha256:failed"]
    assert summary["remaining_aliases"] == {
        "sha256:failed": ["usertest-maintenance:aaaaaaaaaaaaaaaa"]
    }
    assert summary["bounded"] is False
    assert any("usertest-maintenance:aaaaaaaaaaaaaaaa" in error for error in summary["errors"])


def test_cleanup_writes_artifact(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Cleanup should persist a structured artifact when requested."""

    monkeypatch.setattr(backend_mod, "_load_maintenance_docker_config", lambda **_kwargs: _cfg())
    monkeypatch.setattr(
        backend_mod,
        "list_local_maintenance_images",
        lambda **_kwargs: {
            "schema_version": 1,
            "repos_scanned": [
                "usertest-maintenance",
                "ghcr.io/jcmullwh/usertest-maintenance",
            ],
            "protected_tags": [],
            "entries": [],
        },
    )

    artifact_path = tmp_path / "sandbox" / "maintenance_image_cleanup.json"
    summary = backend_mod.cleanup_local_maintenance_images(
        repo_root=tmp_path,
        dry_run=True,
        artifact_path=artifact_path,
    )

    assert artifact_path.exists()
    assert json.loads(artifact_path.read_text(encoding="utf-8")) == summary
