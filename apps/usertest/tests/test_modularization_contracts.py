from __future__ import annotations

import importlib

import pytest

from usertest.cli import build_parser


@pytest.mark.parametrize(
    "module_name",
    [
        "usertest.parser",
        "usertest.commands.batch",
        "usertest.commands.lint",
        "usertest.commands.matrix",
        "usertest.commands.reports",
        "usertest.commands.run",
        "usertest.commands.token_monitor",
    ],
)
def test_planned_usertest_module_boundaries_import(module_name: str) -> None:
    assert importlib.import_module(module_name).__name__ == module_name


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
                "{analyze,batch-context}",
                "Analyze one run directory and write token monitoring",
                "Analyze batch/control-plane context",
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
