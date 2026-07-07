"""Parser construction for the public ``usertest`` CLI."""

from __future__ import annotations

import argparse

from usertest.commands.batch import add_batch_command
from usertest.commands.lint import add_lint_command
from usertest.commands.matrix import add_matrix_command
from usertest.commands.reports import add_report_commands
from usertest.commands.run import add_run_command
from usertest.commands.token_monitor import add_token_monitor_command


def build_parser() -> argparse.ArgumentParser:
    """Build the usertest CLI argument parser."""
    parser = argparse.ArgumentParser(
        prog="usertest",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  # Discover valid persona/mission IDs from this repo:\n"
            "  python -m usertest.cli personas list --repo-root .\n"
            "  python -m usertest.cli missions list --repo-root .\n"
        ),
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    add_run_command(sub)
    add_batch_command(sub)
    add_matrix_command(sub)
    add_lint_command(sub)
    add_report_commands(sub)
    add_token_monitor_command(sub)
    return parser


__all__ = ["build_parser"]
