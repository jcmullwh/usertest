from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from usertest_implement.shared import _utc_now_z, _write_json

PIPELINE_EFFICIENCY_ARTIFACT_NAME = "pipeline_efficiency.json"

_SYSTEM_ORIGINS = {
    "automatic",
    "automated",
    "runner",
    "system",
    "self_healing",
    "system_self_correction",
}
_EXTERNAL_ORIGINS = {"external", "human", "manual", "user", "external_manual"}
_ORIGIN_FIELDS = (
    "correction_origin",
    "instruction_origin",
    "intervention_origin",
    "trigger_origin",
)


def _read_json(path: Path) -> Any | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError):
        return None


def _clean_str(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def _parse_utc(value: Any) -> datetime | None:
    text = _clean_str(value)
    if text is None:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _path_from(value: Any) -> Path | None:
    text = _clean_str(value)
    if text is None:
        return None
    try:
        return Path(text).expanduser().resolve()
    except (OSError, RuntimeError):
        return None


def _add_link(links: list[Path], value: Any) -> None:
    candidate = _path_from(value)
    if candidate is not None and candidate.is_dir() and candidate not in links:
        links.append(candidate)


def _linked_run_dirs(*, run_dir: Path, review_run_dir: Path | None) -> list[Path]:
    """Follow only durable, explicit run-lineage pointers.

    The traversal deliberately does not scan sibling directories or infer ticket
    identity from names. That keeps unrelated runs out of one ticket's metrics.
    """

    pending = [run_dir.resolve()]
    if review_run_dir is not None:
        pending.append(review_run_dir.resolve())
    found: list[Path] = []
    while pending and len(found) < 64:
        candidate = pending.pop(0)
        if candidate in found or not candidate.is_dir():
            continue
        found.append(candidate)

        resume_state = _read_json(candidate / "ticket_resume_state.json")
        if isinstance(resume_state, dict):
            _add_link(pending, resume_state.get("resumed_from_run_dir"))
            _add_link(pending, resume_state.get("review_run_dir"))

        resume_ref = _read_json(candidate / "resume_ref.json")
        if isinstance(resume_ref, dict):
            _add_link(pending, resume_ref.get("resumed_from_run_dir"))

        adoption_ref = _read_json(candidate / "adoption_ref.json")
        if isinstance(adoption_ref, dict):
            _add_link(pending, adoption_ref.get("source_run_dir"))

        review_ref = _read_json(candidate / "review_ref.json")
        if isinstance(review_ref, dict):
            _add_link(pending, review_ref.get("implementation_run_dir"))
            _add_link(pending, review_ref.get("correction_of_review_run_dir"))
            _add_link(pending, review_ref.get("adopted_from_review_run_dir"))

        review_summary = _read_json(candidate / "review_summary.json")
        if isinstance(review_summary, dict):
            _add_link(pending, review_summary.get("correction_of_review_run_dir"))

    return found


def _timestamp_evidence(
    *, run_dirs: list[Path], resume_state: dict[str, Any]
) -> list[dict[str, Any]]:
    fields_by_filename = {
        "run_meta.json": ("run_started_utc", "run_finished_utc"),
        "agent_attempts.json": (),
        "adoption_ref.json": ("adopted_at_utc",),
        "review_summary.json": ("reviewed_at_utc",),
        "merge_ref.json": ("merged_at_utc",),
        "outcome_progression.json": ("generated_at_utc",),
    }
    evidence: list[dict[str, Any]] = []
    for directory in run_dirs:
        for filename, fields in fields_by_filename.items():
            path = directory / filename
            payload = _read_json(path)
            if not isinstance(payload, dict):
                continue
            for field in fields:
                parsed = _parse_utc(payload.get(field))
                if parsed is not None:
                    evidence.append(
                        {
                            "timestamp": parsed,
                            "timestamp_utc": parsed.isoformat().replace("+00:00", "Z"),
                            "path": str(path),
                            "field": field,
                        }
                    )
            if filename == "agent_attempts.json":
                attempts = payload.get("attempts")
                if not isinstance(attempts, list):
                    continue
                for index, attempt in enumerate(attempts):
                    if not isinstance(attempt, dict):
                        continue
                    for field in ("attempt_started_utc", "attempt_finished_utc"):
                        parsed = _parse_utc(attempt.get(field))
                        if parsed is not None:
                            evidence.append(
                                {
                                    "timestamp": parsed,
                                    "timestamp_utc": parsed.isoformat().replace(
                                        "+00:00", "Z"
                                    ),
                                    "path": str(path),
                                    "field": f"attempts[{index}].{field}",
                                }
                            )

    generated = _parse_utc(resume_state.get("generated_at_utc"))
    if generated is not None:
        evidence.append(
            {
                "timestamp": generated,
                "timestamp_utc": generated.isoformat().replace("+00:00", "Z"),
                "path": str(Path(str(resume_state.get("run_dir"))) / "ticket_resume_state.json"),
                "field": "generated_at_utc",
            }
        )
    return evidence


def _run_intervals(run_dirs: list[Path]) -> list[tuple[datetime, datetime]]:
    intervals: list[tuple[datetime, datetime]] = []
    for directory in run_dirs:
        payload = _read_json(directory / "run_meta.json")
        if not isinstance(payload, dict):
            continue
        started = _parse_utc(payload.get("run_started_utc"))
        finished = _parse_utc(payload.get("run_finished_utc"))
        if started is not None and finished is not None and finished >= started:
            intervals.append((started, finished))
    return intervals


def _union_seconds(intervals: list[tuple[datetime, datetime]]) -> float:
    if not intervals:
        return 0.0
    ordered = sorted(intervals)
    current_start, current_end = ordered[0]
    seconds = 0.0
    for start, end in ordered[1:]:
        if start <= current_end:
            current_end = max(current_end, end)
            continue
        seconds += (current_end - current_start).total_seconds()
        current_start, current_end = start, end
    return seconds + (current_end - current_start).total_seconds()


def _elapsed_metrics(
    *, run_dirs: list[Path], resume_state: dict[str, Any]
) -> dict[str, Any]:
    evidence = sorted(
        _timestamp_evidence(run_dirs=run_dirs, resume_state=resume_state),
        key=lambda item: item["timestamp"],
    )
    if not evidence:
        return {
            "status": "unknown",
            "elapsed_wall_seconds": None,
            "started_at_utc": None,
            "observed_through_utc": None,
            "recorded_run_wall_seconds": None,
            "outside_recorded_runs_seconds": None,
            "reason": "No parseable durable timestamps were linked to this ticket.",
            "evidence": [],
        }
    last = max(evidence, key=lambda item: item["timestamp"])
    start_evidence = [
        item
        for item in evidence
        if item["field"] == "run_started_utc"
        or str(item["field"]).endswith(".attempt_started_utc")
    ]
    if not start_evidence:
        return {
            "status": "unknown",
            "elapsed_wall_seconds": None,
            "started_at_utc": None,
            "observed_through_utc": last["timestamp_utc"],
            "recorded_run_wall_seconds": None,
            "outside_recorded_runs_seconds": None,
            "reason": "A durable observation time exists, but no linked execution start exists.",
            "evidence": [
                {key: value for key, value in item.items() if key != "timestamp"}
                for item in evidence
            ],
        }
    first = min(start_evidence, key=lambda item: item["timestamp"])
    elapsed = (last["timestamp"] - first["timestamp"]).total_seconds()
    run_seconds = _union_seconds(_run_intervals(run_dirs))
    return {
        "status": "observed",
        "elapsed_wall_seconds": elapsed,
        "started_at_utc": first["timestamp_utc"],
        "observed_through_utc": last["timestamp_utc"],
        "recorded_run_wall_seconds": run_seconds,
        "outside_recorded_runs_seconds": max(0.0, elapsed - run_seconds),
        "outside_recorded_runs_semantics": (
            "Calendar time not covered by linked run_meta intervals; it can include CI, queues, "
            "review, supervision, and missing run telemetry, so it is not labeled idle."
        ),
        "evidence": [
            {key: value for key, value in item.items() if key != "timestamp"}
            for item in evidence
        ],
    }


def _model_from_argv(argv: Any) -> str | None:
    if not isinstance(argv, list):
        return None
    for index, value in enumerate(argv[:-1]):
        if value == "--model":
            return _clean_str(argv[index + 1])
    return None


def _count_turns(path: Path) -> int | None:
    try:
        count = 0
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(event, dict) and event.get("type") == "turn.started":
                    count += 1
        return count
    except (FileNotFoundError, OSError, UnicodeError):
        return None


def _model_metrics(run_dirs: list[Path]) -> dict[str, Any]:
    invocation_count = 0
    turns = 0
    turn_evidence_complete = True
    expected_invocation_artifacts = 0
    observed_invocation_artifacts = 0
    missing_expected_artifacts = 0
    models: dict[str, int] = {}
    sessions: set[str] = set()
    invocation_keys: set[tuple[Any, ...]] = set()
    evidence: list[str] = []
    for directory in run_dirs:
        attempts_path = directory / "agent_attempts.json"
        attempts_payload = _read_json(attempts_path)
        target_ref = _read_json(directory / "target_ref.json")
        adoption_ref = _read_json(directory / "adoption_ref.json")
        no_model = (
            isinstance(target_ref, dict) and target_ref.get("model_invoked") is False
        ) or (
            isinstance(adoption_ref, dict)
            and isinstance(adoption_ref.get("flags"), dict)
            and adoption_ref["flags"].get("model_invoked") is False
        )
        run_meta_exists = (directory / "run_meta.json").exists()
        if run_meta_exists and not no_model:
            expected_invocation_artifacts += 1
        if not isinstance(attempts_payload, dict):
            if run_meta_exists and not no_model:
                missing_expected_artifacts += 1
            continue
        attempts = attempts_payload.get("attempts")
        if not isinstance(attempts, list):
            if run_meta_exists and not no_model:
                missing_expected_artifacts += 1
            continue
        observed_invocation_artifacts += 1
        evidence.append(str(attempts_path))
        target_model = (
            _clean_str(target_ref.get("model")) if isinstance(target_ref, dict) else None
        )
        for index, attempt in enumerate(attempts):
            if not isinstance(attempt, dict):
                continue
            session = _clean_str(attempt.get("agent_session_id"))
            started = _clean_str(attempt.get("attempt_started_utc"))
            finished = _clean_str(attempt.get("attempt_finished_utc"))
            if session is not None and started is not None:
                invocation_key: tuple[Any, ...] = (
                    "session_time",
                    session,
                    started,
                    finished,
                    attempt.get("attempt"),
                )
            else:
                invocation_key = ("artifact", str(attempts_path), index)
            if invocation_key in invocation_keys:
                continue
            invocation_keys.add(invocation_key)
            invocation_count += 1
            model = _model_from_argv(attempt.get("argv")) or target_model or "unknown"
            models[model] = models.get(model, 0) + 1
            if session is not None:
                sessions.add(session)
            raw_value = _clean_str(attempt.get("raw_events_path"))
            if raw_value is None:
                turn_evidence_complete = False
                continue
            raw_path = Path(raw_value).expanduser()
            if not raw_path.is_absolute():
                raw_path = directory / raw_path
            count = _count_turns(raw_path)
            if count is None:
                turn_evidence_complete = False
            else:
                turns += count
    invocation_status = "observed" if missing_expected_artifacts == 0 else "partial"
    if expected_invocation_artifacts == 0 and observed_invocation_artifacts == 0:
        invocation_status = "unknown"
    if invocation_count == 0:
        turn_status = "unknown"
    else:
        turn_status = "observed" if turn_evidence_complete else "partial"
    return {
        "invocations": {
            "status": invocation_status,
            "observed_count": invocation_count,
            "model_counts": dict(sorted(models.items())),
            "distinct_session_count": len(sessions),
            "expected_run_artifact_count": expected_invocation_artifacts,
            "observed_agent_attempt_artifact_count": observed_invocation_artifacts,
            "missing_expected_agent_attempt_artifact_count": missing_expected_artifacts,
        },
        "agent_turns": {
            "status": turn_status,
            "observed_count": turns,
            "definition": "turn.started events in each agent attempt's bound raw-events file",
        },
        "evidence_paths": evidence,
    }


def _explicit_origin(payload: dict[str, Any]) -> str | None:
    for field in _ORIGIN_FIELDS:
        value = _clean_str(payload.get(field))
        if value is None:
            continue
        normalized = value.lower().replace("-", "_").replace(" ", "_")
        if normalized in _SYSTEM_ORIGINS:
            return "system_self_correction"
        if normalized in _EXTERNAL_ORIGINS:
            return "external_manual"
    return None


def _correction_metrics(run_dirs: list[Path]) -> dict[str, Any]:
    events: list[dict[str, Any]] = []
    edge_keys: set[tuple[str, str, str]] = set()
    for directory in run_dirs:
        attempts_path = directory / "agent_attempts.json"
        attempts_payload = _read_json(attempts_path)
        if isinstance(attempts_payload, dict) and isinstance(
            attempts_payload.get("attempts"), list
        ):
            attempts = attempts_payload["attempts"]
            for index, attempt in enumerate(attempts[:-1]):
                if isinstance(attempt, dict) and attempt.get("followup_scheduled") is True:
                    events.append(
                        {
                            "kind": "same_run_agent_followup",
                            "origin": "system_self_correction",
                            "path": str(attempts_path),
                            "detail": f"attempts[{index}].followup_scheduled",
                        }
                    )

        resume_ref_path = directory / "resume_ref.json"
        resume_ref = _read_json(resume_ref_path)
        resume_state_path = directory / "ticket_resume_state.json"
        resume_state = _read_json(resume_state_path)
        prior = None
        origin_payload: dict[str, Any] = {}
        evidence_path = resume_state_path
        if isinstance(resume_ref, dict):
            prior = _clean_str(resume_ref.get("resumed_from_run_dir"))
            origin_payload = resume_ref
            evidence_path = resume_ref_path
        if prior is None and isinstance(resume_state, dict):
            prior = _clean_str(resume_state.get("resumed_from_run_dir"))
            origin_payload = resume_state
        if prior is not None:
            key = ("resume", str(directory), prior)
            if key not in edge_keys:
                edge_keys.add(key)
                events.append(
                    {
                        "kind": "cross_run_resume",
                        "origin": _explicit_origin(origin_payload) or "unknown",
                        "path": str(evidence_path),
                        "detail": prior,
                    }
                )

        review_ref_path = directory / "review_ref.json"
        review_ref = _read_json(review_ref_path)
        prior_review = (
            _clean_str(review_ref.get("correction_of_review_run_dir"))
            if isinstance(review_ref, dict)
            else None
        )
        if prior_review is not None:
            key = ("review", str(directory), prior_review)
            if key not in edge_keys:
                edge_keys.add(key)
                events.append(
                    {
                        "kind": "review_correction",
                        "origin": _explicit_origin(review_ref) or "unknown",
                        "path": str(review_ref_path),
                        "detail": prior_review,
                    }
                )

    system_count = sum(event["origin"] == "system_self_correction" for event in events)
    external_count = sum(event["origin"] == "external_manual" for event in events)
    unknown_count = sum(event["origin"] == "unknown" for event in events)
    return {
        "observed_cycle_count": len(events),
        "system_self_correction": {
            "status": "observed",
            "count": system_count,
        },
        "external_manual": {
            "status": "partial" if unknown_count else "observed",
            "observed_count": external_count,
            "reason": (
                "Some correction artifacts do not record who initiated the correction."
                if unknown_count
                else None
            ),
        },
        "unknown_origin": {"count": unknown_count},
        "events": events,
    }


def _verification_scope(command_record: dict[str, Any], payload: dict[str, Any]) -> str:
    for source in (command_record, payload):
        for field in ("verification_scope", "scope"):
            value = _clean_str(source.get(field))
            if value is None:
                continue
            normalized = value.lower().replace("-", "_").replace(" ", "_")
            if normalized in {"narrow", "focused", "component"}:
                return "narrow"
            if normalized in {"end_to_end", "full", "full_suite"}:
                return "full"
    command = _clean_str(command_record.get("command")) or ""
    if "pytest" in command.lower() and "::" in command:
        return "narrow"
    return "unclassified"


def _verification_metrics(run_dirs: list[Path]) -> dict[str, Any]:
    unique: dict[tuple[Any, ...], tuple[str, str]] = {}
    evidence_paths: list[str] = []
    for directory in run_dirs:
        path = directory / "verification.json"
        payload = _read_json(path)
        if not isinstance(payload, dict):
            continue
        commands = payload.get("commands")
        if not isinstance(commands, list):
            continue
        evidence_paths.append(str(path))
        broker_request_id = _clean_str(payload.get("broker_request_id"))
        for index, record in enumerate(commands):
            if not isinstance(record, dict):
                continue
            command = _clean_str(record.get("command"))
            if command is None:
                continue
            started = _clean_str(record.get("command_started_utc"))
            if broker_request_id is not None:
                key: tuple[Any, ...] = ("broker", broker_request_id, index, command)
            elif started is not None:
                key = ("started", started, command, record.get("exit_code"))
            else:
                key = ("artifact", str(path), index, command)
            unique[key] = (_verification_scope(record, payload), command)
    counts = {"narrow": 0, "full": 0, "unclassified": 0}
    for scope, _command in unique.values():
        counts[scope] += 1
    status = "unknown" if not evidence_paths else (
        "observed" if counts["unclassified"] == 0 else "partial"
    )
    return {
        "status": status,
        "observed_unique_command_count": len(unique),
        "counts_by_evidenced_scope": counts,
        "classification_rule": (
            "Uses an explicit scope field when present; otherwise only a pytest node target "
            "containing '::' is classified as narrow. Unlabeled commands are not guessed."
        ),
        "coverage": "runner-owned verification.json commands only",
        "evidence_paths": sorted(set(evidence_paths)),
    }


def _terminal_disposition(*, run_dirs: list[Path]) -> dict[str, Any]:
    candidates: list[tuple[str, str]] = []
    for directory in run_dirs:
        outcome_path = directory / "outcome_progression.json"
        outcome = _read_json(outcome_path)
        if isinstance(outcome, dict):
            final_state = _clean_str(outcome.get("final_state"))
            if final_state is not None:
                candidates.append((final_state, str(outcome_path)))
        merge_path = directory / "merge_ref.json"
        merge = _read_json(merge_path)
        if isinstance(merge, dict):
            outcome_state = _clean_str(merge.get("outcome_state"))
            if outcome_state is not None:
                candidates.append((outcome_state, str(merge_path)))
    if candidates:
        # The lineage traversal is breadth-first from the current implementation
        # and review runs, so the first disposition is the nearest current one.
        value, path = candidates[0]
        return {"status": "observed", "value": value, "evidence_path": path}
    return {
        "status": "unknown",
        "value": None,
        "evidence_path": None,
        "reason": (
            "No linked outcome disposition exists; workflow completion is not treated as proof "
            "that the problem was resolved."
        ),
    }


def build_ticket_pipeline_efficiency(
    *,
    run_dir: Path,
    review_run_dir: Path | None,
    resume_state: dict[str, Any],
) -> dict[str, Any]:
    linked = _linked_run_dirs(run_dir=run_dir, review_run_dir=review_run_dir)
    lifecycle_state = _clean_str(resume_state.get("lifecycle_state"))
    terminal_disposition = _terminal_disposition(run_dirs=linked)
    terminal_outcome_states = {"resolved", "mitigated", "duplicate", "superseded"}
    terminal_outcome_state = (
        _clean_str(terminal_disposition.get("value")) or ""
    ).lower()
    terminal_outcome = (
        terminal_disposition.get("status") == "observed"
        and terminal_outcome_state in terminal_outcome_states
    )
    return {
        "schema_version": 1,
        "kind": "ticket_pipeline_efficiency",
        "observational_only": True,
        "generated_at_utc": _utc_now_z(),
        "ticket": resume_state.get("ticket"),
        "measurement_scope": {
            "start_boundary": "earliest_linked_implementation_or_review",
            "end_boundary": "current_resume_state_observation",
            "end_to_end": False,
            "excluded_upstream_stages": [
                "atom_mining",
                "problem_mining",
                "research",
                "optioning",
                "planning",
            ],
        },
        "lifecycle": {
            "current_state": lifecycle_state,
            "resume_state_terminal": lifecycle_state not in {None, "in_progress"},
            "implementation_workflow_terminal": lifecycle_state == "complete",
            "outcome_terminal": terminal_outcome,
            "terminal_disposition": terminal_disposition,
        },
        "elapsed": _elapsed_metrics(run_dirs=linked, resume_state=resume_state),
        "agent_activity": _model_metrics(linked),
        "correction_cycles": _correction_metrics(linked),
        "verification": _verification_metrics(linked),
        "linked_run_dirs": [str(path) for path in linked],
        "limitations": [
            "Elapsed time begins at the earliest linked implementation or review execution; atom, "
            "research, and planning time is not yet linked into this lifecycle graph.",
            "Verification counts exclude ad-hoc test commands that exist only inside agent shell "
            "events; only structured runner verification receipts are counted.",
            "Correction initiator is unknown unless a durable correction-origin field says who "
            "initiated it.",
            "The artifact reports observations and does not approve, block, or change a ticket.",
        ],
    }


def write_ticket_pipeline_efficiency(
    *,
    run_dir: Path,
    review_run_dir: Path | None,
    resume_state: dict[str, Any],
) -> dict[str, Any]:
    artifact = build_ticket_pipeline_efficiency(
        run_dir=run_dir,
        review_run_dir=review_run_dir,
        resume_state=resume_state,
    )
    _write_json(run_dir / PIPELINE_EFFICIENCY_ARTIFACT_NAME, artifact)
    return artifact


__all__ = [
    "PIPELINE_EFFICIENCY_ARTIFACT_NAME",
    "build_ticket_pipeline_efficiency",
    "write_ticket_pipeline_efficiency",
]
