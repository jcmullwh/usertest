from __future__ import annotations

import importlib.resources


def test_sandbox_cli_apt_manifest_includes_file_utility() -> None:
    apt_manifest = (
        importlib.resources.files("sandbox_runner")
        / "builtins"
        / "docker"
        / "contexts"
        / "sandbox_cli"
        / "manifests"
        / "apt.txt"
    )
    with importlib.resources.as_file(apt_manifest) as apt_path:
        text = apt_path.read_text(encoding="utf-8")
    lines = [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    assert "file" in lines


def test_sandbox_cli_pip_manifest_includes_pdm() -> None:
    pip_manifest = (
        importlib.resources.files("sandbox_runner")
        / "builtins"
        / "docker"
        / "contexts"
        / "sandbox_cli"
        / "manifests"
        / "pip.txt"
    )
    with importlib.resources.as_file(pip_manifest) as pip_path:
        text = pip_path.read_text(encoding="utf-8")
    lines = [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    assert any(line.startswith("pdm==") for line in lines)
