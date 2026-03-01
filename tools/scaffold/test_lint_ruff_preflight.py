from __future__ import annotations

import argparse
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest


def _load_scaffold_module():
    scaffold_path = Path(__file__).resolve().with_name("scaffold.py")
    spec = importlib.util.spec_from_file_location("scaffold_cli_module", scaffold_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Failed to load scaffold module from {scaffold_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


scaffold = _load_scaffold_module()


def test_run_lint_fails_fast_with_single_install_remediation_when_ruff_missing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    (tmp_path / "demo").mkdir(parents=True)

    monkeypatch.setattr(scaffold, "_repo_root", lambda: tmp_path)
    monkeypatch.setattr(
        scaffold,
        "_load_projects",
        lambda repo_root: [
            {
                "id": "demo",
                "path": "demo",
                "tasks": {
                    "lint": ["pdm", "run", "ruff", "check", "."],
                },
            }
        ],
    )

    def fake_probe(
        argv: list[str],
        *,
        cwd: Path,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        del argv, cwd, env
        return subprocess.CompletedProcess(args=["pdm"], returncode=1, stdout="", stderr="ruff not found")

    monkeypatch.setattr(scaffold, "_probe", fake_probe)

    def fail_run_manifest_task(*, cmd: list[str], cwd: Path, task_name: str, project_id: str):
        del cmd, cwd, task_name, project_id
        raise AssertionError("Expected lint to fail during preflight (before running the task).")

    monkeypatch.setattr(scaffold, "_run_manifest_task", fail_run_manifest_task)

    args = argparse.Namespace(
        task="lint",
        all=True,
        kind=None,
        project=None,
        skip_missing=False,
        keep_going=False,
    )

    with pytest.raises(scaffold.ScaffoldError) as excinfo:
        scaffold.cmd_run(args)

    msg = str(excinfo.value)
    assert "lint requires 'ruff'" in msg
    assert "python tools/scaffold/scaffold.py run install --all" in msg
