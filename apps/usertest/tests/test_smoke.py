from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from usertest.cli import build_parser


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

    args = parser.parse_args(["report", "--run-dir", "runs\\x\\y\\codex\\0"])
    assert args.run_dir == Path("runs\\x\\y\\codex\\0")

    args = parser.parse_args(["smoke"])
    assert args.skip_install is False
    assert args.use_pythonpath is False
    assert args.require_doctor is False
    args = parser.parse_args(["smoke", "--skip-install", "--use-pythonpath", "--require-doctor"])
    assert args.skip_install is True
    assert args.use_pythonpath is True
    assert args.require_doctor is True

    args = parser.parse_args(["reports", "analyze", "--target", "x"])
    assert args.target == "x"
    args = parser.parse_args(
        [
            "reports",
            "analyze",
            "--target",
            "x",
            "--actions",
            "configs\\issue_actions.json",
        ]
    )
    assert args.actions == Path("configs\\issue_actions.json")
    with pytest.raises(SystemExit):
        parser.parse_args(["reports", "intent-snapshot", "--target", "x"])
    with pytest.raises(SystemExit):
        parser.parse_args(["reports", "review-ux", "--target", "x", "--dry-run"])
    with pytest.raises(SystemExit):
        parser.parse_args(["reports", "export-tickets", "--target", "x"])
    with pytest.raises(SystemExit):
        parser.parse_args(["reports", "backlog", "--target", "x", "--dry-run"])

    args = parser.parse_args(["batch", "--targets", "configs\\targets.yaml"])
    assert args.exec_use_host_agent_login is True
    args = parser.parse_args(
        ["batch", "--targets", "configs\\targets.yaml", "--exec-use-api-key-auth"]
    )
    assert args.exec_use_host_agent_login is False

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
