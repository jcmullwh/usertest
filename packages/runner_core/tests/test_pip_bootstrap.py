from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from runner_core import pip_bootstrap as pb


def test_bootstrap_pip_requirements_uses_workspace_venv_for_docker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_dir = tmp_path / "workspace"
    run_dir = tmp_path / "run"
    workspace_dir.mkdir(parents=True, exist_ok=True)
    run_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(pb, "looks_like_docker_exec_prefix", lambda _prefix: True)
    monkeypatch.setattr(pb, "inject_docker_exec_env", lambda prefix, _env: list(prefix))

    def _fake_run_logged(
        argv: list[str],
        *,
        cwd: Path | None,
        env: dict[str, str] | None,
        log: list[str],
    ) -> subprocess.CompletedProcess[str]:
        del cwd, env
        log.append("$ " + " ".join(argv))
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(pb, "_run_logged", _fake_run_logged)

    def _fake_subprocess_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        del kwargs
        if "--format=json" in argv:
            return subprocess.CompletedProcess(argv, 0, stdout="[]", stderr="")
        return subprocess.CompletedProcess(argv, 0, stdout="/usr/local/bin", stderr="")

    monkeypatch.setattr(pb.subprocess, "run", _fake_subprocess_run)

    result = pb.bootstrap_pip_requirements(
        workspace_dir=workspace_dir,
        requirements_relpath=".usertest/requirements.txt",
        run_dir=run_dir,
        command_prefix=["docker", "exec", "-i", "container"],
        workspace_mount="/workspace",
    )

    assert result.meta["backend"] == "docker"
    assert result.meta["venv_dir"] == "/workspace/.venv"
    assert result.env_overrides["VIRTUAL_ENV"] == "/workspace/.venv"
    assert result.env_overrides["PATH"].startswith("/workspace/.venv/bin:")


def test_bootstrap_pip_requirements_falls_back_when_workspace_venv_forbidden(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_dir = tmp_path / "workspace"
    run_dir = tmp_path / "run"
    workspace_dir.mkdir(parents=True, exist_ok=True)
    run_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(pb, "looks_like_docker_exec_prefix", lambda _prefix: True)
    monkeypatch.setattr(pb, "inject_docker_exec_env", lambda prefix, _env: list(prefix))

    calls: list[list[str]] = []

    def _fake_run_logged(
        argv: list[str],
        *,
        cwd: Path | None,
        env: dict[str, str] | None,
        log: list[str],
    ) -> subprocess.CompletedProcess[str]:
        del cwd, env
        calls.append(argv)
        log.append("$ " + " ".join(argv))
        script = argv[-1]
        if "/workspace/.venv" in script:
            return subprocess.CompletedProcess(
                argv,
                1,
                stdout="",
                stderr=(
                    "Error: [Errno 1] Operation not permitted: 'lib' -> "
                    "'/workspace/.venv/lib64'"
                ),
            )
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(pb, "_run_logged", _fake_run_logged)

    def _fake_subprocess_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        del kwargs
        if "--format=json" in argv:
            return subprocess.CompletedProcess(argv, 0, stdout="[]", stderr="")
        return subprocess.CompletedProcess(argv, 0, stdout="/usr/local/bin", stderr="")

    monkeypatch.setattr(pb.subprocess, "run", _fake_subprocess_run)

    result = pb.bootstrap_pip_requirements(
        workspace_dir=workspace_dir,
        requirements_relpath=".usertest/requirements.txt",
        run_dir=run_dir,
        command_prefix=["docker", "exec", "-i", "container"],
        workspace_mount="/workspace",
    )

    assert len(calls) >= 2
    assert result.meta["backend"] == "docker"
    assert result.meta["venv_dir_requested"] == "/workspace/.venv"
    assert result.meta["venv_dir"] == "/tmp/usertest_pip_venv"
    assert result.meta["venv_fallback_used"] is True
    assert result.env_overrides["VIRTUAL_ENV"] == "/tmp/usertest_pip_venv"
    assert result.env_overrides["PATH"].startswith("/tmp/usertest_pip_venv/bin:")


def test_bootstrap_pdm_requirements_uses_pdm_install_in_docker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_dir = tmp_path / "workspace"
    run_dir = tmp_path / "run"
    workspace_dir.mkdir(parents=True, exist_ok=True)
    run_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(pb, "looks_like_docker_exec_prefix", lambda _prefix: True)
    monkeypatch.setattr(pb, "inject_docker_exec_env", lambda prefix, _env: list(prefix))

    scripts: list[str] = []

    def _fake_run_logged(
        argv: list[str],
        *,
        cwd: Path | None,
        env: dict[str, str] | None,
        log: list[str],
    ) -> subprocess.CompletedProcess[str]:
        del cwd, env
        log.append("$ " + " ".join(argv))
        scripts.append(argv[-1])
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(pb, "_run_logged", _fake_run_logged)

    def _fake_subprocess_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        del kwargs
        if "--format=json" in argv:
            return subprocess.CompletedProcess(argv, 0, stdout="[]", stderr="")
        return subprocess.CompletedProcess(argv, 0, stdout="/usr/local/bin", stderr="")

    monkeypatch.setattr(pb.subprocess, "run", _fake_subprocess_run)

    result = pb.bootstrap_pip_requirements(
        workspace_dir=workspace_dir,
        requirements_relpath=".usertest/requirements.txt",
        run_dir=run_dir,
        command_prefix=["docker", "exec", "-i", "container"],
        workspace_mount="/workspace",
        installer="pdm",
    )

    assert scripts
    assert any("-m pdm install --no-self" in script for script in scripts)
    assert result.meta["installer"] == "pdm"
