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

    def _fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured["argv"] = args[0]
        captured["env"] = kwargs.get("env")
        return subprocess.CompletedProcess(args[0], 0)

    monkeypatch.setattr(subprocess, "run", _fake_run)

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
