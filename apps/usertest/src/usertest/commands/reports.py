"""Parser wiring for report, catalog, and report-history commands."""

from __future__ import annotations

import argparse
from pathlib import Path

from usertest.cli import (
    _cmd_init_users,
    _cmd_missions_list,
    _cmd_personas_list,
    _cmd_report,
    _cmd_reports_analyze,
    _cmd_reports_compile,
)


def add_report_commands(sub: argparse._SubParsersAction) -> None:
    report_p = sub.add_parser("report", help="(Re)render report.md for an existing run dir.")
    report_p.add_argument("--run-dir", required=True, type=Path, help="Run directory to render.")
    report_p.add_argument(
        "--recompute-metrics",
        action="store_true",
        help=(
            "Overwrite normalized_events.jsonl and regenerate metrics.json from raw_events.jsonl. "
            "For reproducibility, an existing normalized_events.jsonl timestamp stream is "
            "reused when available."
        ),
    )
    report_p.add_argument(
        "--repo-root",
        type=Path,
        help="Path to monorepo root (auto-detected by default).",
    )

    init_p = sub.add_parser(
        "init-usertest",
        help="Initialize target .usertest/ scaffold (catalog.yaml + missions/personas dirs).",
    )
    init_p.add_argument("--repo", required=True, type=Path, help="Path to local target repo.")
    init_p.add_argument(
        "--force",
        action="store_true",
        help="Overwrite .usertest scaffold files if they already exist.",
    )
    init_p.add_argument(
        "--repo-root",
        type=Path,
        help="Path to monorepo root (auto-detected by default).",
    )

    personas_p = sub.add_parser("personas", help="Persona catalog commands.")
    personas_sub = personas_p.add_subparsers(dest="personas_cmd", required=True)
    personas_list_p = personas_sub.add_parser(
        "list",
        help="List discovered personas.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Repo selection (--repo):\n"
            "  - Local path: read in-place.\n"
            "  - Git URL: cloned to a temp dir.\n"
            "\n"
            "Examples:\n"
            "  usertest personas list --repo C:\\path\\to\\repo\n"
            "  usertest personas list --repo https://github.com/org/repo\n"
            "  usertest personas list --repo-root .\n"
        ),
    )
    personas_list_p.add_argument(
        "--repo",
        help=(
            "Optional target repo (local path or git URL) to load .usertest/catalog.yaml from "
            "(if present)."
        ),
    )
    personas_list_p.add_argument(
        "--repo-root",
        type=Path,
        help="Path to monorepo root (auto-detected by default).",
    )

    missions_p = sub.add_parser("missions", help="Mission catalog commands.")
    missions_sub = missions_p.add_subparsers(dest="missions_cmd", required=True)
    missions_list_p = missions_sub.add_parser(
        "list",
        help="List discovered missions.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Repo selection (--repo):\n"
            "  - Local path: read in-place.\n"
            "  - Git URL: cloned to a temp dir.\n"
            "\n"
            "Examples:\n"
            "  usertest missions list --repo C:\\path\\to\\repo\n"
            "  usertest missions list --repo https://github.com/org/repo\n"
            "  usertest missions list --repo-root .\n"
        ),
    )
    missions_list_p.add_argument(
        "--repo",
        help=(
            "Optional target repo (local path or git URL) to load .usertest/catalog.yaml from "
            "(if present)."
        ),
    )
    missions_list_p.add_argument(
        "--repo-root",
        type=Path,
        help="Path to monorepo root (auto-detected by default).",
    )

    reports_p = sub.add_parser("reports", help="Report history commands.")
    reports_sub = reports_p.add_subparsers(dest="reports_cmd", required=True)
    reports_compile_p = reports_sub.add_parser(
        "compile",
        help="Compile report.json + metadata across runs into a JSONL history file.",
    )
    reports_compile_p.add_argument(
        "--target",
        help="Optional target slug under runs/usertest (e.g. tiktok_vids).",
    )
    reports_compile_p.add_argument(
        "--repo-input",
        help="Optional match for target_ref.repo_input (path or git URL).",
    )
    reports_compile_p.add_argument(
        "--runs-dir",
        type=Path,
        help="Runs directory (defaults to <repo_root>/runs/usertest).",
    )
    reports_compile_p.add_argument(
        "--out",
        type=Path,
        help=(
            "Output JSONL path (defaults under runs/usertest/<target>/_compiled/ "
            "or runs/usertest/_compiled/ when --target is omitted)."
        ),
    )
    reports_compile_p.add_argument(
        "--embed",
        choices=["none", "definitions", "prompt", "all"],
        default="definitions",
        help=(
            "How much extra run context to embed (beyond JSON artifacts). "
            "none: only JSON; definitions: persona/mission/schema/template; "
            "prompt: + prompt.txt; all: + users.md."
        ),
    )
    reports_compile_p.add_argument(
        "--max-embed-bytes",
        type=int,
        default=200_000,
        help="Skip embedding any single text file larger than this many bytes.",
    )
    reports_compile_p.add_argument(
        "--repo-root",
        type=Path,
        help="Path to monorepo root (auto-detected by default).",
    )

    reports_analyze_p = reports_sub.add_parser(
        "analyze",
        help="Analyze run outcomes and cluster recurring issues from batch/historical runs.",
    )
    reports_analyze_p.add_argument(
        "--target",
        help="Optional target slug under runs/usertest (e.g. tiktok_vids).",
    )
    reports_analyze_p.add_argument(
        "--repo-input",
        help="Optional match for target_ref.repo_input (path or git URL).",
    )
    reports_analyze_p.add_argument(
        "--runs-dir",
        type=Path,
        help="Runs directory (defaults to <repo_root>/runs/usertest).",
    )
    reports_analyze_p.add_argument(
        "--history",
        type=Path,
        help="Path to a compiled report history JSONL (from `reports compile`).",
    )
    reports_analyze_p.add_argument(
        "--out-json",
        type=Path,
        help=(
            "Output analysis JSON path (defaults under runs/usertest/<target>/_compiled/ "
            "or runs/usertest/_compiled/ when --target is omitted)."
        ),
    )
    reports_analyze_p.add_argument(
        "--out-md",
        type=Path,
        help=("Output markdown summary path (defaults next to --out-json with .md extension)."),
    )
    reports_analyze_p.add_argument(
        "--actions",
        type=Path,
        help=(
            "Optional JSON action registry for addressed comments (date/plan metadata). "
            "Defaults to configs/issue_actions.json when present."
        ),
    )
    reports_analyze_p.add_argument(
        "--repo-root",
        type=Path,
        help="Path to monorepo root (auto-detected by default).",
    )

    report_p.set_defaults(func=_cmd_report)
    init_p.set_defaults(func=_cmd_init_users)
    personas_list_p.set_defaults(func=_cmd_personas_list)
    missions_list_p.set_defaults(func=_cmd_missions_list)
    reports_compile_p.set_defaults(func=_cmd_reports_compile)
    reports_analyze_p.set_defaults(func=_cmd_reports_analyze)


__all__ = ["add_report_commands"]
