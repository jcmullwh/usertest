from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from runner_core.pip_target import is_pip_repo_input, parse_pip_repo_input, requirements_path
from runner_core.target_acquire import acquire_target


def test_is_pip_repo_input() -> None:
    assert is_pip_repo_input("pip:agent-adapters")
    assert is_pip_repo_input("pypi:agent-adapters")
    assert is_pip_repo_input("pdm:agent-adapters")
    assert not is_pip_repo_input("https://example.invalid/repo.git")
    assert not is_pip_repo_input("")


def test_parse_pip_repo_input() -> None:
    spec = parse_pip_repo_input("pip:agent-adapters==0.1.0 normalized-events")
    assert spec.installer == "pip"
    assert spec.requirements == ("agent-adapters==0.1.0", "normalized-events")


def test_parse_pip_repo_input_rejects_flags() -> None:
    with pytest.raises(ValueError, match="no pip flags"):
        parse_pip_repo_input("pip:--pre agent-adapters")


def test_parse_pdm_repo_input() -> None:
    spec = parse_pip_repo_input("pdm:agent-adapters normalized-events")
    assert spec.installer == "pdm"
    assert spec.requirements == ("agent-adapters", "normalized-events")


def test_acquire_target_pip_creates_workspace(tmp_path: Path) -> None:
    dest_dir = tmp_path / "workspace"
    acquired = acquire_target(repo="pip:agent-adapters==0.1.0", dest_dir=dest_dir, ref=None)

    assert acquired.mode == "pip"
    assert acquired.workspace_dir.exists()
    assert (acquired.workspace_dir / ".git").exists()

    req_path = requirements_path(acquired.workspace_dir)
    assert req_path.exists()
    assert "agent-adapters==0.1.0" in req_path.read_text(encoding="utf-8")

    proc = subprocess.run(
        ["git", "-C", str(acquired.workspace_dir), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert proc.returncode == 0
    assert proc.stdout.strip() == acquired.commit_sha


def test_acquire_target_pdm_creates_pyproject(tmp_path: Path) -> None:
    dest_dir = tmp_path / "workspace_pdm"
    acquired = acquire_target(repo="pdm:agent-adapters==0.1.0", dest_dir=dest_dir, ref=None)

    assert acquired.mode == "pip"
    pyproject = acquired.workspace_dir / "pyproject.toml"
    assert pyproject.exists()
    text = pyproject.read_text(encoding="utf-8")
    assert '[project]' in text
    assert '"agent-adapters==0.1.0"' in text
