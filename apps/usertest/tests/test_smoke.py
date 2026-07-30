from __future__ import annotations

import argparse
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from usertest.cli import build_parser

_DOCUMENTED_RUN_SECTIONS = (
    ("README.md", "## Run a single target", "## Backlog CLI", 2),
    (
        "docs/tutorials/getting-started.md",
        "### 4) Run",
        "### 5) Inspect the output",
        1,
    ),
    (
        "docs/how-to/run-usertest.md",
        "### Representative validation (default built-in path)",
        "### Faster preflight probe",
        2,
    ),
    (
        "apps/usertest/README.md",
        "### `usertest run`",
        "### `usertest batch`",
        1,
    ),
)
_RUN_COMMAND_RE = re.compile(
    r"```[^\r\n]*\r?\n(?P<fenced>.*?)```"
    r"|`(?P<inline>(?:python -m usertest\.cli|usertest) run [^`\r\n]+)`",
    re.DOTALL,
)


def _normalize_documented_command(command: str) -> str:
    parts = []
    for line in command.splitlines():
        part = line.strip()
        if part.endswith("\\"):
            part = part[:-1].rstrip()
        if part:
            parts.append(part)
    return " ".join(parts)


def _run_commands(markdown: str) -> list[str]:
    commands = []
    for match in _RUN_COMMAND_RE.finditer(markdown):
        command = _normalize_documented_command(match.group("fenced") or match.group("inline"))
        if command.startswith(("python -m usertest.cli run ", "usertest run ")):
            commands.append(command)
    return commands


def _documented_command_argv(command: str) -> list[str]:
    tokens = shlex.split(command, posix=True)
    if tokens[:3] == ["python", "-m", "usertest.cli"]:
        return tokens[3:]
    if tokens[:1] == ["usertest"]:
        return tokens[1:]
    raise AssertionError(f"unsupported documented command prefix: {command}")


def _assert_maintained_codex_commands(parser: argparse.ArgumentParser) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    maintained_count = 0
    for relative_path, start_heading, end_heading, expected_count in _DOCUMENTED_RUN_SECTIONS:
        document = (repo_root / relative_path).read_text(encoding="utf-8")
        assert start_heading in document
        section = document.split(start_heading, 1)[1]
        assert end_heading in section
        section = section.split(end_heading, 1)[0]

        commands = [
            command
            for command in _run_commands(section)
            if "--agent codex" in command
            and "--policy write" in command
            and "--exec-backend local" in command
        ]
        assert len(commands) == expected_count, relative_path
        for command in commands:
            args = parser.parse_args(_documented_command_argv(command))
            assert args.exec_backend == "local", (relative_path, command)
        maintained_count += len(commands)

        docker_commands = [
            command
            for command in _run_commands(document)
            if "--agent codex" in command and "--exec-backend docker" in command
        ]
        assert docker_commands, f"{relative_path} must retain an explicit Docker workflow"

    assert maintained_count == 6


def test_parser_smoke() -> None:
    parser = build_parser()
    args = parser.parse_args(["run", "--repo", "C:\\tmp\\x"])
    assert args.repo == "C:\\tmp\\x"

    args = parser.parse_args(["run", "--repo", "C:\\tmp\\x", "--obfuscate-agent-docs"])
    assert args.obfuscate_agent_docs is True

    args = parser.parse_args(
        [
            "run",
            "--repo",
            "C:\\tmp\\x",
            "--preflight-command",
            "ffmpeg",
            "--preflight-command",
            "ffprobe",
        ]
    )
    assert args.preflight_commands == ["ffmpeg", "ffprobe"]

    args = parser.parse_args(
        [
            "run",
            "--repo",
            "C:\\tmp\\x",
            "--require-preflight-command",
            "python",
        ]
    )
    assert args.preflight_required_commands == ["python"]

    args = parser.parse_args(
        [
            "run",
            "--repo",
            "C:\\tmp\\x",
            "--exec-backend",
            "docker",
            "--exec-use-target-sandbox-cli-install",
        ]
    )
    assert args.exec_use_target_sandbox_cli_install is True
    args = parser.parse_args(["run", "--repo", "C:\\tmp\\x"])
    assert args.exec_backend == "docker"
    _assert_maintained_codex_commands(parser)
    assert args.exec_use_host_agent_login is True
    args = parser.parse_args(
        [
            "run",
            "--repo",
            "C:\\tmp\\x",
            "--exec-backend",
            "docker",
            "--exec-use-host-agent-login",
        ]
    )
    assert args.exec_use_host_agent_login is True
    args = parser.parse_args(
        [
            "run",
            "--repo",
            "C:\\tmp\\x",
            "--exec-use-api-key-auth",
        ]
    )
    assert args.exec_use_host_agent_login is False

    args = parser.parse_args(["report", "--run-dir", "runs/x/y/codex/0"])
    assert args.run_dir == Path("runs/x/y/codex/0")

    args = parser.parse_args(["reports", "analyze", "--target", "x"])
    assert args.target == "x"
    args = parser.parse_args(["token-monitor", "analyze", "--run-dir", "runs/x/y/codex/0"])
    assert args.token_monitor_cmd == "analyze"
    args = parser.parse_args(["token-monitor", "batch-context", "--batch-dir", "runs/_batch/x/y"])
    assert args.token_monitor_cmd == "batch-context"
    args = parser.parse_args(
        [
            "reports",
            "analyze",
            "--target",
            "x",
            "--actions",
            "configs/issue_actions.json",
        ]
    )
    assert args.actions == Path("configs/issue_actions.json")
    with pytest.raises(SystemExit):
        parser.parse_args(["reports", "intent-snapshot", "--target", "x"])
    with pytest.raises(SystemExit):
        parser.parse_args(["reports", "review-ux", "--target", "x", "--dry-run"])
    with pytest.raises(SystemExit):
        parser.parse_args(["reports", "export-tickets", "--target", "x"])
    with pytest.raises(SystemExit):
        parser.parse_args(["reports", "backlog", "--target", "x", "--dry-run"])

    args = parser.parse_args(["batch", "--targets", "configs/targets.yaml"])
    assert args.exec_backend == "docker"
    assert args.exec_use_host_agent_login is True
    args = parser.parse_args(
        ["batch", "--targets", "configs/targets.yaml", "--exec-use-api-key-auth"]
    )
    assert args.exec_use_host_agent_login is False

    args = parser.parse_args(["matrix", "plan", "--spec", "configs/matrix.yaml"])
    assert args.exec_backend == "docker"

    args = parser.parse_args(["init-usertest", "--repo", "C:\\tmp\\x"])
    assert args.repo == Path("C:\\tmp\\x")
    with pytest.raises(SystemExit):
        parser.parse_args(["init-users", "--repo", "C:\\tmp\\x"])
    with pytest.raises(SystemExit):
        parser.parse_args(["run", "--repo", "C:\\tmp\\x", "--use-builtin-context"])


def test_scaffold_doctor_skip_tool_checks_allows_missing_binaries_on_path() -> None:
    repo_root = Path(__file__).resolve().parents[3]
    scaffold_py = repo_root / "tools" / "scaffold" / "scaffold.py"
    assert scaffold_py.exists()

    env = dict(os.environ)
    env["PATH"] = ""

    without_flag = subprocess.run(
        [sys.executable, str(scaffold_py), "doctor"],
        cwd=str(repo_root),
        env=env,
        capture_output=True,
        text=True,
    )
    assert without_flag.returncode != 0

    with_flag = subprocess.run(
        [sys.executable, str(scaffold_py), "doctor", "--skip-tool-checks"],
        cwd=str(repo_root),
        env=env,
        capture_output=True,
        text=True,
    )
    assert with_flag.returncode == 0, with_flag.stderr or with_flag.stdout
