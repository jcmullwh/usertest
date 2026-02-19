from __future__ import annotations

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


def test_prepare_execution_backend_requires_context_when_default_missing(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    run_dir = tmp_path / "run"
    workspace_dir = tmp_path / "workspace"
    run_dir.mkdir(parents=True, exist_ok=True)
    workspace_dir.mkdir(parents=True, exist_ok=True)

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
