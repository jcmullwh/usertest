from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

import runner_core.runner as runner_mod


def test_normalize_verification_commands_prepends_scaffold_install() -> None:
    commands = (
        "python tools/scaffold/scaffold.py run lint --all --skip-missing",
        "python tools/scaffold/scaffold.py run test --all --skip-missing",
    )

    effective = runner_mod._normalize_verification_commands_for_execution(commands)

    assert effective[0].command == "python tools/scaffold/scaffold.py run --all --skip-missing install"
    assert effective[0].track == runner_mod.VerificationTrack.BOOTSTRAP
    assert [e.command for e in effective[1:]] == list(commands)


def test_normalize_verification_commands_keeps_existing_scaffold_install() -> None:
    commands = (
        "python tools/scaffold/scaffold.py run install --all --skip-missing",
        "python tools/scaffold/scaffold.py run lint --all --skip-missing",
    )

    effective = runner_mod._normalize_verification_commands_for_execution(commands)

    assert [e.command for e in effective] == list(commands)
    assert all(e.track == runner_mod.VerificationTrack.REPO_HEALTH for e in effective)


def test_augment_env_with_workspace_pythonpath_discovers_src_dirs(tmp_path: Path) -> None:
    (tmp_path / "apps" / "alpha" / "src").mkdir(parents=True)
    (tmp_path / "packages" / "zeta" / "src").mkdir(parents=True)

    env = runner_mod._augment_env_with_workspace_pythonpath(
        env_overrides={"PYTHONPATH": "/already/there"},
        workspace_dir=tmp_path,
        workspace_mount="/workspace",
    )

    assert isinstance(env, dict)
    path_value = env.get("PYTHONPATH")
    assert isinstance(path_value, str)
    assert path_value.startswith("/workspace/apps/alpha/src:/workspace/packages/zeta/src")
    assert path_value.endswith("/already/there")


def test_run_verification_commands_applies_local_env_overrides(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, Any]] = []

    class _Proc:
        def __init__(self, argv: list[str]) -> None:
            self.returncode = 0
            self.stdout = "ok\n"
            self.stderr = ""
            self.args = list(argv)

    def _fake_run(argv: list[str], **kwargs: Any) -> _Proc:
        calls.append({"argv": list(argv), "env": kwargs.get("env")})
        return _Proc(argv)

    monkeypatch.setattr(runner_mod.subprocess, "run", _fake_run)

    summary = runner_mod._run_verification_commands(
        run_dir=tmp_path / "run",
        attempt_number=1,
        commands=["echo ok"],
        command_prefix=[],
        cwd=tmp_path,
        timeout_seconds=None,
        python_executable=None,
        env_overrides={
            "FOO": "bar",
            "PATH": os.environ.get("PATH", ""),
        },
    )

    assert summary["passed"] is True
    assert calls
    env = calls[0]["env"]
    assert isinstance(env, dict)
    assert env.get("FOO") == "bar"


def test_run_verification_commands_injects_env_overrides_for_docker_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    command_prefix = ["docker", "exec", "-i", "c"]
    injected_prefix = [*command_prefix, "--env", "FOO=bar"]
    calls: list[dict[str, Any]] = []

    class _Proc:
        def __init__(self, argv: list[str]) -> None:
            self.returncode = 0
            self.stdout = "ok\n"
            self.stderr = ""
            self.args = list(argv)

    def _fake_run(argv: list[str], **kwargs: Any) -> _Proc:
        calls.append({"argv": list(argv), "env": kwargs.get("env")})
        return _Proc(argv)

    monkeypatch.setattr(runner_mod.subprocess, "run", _fake_run)
    monkeypatch.setattr(runner_mod, "looks_like_docker_exec_prefix", lambda _prefix: True)
    monkeypatch.setattr(
        runner_mod,
        "inject_docker_exec_env",
        lambda prefix, _env: list(injected_prefix if prefix == command_prefix else prefix),
    )

    summary = runner_mod._run_verification_commands(
        run_dir=tmp_path / "run",
        attempt_number=1,
        commands=["echo ok"],
        command_prefix=command_prefix,
        cwd=tmp_path,
        timeout_seconds=None,
        python_executable=None,
        env_overrides={"FOO": "bar"},
    )

    assert summary["passed"] is True
    assert calls
    argv = calls[0]["argv"]
    assert argv[: len(injected_prefix)] == injected_prefix
    assert calls[0]["env"] is None
