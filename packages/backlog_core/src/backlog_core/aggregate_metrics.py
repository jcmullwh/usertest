from __future__ import annotations

import json
from collections import Counter, defaultdict
from collections.abc import Iterable
from pathlib import Path
from typing import Any

_MAX_TOP_FAILED_COMMANDS = 5
_MAX_FAILED_COMMANDS_PER_RUN = 25


def _coerce_string(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned if cleaned else None


def _command_head(command: str) -> str | None:
    cleaned = command.strip()
    if not cleaned:
        return None
    if cleaned[0] in {'"', "'"}:
        quote = cleaned[0]
        end = cleaned.find(quote, 1)
        if end > 1:
            return cleaned[1:end]
    parts = cleaned.split()
    return parts[0] if parts else None


def _is_ripgrep_no_matches(*, command: str, exit_code: int) -> bool:
    if exit_code != 1:
        return False
    head = _command_head(command)
    if head is None:
        return False
    base = Path(head).name.lower()
    return base in {"rg", "rg.exe"}


def _coerce_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(float(value.strip()))
        except ValueError:
            return None
    return None


def _failure_rate(*, failed: int, executed: int) -> float:
    denom = max(1, int(executed))
    return float(failed) / float(denom)


def _ratio(numerator: float, denominator: float) -> float:
    if denominator <= 0.0:
        return float("inf") if numerator > 0.0 else 1.0
    return float(numerator) / float(denominator)


def _join_streams(stdout: Any, stderr: Any) -> str | None:
    parts: list[str] = []
    if isinstance(stdout, str) and stdout.strip():
        parts.append(stdout.strip())
    if isinstance(stderr, str) and stderr.strip():
        parts.append(stderr.strip())
    joined = "\n".join(parts).strip()
    return joined if joined else None


def _classify_command_failure_kind(
    *,
    command: str,
    exit_code: int,
    output_excerpt: str | None,
) -> str:
    text = (output_excerpt or "").lower()
    cmd_lower = command.lower()

    if exit_code in {124, 137} or "timed out" in text or "timeout" in text:
        return "timeout"
    if exit_code == 127 or "command not found" in text:
        return "command_not_found"
    if "no module named" in text:
        return "python_import_error"
    if "temporary failure in name resolution" in text or "nameresolutionerror" in text:
        return "network_name_resolution"
    if "permission denied" in text or "access is denied" in text:
        return "permission_denied"
    if "no such file or directory" in text or "cannot find the path specified" in text:
        return "missing_path"
    if "connection reset" in text or "connection aborted" in text or "connection refused" in text:
        return "network_connection"
    if "pip" in cmd_lower and ("ssl" in text or "certificate" in text):
        return "network_tls"
    return "nonzero_exit"


def _iter_failed_commands_from_metrics(metrics: dict[str, Any]) -> Iterable[dict[str, Any]]:
    raw = metrics.get("failed_commands")
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        command = _coerce_string(item.get("command"))
        exit_code = item.get("exit_code")
        if command is None or not isinstance(exit_code, int) or exit_code == 0:
            continue
        if _is_ripgrep_no_matches(command=command, exit_code=exit_code):
            continue
        out.append(
            {
                "command": command,
                "exit_code": exit_code,
                "output_excerpt": _coerce_string(item.get("output_excerpt")),
            }
        )
    return out


def _iter_failed_commands_from_events(run_dir: Path) -> Iterable[dict[str, Any]]:
    events_path = run_dir / "normalized_events.jsonl"
    if not events_path.exists():
        return []
    out: list[dict[str, Any]] = []
    try:
        with events_path.open("r", encoding="utf-8") as f:
            for line in f:
                raw = line.strip()
                if not raw:
                    continue
                try:
                    event = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if not isinstance(event, dict):
                    continue
                if _coerce_string(event.get("type")) != "run_command":
                    continue
                data = event.get("data")
                if not isinstance(data, dict):
                    continue
                exit_code = data.get("exit_code")
                if not isinstance(exit_code, int) or exit_code == 0:
                    continue
                command = _coerce_string(data.get("command"))
                if command is None:
                    argv = data.get("argv")
                    if isinstance(argv, list) and all(isinstance(a, str) for a in argv):
                        command = " ".join(argv)
                if command is None:
                    continue
                if _is_ripgrep_no_matches(command=command, exit_code=exit_code):
                    continue
                out.append(
                    {
                        "command": command,
                        "exit_code": exit_code,
                        "output_excerpt": _coerce_string(data.get("output_excerpt")),
                        "stdout_stderr": _join_streams(data.get("stdout"), data.get("stderr")),
                    }
                )
                if len(out) >= _MAX_FAILED_COMMANDS_PER_RUN:
                    break
    except OSError:
        return []
    return out


def _collect_command_failure_breakdown(
    metric_runs: list[dict[str, Any]],
    *,
    max_top: int,
) -> dict[str, Any] | None:
    command_counts: Counter[str] = Counter()
    kind_counts: Counter[str] = Counter()
    command_kind_counts: Counter[tuple[str, str]] = Counter()

    for item in metric_runs:
        run_dir_raw = item.get("run_dir")
        run_dir = Path(run_dir_raw) if isinstance(run_dir_raw, str) else None
        metrics_raw = item.get("metrics")
        metrics = metrics_raw if isinstance(metrics_raw, dict) else None

        failures: list[dict[str, Any]] = []
        if metrics is not None:
            failures = list(_iter_failed_commands_from_metrics(metrics))
        if not failures and run_dir is not None:
            failures = list(_iter_failed_commands_from_events(run_dir))

        for failure in failures:
            command = _coerce_string(failure.get("command"))
            exit_code = failure.get("exit_code")
            if command is None or not isinstance(exit_code, int) or exit_code == 0:
                continue
            output_excerpt = _coerce_string(failure.get("output_excerpt")) or _coerce_string(
                failure.get("stdout_stderr")
            )
            kind = _classify_command_failure_kind(
                command=command,
                exit_code=exit_code,
                output_excerpt=output_excerpt,
            )
            command_counts[command] += 1
            kind_counts[kind] += 1
            command_kind_counts[(command, kind)] += 1

    if not command_counts:
        return None

    top: list[dict[str, Any]] = []
    for command, failures in command_counts.most_common(max_top):
        per_kind = {
            kind: count
            for (cmd, kind), count in command_kind_counts.items()
            if cmd == command and count > 0
        }
        top.append(
            {
                "command": command,
                "failures": int(failures),
                "failure_kinds": dict(sorted(per_kind.items())),
            }
        )

    return {
        "total_failed_commands": int(sum(command_counts.values())),
        "failure_kind_counts": dict(sorted(kind_counts.items())),
        "top_failed_commands": top,
        "top_failed_commands_max": int(max_top),
    }


def build_aggregate_metrics_atoms(
    records: list[dict[str, Any]],
    eligible_run_rels: set[str],
    *,
    run_id_prefix: str,
    top_failed_commands: int = _MAX_TOP_FAILED_COMMANDS,
) -> list[dict[str, Any]]:
    """
    Build synthetic aggregate-metrics atoms from eligible run records.

    Aggregates are computed only over eligible runs (as defined by upstream atom filtering),
    so that the aggregate layer reflects current open friction.
    """

    run_id = run_id_prefix
    run_rel = run_id_prefix

    metric_runs: list[dict[str, Any]] = []
    for record in records:
        rr = _coerce_string(record.get("run_rel"))
        if rr is None or rr not in eligible_run_rels:
            continue
        metrics_raw = record.get("metrics")
        if not isinstance(metrics_raw, dict):
            continue

        executed = _coerce_int(metrics_raw.get("commands_executed"))
        failed = _coerce_int(metrics_raw.get("commands_failed"))
        if executed is None or failed is None:
            continue

        agent = _coerce_string(record.get("agent")) or "unknown"
        target_slug = _coerce_string(record.get("target_slug")) or "unknown"

        repo_input = None
        mission_id = None
        persona_id = None
        target_ref = record.get("target_ref")
        if isinstance(target_ref, dict):
            repo_input = _coerce_string(target_ref.get("repo_input"))
            mission_id = _coerce_string(target_ref.get("mission_id"))
            persona_id = _coerce_string(target_ref.get("persona_id"))

        metric_runs.append(
            {
                "run_rel": rr,
                "agent": agent,
                "target_slug": target_slug,
                "repo_input": repo_input,
                "mission_id": mission_id,
                "persona_id": persona_id,
                "commands_executed": int(executed),
                "commands_failed": int(failed),
                "run_dir": record.get("run_dir"),
                "metrics": dict(metrics_raw),
            }
        )

    if not metric_runs:
        return []

    baseline_runs = len(metric_runs)
    baseline_executed = sum(int(item["commands_executed"]) for item in metric_runs)
    baseline_failed = sum(int(item["commands_failed"]) for item in metric_runs)
    baseline_failure_rate = _failure_rate(failed=baseline_failed, executed=baseline_executed)
    baseline_avg_failed_per_run = float(baseline_failed) / float(baseline_runs)

    atoms: list[dict[str, Any]] = []

    baseline_metrics = {
        "runs": int(baseline_runs),
        "commands_executed": int(baseline_executed),
        "commands_failed": int(baseline_failed),
        "failure_rate": float(baseline_failure_rate),
        "avg_failed_per_run": float(baseline_avg_failed_per_run),
    }
    baseline_breakdown = _collect_command_failure_breakdown(
        metric_runs,
        max_top=top_failed_commands,
    )
    baseline_atom: dict[str, Any] = {
        "atom_id": f"{run_id}:aggregate_metrics:1",
        "run_id": run_id,
        "run_rel": run_rel,
        "run_dir": run_id_prefix,
        "agent": "aggregate",
        "status": "aggregate",
        "timestamp_utc": None,
        "source": "aggregate_metrics",
        "severity_hint": "low",
        "quantified": True,
        "scope": "aggregate",
        "aggregate_kind": "baseline",
        "metrics": baseline_metrics,
        "supporting_run_rels": sorted({item["run_rel"] for item in metric_runs}),
        "supporting_agents": sorted({item["agent"] for item in metric_runs}),
        "text": (
            "Baseline across "
            f"{baseline_runs} eligible runs: "
            f"failure_rate={baseline_failure_rate:.3f} "
            f"(commands_failed={baseline_failed} / commands_executed={baseline_executed}); "
            f"avg_failed_per_run={baseline_avg_failed_per_run:.2f}"
        ),
    }
    if baseline_breakdown is not None:
        baseline_atom["command_failure_breakdown"] = baseline_breakdown
    atoms.append(baseline_atom)

    by_workflow: dict[tuple[str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for item in metric_runs:
        key = (
            str(item.get("target_slug") or ""),
            str(item.get("repo_input") or ""),
            str(item.get("mission_id") or ""),
            str(item.get("persona_id") or ""),
        )
        by_workflow[key].append(item)

    workflow_keys = sorted(by_workflow.keys())
    next_index = 2
    for key in workflow_keys:
        items = by_workflow[key]
        if len(items) < 2:
            continue
        wf_runs = len(items)
        wf_executed = sum(int(item["commands_executed"]) for item in items)
        wf_failed = sum(int(item["commands_failed"]) for item in items)
        wf_failure_rate = _failure_rate(failed=wf_failed, executed=wf_executed)
        wf_avg_failed_per_run = float(wf_failed) / float(wf_runs)

        workflow_key = {
            "target_slug": _coerce_string(items[0].get("target_slug")) or "unknown",
            "repo_input": _coerce_string(items[0].get("repo_input")) or "unknown",
            "mission_id": _coerce_string(items[0].get("mission_id")) or "unknown",
            "persona_id": _coerce_string(items[0].get("persona_id")) or "unknown",
        }

        wf_metrics = {
            "runs": int(wf_runs),
            "commands_executed": int(wf_executed),
            "commands_failed": int(wf_failed),
            "failure_rate": float(wf_failure_rate),
            "avg_failed_per_run": float(wf_avg_failed_per_run),
            "baseline_failure_rate": float(baseline_failure_rate),
            "baseline_avg_failed_per_run": float(baseline_avg_failed_per_run),
            "failure_rate_ratio_vs_baseline": float(_ratio(wf_failure_rate, baseline_failure_rate)),
            "avg_failed_per_run_ratio_vs_baseline": float(
                _ratio(wf_avg_failed_per_run, baseline_avg_failed_per_run)
            ),
        }

        workflow_breakdown = _collect_command_failure_breakdown(
            items,
            max_top=top_failed_commands,
        )
        wf_atom: dict[str, Any] = {
            "atom_id": f"{run_id}:aggregate_metrics:{next_index}",
            "run_id": run_id,
            "run_rel": run_rel,
            "run_dir": run_id_prefix,
            "agent": "aggregate",
            "status": "aggregate",
            "timestamp_utc": None,
            "source": "aggregate_metrics",
            "severity_hint": "low",
            "quantified": True,
            "scope": "aggregate",
            "aggregate_kind": "workflow",
            "workflow_key": workflow_key,
            "metrics": wf_metrics,
            "supporting_run_rels": sorted({item["run_rel"] for item in items}),
            "supporting_agents": sorted({item["agent"] for item in items}),
            "text": (
                f"Across {wf_runs} eligible runs for "
                f"target={workflow_key['target_slug']} "
                f"repo_input={workflow_key['repo_input']} "
                f"mission={workflow_key['mission_id']} "
                f"persona={workflow_key['persona_id']}: "
                f"failure_rate={wf_failure_rate:.3f} vs baseline={baseline_failure_rate:.3f} "
                f"({wf_metrics['failure_rate_ratio_vs_baseline']:.2f}x); "
                f"avg_failed_per_run={wf_avg_failed_per_run:.2f} "
                f"vs baseline={baseline_avg_failed_per_run:.2f} "
                f"({wf_metrics['avg_failed_per_run_ratio_vs_baseline']:.2f}x)"
            ),
        }
        if workflow_breakdown is not None:
            wf_atom["command_failure_breakdown"] = workflow_breakdown
        atoms.append(wf_atom)
        next_index += 1

    return atoms
