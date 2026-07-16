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


def _cfg(*, keep_local_count: int = 2) -> backend_mod.MaintenanceDockerConfig:
    """Build a maintenance config with conservative retention defaults for tests."""

    return backend_mod.MaintenanceDockerConfig(
        local_image_repo="usertest-maintenance",
        published_image_repo="ghcr.io/jcmullwh/usertest-maintenance",
        pull_policy="if_missing",
        seed_root="/opt/usertest_maint_seed",
        cache_root_subdir="usertest_maint_venvs",
        publish_branches=("dev", "main"),
        cleanup_enabled=True,
        keep_local_count=keep_local_count,
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

    def _fake_ls_rows(*, filters=(), **_kwargs):
        if filters:
            return [{"repository": "<none>", "tag": "<none>", "image_id": "sha256:5"}]
        return [
            {"repository": "usertest-maintenance", "tag": "dev-latest", "image_id": "sha256:1"},
            {
                "repository": "ghcr.io/jcmullwh/usertest-maintenance",
                "tag": "aaaaaaaaaaaaaaaa",
                "image_id": "sha256:2",
            },
            {
                "repository": "usertest-maintenance-depth",
                "tag": "a0ffd921",
                "image_id": "sha256:4",
            },
            {"repository": "python", "tag": "3.11", "image_id": "sha256:3"},
        ]

    monkeypatch.setattr(backend_mod, "_docker_image_ls_rows", _fake_ls_rows)
    monkeypatch.setattr(
        backend_mod,
        "_docker_image_inspect_rows",
        lambda refs_or_ids, **_kwargs: [
            {"Id": "sha256:1", "Created": _created(0)},
            {"Id": "sha256:2", "Created": _created(1)},
            {
                "Id": "sha256:4",
                "Created": _created(2),
                "RepoTags": ["usertest-maintenance-depth:a0ffd921"],
            },
            {"Id": "sha256:5", "Created": _created(3), "RepoTags": []},
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
        "usertest-maintenance-depth:a0ffd921",
        "",
    }
    assert refs["usertest-maintenance:dev-latest"]["protected"] is True
    assert refs["ghcr.io/jcmullwh/usertest-maintenance:aaaaaaaaaaaaaaaa"]["hash_tag"] is True


def test_cleanup_dry_run_enforces_unique_id_cap_even_when_every_image_is_recent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The age window must not let a rapid burst exceed the unique-image hard cap."""

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
                    "image_id": "sha256:dev",
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
    assert "usertest-maintenance:bbbbbbbbbbbbbbbb" in summary["deleted_tags"]
    assert "ghcr.io/jcmullwh/usertest-maintenance:cccccccccccccccc" in summary["deleted_tags"]
    assert "usertest-maintenance:dddddddddddddddd" in summary["deleted_tags"]
    assert summary["deleted_image_ids"] == []
    assert summary["retention"] == {
        "unit": "unique_owned_image_id",
        "hard_cap": 2,
        "maximum_age_days": 7,
        "owned_image_count_before": 5,
        "required_image_count": 1,
        "required_overflow": 0,
        "planned_kept_image_count": 2,
        "planned_delete_image_count": 3,
    }


def test_cleanup_deletes_tags_and_unreferenced_image_ids(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Cleanup should remove old tags and then remove image ids with no remaining tags."""

    cfg = _cfg(keep_local_count=0)
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
                    "repo_tags": [
                        "ghcr.io/jcmullwh/usertest-maintenance:bbbbbbbbbbbbbbbb",
                        "other:keep",
                    ],
                },
            ],
        },
    )

    removed: list[tuple[str, ...]] = []

    def _fake_run(argv: list[str], **_kwargs):
        removed.append(tuple(argv))
        return type("Proc", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(backend_mod, "_run_subprocess", _fake_run)

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
    assert not any("prune" in command for argv in removed for command in argv)


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


def test_cleanup_preserves_active_and_running_images_even_when_they_exceed_cap(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Current and running IDs are safety exemptions; overflow must remain visible."""

    monkeypatch.setattr(
        backend_mod,
        "_load_maintenance_docker_config",
        lambda **_kwargs: _cfg(keep_local_count=1),
    )
    monkeypatch.setattr(
        backend_mod,
        "list_local_maintenance_images",
        lambda **_kwargs: {
            "repos_scanned": ["usertest-maintenance"],
            "protected_tags": [],
            "entries": [
                {
                    "repository": "usertest-maintenance",
                    "tag": "active",
                    "image_id": "sha256:active",
                    "created_at": _created(2),
                },
                {
                    "repository": "usertest-maintenance-depth",
                    "tag": "running",
                    "image_id": "sha256:running",
                    "created_at": _created(1),
                },
                {
                    "repository": "usertest-maintenance",
                    "tag": "extra",
                    "image_id": "sha256:extra",
                    "created_at": _created(0),
                },
            ],
        },
    )
    monkeypatch.setattr(
        backend_mod,
        "_docker_running_container_image_ids",
        lambda **_kwargs: {"sha256:running"},
    )

    summary = backend_mod.cleanup_local_maintenance_images(
        repo_root=tmp_path,
        dry_run=True,
        active_image_refs=("usertest-maintenance:active",),
    )

    assert summary["required_image_ids"] == ["sha256:active", "sha256:running"]
    assert summary["retention"]["required_overflow"] == 1
    assert summary["would_delete_image_ids"] == ["sha256:extra"]


def test_cleanup_uses_remaining_cap_for_one_recent_predecessor(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """With no other required IDs, current plus one recent predecessor fit the cap."""

    monkeypatch.setattr(backend_mod, "_load_maintenance_docker_config", lambda **_kwargs: _cfg())
    monkeypatch.setattr(
        backend_mod,
        "list_local_maintenance_images",
        lambda **_kwargs: {
            "repos_scanned": ["usertest-maintenance"],
            "protected_tags": [],
            "entries": [
                {
                    "ref": "usertest-maintenance:current",
                    "image_id": "sha256:current",
                    "created_at": _created(0),
                },
                {
                    "ref": "usertest-maintenance:handoff",
                    "image_id": "sha256:handoff",
                    "created_at": _created(1),
                },
                {
                    "ref": "usertest-maintenance:old",
                    "image_id": "sha256:old",
                    "created_at": _created(2),
                },
            ],
        },
    )
    monkeypatch.setattr(
        backend_mod,
        "_docker_running_container_image_ids",
        lambda **_kwargs: set(),
    )

    summary = backend_mod.cleanup_local_maintenance_images(
        repo_root=tmp_path,
        dry_run=True,
        active_image_refs=("usertest-maintenance:current",),
    )

    assert summary["kept_image_ids"] == ["sha256:current", "sha256:handoff"]
    assert summary["would_delete_image_ids"] == ["sha256:old"]


def test_cleanup_fails_closed_when_running_container_inventory_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Unknown live use must suppress deletion rather than guessing."""

    monkeypatch.setattr(
        backend_mod,
        "_load_maintenance_docker_config",
        lambda **_kwargs: _cfg(keep_local_count=0),
    )
    monkeypatch.setattr(
        backend_mod,
        "list_local_maintenance_images",
        lambda **_kwargs: {
            "repos_scanned": ["usertest-maintenance"],
            "protected_tags": [],
            "entries": [
                {
                    "repository": "usertest-maintenance",
                    "tag": "old",
                    "image_id": "sha256:old",
                    "created_at": _created(30),
                }
            ],
        },
    )
    monkeypatch.setattr(
        backend_mod,
        "_docker_running_container_image_ids",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("daemon unavailable")),
    )
    monkeypatch.setattr(
        backend_mod,
        "_run_subprocess",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not delete")),
    )

    summary = backend_mod.cleanup_local_maintenance_images(repo_root=tmp_path, dry_run=False)

    assert summary["would_delete_image_ids"] == []
    assert summary["errors"] == [
        "Running-container inventory failed; cleanup skipped: daemon unavailable"
    ]


def test_maintenance_build_marks_image_ownership(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """New images must remain identifiable after their last managed tag is removed."""

    captured: dict[str, object] = {}

    def _fake_run(argv, **kwargs):
        captured["argv"] = argv
        captured["cwd"] = kwargs["cwd"]
        return type("Proc", (), {"returncode": 0})()

    monkeypatch.setattr(backend_mod.subprocess, "run", _fake_run)
    backend_mod._build_maintenance_image(
        context_dir=tmp_path,
        local_ref="usertest-maintenance:abc",
        published_ref="ghcr.io/example/usertest-maintenance:abc",
        timeout_seconds=None,
        log_path=tmp_path / "build.log",
    )

    argv = captured["argv"]
    assert isinstance(argv, list)
    assert "--label" in argv
    assert "io.usertest.maintenance-image=true" in argv


def test_resolution_cleans_before_build_and_again_afterward(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Old images must be removed before a new build can consume their Docker-disk space."""

    cfg = replace(_cfg(keep_local_count=1), pull_policy="never")
    context_dir = tmp_path / "context"
    context_dir.mkdir()
    (context_dir / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
    events: list[str] = []

    monkeypatch.setattr(backend_mod, "_load_maintenance_docker_config", lambda **_kwargs: cfg)
    monkeypatch.setattr(
        backend_mod,
        "_prepare_maintenance_image_context",
        lambda **_kwargs: (context_dir, {}),
    )
    monkeypatch.setattr(backend_mod, "compute_image_hash", lambda **_kwargs: "a" * 64)
    monkeypatch.setattr(backend_mod, "_docker_image_exists_local", lambda **_kwargs: False)
    monkeypatch.setattr(backend_mod, "_git_remote_url", lambda **_kwargs: None)

    def _fake_cleanup(**kwargs):
        phase = (
            "cleanup_pre"
            if kwargs["artifact_path"].name == "maintenance_image_cleanup_pre_resolution.json"
            else "cleanup_post"
        )
        events.append(phase)
        summary = {"errors": [], "deleted_image_ids": [phase]}
        kwargs["artifact_path"].write_text(json.dumps(summary), encoding="utf-8")
        return summary

    def _fake_build(**_kwargs):
        events.append("build")

    monkeypatch.setattr(backend_mod, "cleanup_local_maintenance_images", _fake_cleanup)
    monkeypatch.setattr(backend_mod, "_build_maintenance_image", _fake_build)

    result = backend_mod.resolve_maintenance_docker_image(
        repo_root=tmp_path,
        run_dir=tmp_path / "run",
        timeout_seconds=None,
    )

    assert events == ["cleanup_pre", "build", "cleanup_post"]
    assert result.metadata["cleanup_before_resolution"]["deleted_image_ids"] == [
        "cleanup_pre"
    ]
    assert result.metadata["cleanup"]["deleted_image_ids"] == ["cleanup_post"]
