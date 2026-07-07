"""Parser wiring for the ``usertest token-monitor`` command group."""

from __future__ import annotations

import argparse
from pathlib import Path

from usertest.cli import _cmd_token_monitor_analyze, _cmd_token_monitor_batch_context


def add_token_monitor_command(sub: argparse._SubParsersAction) -> None:
    token_monitor_p = sub.add_parser(
        "token-monitor",
        help="Metadata-only token inefficiency monitoring commands.",
    )
    token_monitor_sub = token_monitor_p.add_subparsers(dest="token_monitor_cmd", required=True)
    token_monitor_analyze_p = token_monitor_sub.add_parser(
        "analyze",
        help="Analyze one run directory and write token monitoring artifacts.",
    )
    token_monitor_analyze_p.add_argument(
        "--run-dir",
        required=True,
        type=Path,
        help="Run directory to analyze.",
    )
    token_monitor_analyze_p.add_argument(
        "--codex-sessions-root",
        type=Path,
        help="Override Codex sessions root (defaults to CODEX_HOME/sessions or ~/.codex/sessions).",
    )
    token_monitor_analyze_p.add_argument(
        "--output-dir",
        type=Path,
        help="Output directory for derived artifacts (defaults to --run-dir).",
    )
    token_monitor_analyze_p.add_argument(
        "--no-write",
        action="store_true",
        help="Print metadata-only analysis JSON without writing artifacts.",
    )
    token_monitor_analyze_p.add_argument(
        "--repo-root",
        type=Path,
        help="Path to monorepo root (auto-detected by default).",
    )

    token_monitor_batch_p = token_monitor_sub.add_parser(
        "batch-context",
        help="Analyze batch/control-plane context without assigning completed-run tokens.",
    )
    token_monitor_batch_p.add_argument(
        "--batch-dir",
        required=True,
        type=Path,
        help="Batch directory to analyze.",
    )
    token_monitor_batch_p.add_argument(
        "--output-dir",
        type=Path,
        help="Output directory for derived artifacts (defaults to --batch-dir).",
    )
    token_monitor_batch_p.add_argument(
        "--repo-root",
        type=Path,
        help="Path to monorepo root (auto-detected by default).",
    )

    token_monitor_analyze_p.set_defaults(func=_cmd_token_monitor_analyze)
    token_monitor_batch_p.set_defaults(func=_cmd_token_monitor_batch_context)


__all__ = ["add_token_monitor_command"]
