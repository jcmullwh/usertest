from __future__ import annotations

from usertest_implement.cli import build_parser


def test_supervisor_instruction_is_repeatable_and_cli_only() -> None:
    parser = build_parser()
    args = parser.parse_args(
        [
            "run",
            "--ticket-path",
            "C:\\tmp\\ticket.md",
            "--dry-run",
            "--supervisor-instruction",
            "Do not invoke Docker.",
            "--supervisor-instruction",
            "Do not delete or move files.",
        ]
    )

    assert args.supervisor_instructions == [
        "Do not invoke Docker.",
        "Do not delete or move files.",
    ]

    defaults = parser.parse_args(
        ["run", "--ticket-path", "C:\\tmp\\ticket.md", "--dry-run"]
    )
    assert defaults.supervisor_instructions == []


def test_resume_supervisor_instruction_is_repeatable_and_commit_is_opt_in() -> None:
    parser = build_parser()
    args = parser.parse_args(
        [
            "resume",
            "--run-dir",
            "C:\\tmp\\run",
            "--supervisor-instruction",
            "Preserve the original no-Docker constraint.",
            "--supervisor-instruction",
            "Attempt a tracked-file write before claiming read-only.",
            "--runs-dir",
            "U:\\resumed-runs",
        ]
    )

    assert args.supervisor_instructions == [
        "Preserve the original no-Docker constraint.",
        "Attempt a tracked-file write before claiming read-only.",
    ]
    assert args.commit is False
    assert str(args.runs_dir) == "U:\\resumed-runs"
