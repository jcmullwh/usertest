from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import sandbox_runner.docker as docker
from sandbox_runner.spec import SandboxSpec


def _env_present(argv: list[str], *, key: str, value: str) -> bool:
    needle = f"{key}={value}"
    for idx in range(len(argv) - 1):
        if argv[idx] == "-e" and argv[idx + 1] == needle:
            return True
    return False


def test_env_args_with_overrides_prefers_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FOO", "from-env")
    monkeypatch.setenv("BAR", "from-env-bar")

    args = docker._env_args_with_overrides(  # type: ignore[attr-defined]
        ["FOO", "BAR"],
        {"BAR": "override", "BAZ": "x"},
    )
    assert args == ["-e", "FOO=from-env", "-e", "BAR=override", "-e", "BAZ=x"]


def test_docker_sandbox_passes_env_overrides_into_container(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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
        if argv[:2] == ["docker", "run"]:
            return subprocess.CompletedProcess(argv, 0, stdout="container-id\n", stderr="")
        if argv[:2] == ["docker", "rm"]:
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        raise AssertionError(f"unexpected docker invocation: {argv!r}")

    monkeypatch.setattr(docker, "_docker_run", _fake_docker_run)
    monkeypatch.setenv("ALLOW", "from-env")

    context_dir = tmp_path / "context"
    context_dir.mkdir(parents=True, exist_ok=True)
    (context_dir / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")

    artifacts_dir = tmp_path / "artifacts"
    spec = SandboxSpec(
        backend="docker",
        image_context_path=context_dir,
        env_allowlist=["ALLOW"],
        env_overrides={
            "ALLOW": "override",
            "TMPDIR": "/tmp",
            "TMP": "/tmp",
            "TEMP": "/tmp",
            "PIP_CACHE_DIR": "/tmp/pip-cache",
        },
    )

    sandbox = docker.DockerSandbox(
        workspace_dir=tmp_path / "workspace",
        artifacts_dir=artifacts_dir,
        spec=spec,
        container_name="sandbox-env-overrides-test",
    ).start()
    try:
        run_call = next(argv for argv in calls if argv[:2] == ["docker", "run"])
        assert _env_present(run_call, key="ALLOW", value="override")
        assert _env_present(run_call, key="TMPDIR", value="/tmp")
        assert _env_present(run_call, key="PIP_CACHE_DIR", value="/tmp/pip-cache")
        assert not _env_present(run_call, key="ALLOW", value="from-env")

        meta = json.loads((artifacts_dir / "sandbox.json").read_text(encoding="utf-8"))
        assert meta["env_overrides_safe"]["TMPDIR"] == "/tmp"
        assert meta["env_overrides_safe"]["PIP_CACHE_DIR"] == "/tmp/pip-cache"
        assert "ALLOW" in meta["env_overrides_keys"]
    finally:
        sandbox.close()

