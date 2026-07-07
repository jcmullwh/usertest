#!/usr/bin/env python
from __future__ import annotations

import argparse
import sys

from usertest_implement.ci import (
    _git_head_sha,
    _optional_timeout_seconds,
    _wait_for_ci_success,
)
from usertest_implement.commands.review import _cmd_review_merge, _cmd_review_run
from usertest_implement.parser import build_parser as _build_parser
from usertest_implement.review_context import (
    _build_final_review_summary,
    _build_pr_review_body,
    _collect_pr_review_context,
    _run_gh_json,
    _run_gh_text,
)
from usertest_implement.selection import _resolve_default_branch_name, _should_move_ticket_to_review
from usertest_implement.settings import _apply_cli_settings
from usertest_implement.shared import SelectedTicket, _configure_console_output, _read_json

_configure_console_output()


def build_parser() -> argparse.ArgumentParser:
    return _build_parser()


def main(argv: list[str] | None = None) -> None:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    args = parser.parse_args(raw_argv)
    _apply_cli_settings(args=args, parser=parser, argv=raw_argv)
    func = getattr(args, "func", None)
    if callable(func):
        raise SystemExit(func(args))
    raise SystemExit(2)


__all__ = [
    "SelectedTicket",
    "_build_final_review_summary",
    "_build_pr_review_body",
    "_cmd_review_merge",
    "_cmd_review_run",
    "_collect_pr_review_context",
    "_git_head_sha",
    "_optional_timeout_seconds",
    "_read_json",
    "_resolve_default_branch_name",
    "_run_gh_json",
    "_run_gh_text",
    "_should_move_ticket_to_review",
    "_wait_for_ci_success",
    "_apply_cli_settings",
    "build_parser",
    "main",
]


if __name__ == "__main__":
    main()
