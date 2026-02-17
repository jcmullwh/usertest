from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from reporter import analyze_report_history, write_issue_analysis
from run_artifacts.history import iter_report_history


def _utc_now() -> str:
    return datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_run_dir(stdout_text: str) -> str | None:
    lines = [line.strip() for line in stdout_text.splitlines() if line.strip()]
    if not lines:
        return None
    return lines[0]


def _load_json(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


def _write_comparison_md(path: Path, payload: dict[str, Any]) -> None:
    runs = payload.get("runs")
    if not isinstance(runs, list):
        runs = []
    lines: list[str] = []
    lines.append(
        f"# Iteration {payload.get('iteration')} Comparison ({payload.get('persona_id')} + {payload.get('mission_id')})"
    )
    lines.append("")
    lines.append(f"- Generated: `{payload.get('generated_at_utc')}`")
    lines.append(f"- Policy: `{payload.get('policy')}`")
    lines.append(
        "- Agent retry config: "
        f"`retries={payload.get('agent_rate_limit_retries')}`, "
        f"`backoff={payload.get('agent_rate_limit_backoff_seconds')}`, "
        f"`multiplier={payload.get('agent_rate_limit_backoff_multiplier')}`"
    )
    lines.append("")
    lines.append("| Agent | Exit | Run Dir | Error Type | Error Subtype |")
    lines.append("|---|---:|---|---|---|")
    for item in runs:
        if not isinstance(item, dict):
            continue
        lines.append(
            "| "
            f"{item.get('agent', '')} | "
            f"{item.get('exit_code', '')} | "
            f"`{item.get('run_dir', '')}` | "
            f"{item.get('error_type', '')} | "
            f"{item.get('error_subtype', '')} |"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _analyze_selected_runs(
    repo_root: Path,
    selected_run_dirs: set[str],
    issue_actions_path: Path | None,
) -> dict[str, Any]:
    records: list[dict[str, Any]] = []
    for record in iter_report_history(
        repo_root / "runs" / "usertest",
        target_slug=None,
        repo_input=None,
        embed="none",
    ):
        run_dir = record.get("run_dir")
        if isinstance(run_dir, str) and str(Path(run_dir).resolve()) in selected_run_dirs:
            records.append(record)
    return analyze_report_history(
        records,
        repo_root=repo_root,
        issue_actions_path=issue_actions_path,
    )


def _summarize_adjustment_hints(summary: dict[str, Any]) -> dict[str, Any]:
    themes_raw = summary.get("themes")
    themes = themes_raw if isinstance(themes_raw, list) else []
    top_theme_id = None
    top_theme_mentions = 0
    provider_capacity_mentions = 0
    execution_permissions_mentions = 0
    for item in themes:
        if not isinstance(item, dict):
            continue
        theme_id = item.get("theme_id")
        mentions_raw = item.get("mentions")
        mentions = int(mentions_raw) if isinstance(mentions_raw, int) else 0
        if mentions > top_theme_mentions:
            top_theme_mentions = mentions
            top_theme_id = str(theme_id) if isinstance(theme_id, str) else None
        if theme_id == "provider_capacity":
            provider_capacity_mentions += mentions
        if theme_id == "execution_permissions":
            execution_permissions_mentions += mentions
    return {
        "top_theme_id": top_theme_id,
        "top_theme_mentions": top_theme_mentions,
        "provider_capacity_mentions": provider_capacity_mentions,
        "execution_permissions_mentions": execution_permissions_mentions,
    }


def run_iteration(args: argparse.Namespace) -> int:
    repo_root = args.repo_root.resolve()
    compiled_dir = repo_root / "runs" / "usertest" / "target" / "_compiled"
    compiled_dir.mkdir(parents=True, exist_ok=True)

    runs: list[dict[str, Any]] = []
    py = sys.executable
    procs: list[tuple[str, list[str], subprocess.Popen[str]]] = []
    for agent in args.agents:
        cmd = [
            py,
            "-m",
            "usertest.cli",
            "run",
            "--repo-root",
            str(repo_root),
            "--repo",
            str(repo_root),
            "--agent",
            agent,
            "--policy",
            args.policy,
            "--persona-id",
            args.persona_id,
            "--mission-id",
            args.mission_id,
            "--agent-rate-limit-retries",
            str(args.agent_rate_limit_retries),
            "--agent-rate-limit-backoff-seconds",
            str(args.agent_rate_limit_backoff_seconds),
            "--agent-rate-limit-backoff-multiplier",
            str(args.agent_rate_limit_backoff_multiplier),
        ]
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        procs.append((agent, cmd, proc))

    for agent, _cmd, proc in procs:
        stdout_text, stderr_text = proc.communicate()
        run_dir = _parse_run_dir(stdout_text)
        error_type = None
        error_subtype = None
        if isinstance(run_dir, str):
            error_obj = _load_json(Path(run_dir) / "error.json")
            if isinstance(error_obj, dict):
                error_type_raw = error_obj.get("type")
                error_subtype_raw = error_obj.get("subtype")
                if isinstance(error_type_raw, str):
                    error_type = error_type_raw
                if isinstance(error_subtype_raw, str):
                    error_subtype = error_subtype_raw
        runs.append(
            {
                "agent": agent,
                "exit_code": int(proc.returncode),
                "run_dir": run_dir,
                "stdout": stdout_text,
                "stderr": stderr_text,
                "error_type": error_type,
                "error_subtype": error_subtype,
            }
        )

    selected_run_dirs = {
        str(Path(item["run_dir"]).resolve())
        for item in runs
        if isinstance(item.get("run_dir"), str) and str(item.get("run_dir")).strip()
    }

    issue_actions_path = args.issue_actions.resolve() if args.issue_actions else None
    summary = _analyze_selected_runs(
        repo_root=repo_root,
        selected_run_dirs=selected_run_dirs,
        issue_actions_path=issue_actions_path,
    )
    hints = _summarize_adjustment_hints(summary)

    base = (
        f"20260208.iter{args.iteration:02d}."
        f"{args.persona_id}.{args.mission_id}"
    )
    comparison_json = compiled_dir / f"{base}.comparison.json"
    comparison_md = compiled_dir / f"{base}.comparison.md"
    analysis_json = compiled_dir / f"{base}.issue_analysis.json"
    analysis_md = compiled_dir / f"{base}.issue_analysis.md"
    adjustment_json = compiled_dir / f"{base}.adjustment.json"

    comparison_payload: dict[str, Any] = {
        "generated_at_utc": _utc_now(),
        "iteration": args.iteration,
        "persona_id": args.persona_id,
        "mission_id": args.mission_id,
        "policy": args.policy,
        "agent_rate_limit_retries": args.agent_rate_limit_retries,
        "agent_rate_limit_backoff_seconds": args.agent_rate_limit_backoff_seconds,
        "agent_rate_limit_backoff_multiplier": args.agent_rate_limit_backoff_multiplier,
        "runs": runs,
        "selected_run_dirs": sorted(selected_run_dirs),
    }
    comparison_json.write_text(
        json.dumps(comparison_payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    _write_comparison_md(comparison_md, comparison_payload)

    write_issue_analysis(
        summary,
        out_json_path=analysis_json,
        out_md_path=analysis_md,
        title=(
            f"Iteration {args.iteration} Issue Analysis "
            f"({args.persona_id} + {args.mission_id})"
        ),
    )

    adjustment_payload = {
        "generated_at_utc": _utc_now(),
        "iteration": args.iteration,
        "persona_id": args.persona_id,
        "mission_id": args.mission_id,
        "current_policy": args.policy,
        "current_agent_rate_limit_retries": args.agent_rate_limit_retries,
        "current_agent_rate_limit_backoff_seconds": args.agent_rate_limit_backoff_seconds,
        "current_agent_rate_limit_backoff_multiplier": args.agent_rate_limit_backoff_multiplier,
        "signals": hints,
        "suggested_next": {
            "policy": (
                "inspect"
                if hints["execution_permissions_mentions"] > 0 and args.policy == "safe"
                else args.policy
            ),
            "agent_rate_limit_retries": (
                min(5, args.agent_rate_limit_retries + 1)
                if hints["provider_capacity_mentions"] > 0
                else args.agent_rate_limit_retries
            ),
            "agent_rate_limit_backoff_seconds": (
                min(4.0, round(args.agent_rate_limit_backoff_seconds * 1.5, 3))
                if hints["provider_capacity_mentions"] > 0
                else args.agent_rate_limit_backoff_seconds
            ),
            "agent_rate_limit_backoff_multiplier": args.agent_rate_limit_backoff_multiplier,
        },
        "artifacts": {
            "comparison_json": str(comparison_json),
            "comparison_md": str(comparison_md),
            "analysis_json": str(analysis_json),
            "analysis_md": str(analysis_md),
        },
    }
    adjustment_json.write_text(
        json.dumps(adjustment_payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    print(str(comparison_json))
    print(str(comparison_md))
    print(str(analysis_json))
    print(str(analysis_md))
    print(str(adjustment_json))
    print(json.dumps(summary.get("totals", {}), indent=2, ensure_ascii=False))
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run one usertest iteration and analyze results.")
    parser.add_argument("--repo-root", type=Path, default=Path("."))
    parser.add_argument("--iteration", type=int, required=True)
    parser.add_argument("--persona-id", required=True)
    parser.add_argument("--mission-id", required=True)
    parser.add_argument("--policy", default="safe")
    parser.add_argument(
        "--agents",
        nargs="+",
        default=["codex", "gemini", "claude"],
    )
    parser.add_argument("--agent-rate-limit-retries", type=int, default=2)
    parser.add_argument("--agent-rate-limit-backoff-seconds", type=float, default=1.0)
    parser.add_argument("--agent-rate-limit-backoff-multiplier", type=float, default=2.0)
    parser.add_argument("--issue-actions", type=Path, default=Path("configs/issue_actions.json"))
    return parser


def main() -> None:
    args = _build_parser().parse_args()
    raise SystemExit(run_iteration(args))


if __name__ == "__main__":
    main()
