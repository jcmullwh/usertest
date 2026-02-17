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


def _make_target_repo(tmp_path: Path, *, manifest_text: str | None) -> Path:
    target = tmp_path / "target"
    target.mkdir(parents=True, exist_ok=True)
    if manifest_text is not None:
        _write(target / ".usertest" / "sandbox_cli_install.yaml", manifest_text)
    return target


def test_target_manifest_adds_overlay_items(tmp_path: Path) -> None:
    base_context_dir = _make_sandbox_cli_context(tmp_path)
    target = _make_target_repo(
        tmp_path,
        manifest_text="\n".join(
            [
                "version: 1",
                "sandbox_cli_install:",
                "  apt:",
                "    - ffmpeg",
                "",
            ]
        ),
    )
    run_dir = tmp_path / "run"

    context_dir = _maybe_prepare_sandbox_cli_context(
        repo_root=tmp_path,
        run_dir=run_dir,
        base_context_dir=base_context_dir,
        agent_cfg=None,
        target_repo_root=target,
        docker_python="context",
        use_target_sandbox_cli_install=True,
    )

    assert context_dir == run_dir / "sandbox" / "image_context"

    apt_overlay = (context_dir / "overlays" / "manifests" / "apt.txt").read_text(encoding="utf-8")
    assert "ffmpeg" in apt_overlay

    install_meta = json.loads(
        (run_dir / "sandbox" / "sandbox_cli_install.json").read_text(encoding="utf-8")
    )
    assert install_meta["use_target_sandbox_cli_install"] is True
    assert install_meta["target_manifest_present"] is True
    assert install_meta["target_manifest"]["apt"] == ["ffmpeg"]
    assert "ffmpeg" in install_meta["merged_install"]["apt"]


def test_target_manifest_missing_is_nonfatal(tmp_path: Path) -> None:
    base_context_dir = _make_sandbox_cli_context(tmp_path)
    target = _make_target_repo(tmp_path, manifest_text=None)
    run_dir = tmp_path / "run"

    context_dir = _maybe_prepare_sandbox_cli_context(
        repo_root=tmp_path,
        run_dir=run_dir,
        base_context_dir=base_context_dir,
        agent_cfg=None,
        target_repo_root=target,
        docker_python="context",
        use_target_sandbox_cli_install=True,
    )

    assert context_dir == base_context_dir
    install_meta = json.loads(
        (run_dir / "sandbox" / "sandbox_cli_install.json").read_text(encoding="utf-8")
    )
    assert install_meta["use_target_sandbox_cli_install"] is True
    assert install_meta["target_manifest_present"] is False
    assert install_meta["target_manifest"] is None


def test_target_manifest_invalid_fails_loudly(tmp_path: Path) -> None:
    base_context_dir = _make_sandbox_cli_context(tmp_path)
    target = _make_target_repo(
        tmp_path,
        manifest_text="\n".join(
            [
                "version: 999",
                "sandbox_cli_install:",
                "  apt: [ffmpeg]",
                "",
            ]
        ),
    )
    run_dir = tmp_path / "run"

    with pytest.raises(ValueError, match="Unsupported target sandbox install manifest version"):
        _maybe_prepare_sandbox_cli_context(
            repo_root=tmp_path,
            run_dir=run_dir,
            base_context_dir=base_context_dir,
            agent_cfg=None,
            target_repo_root=target,
            docker_python="context",
            use_target_sandbox_cli_install=True,
        )
