from __future__ import annotations

from pathlib import Path

import pytest

import sandbox_runner.docker as docker
from sandbox_runner.spec import SandboxSpec


def test_docker_sandbox_fails_before_build_when_docker_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    build_called = False

    def _fake_ensure(*, timeout_seconds: float | None = None) -> None:  # noqa: ARG001
        raise RuntimeError("SENTINEL docker unavailable")

    def _fake_build(*_args, **_kwargs) -> int:
        nonlocal build_called
        build_called = True
        raise AssertionError("docker build should not be attempted when preflight fails")

    monkeypatch.setattr(docker, "_ensure_docker_available", _fake_ensure)
    monkeypatch.setattr(docker, "_docker_build_streaming", _fake_build)

    spec = SandboxSpec(
        backend="docker",
        image_context_path=tmp_path / "context",
    )

    sandbox = docker.DockerSandbox(
        workspace_dir=tmp_path / "workspace",
        artifacts_dir=tmp_path / "artifacts",
        spec=spec,
        container_name="sandbox-preflight-test",
    )

    with pytest.raises(RuntimeError, match="SENTINEL"):
        sandbox.start()

    assert build_called is False
