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


def _timestamp(value: Any) -> str:
    if isinstance(value, str) and value.strip():
        candidate = value.strip()
        try:
            parsed = datetime.fromisoformat(candidate.replace("Z", "+00:00"))
        except ValueError:
            pass
        else:
            if parsed.tzinfo is not None:
                return candidate
    return utc_now()


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
    if not isinstance(contract_raw, Mapping):
        return _ModelTelemetryProjection(
            manifest_paths=(),
            expected_invocation_ids=(),
            completed_invocation_ids=(),
            invocation_expected=None,
            complete=False,
            issues=("stage_model_invocation_contract_missing",),
        )

    contract = dict(contract_raw)
    issues: list[str] = []
    if contract.get("schema_version") != 1:
        issues.append("stage_model_invocation_contract_schema_invalid")
    invocation_expected_raw = contract.get("invocation_expected")
    invocation_expected = (
        invocation_expected_raw if isinstance(invocation_expected_raw, bool) else None
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
    paths: list[Path] = []
    expected_ids: set[str] = set()
    completed_ids: set[str] = set()
    for index, ref_raw in enumerate(manifests):
        ref = ref_raw if isinstance(ref_raw, Mapping) else {}
        path_raw = ref.get("path")
        path = Path(str(path_raw)) if isinstance(path_raw, str) and path_raw.strip() else None
        if path is None or not path.is_file():
            issues.append(f"stage_model_invocation_ref_missing:{index}")
            continue
        paths.append(path)
        ref_sha = ref.get("sha256")
        if not isinstance(ref_sha, str) or ref_sha != sha256(path.read_bytes()).hexdigest():
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
        except Exception:  # noqa: BLE001 - retained telemetry remains unknown, not fatal
            issues.append(f"stage_model_invocation_event_log_invalid:{index}")
            continue
        if any(
            event.event_type == "model.invocation.completed"
            and event.context.invocation_id == invocation_id
            for event in source_events
        ):
            completed_ids.add(invocation_id)
        else:
            issues.append(f"stage_model_invocation_completion_missing:{invocation_id}")

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
    lifecycle_ids: Sequence[str],
    cycle: _CycleTelemetry,
) -> int:
    invocation_ids_by_log: dict[Path, set[str]] = {}
    for manifest_path in _model_manifest_paths(stage_doc):
        invocation_id = _manifest_invocation_id(manifest_path)
        if invocation_id is None:
            continue
        invocation_ids_by_log.setdefault(
            manifest_path.parent / "lifecycle_events.jsonl", set()
        ).add(invocation_id)

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
                cycle_id=work_context.cycle_id,
                stage=work_context.stage,
                milestone_id=work_context.milestone_id,
                work_unit_id=source_work_unit_id,
                invocation_id=source_event.context.invocation_id,
                session_id=source_event.context.session_id,
                shared_work_id=(
                    source_event.context.shared_work_id
                    or (source_work_unit_id if len(lifecycle_ids) > 1 else None)
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
                    beneficiary_case_lifecycle_ids=tuple(lifecycle_ids),
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
    shared_work_id = work_unit_id if len(cases) > 1 else None
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
        work_unit_id=work_unit_id,
        shared_work_id=shared_work_id,
        system_fingerprint=cycle.system_fingerprint,
    )
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
                "resource_time_unknown_reason": "stage_boundary_active_time_not_retained",
                "model_usage_telemetry_complete": model_projection.complete,
                "model_usage_telemetry_issues": list(model_projection.issues),
                "model_invocation_expected": model_projection.invocation_expected,
                "expected_model_invocation_ids": list(model_projection.expected_invocation_ids),
                "completed_model_invocation_ids": list(model_projection.completed_invocation_ids),
                "cost_status": (
                    "linked_reused_prior_work" if reused_input else "stage_boundary_only"
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
        lifecycle_ids=lifecycle_ids,
        cycle=cycle,
    )

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
        _append_case_event(
            global_path=global_path,
            case_registry_path=case_registry_path,
            event=make_lifecycle_event(
                "stage.completed",
                context,
                idempotency_key=f"{lifecycle_id}:{work_unit_id}:completed",
                occurred_at=occurred_at,
                attributes={
                    "stage": stage,
                    "milestone_id": milestone,
                    "stage_work_unit_id": work_unit_id,
                    "shared": shared_work_id is not None,
                },
                **fields,
            ),
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
