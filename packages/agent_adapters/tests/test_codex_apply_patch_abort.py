from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from agent_adapters import run_codex_exec


def _make_dummy_emits_apply_patch_request_and_hangs(tmp_path: Path) -> str:
    apply_patch_line = (
        '{"id":"1","msg":{"type":"apply_patch_approval_request","call_id":"call_test","changes":{}}}'
    )
    if os.name == "nt":
        path = tmp_path / "dummy_codex_patch.cmd"
        path.write_text(
            "\n".join(
                [
                    "@echo off",
                    f"echo {apply_patch_line}",
                    "ping -n 600 127.0.0.1 >nul",
                    "exit /b 0",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        return str(path)

    path = tmp_path / "dummy_codex_patch.sh"
    path.write_text(
        "\n".join(
            [
                "#!/bin/sh",
                f'printf "%s\\n" \'{apply_patch_line}\'',
                "sleep 600",
                "",
            ]
        ),
        encoding="utf-8",
    )
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


def test_codex_cli_aborts_on_apply_patch_approval_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dummy = _make_dummy_emits_apply_patch_request_and_hangs(tmp_path)

    result = run_codex_exec(
        workspace_dir=tmp_path,
        prompt="hi",
        raw_events_path=tmp_path / "raw.jsonl",
        last_message_path=tmp_path / "last.txt",
        stderr_path=tmp_path / "stderr.txt",
        binary=dummy,
        sandbox="read-only",
        timeout_seconds=2,
    )

    assert result.exit_code != 0
    assert "apply_patch_approval_request" in (tmp_path / "raw.jsonl").read_text(
        encoding="utf-8", errors="replace"
    )
    stderr_text = (tmp_path / "stderr.txt").read_text(encoding="utf-8", errors="replace")
    assert "apply_patch_approval_request" in stderr_text or "interactive approval" in stderr_text
