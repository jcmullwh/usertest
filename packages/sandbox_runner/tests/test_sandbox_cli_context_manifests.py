from __future__ import annotations

from pathlib import Path


def test_sandbox_cli_apt_manifest_includes_file_utility() -> None:
    sandbox_runner_root = Path(__file__).resolve().parents[1]
    apt_manifest = (
        sandbox_runner_root
        / "builtins"
        / "docker"
        / "contexts"
        / "sandbox_cli"
        / "manifests"
        / "apt.txt"
    )
    lines = [
        line.strip()
        for line in apt_manifest.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    assert "file" in lines


def test_sandbox_cli_pip_manifest_includes_pdm() -> None:
    sandbox_runner_root = Path(__file__).resolve().parents[1]
    pip_manifest = (
        sandbox_runner_root
        / "builtins"
        / "docker"
        / "contexts"
        / "sandbox_cli"
        / "manifests"
        / "pip.txt"
    )
    lines = [
        line.strip()
        for line in pip_manifest.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    assert any(line.startswith("pdm==") for line in lines)
