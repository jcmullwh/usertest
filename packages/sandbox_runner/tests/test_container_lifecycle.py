from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import sandbox_runner.docker as docker
from sandbox_runner.spec import SandboxSpec


def _start_sandbox(
    *,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    keep_container: bool,
) -> tuple[docker.DockerSandboxInstance, list[list[str]]]:
    calls: list[list[str]] = []

    def _fake_docker_run(
        argv: list[str],
        *,
        cwd: Path | None = None,  # noqa: ARG001
        check: bool = True,  # noqa: ARG001
        timeout_seconds: float | None = None,  # noqa: ARG001
    ) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        if argv[:2] == ["docker", "version"]:
            return subprocess.CompletedProcess(argv, 0, stdout="ok\n", stderr="")
        if argv[:3] == ["docker", "image", "inspect"]:
            return subprocess.CompletedProcess(argv, 0, stdout="[]\n", stderr="")
        if argv[:2] in (["docker", "run"], ["docker", "rm"]):
            return subprocess.CompletedProcess(argv, 0, stdout="container-id\n", stderr="")
        raise AssertionError(f"unexpected docker invocation: {argv!r}")

    monkeypatch.setattr(docker, "_docker_run", _fake_docker_run)

    context_dir = tmp_path / "context"
    context_dir.mkdir()
    (context_dir / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")

    instance = docker.DockerSandbox(
        workspace_dir=tmp_path / "workspace",
        artifacts_dir=tmp_path / "artifacts",
        spec=SandboxSpec(
            backend="docker",
            image_context_path=context_dir,
            keep_container=keep_container,
        ),
        container_name="sandbox-lifecycle-test",
    ).start()
    return instance, calls


def test_non_retained_container_uses_daemon_auto_remove(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance, calls = _start_sandbox(
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        keep_container=False,
    )

    run_call = next(argv for argv in calls if argv[:2] == ["docker", "run"])
    assert "--rm" in run_call

    instance.close()
    assert any(argv[:3] == ["docker", "rm", "-f"] for argv in calls)


def test_retained_container_omits_auto_remove_and_close_preserves_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance, calls = _start_sandbox(
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        keep_container=True,
    )

    run_call = next(argv for argv in calls if argv[:2] == ["docker", "run"])
    assert "--rm" not in run_call

    instance.close()
    assert not any(argv[:2] == ["docker", "rm"] for argv in calls)
