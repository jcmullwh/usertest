from __future__ import annotations

import json
import os
import re
from collections import Counter
from datetime import datetime, timezone
from json import JSONDecoder
from pathlib import Path
from typing import Any

from run_artifacts.capture import CaptureResult, TextCapturePolicy, capture_text_artifact
from run_artifacts.run_failure_event import (
    classify_failure_kind,
    classify_known_stderr_warnings,
    coerce_validation_errors,
    extract_error_artifacts,
    render_failure_text,
    sanitize_error,
)
from triage_engine import (
    Embedder as _Embedder,
)
from triage_engine import (
    TrustEvidence as _TrustEvidence,
)
from triage_engine import (
    assess_trust as _assess_trust,
)
from triage_engine import (
    build_merge_candidates as _build_candidates,
)
from triage_engine import (
    dedupe_clusters as _dedupe_clusters,
)
from triage_engine.text import extract_path_anchors_from_chunks, tokenize

_SEVERITY_ORDER: dict[str, int] = {"low": 0, "medium": 1, "high": 2, "blocker": 3}

# Heuristic trust weights by evidence kind.
#
# These weights are intentionally conservative: they are meant for ranking and surfacing
# "more corroborated" tickets, not for hard gating.
_TRUST_SOURCE_WEIGHTS: dict[str, float] = {
    "run_failure_event": 1.00,
    "error_json": 0.95,
    "report_validation_error": 0.90,
    "agent_stderr_artifact": 0.85,
    "capability_warning_artifact": 0.20,
    "capability_notice_artifact": 0.20,
    "agent_last_message_artifact": 0.75,
    "confusion_point": 0.70,
    "suggested_change": 0.65,
    "confidence_missing": 0.45,
}
def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return int(default)
    try:
        return int(str(raw).strip())
    except ValueError:
        return int(default)


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return float(default)
    try:
        return float(str(raw).strip())
    except ValueError:
        return float(default)


def _default_capture_policy() -> TextCapturePolicy:
    """Default capture policy, overridable via environment variables.

    Supported overrides:
    - BACKLOG_CAPTURE_MAX_EXCERPT_BYTES
    - BACKLOG_CAPTURE_HEAD_BYTES
    - BACKLOG_CAPTURE_TAIL_BYTES
    - BACKLOG_CAPTURE_MAX_LINE_COUNT
    - BACKLOG_CAPTURE_BINARY_DETECTION_BYTES
    """

    return TextCapturePolicy(
        max_excerpt_bytes=_env_int("BACKLOG_CAPTURE_MAX_EXCERPT_BYTES", 24_000),
        head_bytes=_env_int("BACKLOG_CAPTURE_HEAD_BYTES", 12_000),
        tail_bytes=_env_int("BACKLOG_CAPTURE_TAIL_BYTES", 12_000),
        max_line_count=_env_int("BACKLOG_CAPTURE_MAX_LINE_COUNT", 300),
        binary_detection_bytes=_env_int("BACKLOG_CAPTURE_BINARY_DETECTION_BYTES", 2_048),
    )


def _max_command_failure_atoms_per_run() -> int:
    """Max number of command-failure atoms emitted per run (env override supported)."""

    names = ("BACKLOG_MAX_COMMAND_FAILURE_ATOMS_PER_RUN", "BACKLOGmax_command_failure_atoms")
    for name in names:
        raw = os.getenv(name)
        if raw is None:
            continue
        try:
            return int(str(raw).strip())
        except ValueError:
            continue
    return 10


def _safe_relpath(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve())).replace("\\", "/")
    except Exception:  # noqa: BLE001
        return str(path).replace("\\", "/")


def _clean_atom_text(text: str) -> str:
    return text.strip()


def _compose_artifact_text(result: CaptureResult) -> str:
    excerpt = result.excerpt
    error = result.error
    if excerpt is None:
        return f"[capture_error] {error}" if isinstance(error, str) and error else ""

    head = excerpt.head
    tail = excerpt.tail
    if not excerpt.truncated:
        return head
    marker = "\n...[truncated; see artifact_ref/capture_manifest]...\n"
    if head and tail:
        return head + marker + tail
    return head or tail


def _artifact_ref_public(result: CaptureResult) -> dict[str, Any]:
    artifact = result.artifact
    return {
        "path": artifact.path,
        "exists": artifact.exists,
        "size_bytes": artifact.size_bytes,
        "sha256": artifact.sha256,
    }


def _capture_manifest_entry(result: CaptureResult) -> dict[str, Any]:
    artifact = result.artifact
    entry: dict[str, Any] = {
        "path": artifact.path,
        "abs_path": artifact.abs_path,
        "exists": artifact.exists,
        "size_bytes": artifact.size_bytes,
        "sha256": artifact.sha256,
        "error": result.error,
        "truncated": bool(result.excerpt.truncated) if result.excerpt is not None else False,
    }
    if result.excerpt is not None:
        entry["excerpt_head_chars"] = len(result.excerpt.head)
        entry["excerpt_tail_chars"] = len(result.excerpt.tail)
    return entry


def _coerce_string(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned if cleaned else None


def _coerce_string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        if isinstance(item, str) and item.strip():
            out.append(item.strip())
    return out


def _coerce_evidence_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []

    out: list[dict[str, Any]] = []
    for item in value:
        if isinstance(item, str) and item.strip():
            out.append({"kind": "note", "value": item.strip()})
            continue
        if not isinstance(item, dict):
            continue

        candidate: dict[str, Any] = {}
        for key, raw in item.items():
            key_s = str(key)
            if isinstance(raw, (str, int, float, bool)) or raw is None:
                candidate[key_s] = raw
                continue
            if isinstance(raw, list):
                values = [v for v in raw if isinstance(v, (str, int, float, bool)) or v is None]
                if values:
                    candidate[key_s] = values
        if candidate:
            out.append(candidate)
    return out


def _coerce_confidence(value: Any) -> float:
    if isinstance(value, (int, float)):
        return max(0.0, min(1.0, float(value)))
    if isinstance(value, str):
        try:
            parsed = float(value.strip())
        except ValueError:
            return 0.0
        return max(0.0, min(1.0, parsed))
    return 0.0


def _severity_rank(value: str) -> int:
    return _SEVERITY_ORDER.get(value, _SEVERITY_ORDER["medium"])


def _severity_from_priority(priority: str | None) -> str:
    if priority is None:
        return "medium"
    value = priority.strip().lower()
    if value in {"p0", "critical", "blocker"}:
        return "high"
    if value in {"p1", "high"}:
        return "high"
    if value in {"p2", "medium"}:
        return "medium"
    if value in {"p3", "low"}:
        return "low"
    return "medium"


def _infer_severity_hint(*, source: str, text: str, priority: str | None = None) -> str:
    if source == "run_failure_event":
        return "high"
    if source == "suggested_change":
        return _severity_from_priority(priority)
    if source in {"agent_stderr", "agent_stderr_artifact"}:
        lowered = text.lower()
        if any(token in lowered for token in ("429", "quota", "capacity", "resource_exhausted")):
            return "medium"
        return "high"
    if source == "capability_warning_artifact":
        return "low"
    if source == "capability_notice_artifact":
        return "low"
    if source == "confidence_missing":
        return "low"
    if source == "confusion_point":
        lowered = text.lower()
        if any(
            token in lowered
            for token in ("crash", "exception", "data loss", "blocked", "cannot", "can't")
        ):
            return "high"
        return "medium"
    if source in {"agent_last_message", "agent_last_message_artifact"}:
        return "low"
    return "medium"


_WS_RE = re.compile(r"\s+")


def _normalize_dedupe_key(text: str) -> str:
    return _WS_RE.sub(" ", text.strip()).lower()


def _severity_hint_from_report_issue_severity(raw: str | None) -> str:
    if raw is None:
        return "medium"
    value = raw.strip().lower()
    if value in {"error", "high", "critical", "blocker"}:
        return "high"
    if value in {"warn", "warning", "medium"}:
        return "medium"
    if value in {"info", "low"}:
        return "low"
    return "medium"


def _iter_unique_capped_strings(value: Any, *, limit: int) -> list[str]:
    if limit <= 0:
        return []
    raw_list: list[Any]
    if isinstance(value, str):
        raw_list = [value]
    elif isinstance(value, list):
        raw_list = value
    else:
        return []

    seen: set[str] = set()
    out: list[str] = []
    for item in raw_list:
        text = _coerce_string(item)
        if text is None:
            continue
        key = _normalize_dedupe_key(text)
        if key in seen:
            continue
        seen.add(key)
        out.append(text)
        if len(out) >= limit:
            break
    return out


def _extract_modern_report_atoms(
    *,
    report: dict[str, Any],
    report_kind: str,
    emit: Any,
) -> None:
    """
    Best-effort extraction for modern report schemas (e.g., task_run_v1, boundary_v1).

    This is intentionally conservative and additive: legacy extraction remains unchanged.
    """

    # issues / risks blocks (issue-like dicts)
    for block_name in ("issues", "risks"):
        items = report.get(block_name)
        if not isinstance(items, list):
            continue

        title_seen: set[str] = set()
        fix_seen: set[str] = set()
        title_emitted = 0
        fix_emitted = 0

        for issue in items:
            if title_emitted >= 10 and fix_emitted >= 10:
                break
            if not isinstance(issue, dict):
                continue

            severity_raw = _coerce_string(issue.get("severity"))
            severity_hint = _severity_hint_from_report_issue_severity(severity_raw)
            title = _coerce_string(issue.get("title"))
            details = _coerce_string(issue.get("details"))
            evidence_text = _coerce_string(issue.get("evidence"))
            suggested_fix = _coerce_string(issue.get("suggested_fix"))

            if title is not None and title_emitted < 10:
                title_key = _normalize_dedupe_key(title)
                if title_key not in title_seen:
                    title_seen.add(title_key)
                    title_emitted += 1
                    emit(
                        "confusion_point",
                        title,
                        impact=details,
                        evidence_text=evidence_text,
                        report_kind=report_kind,
                        report_issue_block=block_name,
                        issue_severity=severity_raw,
                        issue_title=title,
                        severity_hint=severity_hint,
                    )

            if suggested_fix is not None and fix_emitted < 10:
                fix_key = _normalize_dedupe_key(suggested_fix)
                if fix_key not in fix_seen:
                    fix_seen.add(fix_key)
                    fix_emitted += 1
                    emit(
                        "suggested_change",
                        suggested_fix,
                        report_kind=report_kind,
                        report_issue_block=block_name,
                        issue_severity=severity_raw,
                        issue_title=title,
                        evidence_text=evidence_text,
                        severity_hint=severity_hint,
                    )

    ux = report.get("user_experience")
    if isinstance(ux, dict):
        for text in _iter_unique_capped_strings(ux.get("friction_points"), limit=10):
            emit(
                "confusion_point",
                text,
                report_kind=report_kind,
                report_ux_block="friction_points",
            )
        for text in _iter_unique_capped_strings(ux.get("unclear_points"), limit=10):
            emit(
                "confidence_missing",
                text,
                report_kind=report_kind,
                report_ux_block="unclear_points",
            )
        for text in _iter_unique_capped_strings(ux.get("what_would_help_next_time"), limit=10):
            emit(
                "suggested_change",
                text,
                report_kind=report_kind,
                report_ux_block="what_would_help_next_time",
            )

    for text in _iter_unique_capped_strings(report.get("next_actions"), limit=10):
        emit(
            "suggested_change",
            text,
            report_kind=report_kind,
            report_block="next_actions",
        )
    for text in _iter_unique_capped_strings(report.get("recommendations"), limit=10):
        emit(
            "suggested_change",
            text,
            report_kind=report_kind,
            report_block="recommendations",
        )

    failures_and_fixes = report.get("failures_and_fixes")
    if isinstance(failures_and_fixes, list):
        symptom_seen: set[str] = set()
        fix_seen: set[str] = set()
        symptom_emitted = 0
        fix_emitted = 0

        for entry in failures_and_fixes:
            if symptom_emitted >= 10 and fix_emitted >= 10:
                break
            if not isinstance(entry, dict):
                continue
            symptom = _coerce_string(entry.get("symptom"))
            likely_cause = _coerce_string(entry.get("likely_cause"))
            fix = _coerce_string(entry.get("fix"))

            if symptom is not None and symptom_emitted < 10:
                key = _normalize_dedupe_key(symptom)
                if key not in symptom_seen:
                    symptom_seen.add(key)
                    symptom_emitted += 1
                    emit(
                        "confusion_point",
                        symptom,
                        impact=likely_cause,
                        report_kind=report_kind,
                        report_block="failures_and_fixes",
                    )

            if fix is not None and fix_emitted < 10:
                key = _normalize_dedupe_key(fix)
                if key not in fix_seen:
                    fix_seen.add(key)
                    fix_emitted += 1
                    emit(
                        "suggested_change",
                        fix,
                        report_kind=report_kind,
                        report_block="failures_and_fixes",
                    )

    failure_point = _coerce_string(report.get("failure_point"))
    if failure_point is not None:
        emit(
            "confusion_point",
            failure_point,
            report_kind=report_kind,
            report_block="failure_point",
            severity_hint="high",
        )

    evidence = report.get("evidence")
    if isinstance(evidence, dict):
        what_happened = _coerce_string(evidence.get("what_happened"))
        if what_happened is not None:
            emit(
                "confusion_point",
                what_happened,
                report_kind=report_kind,
                report_block="evidence.what_happened",
                severity_hint="high",
            )

    for key in ("recommended_fix_path", "prevent_recurrence"):
        for text in _iter_unique_capped_strings(report.get(key), limit=10):
            emit(
                "suggested_change",
                text,
                report_kind=report_kind,
                report_block=key,
            )


def extract_backlog_atoms(
    records: list[dict[str, Any]],
    repo_root: Path | None = None,
    capture_policy: TextCapturePolicy | None = None,
) -> dict[str, Any]:
    policy = capture_policy or _default_capture_policy()
    max_command_failure_atoms = _max_command_failure_atoms_per_run()
    atoms: list[dict[str, Any]] = []
    source_counts: Counter[str] = Counter()
    severity_counts: Counter[str] = Counter()
    run_ids: set[str] = set()
    capture_manifest: dict[str, list[dict[str, Any]]] = {}

    for record in records:
        run_dir_raw = record.get("run_dir")
        run_dir = Path(run_dir_raw) if isinstance(run_dir_raw, str) else Path(".")
        run_id = str(record.get("run_rel") or run_dir_raw or f"run_{len(run_ids) + 1}")
        run_rel = str(record.get("run_rel") or run_id)
        run_path_display = str(run_dir).replace("\\", "/")
        if repo_root is not None and isinstance(run_dir_raw, str):
            run_path_display = _safe_relpath(Path(run_dir_raw), repo_root)
        run_ids.add(run_id)

        agent = str(record.get("agent") or "unknown")
        status = str(record.get("status") or "unknown")
        timestamp_utc = record.get("timestamp_utc")
        timestamp_utc_s = timestamp_utc if isinstance(timestamp_utc, str) else None
        target_slug = _coerce_string(record.get("target_slug"))

        repo_input = None
        mission_id = None
        persona_id = None
        target_ref = record.get("target_ref")
        if isinstance(target_ref, dict):
            repo_input = _coerce_string(target_ref.get("repo_input"))
            mission_id = _coerce_string(target_ref.get("mission_id"))
            persona_id = _coerce_string(target_ref.get("persona_id"))

        source_index: Counter[str] = Counter()

        def _emit(
            source: str,
            text: str,
            *,
            _run_id: str = run_id,
            _run_rel: str = run_rel,
            _run_dir: str = run_path_display,
            _agent: str = agent,
            _status: str = status,
            _timestamp_utc: str | None = timestamp_utc_s,
            _target_slug: str | None = target_slug,
            _repo_input: str | None = repo_input,
            _mission_id: str | None = mission_id,
            _persona_id: str | None = persona_id,
            _source_index: Counter[str] = source_index,
            **extras: Any,
        ) -> None:
            cleaned = _clean_atom_text(text)
            if not cleaned:
                return
            _source_index[source] += 1
            atom_id = f"{_run_id}:{source}:{_source_index[source]}"
            priority_hint = _coerce_string(extras.get("priority"))
            severity_hint = _coerce_string(extras.get("severity_hint")) or _infer_severity_hint(
                source=source,
                text=cleaned,
                priority=priority_hint,
            )
            atom: dict[str, Any] = {
                "atom_id": atom_id,
                "run_id": _run_id,
                "run_rel": _run_rel,
                "run_dir": _run_dir,
                "agent": _agent,
                "status": _status,
                "timestamp_utc": _timestamp_utc,
                "source": source,
                "text": cleaned,
                "severity_hint": severity_hint,
                "severity_score_hint": _severity_rank(severity_hint),
            }
            if _target_slug:
                atom["target_slug"] = _target_slug
            if _repo_input:
                atom["repo_input"] = _repo_input
            if _mission_id:
                atom["mission_id"] = _mission_id
            if _persona_id:
                atom["persona_id"] = _persona_id
            for key, value in extras.items():
                if value is None:
                    continue
                if key == "severity_hint":
                    continue
                atom[key] = value
            atoms.append(atom)
            source_counts[source] += 1
            severity_counts[severity_hint] += 1

        metrics_raw = record.get("metrics")
        metrics = metrics_raw if isinstance(metrics_raw, dict) else None

        failed_commands: list[dict[str, Any]] = []
        failed_commands_omitted_hint: int | None = None
        if metrics is not None:
            failed_raw = metrics.get("failed_commands")
            if isinstance(failed_raw, list):
                for item in failed_raw:
                    if not isinstance(item, dict):
                        continue
                    command = _coerce_string(item.get("command"))
                    exit_code = item.get("exit_code")
                    if command is None or not isinstance(exit_code, int) or exit_code == 0:
                        continue
                    failed_commands.append(
                        {
                            "command": command,
                            "exit_code": exit_code,
                            "cwd": _coerce_string(item.get("cwd")),
                            "artifacts": item.get("artifacts")
                            if isinstance(item.get("artifacts"), dict)
                            else None,
                            "output_excerpt": _coerce_string(item.get("output_excerpt")),
                            "output_excerpt_truncated": item.get("output_excerpt_truncated")
                            is True,
                            "from_metrics": True,
                        }
                    )
            if metrics.get("failed_commands_truncated") is True:
                omitted = metrics.get("failed_commands_omitted_count")
                if isinstance(omitted, int) and omitted > 0:
                    failed_commands_omitted_hint = omitted

        if not failed_commands:
            events_path = run_dir / "normalized_events.jsonl"
            if events_path.exists():
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
                            failed_commands.append(
                                {
                                    "command": command,
                                    "exit_code": exit_code,
                                    "cwd": _coerce_string(data.get("cwd")),
                                    "artifacts": data.get("failure_artifacts")
                                    if isinstance(data.get("failure_artifacts"), dict)
                                    else None,
                                    "output_excerpt": _coerce_string(data.get("output_excerpt")),
                                    "output_excerpt_truncated": data.get("output_excerpt_truncated")
                                    is True,
                                    "from_events": True,
                                }
                            )
                            if len(failed_commands) >= max_command_failure_atoms:
                                break
                except OSError:
                    failed_commands = []

        if failed_commands:
            emitted = 0
            for entry in failed_commands:
                if emitted >= max_command_failure_atoms:
                    break
                command = _coerce_string(entry.get("command"))
                exit_code = entry.get("exit_code")
                if command is None or not isinstance(exit_code, int) or exit_code == 0:
                    continue
                output_excerpt = _coerce_string(entry.get("output_excerpt"))
                output_excerpt_truncated = (
                    True if entry.get("output_excerpt_truncated") is True else None
                )
                _emit(
                    "command_failure",
                    f"Command failed: exit_code={exit_code}; command={command}",
                    command=command,
                    exit_code=exit_code,
                    cwd=_coerce_string(entry.get("cwd")),
                    artifacts=entry.get("artifacts")
                    if isinstance(entry.get("artifacts"), dict)
                    else None,
                    output_excerpt=output_excerpt,
                    output_excerpt_truncated=output_excerpt_truncated,
                    from_events=True if entry.get("from_events") else None,
                    from_metrics=True if entry.get("from_metrics") else None,
                )
                emitted += 1

            if failed_commands_omitted_hint is not None and failed_commands_omitted_hint > 0:
                _emit(
                    "command_failure_truncated",
                    (
                        "Command failure list truncated by reporter: omitted "
                        f"{failed_commands_omitted_hint} additional failures."
                    ),
                    omitted_count=failed_commands_omitted_hint,
                    severity_hint="low",
                )

        report = record.get("report")
        if isinstance(report, dict):
            confusion = report.get("confusion_points")
            if isinstance(confusion, list):
                for item in confusion:
                    if not isinstance(item, dict):
                        continue
                    summary = _coerce_string(item.get("summary"))
                    if summary is None:
                        continue
                    impact = _coerce_string(item.get("impact"))
                    evidence = _coerce_evidence_list(item.get("evidence"))
                    _emit(
                        "confusion_point",
                        summary,
                        impact=impact,
                        evidence=evidence if evidence else None,
                    )

            suggested = report.get("suggested_changes")
            if isinstance(suggested, list):
                for item in suggested:
                    if not isinstance(item, dict):
                        continue
                    change = _coerce_string(item.get("change"))
                    if change is None:
                        continue
                    change_type = _coerce_string(item.get("type"))
                    location = _coerce_string(item.get("location"))
                    priority = _coerce_string(item.get("priority"))
                    expected_impact = _coerce_string(item.get("expected_impact"))
                    _emit(
                        "suggested_change",
                        change,
                        type=change_type,
                        location=location,
                        priority=priority,
                        expected_impact=expected_impact,
                        severity_hint=_severity_from_priority(priority),
                    )

            confidence = report.get("confidence_signals")
            if isinstance(confidence, dict):
                for missing in _coerce_string_list(confidence.get("missing")):
                    _emit("confidence_missing", missing)

            report_kind = _coerce_string(report.get("kind"))
            if report_kind is not None:
                _extract_modern_report_atoms(report=report, report_kind=report_kind, emit=_emit)

        validation_values = coerce_validation_errors(record.get("report_validation_errors"))
        sanitized_error = sanitize_error(record.get("error"))
        artifacts = extract_error_artifacts(sanitized_error)
        is_failure, failure_kind = classify_failure_kind(
            status=status,
            error=sanitized_error,
            validation_errors=validation_values,
        )

        run_capture_entries: list[dict[str, Any]] = []
        attachments: list[dict[str, Any]] = []
        for filename, source in (
            ("agent_stderr.txt", "agent_stderr_artifact"),
            ("agent_last_message.txt", "agent_last_message_artifact"),
        ):
            capture = capture_text_artifact(run_dir / filename, policy=policy, root=run_dir)
            run_capture_entries.append(_capture_manifest_entry(capture))
            if is_failure:
                if not capture.artifact.exists and not (
                    isinstance(capture.error, str) and capture.error.strip()
                ):
                    continue
                excerpt_head = capture.excerpt.head if capture.excerpt is not None else None
                excerpt_tail = capture.excerpt.tail if capture.excerpt is not None else None
                truncated = (
                    bool(capture.excerpt.truncated) if capture.excerpt is not None else False
                )
                attachments.append(
                    {
                        "path": capture.artifact.path,
                        "artifact_ref": _artifact_ref_public(capture),
                        "truncated": truncated,
                        "excerpt_head": excerpt_head,
                        "excerpt_tail": excerpt_tail,
                        "capture_error": capture.error,
                    }
                )
                continue
            if not capture.artifact.exists:
                continue
            if (
                source == "agent_stderr_artifact"
                and status.strip().lower() == "ok"
                and (capture.artifact.size_bytes == 0)
            ):
                continue
            excerpt_head = capture.excerpt.head if capture.excerpt is not None else None
            excerpt_tail = capture.excerpt.tail if capture.excerpt is not None else None
            truncated = (
                bool(capture.excerpt.truncated) if capture.excerpt is not None else False
            )
            artifact_text = _clean_atom_text(_compose_artifact_text(capture))
            if not artifact_text:
                artifact_text = "[empty artifact]"
            if source == "agent_stderr_artifact" and status.strip().lower() == "ok":
                warning_meta = classify_known_stderr_warnings(artifact_text)
                warning_only = bool(warning_meta.get("warning_only"))
                warning_codes = warning_meta.get("codes")
                warning_counts = warning_meta.get("counts")
                if warning_only and isinstance(warning_codes, list):
                    if warning_codes == ["shell_snapshot_powershell_unsupported"]:
                        _emit(
                            "capability_notice_artifact",
                            (
                                "Known capability notice in agent stderr: "
                                "PowerShell shell snapshot metadata unavailable (expected)."
                            ),
                            warning_codes=warning_codes,
                            warning_counts=warning_counts
                            if isinstance(warning_counts, dict)
                            else None,
                            excerpt_head=excerpt_head,
                            excerpt_tail=excerpt_tail,
                            truncated=truncated,
                            capture_error=capture.error,
                            artifact_ref=_artifact_ref_public(capture),
                            severity_hint="low",
                        )
                        continue
                    _emit(
                        "capability_warning_artifact",
                        (
                            "Known capability warning(s) in agent stderr: "
                            + ", ".join(str(code) for code in warning_codes)
                        ),
                        warning_codes=warning_codes,
                        warning_counts=warning_counts if isinstance(warning_counts, dict) else None,
                        excerpt_head=excerpt_head,
                        excerpt_tail=excerpt_tail,
                        truncated=truncated,
                        capture_error=capture.error,
                        artifact_ref=_artifact_ref_public(capture),
                        severity_hint="low",
                    )
                    continue
            _emit(
                source,
                artifact_text,
                excerpt_head=excerpt_head,
                excerpt_tail=excerpt_tail,
                truncated=truncated,
                capture_error=capture.error,
                artifact_ref=_artifact_ref_public(capture),
            )
        capture_manifest[run_rel] = run_capture_entries

        if is_failure:
            failure_text = render_failure_text(
                failure_kind=failure_kind,
                agent=agent,
                status=status,
                error=sanitized_error,
                report_validation_errors=validation_values,
                artifacts=artifacts,
                attachments=attachments,
            )
            _emit(
                "run_failure_event",
                failure_text,
                severity_hint="high",
                failure_kind=failure_kind,
                error=sanitized_error,
                report_validation_errors=validation_values,
                artifacts=artifacts,
                attachments=attachments,
            )

    return {
        "atoms": atoms,
        "totals": {
            "runs": len(run_ids),
            "atoms": len(atoms),
            "source_counts": dict(sorted(source_counts.items())),
            "severity_hint_counts": dict(sorted(severity_counts.items())),
        },
        "capture_manifest": capture_manifest,
    }


def add_atom_links(atoms: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Add optional relationship metadata to atoms.

    - Computes `path_anchors` for every atom.
    - For `suggested_change` atoms, adds `linked_atom_ids` pointing to objective evidence atoms
      from the same run when anchors/tokens overlap.
    """

    evidence_sources = {
        "command_failure",
        "run_failure_event",
        "report_validation_error",
        "confusion_point",
        "confidence_missing",
    }

    atoms_by_run: dict[str, list[dict[str, Any]]] = {}
    anchors_by_id: dict[str, set[str]] = {}
    tokens_by_id: dict[str, set[str]] = {}

    for atom in atoms:
        run_rel = _coerce_string(atom.get("run_rel"))
        atom_id = _coerce_string(atom.get("atom_id"))
        if run_rel is None or atom_id is None:
            continue

        chunks: list[str] = []
        for key in ("text", "impact", "location", "evidence_text"):
            value = atom.get(key)
            if isinstance(value, str) and value.strip():
                chunks.append(value)

        anchors = extract_path_anchors_from_chunks(chunks) if chunks else set()
        # `extract_path_anchors_from_chunks` intentionally prefers recall for path-like strings
        # with separators. Add a minimal fallback for bare filenames like `README.md`.
        for chunk in chunks:
            for candidate in re.findall(r"[A-Za-z0-9_.-]+\.[A-Za-z0-9]{1,6}", chunk):
                if not any(ch.isalpha() for ch in candidate):
                    continue
                anchors.add(candidate.lower().replace("\\", "/"))
        tokens: set[str] = set()
        for chunk in chunks:
            tokens |= tokenize(chunk)

        atom["path_anchors"] = sorted(anchors)
        anchors_by_id[atom_id] = set(anchors)
        tokens_by_id[atom_id] = tokens
        atoms_by_run.setdefault(run_rel, []).append(atom)

    def _jaccard(a: set[str], b: set[str]) -> float:
        if not a and not b:
            return 0.0
        union = a | b
        if not union:
            return 0.0
        return len(a & b) / len(union)

    def _meaningful_token_overlap(a: set[str], b: set[str]) -> bool:
        overlap = len(a & b)
        if overlap < 2:
            return False
        return _jaccard(a, b) >= 0.2

    for _run_rel, run_atoms in atoms_by_run.items():
        evidence_atoms = [
            atom
            for atom in run_atoms
            for source in [_coerce_string(atom.get("source"))]
            if source in evidence_sources
        ]

        for atom in run_atoms:
            if _coerce_string(atom.get("source")) != "suggested_change":
                continue
            atom_id = _coerce_string(atom.get("atom_id"))
            if atom_id is None:
                continue

            src_anchors = anchors_by_id.get(atom_id, set())
            src_tokens = tokens_by_id.get(atom_id, set())

            scored: list[tuple[float, float, int, str]] = []
            for candidate in evidence_atoms:
                cand_id = _coerce_string(candidate.get("atom_id"))
                if cand_id is None or cand_id == atom_id:
                    continue
                cand_anchors = anchors_by_id.get(cand_id, set())
                cand_tokens = tokens_by_id.get(cand_id, set())

                anchor_score = _jaccard(src_anchors, cand_anchors)
                token_score = _jaccard(src_tokens, cand_tokens)
                token_overlap = len(src_tokens & cand_tokens)

                if anchor_score <= 0.0 and not _meaningful_token_overlap(src_tokens, cand_tokens):
                    continue
                scored.append((anchor_score, token_score, token_overlap, cand_id))

            scored.sort(key=lambda item: (-item[0], -item[1], -item[2], item[3]))
            atom["linked_atom_ids"] = [cand_id for _, _, _, cand_id in scored[:3]]

    return atoms


def write_backlog_atoms(atoms_doc: dict[str, Any], out_jsonl_path: Path) -> None:
    out_jsonl_path.parent.mkdir(parents=True, exist_ok=True)
    atoms = atoms_doc.get("atoms")
    atom_list = atoms if isinstance(atoms, list) else []
    with out_jsonl_path.open("w", encoding="utf-8", newline="\n") as f:
        for atom in atom_list:
            if isinstance(atom, dict):
                f.write(json.dumps(atom, ensure_ascii=False) + "\n")


def _extract_first_json_array(text: str) -> list[Any] | None:
    decoder = JSONDecoder()
    for idx, char in enumerate(text):
        if char != "[":
            continue
        try:
            parsed, _ = decoder.raw_decode(text[idx:])
        except Exception:  # noqa: BLE001
            continue
        if isinstance(parsed, list):
            return parsed
    return None


def _normalize_ticket(
    raw: dict[str, Any],
    *,
    index: int,
) -> tuple[dict[str, Any] | None, str | None]:
    title = _coerce_string(raw.get("title"))
    if title is None:
        return None, f"tickets[{index}] missing required non-empty field: title"

    evidence_ids = _coerce_string_list(raw.get("evidence_atom_ids"))
    if not evidence_ids:
        return None, f"tickets[{index}] missing required evidence_atom_ids"

    severity = (_coerce_string(raw.get("severity")) or "medium").lower()
    if severity not in _SEVERITY_ORDER:
        severity = "medium"

    investigation_steps = _coerce_string_list(raw.get("investigation_steps"))
    success_criteria = _coerce_string_list(raw.get("success_criteria"))
    proposed_fix = _coerce_string(raw.get("proposed_fix"))

    normalized: dict[str, Any] = {
        "title": title,
        "problem": _coerce_string(raw.get("problem")) or "",
        "user_impact": _coerce_string(raw.get("user_impact")) or "",
        "severity": severity,
        "confidence": _coerce_confidence(raw.get("confidence")),
        "evidence_atom_ids": sorted(set(evidence_ids)),
        "investigation_steps": investigation_steps,
        "success_criteria": success_criteria,
    }
    if proposed_fix is not None:
        normalized["proposed_fix"] = proposed_fix
    suggested_owner = _coerce_string(raw.get("suggested_owner"))
    if suggested_owner is not None:
        normalized["suggested_owner"] = suggested_owner
    if not investigation_steps and proposed_fix is None:
        return None, f"tickets[{index}] should include investigation_steps or proposed_fix"
    return normalized, None


def parse_ticket_list(text: str) -> tuple[list[dict[str, Any]], list[str]]:
    raw = text.strip()
    if not raw:
        return [], ["empty output"]

    errors: list[str] = []
    parsed: Any | None = None

    try:
        parsed = json.loads(raw)
    except Exception as e:  # noqa: BLE001
        errors.append(f"json_parse_failed: {e}")

    if parsed is None:
        extracted = _extract_first_json_array(raw)
        if extracted is not None:
            parsed = extracted

    if isinstance(parsed, dict) and isinstance(parsed.get("tickets"), list):
        parsed = parsed.get("tickets")

    if not isinstance(parsed, list):
        errors.append("could not locate a JSON ticket array")
        return [], errors

    out: list[dict[str, Any]] = []
    for idx, item in enumerate(parsed):
        if not isinstance(item, dict):
            errors.append(f"tickets[{idx}] is not an object")
            continue
        normalized, error = _normalize_ticket(item, index=idx)
        if error is not None:
            errors.append(error)
            continue
        if normalized is not None:
            out.append(normalized)
    return out, errors


def _merge_string_lists(a: list[str], b: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in [*a, *b]:
        if value in seen:
            continue
        out.append(value)
        seen.add(value)
    return out


def _merge_two_tickets(base: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)

    merged["evidence_atom_ids"] = sorted(
        set(_coerce_string_list(base.get("evidence_atom_ids")))
        | set(_coerce_string_list(incoming.get("evidence_atom_ids")))
    )

    base_severity = (_coerce_string(base.get("severity")) or "medium").lower()
    incoming_severity = (_coerce_string(incoming.get("severity")) or "medium").lower()
    merged["severity"] = (
        incoming_severity
        if _severity_rank(incoming_severity) > _severity_rank(base_severity)
        else base_severity
    )

    merged["confidence"] = max(
        _coerce_confidence(base.get("confidence")),
        _coerce_confidence(incoming.get("confidence")),
    )

    merged["investigation_steps"] = _merge_string_lists(
        _coerce_string_list(base.get("investigation_steps")),
        _coerce_string_list(incoming.get("investigation_steps")),
    )
    merged["success_criteria"] = _merge_string_lists(
        _coerce_string_list(base.get("success_criteria")),
        _coerce_string_list(incoming.get("success_criteria")),
    )

    base_fix = _coerce_string(base.get("proposed_fix"))
    incoming_fix = _coerce_string(incoming.get("proposed_fix"))
    if base_fix is None and incoming_fix is not None:
        merged["proposed_fix"] = incoming_fix
    elif base_fix is not None:
        merged["proposed_fix"] = base_fix

    if not _coerce_string(merged.get("problem")):
        merged["problem"] = _coerce_string(incoming.get("problem")) or ""
    if not _coerce_string(merged.get("user_impact")):
        merged["user_impact"] = _coerce_string(incoming.get("user_impact")) or ""
    if not _coerce_string(merged.get("suggested_owner")):
        owner = _coerce_string(incoming.get("suggested_owner"))
        if owner is not None:
            merged["suggested_owner"] = owner

    merged["merged_count"] = int(base.get("merged_count", 1)) + int(incoming.get("merged_count", 1))
    return merged


def dedupe_tickets(
    tickets: list[dict[str, Any]],
    *,
    embedder: _Embedder | None = None,
) -> list[dict[str, Any]]:
    if not tickets:
        return []

    def _chunks(ticket: dict[str, Any]) -> list[str]:
        change_surface = ticket.get("change_surface")
        cs = change_surface if isinstance(change_surface, dict) else {}
        kinds = _coerce_string_list(cs.get("kinds"))
        notes = _coerce_string(cs.get("notes"))
        out: list[str] = [
            _coerce_string(ticket.get("title")) or "",
            _coerce_string(ticket.get("problem")) or "",
            _coerce_string(ticket.get("user_impact")) or "",
            _coerce_string(ticket.get("proposed_fix")) or "",
            _coerce_string(ticket.get("suggested_owner")) or "",
            *(kinds or []),
        ]
        if notes:
            out.append(notes)
        out.extend(_coerce_string_list(ticket.get("investigation_steps")))
        out.extend(_coerce_string_list(ticket.get("success_criteria")))
        return [chunk for chunk in out if chunk]

    clusters = _dedupe_clusters(
        tickets,
        get_title=lambda ticket: _coerce_string(ticket.get("title")) or "",
        get_text_chunks=_chunks,
        get_evidence_ids=lambda ticket: _coerce_string_list(ticket.get("evidence_atom_ids")),
        include_singletons=True,
        embedder=embedder,
    )

    deduped: list[dict[str, Any]] = []
    for cluster in clusters:
        if not cluster:
            continue
        base = dict(tickets[cluster[0]])
        base["merged_count"] = int(base.get("merged_count", 1))
        for idx in cluster[1:]:
            base = _merge_two_tickets(base, tickets[idx])
        deduped.append(base)

    return deduped


def _preview_atom(atom: dict[str, Any]) -> dict[str, Any]:
    text = _coerce_string(atom.get("text")) or ""
    if len(text) > 200:
        text = text[:200] + "..."
    return {
        "atom_id": atom.get("atom_id"),
        "run_rel": atom.get("run_rel"),
        "source": atom.get("source"),
        "severity_hint": atom.get("severity_hint"),
        "text": text,
    }


def compute_backlog_coverage(
    atoms: list[dict[str, Any]],
    tickets: list[dict[str, Any]],
    *,
    preview_limit: int = 25,
) -> dict[str, Any]:
    valid_atom_ids = {
        atom_id for atom in atoms for atom_id in [_coerce_string(atom.get("atom_id"))] if atom_id
    }
    covered_atom_ids: set[str] = set()
    for ticket in tickets:
        for atom_id in _coerce_string_list(ticket.get("evidence_atom_ids")):
            if atom_id in valid_atom_ids:
                covered_atom_ids.add(atom_id)

    uncovered_atoms = [
        atom
        for atom in atoms
        if (_coerce_string(atom.get("atom_id")) or "") not in covered_atom_ids
    ]
    uncovered_high = [
        atom
        for atom in uncovered_atoms
        if (_coerce_string(atom.get("severity_hint")) or "medium") in {"high", "blocker"}
    ]

    denominator = len(valid_atom_ids)
    coverage_ratio = (len(covered_atom_ids) / denominator) if denominator > 0 else 1.0
    uncovered_high_atom_ids = [
        atom_id
        for atom in uncovered_high
        for atom_id in [_coerce_string(atom.get("atom_id"))]
        if atom_id
    ]
    return {
        "covered_atoms": len(covered_atom_ids),
        "uncovered_atoms": len(uncovered_atoms),
        "coverage_ratio": coverage_ratio,
        "uncovered_high_severity_atoms": len(uncovered_high),
        "uncovered_high_severity_atom_ids": uncovered_high_atom_ids,
        "uncovered_high_severity_atoms_preview": [
            _preview_atom(atom) for atom in uncovered_high[:preview_limit]
        ],
        "uncovered_preview": [_preview_atom(atom) for atom in uncovered_atoms[:preview_limit]],
    }


def enrich_tickets_with_atom_context(
    tickets: list[dict[str, Any]],
    atoms: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    atoms_by_id: dict[str, dict[str, Any]] = {}
    for atom in atoms:
        atom_id = _coerce_string(atom.get("atom_id"))
        if atom_id:
            atoms_by_id[atom_id] = atom
    enriched: list[dict[str, Any]] = []
    for ticket in tickets:
        evidence_ids = _coerce_string_list(ticket.get("evidence_atom_ids"))
        evidence_atoms = [
            atoms_by_id[atom_id] for atom_id in evidence_ids if atom_id in atoms_by_id
        ]
        runs_citing = sorted(
            {
                run_rel
                for atom in evidence_atoms
                for run_rel in [_coerce_string(atom.get("run_rel"))]
                if run_rel
            }
        )
        agents_citing = sorted(
            {
                agent
                for atom in evidence_atoms
                for agent in [_coerce_string(atom.get("agent"))]
                if agent
            }
        )
        missions_citing = sorted(
            {
                mission
                for atom in evidence_atoms
                for mission in [_coerce_string(atom.get("mission_id"))]
                if mission
            }
        )
        targets_citing = sorted(
            {
                target
                for atom in evidence_atoms
                for target in [_coerce_string(atom.get("target_slug"))]
                if target
            }
        )
        repo_inputs_citing = sorted(
            {
                repo_input
                for atom in evidence_atoms
                for repo_input in [_coerce_string(atom.get("repo_input"))]
                if repo_input
            }
        )
        personas_citing = sorted(
            {
                persona
                for atom in evidence_atoms
                for persona in [_coerce_string(atom.get("persona_id"))]
                if persona
            }
        )
        preview = [_preview_atom(atom) for atom in evidence_atoms[:5]]

        item = dict(ticket)
        change_surface = item.get("change_surface")
        if not isinstance(change_surface, dict):
            change_surface = {}
        kinds = _coerce_string_list(change_surface.get("kinds"))
        if not kinds:
            kinds = ["unknown"]
        item["change_surface"] = {
            "user_visible": bool(change_surface.get("user_visible")),
            "kinds": kinds,
            "notes": _coerce_string(change_surface.get("notes")) or "",
        }

        severity = (_coerce_string(item.get("severity")) or "medium").lower()
        if severity not in _SEVERITY_ORDER:
            severity = "medium"
        item["severity"] = severity

        stage = _coerce_string(item.get("stage")) or "triage"
        risks = _coerce_string_list(item.get("risks"))

        # Evidence gating:
        # - Below high severity cannot be exported/actioned
        #   unless evidence spans >= 2 distinct runs.
        # - Low severity additionally requires evidence from >= 2 distinct agents/models.
        if severity in {"low", "medium"} and len(runs_citing) < 2:
            stage = "blocked"
            if "insufficient_run_breadth_for_non_high_severity" not in risks:
                risks.append("insufficient_run_breadth_for_non_high_severity")
        if severity == "low" and len(agents_citing) < 2:
            stage = "blocked"
            if "insufficient_model_breadth_for_low_severity" not in risks:
                risks.append("insufficient_model_breadth_for_low_severity")

        item["stage"] = stage
        item["risks"] = risks
        item["breadth"] = {
            "runs": len(runs_citing),
            "missions": len(missions_citing),
            "targets": len(targets_citing),
            "repo_inputs": len(repo_inputs_citing),
            "agents": len(agents_citing),
            "personas": len(personas_citing),
        }
        item["runs_citing"] = len(runs_citing)
        item["run_refs"] = runs_citing
        item["repo_inputs_citing"] = repo_inputs_citing
        item["agents_citing"] = agents_citing

        trust_evidence: list[_TrustEvidence] = []
        for atom in evidence_atoms:
            atom_id = _coerce_string(atom.get("atom_id")) or None
            run_rel = _coerce_string(atom.get("run_rel")) or None
            agent = _coerce_string(atom.get("agent")) or None
            kind = _coerce_string(atom.get("source")) or None
            weight = _TRUST_SOURCE_WEIGHTS.get(kind or "", 0.55)
            trust_evidence.append(
                _TrustEvidence(
                    evidence_id=atom_id,
                    group=run_rel,
                    source=agent,
                    kind=kind,
                    weight=weight,
                )
            )
        trust = _assess_trust(trust_evidence, confidence=_coerce_confidence(item.get("confidence")))
        item["trust"] = trust.to_dict()

        item["evidence_atoms_preview"] = preview
        enriched.append(item)
    return enriched


def _ticket_sort_key(ticket: dict[str, Any]) -> tuple[int, int, int, float, str]:
    severity = (_coerce_string(ticket.get("severity")) or "medium").lower()
    runs_citing = int(ticket.get("runs_citing", 0))
    evidence_count = len(_coerce_string_list(ticket.get("evidence_atom_ids")))
    confidence = _coerce_confidence(ticket.get("confidence"))
    title = (_coerce_string(ticket.get("title")) or "").lower()
    return (-_severity_rank(severity), -runs_citing, -evidence_count, -confidence, title)


def build_backlog_document(
    *,
    atoms_doc: dict[str, Any],
    tickets: list[dict[str, Any]],
    input_meta: dict[str, Any],
    artifacts: dict[str, Any] | None = None,
    miners_meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    atoms_raw = atoms_doc.get("atoms")
    atoms = (
        [item for item in atoms_raw if isinstance(item, dict)]
        if isinstance(atoms_raw, list)
        else []
    )

    enriched = enrich_tickets_with_atom_context(tickets, atoms)
    ordered = sorted(enriched, key=_ticket_sort_key)
    for idx, ticket in enumerate(ordered, start=1):
        ticket["ticket_id"] = f"BLG-{idx:03d}"

    coverage = compute_backlog_coverage(atoms, ordered)
    miners = miners_meta or {}
    source_counts = {}
    severity_hint_counts = {}
    totals_raw = atoms_doc.get("totals")
    if isinstance(totals_raw, dict):
        source_counts_raw = totals_raw.get("source_counts")
        if isinstance(source_counts_raw, dict):
            source_counts = source_counts_raw
        severity_counts_raw = totals_raw.get("severity_hint_counts")
        if isinstance(severity_counts_raw, dict):
            severity_hint_counts = severity_counts_raw
    artifacts_payload = dict(artifacts or {})
    capture_manifest = atoms_doc.get("capture_manifest")
    if isinstance(capture_manifest, dict):
        artifacts_payload["capture_manifest"] = capture_manifest

    return {
        "generated_at_utc": datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z"),
        "input": input_meta,
        "totals": {
            "runs": len(
                {
                    str(run_rel)
                    for atom in atoms
                    for run_rel in [atom.get("run_rel")]
                    if isinstance(run_rel, str)
                    and run_rel
                    and not run_rel.startswith("__aggregate__/")
                }
            ),
            "atoms": len(atoms),
            "tickets": len(ordered),
            "source_counts": source_counts,
            "severity_hint_counts": severity_hint_counts,
            "miners_total": int(miners.get("miners_total", 0)),
            "miners_completed": int(miners.get("miners_completed", 0)),
            "miners_failed": int(miners.get("miners_failed", 0)),
            "merge_decisions": int(miners.get("merge_decisions", 0)),
        },
        "tickets": ordered,
        "coverage": coverage,
        "artifacts": artifacts_payload,
    }


def render_backlog_markdown(
    summary: dict[str, Any],
    *,
    title: str = "Usertest Backlog",
) -> str:
    lines: list[str] = []
    lines.append(f"# {title}")
    lines.append("")

    generated = summary.get("generated_at_utc")
    if isinstance(generated, str):
        lines.append(f"Generated: `{generated}`")
        lines.append("")

    totals = summary.get("totals")
    totals_dict = totals if isinstance(totals, dict) else {}
    lines.append("## Summary")
    lines.append(f"- Runs: **{int(totals_dict.get('runs', 0))}**")
    lines.append(f"- Atoms: **{int(totals_dict.get('atoms', 0))}**")
    lines.append(f"- Tickets: **{int(totals_dict.get('tickets', 0))}**")

    coverage = summary.get("coverage")
    coverage_dict = coverage if isinstance(coverage, dict) else {}
    lines.append(
        "- Coverage: "
        f"covered=**{int(coverage_dict.get('covered_atoms', 0))}**, "
        f"uncovered=**{int(coverage_dict.get('uncovered_atoms', 0))}**"
    )
    lines.append("")

    tickets = summary.get("tickets")
    ticket_list = (
        [item for item in tickets if isinstance(item, dict)] if isinstance(tickets, list) else []
    )
    lines.append("## Tickets")
    if not ticket_list:
        lines.append("- No backlog tickets were produced.")
        lines.append("")
    else:
        for ticket in ticket_list:
            ticket_id = _coerce_string(ticket.get("ticket_id")) or "BLG-???"
            title_s = _coerce_string(ticket.get("title")) or "Untitled"
            severity = (_coerce_string(ticket.get("severity")) or "medium").lower()
            confidence = _coerce_confidence(ticket.get("confidence"))
            runs_citing = int(ticket.get("runs_citing", 0))
            lines.append(f"### {ticket_id}: {title_s}")
            lines.append(
                f"- Severity: `{severity}` | Confidence: `{confidence:.2f}` | "
                f"Runs citing: `{runs_citing}`"
            )

            trust = ticket.get("trust")
            trust_dict = trust if isinstance(trust, dict) else {}
            trust_level = (_coerce_string(trust_dict.get("level")) or "").lower()
            trust_score_raw = trust_dict.get("score")
            trust_score = (
                float(trust_score_raw)
                if isinstance(trust_score_raw, (int, float))
                else None
            )
            if trust_level and trust_score is not None:
                lines.append(f"- Trust: `{trust_level}` ({trust_score:.2f})")

            stage = _coerce_string(ticket.get("stage")) or "triage"
            lines.append(f"- Stage: `{stage}`")

            change_surface = ticket.get("change_surface")
            cs = change_surface if isinstance(change_surface, dict) else {}
            kinds = _coerce_string_list(cs.get("kinds")) or ["unknown"]
            user_visible = bool(cs.get("user_visible"))
            notes = _coerce_string(cs.get("notes")) or ""
            kinds_s = ", ".join(f"`{kind}`" for kind in kinds)
            lines.append(f"- Change surface: user_visible=`{user_visible}`; kinds={kinds_s}")
            if notes:
                lines.append(f"- Change surface notes: {notes}")

            breadth = ticket.get("breadth")
            breadth_dict = breadth if isinstance(breadth, dict) else {}
            lines.append(
                "- Breadth: "
                f"missions=`{int(breadth_dict.get('missions', 0))}`, "
                f"targets=`{int(breadth_dict.get('targets', 0))}`, "
                f"repo_inputs=`{int(breadth_dict.get('repo_inputs', 0))}`, "
                f"agents=`{int(breadth_dict.get('agents', 0))}`, "
                f"personas=`{int(breadth_dict.get('personas', 0))}`"
            )
            risks = _coerce_string_list(ticket.get("risks"))
            if risks:
                risks_s = ", ".join(f"`{risk}`" for risk in risks)
                lines.append(f"- Risks: {risks_s}")

            problem = _coerce_string(ticket.get("problem"))
            if problem:
                lines.append(f"- Problem: {problem}")
            user_impact = _coerce_string(ticket.get("user_impact"))
            if user_impact:
                lines.append(f"- User impact: {user_impact}")
            proposed_fix = _coerce_string(ticket.get("proposed_fix"))
            if proposed_fix:
                lines.append(f"- Proposed fix: {proposed_fix}")

            investigation_steps = _coerce_string_list(ticket.get("investigation_steps"))
            if investigation_steps:
                lines.append("- Investigation steps:")
                for step in investigation_steps[:6]:
                    lines.append(f"  - {step}")

            success_criteria = _coerce_string_list(ticket.get("success_criteria"))
            if success_criteria:
                lines.append("- Success criteria:")
                for criterion in success_criteria[:6]:
                    lines.append(f"  - {criterion}")

            evidence_preview = ticket.get("evidence_atoms_preview")
            if isinstance(evidence_preview, list) and evidence_preview:
                lines.append("- Evidence preview:")
                for atom in evidence_preview[:6]:
                    if not isinstance(atom, dict):
                        continue
                    atom_id = _coerce_string(atom.get("atom_id")) or "unknown"
                    run_rel = _coerce_string(atom.get("run_rel")) or "unknown"
                    source = _coerce_string(atom.get("source")) or "unknown"
                    text = _coerce_string(atom.get("text")) or ""
                    lines.append(f"  - `{atom_id}` from `{run_rel}` (`{source}`): {text}")
            lines.append("")

    lines.append("## Untriaged Tail")
    lines.append("")
    uncovered = coverage_dict.get("uncovered_preview")
    uncovered_list = (
        [item for item in uncovered if isinstance(item, dict)]
        if isinstance(uncovered, list)
        else []
    )
    if not uncovered_list:
        lines.append("- No uncovered atoms.")
        lines.append("")
    else:
        for atom in uncovered_list[:40]:
            atom_id = _coerce_string(atom.get("atom_id")) or "unknown"
            run_rel = _coerce_string(atom.get("run_rel")) or "unknown"
            source = _coerce_string(atom.get("source")) or "unknown"
            severity_hint = _coerce_string(atom.get("severity_hint")) or "medium"
            text = _coerce_string(atom.get("text")) or ""
            lines.append(
                f"- `{atom_id}` (`{run_rel}` / `{source}` / severity `{severity_hint}`): {text}"
            )
        lines.append("")

    return "\n".join(lines)


def write_backlog(
    summary: dict[str, Any],
    *,
    out_json_path: Path,
    out_md_path: Path,
    title: str = "Usertest Backlog",
) -> None:
    out_json_path.parent.mkdir(parents=True, exist_ok=True)
    out_md_path.parent.mkdir(parents=True, exist_ok=True)

    out_json_path.write_text(
        json.dumps(summary, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    out_md_path.write_text(
        render_backlog_markdown(summary, title=title),
        encoding="utf-8",
    )


def build_merge_candidates(
    tickets: list[dict[str, Any]],
    *,
    max_candidates: int = 200,
    overall_similarity_threshold: float | None = None,
    keep_anchor_pairs: bool = False,
    embedder: _Embedder | None = None,
) -> list[tuple[int, int]]:
    def _ticket_title(ticket: dict[str, Any]) -> str:
        return _coerce_string(ticket.get("title")) or ""

    def _ticket_evidence(ticket: dict[str, Any]) -> list[str]:
        return _coerce_string_list(ticket.get("evidence_atom_ids"))

    def _ticket_text_chunks(ticket: dict[str, Any]) -> list[str]:
        chunks: list[str] = []
        for key in (
            "title",
            "problem",
            "user_impact",
            "proposed_fix",
            "suggested_owner",
        ):
            value = _coerce_string(ticket.get(key))
            if value:
                chunks.append(value)

        change_surface = ticket.get("change_surface")
        cs = change_surface if isinstance(change_surface, dict) else {}
        chunks.extend(_coerce_string_list(cs.get("kinds")))
        cs_notes = _coerce_string(cs.get("notes"))
        if cs_notes:
            chunks.append(cs_notes)

        chunks.extend(_coerce_string_list(ticket.get("investigation_steps")))
        chunks.extend(_coerce_string_list(ticket.get("success_criteria")))
        return chunks

    return _build_candidates(
        tickets,
        get_title=_ticket_title,
        get_evidence_ids=_ticket_evidence,
        get_text_chunks=_ticket_text_chunks,
        max_candidates=max_candidates,
        overall_similarity_threshold=overall_similarity_threshold,
        keep_anchor_pairs=keep_anchor_pairs,
        embedder=embedder,
    )
