"""Parser wiring for the ``usertest matrix`` command group."""

from __future__ import annotations

import argparse
from pathlib import Path

from usertest.cli import (
    _EXEC_CACHE_DIR_HELP,
    _EXEC_CACHE_HELP,
    _EXEC_NETWORK_HELP,
    _cmd_matrix_plan,
    _cmd_matrix_run,
)


def add_matrix_command(sub: argparse._SubParsersAction) -> None:
    matrix_p = sub.add_parser(
        "matrix",
        help=(
            "Generate and (optionally) run a matrix of persona/mission x agent/model combinations."
        ),
    )
    matrix_sub = matrix_p.add_subparsers(dest="matrix_cmd", required=True)

    matrix_plan_p = matrix_sub.add_parser(
        "plan",
        help="Expand a matrix spec into batch targets and validate (no execution).",
    )
    matrix_run_p = matrix_sub.add_parser(
        "run",
        help="Validate a matrix spec then execute all combinations.",
    )

    for p in (matrix_plan_p, matrix_run_p):
        p.add_argument(
            "--repo-root",
            type=Path,
            default=Path("."),
            help="Monorepo root (auto-detected when omitted).",
        )
        p.add_argument(
            "--spec",
            type=Path,
            required=True,
            help="Path to a YAML matrix spec.",
        )
        p.add_argument(
            "--out-targets",
            type=Path,
            help=(
                "Write expanded batch targets YAML here "
                "(default: runs/usertest/<target>/_compiled/<ts>.matrix.targets.yaml)."
            ),
        )
        p.add_argument(
            "--out-report",
            type=Path,
            help=("Write a JSON validation report (capabilities + requirements per combination)."),
        )
        p.add_argument(
            "--exec-backend",
            choices=["local", "docker"],
            default="docker",
            help=(
                "Execution backend (default: docker; affects tool availability, "
                "especially for gemini shell access)."
            ),
        )
        p.add_argument("--exec-docker-context", type=Path)
        p.add_argument("--exec-dockerfile", type=Path)
        p.add_argument("--exec-docker-python", default="auto")
        p.add_argument("--exec-docker-timeout-seconds", type=float, default=None)
        p.add_argument("--exec-use-target-sandbox-cli-install", action="store_true")
        p.add_argument("--exec-use-host-agent-login", action="store_true")
        p.add_argument(
            "--exec-network",
            choices=["open", "none"],
            default="open",
            help=_EXEC_NETWORK_HELP,
        )
        p.add_argument(
            "--exec-cache",
            choices=["cold", "warm"],
            default="cold",
            help=_EXEC_CACHE_HELP,
        )
        p.add_argument("--exec-cache-dir", type=Path, help=_EXEC_CACHE_DIR_HELP)
        p.add_argument(
            "--exec-env",
            action="append",
            default=[],
            help=(
                "Extra environment variable assignment(s) for sandbox execution "
                "(repeatable KEY=VALUE)."
            ),
        )
        p.add_argument("--exec-keep-container", action="store_true")
        p.add_argument("--exec-rebuild-image", action="store_true")

        p.add_argument(
            "--skip-command-probes",
            action="store_true",
            help="Skip local command responsiveness probes (faster, less validation).",
        )
        p.add_argument(
            "--command-probe-timeout-seconds",
            type=float,
            default=0.25,
            help="Timeout for each command responsiveness probe.",
        )

    matrix_plan_p.set_defaults(func=_cmd_matrix_plan)
    matrix_run_p.set_defaults(func=_cmd_matrix_run)


__all__ = ["add_matrix_command"]
