# ruff: noqa: E501,F401,F403,F405
from __future__ import annotations

from collections.abc import Mapping

from backlog_core import provisional_same_cause_group_errors
from backlog_miner.prompt_correction import (
    CorrectionRunResult,
    acquire_author_session,
    correction_metrics_with_session_acquisition,
)

from usertest_backlog.shared import *
from usertest_backlog.workflows.downstream_hydration import (
    hydrate_retained_downstream_chain,
    hydrate_retained_no_change_disposition,
)
from usertest_backlog.workflows.research_hydration import hydrate_retained_research_proof

_PRIORITY_FORBIDDEN_SOLUTION_FIELDS = frozenset(
    {
        "proposed_fix",
        "selected_solution",
        "family_id",
        "option_id",
        "implementation_steps",
    }
)

_RESEARCH_ROUTE_REVISION = "runner_research_route_v3"
_PROVISIONAL_RESEARCH_UNIT_WAIT_ROUTE = "await_provisional_research_unit"
_RESEARCH_DISPATCH_ROUTES = frozenset(
    {"research_new", "research_update", "resume_prior", "reassess_actionability"}
)
_DOWNSTREAM_ELIGIBLE_ROUTES = frozenset(
    {*_RESEARCH_DISPATCH_ROUTES, "continue_downstream", "await_outcome"}
)
_RESEARCH_ROUTE_ORDER = {
    "research_update": 0,
    "research_new": 1,
    "resume_prior": 2,
    "reassess_actionability": 3,
    "await_evidence": 4,
    _PROVISIONAL_RESEARCH_UNIT_WAIT_ROUTE: 5,
    "continue_downstream": 6,
    "await_outcome": 7,
}
_PRIORITY_BUCKET_ORDER = {"p0": 0, "p1": 1, "p2": 2, "p3": 3, "watch": 4}


def _prior_research_summary(record: Mapping[str, Any]) -> dict[str, Any] | None:
    prior_raw = record.get("prior_stage_context")
    prior = prior_raw if isinstance(prior_raw, Mapping) else {}
    research_raw = prior.get("research")
    research = research_raw if isinstance(research_raw, Mapping) else {}
    current = research.get("current")
    return dict(current) if isinstance(current, Mapping) else None


def _prior_priority_route_reference(record: Mapping[str, Any]) -> dict[str, Any] | None:
    prior_raw = record.get("prior_stage_context")
    prior = prior_raw if isinstance(prior_raw, Mapping) else {}
    refs_raw = prior.get("artifact_refs")
    refs = refs_raw if isinstance(refs_raw, Mapping) else {}
    stage_raw = refs.get("problem_prioritization")
    stage = stage_raw if isinstance(stage_raw, Mapping) else {}
    item_refs = stage.get("item_refs")
    if not isinstance(item_refs, list):
        return None
    case_id = _coerce_string(record.get("case_id"))
    problem_id = _coerce_string(record.get("problem_id"))
    for raw in reversed(item_refs):
        if not isinstance(raw, Mapping):
            continue
        if case_id is not None and _coerce_string(raw.get("case_id")) == case_id:
            return dict(raw)
        if problem_id is not None and _coerce_string(raw.get("problem_id")) == problem_id:
            return dict(raw)
    return None


def _research_frontier_sha256(record: Mapping[str, Any]) -> str:
    """Hash the evidence/research frontier, excluding mutable artifact paths and prose."""

    current = _prior_research_summary(record) or {}
    revision_raw = record.get("case_revision")
    try:
        case_revision = max(1, int(revision_raw or 1))
    except (TypeError, ValueError):
        case_revision = 1
    projection = {
        "case_id": _coerce_string(record.get("case_id")),
        "case_revision": case_revision,
        "source_evidence_atom_ids": sorted(
            {
                value.strip()
                for value in (
                    record.get("source_evidence_atom_ids")
                    if isinstance(record.get("source_evidence_atom_ids"), list)
                    else record.get("evidence_atom_ids")
                    if isinstance(record.get("evidence_atom_ids"), list)
                    else []
                )
                if isinstance(value, str) and value.strip()
            }
        ),
        "source_evidence_snapshot_complete": record.get(
            "source_evidence_snapshot_complete"
        )
        is True,
        "source_evidence_snapshot_sha256": _coerce_string(
            record.get("source_evidence_snapshot_sha256")
        ),
        "research": {
            key: current.get(key)
            for key in (
                "repo_revision",
                "research_schema_version",
                "research_status",
                "reproduction_status",
                "root_cause_status",
                "root_cause_confidence",
                "blocking_reasons",
                "material_unknown_summary",
                "verified_mechanism_sha256",
            )
        },
    }
    return sha256(
        json.dumps(
            projection,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _runner_research_route(record: Mapping[str, Any]) -> dict[str, Any]:
    """Choose whether this cycle should spend a Stage-3 research invocation.

    The route never terminates or deletes a case.  It separates durable case identity from
    immediate scheduling and gives every parked case a concrete reconsideration trigger.
    """

    frontier_sha256 = _research_frontier_sha256(record)
    current = _prior_research_summary(record)
    previous = _prior_priority_route_reference(record) or {}
    previous_route = _coerce_string(previous.get("research_route"))
    previous_revision = _coerce_string(previous.get("research_route_revision"))
    previous_frontier = _coerce_string(previous.get("research_frontier_sha256"))
    previous_snapshot_id = _coerce_string(previous.get("research_snapshot_id"))
    current_snapshot_id = (
        _coerce_string(current.get("stage_snapshot_id")) if current is not None else None
    )

    route: str
    reason: str
    reconsider_when: str | None = None
    if current is None:
        route = "research_new"
        reason = (
            "The canonical case has no retained research proof; current-cycle discovery and "
            "historical carry-forward both enter the same first-research path."
        )
    else:
        blocking_reasons = [
            value.casefold()
            for value in (
                current.get("blocking_reasons")
                if isinstance(current.get("blocking_reasons"), list)
                else []
            )
            if isinstance(value, str) and value.strip()
        ]
        external_wait = any(
            marker in value
            for value in blocking_reasons
            for marker in ("provider_external_wait", "usage_limit", "subscription_wait")
        )
        malformed_legacy = any(
            marker in value
            for value in blocking_reasons
            for marker in ("malformed", "schema_invalid", "contract_invalid")
        )
        reassessment_completed_without_frontier_change = bool(
            previous_revision == _RESEARCH_ROUTE_REVISION
            and previous_route == "reassess_actionability"
            and previous_frontier == frontier_sha256
            and previous_snapshot_id is not None
            and current_snapshot_id is not None
            and previous_snapshot_id != current_snapshot_id
        )
        stable_wait = bool(
            previous_revision == _RESEARCH_ROUTE_REVISION
            and previous_route == "await_evidence"
            and previous_frontier == frontier_sha256
            and previous_snapshot_id == current_snapshot_id
        )
        research_status = _coerce_string(current.get("research_status")) or "unknown"
        root_cause_status = _coerce_string(current.get("root_cause_status")) or "unknown"
        if external_wait:
            route = "resume_prior"
            reason = "A retained provider-wait frontier must resume rather than restart."
        elif research_status in {"blocked", "partial"} or root_cause_status == "blocked":
            if reassessment_completed_without_frontier_change or stable_wait:
                route = "await_evidence"
                reason = (
                    "A runner-versioned reassessment completed and left the exact same blocked "
                    "frontier; identity is retained without immediately repeating that mission."
                )
                reconsider_when = (
                    "A new source-evidence atom, explicit blocker-recheck receipt, or resumable "
                    "same-author checkpoint changes the recorded frontier."
                )
            else:
                route = "reassess_actionability"
                reason = (
                    "The retained blocked proof must receive a current runner-versioned "
                    "reassessment before it may wait."
                    if not malformed_legacy
                    else "A legacy malformed proof must receive a current runner-versioned "
                    "actionability reassessment under the self-healing research contract."
                )
        else:
            hydrated, hydration_errors = hydrate_retained_research_proof(record)
            if hydrated is not None and not hydration_errors:
                no_change, no_change_errors = hydrate_retained_no_change_disposition(
                    record,
                    research_dossier=hydrated,
                )
                if no_change is not None and not no_change_errors:
                    route = "await_evidence"
                    reason = (
                        "The exact current research proof and its content-bound zero-option "
                        "Stage-4 disposition agree that no product change is currently "
                        "required. The case remains nonterminal and waits without repeating "
                        "Stages 3-6."
                    )
                    reconsider_when = (
                        "A new source-evidence atom, changed case/source/research revision, "
                        "or authenticated live-verification evidence changes the recorded "
                        "frontier."
                    )
                elif no_change_errors:
                    route = "continue_downstream"
                    reason = (
                        "The current Stage-3 proof has a no-change actionability disposition, "
                        "but its retained Stage-4 disposition is absent, stale, or unverified "
                        "and will be rebuilt through the deterministic downstream path. First "
                        "result: " + no_change_errors[0] + "."
                    )
                else:
                    downstream_chain, downstream_errors = hydrate_retained_downstream_chain(
                        record,
                        research_dossier=hydrated,
                    )
                    if downstream_chain is not None and not downstream_errors:
                        route = "await_outcome"
                        reason = (
                            "The exact retained research, option, selection, and plan chain is "
                            "content-bound, currently ready, and unchanged; no Stage 3-6 model "
                            "work is needed until outcome or source evidence changes."
                        )
                    else:
                        route = "continue_downstream"
                        reason = (
                            "The complete retained Stage-3 dossier is currently ready, but the "
                            "full downstream chain is absent, stale, or unverified and will "
                            "self-heal through the normal downstream path. First chain result: "
                            + (
                                downstream_errors[0]
                                if downstream_errors
                                else "downstream_chain_unavailable"
                            )
                            + "."
                        )
            else:
                route = "research_update"
                reason = (
                    "The retained nonblocked summary could not hydrate a complete currently-ready "
                    "proof, so the case requires fresh revalidation rather than a checkpoint "
                    "resume. First hydration result: "
                    + (hydration_errors[0] if hydration_errors else "proof_unavailable")
                    + "."
                )

    return {
        "research_route": route,
        "research_route_revision": _RESEARCH_ROUTE_REVISION,
        "research_frontier_sha256": frontier_sha256,
        "research_snapshot_id": current_snapshot_id,
        "route_reason": reason,
        "reconsider_when": reconsider_when,
        "selected_for_research": route in _RESEARCH_DISPATCH_ROUTES,
        "eligible_for_downstream": route in _DOWNSTREAM_ELIGIBLE_ROUTES,
    }


def _research_dispatch_sort_key(decision: Mapping[str, Any]) -> tuple[Any, ...]:
    """Return deterministic Stage-3 order from route, urgency, score, and identity."""

    score_raw = decision.get("pre_score")
    score = (
        float(score_raw)
        if isinstance(score_raw, (int, float)) and not isinstance(score_raw, bool)
        else 0.0
    )
    return (
        _RESEARCH_ROUTE_ORDER.get(_coerce_string(decision.get("research_route")) or "", 99),
        _PRIORITY_BUCKET_ORDER.get(
            _coerce_string(decision.get("priority_bucket")) or "watch", 99
        ),
        -score,
        _coerce_string(decision.get("case_id"))
        or _coerce_string(decision.get("problem_id"))
        or "",
    )


def _priority_response_projection(
    response: str,
    *,
    problem_records: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    """Project one response into parsed decisions, exact errors, and valid keys.

    This validator is deliberately stage-specific but error-identity agnostic.  Every
    deterministic error is correction feedback; no allowlist decides whether the same
    author is permitted to repair it.  Individually valid decisions remain addressable
    even when another problem is missing or malformed.
    """

    expected_records = {
        str(record["problem_id"]): record
        for record in problem_records
        if isinstance(record, dict)
        and isinstance(record.get("problem_id"), str)
        and str(record["problem_id"]).strip()
    }
    try:
        parsed, parse_warnings = parse_priority_decision_list(response)
    except Exception as exc:  # noqa: BLE001 - exact parser feedback is repair input
        return [], [f"{type(exc).__name__}: {exc}"], []

    errors = [str(warning) for warning in parse_warnings if str(warning).strip()]
    candidates: dict[str, list[dict[str, Any]]] = {}
    for index, decision in enumerate(parsed):
        problem_id = _coerce_string(decision.get("problem_id"))
        if problem_id is None or problem_id not in expected_records:
            errors.append(
                "prioritizer_unknown_problem_id:"
                + (problem_id or f"(missing:index={index})")
            )
            continue
        candidates.setdefault(problem_id, []).append(decision)

    valid_keys: list[str] = []
    valid_buckets = {"p0", "p1", "p2", "p3", "watch"}
    for problem_id, record in expected_records.items():
        model_candidates = candidates.get(problem_id, [])
        if not model_candidates:
            errors.append(f"prioritizer_missing_problem_id:{problem_id}")
            continue
        if len(model_candidates) > 1:
            errors.append(f"prioritizer_duplicate_problem_id:{problem_id}")
            continue

        decision = model_candidates[0]
        item_errors: list[str] = []
        if _coerce_string(decision.get("_parse_warning")) is not None:
            item_errors.append(f"prioritizer_invalid_problem_decision:{problem_id}")
        if decision.get("priority_bucket") not in valid_buckets:
            item_errors.append(f"prioritizer_invalid_priority_bucket:{problem_id}")
        if _coerce_string(decision.get("priority_rationale")) is None:
            item_errors.append(f"prioritizer_missing_priority_rationale:{problem_id}")
        if decision.get("priority_status") != "prioritized":
            item_errors.append(f"prioritizer_invalid_priority_status:{problem_id}")

        forbidden = sorted(_PRIORITY_FORBIDDEN_SOLUTION_FIELDS & set(decision))
        for field in forbidden:
            item_errors.append(
                f"priority_decision_forbidden_solution_field:{problem_id}:{field}"
            )

        record_evidence = {
            value
            for value in (
                record.get("evidence_atom_ids")
                if isinstance(record.get("evidence_atom_ids"), list)
                else []
            )
            if isinstance(value, str) and value.strip()
        }
        cited_raw = decision.get("evidence_atom_ids_used")
        cited = (
            [value for value in cited_raw if isinstance(value, str) and value.strip()]
            if isinstance(cited_raw, list)
            else []
        )
        if not cited:
            item_errors.append(f"prioritizer_missing_evidence_atom_ids_used:{problem_id}")
        for atom_id in sorted(set(cited) - record_evidence):
            item_errors.append(
                f"prioritizer_evidence_atom_outside_problem:{problem_id}:{atom_id}"
            )

        errors.extend(item_errors)
        if not item_errors:
            valid_keys.append("priority_decision:" + problem_id)

    return parsed, list(dict.fromkeys(errors)), sorted(valid_keys)


def _priority_correction_progress(assessment: Any) -> dict[str, Any] | None:
    if assessment is None:
        return None
    return {
        "decision": assessment.decision,
        "reason": assessment.reason,
        "resolved_error_identities": list(assessment.resolved_error_identities),
        "introduced_error_identities": list(assessment.introduced_error_identities),
        "repeated_state": assessment.repeated_state,
        "safe_frontier_updated": assessment.safe_frontier_updated,
        "global_best_updated": assessment.global_best_updated,
    }


def _priority_correction_prompt(
    *,
    original_prompt: str,
    prior_response: str,
    validation_errors: tuple[str, ...],
    valid_item_keys: tuple[str, ...],
    prior_assessment: Any,
) -> str:
    progress = _priority_correction_progress(prior_assessment)
    return (
        "SAME-AUTHOR PRIORITIZATION RESPONSE CORRECTION\n\n"
        "Revise your immediately prior complete response in this exact author session and "
        "workspace. Do not restart prioritization. Preserve every valid keyed decision unless "
        "a correlated correction requires changing it. Unknown validator errors are valid "
        "feedback. Return the complete corrected JSON list, not a patch and no prose.\n\n"
        "Original assignment prompt SHA-256: "
        f"{sha256(original_prompt.encode('utf-8')).hexdigest()}\n"
        "Immediately prior response SHA-256: "
        f"{sha256(prior_response.encode('utf-8')).hexdigest()}\n\n"
        "Deterministic parse and quality errors:\n"
        + "\n".join(f"- {error}" for error in validation_errors)
        + "\n\nValid keyed decisions to preserve:\n"
        + ("\n".join(f"- {key}" for key in valid_item_keys) or "- none verified yet")
        + "\n\nPrior correction progress:\n"
        + json.dumps(progress, ensure_ascii=False, indent=2, sort_keys=True)
        + "\n"
    )


def _priority_attempt_history(
    correction: Any,
    *,
    base_tag: str,
    attempt_number_offset: int = 0,
) -> list[dict[str, Any]]:
    history = [dict(attempt.payload["attempt_record"]) for attempt in correction.attempts]
    for index, assessment in enumerate(correction.assessments, start=1):
        history[index]["correction_progress"] = {
            "decision": assessment.decision,
            "reason": assessment.reason,
            "before_error_count": assessment.before_error_count,
            "after_error_count": assessment.after_error_count,
            "resolved_error_identities": list(assessment.resolved_error_identities),
            "introduced_error_identities": list(assessment.introduced_error_identities),
            "repeated_state": assessment.repeated_state,
            "safe_frontier_updated": assessment.safe_frontier_updated,
            "global_best_updated": assessment.global_best_updated,
            "reset_progress_clock": assessment.reset_progress_clock,
        }
    history.extend(
        {
            "schema_version": 2,
            "attempt_number": failure.attempt_number + attempt_number_offset,
            "attempt_tag": f"{base_tag}_correction_{failure.attempt_number - 1:03d}",
            "status": "invocation_failed",
            "agent_session_id": failure.agent_session_id,
            "resumed_from_session_id": failure.agent_session_id,
            "elapsed_seconds": failure.cost_seconds,
            "validation_errors": [f"{failure.error_type}: {failure.error_message}"],
            "failure_identity": failure.failure_identity,
        }
        for failure in correction.invocation_failures
    )
    return sorted(history, key=lambda record: int(record["attempt_number"]))


def _enforce_research_routing_policy(decisions: list[dict[str, Any]]) -> None:
    """Apply runner-owned per-cycle routes without terminating or deleting cases."""

    for decision in decisions:
        if isinstance(decision.get("problem_id"), str) and not decision.get("_parse_warning"):
            route = _coerce_string(decision.get("research_route")) or "research_new"
            decision["research_route"] = route
            decision["selected_for_research"] = route in _RESEARCH_DISPATCH_ROUTES
            decision["eligible_for_downstream"] = route in _DOWNSTREAM_ELIGIBLE_ROUTES
            decision["priority_status"] = "prioritized"


def _enforce_full_drain_research_policy(decisions: list[dict[str, Any]]) -> None:
    """Compatibility wrapper for callers; routing now replaces unconditional full drain."""

    _enforce_research_routing_policy(decisions)


def _priority_string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return list(
        dict.fromkeys(
            item.strip() for item in value if isinstance(item, str) and item.strip()
        )
    )


def _apply_provisional_research_unit_schedule(
    *,
    decisions: list[dict[str, Any]],
    problem_records: list[dict[str, Any]],
) -> list[str]:
    """Dispatch one evidence-complete provisional group as one research unit.

    Provisional grouping does not merge durable cases.  It only prevents Stage 3 from
    researching the same hypothesized cause twice when relation review has nominated one
    evidence-complete research unit.  Fail open to independent scheduling whenever the
    packet is inconsistent or a non-unit member carries retained research state that
    cannot safely be transferred to the nominated author assignment.
    """

    records_by_problem_id = {
        str(record["problem_id"]): record
        for record in problem_records
        if isinstance(record, dict)
        and isinstance(record.get("problem_id"), str)
        and str(record["problem_id"]).strip()
    }
    decisions_by_problem_id: dict[str, list[dict[str, Any]]] = {}
    for decision in decisions:
        problem_id = _coerce_string(decision.get("problem_id"))
        if problem_id is not None:
            decisions_by_problem_id.setdefault(problem_id, []).append(decision)

    grouped_records: dict[str, list[dict[str, Any]]] = {}
    warnings: list[str] = []
    for record in problem_records:
        if record.get("case_identity_status") != "provisional_same_cause":
            continue
        group_raw = record.get("provisional_same_cause_group")
        group = group_raw if isinstance(group_raw, Mapping) else {}
        group_id = _coerce_string(group.get("group_id"))
        if group_id is None:
            problem_id = _coerce_string(record.get("problem_id")) or "(missing)"
            warnings.append(
                f"provisional_research_schedule_group_id_missing:{problem_id}"
            )
            continue
        grouped_records.setdefault(group_id, []).append(record)

    for group_id, observed_records in sorted(grouped_records.items()):
        exemplar_raw = observed_records[0].get("provisional_same_cause_group")
        exemplar = exemplar_raw if isinstance(exemplar_raw, Mapping) else {}
        member_problem_ids = _priority_string_list(exemplar.get("member_problem_ids"))
        member_case_ids = _priority_string_list(exemplar.get("member_case_ids"))
        research_unit_case_id = _coerce_string(exemplar.get("research_unit_case_id"))
        group_errors: list[str] = []
        facet_source_atom_ids: list[str] = []

        for record in observed_records:
            record_problem_id = _coerce_string(record.get("problem_id")) or "(missing)"
            record_case_id = _coerce_string(record.get("case_id"))
            group_raw = record.get("provisional_same_cause_group")
            group = group_raw if isinstance(group_raw, Mapping) else {}
            group_errors.extend(
                f"{record_problem_id}:{error}"
                for error in provisional_same_cause_group_errors(
                    group_raw,
                    owning_case_id=record_case_id,
                )
            )
            if _coerce_string(group.get("group_id")) != group_id:
                group_errors.append(f"{record_problem_id}:group_id_mismatch")
            if set(_priority_string_list(group.get("member_problem_ids"))) != set(
                member_problem_ids
            ):
                group_errors.append(f"{record_problem_id}:member_problem_ids_mismatch")
            if set(_priority_string_list(group.get("member_case_ids"))) != set(
                member_case_ids
            ):
                group_errors.append(f"{record_problem_id}:member_case_ids_mismatch")
            if _coerce_string(group.get("research_unit_case_id")) != research_unit_case_id:
                group_errors.append(f"{record_problem_id}:research_unit_case_id_mismatch")
            for facet in (
                group.get("member_facets")
                if isinstance(group.get("member_facets"), list)
                else []
            ):
                if isinstance(facet, Mapping):
                    facet_source_atom_ids.extend(
                        _priority_string_list(facet.get("source_evidence_atom_ids"))
                    )

        member_records = [
            records_by_problem_id[problem_id]
            for problem_id in member_problem_ids
            if problem_id in records_by_problem_id
        ]
        if len(member_records) != len(member_problem_ids):
            group_errors.append("member_problem_record_missing")
        if {
            _coerce_string(record.get("case_id")) for record in member_records
        } != set(member_case_ids):
            group_errors.append("member_case_record_mismatch")
        member_decisions = [
            decisions_by_problem_id[problem_id][0]
            for problem_id in member_problem_ids
            if len(decisions_by_problem_id.get(problem_id, [])) == 1
        ]
        if len(member_decisions) != len(member_problem_ids):
            group_errors.append("member_priority_decision_missing_or_duplicated")
        unit_records = [
            record
            for record in member_records
            if _coerce_string(record.get("case_id")) == research_unit_case_id
        ]
        if research_unit_case_id is None or len(unit_records) != 1:
            group_errors.append("research_unit_record_invalid")

        source_atom_ids = list(dict.fromkeys(facet_source_atom_ids))
        unit_record = unit_records[0] if len(unit_records) == 1 else None
        unit_problem_id = (
            _coerce_string(unit_record.get("problem_id"))
            if unit_record is not None
            else None
        )
        unit_evidence = {
            *_priority_string_list(
                unit_record.get("source_evidence_atom_ids")
                if unit_record is not None
                else []
            ),
            *_priority_string_list(
                unit_record.get("evidence_atom_ids") if unit_record is not None else []
            ),
        }
        if not source_atom_ids or not set(source_atom_ids).issubset(unit_evidence):
            group_errors.append("research_unit_source_evidence_incomplete")

        unit_decision = (
            decisions_by_problem_id.get(unit_problem_id or "", [None])[0]
            if len(decisions_by_problem_id.get(unit_problem_id or "", [])) == 1
            else None
        )
        if not isinstance(unit_decision, dict) or unit_decision.get(
            "selected_for_research"
        ) is not True:
            group_errors.append("research_unit_not_selected")

        for member_decision in member_decisions:
            if member_decision is unit_decision:
                continue
            if member_decision.get("selected_for_research") is not True:
                continue
            member_route = _coerce_string(member_decision.get("research_route"))
            if member_route != "research_new":
                group_errors.append(
                    "nonunit_retained_research_state_requires_independent_dispatch:"
                    + (_coerce_string(member_decision.get("problem_id")) or "(missing)")
                    + ":"
                    + (member_route or "(missing)")
                )

        if group_errors:
            warnings.extend(
                f"provisional_research_schedule_not_collapsed:{group_id}:{error}"
                for error in dict.fromkeys(group_errors)
            )
            continue

        assert isinstance(unit_decision, dict)
        assert unit_problem_id is not None
        schedule_base = {
            "schema_version": 1,
            "group_id": group_id,
            "research_unit_case_id": research_unit_case_id,
            "research_unit_problem_id": unit_problem_id,
            "member_case_ids": member_case_ids,
            "member_problem_ids": member_problem_ids,
            "source_evidence_atom_ids": source_atom_ids,
        }
        for member_decision in member_decisions:
            member_problem_id = _coerce_string(member_decision.get("problem_id"))
            member_decision.setdefault(
                "individual_research_route", member_decision.get("research_route")
            )
            member_decision.setdefault(
                "individual_selected_for_research",
                member_decision.get("selected_for_research") is True,
            )
            if member_decision is unit_decision:
                member_decision["provisional_research_schedule"] = {
                    **schedule_base,
                    "status": "research_unit",
                }
                continue
            member_decision.update(
                {
                    "research_route": _PROVISIONAL_RESEARCH_UNIT_WAIT_ROUTE,
                    "selected_for_research": False,
                    "eligible_for_downstream": False,
                    "route_reason": (
                        "This provisional same-cause member is represented by the "
                        f"evidence-complete research unit {unit_problem_id}; its durable "
                        "identity and facet evidence remain attached to that assignment."
                    ),
                    "reconsider_when": (
                        "The provisional relation is split or cleared, the nominated research "
                        "unit changes, or new member evidence changes the group frontier."
                    ),
                    "provisional_research_schedule": {
                        **schedule_base,
                        "status": "represented_by_research_unit",
                        "represented_problem_id": member_problem_id,
                    },
                }
            )

    return list(dict.fromkeys(warnings))


def _server_normalize_priority_decisions(
    *,
    decisions: list[dict[str, Any]],
    problem_records: list[dict[str, Any]],
    signals_by_problem_id: dict[str, dict[str, Any]],
    research_routes_by_problem_id: Mapping[str, Mapping[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Return one research-eligible decision for every canonical problem.

    The model still supplies urgency and rationale when its response is valid. Missing,
    duplicate, malformed, or explicitly blocked responses fall back to deterministic
    runner signals instead of silently removing the case from stage 3.
    """

    valid_buckets = {"p0", "p1", "p2", "p3", "watch"}
    expected_records = {
        str(record["problem_id"]): record
        for record in problem_records
        if isinstance(record, dict)
        and isinstance(record.get("problem_id"), str)
        and str(record["problem_id"]).strip()
    }
    candidates: dict[str, list[dict[str, Any]]] = {}
    warnings: list[str] = []
    for decision in decisions:
        problem_id = _coerce_string(decision.get("problem_id"))
        if problem_id is None or problem_id not in expected_records:
            warnings.append("prioritizer_unknown_problem_id:" + (problem_id or "(missing)"))
            continue
        candidates.setdefault(problem_id, []).append(dict(decision))

    normalized: list[dict[str, Any]] = []
    for problem_id, record in expected_records.items():
        model_candidates = candidates.get(problem_id, [])
        use_model = (
            len(model_candidates) == 1
            and _coerce_string(model_candidates[0].get("_parse_warning")) is None
        )
        if len(model_candidates) > 1:
            warnings.append(f"prioritizer_duplicate_problem_id:{problem_id}")
        elif not model_candidates:
            warnings.append(f"prioritizer_missing_problem_id:{problem_id}")
        elif not use_model:
            warnings.append(f"prioritizer_invalid_problem_decision:{problem_id}")

        candidate = dict(model_candidates[0]) if use_model else {}
        signals = signals_by_problem_id.get(problem_id, {})
        bucket = _coerce_string(candidate.get("priority_bucket"))
        if bucket not in valid_buckets:
            bucket = _coerce_string(signals.get("bucket_candidate"))
        if bucket not in valid_buckets:
            bucket = "watch"
        rationale = _coerce_string(candidate.get("priority_rationale"))
        if rationale is None:
            rationale = (
                "Runner fallback retained this canonical case for causal research; "
                "deterministic priority signals control ordering only."
            )
        evidence_ids = candidate.get("evidence_atom_ids_used")
        record_evidence = {
            value
            for value in (
                record.get("evidence_atom_ids")
                if isinstance(record.get("evidence_atom_ids"), list)
                else []
            )
            if isinstance(value, str) and value.strip()
        }
        cited = [
            value
            for value in (evidence_ids if isinstance(evidence_ids, list) else [])
            if isinstance(value, str) and value in record_evidence
        ]
        if not cited:
            cited = sorted(record_evidence)
        candidate.pop("_parse_warning", None)
        route = dict((research_routes_by_problem_id or {}).get(problem_id, {}))
        if not route:
            route = _runner_research_route(record)
        candidate.update(
            {
                "problem_id": problem_id,
                "priority_bucket": bucket,
                "selected_for_research": route["selected_for_research"],
                "priority_rationale": rationale,
                "evidence_atom_ids_used": cited,
                "priority_status": "prioritized",
                "selection_authority": _RESEARCH_ROUTE_REVISION,
                "model_priority_accepted": use_model,
                **route,
            }
        )
        normalized.append(candidate)
    return normalized, warnings


def _render_prioritized_problems_markdown(
    priority_decisions: list[dict[str, Any]],
    *,
    problem_records_by_id: dict[str, dict[str, Any]],
    title: str = "Prioritized Problems",
) -> str:
    """Render stage-2 prioritization decisions as a human-readable Markdown document."""
    lines: list[str] = [f"# {title}\n"]
    if not priority_decisions:
        lines.append("_No prioritization decisions produced._\n")
        return "\n".join(lines)

    for dec in priority_decisions:
        pid = dec.get("problem_id") or "(no id)"
        rec = problem_records_by_id.get(pid) or {}
        rec_title = rec.get("title") or pid
        bucket = dec.get("priority_bucket") or "watch"
        selected = dec.get("selected_for_research")
        selected_str = "true" if selected is True else "false" if selected is False else "?"
        pre_score = dec.get("pre_score")
        pre_str = f"{float(pre_score):.2f}" if isinstance(pre_score, (int, float)) else "?"
        lines.append(f"## {rec_title}")
        lines.append(
            f"**ID**: `{pid}` | **Bucket**: {bucket} | "
            f"**Selected for research**: {selected_str} | **Pre-score**: {pre_str}\n"
        )
        rationale = dec.get("priority_rationale") or ""
        if rationale:
            lines.append(f"**Rationale**: {rationale}\n")
        used = dec.get("evidence_atom_ids_used") or []
        if isinstance(used, list) and used:
            used_list = [e for e in used if isinstance(e, str) and e.strip()]
            if used_list:
                lines.append(
                    f"**Evidence atoms used** ({len(used_list)}): "
                    + ", ".join(f"`{e}`" for e in used_list[:10])
                    + (" …" if len(used_list) > 10 else "")
                    + "\n"
                )
        warn = dec.get("_parse_warning")
        if warn:
            lines.append(f"> ⚠ parse warning: {warn}\n")
        lines.append("")

    return "\n".join(lines)


def _run_problem_prioritization_stage(
    *,
    atoms: list[dict[str, Any]],
    problem_records: list[dict[str, Any]],
    pipeline_manifest: PipelinePromptManifest,
    artifacts_dir: Path,
    out_json: Path,
    out_md: Path,
    agent: str,
    model: str | None,
    cfg: RunnerConfig,
    dry_run: bool,
    stage_guidance_text: str,
    external_correction: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Run stage 2 prioritization and write the stage artifacts."""
    import json as _json

    stage = "problem_prioritization"
    stage_artifacts_dir = artifacts_dir / "problem_prioritization"
    stage_artifacts_dir.mkdir(parents=True, exist_ok=True)
    invocation_tracker = ModelInvocationTracker(stage_artifacts_dir)

    relation_config_raw = yaml.safe_load(
        pipeline_manifest.relation_review_config_path.read_text(encoding="utf-8")
    )
    relation_config = relation_config_raw if isinstance(relation_config_raw, dict) else {}

    neighborhoods = rank_stage_related_items(
        problem_records,
        stage=stage,
        relation_config=relation_config,
        embedder=None,
    )
    priority_signals = compute_problem_priority_signals(problem_records, atoms)
    research_routes_by_problem_id = {
        str(record["problem_id"]): _runner_research_route(record)
        for record in problem_records
        if isinstance(record, Mapping)
        and isinstance(record.get("problem_id"), str)
        and str(record["problem_id"]).strip()
    }
    for item in priority_signals:
        problem_id = _coerce_string(item.get("problem_id"))
        route = research_routes_by_problem_id.get(problem_id or "")
        if route is not None:
            item.update(route)
    signals_by_problem_id: dict[str, dict[str, Any]] = {
        str(item.get("problem_id")): item
        for item in priority_signals
        if isinstance(item, dict) and isinstance(item.get("problem_id"), str)
    }

    problem_records_json = _json.dumps(problem_records, ensure_ascii=False, indent=2)
    signals_json = _json.dumps(priority_signals, ensure_ascii=False, indent=2)
    neighborhoods_json = _json.dumps(neighborhoods, ensure_ascii=False, indent=2)

    template_text = pipeline_manifest.template_text(pipeline_manifest.prioritizer_template)
    prompt = (
        template_text.replace("{{STAGE_GUIDANCE}}", stage_guidance_text)
        .replace("{{PROBLEM_RECORDS_JSON}}", problem_records_json)
        .replace("{{PRIORITY_SIGNALS_JSON}}", signals_json)
        .replace("{{NEIGHBORHOODS_JSON}}", neighborhoods_json)
    )

    tag = "problem_prioritization_001"
    run_out_dir = stage_artifacts_dir / tag
    run_out_dir.mkdir(parents=True, exist_ok=True)

    decisions: list[dict[str, Any]] = []
    warnings_list: list[str] = []
    status: str = "ok"
    error: str | None = None
    correction_status: str | None = None
    correction_metrics: dict[str, Any] | None = None
    correction_attempt_history: list[dict[str, Any]] = []
    correction_cost_since_progress = 0.0
    total_correction_cost = 0.0

    if not problem_records:
        status = "completed_no_input"
        correction_status = "not_required"
        correction_metrics = {
            "status": "not_required",
            "reason": "no_problem_records",
            "not_applicable": True,
            "attempt_count": 0,
            "correction_turn_count": 0,
            "correction_invocation_failure_count": 0,
            "correction_invocation_failure_cost_seconds": 0.0,
            "accepted": None,
            "accepted_good": None,
            "accepted_bad": None,
            "false_rejected": None,
            "repaired": False,
            "stalled": False,
            "repairable_paused": False,
            "initial_cost_seconds": 0.0,
            "total_correction_cost_seconds": 0.0,
            "total_elapsed_seconds": 0.0,
            "best_error_count": 0,
            "best_valid_item_count": 0,
        }
        (run_out_dir / f"{tag}.prompt.txt").write_text(prompt, encoding="utf-8")
        (run_out_dir / f"{tag}.response.txt").write_text(
            "[skipped] stage-2 prioritizer received no problem records; "
            "no model invocation was required.\n",
            encoding="utf-8",
        )
    elif dry_run:
        status = "dry_run_heuristic"
        (run_out_dir / f"{tag}.prompt.txt").write_text(prompt, encoding="utf-8")
        (run_out_dir / f"{tag}.response.txt").write_text(
            "[dry-run] stage-2 prioritizer prompt not executed (offline mode).\n",
            encoding="utf-8",
        )
        for rec in problem_records:
            pid = rec.get("problem_id")
            if not isinstance(pid, str) or not pid.strip():
                continue
            signals = signals_by_problem_id.get(pid, {})
            bucket = signals.get("bucket_candidate") if isinstance(signals, dict) else None
            bucket_s = bucket if isinstance(bucket, str) else "watch"
            route = research_routes_by_problem_id.get(pid) or _runner_research_route(rec)
            selected = route["selected_for_research"]
            pre_score = signals.get("pre_score") if isinstance(signals, dict) else None
            score_breakdown = signals.get("score_breakdown") if isinstance(signals, dict) else None
            cited = (
                rec.get("evidence_atom_ids")
                if isinstance(rec.get("evidence_atom_ids"), list)
                else []
            )
            cited_ids = [e for e in cited if isinstance(e, str) and e.strip()]
            decisions.append(
                {
                    "problem_id": pid,
                    "priority_bucket": bucket_s,
                    "selected_for_research": selected,
                    "priority_rationale": (
                        "Dry-run heuristic (offline): selected bucket from deterministic pre-score "
                        f"(pre_score={pre_score!r})."
                    ),
                    "evidence_atom_ids_used": cited_ids,
                    "priority_status": "prioritized",
                    "pre_score": pre_score,
                    "bucket_candidate": bucket_s,
                    "score_breakdown": score_breakdown,
                    **route,
                    "_dry_run_synthesized": True,
                }
            )
    else:
        import time as _time

        workspace_dir = run_out_dir / f"{tag}.workspace"
        workspace_dir.mkdir(parents=True, exist_ok=True)
        prompt_sha256 = sha256(prompt.encode("utf-8")).hexdigest()
        expected_problem_ids = sorted(
            str(record["problem_id"])
            for record in problem_records
            if isinstance(record, dict)
            and isinstance(record.get("problem_id"), str)
            and str(record["problem_id"]).strip()
        )

        def run_attempt(
            *,
            attempt_prompt: str,
            attempt_tag: str,
            attempt_number: int,
            resume_session_id: str | None,
        ) -> CorrectionObservation[dict[str, Any]]:
            started = _time.monotonic()
            response = ""
            session_id: str | None = None
            observed_workspace = workspace_dir.resolve()
            resumed_from = resume_session_id
            prompt_path = run_out_dir / f"{attempt_tag}.prompt.txt"
            response_path = run_out_dir / f"{attempt_tag}.response.txt"
            invocation_path = run_out_dir / f"{attempt_tag}.model_invocation.json"
            transport_error: str | None = None
            try:
                run = run_stage_prompt_json(
                    stage=stage,
                    prompt=attempt_prompt,
                    out_dir=run_out_dir,
                    tag=attempt_tag,
                    agent=agent,
                    model=model,
                    cfg=cfg,
                    workspace_dir=workspace_dir,
                    resume_session_id=resume_session_id,
                    allow_empty=True,
                    structured=True,
                )
                if isinstance(run, str):
                    response = run
                    elapsed_seconds = max(0.0, _time.monotonic() - started)
                else:
                    response = str(run.response)
                    session_id = _coerce_string(run.agent_session_id)
                    elapsed_seconds = max(0.0, float(run.elapsed_seconds))
                    if run.workspace_dir is not None:
                        observed_workspace = Path(run.workspace_dir).resolve()
                    resumed_from = _coerce_string(run.resumed_from_session_id)
                    prompt_path = Path(run.prompt_path)
                    response_path = Path(run.response_path)
                    invocation_path = Path(run.invocation_manifest_path)
            except Exception as exc:  # noqa: BLE001 - preserve fallback frontier
                if resume_session_id is not None:
                    raise
                elapsed_seconds = max(0.0, _time.monotonic() - started)
                transport_error = f"{type(exc).__name__}: {exc}"

            parsed, validation_errors, valid_item_keys = _priority_response_projection(
                response,
                problem_records=problem_records,
            )
            if transport_error is not None:
                validation_errors.insert(0, transport_error)
            validation_errors = list(dict.fromkeys(validation_errors))
            continuity_key = sha256(
                json.dumps(
                    {
                        "workspace_dir": str(observed_workspace),
                        "original_prompt_sha256": prompt_sha256,
                        "problem_ids": expected_problem_ids,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                ).encode("utf-8")
            ).hexdigest()
            attempt_record = {
                "schema_version": 2,
                "attempt_number": attempt_number,
                "attempt_tag": attempt_tag,
                "status": "verified" if not validation_errors else "invalid",
                "agent_session_id": session_id,
                "resumed_from_session_id": resumed_from,
                "workspace_dir": str(observed_workspace),
                "elapsed_seconds": elapsed_seconds,
                "prompt_sha256": sha256(attempt_prompt.encode("utf-8")).hexdigest(),
                "response_sha256": sha256(response.encode("utf-8")).hexdigest(),
                "validation_errors": validation_errors,
                "valid_item_keys": valid_item_keys,
                "artifacts": {
                    "prompt": str(prompt_path.resolve()),
                    "response": str(response_path.resolve()),
                    "model_invocation": str(invocation_path.resolve()),
                },
            }
            payload = {
                "response": response,
                "decisions": parsed,
                "attempt_record": attempt_record,
            }
            return CorrectionObservation(
                payload=payload,
                validation_errors=tuple(validation_errors),
                state_sha256=correction_state_sha256(
                    candidate=response,
                    validation_errors=validation_errors,
                    valid_item_keys=valid_item_keys,
                ),
                valid_item_keys=tuple(valid_item_keys),
                agent_session_id=session_id,
                continuity_key=continuity_key,
                cost_seconds=elapsed_seconds,
            )

        external_session_id = (
            _coerce_string(external_correction.get("agent_session_id"))
            if isinstance(external_correction, Mapping)
            else None
        )
        external_feedback = (
            external_correction.get("feedback")
            if isinstance(external_correction, Mapping)
            and isinstance(external_correction.get("feedback"), Mapping)
            else None
        )
        current_decisions = (
            external_correction.get("current_payload")
            if isinstance(external_correction, Mapping)
            else None
        )
        initial_prompt = prompt
        if external_session_id is not None and external_feedback is not None:
            initial_prompt = (
                "SAME-AUTHOR PRIORITIZATION INDEPENDENT-REVIEW CORRECTION\n\n"
                "Continue your exact prior prioritization session and workspace. Revise "
                "the complete prior assignment in response to the independent findings. "
                "Preserve unrelated valid decisions. Return the complete corrected JSON "
                "list, not a patch and no prose.\n\n"
                "Independent feedback:\n"
                + _json.dumps(external_feedback, ensure_ascii=False, indent=2)
                + "\n\nRetained current decisions:\n"
                + _json.dumps(current_decisions, ensure_ascii=False, indent=2)
                + "\n\nOriginal assignment:\n"
                + prompt
            )
        initial = run_attempt(
            attempt_prompt=initial_prompt,
            attempt_tag=tag,
            attempt_number=1,
            resume_session_id=external_session_id,
        )
        acquisition_attempts: tuple[CorrectionObservation[dict[str, Any]], ...] = ()
        acquisition_status = "not_required"
        if agent.strip().lower() == "codex" and initial.agent_session_id is None:
            acquisition = acquire_author_session(
                initial=initial,
                invoke_fresh=lambda attempt_number: run_attempt(
                    attempt_prompt=prompt,
                    attempt_tag=f"{tag}_session_acquisition_{attempt_number - 1:03d}",
                    attempt_number=attempt_number,
                    resume_session_id=None,
                ),
            )
            acquisition_status = acquisition.status
            acquisition_attempts = acquisition.attempts
            initial = acquisition.current

        def invoke_correction(
            current: CorrectionObservation[dict[str, Any]],
            attempt_number: int,
            prior_assessment: Any,
        ) -> CorrectionObservation[dict[str, Any]]:
            correction_prompt = _priority_correction_prompt(
                original_prompt=prompt,
                prior_response=str(current.payload.get("response") or ""),
                validation_errors=current.validation_errors,
                valid_item_keys=current.valid_item_keys,
                prior_assessment=prior_assessment,
            )
            return run_attempt(
                attempt_prompt=correction_prompt,
                attempt_tag=f"{tag}_correction_{attempt_number - 1:03d}",
                attempt_number=attempt_number + max(0, len(acquisition_attempts) - 1),
                resume_session_id=initial.agent_session_id,
            )

        if acquisition_status.startswith("repairable_paused:"):
            correction = CorrectionRunResult(
                status=acquisition_status,
                current=initial,
                best=initial,
                attempts=acquisition_attempts,
                assessments=(),
                correction_cost_since_progress=acquisition.cost_since_progress,
                total_correction_cost=max(
                    0.0,
                    acquisition.total_cost
                    - max(0.0, float(acquisition_attempts[0].cost_seconds)),
                ),
            )
        else:
            correction = run_progressive_correction(
                initial=initial,
                invoke_correction=invoke_correction,
            )
        retained = (
            correction.current
            if correction.status in {"accepted", "corrected"}
            else correction.best
        )
        decisions = [
            dict(item)
            for item in retained.payload.get("decisions", [])
            if isinstance(item, dict)
        ]
        retained_valid_problem_ids = {
            key.removeprefix("priority_decision:")
            for key in retained.valid_item_keys
            if key.startswith("priority_decision:")
        }
        for decision in decisions:
            problem_id = _coerce_string(decision.get("problem_id"))
            if problem_id is None or problem_id in retained_valid_problem_ids:
                continue
            existing_warning = _coerce_string(decision.get("_parse_warning"))
            quality_warning = "priority_response_quality_invalid_after_correction"
            decision["_parse_warning"] = (
                f"{existing_warning}; {quality_warning}"
                if existing_warning is not None
                else quality_warning
            )
        correction_status = correction.status
        correction_metrics = correction_run_metrics(correction, expected_quality=None)
        if acquisition_attempts:
            correction_metrics = correction_metrics_with_session_acquisition(
                correction_metrics,
                acquisition,
            )
        correction_attempt_history = _priority_attempt_history(
            correction,
            base_tag=tag,
            attempt_number_offset=max(0, len(acquisition_attempts) - 1),
        )
        if acquisition_status == "acquired" and len(acquisition_attempts) > 1:
            pre_author_attempts = acquisition_attempts[:-1]
            correction_attempt_history = [
                dict(attempt.payload["attempt_record"])
                for attempt in pre_author_attempts
            ] + correction_attempt_history
        correction_cost_since_progress = correction.correction_cost_since_progress
        total_correction_cost = correction.total_correction_cost
        if correction.status == "corrected":
            status = "corrected"
        elif correction.status != "accepted":
            status = "nonblocking_fallback"
            error = correction.operational_error or correction.status
            warnings_list.append(
                "prioritizer_correction_incomplete:" + correction.status
            )
            warnings_list.extend(retained.validation_errors)

    decisions, normalization_warnings = _server_normalize_priority_decisions(
        decisions=decisions,
        problem_records=problem_records,
        signals_by_problem_id=signals_by_problem_id,
        research_routes_by_problem_id=research_routes_by_problem_id,
    )
    warnings_list.extend(normalization_warnings)

    # Enrich with deterministic signals so the artifact always shows the pre-score breakdown.
    for dec in decisions:
        pid = dec.get("problem_id")
        if not isinstance(pid, str):
            continue
        signals = signals_by_problem_id.get(pid)
        if isinstance(signals, dict):
            if "pre_score" not in dec:
                dec["pre_score"] = signals.get("pre_score")
            if "bucket_candidate" not in dec:
                dec["bucket_candidate"] = signals.get("bucket_candidate")
            if "score_breakdown" not in dec:
                dec["score_breakdown"] = signals.get("score_breakdown")

    # Persistent case identity is not the same thing as an instruction to spend another
    # research mission this cycle. Runner-owned routes retain every case and its explicit
    # retry trigger while selecting only new, updated, resumable, or one-time reassessment work.
    _enforce_research_routing_policy(decisions)
    warnings_list.extend(
        _apply_provisional_research_unit_schedule(
            decisions=decisions,
            problem_records=problem_records,
        )
    )
    decisions.sort(key=_research_dispatch_sort_key)

    # Guardrail: stage 2 must not contain solution fields.
    for dec in decisions:
        pid = dec.get("problem_id") or "(no problem_id)"
        bad = [k for k in _PRIORITY_FORBIDDEN_SOLUTION_FIELDS if k in dec]
        if bad:
            warnings_list.append(
                f"priority_decision_forbidden_solution_fields: {pid}: {', '.join(sorted(bad))}"
            )
            existing = dec.get("_parse_warning")
            msg = "forbidden fields present: " + ", ".join(sorted(bad))
            if isinstance(existing, str) and existing.strip():
                dec["_parse_warning"] = existing.strip() + "; " + msg
            else:
                dec["_parse_warning"] = msg

    stage_doc = build_stage_document(
        stage,
        decisions,
        input_meta={
            "atom_count": len(atoms),
            "problem_record_count": len(problem_records),
            "dry_run": dry_run,
            "prioritizer_status": status,
            "prioritizer_error": error,
            "prioritizer_warnings": warnings_list,
            "prioritizer_correction_status": correction_status,
            "prioritizer_correction_metrics": correction_metrics,
            "prioritizer_attempt_history": correction_attempt_history,
            "prioritizer_correction_cost_since_progress": correction_cost_since_progress,
            "prioritizer_total_correction_cost": total_correction_cost,
            **(
                {
                    "stage_status": "completed",
                    "model_invocation_skipped": "no_problem_records",
                }
                if not problem_records
                else {}
            ),
            "prioritizer_fallback_decision_count": sum(
                1 for decision in decisions if decision.get("model_priority_accepted") is not True
            ),
            "provisional_research_unit_count": sum(
                1
                for decision in decisions
                if isinstance(decision.get("provisional_research_schedule"), Mapping)
                and decision["provisional_research_schedule"].get("status")
                == "research_unit"
            ),
            "provisional_research_member_wait_count": sum(
                1
                for decision in decisions
                if isinstance(decision.get("provisional_research_schedule"), Mapping)
                and decision["provisional_research_schedule"].get("status")
                == "represented_by_research_unit"
            ),
            "neighborhood_count": len(neighborhoods),
        },
        artifacts={
            "prioritized_problems_json": str(out_json),
            "prioritized_problems_md": str(out_md),
            "prioritizer_prompt": str(run_out_dir / f"{tag}.prompt.txt"),
            "prioritizer_response": str(run_out_dir / f"{tag}.response.txt"),
        },
    )
    stage_doc = attach_stage_model_invocation_contract(
        stage_doc,
        agent=agent,
        dry_run=dry_run,
        manifest_refs=invocation_tracker.collect(),
        invocation_expected=bool(problem_records),
    )

    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(
        _json.dumps(stage_doc, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    records_by_id: dict[str, dict[str, Any]] = {
        str(item.get("problem_id")): item
        for item in problem_records
        if isinstance(item, dict) and isinstance(item.get("problem_id"), str)
    }
    title = out_json.stem.removesuffix(".prioritized_problems") or "Prioritized Problems"
    out_md.write_text(
        _render_prioritized_problems_markdown(
            decisions,
            problem_records_by_id=records_by_id,
            title=f"{title} – Prioritized Problems",
        ),
        encoding="utf-8",
    )

    print(f"[stage2] wrote {out_json}", file=sys.stderr)
    print(f"[stage2] wrote {out_md}", file=sys.stderr)
    return stage_doc


__all__ = [name for name in globals() if not name.startswith("__")]
