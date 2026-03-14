from __future__ import annotations

import json
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


def test_cleanup_dry_run_keeps_recent_and_protected_tags(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Dry-run cleanup should only target old unprotected hash tags."""

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
    assert "ghcr.io/jcmullwh/usertest-maintenance:cccccccccccccccc" in summary["kept_tags"]
    assert "usertest-maintenance:dddddddddddddddd" in summary["deleted_tags"]
    assert summary["deleted_image_ids"] == []


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
