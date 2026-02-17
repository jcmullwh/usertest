from __future__ import annotations

import json
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest

from sandbox_runner import DockerSandbox, SandboxSpec


def _docker_available() -> tuple[bool, str]:
    if shutil.which("docker") is None:
        return False, "docker not on PATH"

    try:
        proc = subprocess.run(
            ["docker", "version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=10,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return False, "docker version timed out"
    if proc.returncode == 0:
        return True, ""
    return False, proc.stderr.strip() or proc.stdout.strip() or "docker version failed"


def _ensure_docker_image(tag: str) -> tuple[bool, str]:
    try:
        inspect = subprocess.run(
            ["docker", "image", "inspect", tag],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=10,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return False, f"docker image inspect {tag} timed out"
    if inspect.returncode == 0:
        return True, ""

    try:
        pull = subprocess.run(
            ["docker", "pull", tag],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=300,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return False, f"docker pull {tag} timed out"
    if pull.returncode == 0:
        return True, ""
    return False, pull.stderr.strip() or pull.stdout.strip() or f"docker pull {tag} failed"


@pytest.mark.docker
def test_docker_sandbox_smoke(tmp_path: Path) -> None:
    ok, reason = _docker_available()
    if not ok:
        pytest.skip(reason)

    ok, reason = _ensure_docker_image("busybox:1.36")
    if not ok:
        pytest.skip(reason)

    context_dir = tmp_path / "context"
    context_dir.mkdir()
    dockerfile = context_dir / "Dockerfile"
    dockerfile.write_text(
        "\n".join(
            [
                "FROM busybox:1.36",
                "RUN echo hello > /hello.txt",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir()
    artifacts_dir = tmp_path / "artifacts"

    spec = SandboxSpec(
        backend="docker",
        image_context_path=context_dir,
        network_mode="open",
    )

    name = f"sandbox-smoke-{uuid.uuid4().hex[:10]}"
    sandbox = DockerSandbox(
        workspace_dir=workspace_dir,
        artifacts_dir=artifacts_dir,
        spec=spec,
        container_name=name,
    ).start()
    try:
        meta_path = artifacts_dir / "sandbox.json"
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        assert meta.get("backend") == "docker"
        assert meta.get("container_name") == sandbox.container_name

        proc = subprocess.run(
            [*sandbox.command_prefix, "sh", "-lc", "cat /hello.txt"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=10,
            check=False,
        )
        assert proc.returncode == 0
        assert proc.stdout.strip() == "hello"
    finally:
        sandbox.close()

    gone = subprocess.run(
        ["docker", "container", "inspect", sandbox.container_name],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=10,
        check=False,
    )
    assert gone.returncode != 0
