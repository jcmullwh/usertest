from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from agent_adapters.claude_cli import run_claude_print


def test_run_claude_print_injects_docker_exec_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {}

    class _FakeStdin:
        def write(self, text: str) -> None:
            return

        def close(self) -> None:
            return

    class _FakeProc:
        def __init__(self, argv: object, **kwargs: object) -> None:
            captured["argv"] = argv
            captured["env"] = kwargs.get("env")
            self.stdin = _FakeStdin()
            self.stdout = []
            self.returncode = 0

        def wait(self) -> int:
            self.returncode = 0
            return 0

    def _fake_popen(*args: object, **kwargs: object) -> _FakeProc:
        return _FakeProc(args[0], **kwargs)

    monkeypatch.setattr(subprocess, "Popen", _fake_popen)

    run_claude_print(
        workspace_dir=tmp_path,
        prompt="hi",
        raw_events_path=tmp_path / "raw.jsonl",
        last_message_path=tmp_path / "last.txt",
        stderr_path=tmp_path / "stderr.txt",
        command_prefix=["docker", "exec", "-i", "-w", "/workspace", "c1"],
        env_overrides={"TOKEN": "x", "CODEX_HOME": "/artifacts/codex_home"},
    )

    assert captured["env"] is None
    assert captured["argv"] == [
        "docker",
        "exec",
        "-i",
        "-w",
        "/workspace",
        "-e",
        "CODEX_HOME=/artifacts/codex_home",
        "-e",
        "TOKEN=x",
        "c1",
        "claude",
        "-p",
        "--output-format",
        "stream-json",
        "--verbose",
    ]
