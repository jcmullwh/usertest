from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from runner_core import find_repo_root


def _run_lint_script(repo_root: Path, rel_path: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(repo_root / rel_path)],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )


def test_lint_prompts_script_passes() -> None:
    repo_root = find_repo_root(Path(__file__).resolve())
    cp = _run_lint_script(repo_root, "tools/lint_prompts.py")
    assert cp.returncode == 0, (cp.stdout + "\n" + cp.stderr).strip()


def test_lint_analysis_principles_script_passes() -> None:
    repo_root = find_repo_root(Path(__file__).resolve())
    cp = _run_lint_script(repo_root, "tools/lint_analysis_principles.py")
    assert cp.returncode == 0, (cp.stdout + "\n" + cp.stderr).strip()


def test_lint_local_dependency_urls_script_passes() -> None:
    repo_root = find_repo_root(Path(__file__).resolve())
    cp = _run_lint_script(repo_root, "tools/lint_local_dependency_urls.py")
    assert cp.returncode == 0, (cp.stdout + "\n" + cp.stderr).strip()
