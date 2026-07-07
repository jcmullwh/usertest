"""Parser wiring for the ``usertest lint`` command."""

from __future__ import annotations

import argparse
from pathlib import Path

from usertest.cli import _cmd_lint


def add_lint_command(sub: argparse._SubParsersAction) -> None:
    lint_p = sub.add_parser(
        "lint",
        help="Lint missions/policies/catalog configuration (capability contract).",
    )
    lint_p.add_argument(
        "--repo-root",
        type=Path,
        help="Path to monorepo root (auto-detected by default).",
    )
    lint_p.add_argument(
        "--repo",
        help=(
            "Optional target repo input to lint catalog overrides (accepted forms: same syntax as "
            "`run --repo`). Local paths are read in-place; git URLs / pip:/pdm: "
            "inputs are acquired "
            "into a temp workspace."
        ),
    )
    lint_p.add_argument("--ref", help="Branch/tag/SHA to checkout when --repo is a git URL.")
    lint_p.add_argument(
        "--strict",
        action="store_true",
        help="Fail (non-zero exit) if any warnings are emitted.",
    )
    lint_p.add_argument(
        "--format",
        choices=["text", "json"],
        default="text",
        help="Console output format.",
    )
    lint_p.add_argument(
        "--out-json",
        type=Path,
        help="Write full lint report JSON to this path (optional).",
    )
    lint_p.set_defaults(func=_cmd_lint)


__all__ = ["add_lint_command"]
