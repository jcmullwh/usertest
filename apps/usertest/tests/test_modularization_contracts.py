from __future__ import annotations

import ast
import importlib
from pathlib import Path

import pytest

from usertest.cli import build_parser

USERTEST_SRC = Path(__file__).resolve().parents[1] / "src" / "usertest"


@pytest.mark.parametrize(
    ("module_name", "registration_name"),
    [
        ("usertest.parser", "build_parser"),
        ("usertest.commands.batch", "add_batch_command"),
        ("usertest.commands.lint", "add_lint_command"),
        ("usertest.commands.matrix", "add_matrix_command"),
        ("usertest.commands.reports", "add_report_commands"),
        ("usertest.commands.run", "add_run_command"),
        ("usertest.commands.token_monitor", "add_token_monitor_command"),
    ],
)
def test_usertest_module_boundaries_have_parser_wiring(
    module_name: str, registration_name: str
) -> None:
    module = importlib.import_module(module_name)
    registration = getattr(module, registration_name)

    assert callable(registration)
    assert registration.__module__ == module_name


@pytest.mark.parametrize(
    ("argv", "snippets"),
    [
        (
            ["--help"],
            [
                "Run a single persona exploration against a target",
                "Run multiple targets from a YAML file.",
                "Generate and (optionally) run a matrix",
                "Lint missions/policies/catalog configuration",
                "Report history commands.",
                "Metadata-only token inefficiency monitoring commands.",
            ],
        ),
        (
            ["run", "--help"],
            [
                "--repo REPO",
                "--agent-append-system-prompt-file",
                "--preflight-command PREFLIGHT_COMMANDS",
                "--verify-command VERIFICATION_COMMANDS",
                "--exec-backend {local,docker}",
                "--exec-use-host-agent-login",
            ],
        ),
        (
            ["batch", "--help"],
            [
                "--targets TARGETS",
                "--print-requests",
                "--skip-command-probes",
                "Batch validation runs in phases:",
                "Catalog/policy/environment validation",
            ],
        ),
        (
            ["matrix", "--help"],
            [
                "{plan,run}",
                "Expand a matrix spec into batch targets",
                "Validate a matrix spec then execute all combinations.",
            ],
        ),
        (
            ["reports", "--help"],
            [
                "{compile,analyze}",
                "Compile report.json + metadata across runs",
                "Analyze run outcomes and cluster recurring issues",
            ],
        ),
        (
            ["token-monitor", "--help"],
            [
                "{analyze,batch-context,delegation-ab}",
                "Analyze one run directory and write token monitoring",
                "Analyze batch/control-plane context",
                "Compare delegation-disabled and delegation-enabled",
            ],
        ),
    ],
)
def test_usertest_command_group_help_contracts(
    argv: list[str], snippets: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    parser = build_parser()

    with pytest.raises(SystemExit) as excinfo:
        parser.parse_args(argv)

    assert excinfo.value.code == 0
    out = capsys.readouterr().out
    for snippet in snippets:
        assert snippet in out


def test_public_cli_entrypoint_stays_thin() -> None:
    source = (USERTEST_SRC / "cli.py").read_text(encoding="utf-8")

    assert len(source.splitlines()) <= 80
    assert "def _cmd_" not in source
    assert ".add_argument(" not in source
    assert ".add_parser(" not in source


def test_command_modules_do_not_import_public_cli_entrypoint() -> None:
    command_dir = USERTEST_SRC / "commands"
    for path in command_dir.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert node.module != "usertest.cli", path
            elif isinstance(node, ast.Import):
                assert all(alias.name != "usertest.cli" for alias in node.names), path
