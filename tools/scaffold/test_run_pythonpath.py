from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest


def _load_scaffold_module():
    scaffold_path = Path(__file__).resolve().with_name("scaffold.py")
    spec = importlib.util.spec_from_file_location("scaffold_cli_module_for_pythonpath_tests", scaffold_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Failed to load scaffold module from {scaffold_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


scaffold = _load_scaffold_module()


def test_run_manifest_task_test_injects_monorepo_pythonpath(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    app_src = tmp_path / "apps" / "demo_app" / "src"
    pkg_src = tmp_path / "packages" / "demo_pkg" / "src"
    app_src.mkdir(parents=True)
    pkg_src.mkdir(parents=True)

    monkeypatch.setattr(scaffold, "_repo_root", lambda: tmp_path)
    monkeypatch.setenv("PYTHONPATH", "/existing/path")

    captured_env: dict[str, str] = {}

    def fake_run(
        argv: list[str],
        *,
        cwd: Path,
        env: dict[str, str] | None = None,
        capture: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        del argv, cwd, capture
        assert env is not None
        captured_env.update(env)
        return subprocess.CompletedProcess(args=["pdm"], returncode=0, stdout="", stderr="")

    monkeypatch.setattr(scaffold, "_run", fake_run)

    cp = scaffold._run_manifest_task(
        cmd=["pdm", "run", "pytest", "-q"],
        cwd=tmp_path,
        task_name="test",
        project_id="demo_pkg",
    )

    assert cp.returncode == 0
    assert captured_env["PDM_IGNORE_ACTIVE_VENV"] == "1"
    parts = captured_env["PYTHONPATH"].split(os.pathsep)
    assert str(app_src) in parts
    assert str(pkg_src) in parts
    assert "/existing/path" in parts
