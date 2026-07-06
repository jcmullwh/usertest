from __future__ import annotations

import importlib

import pytest

from usertest_backlog.cli import build_parser


@pytest.mark.parametrize(
    "module_name",
    [
        "usertest_backlog.parser",
        "usertest_backlog.commands.atom_actions",
        "usertest_backlog.commands.export_tickets",
        "usertest_backlog.commands.plan_cleanup",
        "usertest_backlog.commands.reports",
        "usertest_backlog.commands.review_ux",
        "usertest_backlog.workflows.implementation_planning",
        "usertest_backlog.workflows.prioritization",
        "usertest_backlog.workflows.problem_mining",
        "usertest_backlog.workflows.reproduction_research",
        "usertest_backlog.workflows.solution_options",
        "usertest_backlog.workflows.solution_selection",
        "usertest_backlog.workflows.staged",
    ],
)
def test_planned_backlog_module_boundaries_import(module_name: str) -> None:
    assert importlib.import_module(module_name).__name__ == module_name


@pytest.mark.parametrize(
    ("argv", "snippets"),
    [
        (
            ["--help"],
            [
                "{reports,triage-prs,triage-backlog,triage-atoms}",
                "Report history commands.",
                "Cluster existing pull requests",
                "Cluster issue-like backlog items",
                "Cluster backlog atoms",
            ],
        ),
        (
            ["reports", "--help"],
            [
                "compile",
                "analyze",
                "window",
                "backlog",
                "intent-snapshot",
                "review-ux",
                "sync-atom-actions",
                "export-tickets",
                "Export staged backlog items as external ticket",
            ],
        ),
        (
            ["triage-atoms", "--help"],
            [
                "--in ATOMS_JSONL",
                "--backlog-json BACKLOG_JSON",
                "--implementation-root IMPLEMENTATION_ROOT",
                "--out-json OUT_JSON",
                "--out-md OUT_MD",
            ],
        ),
    ],
)
def test_backlog_command_group_help_contracts(
    argv: list[str], snippets: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    parser = build_parser()

    with pytest.raises(SystemExit) as excinfo:
        parser.parse_args(argv)

    assert excinfo.value.code == 0
    out = capsys.readouterr().out
    for snippet in snippets:
        assert snippet in out
