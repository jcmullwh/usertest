from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from runner_core.execution_backend import prepare_execution_backend
from runner_core.runner import RunRequest


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def _make_default_context(repo_root: Path) -> Path:
    context_dir = (
        repo_root
        / "packages"
        / "sandbox_runner"
        / "src"
        / "sandbox_runner"
        / "builtins"
        / "docker"
        / "contexts"
        / "sandbox_cli"
    )
    _write(context_dir / "Dockerfile", "FROM python:3.11-slim\n")
    _write(context_dir / "scripts" / "install_manifests.sh", "#!/bin/sh\n")
    return context_dir


def test_prepare_execution_backend_uses_default_docker_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = tmp_path / "repo"
    run_dir = tmp_path / "run"
    workspace_dir = tmp_path / "workspace"
    run_dir.mkdir(parents=True, exist_ok=True)
    workspace_dir.mkdir(parents=True, exist_ok=True)
    default_context = _make_default_context(repo_root)

    captured: dict[str, object] = {}

    class _DummyInstance:
        command_prefix = ["docker", "exec"]
        workspace_mount = "/workspace"

        def close(self) -> None:
            return

    class _DummyDockerSandbox:
        def __init__(
            self,
            *,
            workspace_dir: Path,
            artifacts_dir: Path,
            spec: object,
            container_name: str,
        ):
            captured["spec"] = spec

        def start(self) -> _DummyInstance:
            return _DummyInstance()

    import runner_core.execution_backend as backend_mod

    monkeypatch.setattr(backend_mod, "DockerSandbox", _DummyDockerSandbox)

    req = RunRequest(
        repo=".",
        agent="codex",
        exec_backend="docker",
        exec_docker_context=None,
        exec_use_host_agent_login=False,
    )

    ctx = prepare_execution_backend(
        repo_root=repo_root,
        run_dir=run_dir,
        workspace_dir=workspace_dir,
        request=req,
        workspace_id="w1",
        agent_cfg={},
    )

    assert ctx.workspace_mount == "/workspace"
    spec = captured["spec"]
    image_context = spec.image_context_path
    assert isinstance(image_context, Path)
    assert image_context.resolve() == default_context.resolve()


def test_prepare_execution_backend_enables_maintenance_venv_cache_env_for_warm_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = tmp_path / "repo"
    run_dir = tmp_path / "run"
    workspace_dir = tmp_path / "workspace"
    run_dir.mkdir(parents=True, exist_ok=True)
    workspace_dir.mkdir(parents=True, exist_ok=True)
    _make_default_context(repo_root)

    captured: dict[str, object] = {}

    class _DummyInstance:
        command_prefix = ["docker", "exec"]
        workspace_mount = "/workspace"

        def close(self) -> None:
            return

    class _DummyDockerSandbox:
        def __init__(
            self,
            *,
            workspace_dir: Path,
            artifacts_dir: Path,
            spec: object,
            container_name: str,
        ):
            captured["spec"] = spec

        def start(self) -> _DummyInstance:
            return _DummyInstance()

    import runner_core.execution_backend as backend_mod

    monkeypatch.setattr(backend_mod, "DockerSandbox", _DummyDockerSandbox)

    req = RunRequest(
        repo=".",
        agent="codex",
        exec_backend="docker",
        exec_docker_context=None,
        exec_use_host_agent_login=False,
        exec_cache="warm",
        exec_maintenance_venv_cache=True,
    )

    prepare_execution_backend(
        repo_root=repo_root,
        run_dir=run_dir,
        workspace_dir=workspace_dir,
        request=req,
        workspace_id="w1",
        agent_cfg={},
    )

    spec = captured["spec"]
    env_overrides = spec.env_overrides
    assert env_overrides["USERTEST_MAINT_VENV_CACHE_ENABLED"] == "1"
    assert env_overrides["USERTEST_MAINT_VENV_CACHE_ROOT"] == "/cache/usertest_maint_venvs"


def test_prepare_execution_backend_disables_maintenance_venv_cache_env_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = tmp_path / "repo"
    run_dir = tmp_path / "run"
    workspace_dir = tmp_path / "workspace"
    run_dir.mkdir(parents=True, exist_ok=True)
    workspace_dir.mkdir(parents=True, exist_ok=True)
    _make_default_context(repo_root)

    captured: dict[str, object] = {}

    class _DummyInstance:
        command_prefix = ["docker", "exec"]
        workspace_mount = "/workspace"

        def close(self) -> None:
            return

    class _DummyDockerSandbox:
        def __init__(
            self,
            *,
            workspace_dir: Path,
            artifacts_dir: Path,
            spec: object,
            container_name: str,
        ):
            captured["spec"] = spec

        def start(self) -> _DummyInstance:
            return _DummyInstance()

    import runner_core.execution_backend as backend_mod

    monkeypatch.setattr(backend_mod, "DockerSandbox", _DummyDockerSandbox)

    req = RunRequest(
        repo=".",
        agent="codex",
        exec_backend="docker",
        exec_docker_context=None,
        exec_use_host_agent_login=False,
        exec_cache="warm",
        exec_maintenance_venv_cache=False,
    )

    prepare_execution_backend(
        repo_root=repo_root,
        run_dir=run_dir,
        workspace_dir=workspace_dir,
        request=req,
        workspace_id="w1",
        agent_cfg={},
    )

    spec = captured["spec"]
    env_overrides = spec.env_overrides
    assert env_overrides["USERTEST_MAINT_VENV_CACHE_ENABLED"] == "0"
    assert "USERTEST_MAINT_VENV_CACHE_ROOT" not in env_overrides


def test_prepare_execution_backend_requires_context_when_default_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = tmp_path / "repo"
    run_dir = tmp_path / "run"
    workspace_dir = tmp_path / "workspace"
    run_dir.mkdir(parents=True, exist_ok=True)
    workspace_dir.mkdir(parents=True, exist_ok=True)

    import runner_core.execution_backend as backend_mod

    def _noop_copy(**_kwargs):  # noqa: ARG001
        return None

    monkeypatch.setattr(backend_mod, "_copy_builtin_sandbox_cli_context_from_resources", _noop_copy)

    req = RunRequest(
        repo=".",
        agent="codex",
        exec_backend="docker",
        exec_docker_context=None,
        exec_use_host_agent_login=False,
    )

    with pytest.raises(ValueError, match="requires exec_docker_context"):
        prepare_execution_backend(
            repo_root=repo_root,
            run_dir=run_dir,
            workspace_dir=workspace_dir,
            request=req,
            workspace_id="w1",
            agent_cfg={},
        )


def test_prepare_execution_backend_uses_resource_context_when_repo_default_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = tmp_path / "repo"
    run_dir = tmp_path / "run"
    workspace_dir = tmp_path / "workspace"
    run_dir.mkdir(parents=True, exist_ok=True)
    workspace_dir.mkdir(parents=True, exist_ok=True)

    import runner_core.execution_backend as backend_mod

    captured: dict[str, object] = {}

    class _DummyInstance:
        command_prefix = ["docker", "exec"]
        workspace_mount = "/workspace"

        def close(self) -> None:
            return

    class _DummyDockerSandbox:
        def __init__(
            self,
            *,
            workspace_dir: Path,
            artifacts_dir: Path,
            spec: object,
            container_name: str,
        ):
            captured["spec"] = spec

        def start(self) -> _DummyInstance:
            return _DummyInstance()

    monkeypatch.setattr(backend_mod, "DockerSandbox", _DummyDockerSandbox)

    req = RunRequest(
        repo=".",
        agent="codex",
        exec_backend="docker",
        exec_docker_context=None,
        exec_use_host_agent_login=False,
    )

    prepare_execution_backend(
        repo_root=repo_root,
        run_dir=run_dir,
        workspace_dir=workspace_dir,
        request=req,
        workspace_id="w1",
        agent_cfg={},
    )

    spec = captured["spec"]
    image_context = spec.image_context_path
    assert isinstance(image_context, Path)
    expected = run_dir / "sandbox" / "builtin_context"
    assert image_context.resolve() == expected.resolve()
    assert (expected / "Dockerfile").exists()
    assert (expected / "scripts" / "install_manifests.sh").exists()


def test_prepare_execution_backend_mounts_host_claude_json_when_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = tmp_path / "repo"
    run_dir = tmp_path / "run"
    workspace_dir = tmp_path / "workspace"
    run_dir.mkdir(parents=True, exist_ok=True)
    workspace_dir.mkdir(parents=True, exist_ok=True)
    _make_default_context(repo_root)

    fake_home = tmp_path / "home"
    (fake_home / ".claude").mkdir(parents=True, exist_ok=True)
    (fake_home / ".claude.json").write_text("{}", encoding="utf-8")

    import runner_core.execution_backend as backend_mod

    monkeypatch.setattr(backend_mod.Path, "home", lambda: fake_home)

    captured: dict[str, object] = {}

    class _DummyInstance:
        command_prefix = ["docker", "exec"]
        workspace_mount = "/workspace"

        def close(self) -> None:
            return

    class _DummyDockerSandbox:
        def __init__(
            self,
            *,
            workspace_dir: Path,
            artifacts_dir: Path,
            spec: object,
            container_name: str,
        ):
            captured["spec"] = spec

        def start(self) -> _DummyInstance:
            return _DummyInstance()

    monkeypatch.setattr(backend_mod, "DockerSandbox", _DummyDockerSandbox)

    req = RunRequest(
        repo=".",
        agent="claude",
        exec_backend="docker",
        exec_docker_context=None,
        exec_use_host_agent_login=True,
    )

    prepare_execution_backend(
        repo_root=repo_root,
        run_dir=run_dir,
        workspace_dir=workspace_dir,
        request=req,
        workspace_id="w1",
        agent_cfg={},
    )

    spec = captured["spec"]
    mounts = spec.extra_mounts
    assert any(m.container_path == "/root/.claude" for m in mounts)
    assert any(m.container_path == "/root/.claude.json" for m in mounts)


def test_prepare_execution_backend_fails_before_docker_start_when_host_login_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = tmp_path / "repo"
    run_dir = tmp_path / "run"
    workspace_dir = tmp_path / "workspace"
    run_dir.mkdir(parents=True, exist_ok=True)
    workspace_dir.mkdir(parents=True, exist_ok=True)
    _make_default_context(repo_root)

    fake_home = tmp_path / "home"
    fake_home.mkdir(parents=True, exist_ok=True)

    import runner_core.execution_backend as backend_mod

    monkeypatch.setattr(backend_mod.Path, "home", lambda: fake_home)

    class _FailDockerSandbox:
        def __init__(self, *args: object, **kwargs: object) -> None:  # noqa: ARG002
            raise AssertionError(
                "DockerSandbox should not be constructed when host login mount preflight fails"
            )

    monkeypatch.setattr(backend_mod, "DockerSandbox", _FailDockerSandbox)

    req = RunRequest(
        repo=".",
        agent="codex",
        exec_backend="docker",
        exec_docker_context=None,
        exec_use_host_agent_login=True,
    )

    with pytest.raises(FileNotFoundError, match="Host agent login directory not found"):
        prepare_execution_backend(
            repo_root=repo_root,
            run_dir=run_dir,
            workspace_dir=workspace_dir,
            request=req,
            workspace_id="w1",
            agent_cfg={},
        )


def test_prepare_execution_backend_maintenance_profile_uses_image_ref_and_updates_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = tmp_path / "repo"
    run_dir = tmp_path / "run"
    workspace_dir = tmp_path / "workspace"
    run_dir.mkdir(parents=True, exist_ok=True)
    workspace_dir.mkdir(parents=True, exist_ok=True)

    captured: dict[str, object] = {}

    class _DummyInstance:
        command_prefix = ["docker", "exec"]
        workspace_mount = "/workspace"
        container_name = "sandbox-maintenance"
        image_tag = "usertest-maintenance:abc123"

        def close(self) -> None:
            return

    class _DummyDockerSandbox:
        def __init__(
            self,
            *,
            workspace_dir: Path,
            artifacts_dir: Path,
            spec: object,
            container_name: str,
        ):
            del workspace_dir, container_name
            captured["spec"] = spec
            captured["artifacts_dir"] = artifacts_dir

        def start(self) -> _DummyInstance:
            artifacts_dir = captured["artifacts_dir"]
            assert isinstance(artifacts_dir, Path)
            artifacts_dir.mkdir(parents=True, exist_ok=True)
            (artifacts_dir / "sandbox.json").write_text(
                json.dumps({"backend": "docker", "image_ref": None}) + "\n",
                encoding="utf-8",
            )
            return _DummyInstance()

    import runner_core.execution_backend as backend_mod

    monkeypatch.setattr(backend_mod, "DockerSandbox", _DummyDockerSandbox)
    monkeypatch.setattr(
        backend_mod,
        "_prepare_maintenance_profile",
        lambda **_kwargs: backend_mod.MaintenanceProfilePreparation(
            image_ref="usertest-maintenance:abc123",
            env_hash="deadbeef" * 8,
            image_source="local",
            image_resolution_seconds=1.5,
            fingerprint_seconds=0.5,
            cache_mount_hits=1,
            cache_mounts=[
                backend_mod.MountSpec(
                    host_path=tmp_path / "cache" / "demo",
                    container_path="/workspace/packages/demo/.venv",
                    read_only=False,
                )
            ],
            env_overrides={
                "USERTEST_MAINT_VENV_CACHE_ENABLED": "1",
                "USERTEST_MAINT_VENV_CACHE_ROOT": "/cache/usertest_maint_venvs",
                "USERTEST_MAINT_VENV_SEED_ROOT": "/opt/usertest_maint_seed",
            },
            metadata={
                "schema_version": 1,
                "profile": "maintenance",
                "timings": {
                    "fingerprint_seconds": 0.5,
                    "image_resolution_seconds": 1.5,
                    "container_start_seconds": None,
                    "cache_mount_hits": 1,
                    "seed_hits": None,
                    "install_projects_run": None,
                },
            },
        ),
    )

    req = RunRequest(
        repo=".",
        agent="codex",
        exec_backend="docker",
        exec_docker_profile="maintenance",
        exec_use_host_agent_login=False,
    )

    prepare_execution_backend(
        repo_root=repo_root,
        run_dir=run_dir,
        workspace_dir=workspace_dir,
        request=req,
        workspace_id="w1",
        agent_cfg={},
    )

    spec = captured["spec"]
    assert spec.image_ref == "usertest-maintenance:abc123"
    assert spec.image_context_path is None
    assert any(
        mount.container_path == "/workspace/packages/demo/.venv" for mount in spec.extra_mounts
    )

    maintenance_meta = json.loads(
        (run_dir / "sandbox" / "maintenance_profile.json").read_text(encoding="utf-8")
    )
    assert maintenance_meta["profile"] == "maintenance"
    assert maintenance_meta["image_ref"] == "usertest-maintenance:abc123"
    assert maintenance_meta["timings"]["cache_mount_hits"] == 1

    sandbox_meta = json.loads((run_dir / "sandbox" / "sandbox.json").read_text(encoding="utf-8"))
    assert sandbox_meta["docker_profile"] == "maintenance"
    assert sandbox_meta["maintenance_image_source"] == "local"
    assert sandbox_meta["maintenance_cache_mount_count"] == 1


def test_prepare_execution_backend_maintenance_profile_rejects_custom_context(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo"
    run_dir = tmp_path / "run"
    workspace_dir = tmp_path / "workspace"
    custom_context = tmp_path / "context"
    run_dir.mkdir(parents=True, exist_ok=True)
    workspace_dir.mkdir(parents=True, exist_ok=True)
    custom_context.mkdir(parents=True, exist_ok=True)

    req = RunRequest(
        repo=".",
        agent="codex",
        exec_backend="docker",
        exec_docker_profile="maintenance",
        exec_docker_context=custom_context,
        exec_use_host_agent_login=False,
    )

    with pytest.raises(ValueError, match="does not support exec_docker_context"):
        prepare_execution_backend(
            repo_root=repo_root,
            run_dir=run_dir,
            workspace_dir=workspace_dir,
            request=req,
            workspace_id="w1",
            agent_cfg={},
        )


def test_prepare_maintenance_profile_prefers_local_image_and_plans_cache_mounts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = tmp_path / "repo"
    run_dir = tmp_path / "run"
    workspace_dir = tmp_path / "workspace"
    cache_dir = tmp_path / "cache"
    run_dir.mkdir(parents=True, exist_ok=True)
    workspace_dir.mkdir(parents=True, exist_ok=True)

    import runner_core.execution_backend as backend_mod

    context_dir = run_dir / "sandbox" / "maintenance_image_context"
    context_dir.mkdir(parents=True, exist_ok=True)
    (context_dir / "Dockerfile").write_text("FROM python:3.11-slim\n", encoding="utf-8")

    monkeypatch.setattr(
        backend_mod,
        "_load_maintenance_docker_config",
        lambda repo_root: backend_mod.MaintenanceDockerConfig(
            local_image_repo="usertest-maintenance",
            published_image_repo="ghcr.io/jcmullwh/usertest-maintenance",
            pull_policy="if_missing",
            seed_root="/opt/usertest_maint_seed",
            cache_root_subdir="usertest_maint_venvs",
            publish_branches=("dev", "main"),
        ),
    )
    monkeypatch.setattr(
        backend_mod,
        "_prepare_maintenance_image_context",
        lambda **_kwargs: (
            context_dir,
            {"python_major_minor": "3.11", "pdm_version": "2.26.2"},
        ),
    )
    monkeypatch.setattr(backend_mod, "compute_image_hash", lambda **_kwargs: "a" * 64)
    monkeypatch.setattr(backend_mod, "_docker_image_exists_local", lambda **_kwargs: True)
    monkeypatch.setattr(
        backend_mod,
        "_docker_pull_image",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("docker pull should not run")),
    )
    monkeypatch.setattr(
        backend_mod,
        "_build_maintenance_image",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("docker build should not run")),
    )
    monkeypatch.setattr(
        backend_mod,
        "_compute_install_cache_fingerprints",
        lambda **_kwargs: {
            "projects": [
                {"id": "demo", "path": "packages/demo", "fingerprint": "f" * 64},
            ]
        },
    )

    mounted_venv = cache_dir / "usertest_maint_venvs" / "demo" / ("f" * 64) / "venv"
    mounted_venv.mkdir(parents=True, exist_ok=True)

    request = RunRequest(
        repo=".",
        agent="codex",
        exec_backend="docker",
        exec_docker_profile="maintenance",
        verification_commands=("smoke", "install", "lint", "test"),
    )

    prep = backend_mod._prepare_maintenance_profile(
        repo_root=repo_root,
        run_dir=run_dir,
        workspace_dir=workspace_dir,
        request=request,
        cache_mode="warm",
        cache_dir=cache_dir,
        maintenance_venv_reuse_enabled=True,
        timeout_seconds=30.0,
    )

    assert prep.image_ref == "usertest-maintenance:" + ("a" * 16)
    assert prep.image_source == "local"
    assert prep.cache_mount_hits == 1
    assert prep.cache_mounts[0].container_path == "/workspace/packages/demo/.venv"
    assert prep.env_overrides["USERTEST_MAINT_VENV_CACHE_ENABLED"] == "1"
    assert prep.env_overrides["USERTEST_MAINT_VENV_CACHE_ROOT"] == "/cache/usertest_maint_venvs"
    assert prep.env_overrides["USERTEST_MAINT_VENV_SEED_ROOT"] == "/opt/usertest_maint_seed"


def test_prepare_maintenance_profile_uses_branch_alias_as_build_cache_when_hash_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = tmp_path / "repo"
    run_dir = tmp_path / "run"
    workspace_dir = tmp_path / "workspace"
    cache_dir = tmp_path / "cache"
    run_dir.mkdir(parents=True, exist_ok=True)
    workspace_dir.mkdir(parents=True, exist_ok=True)

    import runner_core.execution_backend as backend_mod

    context_dir = run_dir / "sandbox" / "maintenance_image_context"
    context_dir.mkdir(parents=True, exist_ok=True)
    (context_dir / "Dockerfile").write_text("FROM python:3.11-slim\n", encoding="utf-8")

    monkeypatch.setattr(
        backend_mod,
        "_load_maintenance_docker_config",
        lambda repo_root: backend_mod.MaintenanceDockerConfig(
            local_image_repo="usertest-maintenance",
            published_image_repo="ghcr.io/jcmullwh/usertest-maintenance",
            pull_policy="if_missing",
            seed_root="/opt/usertest_maint_seed",
            cache_root_subdir="usertest_maint_venvs",
            publish_branches=("dev", "main"),
        ),
    )
    monkeypatch.setattr(
        backend_mod,
        "_prepare_maintenance_image_context",
        lambda **_kwargs: (
            context_dir,
            {"python_major_minor": "3.11", "pdm_version": "2.26.2"},
        ),
    )
    monkeypatch.setattr(backend_mod, "compute_image_hash", lambda **_kwargs: "d" * 64)
    monkeypatch.setattr(backend_mod, "_docker_image_exists_local", lambda **_kwargs: False)
    monkeypatch.setattr(backend_mod, "_git_current_branch", lambda **_kwargs: "dev")
    monkeypatch.setattr(
        backend_mod,
        "_docker_pull_image",
        lambda **_kwargs: subprocess.CompletedProcess(
            args=["docker", "pull"], returncode=1, stdout="", stderr="not found"
        ),
    )
    monkeypatch.setattr(
        backend_mod,
        "_docker_pull_images",
        lambda **kwargs: [
            {
                "argv": ["docker", "pull", kwargs["refs"][0]],
                "ref": kwargs["refs"][0],
                "returncode": 0,
                "stdout": "",
                "stderr": "",
            }
        ],
    )
    monkeypatch.setattr(
        backend_mod,
        "_compute_install_cache_fingerprints",
        lambda **_kwargs: {"projects": []},
    )
    captured_build: dict[str, object] = {}

    def _fake_build(**kwargs):
        captured_build.update(kwargs)

    monkeypatch.setattr(backend_mod, "_build_maintenance_image", _fake_build)

    request = RunRequest(
        repo=".",
        agent="codex",
        exec_backend="docker",
        exec_docker_profile="maintenance",
        verification_commands=("smoke", "install", "lint", "test"),
    )

    prep = backend_mod._prepare_maintenance_profile(
        repo_root=repo_root,
        run_dir=run_dir,
        workspace_dir=workspace_dir,
        request=request,
        cache_mode="warm",
        cache_dir=cache_dir,
        maintenance_venv_reuse_enabled=True,
        timeout_seconds=30.0,
    )

    expected_cache_ref = "ghcr.io/jcmullwh/usertest-maintenance:dev-latest"
    assert captured_build["cache_from"] == [expected_cache_ref]
    assert prep.image_source == "built"
    assert prep.metadata["image"]["build_cache_from"] == [expected_cache_ref]
    assert prep.metadata["image"]["alias_pull_attempts"][0]["ref"] == expected_cache_ref


def test_resolve_maintenance_docker_image_records_pull_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = tmp_path / "repo"
    run_dir = tmp_path / "run"
    repo_root.mkdir(parents=True, exist_ok=True)
    run_dir.mkdir(parents=True, exist_ok=True)

    import runner_core.execution_backend as backend_mod

    context_dir = run_dir / "sandbox" / "maintenance_image_context"
    context_dir.mkdir(parents=True, exist_ok=True)
    (context_dir / "Dockerfile").write_text("FROM python:3.11-slim\n", encoding="utf-8")

    monkeypatch.setattr(
        backend_mod,
        "_load_maintenance_docker_config",
        lambda repo_root: backend_mod.MaintenanceDockerConfig(
            local_image_repo="usertest-maintenance",
            published_image_repo="ghcr.io/example/usertest-maintenance",
            pull_policy="if_missing",
            seed_root="/opt/usertest_maint_seed",
            cache_root_subdir="usertest_maint_venvs",
            publish_branches=("dev", "main"),
            cleanup_enabled=False,
        ),
    )
    monkeypatch.setattr(
        backend_mod,
        "_prepare_maintenance_image_context",
        lambda **_kwargs: (
            context_dir,
            {"python_major_minor": "3.11", "pdm_version": "2.26.2"},
        ),
    )
    monkeypatch.setattr(backend_mod, "compute_image_hash", lambda **_kwargs: "e" * 64)
    monkeypatch.setattr(backend_mod, "_docker_image_exists_local", lambda **_kwargs: False)

    def _fake_pull(**kwargs):
        kwargs["log_path"].write_text(
            json.dumps({"returncode": 0, "stdout": "pulled", "stderr": ""}) + "\n",
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(
            args=["docker", "pull", kwargs["ref"]],
            returncode=0,
            stdout="pulled",
            stderr="",
        )

    tagged: dict[str, str] = {}
    monkeypatch.setattr(backend_mod, "_docker_pull_image", _fake_pull)
    monkeypatch.setattr(
        backend_mod,
        "_docker_tag_image",
        lambda **kwargs: tagged.update(
            {"source": kwargs["source_ref"], "target": kwargs["target_ref"]}
        ),
    )
    monkeypatch.setattr(
        backend_mod,
        "_build_maintenance_image",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("docker build should not run")),
    )

    artifact_path = run_dir / "preflight" / "maintenance_image.json"
    resolution = backend_mod.resolve_maintenance_docker_image(
        repo_root=repo_root,
        run_dir=run_dir,
        timeout_seconds=30.0,
        artifact_path=artifact_path,
    )

    assert resolution.image_source == "pulled"
    assert resolution.image_ref == "usertest-maintenance:" + ("e" * 16)
    assert tagged == {
        "source": "ghcr.io/example/usertest-maintenance:" + ("e" * 16),
        "target": "usertest-maintenance:" + ("e" * 16),
    }
    persisted = json.loads(artifact_path.read_text(encoding="utf-8"))
    assert persisted["image"]["source"] == "pulled"
    assert persisted["image"]["pull_attempted"] is True
    assert persisted["artifacts"]["pull_log"].endswith("maintenance_docker_pull.json")
    assert persisted["timings"]["image_resolution_seconds"] >= 0


def test_prepare_maintenance_profile_consumes_pre_resolved_image_without_resolution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = tmp_path / "repo"
    run_dir = tmp_path / "run"
    workspace_dir = tmp_path / "workspace"
    cache_dir = tmp_path / "cache"
    repo_root.mkdir(parents=True, exist_ok=True)
    run_dir.mkdir(parents=True, exist_ok=True)
    workspace_dir.mkdir(parents=True, exist_ok=True)

    import runner_core.execution_backend as backend_mod

    metadata_path = tmp_path / "batch" / "preflight" / "maintenance_image.json"
    metadata_path.parent.mkdir(parents=True, exist_ok=True)
    pre_resolved = {
        "schema_version": 1,
        "kind": "maintenance_image_resolution",
        "profile": "maintenance",
        "image": {
            "env_hash": "f" * 64,
            "image_ref": "usertest-maintenance:" + ("f" * 16),
            "local_ref": "usertest-maintenance:" + ("f" * 16),
            "published_ref": "ghcr.io/example/usertest-maintenance:" + ("f" * 16),
            "source": "built",
            "pull_attempted": True,
            "alias_pull_attempts": [{"ref": "ghcr.io/example/usertest-maintenance:dev-latest"}],
            "build_cache_from": ["ghcr.io/example/usertest-maintenance:dev-latest"],
            "build_performed": True,
            "context_dir": str(tmp_path / "context"),
            "context_metadata": {"python_major_minor": "3.11", "pdm_version": "2.26.2"},
        },
        "artifacts": {
            "pull_log": str(tmp_path / "pull.json"),
            "alias_pull_log": str(tmp_path / "cache-pulls.json"),
            "build_log": str(tmp_path / "build.log"),
        },
        "timings": {"image_resolution_seconds": 12.5},
    }
    metadata_path.write_text(json.dumps(pre_resolved) + "\n", encoding="utf-8")

    monkeypatch.setattr(
        backend_mod,
        "_load_maintenance_docker_config",
        lambda repo_root: backend_mod.MaintenanceDockerConfig(
            local_image_repo="usertest-maintenance",
            published_image_repo="ghcr.io/example/usertest-maintenance",
            pull_policy="if_missing",
            seed_root="/opt/usertest_maint_seed",
            cache_root_subdir="usertest_maint_venvs",
            publish_branches=("dev", "main"),
            cleanup_enabled=False,
        ),
    )
    monkeypatch.setattr(
        backend_mod,
        "_prepare_maintenance_image_context",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("context preparation should not run")
        ),
    )
    monkeypatch.setattr(
        backend_mod,
        "_docker_image_exists_local",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("local image resolution should not run")
        ),
    )
    monkeypatch.setattr(
        backend_mod,
        "_docker_pull_image",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("docker pull should not run")),
    )
    monkeypatch.setattr(
        backend_mod,
        "_build_maintenance_image",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("docker build should not run")),
    )
    monkeypatch.setattr(
        backend_mod,
        "_compute_install_cache_fingerprints",
        lambda **_kwargs: {"projects": []},
    )

    request = RunRequest(
        repo=".",
        agent="codex",
        exec_backend="docker",
        exec_docker_profile="maintenance",
        exec_maintenance_image_metadata_path=metadata_path,
    )

    prep = backend_mod._prepare_maintenance_profile(
        repo_root=repo_root,
        run_dir=run_dir,
        workspace_dir=workspace_dir,
        request=request,
        cache_mode="warm",
        cache_dir=cache_dir,
        maintenance_venv_reuse_enabled=True,
        timeout_seconds=30.0,
    )

    assert prep.image_ref == "usertest-maintenance:" + ("f" * 16)
    assert prep.image_source == "built"
    assert prep.image_resolution_seconds == 0.0
    assert prep.metadata["image"]["pre_resolved"] is True
    assert prep.metadata["image"]["pre_resolved_image_ref"] == prep.image_ref
    assert prep.metadata["image_resolution"]["metadata_path"] == str(metadata_path.resolve())
    assert prep.metadata["image_resolution"]["provenance"]["image"]["source"] == "built"


def test_prepare_maintenance_profile_runs_cleanup_and_records_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Maintenance profile preparation should persist cleanup output when enabled."""

    repo_root = tmp_path / "repo"
    run_dir = tmp_path / "run"
    workspace_dir = tmp_path / "workspace"
    cache_dir = tmp_path / "cache"
    run_dir.mkdir(parents=True, exist_ok=True)
    workspace_dir.mkdir(parents=True, exist_ok=True)

    import runner_core.execution_backend as backend_mod

    context_dir = run_dir / "sandbox" / "maintenance_image_context"
    context_dir.mkdir(parents=True, exist_ok=True)
    (context_dir / "Dockerfile").write_text("FROM python:3.11-slim\n", encoding="utf-8")

    monkeypatch.setattr(
        backend_mod,
        "_load_maintenance_docker_config",
        lambda repo_root: backend_mod.MaintenanceDockerConfig(
            local_image_repo="usertest-maintenance",
            published_image_repo="ghcr.io/jcmullwh/usertest-maintenance",
            pull_policy="if_missing",
            seed_root="/opt/usertest_maint_seed",
            cache_root_subdir="usertest_maint_venvs",
            publish_branches=("dev", "main"),
            cleanup_enabled=True,
            cleanup_on_prepare=True,
        ),
    )
    monkeypatch.setattr(
        backend_mod,
        "_prepare_maintenance_image_context",
        lambda **_kwargs: (
            context_dir,
            {"python_major_minor": "3.11", "pdm_version": "2.26.2"},
        ),
    )
    monkeypatch.setattr(backend_mod, "compute_image_hash", lambda **_kwargs: "b" * 64)
    monkeypatch.setattr(backend_mod, "_docker_image_exists_local", lambda **_kwargs: True)
    monkeypatch.setattr(
        backend_mod,
        "_compute_install_cache_fingerprints",
        lambda **_kwargs: {"projects": []},
    )
    def _fake_cleanup(**kwargs):
        summary = {
            "schema_version": 1,
            "cleanup_enabled": True,
            "dry_run": False,
            "repos_scanned": [
                "usertest-maintenance",
                "ghcr.io/jcmullwh/usertest-maintenance",
            ],
            "protected_tags": ["latest"],
            "kept_tags": ["usertest-maintenance:latest"],
            "deleted_tags": ["usertest-maintenance:aaaaaaaaaaaaaaaa"],
            "deleted_image_ids": ["sha256:a"],
            "errors": [],
        }
        kwargs["artifact_path"].write_text(
            json.dumps(summary, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        return summary

    monkeypatch.setattr(backend_mod, "cleanup_local_maintenance_images", _fake_cleanup)

    request = RunRequest(
        repo=".",
        agent="codex",
        exec_backend="docker",
        exec_docker_profile="maintenance",
        verification_commands=("smoke", "install", "lint", "test"),
    )

    prep = backend_mod._prepare_maintenance_profile(
        repo_root=repo_root,
        run_dir=run_dir,
        workspace_dir=workspace_dir,
        request=request,
        cache_mode="warm",
        cache_dir=cache_dir,
        maintenance_venv_reuse_enabled=True,
        timeout_seconds=30.0,
    )

    cleanup_artifact = run_dir / "sandbox" / "maintenance_image_cleanup.json"
    assert cleanup_artifact.exists()
    cleanup_meta = json.loads(cleanup_artifact.read_text(encoding="utf-8"))
    assert cleanup_meta["deleted_tags"] == ["usertest-maintenance:aaaaaaaaaaaaaaaa"]
    assert prep.metadata["cleanup"]["deleted_image_ids"] == ["sha256:a"]


def test_prepare_maintenance_profile_cleanup_failure_is_best_effort(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cleanup failure should be recorded without aborting maintenance preparation."""

    repo_root = tmp_path / "repo"
    run_dir = tmp_path / "run"
    workspace_dir = tmp_path / "workspace"
    cache_dir = tmp_path / "cache"
    run_dir.mkdir(parents=True, exist_ok=True)
    workspace_dir.mkdir(parents=True, exist_ok=True)

    import runner_core.execution_backend as backend_mod

    context_dir = run_dir / "sandbox" / "maintenance_image_context"
    context_dir.mkdir(parents=True, exist_ok=True)
    (context_dir / "Dockerfile").write_text("FROM python:3.11-slim\n", encoding="utf-8")

    monkeypatch.setattr(
        backend_mod,
        "_load_maintenance_docker_config",
        lambda repo_root: backend_mod.MaintenanceDockerConfig(
            local_image_repo="usertest-maintenance",
            published_image_repo="ghcr.io/jcmullwh/usertest-maintenance",
            pull_policy="if_missing",
            seed_root="/opt/usertest_maint_seed",
            cache_root_subdir="usertest_maint_venvs",
            publish_branches=("dev", "main"),
            cleanup_enabled=True,
            cleanup_on_prepare=True,
        ),
    )
    monkeypatch.setattr(
        backend_mod,
        "_prepare_maintenance_image_context",
        lambda **_kwargs: (
            context_dir,
            {"python_major_minor": "3.11", "pdm_version": "2.26.2"},
        ),
    )
    monkeypatch.setattr(backend_mod, "compute_image_hash", lambda **_kwargs: "c" * 64)
    monkeypatch.setattr(backend_mod, "_docker_image_exists_local", lambda **_kwargs: True)
    monkeypatch.setattr(
        backend_mod,
        "_compute_install_cache_fingerprints",
        lambda **_kwargs: {"projects": []},
    )

    def _raise_cleanup(**_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(backend_mod, "cleanup_local_maintenance_images", _raise_cleanup)

    request = RunRequest(
        repo=".",
        agent="codex",
        exec_backend="docker",
        exec_docker_profile="maintenance",
        verification_commands=("smoke", "install", "lint", "test"),
    )

    prep = backend_mod._prepare_maintenance_profile(
        repo_root=repo_root,
        run_dir=run_dir,
        workspace_dir=workspace_dir,
        request=request,
        cache_mode="warm",
        cache_dir=cache_dir,
        maintenance_venv_reuse_enabled=True,
        timeout_seconds=30.0,
    )

    cleanup_artifact = run_dir / "sandbox" / "maintenance_image_cleanup.json"
    assert cleanup_artifact.exists()
    cleanup_meta = json.loads(cleanup_artifact.read_text(encoding="utf-8"))
    assert cleanup_meta["errors"] == ["Automatic maintenance image cleanup failed: boom"]
    assert prep.metadata["cleanup"]["errors"] == [
        "Automatic maintenance image cleanup failed: boom"
    ]
