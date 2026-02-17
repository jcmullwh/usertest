from __future__ import annotations

import json
from pathlib import Path

import pytest

from runner_core.execution_backend import _maybe_prepare_sandbox_cli_context


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def _make_sandbox_cli_context(tmp_path: Path) -> Path:
    context_dir = tmp_path / "context"
    (context_dir / "scripts").mkdir(parents=True, exist_ok=True)
    _write(context_dir / "scripts" / "install_manifests.sh", "#!/bin/sh\n")
    _write(context_dir / "Dockerfile", "FROM python:3.11-slim\n")
    return context_dir


def _make_target_repo(tmp_path: Path, *, requires_python: str | None) -> Path:
    target = tmp_path / "target"
    target.mkdir(parents=True, exist_ok=True)
    if requires_python is not None:
        _write(
            target / "pyproject.toml",
            "\n".join(
                [
                    "[project]",
                    f'requires-python = "{requires_python}"',
                    "",
                ]
            ),
        )
    return target


def test_auto_overrides_base_image_when_target_requires_newer_python(tmp_path: Path) -> None:
    base_context_dir = _make_sandbox_cli_context(tmp_path)
    target = _make_target_repo(tmp_path, requires_python=">=3.12")
    run_dir = tmp_path / "run"

    context_dir = _maybe_prepare_sandbox_cli_context(
        repo_root=tmp_path,
        run_dir=run_dir,
        base_context_dir=base_context_dir,
        agent_cfg=None,
        target_repo_root=target,
        docker_python="auto",
    )

    assert context_dir == run_dir / "sandbox" / "image_context"
    dockerfile_text = (context_dir / "Dockerfile").read_text(encoding="utf-8")
    assert dockerfile_text.splitlines()[0] == "FROM python:3.12-slim"

    selection_path = run_dir / "sandbox" / "python_selection.json"
    selection = json.loads(selection_path.read_text(encoding="utf-8"))
    assert selection["selected_base_image"] == "python:3.12-slim"
    assert "override" in selection["selection_reason"]


def test_auto_keeps_base_image_when_target_requires_compatible_python(tmp_path: Path) -> None:
    base_context_dir = _make_sandbox_cli_context(tmp_path)
    target = _make_target_repo(tmp_path, requires_python=">=3.9")
    run_dir = tmp_path / "run"

    context_dir = _maybe_prepare_sandbox_cli_context(
        repo_root=tmp_path,
        run_dir=run_dir,
        base_context_dir=base_context_dir,
        agent_cfg=None,
        target_repo_root=target,
        docker_python="auto",
    )

    assert context_dir == base_context_dir
    selection_path = run_dir / "sandbox" / "python_selection.json"
    selection = json.loads(selection_path.read_text(encoding="utf-8"))
    assert selection["selected_base_image"] == "python:3.11-slim"
    assert "satisfies" in selection["selection_reason"]


def test_explicit_version_always_overrides(tmp_path: Path) -> None:
    base_context_dir = _make_sandbox_cli_context(tmp_path)
    target = _make_target_repo(tmp_path, requires_python=None)
    run_dir = tmp_path / "run"

    context_dir = _maybe_prepare_sandbox_cli_context(
        repo_root=tmp_path,
        run_dir=run_dir,
        base_context_dir=base_context_dir,
        agent_cfg=None,
        target_repo_root=target,
        docker_python="3.12",
    )

    assert context_dir == run_dir / "sandbox" / "image_context"
    dockerfile_text = (context_dir / "Dockerfile").read_text(encoding="utf-8")
    assert dockerfile_text.splitlines()[0] == "FROM python:3.12-slim"


def test_auto_fails_loudly_when_requires_python_is_unsatisfied(tmp_path: Path) -> None:
    base_context_dir = _make_sandbox_cli_context(tmp_path)
    target = _make_target_repo(tmp_path, requires_python=">=4.0")
    run_dir = tmp_path / "run"

    with pytest.raises(ValueError, match="auto-selection failed"):
        _maybe_prepare_sandbox_cli_context(
            repo_root=tmp_path,
            run_dir=run_dir,
            base_context_dir=base_context_dir,
            agent_cfg=None,
            target_repo_root=target,
            docker_python="auto",
        )

    selection_path = run_dir / "sandbox" / "python_selection.json"
    selection = json.loads(selection_path.read_text(encoding="utf-8"))
    assert selection["error"]
