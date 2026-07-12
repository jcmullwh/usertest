"""Content-bound, same-author healing for independently adjudicated shadow output.

This module is deliberately stage- and benchmark-neutral.  Qualification identifies a
concrete authoring frontier and supplies feedback; stage adapters own parsing and local
contract validation.  A correction is *not* proof that the semantic finding is fixed.
It only establishes that the exact author/session/workspace returned a locally valid
revision which acknowledges the bound feedback.  A fresh independent adjudication is
therefore mandatory, and a same-corpus repair can never earn release qualification.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from hashlib import sha256
from typing import Any, Protocol

from backlog_miner.prompt_correction import (
    CorrectionAssessment,
    CorrectionInvocationFailure,
    CorrectionObservation,
    CorrectionRunResult,
    correction_run_metrics,
    correction_state_sha256,
    run_progressive_correction,
)

_CONSUMPTION_SCHEMA_VERSION = 1
_PENDING_REPAIR_SCHEMA_VERSION = 1


def _canonical_hash(value: Any) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


def _content_hash(document: Mapping[str, Any]) -> str:
    return _canonical_hash(
        {key: value for key, value in document.items() if key != "content_sha256"}
    )


def _text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def _valid_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value.casefold())
    )


def _route_without_hash(route: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in route.items() if key != "route_sha256"}


def qualification_correction_route_errors(route: Any) -> list[str]:
    """Validate an open correction route without enumerating stages or defect kinds."""

    if not isinstance(route, Mapping):
        return ["qualification_correction_route_invalid"]
    errors: list[str] = []
    if route.get("schema_version") != 1:
        errors.append("qualification_correction_route_schema_invalid")
    route_sha256 = route.get("route_sha256")
    if not _valid_sha256(route_sha256):
        errors.append("qualification_correction_route_sha256_invalid")
    elif route_sha256 != _canonical_hash(_route_without_hash(route)):
        errors.append("qualification_correction_route_sha256_mismatch")
    if _text(route.get("feedback_kind")) is None:
        errors.append("qualification_correction_feedback_kind_missing")
    if _text(route.get("authoring_stage")) is None:
        errors.append("qualification_correction_authoring_stage_missing")
    restart = _text(route.get("restart_from_stage"))
    stages_raw = route.get("rerun_downstream_stages")
    stages = (
        [item.strip() for item in stages_raw if isinstance(item, str) and item.strip()]
        if isinstance(stages_raw, list)
        else []
    )
    if restart is None or not stages or stages[0] != restart or len(stages) != len(set(stages)):
        errors.append("qualification_correction_stage_frontier_invalid")
    if route.get("route_status") not in {
        "same_author_resume",
        "author_provenance_unavailable",
        "uncorrectable",
    }:
        errors.append("qualification_correction_route_status_invalid")
    if route.get("correctability") not in {"correctable", "uncorrectable", "unknown"}:
        errors.append("qualification_correction_correctability_invalid")
    rationale = _text(route.get("rationale"))
    if rationale is None:
        errors.append("qualification_correction_rationale_missing")
    label_ids = route.get("actionable_label_ids")
    if not isinstance(label_ids, list) or any(_text(item) is None for item in label_ids):
        errors.append("qualification_correction_label_ids_invalid")
    causal_target = route.get("causal_target")
    if causal_target is not None:
        if not isinstance(causal_target, Mapping):
            errors.append("qualification_correction_causal_target_invalid")
        else:
            for field in (
                "problem_ids",
                "case_ids",
                "evidence_atom_ids",
                "actionable_label_ids",
                "expected_item_keys",
            ):
                values = causal_target.get(field)
                if not isinstance(values, list) or any(_text(item) is None for item in values):
                    errors.append(
                        f"qualification_correction_causal_target_field_invalid:{field}"
                    )
    provenance = route.get("author_provenance")
    if route.get("route_status") == "same_author_resume":
        session_id = _text(route.get("agent_session_id"))
        workspace_dir = _text(route.get("workspace_dir"))
        if not isinstance(provenance, Mapping):
            errors.append("qualification_correction_author_provenance_missing")
        else:
            if provenance.get("exact_session_continuation") is not True:
                errors.append("qualification_correction_session_continuity_unverified")
            if provenance.get("workspace_continuity_verified") is not True:
                errors.append("qualification_correction_workspace_continuity_unverified")
            if _text(provenance.get("agent_session_id")) != session_id:
                errors.append("qualification_correction_session_binding_mismatch")
            if _text(provenance.get("workspace_dir")) != workspace_dir:
                errors.append("qualification_correction_workspace_binding_mismatch")
        if session_id is None:
            errors.append("qualification_correction_session_missing")
        if workspace_dir is None:
            errors.append("qualification_correction_workspace_missing")
    return list(dict.fromkeys(errors))


def correction_feedback_document(
    route: Mapping[str, Any],
    *,
    source_pending_run_sha256: str,
    source_adjudication_sha256: str,
) -> dict[str, Any]:
    """Build the immutable feedback packet shown to the originating author."""

    errors = qualification_correction_route_errors(route)
    if errors:
        raise ValueError("qualification_correction_route_invalid:" + ",".join(errors))
    if not _valid_sha256(source_pending_run_sha256):
        raise ValueError("qualification_correction_pending_run_sha256_invalid")
    if not _valid_sha256(source_adjudication_sha256):
        raise ValueError("qualification_correction_adjudication_sha256_invalid")
    feedback: dict[str, Any] = {
        "schema_version": 1,
        "contract_kind": "qualification_author_feedback",
        "route_sha256": route["route_sha256"],
        "source_pending_run_sha256": source_pending_run_sha256,
        "source_adjudication_sha256": source_adjudication_sha256,
        "feedback_kind": route["feedback_kind"],
        "authoring_stage": route["authoring_stage"],
        "target_identity": route.get("target_identity"),
        "actionable_label_ids": list(route.get("actionable_label_ids") or []),
        "bad_severity": route.get("bad_severity"),
        "bad_categories": list(route.get("bad_categories") or []),
        "rationale": route["rationale"],
        "causal_target": (
            dict(route["causal_target"])
            if isinstance(route.get("causal_target"), Mapping)
            else None
        ),
        "evidence_atom_ids": list(
            dict.fromkeys(
                atom_id
                for source in (
                    route.get("causal_target"),
                    route.get("author_provenance"),
                )
                if isinstance(source, Mapping)
                for atom_id in source.get("evidence_atom_ids", [])
                if isinstance(atom_id, str) and atom_id.strip()
            )
        ),
        "independent_finding": {
            key: route.get(key)
            for key in (
                "quality",
                "bad_severity",
                "bad_categories",
                "rationale",
                "actionable_label_ids",
                "correctability",
            )
        },
        "instruction": (
            "Continue the exact authoring conversation. Correct the retained output using "
            "the bound finding, preserve independently valid work, and return the complete "
            "stage response. Do not claim the finding is resolved merely because you edited "
            "the response; independent re-adjudication remains authoritative."
        ),
    }
    feedback["content_sha256"] = _content_hash(feedback)
    return feedback


@dataclass(frozen=True)
class AuthorRevision:
    """One exact-author response projected by a stage adapter."""

    payload: Any
    validation_errors: tuple[str, ...]
    valid_item_keys: tuple[str, ...]
    agent_session_id: str
    workspace_dir: str
    cost_seconds: float = 0.0


class ExactAuthorInvoker(Protocol):
    def __call__(
        self,
        *,
        route: Mapping[str, Any],
        feedback: Mapping[str, Any],
        current_payload: Any,
        attempt_number: int,
        prior_assessment: CorrectionAssessment | None,
    ) -> AuthorRevision: ...


class DownstreamRerunner(Protocol):
    def __call__(
        self,
        *,
        accepted_repairs: Sequence[Mapping[str, Any]],
        stages: Sequence[str],
    ) -> Mapping[str, Any]: ...


def _external_finding_errors(route: Mapping[str, Any]) -> tuple[str, ...]:
    categories = [
        str(item).strip()
        for item in route.get("bad_categories", [])
        if isinstance(item, str) and item.strip()
    ]
    # Each independently reported finding is a separate error identity.  This is
    # intentionally open: unknown categories are preserved verbatim, not rejected.
    if categories:
        return tuple(f"independent_finding:{category}" for category in categories)
    feedback_kind = _text(route.get("feedback_kind")) or "unknown"
    return (f"independent_finding:{feedback_kind}",)


def _compatible_route_groups(
    routes: Sequence[Mapping[str, Any]],
) -> list[list[Mapping[str, Any]]]:
    """Group findings that the same author must resolve on one shared frontier."""

    groups: list[list[Mapping[str, Any]]] = []
    group_index: dict[str, int] = {}
    for index, route in enumerate(routes):
        provenance = route.get("author_provenance")
        provenance_map = provenance if isinstance(provenance, Mapping) else {}
        if route.get("route_status") != "same_author_resume":
            key = f"unavailable:{index}"
        else:
            key = _canonical_hash(
                {
                    "authoring_stage": route.get("authoring_stage"),
                    "agent_session_id": route.get("agent_session_id"),
                    "workspace_dir": route.get("workspace_dir"),
                    "author_attempt_identity": route.get("author_attempt_identity"),
                    "repository_revision": provenance_map.get("repository_revision"),
                    "workspace_manifest_sha256": provenance_map.get(
                        "workspace_manifest_sha256"
                    ),
                    "shared_response_identity": (
                        provenance_map.get("shared_response_identity")
                        or provenance_map.get("shared_response_sha256")
                        or (
                            route.get("author_attempt_identity", {}).get(
                                "response_sha256"
                            )
                            if isinstance(route.get("author_attempt_identity"), Mapping)
                            else None
                        )
                    ),
                    "assignment_identity": {
                        field: provenance_map.get(field)
                        for field in (
                            "assignment_id",
                            "assignment_tag",
                            "miner_id",
                            "miner_tag",
                            "relation_review_batch_id",
                            "relation_review_batch_tag",
                            "stage1_correction_adapter",
                            "author_role",
                        )
                    },
                    "restart_from_stage": route.get("restart_from_stage"),
                    "rerun_downstream_stages": route.get("rerun_downstream_stages"),
                }
            )
        if key not in group_index:
            group_index[key] = len(groups)
            groups.append([])
        groups[group_index[key]].append(route)
    return groups


def _group_feedback(
    routes: Sequence[Mapping[str, Any]],
    *,
    source_pending_run_sha256: str,
    source_adjudication_sha256: str,
) -> dict[str, Any]:
    individual = [
        correction_feedback_document(
            route,
            source_pending_run_sha256=source_pending_run_sha256,
            source_adjudication_sha256=source_adjudication_sha256,
        )
        for route in routes
    ]
    if len(individual) == 1:
        return individual[0]
    primary = individual[0]
    feedback: dict[str, Any] = {
        **primary,
        "feedback_kind": "grouped_independent_findings",
        "grouped_route_sha256s": [route["route_sha256"] for route in routes],
        "grouped_feedback_sha256s": [item["content_sha256"] for item in individual],
        "target_identities": [route.get("target_identity") for route in routes],
        "actionable_label_ids": list(
            dict.fromkeys(
                label_id
                for route in routes
                for label_id in route.get("actionable_label_ids", [])
                if isinstance(label_id, str) and label_id.strip()
            )
        ),
        "evidence_atom_ids": list(
            dict.fromkeys(
                atom_id
                for route in routes
                for provenance in [route.get("author_provenance")]
                if isinstance(provenance, Mapping)
                for atom_id in provenance.get("evidence_atom_ids", [])
                if isinstance(atom_id, str) and atom_id.strip()
            )
        ),
        "assigned_atom_ids": list(
            dict.fromkeys(
                atom_id
                for route in routes
                for provenance in [route.get("author_provenance")]
                if isinstance(provenance, Mapping)
                for atom_id in provenance.get("assigned_atom_ids", [])
                if isinstance(atom_id, str) and atom_id.strip()
            )
        ),
        "bad_categories": list(
            dict.fromkeys(
                category
                for route in routes
                for category in route.get("bad_categories", [])
                if isinstance(category, str) and category.strip()
            )
        ),
        "findings": [
            {
                "route_sha256": route["route_sha256"],
                "target_identity": route.get("target_identity"),
                "bad_severity": route.get("bad_severity"),
                "bad_categories": list(route.get("bad_categories") or []),
                "rationale": route.get("rationale"),
                "actionable_label_ids": list(route.get("actionable_label_ids") or []),
                "evidence_atom_ids": list(
                    dict.fromkeys(
                        atom_id
                        for source in (
                            route.get("causal_target"),
                            route.get("author_provenance"),
                        )
                        if isinstance(source, Mapping)
                        for atom_id in source.get("evidence_atom_ids", [])
                        if isinstance(atom_id, str) and atom_id.strip()
                    )
                ),
                "independent_finding": dict(item.get("independent_finding") or {}),
                "problem_id": (
                    route.get("author_provenance", {}).get("problem_id")
                    if isinstance(route.get("author_provenance"), Mapping)
                    else None
                ),
                "case_id": (
                    route.get("author_provenance", {}).get("case_id")
                    if isinstance(route.get("author_provenance"), Mapping)
                    else None
                ),
            }
            for route, item in zip(routes, individual, strict=True)
        ],
        "rationale": "\n\n".join(
            f"[{route['route_sha256']}] {route.get('rationale')}" for route in routes
        ),
    }
    feedback.pop("content_sha256", None)
    feedback["content_sha256"] = _content_hash(feedback)
    return feedback


def _required_valid_item_keys(route: Mapping[str, Any]) -> tuple[str, ...]:
    causal_target = route.get("causal_target")
    if not isinstance(causal_target, Mapping):
        return ()
    values = causal_target.get("expected_item_keys")
    if not isinstance(values, list):
        return ()
    return tuple(
        dict.fromkeys(
            value.strip() for value in values if isinstance(value, str) and value.strip()
        )
    )


def _attempt_projection(
    observation: CorrectionObservation[Any],
    *,
    attempt_number: int,
) -> dict[str, Any]:
    return {
        "attempt_number": attempt_number,
        "payload_sha256": _canonical_hash(observation.payload),
        "validation_errors": list(observation.validation_errors),
        "valid_item_keys": list(observation.valid_item_keys),
        "state_sha256": observation.state_sha256,
        "agent_session_id": observation.agent_session_id,
        "continuity_key": observation.continuity_key,
        "cost_seconds": max(0.0, float(observation.cost_seconds)),
    }


def _assessment_projection(assessment: CorrectionAssessment) -> dict[str, Any]:
    return {
        "decision": assessment.decision,
        "reason": assessment.reason,
        "resolved_error_identities": list(assessment.resolved_error_identities),
        "introduced_error_identities": list(assessment.introduced_error_identities),
        "before_error_count": assessment.before_error_count,
        "after_error_count": assessment.after_error_count,
        "repeated_state": assessment.repeated_state,
        "safe_frontier_updated": assessment.safe_frontier_updated,
        "global_best_updated": assessment.global_best_updated,
        "reset_progress_clock": assessment.reset_progress_clock,
    }


def _frontier_observation_projection(
    observation: CorrectionObservation[Any],
) -> dict[str, Any]:
    return {
        "payload": observation.payload,
        "validation_errors": list(observation.validation_errors),
        "state_sha256": observation.state_sha256,
        "valid_item_keys": list(observation.valid_item_keys),
        "agent_session_id": observation.agent_session_id,
        "continuity_key": observation.continuity_key,
        "cost_seconds": max(0.0, float(observation.cost_seconds)),
    }


def _correction_resume_frontier(
    correction: CorrectionRunResult[Any],
    *,
    route_group: Sequence[Mapping[str, Any]],
    feedback_sha256: str,
    source_pending_run_sha256: str,
    source_adjudication_sha256: str,
) -> dict[str, Any]:
    route = route_group[0]
    frontier: dict[str, Any] = {
        "schema_version": 1,
        "contract_kind": "qualification_correction_resume_frontier",
        "route_sha256s": [item["route_sha256"] for item in route_group],
        "feedback_sha256": feedback_sha256,
        "source_pending_run_sha256": source_pending_run_sha256,
        "source_adjudication_sha256": source_adjudication_sha256,
        "agent_session_id": route.get("agent_session_id"),
        "workspace_dir": route.get("workspace_dir"),
        "continuity_key": correction.current.continuity_key,
        "status": correction.status,
        "current": _frontier_observation_projection(correction.current),
        "best": _frontier_observation_projection(correction.best),
        "attempts": [
            _frontier_observation_projection(attempt)
            for attempt in correction.attempts
        ],
        "assessments": [
            _assessment_projection(assessment) for assessment in correction.assessments
        ],
        "invocation_failures": [
            {
                "attempt_number": failure.attempt_number,
                "agent_session_id": failure.agent_session_id,
                "error_type": failure.error_type,
                "error_message": failure.error_message,
                "failure_identity": failure.failure_identity,
                "cost_seconds": max(0.0, float(failure.cost_seconds)),
            }
            for failure in correction.invocation_failures
        ],
        "operational_error": correction.operational_error,
        "correction_cost_since_progress": max(
            0.0, float(correction.correction_cost_since_progress)
        ),
        "total_correction_cost": max(0.0, float(correction.total_correction_cost)),
        "next_attempt_number": (
            len(correction.attempts) + len(correction.invocation_failures) + 1
        ),
    }
    frontier["content_sha256"] = _content_hash(frontier)
    return frontier


def _frontier_string_tuple(value: Any, *, error: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError(error)
    return tuple(value)


def _frontier_observation(value: Any) -> CorrectionObservation[Any]:
    if not isinstance(value, Mapping) or "payload" not in value:
        raise ValueError("qualification_correction_resume_observation_invalid")
    errors = _frontier_string_tuple(
        value.get("validation_errors"),
        error="qualification_correction_resume_observation_errors_invalid",
    )
    valid_keys = _frontier_string_tuple(
        value.get("valid_item_keys"),
        error="qualification_correction_resume_observation_keys_invalid",
    )
    state_sha256 = value.get("state_sha256")
    expected_state = correction_state_sha256(
        candidate=value["payload"],
        validation_errors=list(errors),
        valid_item_keys=list(valid_keys),
    )
    if state_sha256 != expected_state:
        raise ValueError("qualification_correction_resume_observation_state_mismatch")
    cost = value.get("cost_seconds")
    if (
        not isinstance(cost, (int, float))
        or isinstance(cost, bool)
        or float(cost) < 0.0
    ):
        raise ValueError("qualification_correction_resume_observation_cost_invalid")
    return CorrectionObservation(
        payload=value["payload"],
        validation_errors=errors,
        state_sha256=str(state_sha256),
        valid_item_keys=valid_keys,
        agent_session_id=_text(value.get("agent_session_id")),
        continuity_key=_text(value.get("continuity_key")),
        cost_seconds=float(cost),
    )


def _frontier_assessment(value: Any) -> CorrectionAssessment:
    if not isinstance(value, Mapping):
        raise ValueError("qualification_correction_resume_assessment_invalid")
    decision = _text(value.get("decision"))
    reason = _text(value.get("reason"))
    before_count = value.get("before_error_count")
    after_count = value.get("after_error_count")
    bool_fields = {
        field: value.get(field)
        for field in (
            "repeated_state",
            "safe_frontier_updated",
            "global_best_updated",
            "reset_progress_clock",
        )
    }
    if (
        decision is None
        or reason is None
        or not isinstance(before_count, int)
        or isinstance(before_count, bool)
        or before_count < 0
        or not isinstance(after_count, int)
        or isinstance(after_count, bool)
        or after_count < 0
        or any(not isinstance(item, bool) for item in bool_fields.values())
    ):
        raise ValueError("qualification_correction_resume_assessment_fields_invalid")
    return CorrectionAssessment(
        decision=decision,
        reason=reason,
        resolved_error_identities=_frontier_string_tuple(
            value.get("resolved_error_identities"),
            error="qualification_correction_resume_assessment_resolved_invalid",
        ),
        introduced_error_identities=_frontier_string_tuple(
            value.get("introduced_error_identities"),
            error="qualification_correction_resume_assessment_introduced_invalid",
        ),
        before_error_count=before_count,
        after_error_count=after_count,
        repeated_state=bool_fields["repeated_state"],
        safe_frontier_updated=bool_fields["safe_frontier_updated"],
        global_best_updated=bool_fields["global_best_updated"],
        reset_progress_clock=bool_fields["reset_progress_clock"],
    )


def _frontier_invocation_failure(value: Any) -> CorrectionInvocationFailure:
    if not isinstance(value, Mapping):
        raise ValueError("qualification_correction_resume_failure_invalid")
    attempt_number = value.get("attempt_number")
    cost = value.get("cost_seconds")
    fields = {
        field: _text(value.get(field))
        for field in (
            "agent_session_id",
            "error_type",
            "error_message",
            "failure_identity",
        )
    }
    if (
        not isinstance(attempt_number, int)
        or isinstance(attempt_number, bool)
        or attempt_number < 2
        or not isinstance(cost, (int, float))
        or isinstance(cost, bool)
        or float(cost) < 0.0
        or any(item is None for item in fields.values())
    ):
        raise ValueError("qualification_correction_resume_failure_fields_invalid")
    return CorrectionInvocationFailure(
        attempt_number=attempt_number,
        agent_session_id=str(fields["agent_session_id"]),
        error_type=str(fields["error_type"]),
        error_message=str(fields["error_message"]),
        failure_identity=str(fields["failure_identity"]),
        cost_seconds=float(cost),
    )


def qualification_correction_resume_frontier(
    value: Any,
    *,
    route_group: Sequence[Mapping[str, Any]],
    feedback_sha256: str,
    source_pending_run_sha256: str,
    source_adjudication_sha256: str,
) -> CorrectionRunResult[Any]:
    """Validate and restore one exact-author correction frontier from durable JSON."""

    if not isinstance(value, Mapping):
        raise ValueError("qualification_correction_resume_frontier_invalid")
    observed_hash = value.get("content_sha256")
    if observed_hash != _content_hash(value):
        raise ValueError("qualification_correction_resume_frontier_hash_invalid")
    route = route_group[0]
    expected_routes = [item["route_sha256"] for item in route_group]
    if (
        value.get("schema_version") != 1
        or value.get("contract_kind") != "qualification_correction_resume_frontier"
        or value.get("route_sha256s") != expected_routes
        or value.get("feedback_sha256") != feedback_sha256
        or value.get("source_pending_run_sha256") != source_pending_run_sha256
        or value.get("source_adjudication_sha256") != source_adjudication_sha256
    ):
        raise ValueError("qualification_correction_resume_frontier_input_mismatch")
    if (
        value.get("agent_session_id") != route.get("agent_session_id")
        or value.get("workspace_dir") != route.get("workspace_dir")
        or value.get("continuity_key") != _continuity_key(route)
    ):
        raise ValueError("qualification_correction_resume_frontier_binding_mismatch")
    status = _text(value.get("status"))
    if status is None or not status.startswith("repairable_paused:"):
        raise ValueError("qualification_correction_resume_frontier_status_invalid")
    attempts_raw = value.get("attempts")
    assessments_raw = value.get("assessments")
    failures_raw = value.get("invocation_failures")
    if (
        not isinstance(attempts_raw, list)
        or not attempts_raw
        or not isinstance(assessments_raw, list)
        or not isinstance(failures_raw, list)
    ):
        raise ValueError("qualification_correction_resume_frontier_history_invalid")
    attempts = tuple(_frontier_observation(item) for item in attempts_raw)
    current = _frontier_observation(value.get("current"))
    best = _frontier_observation(value.get("best"))
    expected_session = _text(route.get("agent_session_id"))
    expected_continuity = _continuity_key(route)
    if (
        current.agent_session_id != expected_session
        or best.agent_session_id != expected_session
        or current.continuity_key != expected_continuity
        or best.continuity_key != expected_continuity
    ):
        raise ValueError("qualification_correction_resume_frontier_binding_mismatch")
    failures = tuple(_frontier_invocation_failure(item) for item in failures_raw)
    expected_next = len(attempts) + len(failures) + 1
    if value.get("next_attempt_number") != expected_next:
        raise ValueError("qualification_correction_resume_frontier_attempt_number_invalid")
    costs = [
        value.get("correction_cost_since_progress"),
        value.get("total_correction_cost"),
    ]
    if any(
        not isinstance(item, (int, float))
        or isinstance(item, bool)
        or float(item) < 0.0
        for item in costs
    ):
        raise ValueError("qualification_correction_resume_frontier_cost_invalid")
    return CorrectionRunResult(
        status=status,
        current=current,
        best=best,
        attempts=attempts,
        assessments=tuple(_frontier_assessment(item) for item in assessments_raw),
        invocation_failures=failures,
        operational_error=(
            str(value["operational_error"])
            if value.get("operational_error") is not None
            else None
        ),
        correction_cost_since_progress=float(costs[0]),
        total_correction_cost=float(costs[1]),
    )


def _continuity_key(route: Mapping[str, Any]) -> str:
    provenance = route.get("author_provenance")
    return _canonical_hash(
        {
            "agent_session_id": route.get("agent_session_id"),
            "workspace_dir": route.get("workspace_dir"),
            "repository_revision": (
                provenance.get("repository_revision")
                if isinstance(provenance, Mapping)
                else None
            ),
            "author_attempt_identity": route.get("author_attempt_identity"),
        }
    )


def _validate_author_revision(
    revision: AuthorRevision,
    *,
    route: Mapping[str, Any],
) -> tuple[str, ...]:
    errors = list(revision.validation_errors)
    if revision.agent_session_id != route.get("agent_session_id"):
        errors.append("qualification_correction_exact_author_session_changed")
    if revision.workspace_dir != route.get("workspace_dir"):
        errors.append("qualification_correction_exact_author_workspace_changed")
    return tuple(dict.fromkeys(str(error) for error in errors if str(error).strip()))


def consume_qualification_corrections(
    *,
    routes: Sequence[Mapping[str, Any]],
    source_pending_run_sha256: str,
    source_adjudication_sha256: str,
    load_current_payload: Callable[[Mapping[str, Any]], Any],
    invoke_exact_author: ExactAuthorInvoker,
    rerun_downstream: DownstreamRerunner,
    resume_frontiers: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Consume route feedback, preserving every frontier and rerunning only downstream.

    There is no attempt-count cap.  The only ordinary economic pause is when correction
    cost since the last objective improvement reaches the originating authoring cost.
    Exact recurrence, exact-session loss, or an explicitly uncorrectable route may stop a
    chain earlier.  A changed mistake with fewer total errors is objective progress through
    :mod:`backlog_miner.prompt_correction` and resets the economic clock.
    """

    if not _valid_sha256(source_pending_run_sha256):
        raise ValueError("qualification_correction_pending_run_sha256_invalid")
    if not _valid_sha256(source_adjudication_sha256):
        raise ValueError("qualification_correction_adjudication_sha256_invalid")
    route_errors = [
        f"route={index}:{error}"
        for index, route in enumerate(routes)
        for error in qualification_correction_route_errors(route)
    ]
    if route_errors:
        raise ValueError("qualification_correction_routes_invalid:" + ",".join(route_errors))

    resume_frontier_map = dict(resume_frontiers or {})
    route_sha256s = {str(route.get("route_sha256")) for route in routes}
    unknown_resume_routes = sorted(set(resume_frontier_map) - route_sha256s)
    if unknown_resume_routes:
        raise ValueError(
            "qualification_correction_resume_frontier_route_unknown:"
            + ",".join(unknown_resume_routes)
        )

    route_receipts_by_sha256: dict[str, dict[str, Any]] = {}
    accepted_repairs: list[dict[str, Any]] = []
    accepted_route_count = 0
    rerun_stage_order: list[str] = []
    for route_group in _compatible_route_groups(routes):
        route = route_group[0]
        feedback = _group_feedback(
            route_group,
            source_pending_run_sha256=source_pending_run_sha256,
            source_adjudication_sha256=source_adjudication_sha256,
        )
        if route.get("route_status") != "same_author_resume":
            receipt = {
                "route_sha256": route["route_sha256"],
                "feedback_sha256": feedback["content_sha256"],
                "status": (
                    "uncorrectable"
                    if route.get("route_status") == "uncorrectable"
                    else "repairable_paused:author_provenance_unavailable"
                ),
                "authored_work_disposition": "retained",
                "attempts": [],
                "assessments": [],
                "current_payload_sha256": None,
                "best_payload_sha256": None,
                "accepted_payload_sha256": None,
                "rerun_downstream_stages": [],
            }
            receipt["content_sha256"] = _content_hash(receipt)
            route_receipts_by_sha256[str(route["route_sha256"])] = receipt
            continue

        continuity_key = _continuity_key(route)
        supplied_frontiers = [
            resume_frontier_map[str(route_item["route_sha256"])]
            for route_item in route_group
            if str(route_item["route_sha256"]) in resume_frontier_map
        ]
        if supplied_frontiers and any(
            item != supplied_frontiers[0] for item in supplied_frontiers[1:]
        ):
            raise ValueError("qualification_correction_resume_frontier_group_conflict")
        resume_from = (
            qualification_correction_resume_frontier(
                supplied_frontiers[0],
                route_group=route_group,
                feedback_sha256=str(feedback["content_sha256"]),
                source_pending_run_sha256=source_pending_run_sha256,
                source_adjudication_sha256=source_adjudication_sha256,
            )
            if supplied_frontiers
            else None
        )
        if resume_from is None:
            current_payload = load_current_payload(route)
            initial_errors = tuple(
                f"{route_item['route_sha256']}:{error}"
                for route_item in route_group
                for error in _external_finding_errors(route_item)
            )
            initial = CorrectionObservation(
                payload=current_payload,
                validation_errors=initial_errors,
                state_sha256=correction_state_sha256(
                    candidate=current_payload,
                    validation_errors=list(initial_errors),
                ),
                valid_item_keys=(),
                agent_session_id=str(route["agent_session_id"]),
                continuity_key=continuity_key,
                cost_seconds=0.0,
            )
        else:
            initial = resume_from.current

        def invoke(
            current: CorrectionObservation[Any],
            attempt_number: int,
            prior_assessment: CorrectionAssessment | None,
            *,
            _route: Mapping[str, Any] = route,
            _feedback: Mapping[str, Any] = feedback,
            _continuity_key: str = continuity_key,
            _route_group: tuple[Mapping[str, Any], ...] = tuple(route_group),
        ) -> CorrectionObservation[Any]:
            revision = invoke_exact_author(
                route=_route,
                feedback=_feedback,
                current_payload=current.payload,
                attempt_number=attempt_number,
                prior_assessment=prior_assessment,
            )
            errors = list(_validate_author_revision(revision, route=_route))
            valid_keys = set(revision.valid_item_keys)
            for grouped_route in _route_group:
                required_keys = _required_valid_item_keys(grouped_route)
                if required_keys and not set(required_keys).issubset(valid_keys):
                    errors.append(
                        "qualification_correction_target_omitted:"
                        + str(grouped_route["route_sha256"])
                    )
            errors = list(dict.fromkeys(errors))
            return CorrectionObservation(
                payload=revision.payload,
                validation_errors=tuple(errors),
                state_sha256=correction_state_sha256(
                    candidate=revision.payload,
                    validation_errors=list(errors),
                    valid_item_keys=list(revision.valid_item_keys),
                ),
                valid_item_keys=tuple(revision.valid_item_keys),
                agent_session_id=revision.agent_session_id,
                continuity_key=(
                    _continuity_key
                    if revision.workspace_dir == _route.get("workspace_dir")
                    else _canonical_hash(
                        {
                            "expected": _continuity_key,
                            "observed_workspace_dir": revision.workspace_dir,
                        }
                    )
                ),
                cost_seconds=max(0.0, float(revision.cost_seconds)),
            )

        original_cost_candidates = [
            provenance.get("original_author_cost_seconds")
            for route_item in route_group
            for provenance in [route_item.get("author_provenance")]
            if isinstance(provenance, Mapping)
            and isinstance(provenance.get("original_author_cost_seconds"), (int, float))
            and not isinstance(provenance.get("original_author_cost_seconds"), bool)
            and float(provenance.get("original_author_cost_seconds")) > 0.0
        ]
        original_cost = (
            max(float(item) for item in original_cost_candidates)
            if original_cost_candidates
            else None
        )
        correction = run_progressive_correction(
            initial=initial,
            resume_from=resume_from,
            invoke_correction=invoke,
            pause_policy=(
                (
                    lambda _current, _assessment, since_progress, _total,
                    _original_cost=original_cost: (
                        "correction_cost_reached_original_authoring_cost"
                        if since_progress >= _original_cost
                        else None
                    )
                )
                if original_cost is not None
                else None
            ),
        )
        accepted = correction.status in {"accepted", "corrected"}
        retained = correction.current if accepted else correction.best
        receipt_base = {
            "feedback_sha256": feedback["content_sha256"],
            "grouped_route_sha256s": [
                route_item["route_sha256"] for route_item in route_group
            ],
            "status": correction.status,
            "authored_work_disposition": "retained",
            "attempts": [
                _attempt_projection(attempt, attempt_number=index)
                for index, attempt in enumerate(correction.attempts, start=1)
            ],
            "assessments": [
                _assessment_projection(assessment) for assessment in correction.assessments
            ],
            "invocation_failures": [
                {
                    "attempt_number": failure.attempt_number,
                    "agent_session_id": failure.agent_session_id,
                    "error_type": failure.error_type,
                    "error_message": failure.error_message,
                    "failure_identity": failure.failure_identity,
                    "cost_seconds": failure.cost_seconds,
                }
                for failure in correction.invocation_failures
            ],
            "metrics": correction_run_metrics(correction, expected_quality=None),
            "current_payload_sha256": _canonical_hash(correction.current.payload),
            "best_payload_sha256": _canonical_hash(correction.best.payload),
            "accepted_payload_sha256": (
                _canonical_hash(retained.payload) if accepted else None
            ),
            "rerun_downstream_stages": (
                list(route["rerun_downstream_stages"]) if accepted else []
            ),
            "correction_frontier": _correction_resume_frontier(
                correction,
                route_group=route_group,
                feedback_sha256=str(feedback["content_sha256"]),
                source_pending_run_sha256=source_pending_run_sha256,
                source_adjudication_sha256=source_adjudication_sha256,
            ),
        }
        for route_item in route_group:
            receipt = {
                "route_sha256": route_item["route_sha256"],
                **receipt_base,
            }
            receipt["content_sha256"] = _content_hash(receipt)
            route_receipts_by_sha256[str(route_item["route_sha256"])] = receipt
        if accepted:
            accepted_repairs.append(
                {
                    "route": dict(route),
                    "routes": [dict(route_item) for route_item in route_group],
                    "feedback": feedback,
                    "payload": retained.payload,
                    "route_consumption_receipt_sha256": route_receipts_by_sha256[
                        str(route["route_sha256"])
                    ]["content_sha256"],
                    "route_consumption_receipt_sha256s": {
                        str(route_item["route_sha256"]): route_receipts_by_sha256[
                            str(route_item["route_sha256"])
                        ]["content_sha256"]
                        for route_item in route_group
                    },
                }
            )
            accepted_route_count += len(route_group)
            for stage in route["rerun_downstream_stages"]:
                if stage not in rerun_stage_order:
                    rerun_stage_order.append(stage)

    downstream = (
        dict(
            rerun_downstream(
                accepted_repairs=accepted_repairs,
                stages=rerun_stage_order,
            )
        )
        if accepted_repairs
        else {}
    )
    route_receipts = [
        route_receipts_by_sha256[str(route["route_sha256"])] for route in routes
    ]
    consumption: dict[str, Any] = {
        "schema_version": _CONSUMPTION_SCHEMA_VERSION,
        "contract_kind": "qualification_correction_consumption",
        "source_pending_run_sha256": source_pending_run_sha256,
        "source_adjudication_sha256": source_adjudication_sha256,
        "route_set_sha256": _canonical_hash([route["route_sha256"] for route in routes]),
        "route_receipts": route_receipts,
        "accepted_repair_count": accepted_route_count,
        "accepted_repair_group_count": len(accepted_repairs),
        "unresolved_route_count": len(routes) - accepted_route_count,
        "rerun_downstream_stages": rerun_stage_order,
        "downstream_result": downstream,
        "downstream_result_sha256": _canonical_hash(downstream),
        "same_corpus_feedback_exposed": True,
        "release_qualification_eligible": False,
        "fresh_independent_readjudication_required": True,
    }
    consumption["content_sha256"] = _content_hash(consumption)
    return consumption


def qualification_correction_consumption_errors(value: Any) -> list[str]:
    if not isinstance(value, Mapping):
        return ["qualification_correction_consumption_invalid"]
    errors: list[str] = []
    if value.get("schema_version") != _CONSUMPTION_SCHEMA_VERSION:
        errors.append("qualification_correction_consumption_schema_invalid")
    if value.get("contract_kind") != "qualification_correction_consumption":
        errors.append("qualification_correction_consumption_kind_invalid")
    content_sha256 = value.get("content_sha256")
    if not _valid_sha256(content_sha256):
        errors.append("qualification_correction_consumption_sha256_invalid")
    elif content_sha256 != _content_hash(value):
        errors.append("qualification_correction_consumption_sha256_mismatch")
    for field in (
        "source_pending_run_sha256",
        "source_adjudication_sha256",
        "route_set_sha256",
        "downstream_result_sha256",
    ):
        if not _valid_sha256(value.get(field)):
            errors.append(f"qualification_correction_consumption_hash_invalid:{field}")
    if value.get("same_corpus_feedback_exposed") is not True:
        errors.append("qualification_correction_same_corpus_exposure_missing")
    if value.get("release_qualification_eligible") is not False:
        errors.append("qualification_correction_must_not_qualify_release")
    if value.get("fresh_independent_readjudication_required") is not True:
        errors.append("qualification_correction_readjudication_not_required")
    receipts = value.get("route_receipts")
    if not isinstance(receipts, list) or any(not isinstance(item, Mapping) for item in receipts):
        errors.append("qualification_correction_route_receipts_invalid")
    elif value.get("route_set_sha256") != _canonical_hash(
        [item.get("route_sha256") for item in receipts]
    ):
        errors.append("qualification_correction_route_set_mismatch")
    downstream = value.get("downstream_result")
    if not isinstance(downstream, Mapping):
        errors.append("qualification_correction_downstream_result_invalid")
    elif value.get("downstream_result_sha256") != _canonical_hash(downstream):
        errors.append("qualification_correction_downstream_result_mismatch")
    return list(dict.fromkeys(errors))


def build_pending_repaired_shadow_run(
    *,
    correction_consumption: Mapping[str, Any],
    qualification_manifest_sha256: str,
    repaired_backlog_sha256: str,
    repaired_pending_run_sha256: str,
    repaired_artifact_receipts: Sequence[Mapping[str, Any]],
    correction_consumption_path: str,
    parent_cycle_contract_path: str | None = None,
    parent_cycle_contract_sha256: str | None = None,
    qualification_input_bundle_path: str | None = None,
    qualification_input_bundle_sha256: str | None = None,
    shared_shadow_state_path: str | None = None,
) -> dict[str, Any]:
    """Bind repaired artifacts for a new, independently adjudicated shadow phase."""

    errors = qualification_correction_consumption_errors(correction_consumption)
    if errors:
        raise ValueError("qualification_correction_consumption_invalid:" + ",".join(errors))
    if not _valid_sha256(qualification_manifest_sha256):
        raise ValueError("qualification_repair_manifest_sha256_invalid")
    if not _valid_sha256(repaired_backlog_sha256):
        raise ValueError("qualification_repair_backlog_sha256_invalid")
    if not _valid_sha256(repaired_pending_run_sha256):
        raise ValueError("qualification_repair_pending_run_sha256_invalid")
    if _text(correction_consumption_path) is None:
        raise ValueError("qualification_repair_consumption_path_invalid")
    parent_values = (
        parent_cycle_contract_path,
        parent_cycle_contract_sha256,
        qualification_input_bundle_path,
        qualification_input_bundle_sha256,
        shared_shadow_state_path,
    )
    sealed_parent_bound = any(value is not None for value in parent_values)
    if sealed_parent_bound and (
        any(value is None for value in parent_values)
        or not _valid_sha256(parent_cycle_contract_sha256)
        or not _valid_sha256(qualification_input_bundle_sha256)
        or _text(parent_cycle_contract_path) is None
        or _text(qualification_input_bundle_path) is None
        or _text(shared_shadow_state_path) is None
    ):
        raise ValueError("qualification_repair_parent_binding_invalid")
    receipts = [dict(receipt) for receipt in repaired_artifact_receipts]
    pending: dict[str, Any] = {
        "schema_version": _PENDING_REPAIR_SCHEMA_VERSION,
        "contract_kind": "pending_repaired_shadow_run",
        "qualification_manifest_sha256": qualification_manifest_sha256,
        "source_pending_run_sha256": correction_consumption["source_pending_run_sha256"],
        "source_adjudication_sha256": correction_consumption[
            "source_adjudication_sha256"
        ],
        "correction_consumption_sha256": correction_consumption["content_sha256"],
        "correction_consumption_path": correction_consumption_path,
        "parent_cycle_contract_path": parent_cycle_contract_path,
        "parent_cycle_contract_sha256": parent_cycle_contract_sha256,
        "qualification_input_bundle_path": qualification_input_bundle_path,
        "qualification_input_bundle_sha256": qualification_input_bundle_sha256,
        "shared_shadow_state_path": shared_shadow_state_path,
        "sealed_parent_bound": sealed_parent_bound,
        "repaired_backlog_sha256": repaired_backlog_sha256,
        "repaired_pending_run_sha256": repaired_pending_run_sha256,
        "repaired_artifact_receipts": receipts,
        "repaired_artifact_receipts_sha256": _canonical_hash(receipts),
        "same_corpus_feedback_exposed": True,
        "release_qualification_eligible": False,
        "fresh_independent_readjudication_required": True,
    }
    pending["content_sha256"] = _content_hash(pending)
    return pending


def pending_repaired_shadow_run_errors(value: Any) -> list[str]:
    if not isinstance(value, Mapping):
        return ["pending_repaired_shadow_run_invalid"]
    errors: list[str] = []
    if value.get("schema_version") != _PENDING_REPAIR_SCHEMA_VERSION:
        errors.append("pending_repaired_shadow_run_schema_invalid")
    if value.get("contract_kind") != "pending_repaired_shadow_run":
        errors.append("pending_repaired_shadow_run_kind_invalid")
    content_sha256 = value.get("content_sha256")
    if not _valid_sha256(content_sha256):
        errors.append("pending_repaired_shadow_run_sha256_invalid")
    elif content_sha256 != _content_hash(value):
        errors.append("pending_repaired_shadow_run_sha256_mismatch")
    for field in (
        "qualification_manifest_sha256",
        "source_pending_run_sha256",
        "source_adjudication_sha256",
        "correction_consumption_sha256",
        "repaired_backlog_sha256",
        "repaired_pending_run_sha256",
        "repaired_artifact_receipts_sha256",
    ):
        if not _valid_sha256(value.get(field)):
            errors.append(f"pending_repaired_shadow_run_hash_invalid:{field}")
    if _text(value.get("correction_consumption_path")) is None:
        errors.append("pending_repaired_shadow_run_consumption_path_invalid")
    sealed_parent_bound = value.get("sealed_parent_bound")
    if not isinstance(sealed_parent_bound, bool):
        errors.append("pending_repaired_shadow_run_parent_bound_invalid")
    if sealed_parent_bound is True:
        for field in (
            "parent_cycle_contract_sha256",
            "qualification_input_bundle_sha256",
        ):
            if not _valid_sha256(value.get(field)):
                errors.append(f"pending_repaired_shadow_run_hash_invalid:{field}")
        for field in (
            "parent_cycle_contract_path",
            "qualification_input_bundle_path",
            "shared_shadow_state_path",
        ):
            if _text(value.get(field)) is None:
                errors.append(f"pending_repaired_shadow_run_path_invalid:{field}")
    elif any(
        value.get(field) is not None
        for field in (
            "parent_cycle_contract_path",
            "parent_cycle_contract_sha256",
            "qualification_input_bundle_path",
            "qualification_input_bundle_sha256",
            "shared_shadow_state_path",
        )
    ):
        errors.append("pending_repaired_shadow_run_parent_binding_partial")
    receipts = value.get("repaired_artifact_receipts")
    if not isinstance(receipts, list) or any(not isinstance(item, Mapping) for item in receipts):
        errors.append("pending_repaired_shadow_run_artifact_receipts_invalid")
    elif value.get("repaired_artifact_receipts_sha256") != _canonical_hash(receipts):
        errors.append("pending_repaired_shadow_run_artifact_receipts_mismatch")
    if value.get("same_corpus_feedback_exposed") is not True:
        errors.append("pending_repaired_shadow_run_exposure_missing")
    if value.get("release_qualification_eligible") is not False:
        errors.append("pending_repaired_shadow_run_must_not_qualify_release")
    if value.get("fresh_independent_readjudication_required") is not True:
        errors.append("pending_repaired_shadow_run_readjudication_not_required")
    return list(dict.fromkeys(errors))


__all__ = [
    "AuthorRevision",
    "build_pending_repaired_shadow_run",
    "consume_qualification_corrections",
    "correction_feedback_document",
    "pending_repaired_shadow_run_errors",
    "qualification_correction_consumption_errors",
    "qualification_correction_resume_frontier",
    "qualification_correction_route_errors",
]
