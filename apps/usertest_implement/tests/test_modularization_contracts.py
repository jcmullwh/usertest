from __future__ import annotations

import importlib

import pytest

from usertest_implement.cli import build_parser


@pytest.mark.parametrize(
    "module_name",
    [
        "usertest_implement.ci",
        "usertest_implement.parser",
        "usertest_implement.review_context",
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
    ("argv", "snippets"),
    [
        (
            ["--help"],
            [
                "{run,review,maintenance-images,reports,tickets,batch}",
                "Run one ticket implementation.",
                "Review and merge PR-backed implementation tickets.",
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
                "{run,status,merge}",
                "Run an implementation review for a PR-backed ticket.",
                "Show the latest review summary for a ticket.",
                "Merge a reviewed PR when review + CI are green.",
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
                "Prune old local maintenance-image tags",
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
