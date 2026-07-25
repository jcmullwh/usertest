from __future__ import annotations

import argparse

from usertest_backlog.commands.atom_actions import _update_atom_actions_from_backlog
from usertest_backlog.commands.plan_cleanup import _cleanup_stale_ticket_idea_files
from usertest_backlog.shared import _configure_console_output, resolve_embedder
from usertest_backlog.workflows.problem_mining import _write_chunked_problem_mining_atoms_workspace

_configure_console_output()


def build_parser() -> argparse.ArgumentParser:
    from usertest_backlog.parser import build_parser as _build_parser

    return _build_parser()


def main(argv: list[str] | None = None) -> None:
    from usertest_backlog.commands.dispatch import dispatch

    parser = build_parser()
    args = parser.parse_args(argv)
    raise SystemExit(dispatch(args))


__all__ = [
    "_cleanup_stale_ticket_idea_files",
    "_update_atom_actions_from_backlog",
    "_write_chunked_problem_mining_atoms_workspace",
    "build_parser",
    "main",
    "resolve_embedder",
]


if __name__ == "__main__":
    main()
