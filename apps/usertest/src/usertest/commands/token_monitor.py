# ruff: noqa: E501
from __future__ import annotations

import argparse
import json
from pathlib import Path

from token_monitoring import analyze_run as analyze_token_run
from token_monitoring import public_analysis_payload, write_batch_context, write_run_monitoring

from usertest.commands.shared import _resolve_optional_path, _resolve_repo_root


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

def _cmd_token_monitor_analyze(args: argparse.Namespace) -> int:
    """Execute token-monitor analyze for a run directory."""
    repo_root = _resolve_repo_root(args.repo_root)
    run_dir = _resolve_optional_path(repo_root, args.run_dir) or args.run_dir.resolve()
    sessions_root = (
        (
            _resolve_optional_path(repo_root, args.codex_sessions_root)
            or args.codex_sessions_root.resolve()
        )
        if args.codex_sessions_root is not None
        else None
    )
    output_dir = (
        (_resolve_optional_path(repo_root, args.output_dir) or args.output_dir.resolve())
        if args.output_dir is not None
        else None
    )

    if args.no_write:
        analysis = analyze_token_run(run_dir, codex_sessions_root=sessions_root)
        print(json.dumps(public_analysis_payload(analysis), indent=2, ensure_ascii=False))
        return 0

    write_run_monitoring(run_dir, codex_sessions_root=sessions_root, output_dir=output_dir)
    destination = output_dir or run_dir
    print(str(destination / "token_monitoring.json"))
    print(str(destination / "token_monitoring.md"))
    print(str(destination / "token_causal_trace.jsonl"))
    return 0

def _cmd_token_monitor_batch_context(args: argparse.Namespace) -> int:
    """Execute token-monitor batch-context for a batch directory."""
    repo_root = _resolve_repo_root(args.repo_root)
    batch_dir = _resolve_optional_path(repo_root, args.batch_dir) or args.batch_dir.resolve()
    output_dir = (
        (_resolve_optional_path(repo_root, args.output_dir) or args.output_dir.resolve())
        if args.output_dir is not None
        else None
    )
    write_batch_context(batch_dir, output_dir=output_dir)
    destination = output_dir or batch_dir
    print(str(destination / "token_batch_context.json"))
    print(str(destination / "token_batch_context.md"))
    return 0

__all__ = ['add_token_monitor_command', '_cmd_token_monitor_analyze', '_cmd_token_monitor_batch_context']
