from __future__ import annotations

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


def test_detects_known_transient_pdm_local_path_failure() -> None:
    stderr = (
        "ERROR: Unable to find candidates for normalized-events\n"
        "See pdm resolver output for local path dependency details\n"
    )
    assert scaffold._looks_like_transient_pdm_local_path_failure(stdout="", stderr=stderr)


def test_does_not_mark_unrelated_install_error_as_transient() -> None:
    stderr = "ERROR: no such option: --frozen-lockfile\n"
    assert not scaffold._looks_like_transient_pdm_local_path_failure(stdout="", stderr=stderr)


def test_run_manifest_task_retries_once_for_known_transient_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[subprocess.CompletedProcess[str]] = []

    first = subprocess.CompletedProcess(
        args=["pdm", "install"],
        returncode=1,
        stdout="",
        stderr="Unable to find candidates for normalized-events",
    )
    second = subprocess.CompletedProcess(
        args=["pdm", "install"],
        returncode=0,
        stdout="Installed successfully",
        stderr="",
    )

    def fake_run(
        argv: list[str],
        *,
        cwd: Path,
        env: dict[str, str] | None = None,
        capture: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        del argv, cwd, env, capture
        if not calls:
            calls.append(first)
            return first
        calls.append(second)
        return second

    monkeypatch.setattr(scaffold, "_run", fake_run)

    cp = scaffold._run_manifest_task(
        cmd=["pdm", "install"],
        cwd=tmp_path,
        task_name="install",
        project_id="demo",
    )
    assert cp.returncode == 0
    assert len(calls) == 2


def test_run_manifest_task_does_not_retry_for_non_matching_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[subprocess.CompletedProcess[str]] = []
    failure = subprocess.CompletedProcess(
        args=["pdm", "install"],
        returncode=1,
        stdout="",
        stderr="ERROR: incompatible wheel",
    )

    def fake_run(
        argv: list[str],
        *,
        cwd: Path,
        env: dict[str, str] | None = None,
        capture: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        del argv, cwd, env, capture
        calls.append(failure)
        return failure

    monkeypatch.setattr(scaffold, "_run", fake_run)

    cp = scaffold._run_manifest_task(
        cmd=["pdm", "install"],
        cwd=tmp_path,
        task_name="install",
        project_id="demo",
    )
    assert cp.returncode == 1
    assert len(calls) == 1
