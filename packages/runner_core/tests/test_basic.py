from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from uuid import uuid4

import pytest

from runner_core.pathing import slugify
from runner_core.prompt import TemplateSubstitutionError, build_prompt_from_template
from runner_core.target_acquire import acquire_target


def test_slugify() -> None:
    assert slugify("https://github.com/org/repo.git") == "repo"
    assert slugify(r"I:\code\some_repo") == "some_repo"


def test_build_prompt_from_template_substitutes() -> None:
    template = "Hello ${name}.\nPolicy:\n${policy_json}\n"
    out = build_prompt_from_template(
        template_text=template,
        variables={"name": "World", "policy_json": '{"allow_edits": false}'},
    )
    assert "Hello World." in out
    assert '{"allow_edits": false}' in out


def test_build_prompt_from_template_errors_on_missing_vars() -> None:
    template = "Hello ${name}. Missing: ${nope}\n"
    with pytest.raises(TemplateSubstitutionError):
        build_prompt_from_template(template_text=template, variables={"name": "World"})


def test_acquire_target_relocates_dest_when_inside_source(tmp_path: Path) -> None:
    src = tmp_path / "src_repo"
    src.mkdir()
    (src / "README.md").write_text("# hi\n", encoding="utf-8")

    dest_inside = src / "runs" / "_workspaces" / f"ws_{uuid4().hex}"
    acquired = acquire_target(repo=str(src), dest_dir=dest_inside, ref=None)

    try:
        assert acquired.workspace_dir.is_dir()
        assert not acquired.workspace_dir.resolve().is_relative_to(src.resolve())
    finally:
        if not acquired.workspace_dir.resolve().is_relative_to(src.resolve()):
            shutil.rmtree(acquired.workspace_dir, ignore_errors=True)


def test_acquire_target_copy_ignores_generated_dirs(tmp_path: Path) -> None:
    src = tmp_path / "src_repo"
    src.mkdir()

    (src / "keep.txt").write_text("ok\n", encoding="utf-8")
    (src / ".venv" / "pyvenv.cfg").parent.mkdir(parents=True)
    (src / ".venv" / "pyvenv.cfg").write_text("venv\n", encoding="utf-8")
    (src / "node_modules" / "x" / "y.js").parent.mkdir(parents=True)
    (src / "node_modules" / "x" / "y.js").write_text("x\n", encoding="utf-8")
    (src / "runs" / "_workspaces" / "ws1" / "nested.txt").parent.mkdir(parents=True)
    (src / "runs" / "_workspaces" / "ws1" / "nested.txt").write_text("nope\n", encoding="utf-8")

    # Ensure "runs" is only ignored at repo root (not globally).
    (src / "src" / "runs" / "keep2.txt").parent.mkdir(parents=True)
    (src / "src" / "runs" / "keep2.txt").write_text("ok\n", encoding="utf-8")

    dest = tmp_path / f"dest_{uuid4().hex}"
    acquired = acquire_target(repo=str(src), dest_dir=dest, ref=None)
    try:
        workspace = acquired.workspace_dir
        assert (workspace / "keep.txt").exists()
        assert not (workspace / ".venv").exists()
        assert not (workspace / "node_modules").exists()
        assert not (workspace / "runs").exists()
        assert (workspace / "src" / "runs" / "keep2.txt").exists()
    finally:
        shutil.rmtree(acquired.workspace_dir, ignore_errors=True)


@pytest.mark.skipif(os.name != "nt", reason="Windows-only long path handling")
def test_acquire_target_relocates_dest_for_windows_long_paths(tmp_path: Path) -> None:
    src = Path(tempfile.gettempdir()) / f"ut_src_{uuid4().hex}"
    dest_name = f"ws_{uuid4().hex}"
    dest = tmp_path / ("a" * 80) / ("b" * 80) / dest_name

    src.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(["git", "-C", str(src), "init"], check=True, capture_output=True, text=True)
        subprocess.run(
            ["git", "-C", str(src), "config", "user.email", "usertest@local"],
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "-C", str(src), "config", "user.name", "usertest"],
            check=True,
            capture_output=True,
            text=True,
        )

        tmp_root = Path(tempfile.gettempdir())
        base_len = len(str(tmp_root / "usertest_workspaces" / dest_name)) + 1
        long_dir_len = max(1, 248 - base_len)
        long_dir = "d" * long_dir_len

        tracked = src / long_dir / "x.txt"
        tracked.parent.mkdir(parents=True, exist_ok=True)
        tracked.write_text("x\n", encoding="utf-8")
        subprocess.run(
            ["git", "-C", str(src), "add", "-A"],
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "-C", str(src), "commit", "-m", "init"],
            check=True,
            capture_output=True,
            text=True,
        )

        acquired = acquire_target(repo=str(src), dest_dir=dest, ref=None)
        try:
            assert acquired.workspace_dir != dest
            assert "ut" in acquired.workspace_dir.parts
            assert (acquired.workspace_dir / long_dir / "x.txt").exists()
        finally:
            shutil.rmtree(acquired.workspace_dir, ignore_errors=True)
    finally:
        shutil.rmtree(src, ignore_errors=True)
