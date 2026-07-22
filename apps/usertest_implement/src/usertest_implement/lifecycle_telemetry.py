from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any, Literal

from reporter.materialize import materialize_lifecycle_metrics
from run_artifacts.lifecycle_events import (
    LifecycleContext,
    LifecycleManifest,
    append_lifecycle_event,
    make_lifecycle_event,
    read_lifecycle_events,
    read_lifecycle_manifest,
    utc_now,
    write_lifecycle_manifest,
)

LIFECYCLE_EVENTS_ARTIFACT_NAME = "lifecycle_events.jsonl"
LIFECYCLE_MANIFEST_ARTIFACT_NAME = "lifecycle_manifest.json"

_TERMINAL_FAILURE_STATES = {
    "agent_failed",
    "verification_failed",
    "verification_failed_resume_ready",
    "push_failed",
    "ci_failed",
    "pr_creation_failed",
}
_TERMINAL_OUTCOME_STATES = {"resolved", "mitigated", "duplicate", "superseded"}
_ORIGIN_FIELDS = (
    "correction_origin",
    "instruction_origin",
    "intervention_origin",
    "trigger_origin",
)
_AUTOMATIC_ORIGINS = {
    "automatic",
    "automated",
    "runner",
    "system",
    "self_healing",
    "system_self_correction",
}
_MANUAL_ORIGINS = {"external", "human", "manual", "user", "external_manual"}
_SUPERVISOR_ORIGINS = {"supervisor", "supervising_agent", "supervisor_correction"}
_EXTERNAL_SERVICE_ORIGINS = {"external_service", "provider", "service"}


@dataclass(frozen=True)
class _Origin:
    initiator_type: str
    root_initiator_type: str
    origin: str
    provenance_quality: Literal[
        "authoritative",
        "artifact_derived",
        "operator_attested",
        "inferred",
        "unknown",
    ]
    evidence: str


@dataclass(frozen=True)
class _Milestone:
    milestone_id: str
    stage: str
    event_type: str
    path: Path
    payload: dict[str, Any]
    status: str
    successful: bool | None
    occurred_at: str
    started_at: str | None = None
    ended_at: str | None = None
    active_seconds: float | None = None
    external_wait_seconds: float | None = None
    error_on_failure: bool = False


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _clean_str(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def _stable_id(prefix: str, *parts: object) -> str:
    encoded = "\x00".join(str(part) for part in parts).encode("utf-8", errors="replace")
    return f"{prefix}:{sha256(encoded).hexdigest()}"


def _artifact_sha256(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def _valid_timestamp(value: Any) -> str | None:
    text = _clean_str(value)
    if text is None:
        return None
    try:
        parsed = datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return text


def _mtime_timestamp(path: Path) -> str:
    observed = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    return observed.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _event_timestamp(
    path: Path,
    payload: Mapping[str, Any],
    *fields: str,
) -> tuple[str, str]:
    for field in fields:
        value = _valid_timestamp(payload.get(field))
        if value is not None:
            return value, field
    return _mtime_timestamp(path), "artifact_mtime"


def _nonnegative_seconds(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    return result if result >= 0 else None


def _interval_seconds(started_at: str | None, ended_at: str | None) -> float | None:
    if started_at is None or ended_at is None:
        return None
    try:
        start = datetime.fromisoformat(
            started_at[:-1] + "+00:00" if started_at.endswith("Z") else started_at
        )
        end = datetime.fromisoformat(
            ended_at[:-1] + "+00:00" if ended_at.endswith("Z") else ended_at
        )
    except ValueError:
        return None
    seconds = (end - start).total_seconds()
    return seconds if seconds >= 0 else None


def _existing_manifest(run_dir: Path) -> LifecycleManifest | None:
    try:
        return read_lifecycle_manifest(run_dir / LIFECYCLE_MANIFEST_ARTIFACT_NAME)
    except Exception:  # noqa: BLE001 - malformed optional telemetry is not a lifecycle gate
        return None


def _base_context(
    *, run_dir: Path, resume_state: Mapping[str, Any]
) -> tuple[LifecycleContext, LifecycleManifest | None]:
    case_lifecycle_id = str(resume_state["case_lifecycle_id"])
    case_id = str(resume_state["case_id"])
    manifest = _existing_manifest(run_dir)
    fingerprint = (
        dict(manifest.system_fingerprint)
        if manifest is not None
        and manifest.case_lifecycle_id == case_lifecycle_id
        and manifest.case_id == case_id
        else {}
    )
    cycle_id: str | None = None
    events_path = run_dir / LIFECYCLE_EVENTS_ARTIFACT_NAME
    try:
        for event in reversed(read_lifecycle_events(events_path)):
            if event.context.case_lifecycle_id != case_lifecycle_id:
                continue
            cycle_id = event.context.cycle_id
            fingerprint = {**event.context.system_fingerprint, **fingerprint}
            break
    except Exception:  # noqa: BLE001 - a later validator reports malformed retained telemetry
        pass
    run_identity = str(run_dir.resolve())
    return (
        LifecycleContext(
            case_lifecycle_id=case_lifecycle_id,
            case_id=case_id,
            cycle_id=cycle_id or _stable_id("implementation-cycle", run_identity),
            stage="implementation",
            system_fingerprint=fingerprint,
        ),
        manifest,
    )


def _normalize_origin(value: str) -> str:
    return value.lower().replace("-", "_").replace(" ", "_")


def _origin_from_evidence(
    *,
    context: LifecycleContext,
    run_dir: Path,
    review_run_dir: Path | None,
) -> _Origin:
    if context.system_fingerprint.get("controller_context_verified") == "true":
        return _Origin(
            initiator_type="controller",
            root_initiator_type="controller",
            origin="automatic",
            provenance_quality="authoritative",
            evidence="system_fingerprint.controller_context_verified",
        )

    paths = [run_dir / "resume_ref.json"]
    if review_run_dir is not None:
        paths.extend(
            [review_run_dir / "review_ref.json", review_run_dir / "review_summary.json"]
        )
    for path in paths:
        payload = _read_json(path)
        if payload is None:
            continue
        for field in _ORIGIN_FIELDS:
            raw = _clean_str(payload.get(field))
            if raw is None:
                continue
            normalized = _normalize_origin(raw)
            evidence = f"{path}:{field}"
            if normalized in _AUTOMATIC_ORIGINS:
                return _Origin(
                    initiator_type="controller",
                    root_initiator_type="controller",
                    origin="automatic",
                    provenance_quality="artifact_derived",
                    evidence=evidence,
                )
            if normalized in _MANUAL_ORIGINS:
                return _Origin(
                    initiator_type="human",
                    root_initiator_type="human",
                    origin="manual",
                    provenance_quality="artifact_derived",
                    evidence=evidence,
                )
            if normalized in _SUPERVISOR_ORIGINS:
                return _Origin(
                    initiator_type="supervising_agent",
                    root_initiator_type="supervising_agent",
                    origin="supervising_agent",
                    provenance_quality="artifact_derived",
                    evidence=evidence,
                )
            if normalized in _EXTERNAL_SERVICE_ORIGINS:
                return _Origin(
                    initiator_type="external_service",
                    root_initiator_type="external_service",
                    origin="external_service",
                    provenance_quality="artifact_derived",
                    evidence=evidence,
                )
    return _Origin(
        initiator_type="unknown",
        root_initiator_type="unknown",
        origin="unknown_external",
        provenance_quality="unknown",
        evidence="no_verified_controller_or_explicit_origin",
    )


def _milestone_context(
    context: LifecycleContext, milestone: _Milestone, digest: str
) -> LifecycleContext:
    return replace(
        context,
        stage=milestone.stage,
        milestone_id=milestone.milestone_id,
        work_unit_id=_stable_id(
            "implementation-work",
            context.case_lifecycle_id,
            milestone.milestone_id,
            digest,
        ),
    )


def _verification_milestone(run_dir: Path) -> _Milestone | None:
    path = run_dir / "verification.json"
    payload = _read_json(path)
    if payload is None:
        return None
    occurred_at, timestamp_source = _event_timestamp(
        path, payload, "finished_at_utc", "generated_at_utc"
    )
    passed = payload.get("passed") if isinstance(payload.get("passed"), bool) else None
    payload = {**payload, "_telemetry_timestamp_source": timestamp_source}
    return _Milestone(
        milestone_id="implementation_verified",
        stage="implementation",
        event_type="stage.completed",
        path=path,
        payload=payload,
        status="passed" if passed is True else "failed" if passed is False else "unknown",
        successful=passed,
        occurred_at=occurred_at,
        active_seconds=_nonnegative_seconds(payload.get("wall_seconds")),
        error_on_failure=True,
    )


def _commit_milestone(run_dir: Path) -> _Milestone | None:
    path = run_dir / "git_ref.json"
    payload = _read_json(path)
    if payload is None or payload.get("commit_attempted") is not True:
        return None
    occurred_at, timestamp_source = _event_timestamp(
        path, payload, "committed_at_utc", "observed_at_utc"
    )
    success = bool(
        not payload.get("error")
        and _clean_str(payload.get("head_commit")) is not None
        and (payload.get("commit_performed") is True or payload.get("commit_observed") is True)
    )
    if payload.get("commit_performed") is True:
        status = "performed"
    elif payload.get("commit_observed") is True:
        status = "observed_existing"
    elif payload.get("error"):
        status = "failed"
    else:
        status = "incomplete"
    return _Milestone(
        milestone_id="commit",
        stage="delivery",
        event_type="delivery.completed",
        path=path,
        payload={**payload, "_telemetry_timestamp_source": timestamp_source},
        status=status,
        successful=success,
        occurred_at=occurred_at,
        error_on_failure=True,
    )


def _push_milestone(run_dir: Path) -> _Milestone | None:
    path = run_dir / "push_ref.json"
    payload = _read_json(path)
    if payload is None:
        return None
    occurred_at, timestamp_source = _event_timestamp(
        path, payload, "pushed_at_utc", "finished_at_utc"
    )
    success = payload.get("pushed") is True
    return _Milestone(
        milestone_id="push",
        stage="delivery",
        event_type="delivery.completed",
        path=path,
        payload={**payload, "_telemetry_timestamp_source": timestamp_source},
        status="pushed" if success else "failed",
        successful=success,
        occurred_at=occurred_at,
        error_on_failure=True,
    )


def _pr_milestone(run_dir: Path) -> _Milestone | None:
    path = run_dir / "pr_ref.json"
    payload = _read_json(path)
    if payload is None:
        return None
    requested = payload.get("requested") is True
    created = payload.get("created") is True or payload.get("pr_adopted") is True
    if not requested and not created and not payload.get("error"):
        return None
    occurred_at, timestamp_source = _event_timestamp(
        path, payload, "created_at_utc", "observed_at_utc"
    )
    status = "adopted" if payload.get("pr_adopted") is True else "created" if created else "failed"
    return _Milestone(
        milestone_id="pr_created",
        stage="delivery",
        event_type="delivery.completed",
        path=path,
        payload={**payload, "_telemetry_timestamp_source": timestamp_source},
        status=status,
        successful=created,
        occurred_at=occurred_at,
        error_on_failure=True,
    )


def _ci_milestone(run_dir: Path) -> _Milestone | None:
    path = run_dir / "ci_gate.json"
    payload = _read_json(path)
    if payload is None:
        return None
    started_at = _valid_timestamp(payload.get("started_at_utc"))
    ended_at = _valid_timestamp(payload.get("finished_at_utc"))
    terminal = (
        ended_at is not None
        or payload.get("skipped") is True
        or bool(payload.get("error"))
        or str(payload.get("status") or "").strip().lower() == "completed"
    )
    if not terminal:
        return None
    occurred_at, timestamp_source = _event_timestamp(
        path, payload, "finished_at_utc", "updated_at_utc"
    )
    passed = payload.get("passed") if isinstance(payload.get("passed"), bool) else None
    status = (
        "skipped"
        if payload.get("skipped") is True
        else "passed"
        if passed is True
        else "failed"
        if passed is False or payload.get("error")
        else "unknown"
    )
    return _Milestone(
        milestone_id="ci",
        stage="delivery",
        event_type="delivery.completed",
        path=path,
        payload={**payload, "_telemetry_timestamp_source": timestamp_source},
        status=status,
        successful=passed,
        occurred_at=occurred_at,
        started_at=started_at,
        ended_at=ended_at,
        external_wait_seconds=_interval_seconds(started_at, ended_at),
        error_on_failure=passed is False,
    )


def _review_milestone(review_run_dir: Path | None) -> _Milestone | None:
    if review_run_dir is None:
        return None
    path = review_run_dir / "review_summary.json"
    payload = _read_json(path)
    if payload is None:
        return None
    occurred_at, timestamp_source = _event_timestamp(path, payload, "reviewed_at_utc")
    decision = (_clean_str(payload.get("review_decision")) or "unknown").lower()
    return _Milestone(
        milestone_id="review",
        stage="delivery",
        event_type="delivery.completed",
        path=path,
        payload={**payload, "_telemetry_timestamp_source": timestamp_source},
        status=decision,
        successful=payload.get("merge_ready") is True,
        occurred_at=occurred_at,
        error_on_failure=False,
    )


def _merge_milestone(review_run_dir: Path | None) -> _Milestone | None:
    if review_run_dir is None:
        return None
    path = review_run_dir / "merge_ref.json"
    payload = _read_json(path)
    if payload is None:
        return None
    merged = payload.get("merged") is True
    completed = merged or payload.get("returncode") is not None or bool(payload.get("error"))
    if not completed:
        return None
    occurred_at, timestamp_source = _event_timestamp(path, payload, "merged_at_utc")
    return _Milestone(
        milestone_id="merge",
        stage="delivery",
        event_type="delivery.completed",
        path=path,
        payload={**payload, "_telemetry_timestamp_source": timestamp_source},
        status="merged" if merged else "failed",
        successful=merged,
        occurred_at=occurred_at,
        error_on_failure=True,
    )


def _outcome_milestone(review_run_dir: Path | None) -> _Milestone | None:
    if review_run_dir is None:
        return None
    path = review_run_dir / "outcome_progression.json"
    payload = _read_json(path)
    if payload is None:
        return None
    occurred_at, timestamp_source = _event_timestamp(path, payload, "generated_at_utc")
    final_state = (_clean_str(payload.get("final_state")) or "unknown").lower()
    complete = payload.get("complete") is True or final_state in _TERMINAL_OUTCOME_STATES
    return _Milestone(
        milestone_id="outcome_verified",
        stage="delivery",
        event_type="delivery.completed",
        path=path,
        payload={**payload, "_telemetry_timestamp_source": timestamp_source},
        status=final_state,
        successful=complete,
        occurred_at=occurred_at,
        error_on_failure=bool(payload.get("error")),
    )


def _milestones(run_dir: Path, review_run_dir: Path | None) -> list[_Milestone]:
    candidates = (
        _verification_milestone(run_dir),
        _commit_milestone(run_dir),
        _push_milestone(run_dir),
        _pr_milestone(run_dir),
        _ci_milestone(run_dir),
        _review_milestone(review_run_dir),
        _merge_milestone(review_run_dir),
        _outcome_milestone(review_run_dir),
    )
    return [item for item in candidates if item is not None]


def _actor_fields(origin: _Origin) -> dict[str, Any]:
    return {
        "actor_type": "system",
        "initiator_type": origin.initiator_type,
        "root_initiator_type": origin.root_initiator_type,
        "origin": origin.origin,
        "provenance_quality": origin.provenance_quality,
    }


def _automated(origin: _Origin) -> bool | None:
    if origin.origin in {"automatic", "external_service"}:
        return True
    if origin.origin in {"manual", "supervising_agent"}:
        return False
    return None


def _resolution_mode(origin: _Origin) -> str:
    if origin.origin == "automatic":
        return "self_healed_controller"
    if origin.origin == "manual":
        return "resolved_human"
    if origin.origin == "supervising_agent":
        return "resolved_supervisor"
    return "resolved_external"


def _known_error_state(events_path: Path) -> tuple[set[str], set[str]]:
    occurred: set[str] = set()
    resolved: set[str] = set()
    try:
        for event in read_lifecycle_events(events_path):
            if event.error_cluster_id is None:
                continue
            if event.event_type == "error.occurred":
                occurred.add(event.error_cluster_id)
            elif event.event_type == "error.resolved":
                resolved.add(event.error_cluster_id)
    except Exception:  # noqa: BLE001 - append/validation owns durable corruption reporting
        pass
    return occurred, resolved


def _emit_milestone(
    *,
    events_path: Path,
    context: LifecycleContext,
    milestone: _Milestone,
    origin: _Origin,
    terminal_failure: bool,
    occurred_errors: set[str],
    resolved_errors: set[str],
) -> int:
    digest = _artifact_sha256(milestone.path)
    event_context = _milestone_context(context, milestone, digest)
    common_attributes = {
        "scope": milestone.stage,
        "milestone": milestone.milestone_id,
        "status": milestone.status,
        "successful": milestone.successful,
        "origin_evidence": origin.evidence,
        "automated": _automated(origin),
        "timestamp_semantics": milestone.payload.get("_telemetry_timestamp_source"),
        **(
            {
                "wait_category": "ci",
                "wait_seconds_by_category": {"ci": milestone.external_wait_seconds},
            }
            if milestone.milestone_id == "ci"
            and milestone.external_wait_seconds is not None
            else {}
        ),
    }
    appended = int(
        append_lifecycle_event(
            events_path,
            make_lifecycle_event(
                milestone.event_type,
                event_context,
                idempotency_key=_stable_id(
                    "implementation-event",
                    context.case_lifecycle_id,
                    milestone.milestone_id,
                    digest,
                ),
                occurred_at=milestone.occurred_at,
                started_at=milestone.started_at,
                ended_at=milestone.ended_at,
                active_seconds=milestone.active_seconds,
                external_wait_seconds=milestone.external_wait_seconds,
                evidence_paths=(str(milestone.path),),
                artifact_hashes={"source_artifact": digest},
                attributes=common_attributes,
                **_actor_fields(origin),
            ),
        )
    )

    if milestone.milestone_id == "pr_created" and milestone.successful is True:
        disposition_attributes = {
            **common_attributes,
            "disposition": "pr",
            "pr_url": _clean_str(milestone.payload.get("url")),
            "pr_created_at": milestone.occurred_at,
            "closure_valid": True,
        }
        for event_type in ("disposition.reached", "disposition.verified"):
            appended += int(
                append_lifecycle_event(
                    events_path,
                    make_lifecycle_event(
                        event_type,
                        event_context,
                        idempotency_key=_stable_id(
                            "implementation-disposition",
                            context.case_lifecycle_id,
                            event_type,
                            "pr",
                            digest,
                        ),
                        occurred_at=milestone.occurred_at,
                        evidence_paths=(str(milestone.path),),
                        artifact_hashes={"source_artifact": digest},
                        attributes=disposition_attributes,
                        **_actor_fields(origin),
                    ),
                )
            )

    if milestone.milestone_id == "outcome_verified" and milestone.successful is True:
        appended += int(
            append_lifecycle_event(
                events_path,
                make_lifecycle_event(
                    "outcome.verified",
                    event_context,
                    idempotency_key=_stable_id(
                        "implementation-disposition-verification",
                        context.case_lifecycle_id,
                        digest,
                    ),
                    occurred_at=milestone.occurred_at,
                    evidence_paths=(str(milestone.path),),
                    artifact_hashes={"source_artifact": digest},
                    attributes={
                        **common_attributes,
                        "delivery_disposition": "pr",
                        "outcome_state": milestone.status,
                        "outcome_verified_at": milestone.occurred_at,
                        "closure_valid": True,
                    },
                    **_actor_fields(origin),
                ),
            )
        )

    cluster_id = _stable_id(
        "error", context.case_lifecycle_id, milestone.milestone_id
    )
    failed = milestone.error_on_failure and milestone.successful is False
    if failed:
        appended += int(
            append_lifecycle_event(
                events_path,
                make_lifecycle_event(
                    "error.occurred",
                    event_context,
                    idempotency_key=_stable_id("error-occurrence", cluster_id, digest),
                    occurred_at=milestone.occurred_at,
                    error_cluster_id=cluster_id,
                    evidence_paths=(str(milestone.path),),
                    artifact_hashes={"source_artifact": digest},
                    attributes={
                        "error_kind": f"{milestone.milestone_id}_failed",
                        "status": milestone.status,
                        "terminal": terminal_failure,
                    },
                    **_actor_fields(origin),
                ),
            )
        )
        occurred_errors.add(cluster_id)
        if terminal_failure and cluster_id not in resolved_errors:
            appended += int(
                append_lifecycle_event(
                    events_path,
                    make_lifecycle_event(
                        "error.resolved",
                        event_context,
                        idempotency_key=f"{cluster_id}:unresolved-terminal",
                        occurred_at=milestone.occurred_at,
                        error_cluster_id=cluster_id,
                        attributes={
                            "error_kind": f"{milestone.milestone_id}_failed",
                            "resolution_mode": "unresolved_terminal",
                            "resolution_category": "unresolved_terminal",
                        },
                        **_actor_fields(origin),
                    ),
                )
            )
            resolved_errors.add(cluster_id)
    elif (
        milestone.successful is True
        and cluster_id in occurred_errors
        and cluster_id not in resolved_errors
    ):
        appended += int(
            append_lifecycle_event(
                events_path,
                make_lifecycle_event(
                    "error.resolved",
                    event_context,
                    idempotency_key=f"{cluster_id}:resolved",
                    occurred_at=milestone.occurred_at,
                    error_cluster_id=cluster_id,
                    attributes={
                        "error_kind": f"{milestone.milestone_id}_failed",
                        "resolution_mode": _resolution_mode(origin),
                        "resolution_category": _resolution_mode(origin),
                    },
                    **_actor_fields(origin),
                ),
            )
        )
        resolved_errors.add(cluster_id)
    return appended


def _run_started_at(run_dir: Path, fallback: str) -> str:
    run_meta = _read_json(run_dir / "run_meta.json") or {}
    return _valid_timestamp(run_meta.get("run_started_utc")) or fallback


def _write_manifest(
    *,
    run_dir: Path,
    review_run_dir: Path | None,
    resume_state: Mapping[str, Any],
    context: LifecycleContext,
    existing: LifecycleManifest | None,
    disposition_reached: bool,
    terminal_failure: bool,
    origin: _Origin,
) -> None:
    case_lifecycle_id = str(resume_state["case_lifecycle_id"])
    case_id = str(resume_state["case_id"])
    if existing is not None and (
        existing.case_lifecycle_id != case_lifecycle_id or existing.case_id != case_id
    ):
        return
    updated_at = _valid_timestamp(resume_state.get("generated_at_utc")) or utc_now()
    created_at = (
        existing.created_at
        if existing is not None
        else _run_started_at(run_dir, updated_at)
    )
    if disposition_reached or resume_state.get("lifecycle_state") == "complete":
        status = "terminal"
    elif terminal_failure:
        status = "incomplete"
    else:
        status = "active"
    metadata = dict(existing.metadata) if existing is not None else {}
    metadata.update(
        {
            "run_dir": str(run_dir),
            "review_run_dir": str(review_run_dir) if review_run_dir is not None else None,
            "plan_revision_id": resume_state.get("plan_revision_id"),
            "resume_state_path": str(run_dir / "ticket_resume_state.json"),
            "origin_evidence": origin.evidence,
        }
    )
    manifest = LifecycleManifest(
        case_lifecycle_id=case_lifecycle_id,
        case_id=case_id,
        created_at=created_at,
        updated_at=updated_at,
        status=status,
        event_log_path=LIFECYCLE_EVENTS_ARTIFACT_NAME,
        dependency_lifecycle_ids=(
            existing.dependency_lifecycle_ids if existing is not None else ()
        ),
        shared_work_ids=existing.shared_work_ids if existing is not None else (),
        usage_receipt_paths=(existing.usage_receipt_paths if existing is not None else ()),
        system_fingerprint=context.system_fingerprint,
        provenance_quality=(
            existing.provenance_quality
            if existing is not None and existing.provenance_quality != "unknown"
            else origin.provenance_quality
        ),
        metadata=metadata,
    )
    write_lifecycle_manifest(run_dir / LIFECYCLE_MANIFEST_ARTIFACT_NAME, manifest)


def write_implementation_lifecycle_telemetry(
    *,
    run_dir: Path,
    review_run_dir: Path | None,
    resume_state: Mapping[str, Any],
) -> dict[str, Any]:
    """Materialize replay-safe observational events from retained delivery artifacts."""

    run_dir = run_dir.resolve()
    review_run_dir = review_run_dir.resolve() if review_run_dir is not None else None
    context, manifest = _base_context(run_dir=run_dir, resume_state=resume_state)
    origin = _origin_from_evidence(
        context=context,
        run_dir=run_dir,
        review_run_dir=review_run_dir,
    )
    events_path = run_dir / LIFECYCLE_EVENTS_ARTIFACT_NAME
    generated_at = _valid_timestamp(resume_state.get("generated_at_utc")) or utc_now()
    started_at = _run_started_at(run_dir, generated_at)
    appended = int(
        append_lifecycle_event(
            events_path,
            make_lifecycle_event(
                "lifecycle.opened",
                context,
                idempotency_key=f"{context.case_lifecycle_id}:implementation-opened",
                occurred_at=started_at,
                started_at=started_at,
                evidence_paths=tuple(
                    str(path)
                    for path in (run_dir / "run_meta.json", run_dir / "ticket_ref.json")
                    if path.exists()
                ),
                attributes={
                    "scope": "case",
                    "origin_evidence": origin.evidence,
                    "automated": _automated(origin),
                },
                **_actor_fields(origin),
            ),
        )
    )

    lifecycle_state = _clean_str(resume_state.get("lifecycle_state")) or "in_progress"
    terminal_failure = lifecycle_state in _TERMINAL_FAILURE_STATES
    occurred_errors, resolved_errors = _known_error_state(events_path)
    observed = _milestones(run_dir, review_run_dir)
    for milestone in observed:
        appended += _emit_milestone(
            events_path=events_path,
            context=context,
            milestone=milestone,
            origin=origin,
            terminal_failure=terminal_failure,
            occurred_errors=occurred_errors,
            resolved_errors=resolved_errors,
        )

    pr_milestone = next(
        (item for item in observed if item.milestone_id == "pr_created"), None
    )
    disposition_reached = bool(
        pr_milestone is not None and pr_milestone.successful is True
    )
    if disposition_reached or terminal_failure:
        closure_time = (
            pr_milestone.occurred_at
            if pr_milestone is not None and pr_milestone.successful is True
            else generated_at
        )
        appended += int(
            append_lifecycle_event(
                events_path,
                make_lifecycle_event(
                    "lifecycle.closed",
                    context,
                    idempotency_key=f"{context.case_lifecycle_id}:implementation-closed",
                    occurred_at=closure_time,
                    started_at=started_at,
                    ended_at=closure_time,
                    evidence_paths=(str(run_dir / "ticket_resume_state.json"),),
                    attributes={
                        "scope": "case",
                        "closure_valid": disposition_reached,
                        "status": "pr" if disposition_reached else "failed_incomplete",
                        "disposition": "pr" if disposition_reached else "failed_incomplete",
                        "origin_evidence": origin.evidence,
                        "automated": _automated(origin),
                    },
                    **_actor_fields(origin),
                ),
            )
        )

    _write_manifest(
        run_dir=run_dir,
        review_run_dir=review_run_dir,
        resume_state=resume_state,
        context=context,
        existing=manifest,
        disposition_reached=disposition_reached,
        terminal_failure=terminal_failure,
        origin=origin,
    )
    materialized = materialize_lifecycle_metrics(
        event_sources=[events_path],
        output_dir=run_dir,
    )
    return {
        "events_path": str(events_path),
        "appended_event_count": appended,
        "observed_milestones": [item.milestone_id for item in observed],
        "case_lifecycle_id": context.case_lifecycle_id,
        "origin": origin.origin,
        "origin_evidence": origin.evidence,
        "case_metrics_path": str(materialized.case_metrics_path),
        "cohort_metrics_path": str(materialized.cohort_metrics_path),
    }


__all__ = [
    "LIFECYCLE_EVENTS_ARTIFACT_NAME",
    "LIFECYCLE_MANIFEST_ARTIFACT_NAME",
    "write_implementation_lifecycle_telemetry",
]
