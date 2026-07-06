from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from token_monitoring.codex import (
    TOKEN_DIMENSIONS,
    CodexSessionResult,
    add_usage,
    default_codex_sessions_root,
    find_codex_session_for_thread,
    parse_codex_session,
    zero_usage,
)

WAIT_POLL_INPUT_MIN = 100_000
WAIT_POLL_SHARE_MIN = 0.15
SOURCE_READ_INPUT_MIN = 100_000
SOURCE_READ_SHARE_MIN = 0.15
LARGE_CONTEXT_PEAK_MIN = 100_000
LARGE_OUTPUT_CHARS_MIN = 50_000
LARGE_OUTPUT_TOKEN_ESTIMATE_MIN = 10_000
VERIFICATION_LOOP_INPUT_MIN = 100_000


def _utc_now_z() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _iter_jsonl(path: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not path.exists():
        return out
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                out.append(parsed)
    return out


def _extract_thread_id(raw_events_path: Path) -> tuple[str | None, list[dict[str, Any]]]:
    if not raw_events_path.exists():
        return None, [{"code": "missing_raw_events", "path": str(raw_events_path)}]
    matches: list[str] = []
    for event in _iter_jsonl(raw_events_path):
        event_type = event.get("type")
        if event_type == "thread.started":
            thread_id = event.get("thread_id")
            if isinstance(thread_id, str) and thread_id.strip():
                matches.append(thread_id.strip())
        msg = event.get("msg")
        if isinstance(msg, dict) and msg.get("type") == "thread.started":
            thread_id = msg.get("thread_id")
            if isinstance(thread_id, str) and thread_id.strip():
                matches.append(thread_id.strip())
    unique = sorted(set(matches))
    if len(unique) == 1:
        return unique[0], []
    if not unique:
        return None, [{"code": "missing_thread_started_thread_id", "path": str(raw_events_path)}]
    return None, [
        {"code": "ambiguous_thread_ids", "path": str(raw_events_path), "thread_ids": unique}
    ]


def _target_ref_agent(run_dir: Path) -> str | None:
    path = run_dir / "target_ref.json"
    if not path.exists():
        return None
    try:
        raw = _load_json(path)
    except Exception:  # noqa: BLE001
        return None
    if isinstance(raw, dict) and isinstance(raw.get("agent"), str):
        return raw["agent"]
    return None


def _status_class(run_dir: Path) -> str:
    if (run_dir / "report.json").exists():
        return "completed_report"
    if (run_dir / "error.json").exists():
        return "failed_error"
    if (run_dir / "report_validation_errors.json").exists():
        return "failed_report_validation"
    return "missing_contract_artifacts"


def _read_file_evidence(run_dir: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for event in _iter_jsonl(run_dir / "normalized_events.jsonl"):
        if event.get("type") != "read_file":
            continue
        data = event.get("data")
        if not isinstance(data, dict):
            continue
        path = data.get("path")
        bytes_value = data.get("bytes")
        if not isinstance(path, str):
            continue
        out.append(
            {
                "path": path,
                "bytes": bytes_value if isinstance(bytes_value, int) else None,
                "source": str(run_dir / "normalized_events.jsonl"),
            }
        )
    out.sort(key=lambda item: int(item.get("bytes") or 0), reverse=True)
    return out[:25]


def _agent_attempts_summary(run_dir: Path) -> dict[str, Any]:
    path = run_dir / "agent_attempts.json"
    if not path.exists():
        return {"attempt_count": 0, "followup_attempts_used": 0, "rate_limit_retries_used": 0}
    try:
        raw = _load_json(path)
    except Exception:  # noqa: BLE001
        return {"attempt_count": 0, "followup_attempts_used": 0, "rate_limit_retries_used": 0}
    if not isinstance(raw, dict):
        return {"attempt_count": 0, "followup_attempts_used": 0, "rate_limit_retries_used": 0}
    attempts = raw.get("attempts")
    return {
        "attempt_count": len(attempts) if isinstance(attempts, list) else 0,
        "followup_attempts_used": raw.get("followup_attempts_used")
        if isinstance(raw.get("followup_attempts_used"), int)
        else 0,
        "rate_limit_retries_used": raw.get("rate_limit_retries_used")
        if isinstance(raw.get("rate_limit_retries_used"), int)
        else 0,
    }


def _sum_calls(calls: list[dict[str, Any]]) -> dict[str, int]:
    total = zero_usage()
    for call in calls:
        usage = call.get("token_usage")
        if isinstance(usage, dict):
            total = add_usage(total, {key: int(usage.get(key, 0)) for key in TOKEN_DIMENSIONS})
    return total


def _token_impact(usage: dict[str, int]) -> dict[str, int]:
    return {key: int(usage.get(key, 0)) for key in TOKEN_DIMENSIONS}


def _threshold_met(*, amount: int, total: int, absolute: int, share: float) -> bool:
    return amount >= absolute or (total > 0 and amount / total >= share)


def _signal(
    *,
    signal_id: str,
    confidence: str,
    causal_mechanism: str,
    token_usage: dict[str, int],
    evidence_path: Path,
    evidence: dict[str, Any],
    mitigation: str,
    false_positive_risk: str,
    confirmed_by_counters: bool,
) -> dict[str, Any]:
    return {
        "signal_id": signal_id,
        "confidence": confidence,
        "causal_mechanism": causal_mechanism,
        "token_dimensions_affected": _token_impact(token_usage),
        "evidence_path": str(evidence_path),
        "evidence": evidence,
        "mitigation_lever": mitigation,
        "false_positive_risk": false_positive_risk,
        "confirmed_by_counters": confirmed_by_counters,
    }


def _calls_by_action(trace: list[dict[str, Any]], action_type: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for call in trace:
        action = call.get("action")
        if isinstance(action, dict) and action.get("type") == action_type:
            out.append(call)
    return out


def _build_signals(
    *,
    run_dir: Path,
    agent: str | None,
    session: CodexSessionResult | None,
    read_files: list[dict[str, Any]],
    attempts: dict[str, Any],
) -> list[dict[str, Any]]:
    signals: list[dict[str, Any]] = []
    if agent and agent != "codex":
        signals.append(
            _signal(
                signal_id="unsupported_provider_gap",
                confidence="unattributable",
                causal_mechanism=(
                    f"Agent '{agent}' has no v1 local token telemetry source, so inefficient-token "
                    "causes cannot be confirmed from counters."
                ),
                token_usage=zero_usage(),
                evidence_path=run_dir / "target_ref.json",
                evidence={"agent": agent},
                mitigation=(
                    "Add provider-equivalent per-call token telemetry before ranking "
                    "or alerting."
                ),
                false_positive_risk="Low; this is an evidence gap, not a usage diagnosis.",
                confirmed_by_counters=False,
            )
        )
        return signals

    if session is None or not session.accepted:
        return signals

    trace = session.trace
    final_input = int(session.final_usage.get("input_tokens", 0))
    wait_calls = _calls_by_action(trace, "wait_poll")
    wait_usage = _sum_calls(wait_calls)
    if len(wait_calls) >= 2 and _threshold_met(
        amount=wait_usage["input_tokens"],
        total=final_input,
        absolute=WAIT_POLL_INPUT_MIN,
        share=WAIT_POLL_SHARE_MIN,
    ):
        signals.append(
            _signal(
                signal_id="wait_poll_resend",
                confidence="authoritative",
                causal_mechanism=(
                    "Repeated wait or terminal-poll actions caused the retained "
                    "context to be resent "
                    "across multiple model calls while waiting for external work."
                ),
                token_usage=wait_usage,
                evidence_path=session.path,
                evidence={
                    "call_count": len(wait_calls),
                    "call_indexes": [call.get("call_index") for call in wait_calls[:20]],
                    "command_classes": sorted(
                        {
                            str(call.get("action", {}).get("command_class"))
                            for call in wait_calls
                            if isinstance(call.get("action"), dict)
                        }
                    ),
                },
                mitigation=(
                    "Prefer a single longer wait or an expected-duration hint over "
                    "repeated polling turns."
                ),
                false_positive_risk=(
                    "A wait action may be legitimate; the signal requires repeated "
                    "calls and measured "
                    "input-token impact."
                ),
                confirmed_by_counters=True,
            )
        )

    source_calls = _calls_by_action(trace, "source_read")
    source_usage = _sum_calls(source_calls)
    if source_calls and _threshold_met(
        amount=source_usage["input_tokens"],
        total=final_input,
        absolute=SOURCE_READ_INPUT_MIN,
        share=SOURCE_READ_SHARE_MIN,
    ):
        signals.append(
            _signal(
                signal_id="broad_source_config_read",
                confidence="authoritative",
                causal_mechanism=(
                    "Source/config exploration was retained in context and materially contributed "
                    "to later model-call input."
                ),
                token_usage=source_usage,
                evidence_path=session.path,
                evidence={
                    "call_count": len(source_calls),
                    "call_indexes": [call.get("call_index") for call in source_calls[:20]],
                    "paths_from_calls": [
                        path
                        for call in source_calls[:20]
                        if isinstance(call.get("action"), dict)
                        for path in call["action"].get("paths", [])
                    ][:25],
                    "largest_read_files": read_files[:10],
                },
                mitigation=(
                    "Modularize oversized files and steer agents toward targeted "
                    "symbol or section reads "
                    "instead of broad file/config dumps."
                ),
                false_positive_risk=(
                    "Initial repo orientation can be useful; this signal requires "
                    "measured token impact "
                    "and named file/path evidence."
                ),
                confirmed_by_counters=True,
            )
        )

    retained_output_calls = [
        call
        for call in trace
        if isinstance(call.get("context_evidence"), dict)
        and int(call["context_evidence"].get("retained_output_chars") or 0)
        >= LARGE_OUTPUT_CHARS_MIN
    ]
    retained_output_usage = _sum_calls(retained_output_calls)
    if (
        retained_output_calls
        or retained_output_usage["input_tokens"] >= LARGE_OUTPUT_TOKEN_ESTIMATE_MIN
    ):
        signals.append(
            _signal(
                signal_id="retained_large_output",
                confidence="authoritative",
                causal_mechanism=(
                    "Large tool output remained in the conversation and was resent "
                    "into later model calls."
                ),
                token_usage=retained_output_usage,
                evidence_path=session.path,
                evidence={
                    "call_count": len(retained_output_calls),
                    "call_indexes": [call.get("call_index") for call in retained_output_calls[:20]],
                    "retained_output_chars": [
                        int(call.get("context_evidence", {}).get("retained_output_chars") or 0)
                        for call in retained_output_calls[:20]
                    ],
                },
                mitigation=(
                    "Capture large command output into artifacts and summarize "
                    "metadata instead of retaining full output."
                ),
                false_positive_risk=(
                    "Some large outputs are required evidence; v1 flags only "
                    "retained-output metadata."
                ),
                confirmed_by_counters=True,
            )
        )

    verifier_calls = _calls_by_action(trace, "verification") + _calls_by_action(trace, "dependency")
    verifier_usage = _sum_calls(verifier_calls)
    if len(verifier_calls) >= 2 and _threshold_met(
        amount=verifier_usage["input_tokens"],
        total=final_input,
        absolute=VERIFICATION_LOOP_INPUT_MIN,
        share=SOURCE_READ_SHARE_MIN,
    ):
        signals.append(
            _signal(
                signal_id="verification_or_dependency_loop",
                confidence="authoritative",
                causal_mechanism=(
                    "Verification or dependency commands repeated across model turns, causing the "
                    "same surrounding context to be resent."
                ),
                token_usage=verifier_usage,
                evidence_path=session.path,
                evidence={
                    "call_count": len(verifier_calls),
                    "call_indexes": [call.get("call_index") for call in verifier_calls[:20]],
                },
                mitigation=(
                    "Use runner-owned verification reuse and bounded retry rules "
                    "before asking the model again."
                ),
                false_positive_risk=(
                    "Repeated verification may be necessary after edits; classify "
                    "with state-change data in later versions."
                ),
                confirmed_by_counters=True,
            )
        )

    peak = session.peak_call
    peak_usage = peak.get("token_usage") if isinstance(peak.get("token_usage"), dict) else {}
    peak_input = int(peak_usage.get("input_tokens", 0))
    large_calls = [
        call
        for call in trace
        if isinstance(call.get("token_usage"), dict)
        and int(call["token_usage"].get("input_tokens", 0))
        >= max(LARGE_CONTEXT_PEAK_MIN, int(peak_input * 0.5))
    ]
    mechanisms = [
        s["signal_id"]
        for s in signals
        if s["signal_id"]
        in {"wait_poll_resend", "broad_source_config_read", "retained_large_output"}
    ]
    if len(large_calls) >= 3 and mechanisms:
        signals.append(
            _signal(
                signal_id="large_context_resend",
                confidence="authoritative",
                causal_mechanism=(
                    "A large retained context was resent repeatedly after specific causal inputs: "
                    + ", ".join(mechanisms)
                    + "."
                ),
                token_usage=_sum_calls(large_calls),
                evidence_path=session.path,
                evidence={
                    "large_call_count": len(large_calls),
                    "peak_call_index": peak.get("call_index"),
                    "peak_input_tokens": peak_input,
                    "causal_inputs": mechanisms,
                },
                mitigation=(
                    "Reduce retained context by splitting large files, artifacting "
                    "large outputs, and avoiding wait turns."
                ),
                false_positive_risk=(
                    "Large context alone is not emitted; this signal requires a "
                    "named causal input."
                ),
                confirmed_by_counters=True,
            )
        )

    retry_count = int(attempts.get("followup_attempts_used", 0)) + int(
        attempts.get("rate_limit_retries_used", 0)
    )
    attempt_count = int(attempts.get("attempt_count", 0))
    if retry_count > 0 or attempt_count > 1:
        signals.append(
            _signal(
                signal_id="retry_after_known_failure",
                confidence="inferred",
                causal_mechanism=(
                    "The run required retry/follow-up attempts after a known "
                    "failure or invalid output; v1 can bound the run-level token "
                    "impact but cannot isolate each retry without richer attempt "
                    "telemetry."
                ),
                token_usage=session.final_usage,
                evidence_path=run_dir / "agent_attempts.json",
                evidence={"attempt_count": attempt_count, "retry_count": retry_count},
                mitigation=(
                    "Classify known failures before retrying and stop retry loops "
                    "that cannot change state."
                ),
                false_positive_risk=(
                    "Some retries are legitimate; v1 marks this inferred unless "
                    "per-attempt token joins are available."
                ),
                confirmed_by_counters=True,
            )
        )

    return signals


def _artifact_exceptions(run_dir: Path) -> list[dict[str, Any]]:
    exceptions: list[dict[str, Any]] = []
    for name in ("raw_events.jsonl", "normalized_events.jsonl", "metrics.json", "run_meta.json"):
        path = run_dir / name
        if not path.exists():
            exceptions.append({"code": f"missing_{name.replace('.', '_')}", "path": str(path)})
    if not (run_dir / "report.json").exists() and not (run_dir / "error.json").exists():
        exceptions.append(
            {
                "code": "missing_report_or_error_artifact",
                "path": str(run_dir),
                "message": "Run has neither report.json nor error.json.",
            }
        )
    return exceptions


def analyze_run(run_dir: Path, *, codex_sessions_root: Path | None = None) -> dict[str, Any]:
    run_dir = run_dir.resolve()
    sessions_root = codex_sessions_root or default_codex_sessions_root()
    agent = _target_ref_agent(run_dir)
    exceptions = _artifact_exceptions(run_dir)
    raw_events_path = run_dir / "raw_events.jsonl"
    thread_id, thread_exceptions = _extract_thread_id(raw_events_path)
    exceptions.extend(thread_exceptions)

    session: CodexSessionResult | None = None
    join: dict[str, Any] = {
        "thread_id": thread_id,
        "session_path": None,
        "confidence": "unattributable",
        "exception": None,
    }
    if agent == "codex" and thread_id is not None:
        session_path, join_exceptions = find_codex_session_for_thread(sessions_root, thread_id)
        exceptions.extend(join_exceptions)
        if session_path is not None:
            session = parse_codex_session(session_path)
            exceptions.extend(session.exceptions)
            join = {
                "thread_id": thread_id,
                "session_path": str(session_path),
                "confidence": "authoritative" if session.accepted else "unattributable",
                "exception": None if session.accepted else "session_not_authoritative",
            }
    elif agent and agent != "codex":
        join["exception"] = f"unsupported_provider_{agent}"
    elif agent is None:
        join["exception"] = "missing_agent_metadata"

    read_files = _read_file_evidence(run_dir)
    attempts = _agent_attempts_summary(run_dir)
    signals = _build_signals(
        run_dir=run_dir,
        agent=agent,
        session=session,
        read_files=read_files,
        attempts=attempts,
    )
    authoritative = bool(session is not None and session.accepted)
    token_summary = {
        "authoritative": authoritative,
        "dimensions": session.final_usage
        if authoritative and session is not None
        else zero_usage(),
        "model_call_count": session.model_call_count if session is not None else 0,
        "token_event_count": session.token_event_count if session is not None else 0,
        "peak_call": session.peak_call
        if session is not None
        else {"call_index": None, "token_usage": zero_usage()},
    }

    return {
        "schema_version": 1,
        "generated_at_utc": _utc_now_z(),
        "run_dir": str(run_dir),
        "agent": agent,
        "status_class": _status_class(run_dir),
        "join": join,
        "token_summary": token_summary,
        "signals": signals,
        "exceptions": exceptions,
        "privacy": {
            "contains_raw_prompt": False,
            "contains_raw_source": False,
            "contains_raw_command_output": False,
        },
        "_trace": session.trace if session is not None and session.accepted else [],
    }


def public_analysis_payload(analysis: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in analysis.items() if key != "_trace"}


def render_monitoring_markdown(analysis: dict[str, Any]) -> str:
    public = public_analysis_payload(analysis)
    lines = [
        "# Token monitoring",
        "",
        f"- Run: `{public.get('run_dir')}`",
        f"- Agent: `{public.get('agent')}`",
        f"- Status: `{public.get('status_class')}`",
        f"- Join confidence: `{public.get('join', {}).get('confidence')}`",
        "",
        "## Signals",
        "",
    ]
    signals = public.get("signals")
    if not isinstance(signals, list) or not signals:
        lines.append("No causal token inefficiency signal met the v1 evidence rules.")
    else:
        lines.append("| Signal | Confidence | Input tokens | Mechanism | Mitigation |")
        lines.append("| --- | --- | ---: | --- | --- |")
        for signal in signals:
            if not isinstance(signal, dict):
                continue
            usage = signal.get("token_dimensions_affected")
            input_tokens = usage.get("input_tokens", 0) if isinstance(usage, dict) else 0
            lines.append(
                "| "
                + str(signal.get("signal_id"))
                + " | "
                + str(signal.get("confidence"))
                + " | "
                + str(input_tokens)
                + " | "
                + str(signal.get("causal_mechanism", "")).replace("|", "\\|")
                + " | "
                + str(signal.get("mitigation_lever", "")).replace("|", "\\|")
                + " |"
            )
    exceptions = public.get("exceptions")
    if isinstance(exceptions, list) and exceptions:
        lines.extend(["", "## Exceptions", ""])
        for item in exceptions:
            if isinstance(item, dict):
                lines.append(
                    f"- `{item.get('code')}`: `{item.get('path') or item.get('thread_id') or ''}`"
                )
    lines.append("")
    return "\n".join(lines)


def write_run_monitoring(
    run_dir: Path,
    *,
    codex_sessions_root: Path | None = None,
    output_dir: Path | None = None,
) -> dict[str, Any]:
    analysis = analyze_run(run_dir, codex_sessions_root=codex_sessions_root)
    destination = (output_dir or run_dir).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    public = public_analysis_payload(analysis)
    (destination / "token_monitoring.json").write_text(
        json.dumps(public, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    (destination / "token_monitoring.md").write_text(
        render_monitoring_markdown(analysis),
        encoding="utf-8",
        newline="\n",
    )
    with (destination / "token_causal_trace.jsonl").open("w", encoding="utf-8", newline="\n") as f:
        for record in analysis.get("_trace", []):
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    return public
