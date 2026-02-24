from __future__ import annotations

from pathlib import Path

from runner_core.agent_prompt_files import _materialize_agent_prompt_into_workspace


def test_materialize_agent_prompt_into_workspace_writes_text(tmp_path: Path) -> None:
    workspace_dir = tmp_path / "ws"
    workspace_dir.mkdir()

    payload = "# Ticket\n\nHello.\n"
    dest = _materialize_agent_prompt_into_workspace(
        workspace_dir=workspace_dir,
        name="append_system_prompt.md",
        src_path=None,
        text=payload,
    )

    assert dest == workspace_dir / "append_system_prompt.md"
    assert dest.read_text(encoding="utf-8") == payload


def test_materialize_agent_prompt_into_workspace_copies_file_and_skips_samefile(
    tmp_path: Path,
) -> None:
    workspace_dir = tmp_path / "ws"
    workspace_dir.mkdir()

    src = tmp_path / "src.md"
    payload = "from file\n"
    src.write_text(payload, encoding="utf-8")

    dest = _materialize_agent_prompt_into_workspace(
        workspace_dir=workspace_dir,
        name="append_system_prompt.md",
        src_path=src,
        text=None,
    )
    assert dest.read_text(encoding="utf-8") == payload

    # If a user explicitly points at `append_system_prompt.md` in the workspace already,
    # the runner should treat it as already materialized (no SameFileError).
    _materialize_agent_prompt_into_workspace(
        workspace_dir=workspace_dir,
        name="append_system_prompt.md",
        src_path=dest,
        text=None,
    )
    assert dest.read_text(encoding="utf-8") == payload
