from __future__ import annotations

import ast
import importlib
from pathlib import Path

import pytest

from usertest_implement.cli import build_parser


@pytest.mark.parametrize(
    "module_name",
    [
        "usertest_implement.shared",
        "usertest_implement.ci",
        "usertest_implement.parser",
        "usertest_implement.review_context",
        "usertest_implement.selection",
        "usertest_implement.settings",
        "usertest_implement.commands.maintenance_images",
        "usertest_implement.commands.reports",
        "usertest_implement.commands.review",
        "usertest_implement.commands.run",
        "usertest_implement.commands.tickets",
    ],
)
def test_planned_implement_module_boundaries_import(module_name: str) -> None:
    assert importlib.import_module(module_name).__name__ == module_name


@pytest.mark.parametrize(
    ("module_name", "function_name"),
    [
        ("usertest_implement.settings", "_apply_cli_settings"),
        ("usertest_implement.ci", "_wait_for_ci_success"),
        ("usertest_implement.review_context", "_collect_pr_review_context"),
        ("usertest_implement.selection", "_select_ticket_from_owner_root"),
        ("usertest_implement.parser", "build_parser"),
        ("usertest_implement.commands.run", "_cmd_run"),
        ("usertest_implement.commands.review", "_cmd_review_run"),
        ("usertest_implement.commands.tickets", "_cmd_tickets_discard"),
        ("usertest_implement.commands.maintenance_images", "_cmd_maintenance_images_list"),
        ("usertest_implement.commands.reports", "_cmd_reports_summarize"),
    ],
)
def test_implement_cli_behaviors_live_in_extracted_modules(
    module_name: str,
    function_name: str,
) -> None:
    module = importlib.import_module(module_name)

    assert callable(getattr(module, function_name))


def test_cli_facade_stays_thin() -> None:
    cli_path = Path(__file__).resolve().parents[1] / "src" / "usertest_implement" / "cli.py"
    source = cli_path.read_text(encoding="utf-8")

    assert len(source.splitlines()) <= 90
    assert "def _cmd_" not in source
    assert ".add_parser(" not in source
    assert ".add_argument(" not in source


@pytest.mark.parametrize(
    "module_relpath",
    [
        "ci.py",
        "parser.py",
        "review_context.py",
        "selection.py",
        "settings.py",
        "commands/maintenance_images.py",
        "commands/reports.py",
        "commands/review.py",
        "commands/run.py",
        "commands/tickets.py",
    ],
)
def test_extracted_modules_do_not_import_cli_facade(module_relpath: str) -> None:
    module_path = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "usertest_implement"
        / module_relpath
    )
    tree = ast.parse(module_path.read_text(encoding="utf-8"), filename=str(module_path))

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert node.module != "usertest_implement.cli"
        elif isinstance(node, ast.Import):
            assert all(alias.name != "usertest_implement.cli" for alias in node.names)


@pytest.mark.parametrize(
    ("argv", "snippets"),
    [
        (
            ["--help"],
            [
                    "{run,resume,handoff,review,outcome,maintenance-images,reports,tickets,batch}",
                "Run one ticket implementation.",
                "Resume a verification-failed implementation run from",
                    "Review and merge PR-backed implementation tickets.",
                    "Advance evidence-backed implementation outcomes",
                "Inspect and prune local maintenance-image tags.",
                "Local ticket queue helpers",
                "Run and inspect maintenance implementation batches.",
            ],
        ),
        (
            ["run", "--help"],
            [
                "--ticket-path TICKET_PATH",
                "--base-branch BASE_BRANCH",
                "--exec-backend {docker,local}",
                "--no-draft-pr-on-ci-failure",
                "--ci-timeout-seconds CI_TIMEOUT_SECONDS",
            ],
        ),
        (
            ["review", "--help"],
            [
                    "{run,adopt-run,status,merge}",
                "Run an implementation review for a PR-backed ticket.",
                "Show the latest review summary for a ticket.",
                "Merge a reviewed PR when review + CI are green.",
            ],
        ),
            (
                ["outcome", "--help"],
                [
                    "{bind-verification-amendment,run-role,advance}",
                    "Atomically advance the outcome embedded in a completed",
                ],
            ),
            (
                ["tickets", "--help"],
            [
                "{list,next,run-next,move,discard}",
                "List tickets in .agents/plans.",
                "Select the next ticket by bucket priority.",
                "Refresh the backlog + ticket exports",
                "Move a generated ticket to the non-actioned discarded",
            ],
        ),
        (
            ["maintenance-images", "--help"],
            [
                "{list,cleanup}",
                "List local maintenance-image tags retained on the Docker",
                "Prune local maintenance-image identities",
            ],
        ),
    ],
)
def test_implement_command_group_help_contracts(
    argv: list[str], snippets: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    parser = build_parser()

    with pytest.raises(SystemExit) as excinfo:
        parser.parse_args(argv)

    assert excinfo.value.code == 0
    out = capsys.readouterr().out
    for snippet in snippets:
        assert snippet in out
