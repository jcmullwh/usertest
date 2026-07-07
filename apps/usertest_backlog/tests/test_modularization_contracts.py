from __future__ import annotations

import ast
import importlib
from pathlib import Path

import pytest

from usertest_backlog.cli import build_parser


@pytest.mark.parametrize(
    ("module_name", "function_name"),
    [
        ("usertest_backlog.parser", "build_parser"),
        ("usertest_backlog.commands.atom_actions", "_cmd_reports_sync_atom_actions"),
        ("usertest_backlog.commands.dispatch", "dispatch"),
        ("usertest_backlog.commands.export_tickets", "_cmd_reports_export_tickets"),
        ("usertest_backlog.commands.plan_cleanup", "_cleanup_stale_ticket_idea_files"),
        ("usertest_backlog.commands.reports", "_cmd_reports_compile"),
        ("usertest_backlog.commands.review_ux", "_cmd_reports_review_ux"),
        ("usertest_backlog.commands.triage", "_cmd_triage_atoms"),
        (
            "usertest_backlog.workflows.implementation_planning",
            "_run_implementation_planning_stage",
        ),
        ("usertest_backlog.workflows.prioritization", "_run_problem_prioritization_stage"),
        ("usertest_backlog.workflows.problem_mining", "_run_problem_mining_stage"),
        ("usertest_backlog.workflows.reproduction_research", "_run_repro_research_stage"),
        ("usertest_backlog.workflows.solution_options", "_run_solution_optioning_stage"),
        ("usertest_backlog.workflows.solution_selection", "_run_solution_selection_stage"),
        ("usertest_backlog.workflows.staged", "_cmd_reports_backlog"),
    ],
)
def test_planned_backlog_module_boundaries_own_real_functions(
    module_name: str, function_name: str
) -> None:
    module = importlib.import_module(module_name)
    func = getattr(module, function_name)
    assert func.__module__ == module_name


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


def test_public_cli_entrypoint_stays_thin() -> None:
    cli_path = Path(__file__).parents[1] / "src" / "usertest_backlog" / "cli.py"
    source = cli_path.read_text(encoding="utf-8")

    assert len(source.splitlines()) <= 80
    assert "def _cmd_" not in source
    assert ".add_argument(" not in source
    assert ".add_parser(" not in source


@pytest.mark.parametrize(
    "module_name",
    [
        "commands.atom_actions",
        "commands.dispatch",
        "commands.export_tickets",
        "commands.plan_cleanup",
        "commands.reports",
        "commands.review_ux",
        "commands.triage",
        "workflows.implementation_planning",
        "workflows.prioritization",
        "workflows.problem_mining",
        "workflows.reproduction_research",
        "workflows.solution_options",
        "workflows.solution_selection",
        "workflows.staged",
    ],
)
def test_backlog_modules_do_not_import_public_cli_entrypoint(module_name: str) -> None:
    module_path = (
        Path(__file__).parents[1]
        / "src"
        / "usertest_backlog"
        / Path(*module_name.split(".")).with_suffix(".py")
    )

    tree = ast.parse(module_path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert all(alias.name != "usertest_backlog.cli" for alias in node.names)
        if isinstance(node, ast.ImportFrom):
            assert node.module != "usertest_backlog.cli"
