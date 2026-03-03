from __future__ import annotations

import argparse
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest


def _load_scaffold_module():
    scaffold_path = Path(__file__).resolve().with_name("scaffold.py")
    spec = importlib.util.spec_from_file_location("scaffold_cli_module_for_bootstrap_tests", scaffold_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Failed to load scaffold module from {scaffold_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


scaffold = _load_scaffold_module()


def test_format_run_install_remediation_cmd_preserves_skip_missing() -> None:
    args = argparse.Namespace(all=True, kind=None, project=None, skip_missing=True)
    cmd = scaffold._format_run_install_remediation_cmd(args, failing_project_id="demo")
    assert cmd == "python tools/scaffold/scaffold.py run install --all --skip-missing"


def test_run_test_surfaces_bootstrap_remediation_for_pdm_prereq_failures(
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
                    "install": ["pdm", "install"],
                    "test": ["pdm", "run", "pytest", "-q"],
                },
            }
        ],
    )

    def fake_run_manifest_task(*, cmd: list[str], cwd: Path, task_name: str, project_id: str):
        del cmd, cwd, task_name, project_id
        return subprocess.CompletedProcess(
            args=["pdm", "run", "pytest", "-q"],
            returncode=1,
            stdout="",
            stderr="[VirtualenvCreateError]: failed to create virtualenv",
        )

    monkeypatch.setattr(scaffold, "_run_manifest_task", fake_run_manifest_task)

    args = argparse.Namespace(
        task="test",
        fix=False,
        all=True,
        kind=None,
        project=None,
        skip_missing=True,
        keep_going=False,
    )

    with pytest.raises(scaffold.ScaffoldError) as excinfo:
        scaffold.cmd_run(args)

    msg = str(excinfo.value)
    assert "bootstrap prerequisites were satisfied" in msg
    assert "python tools/scaffold/scaffold.py run install --all --skip-missing" in msg
    assert "requirements-dev.txt" in msg
