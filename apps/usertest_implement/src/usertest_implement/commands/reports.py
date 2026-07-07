# ruff: noqa: E501,F401,F403,F405
from __future__ import annotations

from usertest_implement.shared import *


def _cmd_reports_summarize(args: argparse.Namespace) -> int:
    repo_root = _resolve_repo_root(args.repo_root)
    cfg = _load_runner_config(repo_root)
    runs_dir = args.runs_dir.resolve() if args.runs_dir is not None else cfg.runs_dir
    out_path = (
        args.out.resolve()
        if args.out is not None
        else (runs_dir / "_compiled" / "implementation_metrics.jsonl")
    )
    rows = iter_implementation_rows(
        runs_dir,
        target_slug=args.target,
        repo_input=args.repo_input,
        test_command_regexes=list(args.test_command_regex or []) or None,
    )
    write_jsonl(rows, out_path)
    print(str(out_path))
    return 0




__all__ = [name for name in globals() if not name.startswith("__")]
