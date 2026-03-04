from __future__ import annotations

import argparse
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest


def _load_scaffold_module():
    scaffold_path = Path(__file__).resolve().with_name("scaffold.py")
    spec = importlib.util.spec_from_file_location("scaffold_cli_module_for_run_prereq_tests", scaffold_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Failed to load scaffold module from {scaffold_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


scaffold = _load_scaffold_module()


def _run_args(*, task: str, fix: bool = False) -> argparse.Namespace:
    return argparse.Namespace(
        task=task,
        fix=fix,
        all=True,
        kind=None,
        project=[],
        skip_missing=False,
        keep_going=False,
    )


def test_run_test_bootstraps_requirements_and_injects_pythonpath(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repo_root = tmp_path
    project_dir = repo_root / "demo"
    (project_dir / "src").mkdir(parents=True)
    (repo_root / "requirements-dev.txt").write_text("pytest>=8.0.0\n", encoding="utf-8")

    monkeypatch.setattr(scaffold, "_repo_root", lambda: repo_root)
    monkeypatch.setattr(
        scaffold,
        "_load_projects",
        lambda _: [{"id": "demo", "path": "demo", "tasks": {"test": ["python", "-m", "pytest", "-q"]}}],
    )

    state = {"bootstrapped": False}

    def fake_probe(
        argv: list[str],
        *,
        cwd: Path,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        del cwd, env
        if argv == [sys.executable, "-m", "pip", "--version"]:
            return subprocess.CompletedProcess(args=argv, returncode=0, stdout="pip ok", stderr="")
        if argv == [sys.executable, "-m", "pytest", "--version"]:
            return subprocess.CompletedProcess(
                args=argv,
                returncode=0 if state["bootstrapped"] else 1,
                stdout="pytest 8.0.0\n" if state["bootstrapped"] else "",
                stderr="" if state["bootstrapped"] else "pytest missing",
            )
        return subprocess.CompletedProcess(args=argv, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(scaffold, "_probe", fake_probe)

    pip_installs: list[list[str]] = []

    def fake_run(
        argv: list[str],
        *,
        cwd: Path,
        env: dict[str, str] | None = None,
        capture: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        del cwd, env, capture
        if argv[:4] == [sys.executable, "-m", "pip", "install"]:
            pip_installs.append(argv)
            state["bootstrapped"] = True
        return subprocess.CompletedProcess(args=argv, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(scaffold, "_run", fake_run)

    task_envs: list[dict[str, str] | None] = []

    def fake_run_manifest_task(
        *,
        cmd: list[str],
        cwd: Path,
        task_name: str,
        project_id: str,
        extra_env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        del cmd, cwd, task_name, project_id
        task_envs.append(extra_env)
        return subprocess.CompletedProcess(args=["python", "-m", "pytest", "-q"], returncode=0)

    monkeypatch.setattr(scaffold, "_run_manifest_task", fake_run_manifest_task)

    rc = scaffold.cmd_run(_run_args(task="test"))
    assert rc == 0
    assert pip_installs, "Expected requirements bootstrap via pip install."
    assert task_envs and task_envs[0] is not None
    pythonpath = task_envs[0].get("PYTHONPATH")
    assert pythonpath is not None
    assert str(project_dir / "src") in pythonpath


def test_run_test_retries_install_when_pdm_pytest_missing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repo_root = tmp_path
    project_dir = repo_root / "demo"
    (project_dir / ".venv").mkdir(parents=True)

    monkeypatch.setattr(scaffold, "_repo_root", lambda: repo_root)
    monkeypatch.setattr(
        scaffold,
        "_load_projects",
        lambda _: [
            {
                "id": "demo",
                "path": "demo",
                "tasks": {
                    "install": ["pdm", "install"],
                    "test": ["pdm", "run", "pytest", "-q"],
                },
            }
        ],
    )

    state = {"installed": False}

    def fake_probe(
        argv: list[str],
        *,
        cwd: Path,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        del cwd, env
        if argv == ["pdm", "run", "pytest", "--version"]:
            return subprocess.CompletedProcess(
                args=argv,
                returncode=0 if state["installed"] else 1,
                stdout="pytest 8.0.0\n" if state["installed"] else "",
                stderr="" if state["installed"] else "pytest missing",
            )
        return subprocess.CompletedProcess(args=argv, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(scaffold, "_probe", fake_probe)

    task_calls: list[str] = []

    def fake_run_manifest_task(
        *,
        cmd: list[str],
        cwd: Path,
        task_name: str,
        project_id: str,
        extra_env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        del cmd, cwd, project_id, extra_env
        task_calls.append(task_name)
        if task_name == "install":
            state["installed"] = True
        return subprocess.CompletedProcess(args=["pdm", task_name], returncode=0)

    monkeypatch.setattr(scaffold, "_run_manifest_task", fake_run_manifest_task)

    rc = scaffold.cmd_run(_run_args(task="test"))
    assert rc == 0
    assert task_calls == ["install", "test"]


def test_resolve_argv_uses_python_module_fallback_for_pdm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if scaffold.os.name == "nt":
        pytest.skip("non-Windows fallback only")

    monkeypatch.setattr(scaffold, "_which", lambda cmd: None if cmd == "pdm" else "/usr/bin/other")
    monkeypatch.setattr(scaffold, "_pdm_importable", lambda: True)

    resolved = scaffold._resolve_argv(["pdm", "--version"])
    assert resolved == [sys.executable, "-m", "pdm", "--version"]
