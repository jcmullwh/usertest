from __future__ import annotations

import subprocess
from pathlib import Path

import yaml

from usertest_implement import batch_preflight


def _completed(argv: list[str], *, returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(argv, returncode, stdout="", stderr="")


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
