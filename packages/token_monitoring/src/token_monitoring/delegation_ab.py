from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from typing import Any

from token_monitoring.run_analysis import analyze_run


def _utc_now_z() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _load_json_object(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _load_json_value(path: Path) -> Any:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _iter_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                try:
                    parsed = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(parsed, dict):
                    out.append(parsed)
    except OSError:
        return []
    return out


def _coerce_int(value: Any) -> int | None:
    return value if isinstance(value, int) else None


def _coerce_float(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) else None


def _list_len(value: Any) -> int:
    return len(value) if isinstance(value, list) else 0


def _validation_error_count(value: Any) -> int:
    if isinstance(value, list):
        return len(value)
    if isinstance(value, dict):
        errors = value.get("errors")
        if isinstance(errors, list):
            return len(errors)
    return 0


def _signal_ids(analysis: dict[str, Any]) -> list[str]:
    signals = analysis.get("signals")
    if not isinstance(signals, list):
        return []
    out: list[str] = []
    for signal in signals:
        if not isinstance(signal, dict):
            continue
        signal_id = signal.get("signal_id")
        if isinstance(signal_id, str):
            out.append(signal_id)
    return out


def _token_peak_input(analysis: dict[str, Any]) -> int:
    token_summary = analysis.get("token_summary")
    if not isinstance(token_summary, dict):
        return 0
    peak_call = token_summary.get("peak_call")
    if not isinstance(peak_call, dict):
        return 0
    token_usage = peak_call.get("token_usage")
    if not isinstance(token_usage, dict):
        return 0
    return int(token_usage.get("input_tokens", 0) or 0)


def _combined_input_tokens(analysis: dict[str, Any]) -> int:
    token_summary = analysis.get("token_summary")
    if not isinstance(token_summary, dict):
        return 0
    parent = int(token_summary.get("parent_input_tokens", 0) or 0)
    delegated = token_summary.get("delegated_token_dimensions")
    if not isinstance(delegated, dict):
        return parent
    return parent + int(delegated.get("input_tokens", 0) or 0)


def _test_heuristics(run_dir: Path) -> dict[str, int]:
    test_events = 0
    test_failures_before_success = 0
    saw_success = False
    for event in _iter_jsonl(run_dir / "normalized_events.jsonl"):
        if event.get("type") != "run_command":
            continue
        data = event.get("data")
        if not isinstance(data, dict):
            continue
        command = data.get("command")
        if not isinstance(command, str):
            argv = data.get("argv")
            if isinstance(argv, list):
                command = " ".join(str(part) for part in argv)
        if not isinstance(command, str):
            continue
        lowered = command.lower()
        if not any(marker in lowered for marker in ("pytest", "npm test", "go test", "cargo test")):
            continue
        exit_code = _coerce_int(data.get("exit_code"))
        if exit_code is None:
            continue
        test_events += 1
        if exit_code == 0:
            saw_success = True
        elif not saw_success:
            test_failures_before_success += 1
    return {
        "test_runs_total": test_events,
        "test_runs_failed_before_success": test_failures_before_success,
    }


def _verification_summary(run_dir: Path) -> dict[str, Any]:
    verification = _load_json_object(run_dir / "verification.json")
    timing = _load_json_object(run_dir / "timing.json")
    run_meta = _load_json_object(run_dir / "run_meta.json")
    duration = _coerce_float(timing.get("duration_seconds"))
    if duration is None:
        duration = _coerce_float(run_meta.get("run_wall_seconds"))
    if duration is None:
        duration = _coerce_float(run_meta.get("duration_seconds"))
    passed_raw = verification.get("passed")
    return {
        "verification_present": bool(verification),
        "verification_passed": passed_raw if isinstance(passed_raw, bool) else None,
        "elapsed_seconds": duration,
        **_test_heuristics(run_dir),
    }


def _run_evidence(
    *,
    run_dir: Path,
    arm: str,
    codex_sessions_root: Path | None,
) -> dict[str, Any]:
    run_dir = run_dir.resolve()
    analysis = analyze_run(run_dir, codex_sessions_root=codex_sessions_root)
    token_summary = analysis.get("token_summary")
    token_summary_dict = token_summary if isinstance(token_summary, dict) else {}
    delegation = analysis.get("delegation_summary")
    delegation_dict = delegation if isinstance(delegation, dict) else {}
    report = _load_json_object(run_dir / "report.json")
    ticket_ref = _load_json_object(run_dir / "ticket_ref.json")
    metrics = _load_json_object(run_dir / "metrics.json")
    handoff_summary = _load_json_object(run_dir / "handoff_summary.json")
    review_summary = _load_json_object(run_dir / "review_summary.json")
    report_validation_errors = _load_json_value(run_dir / "report_validation_errors.json")
    signals = _signal_ids(analysis)
    issues = report.get("issues")
    status = report.get("status")
    return {
        "run_dir": str(run_dir),
        "arm": arm,
        "agent": analysis.get("agent"),
        "ticket": {
            "fingerprint": ticket_ref.get("fingerprint"),
            "title": ticket_ref.get("title"),
        },
        "implementation_quality": {
            "status_class": analysis.get("status_class"),
            "report_status": status if isinstance(status, str) else None,
            "report_issue_count": _list_len(issues),
            "report_validation_error_count": _validation_error_count(report_validation_errors),
            "commands_failed": metrics.get("commands_failed")
            if isinstance(metrics.get("commands_failed"), int)
            else None,
            "review_decision": review_summary.get("review_decision")
            or handoff_summary.get("review_decision"),
            "review_merge_ready": review_summary.get("merge_ready")
            if "merge_ready" in review_summary
            else handoff_summary.get("review_merge_ready"),
            "review_finding_count": _list_len(review_summary.get("findings"))
            + _list_len(review_summary.get("blocking_findings")),
        },
        "tokens": {
            "parent_input_tokens": int(token_summary_dict.get("parent_input_tokens", 0) or 0),
            "parent_input_peak": _token_peak_input(analysis),
            "combined_input_tokens": _combined_input_tokens(analysis),
            "combined_total_tokens": int(token_summary_dict.get("combined_total_tokens", 0) or 0),
            "authoritative": bool(token_summary_dict.get("authoritative")),
        },
        "signals": {
            "signal_ids": signals,
            "broad_source_config_read_count": signals.count("broad_source_config_read"),
            "large_context_resend_count": signals.count("large_context_resend"),
        },
        "delegation": {
            "classification": delegation_dict.get("classification"),
            "invocation_count": int(delegation_dict.get("invocation_count", 0) or 0),
            "summary_count": int(delegation_dict.get("summary_count", 0) or 0),
            "raw_broad_source_leak_count": int(
                delegation_dict.get("raw_broad_source_leak_count", 0) or 0
            ),
            "error_count": int(delegation_dict.get("error_count", 0) or 0),
        },
        "verification": _verification_summary(run_dir),
    }


def _numeric_values(rows: Iterable[dict[str, Any]], path: tuple[str, ...]) -> list[float]:
    values: list[float] = []
    for row in rows:
        cur: Any = row
        for key in path:
            if not isinstance(cur, dict):
                cur = None
                break
            cur = cur.get(key)
        value = _coerce_float(cur)
        if value is not None:
            values.append(value)
    return values


def _avg(rows: list[dict[str, Any]], path: tuple[str, ...]) -> float | None:
    values = _numeric_values(rows, path)
    return round(mean(values), 2) if values else None


def _sum_int(rows: list[dict[str, Any]], path: tuple[str, ...]) -> int:
    return int(sum(_numeric_values(rows, path)))


def _arm_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "run_count": len(rows),
        "completed_report_count": sum(
            1
            for row in rows
            if row.get("implementation_quality", {}).get("status_class") == "completed_report"
        ),
        "report_success_count": sum(
            1
            for row in rows
            if row.get("implementation_quality", {}).get("report_status") == "success"
        ),
        "avg_parent_input_peak": _avg(rows, ("tokens", "parent_input_peak")),
        "avg_parent_input_tokens": _avg(rows, ("tokens", "parent_input_tokens")),
        "avg_combined_input_tokens": _avg(rows, ("tokens", "combined_input_tokens")),
        "avg_combined_total_tokens": _avg(rows, ("tokens", "combined_total_tokens")),
        "broad_source_config_read_count": _sum_int(
            rows, ("signals", "broad_source_config_read_count")
        ),
        "large_context_resend_count": _sum_int(rows, ("signals", "large_context_resend_count")),
        "avg_elapsed_seconds": _avg(rows, ("verification", "elapsed_seconds")),
        "verification_passed_count": sum(
            1 for row in rows if row.get("verification", {}).get("verification_passed") is True
        ),
        "test_runs_total": _sum_int(rows, ("verification", "test_runs_total")),
        "test_runs_failed_before_success": _sum_int(
            rows, ("verification", "test_runs_failed_before_success")
        ),
        "delegation_invocation_count": _sum_int(rows, ("delegation", "invocation_count")),
        "delegation_raw_broad_source_leak_count": _sum_int(
            rows, ("delegation", "raw_broad_source_leak_count")
        ),
    }


def _pair_key(row: dict[str, Any]) -> str:
    ticket = row.get("ticket")
    if isinstance(ticket, dict):
        fingerprint = ticket.get("fingerprint")
        if isinstance(fingerprint, str) and fingerprint.strip():
            return fingerprint
        title = ticket.get("title")
        if isinstance(title, str) and title.strip():
            return title.strip().lower()
    return str(row.get("run_dir"))


def _paired_comparisons(
    disabled_rows: list[dict[str, Any]], enabled_rows: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    disabled_by_key = {_pair_key(row): row for row in disabled_rows}
    enabled_by_key = {_pair_key(row): row for row in enabled_rows}
    out: list[dict[str, Any]] = []
    for key in sorted(set(disabled_by_key) & set(enabled_by_key)):
        disabled = disabled_by_key[key]
        enabled = enabled_by_key[key]
        out.append(
            {
                "pair_key": key,
                "disabled_run_dir": disabled.get("run_dir"),
                "enabled_run_dir": enabled.get("run_dir"),
                "parent_input_peak_delta": int(
                    enabled.get("tokens", {}).get("parent_input_peak", 0) or 0
                )
                - int(disabled.get("tokens", {}).get("parent_input_peak", 0) or 0),
                "combined_input_tokens_delta": int(
                    enabled.get("tokens", {}).get("combined_input_tokens", 0) or 0
                )
                - int(disabled.get("tokens", {}).get("combined_input_tokens", 0) or 0),
                "large_context_resend_delta": int(
                    enabled.get("signals", {}).get("large_context_resend_count", 0) or 0
                )
                - int(disabled.get("signals", {}).get("large_context_resend_count", 0) or 0),
                "quality_status": {
                    "disabled": disabled.get("implementation_quality", {}).get("report_status"),
                    "enabled": enabled.get("implementation_quality", {}).get("report_status"),
                },
                "verification_passed": {
                    "disabled": disabled.get("verification", {}).get("verification_passed"),
                    "enabled": enabled.get("verification", {}).get("verification_passed"),
                },
            }
        )
    return out


def _tradeoff_evaluation(
    *,
    disabled: dict[str, Any],
    enabled: dict[str, Any],
    pairs: list[dict[str, Any]],
) -> dict[str, Any]:
    disabled_input = _coerce_float(disabled.get("avg_combined_input_tokens"))
    enabled_input = _coerce_float(enabled.get("avg_combined_input_tokens"))
    disabled_total = _coerce_float(disabled.get("avg_combined_total_tokens"))
    enabled_total = _coerce_float(enabled.get("avg_combined_total_tokens"))
    disabled_peak = _coerce_float(disabled.get("avg_parent_input_peak"))
    enabled_peak = _coerce_float(enabled.get("avg_parent_input_peak"))
    combined_input_delta = (
        round(enabled_input - disabled_input, 2)
        if enabled_input is not None and disabled_input is not None
        else None
    )
    combined_total_delta = (
        round(enabled_total - disabled_total, 2)
        if enabled_total is not None and disabled_total is not None
        else None
    )
    parent_peak_delta = (
        round(enabled_peak - disabled_peak, 2)
        if enabled_peak is not None and disabled_peak is not None
        else None
    )
    enabled_has_quality_drop = int(enabled.get("report_success_count", 0) or 0) < int(
        disabled.get("report_success_count", 0) or 0
    )
    enabled_has_signal_drop = int(enabled.get("large_context_resend_count", 0) or 0) < int(
        disabled.get("large_context_resend_count", 0) or 0
    ) or int(enabled.get("broad_source_config_read_count", 0) or 0) < int(
        disabled.get("broad_source_config_read_count", 0) or 0
    )

    token_delta_for_policy = (
        combined_total_delta if combined_total_delta is not None else combined_input_delta
    )

    if token_delta_for_policy is None:
        conclusion = "token_tradeoff_unattributable"
        rationale = "At least one arm lacks authoritative token counters."
    elif token_delta_for_policy <= 0:
        conclusion = "delegation_did_not_increase_combined_tokens"
        rationale = "Delegation-enabled runs did not increase average combined tokens."
    elif (
        (parent_peak_delta is not None and parent_peak_delta < 0)
        or enabled_has_signal_drop
        or not enabled_has_quality_drop
    ):
        conclusion = "delegation_increased_combined_tokens_with_compensating_evidence"
        rationale = (
            "Delegation increased average combined input tokens, but the comparison includes "
            "compensating evidence from parent-context pressure, quality, or resend-signal "
            "behavior."
        )
    else:
        conclusion = "delegation_increased_combined_tokens_without_compensating_evidence"
        rationale = (
            "Delegation increased average combined input tokens without observed parent-pressure, "
            "quality, or resend-signal improvement."
        )

    return {
        "combined_input_tokens_delta": combined_input_delta,
        "combined_total_tokens_delta": combined_total_delta,
        "parent_input_peak_delta": parent_peak_delta,
        "paired_run_count": len(pairs),
        "conclusion": conclusion,
        "rationale": rationale,
    }


def _evidence_strength(
    *,
    disabled_rows: list[dict[str, Any]],
    enabled_rows: list[dict[str, Any]],
    pairs: list[dict[str, Any]],
) -> str:
    if not disabled_rows or not enabled_rows:
        return "insufficient_missing_arm"
    if not pairs:
        return "insufficient_no_comparable_ticket_pair"
    all_rows = disabled_rows + enabled_rows
    if any(row.get("tokens", {}).get("authoritative") is not True for row in all_rows):
        return "partial_missing_authoritative_token_join"
    return "representative_ab_evidence"


def _next_actions(
    *,
    strength: str,
    enabled_summary: dict[str, Any],
    tradeoff: dict[str, Any],
) -> list[str]:
    actions: list[str] = []
    if strength != "representative_ab_evidence":
        actions.append(
            "Run more same-ticket disabled/enabled maintenance pairs before making delegation "
            "more aggressive."
        )
    if int(enabled_summary.get("delegation_raw_broad_source_leak_count", 0) or 0) > 0:
        actions.append(
            "Tighten delegation prompts/policy to require concise summaries and artifact "
            "references instead of raw broad-source or log dumps."
        )
    conclusion = tradeoff.get("conclusion")
    if conclusion == "delegation_increased_combined_tokens_without_compensating_evidence":
        actions.append(
            "Keep delegation conservative; collect higher-quality pairs or tighten prompts "
            "before broadening delegation policy."
        )
    elif conclusion == "delegation_increased_combined_tokens_with_compensating_evidence":
        actions.append(
            "Consider cautious delegation expansion only for tasks matching the measured "
            "parent-context or quality benefits."
        )
    if not actions:
        actions.append(
            "Keep the A/B evidence with the policy change record and monitor future runs."
        )
    return actions


def analyze_delegation_ab(
    *,
    disabled_run_dirs: Iterable[Path],
    enabled_run_dirs: Iterable[Path],
    codex_sessions_root: Path | None = None,
) -> dict[str, Any]:
    disabled_rows = [
        _run_evidence(
            run_dir=Path(run_dir),
            arm="delegation_disabled",
            codex_sessions_root=codex_sessions_root,
        )
        for run_dir in disabled_run_dirs
    ]
    enabled_rows = [
        _run_evidence(
            run_dir=Path(run_dir),
            arm="delegation_enabled",
            codex_sessions_root=codex_sessions_root,
        )
        for run_dir in enabled_run_dirs
    ]
    disabled_summary = _arm_summary(disabled_rows)
    enabled_summary = _arm_summary(enabled_rows)
    pairs = _paired_comparisons(disabled_rows, enabled_rows)
    strength = _evidence_strength(
        disabled_rows=disabled_rows,
        enabled_rows=enabled_rows,
        pairs=pairs,
    )
    tradeoff = _tradeoff_evaluation(
        disabled=disabled_summary,
        enabled=enabled_summary,
        pairs=pairs,
    )
    return {
        "schema_version": 1,
        "generated_at_utc": _utc_now_z(),
        "validation_kind": "delegation_ab",
        "evidence_strength": strength,
        "arms": {
            "delegation_disabled": disabled_summary,
            "delegation_enabled": enabled_summary,
        },
        "comparisons": pairs,
        "tradeoff_evaluation": tradeoff,
        "runs": [*disabled_rows, *enabled_rows],
        "next_actions": _next_actions(
            strength=strength,
            enabled_summary=enabled_summary,
            tradeoff=tradeoff,
        ),
        "privacy": {
            "contains_raw_prompt": False,
            "contains_raw_source": False,
            "contains_raw_command_output": False,
        },
    }


def render_delegation_ab_markdown(report: dict[str, Any]) -> str:
    arms = report.get("arms")
    arms_dict = arms if isinstance(arms, dict) else {}
    tradeoff = report.get("tradeoff_evaluation")
    tradeoff_dict = tradeoff if isinstance(tradeoff, dict) else {}
    lines = [
        "# Delegation A/B validation",
        "",
        f"- Evidence strength: `{report.get('evidence_strength')}`",
        f"- Tradeoff conclusion: `{tradeoff_dict.get('conclusion')}`",
        f"- Combined input delta: `{tradeoff_dict.get('combined_input_tokens_delta')}`",
        f"- Combined total token delta: `{tradeoff_dict.get('combined_total_tokens_delta')}`",
        f"- Parent input peak delta: `{tradeoff_dict.get('parent_input_peak_delta')}`",
        "",
        "## Arms",
        "",
        "| Arm | Runs | Successes | Avg parent peak | Avg combined input | "
        "Broad-read signals | Large-context signals |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for arm_name in ("delegation_disabled", "delegation_enabled"):
        arm = arms_dict.get(arm_name)
        arm_dict = arm if isinstance(arm, dict) else {}
        lines.append(
            f"| `{arm_name}` | {arm_dict.get('run_count')} | "
            f"{arm_dict.get('report_success_count')} | {arm_dict.get('avg_parent_input_peak')} | "
            f"{arm_dict.get('avg_combined_input_tokens')} | "
            f"{arm_dict.get('broad_source_config_read_count')} | "
            f"{arm_dict.get('large_context_resend_count')} |"
        )

    lines.extend(["", "## Evaluation", "", str(tradeoff_dict.get("rationale") or "")])
    next_actions = report.get("next_actions")
    if isinstance(next_actions, list) and next_actions:
        lines.extend(["", "## Next actions", ""])
        for action in next_actions:
            lines.append(f"- {action}")
    lines.append("")
    return "\n".join(lines)


def write_delegation_ab_validation(
    *,
    disabled_run_dirs: Iterable[Path],
    enabled_run_dirs: Iterable[Path],
    output_dir: Path,
    codex_sessions_root: Path | None = None,
) -> dict[str, Any]:
    report = analyze_delegation_ab(
        disabled_run_dirs=disabled_run_dirs,
        enabled_run_dirs=enabled_run_dirs,
        codex_sessions_root=codex_sessions_root,
    )
    destination = output_dir.resolve()
    destination.mkdir(parents=True, exist_ok=True)
    (destination / "delegation_ab_validation.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    (destination / "delegation_ab_validation.md").write_text(
        render_delegation_ab_markdown(report),
        encoding="utf-8",
        newline="\n",
    )
    return report
