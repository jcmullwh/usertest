from __future__ import annotations

import json
import re
import threading
import uuid
import warnings
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from typing import Any

from reporter.materialize import materialize_lifecycle_metrics
from run_artifacts.lifecycle_events import (
    LifecycleContext,
    LifecycleManifest,
    append_lifecycle_event,
    canonical_sha256,
    load_context_from_env,
    make_lifecycle_event,
    read_lifecycle_events,
    utc_now,
    write_lifecycle_manifest,
)

_STAGE_MILESTONES = {
    "problem_mining": "stage1",
    "problem_prioritization": "stage2",
    "repro_research": "stage3",
    "solution_options": "stage4",
    "selected_solution": "stage5",
    "implementation_planning": "stage6",
    "ticket_assembly": "disposition",
}
_TERMINAL_NEGATIVE_DISPOSITIONS = {
    "already_addressed",
    "non_actionable",
    "duplicate",
    "superseded",
}
_ATOM_ID_TIMESTAMP = re.compile(r"(?:^|/)(?P<stamp>\d{8}T\d{6}Z)(?:/|$)")
_NON_MODEL_RESEARCH_ATTEMPT_KINDS = {
    "evidence_verification_feedback",
    "evidence_verification_persistence_replay",
    "evidence_verification_promotion",
    "evidence_verification_rescore",
}


@dataclass(frozen=True)
class _CycleTelemetry:
    cycle_id: str
    system_fingerprint: dict[str, str]
    automatic: bool


@dataclass(frozen=True)
class _ModelTelemetryProjection:
    manifest_paths: tuple[Path, ...]
    expected_invocation_ids: tuple[str, ...]
    completed_invocation_ids: tuple[str, ...]
    invocation_expected: bool | None
    complete: bool
    issues: tuple[str, ...]


_CONTEXT_LOCK = threading.Lock()
_CYCLE_BY_REGISTRY: dict[Path, _CycleTelemetry] = {}


def _stable_id(prefix: str, *parts: object) -> str:
    encoded = "\x00".join(str(part) for part in parts).encode("utf-8", errors="replace")
    return f"{prefix}:{sha256(encoded).hexdigest()}"


def _cycle_for(case_registry_path: Path) -> _CycleTelemetry:
    key = case_registry_path.resolve()
    with _CONTEXT_LOCK:
        existing = _CYCLE_BY_REGISTRY.get(key)
        if existing is not None:
            return existing
        inherited = load_context_from_env(required=False)
        automatic = bool(
            inherited is not None
            and inherited.system_fingerprint.get("controller_context_verified") == "true"
        )
        value = _CycleTelemetry(
            cycle_id=(
                inherited.cycle_id
                if inherited is not None and inherited.cycle_id is not None
                else f"pipeline-cycle:{uuid.uuid4()}"
            ),
            system_fingerprint=(
                dict(inherited.system_fingerprint) if inherited is not None else {}
            ),
            automatic=automatic,
        )
        _CYCLE_BY_REGISTRY[key] = value
        return value


def _timestamp_or_none(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        candidate = value.strip()
        try:
            parsed = datetime.fromisoformat(candidate.replace("Z", "+00:00"))
        except ValueError:
            pass
        else:
            if parsed.tzinfo is not None:
                return candidate
    return None


def _timestamp(value: Any) -> str:
    return _timestamp_or_none(value) or utc_now()


def _case_ids(stage_doc: Mapping[str, Any]) -> list[str]:
    items = stage_doc.get("items")
    if not isinstance(items, list):
        return []
    return sorted(
        {
            str(item["case_id"]).strip()
            for item in items
            if isinstance(item, Mapping)
            and isinstance(item.get("case_id"), str)
            and str(item["case_id"]).strip()
        }
    )


def _origin_atom_ids(*values: Any) -> list[str]:
    found: list[str] = []

    def visit(value: Any, field_name: str | None = None) -> None:
        if isinstance(value, Mapping):
            for key, nested in value.items():
                visit(nested, str(key).casefold())
            return
        if isinstance(value, list):
            for nested in value:
                visit(nested, field_name)
            return
        if not isinstance(value, str) or field_name is None:
            return
        if field_name == "atom_id" or field_name.endswith("_atom_id"):
            candidate = value.strip()
            if candidate and candidate not in found:
                found.append(candidate)
        elif field_name in {
            "atom_ids",
            "assigned_atom_ids",
            "evidence_atom_ids",
            "origin_atom_ids",
            "source_evidence_atom_ids",
        }:
            candidate = value.strip()
            if candidate and candidate not in found:
                found.append(candidate)

    for value in values:
        visit(value)
    return found


def _earliest_atom_timestamp(atom_ids: Sequence[str]) -> str | None:
    observed: list[datetime] = []
    for atom_id in atom_ids:
        match = _ATOM_ID_TIMESTAMP.search(atom_id)
        if match is None:
            continue
        try:
            observed.append(datetime.strptime(match.group("stamp"), "%Y%m%dT%H%M%SZ"))
        except ValueError:
            continue
    if not observed:
        return None
    return min(observed).isoformat(timespec="seconds") + "Z"


def case_lifecycle_id(*, case_registry_path: Path, case_id: str) -> str:
    cycle = _cycle_for(case_registry_path)
    return _stable_id("case-lifecycle", cycle.cycle_id, case_id)


def bind_ticket_lifecycle_ids(
    tickets: Sequence[Mapping[str, Any]],
    *,
    case_registry_path: Path,
) -> list[dict[str, Any]]:
    """Bind downstream delivery artifacts to the current processing attempt."""

    bound: list[dict[str, Any]] = []
    for ticket in tickets:
        updated = dict(ticket)
        raw_case_id = updated.get("case_id")
        if not isinstance(raw_case_id, str) or not raw_case_id.strip():
            for field in ("problem_record", "change_plan", "selected_solution"):
                nested = updated.get(field)
                if isinstance(nested, Mapping) and isinstance(nested.get("case_id"), str):
                    raw_case_id = nested.get("case_id")
                    break
        if isinstance(raw_case_id, str) and raw_case_id.strip():
            updated["case_lifecycle_id"] = case_lifecycle_id(
                case_registry_path=case_registry_path,
                case_id=raw_case_id.strip(),
            )
        bound.append(updated)
    return bound


def _event_fields(cycle: _CycleTelemetry) -> dict[str, str]:
    return {
        "actor_type": "controller",
        "initiator_type": "controller" if cycle.automatic else "unknown",
        "root_initiator_type": "controller" if cycle.automatic else "unknown",
        "origin": "automatic" if cycle.automatic else "unknown_external",
        "provenance_quality": "authoritative" if cycle.automatic else "unknown",
    }


def _case_root(case_registry_path: Path, lifecycle_id: str) -> Path:
    digest = sha256(lifecycle_id.encode("utf-8")).hexdigest()
    return case_registry_path.parent / "telemetry" / "cases" / digest


def _append_case_event(
    *,
    global_path: Path,
    case_registry_path: Path,
    event: Any,
) -> None:
    append_lifecycle_event(global_path, event)
    lifecycle_id = event.context.case_lifecycle_id
    if lifecycle_id is not None:
        append_lifecycle_event(
            _case_root(case_registry_path, lifecycle_id) / "lifecycle_events.jsonl",
            event,
        )


def _disposition_for_case(
    *,
    case_registry: Mapping[str, Any],
    case_id: str,
) -> str | None:
    cases = case_registry.get("cases")
    cases = cases if isinstance(cases, Mapping) else {}
    entry = cases.get(case_id)
    if not isinstance(entry, Mapping):
        return None
    state = str(entry.get("state") or "").strip().casefold()
    if state in {"duplicate", "superseded"}:
        return state
    research = entry.get("current_research_proof")
    research = research if isinstance(research, Mapping) else {}
    assessment = research.get("actionability_assessment")
    assessment = assessment if isinstance(assessment, Mapping) else {}
    disposition = str(assessment.get("disposition") or "").strip().casefold()
    return disposition if disposition in _TERMINAL_NEGATIVE_DISPOSITIONS else None


def _input_reused(stage_doc: Mapping[str, Any]) -> bool:
    meta = stage_doc.get("input_meta")
    if not isinstance(meta, Mapping):
        return False
    for key, value in meta.items():
        normalized = str(key).casefold()
        if any(marker in normalized for marker in ("reuse", "retained", "imported")):
            if value is not None and value is not False and value not in (0, "", [], {}):
                return True
    return False


def _reused_work_dependencies(stage_doc: Mapping[str, Any]) -> tuple[list[str], bool]:
    """Return explicit retained-work edges and whether they are a complete lineage set."""

    meta = stage_doc.get("input_meta")
    if not isinstance(meta, Mapping):
        return [], False
    dependency_ids: list[str] = []
    for field in (
        "dependency_work_unit_ids",
        "prior_work_unit_ids",
        "reused_work_unit_ids",
        "retained_work_unit_ids",
    ):
        raw = meta.get(field)
        values = [raw] if isinstance(raw, str) else raw
        if not isinstance(values, Sequence) or isinstance(values, (bytes, bytearray)):
            continue
        for value in values:
            candidate = str(value).strip() if isinstance(value, str) else ""
            if candidate and candidate not in dependency_ids:
                dependency_ids.append(candidate)
    lineage_complete = (
        bool(dependency_ids)
        and meta.get("reused_work_dependency_set_complete") is True
    )
    return dependency_ids, lineage_complete


def _model_manifest_paths(stage_doc: Mapping[str, Any]) -> list[Path]:
    input_meta = stage_doc.get("input_meta")
    input_meta = input_meta if isinstance(input_meta, Mapping) else {}
    contract = input_meta.get("model_invocation_contract")
    contract = contract if isinstance(contract, Mapping) else {}
    manifests = contract.get("manifests")
    if not isinstance(manifests, list):
        return []
    return [
        Path(str(ref["path"]))
        for ref in manifests
        if isinstance(ref, Mapping)
        and isinstance(ref.get("path"), str)
        and str(ref["path"]).strip()
    ]


def _research_attempts(
    stage_doc: Mapping[str, Any],
) -> list[tuple[Mapping[str, Any], Mapping[str, Any]]]:
    if str(stage_doc.get("stage") or "").strip() != "repro_research":
        return []
    items = stage_doc.get("items")
    if not isinstance(items, list):
        return []
    attempts: list[tuple[Mapping[str, Any], Mapping[str, Any]]] = []
    for item in items:
        if not isinstance(item, Mapping):
            continue
        raw_attempts = item.get("research_attempts")
        if not isinstance(raw_attempts, list):
            continue
        attempts.extend(
            (item, attempt)
            for attempt in raw_attempts
            if isinstance(attempt, Mapping)
        )
    return attempts


def _is_committed_progress_checkpoint(stage_doc: Mapping[str, Any]) -> bool:
    if str(stage_doc.get("stage") or "").strip() != "repro_research":
        return False
    input_meta = stage_doc.get("input_meta")
    return (
        isinstance(input_meta, Mapping)
        and input_meta.get("stage_status") == "checkpointed_progress"
    )


def _attempt_has_model_work(attempt: Mapping[str, Any]) -> bool:
    kind = str(attempt.get("attempt_kind") or "").strip()
    if kind in _NON_MODEL_RESEARCH_ATTEMPT_KINDS:
        return False
    run_dir = attempt.get("run_dir")
    return isinstance(run_dir, str) and bool(run_dir.strip())


def _attempt_run_dir(attempt: Mapping[str, Any]) -> Path | None:
    raw = attempt.get("run_dir")
    if not isinstance(raw, str) or not raw.strip():
        return None
    return Path(raw).resolve()


def _research_model_event_log_cases(
    stage_doc: Mapping[str, Any],
) -> dict[Path, set[str]]:
    logs: dict[Path, set[str]] = {}
    for item, attempt in _research_attempts(stage_doc):
        if not _attempt_has_model_work(attempt):
            continue
        run_dir = _attempt_run_dir(attempt)
        if run_dir is None:
            continue
        path = run_dir / "lifecycle_events.jsonl"
        case_id = item.get("case_id")
        if isinstance(case_id, str) and case_id.strip():
            logs.setdefault(path, set()).add(case_id.strip())
    return logs


def _research_model_event_logs(stage_doc: Mapping[str, Any]) -> list[Path]:
    return list(_research_model_event_log_cases(stage_doc))


def _manifest_invocation_id(path: Path) -> str | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return None
    invocation_id = value.get("invocation_id") if isinstance(value, Mapping) else None
    return invocation_id if isinstance(invocation_id, str) and invocation_id else None


def _model_telemetry_projection(
    stage_doc: Mapping[str, Any],
) -> _ModelTelemetryProjection:
    input_meta = stage_doc.get("input_meta")
    input_meta = input_meta if isinstance(input_meta, Mapping) else {}
    contract_raw = input_meta.get("model_invocation_contract")
    research_logs = _research_model_event_logs(stage_doc)
    issues: list[str] = []
    paths: list[Path] = []
    expected_ids: set[str] = set()
    completed_ids: set[str] = set()
    invocation_expected: bool | None = None

    if isinstance(contract_raw, Mapping):
        contract = dict(contract_raw)
        if contract.get("schema_version") != 1:
            issues.append("stage_model_invocation_contract_schema_invalid")
        invocation_expected_raw = contract.get("invocation_expected")
        invocation_expected = (
            invocation_expected_raw
            if isinstance(invocation_expected_raw, bool)
            else None
        )
        if invocation_expected is None:
            issues.append("stage_model_invocation_contract_expectation_invalid")
        expected_contract_hash = canonical_sha256(
            {key: value for key, value in contract.items() if key != "contract_sha256"}
        )
        if contract.get("contract_sha256") != expected_contract_hash:
            issues.append("stage_model_invocation_contract_hash_changed")

        manifests_raw = contract.get("manifests")
        manifests = manifests_raw if isinstance(manifests_raw, list) else []
        if not isinstance(manifests_raw, list):
            issues.append("stage_model_invocation_contract_manifests_invalid")
        for index, ref_raw in enumerate(manifests):
            ref = ref_raw if isinstance(ref_raw, Mapping) else {}
            path_raw = ref.get("path")
            path = (
                Path(str(path_raw))
                if isinstance(path_raw, str) and path_raw.strip()
                else None
            )
            if path is None or not path.is_file():
                issues.append(f"stage_model_invocation_ref_missing:{index}")
                continue
            paths.append(path)
            ref_sha = ref.get("sha256")
            if (
                not isinstance(ref_sha, str)
                or ref_sha != sha256(path.read_bytes()).hexdigest()
            ):
                issues.append(f"stage_model_invocation_ref_changed:{index}")
            invocation_id = _manifest_invocation_id(path)
            if invocation_id is None:
                issues.append(f"stage_model_invocation_id_missing:{index}")
                continue
            if invocation_id in expected_ids:
                issues.append(f"stage_model_invocation_id_duplicated:{invocation_id}")
            expected_ids.add(invocation_id)
            source_path = path.parent / "lifecycle_events.jsonl"
            if not source_path.is_file():
                issues.append(f"stage_model_invocation_event_log_missing:{index}")
                continue
            try:
                source_events = read_lifecycle_events(source_path)
            except Exception:  # noqa: BLE001 - retained telemetry remains unknown
                issues.append(f"stage_model_invocation_event_log_invalid:{index}")
                continue
            if any(
                event.event_type == "model.invocation.completed"
                and event.context.invocation_id == invocation_id
                for event in source_events
            ):
                completed_ids.add(invocation_id)
            else:
                issues.append(
                    f"stage_model_invocation_completion_missing:{invocation_id}"
                )
    elif not research_logs:
        issues.append("stage_model_invocation_contract_missing")

    if research_logs:
        invocation_expected = True
    for index, source_path in enumerate(research_logs):
        if not source_path.is_file():
            issues.append(f"stage_research_invocation_event_log_missing:{index}")
            continue
        try:
            source_events = read_lifecycle_events(source_path)
        except Exception:  # noqa: BLE001 - retained telemetry remains unknown
            issues.append(f"stage_research_invocation_event_log_invalid:{index}")
            continue
        started = {
            event.context.invocation_id
            for event in source_events
            if event.event_type == "model.invocation.started"
            and event.context.invocation_id is not None
        }
        completed_events = [
            event
            for event in source_events
            if event.event_type == "model.invocation.completed"
            and event.context.invocation_id is not None
        ]
        completed = {
            event.context.invocation_id for event in completed_events
        }
        if not started:
            issues.append(f"stage_research_invocation_start_missing:{index}")
        if not completed:
            issues.append(f"stage_research_invocation_completion_missing:{index}")
        for invocation_id in sorted(started - completed):
            issues.append(
                f"stage_research_invocation_completion_missing:{invocation_id}"
            )
        for invocation_id in sorted(completed - started):
            issues.append(f"stage_research_invocation_start_missing:{invocation_id}")
        for event in completed_events:
            if not _complete_token_usage(event):
                issues.append(
                    "stage_research_invocation_usage_incomplete:"
                    + str(event.context.invocation_id)
                )
        expected_ids.update(started or completed)
        completed_ids.update(completed)

    if invocation_expected is True and not expected_ids:
        issues.append("stage_model_invocation_manifest_missing")
    return _ModelTelemetryProjection(
        manifest_paths=tuple(paths),
        expected_invocation_ids=tuple(sorted(expected_ids)),
        completed_invocation_ids=tuple(sorted(completed_ids)),
        invocation_expected=invocation_expected,
        complete=not issues,
        issues=tuple(dict.fromkeys(issues)),
    )


def _link_model_invocation_events(
    *,
    stage_doc: Mapping[str, Any],
    global_path: Path,
    work_context: LifecycleContext,
    case_ids: Sequence[str],
    lifecycle_ids: Sequence[str],
    cycle: _CycleTelemetry,
) -> int:
    invocation_ids_by_log: dict[Path, set[str]] = {}
    case_lifecycle_by_id = dict(zip(case_ids, lifecycle_ids, strict=True))
    beneficiaries_by_log: dict[Path, set[str]] = {}
    for manifest_path in _model_manifest_paths(stage_doc):
        invocation_id = _manifest_invocation_id(manifest_path)
        if invocation_id is None:
            continue
        source_path = manifest_path.parent / "lifecycle_events.jsonl"
        invocation_ids_by_log.setdefault(source_path, set()).add(invocation_id)
        beneficiaries_by_log.setdefault(source_path, set()).update(lifecycle_ids)
    for source_path, source_case_ids in _research_model_event_log_cases(
        stage_doc
    ).items():
        if not source_path.is_file():
            continue
        try:
            source_events = read_lifecycle_events(source_path)
        except Exception:  # noqa: BLE001 - completeness is reported separately
            continue
        invocation_ids = {
            event.context.invocation_id
            for event in source_events
            if event.event_type
            in {"model.invocation.started", "model.invocation.completed"}
            and event.context.invocation_id is not None
        }
        if invocation_ids:
            invocation_ids_by_log.setdefault(source_path, set()).update(
                invocation_ids
            )
            beneficiaries_by_log[source_path] = {
                case_lifecycle_by_id[case_id]
                for case_id in source_case_ids
                if case_id in case_lifecycle_by_id
            }

    linked = 0
    allowed_types = {
        "model.invocation.completed",
        "error.occurred",
        "error.resolved",
        "intervention.started",
        "intervention.completed",
        "action.started",
        "action.completed",
    }
    for source_path, invocation_ids in sorted(
        invocation_ids_by_log.items(), key=lambda item: str(item[0])
    ):
        if not source_path.is_file() or source_path.resolve() == global_path.resolve():
            continue
        beneficiary_lifecycle_ids = tuple(
            sorted(beneficiaries_by_log.get(source_path, set()))
        )
        if not beneficiary_lifecycle_ids:
            continue
        beneficiary_case_ids = tuple(
            case_id
            for case_id, lifecycle_id in case_lifecycle_by_id.items()
            if lifecycle_id in beneficiary_lifecycle_ids
        )
        for source_event in read_lifecycle_events(source_path):
            if (
                source_event.event_type not in allowed_types
                or (
                    source_event.context.invocation_id is not None
                    and source_event.context.invocation_id not in invocation_ids
                )
            ):
                continue
            source_action_id = source_event.attributes.get("action_id")
            source_work_unit_id = source_event.context.work_unit_id or _stable_id(
                "linked-source-work",
                str(source_path.resolve()),
                source_event.context.invocation_id
                or source_event.intervention_id
                or (
                    source_action_id
                    if isinstance(source_action_id, str) and source_action_id
                    else source_event.event_id
                ),
            )
            linked_context = LifecycleContext(
                case_lifecycle_id=(
                    beneficiary_lifecycle_ids[0]
                    if len(beneficiary_lifecycle_ids) == 1
                    else None
                ),
                case_id=(
                    beneficiary_case_ids[0]
                    if len(beneficiary_case_ids) == 1
                    else None
                ),
                cycle_id=work_context.cycle_id,
                stage=work_context.stage,
                milestone_id=work_context.milestone_id,
                work_unit_id=source_work_unit_id,
                invocation_id=source_event.context.invocation_id,
                session_id=source_event.context.session_id,
                shared_work_id=(
                    source_event.context.shared_work_id
                    or (
                        source_work_unit_id
                        if len(beneficiary_lifecycle_ids) > 1
                        else None
                    )
                ),
                parent_action_id=source_event.context.parent_action_id,
                system_fingerprint={
                    **work_context.system_fingerprint,
                    **source_event.context.system_fingerprint,
                },
            )
            append_lifecycle_event(
                global_path,
                make_lifecycle_event(
                    source_event.event_type,
                    linked_context,
                    idempotency_key=(
                        f"linked:{cycle.cycle_id}:{source_event.event_id}"
                    ),
                    occurred_at=source_event.occurred_at,
                    started_at=source_event.started_at,
                    ended_at=source_event.ended_at,
                    active_seconds=source_event.active_seconds,
                    machine_wait_seconds=source_event.machine_wait_seconds,
                    external_wait_seconds=source_event.external_wait_seconds,
                    actor_type=source_event.actor_type,
                    initiator_type=source_event.initiator_type,
                    root_initiator_type=source_event.root_initiator_type,
                    origin=source_event.origin,
                    error_cluster_id=source_event.error_cluster_id,
                    intervention_id=source_event.intervention_id,
                    beneficiary_case_lifecycle_ids=beneficiary_lifecycle_ids,
                    evidence_paths=tuple(
                        dict.fromkeys((*source_event.evidence_paths, str(source_path)))
                    ),
                    artifact_hashes=source_event.artifact_hashes,
                    provenance_quality=source_event.provenance_quality,
                    attributes={
                        **source_event.attributes,
                        "stage_work_unit_id": work_context.work_unit_id,
                        "linked_source_event_id": source_event.event_id,
                        "linked_source_event_log": str(source_path),
                    },
                ),
            )
            linked += 1
    return linked


def _complete_token_usage(event: Any) -> bool:
    usage = event.attributes.get("token_usage")
    if not isinstance(usage, Mapping):
        return False
    required = (
        "input_tokens",
        "cached_input_tokens",
        "uncached_input_tokens",
        "output_tokens",
        "total_tokens",
    )
    if any(not isinstance(usage.get(field), int) for field in required):
        return False
    return isinstance(
        usage.get("reasoning_output_tokens", usage.get("reasoning_tokens")), int
    )


def _research_model_work_by_run(
    stage_doc: Mapping[str, Any],
) -> dict[Path, tuple[tuple[str, ...], bool]]:
    projected: dict[Path, tuple[tuple[str, ...], bool]] = {}
    for source_path in _research_model_event_logs(stage_doc):
        if not source_path.is_file():
            continue
        try:
            events = read_lifecycle_events(source_path)
        except Exception:  # noqa: BLE001 - missing cost remains explicitly incomplete
            continue
        completed = [
            event
            for event in events
            if event.event_type == "model.invocation.completed"
        ]
        work_ids = tuple(
            dict.fromkeys(
                event.context.work_unit_id
                for event in completed
                if event.context.work_unit_id is not None
            )
        )
        projected[source_path.parent.resolve()] = (
            work_ids,
            bool(completed)
            and len(work_ids) == len(completed)
            and all(_complete_token_usage(event) for event in completed),
        )
    return projected


def _validation_error_identity(error: str) -> str:
    normalized = " ".join(error.split())
    return normalized.partition(":details=")[0]


def _attempt_validation_errors(attempt: Mapping[str, Any]) -> list[str]:
    raw = attempt.get("validation_errors")
    if not isinstance(raw, list):
        return []
    return [
        normalized
        for value in raw
        for normalized in [" ".join(str(value).split())]
        if normalized
    ]


def _attempt_boundary(
    attempt: Mapping[str, Any],
    *,
    following_attempts: Sequence[Mapping[str, Any]],
    fallback: str,
) -> dict[str, Any]:
    run_dir = _attempt_run_dir(attempt)
    evidence_paths: list[str] = []
    report_path = attempt.get("report_path")
    if isinstance(report_path, str) and report_path.strip():
        evidence_paths.append(report_path)
    if _attempt_has_model_work(attempt) and run_dir is not None:
        meta_path = run_dir / "run_meta.json"
        if meta_path.is_file():
            evidence_paths.append(str(meta_path))
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, ValueError):
                meta = {}
            if isinstance(meta, Mapping):
                started = _timestamp_or_none(meta.get("run_started_utc"))
                ended = _timestamp_or_none(meta.get("run_finished_utc"))
                if ended is not None:
                    return {
                        "occurred_at": ended,
                        "started_at": started,
                        "timing_complete": started is not None,
                        "timestamp_semantics": "authoritative_run_boundary",
                        "evidence_paths": evidence_paths,
                    }

    for following in following_attempts:
        if not _attempt_has_model_work(following):
            continue
        following_dir = _attempt_run_dir(following)
        if following_dir is None:
            continue
        meta_path = following_dir / "run_meta.json"
        if not meta_path.is_file():
            continue
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError):
            continue
        if not isinstance(meta, Mapping):
            continue
        upper_bound = _timestamp_or_none(meta.get("run_started_utc"))
        if upper_bound is not None:
            evidence_paths.append(str(meta_path))
            return {
                "occurred_at": upper_bound,
                "started_at": None,
                "timing_complete": False,
                "timestamp_semantics": "observed_no_later_than_next_run",
                "evidence_paths": evidence_paths,
            }
    return {
        "occurred_at": fallback,
        "started_at": None,
        "timing_complete": False,
        "timestamp_semantics": "observed_no_later_than_stage_persistence",
        "evidence_paths": evidence_paths,
    }


def _context_with_session(
    context: LifecycleContext, session_id: object
) -> LifecycleContext:
    raw = context.to_dict()
    raw["session_id"] = session_id if isinstance(session_id, str) else None
    return LifecycleContext.from_dict(raw)


def _record_research_validation_errors(
    *,
    item: Mapping[str, Any],
    context: LifecycleContext,
    global_path: Path,
    case_registry_path: Path,
    occurred_at: str,
    cycle: _CycleTelemetry,
    model_work_by_run: Mapping[Path, tuple[tuple[str, ...], bool]],
) -> None:
    attempts_raw = item.get("research_attempts")
    attempts = (
        [attempt for attempt in attempts_raw if isinstance(attempt, Mapping)]
        if isinstance(attempts_raw, list)
        else []
    )
    if not attempts:
        return

    observations = [
        {
            "attempt": attempt,
            "boundary": _attempt_boundary(
                attempt,
                following_attempts=attempts[index + 1 :],
                fallback=occurred_at,
            ),
        }
        for index, attempt in enumerate(attempts)
    ]
    open_clusters: dict[str, dict[str, Any]] = {}
    episode_by_identity: dict[str, int] = {}
    fields = {
        **_event_fields(cycle),
        "provenance_quality": "artifact_derived",
    }

    for observation in observations:
        attempt = observation["attempt"]
        boundary = observation["boundary"]
        run_dir = _attempt_run_dir(attempt)
        work_ids: tuple[str, ...] = ()
        work_complete = False
        if _attempt_has_model_work(attempt) and run_dir is not None:
            work_ids, work_complete = model_work_by_run.get(run_dir, ((), False))
            for state in open_clusters.values():
                state["resolution_work_unit_ids"].update(work_ids)
                if not work_ids or not work_complete:
                    state["resolution_cost_attribution_complete"] = False

        errors = _attempt_validation_errors(attempt)
        current_identities = {_validation_error_identity(error) for error in errors}
        resolved_identities = sorted(set(open_clusters) - current_identities)
        for identity in resolved_identities:
            state = open_clusters.pop(identity)
            source_session = state.get("source_session_id")
            observed_session = attempt.get("observed_agent_session_id")
            resumed_session = attempt.get("resumed_from_session_id")
            same_author = bool(
                isinstance(source_session, str)
                and source_session
                and observed_session == source_session
                and resumed_session == source_session
            )
            cluster_id = str(state["cluster_id"])
            resolution_work_unit_ids = sorted(
                state["resolution_work_unit_ids"]
            )
            timing_unknown = bool(
                state["resolution_timing_unknown"]
                or not boundary["timing_complete"]
            )
            _append_case_event(
                global_path=global_path,
                case_registry_path=case_registry_path,
                event=make_lifecycle_event(
                    "error.resolved",
                    _context_with_session(context, observed_session),
                    idempotency_key=f"{cluster_id}:resolved",
                    occurred_at=boundary["occurred_at"],
                    error_cluster_id=cluster_id,
                    evidence_paths=tuple(boundary["evidence_paths"]),
                    attributes={
                        "error_kind": state["error_kind"],
                        "validation_error_identity": identity,
                        "resolution_mode": (
                            "self_healed_same_author"
                            if same_author
                            else "self_healed_controller"
                        ),
                        "resolution_work_unit_ids": resolution_work_unit_ids,
                        "resolution_cost_attribution_complete": bool(
                            resolution_work_unit_ids
                            and state["resolution_cost_attribution_complete"]
                        ),
                        "resolution_timing_unknown": timing_unknown,
                        "resolution_timestamp_semantics": boundary[
                            "timestamp_semantics"
                        ],
                        "attempt_number": attempt.get("attempt_number"),
                        "attempt_kind": attempt.get("attempt_kind"),
                        "attempt_outcome": attempt.get("outcome"),
                        "telemetry_layer": "stage_validation",
                    },
                    **fields,
                ),
            )

        for error_index, error in enumerate(errors):
            identity = _validation_error_identity(error)
            state = open_clusters.get(identity)
            if state is None:
                episode = episode_by_identity.get(identity, 0) + 1
                episode_by_identity[identity] = episode
                cluster_id = _stable_id(
                    "error",
                    context.case_lifecycle_id,
                    context.stage,
                    identity,
                    episode,
                )
                source_session = attempt.get("observed_agent_session_id") or attempt.get(
                    "agent_session_id"
                )
                state = {
                    "cluster_id": cluster_id,
                    "error_kind": identity.partition(":")[0],
                    "source_session_id": source_session,
                    "resolution_work_unit_ids": set(),
                    "resolution_cost_attribution_complete": True,
                    "resolution_timing_unknown": not boundary["timing_complete"],
                }
                open_clusters[identity] = state
            cluster_id = str(state["cluster_id"])
            _append_case_event(
                global_path=global_path,
                case_registry_path=case_registry_path,
                event=make_lifecycle_event(
                    "error.occurred",
                    _context_with_session(
                        context, attempt.get("observed_agent_session_id")
                    ),
                    idempotency_key=(
                        f"{cluster_id}:occurrence:{attempt.get('attempt_number')}:"
                        f"{error_index}:{sha256(error.encode('utf-8')).hexdigest()}"
                    ),
                    occurred_at=boundary["occurred_at"],
                    error_cluster_id=cluster_id,
                    evidence_paths=tuple(boundary["evidence_paths"]),
                    attributes={
                        "error_kind": state["error_kind"],
                        "validation_error": error,
                        "validation_error_identity": identity,
                        "resolution_timing_unknown": not boundary[
                            "timing_complete"
                        ],
                        "occurrence_timestamp_semantics": boundary[
                            "timestamp_semantics"
                        ],
                        "source_work_unit_ids": list(work_ids),
                        "attempt_number": attempt.get("attempt_number"),
                        "attempt_kind": attempt.get("attempt_kind"),
                        "attempt_outcome": attempt.get("outcome"),
                        "telemetry_layer": "stage_validation",
                    },
                    **fields,
                ),
            )


def _research_item_completed_at(item: object, *, fallback: str) -> str:
    if not isinstance(item, Mapping):
        return fallback
    attempts_raw = item.get("research_attempts")
    attempts = (
        [attempt for attempt in attempts_raw if isinstance(attempt, Mapping)]
        if isinstance(attempts_raw, list)
        else []
    )
    if not attempts:
        return fallback
    boundary = _attempt_boundary(
        attempts[-1],
        following_attempts=(),
        fallback=fallback,
    )
    return str(boundary["occurred_at"])


def record_stage_telemetry(
    *,
    case_registry: Mapping[str, Any],
    case_registry_path: Path,
    stage_doc: Mapping[str, Any],
) -> None:
    """Project one persisted stage into idempotent lifecycle telemetry.

    No duration or token value is estimated. Stage artifacts without a measured
    interval remain timestamped milestones; invocation receipts provide cost later.
    """

    stage = str(stage_doc.get("stage") or "unknown").strip()
    milestone = _STAGE_MILESTONES.get(stage, stage)
    occurred_at = _timestamp(stage_doc.get("generated_at"))
    cases = _case_ids(stage_doc)
    if not cases:
        return
    cycle = _cycle_for(case_registry_path)
    global_path = case_registry_path.parent / "lifecycle_events.jsonl"
    lifecycle_ids = [
        case_lifecycle_id(case_registry_path=case_registry_path, case_id=case_id)
        for case_id in cases
    ]
    stage_digest = canonical_sha256(dict(stage_doc))
    work_unit_id = _stable_id("work", cycle.cycle_id, stage, stage_digest)
    progress_checkpoint = _is_committed_progress_checkpoint(stage_doc)
    shared_work_id = (
        work_unit_id if len(cases) > 1 and not progress_checkpoint else None
    )
    fields = _event_fields(cycle)
    reused_input = _input_reused(stage_doc)
    dependency_ids, reused_lineage_complete = _reused_work_dependencies(stage_doc)
    reused_cost_unknown = reused_input and not reused_lineage_complete
    model_projection = _model_telemetry_projection(stage_doc)
    cost_unknown_reasons: list[str] = []
    if reused_cost_unknown:
        cost_unknown_reasons.append("reused_prior_work_missing_complete_dependency_lineage")
    if not model_projection.complete:
        cost_unknown_reasons.append("model_usage_telemetry_incomplete")

    work_context = LifecycleContext(
        cycle_id=cycle.cycle_id,
        stage=stage,
        milestone_id=milestone,
        work_unit_id=None if progress_checkpoint else work_unit_id,
        shared_work_id=shared_work_id,
        system_fingerprint=cycle.system_fingerprint,
    )
    if progress_checkpoint:
        checkpoint_context = LifecycleContext(
            cycle_id=cycle.cycle_id,
            stage=stage,
            system_fingerprint=cycle.system_fingerprint,
        )
        append_lifecycle_event(
            global_path,
            make_lifecycle_event(
                "stage.checkpointed",
                checkpoint_context,
                idempotency_key=(
                    f"{cycle.cycle_id}:{stage}:checkpoint:{stage_digest}"
                ),
                occurred_at=occurred_at,
                beneficiary_case_lifecycle_ids=tuple(lifecycle_ids),
                attributes={
                    "stage": stage,
                    "checkpoint_status": "checkpointed_progress",
                    "checkpoint_scope": "committed_case_prefix",
                    "model_usage_telemetry_complete": model_projection.complete,
                    "model_usage_telemetry_issues": list(model_projection.issues),
                    "model_invocation_expected": model_projection.invocation_expected,
                    "expected_model_invocation_ids": list(
                        model_projection.expected_invocation_ids
                    ),
                    "completed_model_invocation_ids": list(
                        model_projection.completed_invocation_ids
                    ),
                    "artifact_sha256": stage_digest,
                    "beneficiary_case_ids": cases,
                },
                **fields,
            ),
        )
    else:
        append_lifecycle_event(
            global_path,
            make_lifecycle_event(
                "work.reused" if reused_input else "work.completed",
                work_context,
                idempotency_key=f"{work_unit_id}:work",
                occurred_at=occurred_at,
                beneficiary_case_lifecycle_ids=tuple(lifecycle_ids),
                attributes={
                    "stage": stage,
                    "milestone_id": milestone,
                    "dependency_ids": dependency_ids,
                    "cost_unknown": bool(cost_unknown_reasons),
                    "cost_unknown_reason": (
                        ",".join(cost_unknown_reasons) if cost_unknown_reasons else None
                    ),
                    "resource_time_unknown": True,
                    "resource_time_unknown_reason": (
                        "stage_boundary_active_time_not_retained"
                    ),
                    "model_usage_telemetry_complete": model_projection.complete,
                    "model_usage_telemetry_issues": list(model_projection.issues),
                    "model_invocation_expected": model_projection.invocation_expected,
                    "expected_model_invocation_ids": list(
                        model_projection.expected_invocation_ids
                    ),
                    "completed_model_invocation_ids": list(
                        model_projection.completed_invocation_ids
                    ),
                    "cost_status": (
                        "linked_reused_prior_work"
                        if reused_input
                        else "stage_boundary_only"
                    ),
                    "reused_work_dependency_set_complete": reused_lineage_complete,
                    "artifact_sha256": stage_digest,
                    "beneficiary_case_ids": cases,
                },
                **fields,
            ),
        )
    _link_model_invocation_events(
        stage_doc=stage_doc,
        global_path=global_path,
        work_context=work_context,
        case_ids=cases,
        lifecycle_ids=lifecycle_ids,
        cycle=cycle,
    )
    model_work_by_run = _research_model_work_by_run(stage_doc)

    items = stage_doc.get("items")
    item_by_case = (
        {
            str(item.get("case_id")): item
            for item in items
            if isinstance(item, Mapping)
        }
        if isinstance(items, list)
        else {}
    )
    registry_cases = case_registry.get("cases")
    registry_cases = registry_cases if isinstance(registry_cases, Mapping) else {}
    existing_stage_completions: set[tuple[str, str]] = set()
    if stage == "repro_research" and global_path.is_file():
        for retained_event in read_lifecycle_events(global_path):
            retained_stage = retained_event.context.stage or str(
                retained_event.attributes.get("stage") or ""
            ).strip()
            retained_lifecycle_id = retained_event.context.case_lifecycle_id
            if (
                retained_event.event_type == "stage.completed"
                and retained_lifecycle_id is not None
                and retained_stage
            ):
                existing_stage_completions.add(
                    (retained_lifecycle_id, retained_stage)
                )
    for case_id, lifecycle_id in zip(cases, lifecycle_ids, strict=True):
        context = LifecycleContext(
            case_lifecycle_id=lifecycle_id,
            case_id=case_id,
            cycle_id=cycle.cycle_id,
            stage=stage,
            milestone_id=milestone,
            shared_work_id=shared_work_id,
            system_fingerprint=cycle.system_fingerprint,
        )
        item = item_by_case.get(case_id)
        case_completed_at = (
            _research_item_completed_at(item, fallback=occurred_at)
            if progress_checkpoint
            else occurred_at
        )
        registry_entry = registry_cases.get(case_id)
        origin_atom_ids = _origin_atom_ids(item, registry_entry)
        atom_created_at = _earliest_atom_timestamp(origin_atom_ids)
        _append_case_event(
            global_path=global_path,
            case_registry_path=case_registry_path,
            event=make_lifecycle_event(
                "lifecycle.opened",
                context,
                idempotency_key=f"{lifecycle_id}:opened",
                occurred_at=occurred_at,
                started_at=occurred_at,
                attributes={
                    "admission_boundary": "first_persisted_stage_observation",
                    "origin_ids": origin_atom_ids,
                    "atom_created_at": atom_created_at,
                    "earliest_raw_atom_status": (
                        "artifact_derived_from_atom_id"
                        if atom_created_at is not None
                        else "unknown_unless_linked_by_source_telemetry"
                    ),
                },
                **fields,
            ),
        )
        stage_completion_identity = (lifecycle_id, stage)
        if stage_completion_identity not in existing_stage_completions:
            _append_case_event(
                global_path=global_path,
                case_registry_path=case_registry_path,
                event=make_lifecycle_event(
                    "stage.completed",
                    context,
                    idempotency_key=(
                        f"{lifecycle_id}:{stage}:completed"
                        if stage == "repro_research"
                        else f"{lifecycle_id}:{work_unit_id}:completed"
                    ),
                    occurred_at=case_completed_at,
                    attributes={
                        "stage": stage,
                        "milestone_id": milestone,
                        "stage_work_unit_id": (
                            None if progress_checkpoint else work_unit_id
                        ),
                        "shared": shared_work_id is not None,
                        "completion_scope": (
                            "committed_case_prefix"
                            if progress_checkpoint
                            else "persisted_stage"
                        ),
                        "checkpoint_artifact_sha256": (
                            stage_digest if progress_checkpoint else None
                        ),
                    },
                    **fields,
                ),
            )
            existing_stage_completions.add(stage_completion_identity)
        if isinstance(item, Mapping):
            _record_research_validation_errors(
                item=item,
                context=context,
                global_path=global_path,
                case_registry_path=case_registry_path,
                occurred_at=occurred_at,
                cycle=cycle,
                model_work_by_run=model_work_by_run,
            )

        not_emitted = isinstance(item, Mapping) and item.get("ticket_stage") == "not_emitted"
        disposition = (
            _disposition_for_case(case_registry=case_registry, case_id=case_id)
            if stage == "ticket_assembly" and not_emitted
            else None
        )
        if disposition is None:
            continue
        disposition_event = make_lifecycle_event(
            "disposition.verified",
            context,
            idempotency_key=f"{lifecycle_id}:disposition:{disposition}",
            occurred_at=occurred_at,
            attributes={
                "disposition": disposition,
                "verified": True,
                "closure_valid": True,
                "historical_product_implementation_cost_included": False,
                "current_revalidation_cost_included": True,
            },
            **fields,
        )
        _append_case_event(
            global_path=global_path,
            case_registry_path=case_registry_path,
            event=disposition_event,
        )
        closed_event = make_lifecycle_event(
            "lifecycle.closed",
            context,
            idempotency_key=f"{lifecycle_id}:closed",
            occurred_at=occurred_at,
            started_at=occurred_at,
            ended_at=occurred_at,
            attributes={"status": "complete", "disposition": disposition},
            **fields,
        )
        _append_case_event(
            global_path=global_path,
            case_registry_path=case_registry_path,
            event=closed_event,
        )
        case_root = _case_root(case_registry_path, lifecycle_id)
        write_lifecycle_manifest(
            case_root / "lifecycle_manifest.json",
            LifecycleManifest(
                case_lifecycle_id=lifecycle_id,
                case_id=case_id,
                created_at=occurred_at,
                updated_at=occurred_at,
                status="terminal",
                shared_work_ids=(shared_work_id,) if shared_work_id is not None else (),
                system_fingerprint=cycle.system_fingerprint,
                provenance_quality="authoritative" if cycle.automatic else "unknown",
                metadata={
                    "disposition": disposition,
                    "global_event_log_path": str(global_path),
                },
            ),
        )

    try:
        materialize_lifecycle_metrics(
            event_sources=[global_path],
            output_dir=global_path.parent,
        )
    except Exception as exc:  # noqa: BLE001 - metrics cannot block case disposition
        warnings.warn(
            f"automatic pipeline metrics refresh failed for {global_path}: {exc}",
            RuntimeWarning,
            stacklevel=2,
        )


__all__ = [
    "bind_ticket_lifecycle_ids",
    "case_lifecycle_id",
    "record_stage_telemetry",
]
