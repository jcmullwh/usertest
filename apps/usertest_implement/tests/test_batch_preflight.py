from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
import yaml
from runner_core.execution_backend import MaintenanceDockerConfig

from usertest_implement import batch_preflight


def test_effective_handoff_flags_follow_dependency_chain() -> None:
    assert batch_preflight._effective_handoff_flags(
        {"commit": False, "push": True, "pr": True}
    ) == (False, False, False)
    assert batch_preflight._effective_handoff_flags(
        {"commit": True, "push": False, "pr": True}
    ) == (True, False, False)
    assert batch_preflight._effective_handoff_flags(
        {"commit": True, "push": True, "pr": True}
    ) == (True, True, True)
    assert batch_preflight._effective_handoff_flags(
        {"commit": "true", "push": "false", "pr": "true"}
    ) == (True, False, False)


def _completed(
    argv: list[str], *, returncode: int = 0, stdout: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(argv, returncode, stdout=stdout, stderr="")


def test_git_branch_accepts_detached_head(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        batch_preflight,
        "_run",
        lambda argv, **kwargs: _completed(argv, stdout="\n"),
    )

    assert batch_preflight._git_branch(tmp_path) is None


def test_default_batch_config_reuses_successful_exact_commit_ci() -> None:
    repo_root = Path(__file__).resolve().parents[3]
    config = yaml.safe_load(
        (repo_root / "configs" / "backlog_implement_batch.yaml").read_text(
            encoding="utf-8"
        )
    )

    assert config["defaults"]["require_local_green"] is True
    assert config["defaults"]["require_ci_green_for_base"] is True
    assert config["defaults"]["reuse_successful_ci_for_local_green"] is True


def test_batch_preflight_resolves_detached_identity_before_local_qualification(
    tmp_path: Path,
    monkeypatch,
) -> None:
    settings_path = tmp_path / "settings.yaml"
    settings_path.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "default_profile": "local_exercise",
                "profiles": {
                    "local_exercise": {"run_common": {"push": False, "pr": False}}
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    events: list[str] = []

    def fake_run(argv: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
        events.append("run:" + " ".join(argv))
        return _completed(argv)

    monkeypatch.setattr(batch_preflight, "_run", fake_run)
    monkeypatch.setattr(
        batch_preflight,
        "_git_branch",
        lambda _: events.append("identity:branch") or None,
    )
    monkeypatch.setattr(
        batch_preflight,
        "_git_head",
        lambda _: events.append("identity:head") or "abc123",
    )
    monkeypatch.setattr(batch_preflight, "_gitlab_registry_probe", lambda: None)

    result = batch_preflight.run_batch_preflight(
        repo_root=tmp_path,
        batch_dir=tmp_path / "batch",
        batch_config={
            "defaults": {
                "require_clean_git": False,
                "require_local_green": True,
                "require_ci_green_for_base": False,
                "run_settings_path": str(settings_path),
                "run_settings_profile": "local_exercise",
            }
        },
        worker_roster=[],
        exec_backend="host",
    )

    assert result["branch"] is None
    assert result["checkout_mode"] == "detached"
    assert result["head_sha"] == "abc123"
    assert events[:2] == ["identity:branch", "identity:head"]
    identity = json.loads(
        (tmp_path / "batch" / "preflight" / "git_identity.json").read_text(
            encoding="utf-8"
        )
    )
    assert identity == {
        "branch": None,
        "checkout_mode": "detached",
        "head_sha": "abc123",
        "schema_version": 1,
    }


def test_batch_preflight_qualifies_ci_by_commit_when_detached(
    tmp_path: Path,
    monkeypatch,
) -> None:
    called: list[list[str]] = []
    ci_runs = json.dumps(
        [
            {
                "databaseId": 7,
                "headSha": "abc123",
                "event": "push",
                "status": "completed",
                "conclusion": "success",
                "createdAt": "2026-07-22T00:00:00Z",
                "url": "https://example.test/actions/7",
            }
        ]
    )

    def fake_run(argv: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
        called.append(argv)
        if argv[:3] == ["gh", "run", "list"]:
            return _completed(argv, stdout=ci_runs)
        return _completed(argv)

    monkeypatch.setattr(batch_preflight, "_run", fake_run)
    monkeypatch.setattr(batch_preflight, "_git_branch", lambda _: None)
    monkeypatch.setattr(batch_preflight, "_git_head", lambda _: "abc123")
    monkeypatch.setattr(batch_preflight, "_gitlab_registry_probe", lambda: None)

    result = batch_preflight.run_batch_preflight(
        repo_root=tmp_path,
        batch_dir=tmp_path / "batch",
        batch_config={
            "defaults": {
                "require_clean_git": False,
                "require_local_green": False,
                "require_ci_green_for_base": True,
            }
        },
        worker_roster=[],
        exec_backend="host",
    )

    ci_query = next(argv for argv in called if argv[:3] == ["gh", "run", "list"])
    assert ci_query[ci_query.index("--commit") + 1] == "abc123"
    assert "--branch" not in ci_query
    assert result["base_ci_run_url"] == "https://example.test/actions/7"
    assert result["blockers"] == []


def test_batch_preflight_reuses_successful_exact_commit_ci_for_local_green(
    tmp_path: Path,
    monkeypatch,
) -> None:
    called: list[list[str]] = []
    ci_runs = json.dumps(
        [
            {
                "databaseId": 7,
                "headSha": "abc123",
                "event": "push",
                "status": "completed",
                "conclusion": "success",
                "createdAt": "2026-07-22T00:00:00Z",
                "url": "https://example.test/actions/7",
            }
        ]
    )

    def fake_run(argv: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
        called.append(argv)
        if argv[:3] == ["gh", "run", "list"]:
            return _completed(argv, stdout=ci_runs)
        return _completed(argv)

    monkeypatch.setattr(batch_preflight, "_run", fake_run)
    monkeypatch.setattr(batch_preflight, "_git_branch", lambda _: None)
    monkeypatch.setattr(batch_preflight, "_git_head", lambda _: "abc123")
    monkeypatch.setattr(batch_preflight, "_gitlab_registry_probe", lambda: None)

    result = batch_preflight.run_batch_preflight(
        repo_root=tmp_path,
        batch_dir=tmp_path / "batch",
        batch_config={
            "defaults": {
                "require_clean_git": False,
                "require_local_green": True,
                "require_ci_green_for_base": True,
                "reuse_successful_ci_for_local_green": True,
            }
        },
        worker_roster=[],
        exec_backend="host",
    )

    assert not any("tools/scaffold/scaffold.py" in argv for argv in called)
    assert result["local_green_source"] == "exact_commit_ci"
    assert result["local_green_satisfied"] is True
    receipt = json.loads(
        (tmp_path / "batch" / "preflight" / "local_green.json").read_text(
            encoding="utf-8"
        )
    )
    assert receipt == {
        "ci_run_url": "https://example.test/actions/7",
        "head_sha": "abc123",
        "lint_executed": False,
        "satisfied": True,
        "schema_version": 1,
        "source": "exact_commit_ci",
        "test_executed": False,
    }
    assert "exact_commit_ci" in (
        tmp_path / "batch" / "preflight" / "local_test.log"
    ).read_text(encoding="utf-8")


def test_batch_preflight_reuses_completed_pr_ci_while_push_twin_is_running(
    tmp_path: Path,
    monkeypatch,
) -> None:
    called: list[list[str]] = []
    ci_runs = json.dumps(
        [
            {
                "databaseId": 9,
                "headSha": "abc123",
                "event": "pull_request",
                "status": "completed",
                "conclusion": "success",
                "createdAt": "2026-07-22T00:00:04Z",
                "url": "https://example.test/actions/9",
            },
            {
                "databaseId": 8,
                "headSha": "abc123",
                "event": "push",
                "status": "in_progress",
                "conclusion": "",
                "createdAt": "2026-07-22T00:00:00Z",
                "url": "https://example.test/actions/8",
            },
        ]
    )

    def fake_run(argv: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
        called.append(argv)
        if argv[:3] == ["gh", "run", "list"]:
            return _completed(argv, stdout=ci_runs)
        return _completed(argv)

    monkeypatch.setattr(batch_preflight, "_run", fake_run)
    monkeypatch.setattr(batch_preflight, "_git_branch", lambda _: None)
    monkeypatch.setattr(batch_preflight, "_git_head", lambda _: "abc123")
    monkeypatch.setattr(batch_preflight, "_gitlab_registry_probe", lambda: None)

    result = batch_preflight.run_batch_preflight(
        repo_root=tmp_path,
        batch_dir=tmp_path / "batch",
        batch_config={
            "defaults": {
                "require_clean_git": False,
                "require_local_green": True,
                "require_ci_green_for_base": True,
                "reuse_successful_ci_for_local_green": True,
            }
        },
        worker_roster=[],
        exec_backend="host",
    )

    assert not any("tools/scaffold/scaffold.py" in argv for argv in called)
    assert result["blockers"] == []
    assert result["base_ci_run_url"] == "https://example.test/actions/9"
    assert result["local_green_source"] == "exact_commit_ci"
    assert result["local_green_satisfied"] is True


def test_batch_preflight_blocks_conflicting_completed_exact_commit_ci(
    tmp_path: Path,
    monkeypatch,
) -> None:
    called: list[list[str]] = []
    ci_runs = json.dumps(
        [
            {
                "databaseId": 9,
                "headSha": "abc123",
                "event": "pull_request",
                "status": "completed",
                "conclusion": "success",
                "createdAt": "2026-07-22T00:00:04Z",
                "url": "https://example.test/actions/9",
            },
            {
                "databaseId": 8,
                "headSha": "abc123",
                "event": "push",
                "status": "completed",
                "conclusion": "failure",
                "createdAt": "2026-07-22T00:00:00Z",
                "url": "https://example.test/actions/8",
            },
        ]
    )

    def fake_run(argv: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
        called.append(argv)
        if argv[:3] == ["gh", "run", "list"]:
            return _completed(argv, stdout=ci_runs)
        return _completed(argv)

    monkeypatch.setattr(batch_preflight, "_run", fake_run)
    monkeypatch.setattr(batch_preflight, "_git_branch", lambda _: None)
    monkeypatch.setattr(batch_preflight, "_git_head", lambda _: "abc123")
    monkeypatch.setattr(batch_preflight, "_gitlab_registry_probe", lambda: None)

    result = batch_preflight.run_batch_preflight(
        repo_root=tmp_path,
        batch_dir=tmp_path / "batch",
        batch_config={
            "defaults": {
                "require_clean_git": False,
                "require_local_green": True,
                "require_ci_green_for_base": True,
                "reuse_successful_ci_for_local_green": True,
            }
        },
        worker_roster=[],
        exec_backend="host",
    )

    assert not any("tools/scaffold/scaffold.py" in argv for argv in called)
    assert result["local_green_source"] == "skipped_base_ci_blocked"
    assert result["local_green_satisfied"] is False
    assert any(
        blocker["summary"] == "Completed CI results for the batch commit conflict."
        for blocker in result["blockers"]
    )


def test_batch_preflight_skips_local_gate_when_mandatory_ci_blocks(
    tmp_path: Path,
    monkeypatch,
) -> None:
    called: list[list[str]] = []
    ci_runs = json.dumps(
        [
            {
                "databaseId": 8,
                "headSha": "abc123",
                "event": "push",
                "status": "completed",
                "conclusion": "failure",
                "createdAt": "2026-07-22T00:00:00Z",
                "url": "https://example.test/actions/8",
            }
        ]
    )

    def fake_run(argv: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
        called.append(argv)
        if argv[:3] == ["gh", "run", "list"]:
            return _completed(argv, stdout=ci_runs)
        return _completed(argv)

    monkeypatch.setattr(batch_preflight, "_run", fake_run)
    monkeypatch.setattr(batch_preflight, "_git_branch", lambda _: "dev")
    monkeypatch.setattr(batch_preflight, "_git_head", lambda _: "abc123")
    monkeypatch.setattr(batch_preflight, "_gitlab_registry_probe", lambda: None)

    result = batch_preflight.run_batch_preflight(
        repo_root=tmp_path,
        batch_dir=tmp_path / "batch",
        batch_config={
            "defaults": {
                "require_clean_git": False,
                "require_local_green": True,
                "require_ci_green_for_base": True,
                "reuse_successful_ci_for_local_green": True,
            }
        },
        worker_roster=[],
        exec_backend="host",
    )

    assert not any("tools/scaffold/scaffold.py" in argv for argv in called)
    assert result["local_green_source"] == "skipped_base_ci_blocked"
    assert result["local_green_satisfied"] is False
    assert any(
        blocker["summary"] == "Latest CI for the batch commit is not green."
        for blocker in result["blockers"]
    )


def test_batch_preflight_skips_github_auth_for_local_exercise_profile(
    tmp_path: Path,
    monkeypatch,
) -> None:
    settings_path = tmp_path / "settings.yaml"
    settings_path.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "default_profile": "local_exercise",
                "profiles": {
                    "local_exercise": {
                        "run_common": {
                            "push": False,
                            "pr": False,
                        }
                    }
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    called: list[list[str]] = []

    def fake_run(argv: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
        called.append(argv)
        return _completed(argv)

    monkeypatch.setattr(batch_preflight, "_run", fake_run)
    monkeypatch.setattr(batch_preflight, "_git_branch", lambda _: "dev")
    monkeypatch.setattr(batch_preflight, "_git_head", lambda _: "abc123")
    monkeypatch.setattr(batch_preflight, "_gitlab_registry_probe", lambda: None)

    result = batch_preflight.run_batch_preflight(
        repo_root=tmp_path,
        batch_dir=tmp_path / "batch",
        batch_config={
            "defaults": {
                "require_clean_git": False,
                "require_local_green": False,
                "require_ci_green_for_base": False,
                "run_settings_path": str(settings_path),
                "run_settings_profile": "local_exercise",
            }
        },
        worker_roster=[],
        exec_backend="host",
    )

    assert result["blockers"] == []
    assert ["gh", "auth", "status"] not in called
    assert "skipped" in (tmp_path / "batch" / "preflight" / "gh_auth.log").read_text(
        encoding="utf-8"
    )


def test_batch_preflight_normalizes_run_handoff_dependencies(
    tmp_path: Path,
    monkeypatch,
) -> None:
    settings_path = tmp_path / "settings.yaml"
    settings_path.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "default_profile": "local_exercise",
                "profiles": {
                    "local_exercise": {
                        "run_common": {"commit": True, "push": True, "pr": True},
                        "run": {"commit": False},
                    }
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    called: list[list[str]] = []

    def fake_run(argv: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
        called.append(argv)
        return _completed(argv)

    monkeypatch.setattr(batch_preflight, "_run", fake_run)
    monkeypatch.setattr(batch_preflight, "_git_branch", lambda _: "dev")
    monkeypatch.setattr(batch_preflight, "_git_head", lambda _: "abc123")
    monkeypatch.setattr(batch_preflight, "_gitlab_registry_probe", lambda: None)

    result = batch_preflight.run_batch_preflight(
        repo_root=tmp_path,
        batch_dir=tmp_path / "batch",
        batch_config={
            "defaults": {
                "require_clean_git": False,
                "require_local_green": False,
                "require_ci_green_for_base": False,
                "run_settings_path": str(settings_path),
                "run_settings_profile": "local_exercise",
            }
        },
        worker_roster=[],
        exec_backend="host",
    )

    assert result["blockers"] == []
    assert ["gh", "auth", "status"] not in called


def test_batch_preflight_accepts_env_token_when_auth_status_reports_stale_default(
    tmp_path: Path,
    monkeypatch,
) -> None:
    called: list[list[str]] = []

    def fake_run(argv: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
        called.append(argv)
        if argv == ["gh", "auth", "status"]:
            return _completed(argv, returncode=1)
        return _completed(argv)

    monkeypatch.setattr(batch_preflight, "_run", fake_run)
    monkeypatch.setattr(batch_preflight, "_git_branch", lambda _: "dev")
    monkeypatch.setattr(batch_preflight, "_git_head", lambda _: "abc123")
    monkeypatch.setattr(batch_preflight, "_gitlab_registry_probe", lambda: None)

    result = batch_preflight.run_batch_preflight(
        repo_root=tmp_path,
        batch_dir=tmp_path / "batch",
        batch_config={
            "defaults": {
                "require_clean_git": False,
                "require_local_green": False,
                "require_ci_green_for_base": False,
            }
        },
        worker_roster=[],
        exec_backend="host",
    )

    assert ["gh", "auth", "status"] in called
    assert ["gh", "api", "user", "--jq", ".login"] in called
    assert ["gh", "repo", "view", "--json", "nameWithOwner"] in called
    assert result["blockers"] == []


def test_batch_preflight_blocks_when_github_capability_probe_fails(
    tmp_path: Path,
    monkeypatch,
) -> None:
    called: list[list[str]] = []

    def fake_run(argv: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
        called.append(argv)
        if argv in (
            ["gh", "auth", "status"],
            ["gh", "api", "user", "--jq", ".login"],
        ):
            return _completed(argv, returncode=1)
        return _completed(argv)

    monkeypatch.setattr(batch_preflight, "_run", fake_run)
    monkeypatch.setattr(batch_preflight, "_git_branch", lambda _: "dev")
    monkeypatch.setattr(batch_preflight, "_git_head", lambda _: "abc123")
    monkeypatch.setattr(batch_preflight, "_gitlab_registry_probe", lambda: None)

    result = batch_preflight.run_batch_preflight(
        repo_root=tmp_path,
        batch_dir=tmp_path / "batch",
        batch_config={
            "defaults": {
                "require_clean_git": False,
                "require_local_green": False,
                "require_ci_green_for_base": False,
            }
        },
        worker_roster=[],
        exec_backend="host",
    )

    assert ["gh", "auth", "status"] in called
    assert ["gh", "api", "user", "--jq", ".login"] in called
    assert [item["blocker_id"] for item in result["blockers"]] == ["batch_control_plane"]


def test_batch_preflight_persists_resolved_maintenance_image_metadata(
    tmp_path: Path,
    monkeypatch,
) -> None:
    events: list[str] = []

    def fake_run(argv: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
        if argv[:3] == ["docker", "buildx", "build"]:
            events.append("scratch_build")
        return _completed(argv)

    class _Resolution:
        env_hash = "a" * 64
        image_ref = "usertest-maintenance:" + ("a" * 16)
        image_source = "local"
        metadata = {
            "timings": {"image_resolution_seconds": 1.25},
            "artifacts": {"pull_log": None, "build_log": None},
        }

    captured: dict[str, object] = {}

    def fake_resolve(**kwargs):
        events.append("resolve")
        captured.update(kwargs)
        kwargs["artifact_path"].write_text(
            '{"schema_version":1,"image":{"source":"local"}}\n',
            encoding="utf-8",
        )
        return _Resolution()

    monkeypatch.setattr(batch_preflight, "_run", fake_run)
    monkeypatch.setattr(batch_preflight, "_git_branch", lambda _: "dev")
    monkeypatch.setattr(batch_preflight, "_git_head", lambda _: "abc123")
    monkeypatch.setattr(batch_preflight, "_gitlab_registry_probe", lambda: None)

    monkeypatch.setattr(
        batch_preflight,
        "_load_maintenance_docker_config",
        lambda **_kwargs: MaintenanceDockerConfig(
            local_image_repo="usertest-maintenance",
            published_image_repo="ghcr.io/jcmullwh/usertest-maintenance",
            pull_policy="if_missing",
            seed_root="/seed",
            cache_root_subdir="cache",
            publish_branches=(),
        ),
    )

    def fake_prepare(**_kwargs):
        events.append("prepare")
        return type(
            "Preparation",
            (),
            {
                "local_ref": "usertest-maintenance:" + ("a" * 16),
                "published_ref": "ghcr.io/jcmullwh/usertest-maintenance:" + ("a" * 16),
            },
        )()

    monkeypatch.setattr(batch_preflight, "prepare_maintenance_docker_image", fake_prepare)
    monkeypatch.setattr(
        batch_preflight,
        "cleanup_local_maintenance_images",
        lambda **_kwargs: events.append("batch_prewrite") or {"schema_version": 1, "errors": []},
    )
    monkeypatch.setattr(batch_preflight, "resolve_maintenance_docker_image", fake_resolve)

    result = batch_preflight.run_batch_preflight(
        repo_root=tmp_path,
        batch_dir=tmp_path / "batch",
        batch_config={
            "defaults": {
                "require_clean_git": False,
                "require_local_green": False,
                "require_ci_green_for_base": False,
            }
        },
        worker_roster=[],
        exec_backend="docker",
        exec_docker_profile="maintenance",
        resolve_maintenance_image=True,
    )

    metadata = result["maintenance_image_metadata"]
    assert result["blockers"] == []
    assert metadata["env_hash"] == "a" * 64
    assert metadata["image_ref"] == "usertest-maintenance:" + ("a" * 16)
    assert metadata["source"] == "local"
    assert Path(metadata["path"]).exists()
    assert captured["run_dir"] == tmp_path / "batch" / "preflight_maintenance_image"
    assert events == ["prepare", "batch_prewrite", "scratch_build", "resolve"]


@pytest.mark.parametrize(
    ("cleanup_enabled", "cleanup_on_prepare", "dry_run_default", "cleanup_expected"),
    [
        (False, True, False, False),
        (True, False, False, False),
        (True, True, True, True),
        (True, True, False, True),
    ],
)
def test_batch_prewrite_honors_automatic_cleanup_policy(
    tmp_path: Path,
    monkeypatch,
    cleanup_enabled: bool,
    cleanup_on_prepare: bool,
    dry_run_default: bool,
    cleanup_expected: bool,
) -> None:
    """Batch prewrite cleanup follows the same automatic-cleanup policy as resolution."""

    captured_cleanup: dict[str, object] = {}
    captured_resolve: dict[str, object] = {}

    config = MaintenanceDockerConfig(
        local_image_repo="usertest-maintenance",
        published_image_repo="ghcr.io/jcmullwh/usertest-maintenance",
        pull_policy="if_missing",
        seed_root="/seed",
        cache_root_subdir="cache",
        publish_branches=(),
        cleanup_enabled=cleanup_enabled,
        cleanup_on_prepare=cleanup_on_prepare,
        cleanup_dry_run_default=dry_run_default,
    )

    monkeypatch.setattr(batch_preflight, "_run", lambda argv, **_kwargs: _completed(argv))
    monkeypatch.setattr(batch_preflight, "_git_branch", lambda _: "dev")
    monkeypatch.setattr(batch_preflight, "_git_head", lambda _: "abc123")
    monkeypatch.setattr(batch_preflight, "_gitlab_registry_probe", lambda: None)
    monkeypatch.setattr(
        batch_preflight,
        "_load_maintenance_docker_config",
        lambda **_kwargs: config,
    )
    monkeypatch.setattr(
        batch_preflight,
        "prepare_maintenance_docker_image",
        lambda **_kwargs: type(
            "Preparation",
            (),
            {
                "local_ref": "usertest-maintenance:" + ("a" * 16),
                "published_ref": "ghcr.io/jcmullwh/usertest-maintenance:" + ("a" * 16),
            },
        )(),
    )

    def fake_cleanup(**kwargs):
        captured_cleanup.update(kwargs)
        return {"schema_version": 1, "errors": []}

    monkeypatch.setattr(batch_preflight, "cleanup_local_maintenance_images", fake_cleanup)

    def fake_resolve(**kwargs):
        captured_resolve.update(kwargs)
        kwargs["artifact_path"].write_text("{}\n", encoding="utf-8")
        return type(
            "Resolution",
            (),
            {
                "env_hash": "a" * 64,
                "image_ref": "usertest-maintenance:" + ("a" * 16),
                "image_source": "local",
                "metadata": {"timings": {}, "artifacts": {}},
            },
        )()

    monkeypatch.setattr(batch_preflight, "resolve_maintenance_docker_image", fake_resolve)

    result = batch_preflight.run_batch_preflight(
        repo_root=tmp_path,
        batch_dir=tmp_path / "batch",
        batch_config={
            "defaults": {
                "require_clean_git": False,
                "require_local_green": False,
                "require_ci_green_for_base": False,
            }
        },
        worker_roster=[],
        exec_backend="docker",
        exec_docker_profile="maintenance",
        resolve_maintenance_image=True,
    )

    assert result["blockers"] == []
    if cleanup_expected:
        assert captured_cleanup["dry_run"] is dry_run_default
        assert captured_resolve["prewrite_cleanup"] == {"schema_version": 1, "errors": []}
    else:
        assert captured_cleanup == {}
        assert captured_resolve["prewrite_cleanup"]["skipped"] is True
