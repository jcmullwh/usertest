from __future__ import annotations

import argparse
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest


def _load_scaffold_module():
    scaffold_path = Path(__file__).resolve().with_name("scaffold.py")
    spec = importlib.util.spec_from_file_location("scaffold_cli_module_for_run_fix_tests", scaffold_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Failed to load scaffold module from {scaffold_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


scaffold = _load_scaffold_module()


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _run_args(*, fix: bool) -> argparse.Namespace:
    return argparse.Namespace(
        task="lint",
        fix=fix,
        all=True,
        kind=None,
        project=[],
        skip_missing=False,
        keep_going=False,
    )


def test_run_lint_fix_prefers_manifest_task(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    repo_root = tmp_path
    monkeypatch.setattr(scaffold, "_repo_root", lambda: repo_root)

    _write(
        repo_root / "tools/scaffold/monorepo.toml",
        """
schema_version = 1

[[projects]]
id = "demo"
kind = "lib"
path = "packages/demo"
generator = "python_pdm_lib"
toolchain = "python"
package_manager = "pdm"
ci = { lint = true, test = false, build = false }
tasks.lint = ["pdm", "run", "ruff", "check", "."]
tasks.lint_fix = ["pdm", "run", "ruff", "check", "--fix", "."]
""".lstrip(),
    )
    (repo_root / "packages/demo").mkdir(parents=True, exist_ok=True)

    calls: list[list[str]] = []

    def fake_run_manifest_task(*, cmd: list[str], cwd: Path, task_name: str, project_id: str):
        del cwd, task_name, project_id
        calls.append(cmd)
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(scaffold, "_run_manifest_task", fake_run_manifest_task)

    rc = scaffold.cmd_run(_run_args(fix=True))
    assert rc == 0
    assert calls == [["pdm", "run", "ruff", "check", "--fix", "."]]


def test_run_lint_fix_inserts_ruff_fix_when_missing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    repo_root = tmp_path
    monkeypatch.setattr(scaffold, "_repo_root", lambda: repo_root)

    _write(
        repo_root / "tools/scaffold/monorepo.toml",
        """
schema_version = 1

[[projects]]
id = "demo"
kind = "lib"
path = "packages/demo"
generator = "python_pdm_lib"
toolchain = "python"
package_manager = "pdm"
ci = { lint = true, test = false, build = false }
tasks.lint = ["pdm", "run", "ruff", "check", "."]
""".lstrip(),
    )
    (repo_root / "packages/demo").mkdir(parents=True, exist_ok=True)

    calls: list[list[str]] = []

    def fake_run_manifest_task(*, cmd: list[str], cwd: Path, task_name: str, project_id: str):
        del cwd, task_name, project_id
        calls.append(cmd)
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(scaffold, "_run_manifest_task", fake_run_manifest_task)

    rc = scaffold.cmd_run(_run_args(fix=True))
    assert rc == 0
    assert calls == [["pdm", "run", "ruff", "check", "--fix", "."]]


def test_run_lint_fix_errors_for_unsupported_lint(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    repo_root = tmp_path
    monkeypatch.setattr(scaffold, "_repo_root", lambda: repo_root)

    _write(
        repo_root / "tools/scaffold/monorepo.toml",
        """
schema_version = 1

[[projects]]
id = "demo"
kind = "lib"
path = "packages/demo"
generator = "python_stdlib_copy"
toolchain = "python"
package_manager = "none"
ci = { lint = true, test = false, build = false }
tasks.lint = ["python", "-m", "compileall", "src"]
""".lstrip(),
    )
    (repo_root / "packages/demo").mkdir(parents=True, exist_ok=True)

    with pytest.raises(scaffold.ScaffoldError, match=r"missing tasks\.lint_fix"):
        scaffold.cmd_run(_run_args(fix=True))
