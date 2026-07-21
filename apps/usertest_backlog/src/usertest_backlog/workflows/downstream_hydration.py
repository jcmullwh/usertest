from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from hashlib import sha256
from pathlib import Path
from typing import Any

from backlog_core import (
    DOWNSTREAM_CHAIN_CONTRACT_REVISION,
    assess_change_plan_readiness,
    assess_research_readiness,
    assess_selection_readiness,
    assess_solution_option_readiness,
    downstream_chain_input_sha256,
    plan_revision_id_for,
    research_actionability_assessment,
)

from usertest_backlog.workflows.depth_contracts import research_contract_view
from usertest_backlog.workflows.research_hydration import (
    hydrate_retained_research_evidence,
    hydrate_retained_research_proof,
)


def _text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def _canonical_sha256(value: Any) -> str:
    return sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _flatten_artifact_refs(value: Any, *, prefix: str = "") -> list[dict[str, str]]:
    if isinstance(value, str) and value.strip():
        return [{"name": prefix or "artifact", "path": value.strip()}]
    if not isinstance(value, Mapping):
        return []
    refs: list[dict[str, str]] = []
    for raw_key in sorted(value, key=str):
        key = str(raw_key)
        child_prefix = f"{prefix}.{key}" if prefix else key
        refs.extend(_flatten_artifact_refs(value[raw_key], prefix=child_prefix))
    return refs


def _prior_context(record: Mapping[str, Any]) -> Mapping[str, Any]:
    raw = record.get("prior_stage_context")
    return raw if isinstance(raw, Mapping) else {}


def _source_evidence_atom_ids(record: Mapping[str, Any]) -> list[str]:
    raw = record.get("source_evidence_atom_ids")
    return sorted(
        {
            value.strip()
            for value in (raw if isinstance(raw, list) else [])
            if isinstance(value, str) and value.strip()
        }
    )


def _case_revision(record: Mapping[str, Any]) -> int:
    try:
        return max(1, int(record.get("case_revision") or 1))
    except (TypeError, ValueError):
        return 1


def _load_stage_case_records(
    *,
    summary: Mapping[str, Any],
    stage: str,
    allowed_ref_names: set[str],
    case_id: str,
    problem_id: str,
) -> tuple[list[dict[str, Any]] | None, list[str]]:
    if summary.get("downstream_contract_revision") != DOWNSTREAM_CHAIN_CONTRACT_REVISION:
        return None, [f"retained_{stage}_contract_revision_missing_or_stale"]
    expected_digest = _text(summary.get("full_records_sha256"))
    if expected_digest is None or len(expected_digest) != 64:
        return None, [f"retained_{stage}_records_digest_missing_or_invalid"]
    ref_raw = summary.get("stage_artifact_ref")
    ref = dict(ref_raw) if isinstance(ref_raw, Mapping) else {}
    ref_name = _text(ref.get("name"))
    ref_path = _text(ref.get("path"))
    if ref_name not in allowed_ref_names or ref_path is None:
        return None, [f"retained_{stage}_artifact_ref_missing_or_invalid"]
    path = Path(ref_path).expanduser()
    if not path.is_file():
        return None, [f"retained_{stage}_artifact_missing"]
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None, [f"retained_{stage}_artifact_unreadable"]
    if not isinstance(document, dict) or document.get("stage") != stage:
        return None, [f"retained_{stage}_artifact_stage_invalid"]
    exact_ref = {"name": ref_name, "path": ref_path}
    if exact_ref not in _flatten_artifact_refs(document.get("artifacts")):
        return None, [f"retained_{stage}_artifact_self_ref_mismatch"]
    raw_items = document.get("items")
    if not isinstance(raw_items, list):
        return None, [f"retained_{stage}_items_invalid"]
    records = [
        dict(item)
        for item in raw_items
        if isinstance(item, Mapping)
        and _text(item.get("case_id")) == case_id
        and _text(item.get("problem_id")) == problem_id
    ]
    if not records:
        return None, [f"retained_{stage}_case_records_missing"]
    if _canonical_sha256(records) != expected_digest.casefold():
        return None, [f"retained_{stage}_records_digest_mismatch"]
    if _text(summary.get("problem_id")) != problem_id:
        return None, [f"retained_{stage}_summary_problem_identity_mismatch"]
    return records, []


def _expected_input_sha256(
    *,
    stage: str,
    record: Mapping[str, Any],
    research_dossier_sha256: str | None,
    option_records_sha256: str | None = None,
    selection_records_sha256: str | None = None,
) -> str:
    return downstream_chain_input_sha256(
        stage=stage,
        case_id=_text(record.get("case_id")),
        case_revision=_case_revision(record),
        source_evidence_atom_ids=_source_evidence_atom_ids(record),
        source_evidence_snapshot_sha256=_text(
            record.get("source_evidence_snapshot_sha256")
        ),
        research_dossier_sha256=research_dossier_sha256,
        option_records_sha256=option_records_sha256,
        selection_records_sha256=selection_records_sha256,
    )


def hydrate_retained_insufficient_evidence_disposition(
    record: Mapping[str, Any],
) -> tuple[dict[str, Any] | None, list[str]]:
    """Authenticate one exact Stage-4 wait-for-evidence disposition.

    A verified but implementation-unready research proof should not spend another model
    turn merely because the readiness gate correctly stopped optioning.  It may park only
    when the exact current dossier and a content-bound, zero-option Stage-4 artifact agree
    on the same research-readiness blockers.  Missing or stale Stage-4 state remains a
    downstream cache miss; invalid Stage-3 evidence still routes back to research.
    """

    case_id = _text(record.get("case_id"))
    problem_id = _text(record.get("problem_id"))
    if case_id is None or problem_id is None:
        return None, ["retained_insufficient_evidence_case_identity_missing"]

    research, research_errors = hydrate_retained_research_evidence(record)
    if research is None or research_errors:
        return None, [
            "retained_insufficient_evidence_research_invalid",
            *(
                f"retained_insufficient_evidence_research:{error}"
                for error in research_errors
            ),
        ]
    if (
        _text(research.get("case_id")) != case_id
        or _text(research.get("problem_id")) != problem_id
    ):
        return None, ["retained_insufficient_evidence_research_identity_mismatch"]
    if _text(research.get("research_status")) != "insufficient_evidence":
        return None, []

    ready, readiness_blockers = assess_research_readiness(
        research_contract_view(research)
    )
    if ready or not readiness_blockers:
        return None, ["retained_insufficient_evidence_research_readiness_mismatch"]

    context = _prior_context(record)
    research_context_raw = context.get("research")
    research_context = (
        research_context_raw if isinstance(research_context_raw, Mapping) else {}
    )
    research_summary_raw = research_context.get("current")
    research_summary = (
        research_summary_raw if isinstance(research_summary_raw, Mapping) else {}
    )
    research_digest = _text(research_summary.get("full_dossier_sha256"))
    if research_digest is None or _canonical_sha256(research) != research_digest:
        return None, ["retained_insufficient_evidence_research_digest_mismatch"]

    option_summary_raw = context.get("optioning")
    option_summary = (
        option_summary_raw if isinstance(option_summary_raw, Mapping) else {}
    )
    if not option_summary:
        return None, ["retained_insufficient_evidence_optioning_summary_missing"]
    if option_summary.get("downstream_contract_revision") != DOWNSTREAM_CHAIN_CONTRACT_REVISION:
        return None, ["retained_insufficient_evidence_contract_revision_missing_or_stale"]
    if (
        _text(option_summary.get("case_id")) != case_id
        or _text(option_summary.get("problem_id")) != problem_id
    ):
        return None, ["retained_insufficient_evidence_summary_identity_mismatch"]
    if _text(option_summary.get("optioning_status")) != "insufficient_evidence":
        return None, ["retained_insufficient_evidence_optioning_status_invalid"]
    if option_summary.get("input_chain_sha256") != _expected_input_sha256(
        stage="solution_optioning",
        record=record,
        research_dossier_sha256=research_digest,
    ):
        return None, ["retained_insufficient_evidence_input_chain_mismatch"]
    if option_summary.get("full_records_sha256") != _canonical_sha256([]):
        return None, ["retained_insufficient_evidence_option_records_digest_invalid"]
    if option_summary.get("option_ids") != [] or option_summary.get("family_ids") != []:
        return None, ["retained_insufficient_evidence_option_identity_set_not_empty"]
    if option_summary.get("optioning_outcome_count") != 1:
        return None, ["retained_insufficient_evidence_outcome_count_invalid"]
    expected_outcome_digest = _text(option_summary.get("optioning_outcome_sha256"))
    if (
        expected_outcome_digest is None
        or len(expected_outcome_digest) != 64
        or any(
            character not in "0123456789abcdef"
            for character in expected_outcome_digest.casefold()
        )
    ):
        return None, ["retained_insufficient_evidence_outcome_digest_missing_or_invalid"]
    if option_summary.get("research_readiness_blockers") != readiness_blockers:
        return None, ["retained_insufficient_evidence_summary_blockers_mismatch"]

    ref_raw = option_summary.get("stage_artifact_ref")
    ref = dict(ref_raw) if isinstance(ref_raw, Mapping) else {}
    ref_name = _text(ref.get("name"))
    ref_path = _text(ref.get("path"))
    if ref_name != "solution_options_json" or ref_path is None:
        return None, ["retained_insufficient_evidence_artifact_ref_missing_or_invalid"]
    path = Path(ref_path).expanduser()
    if not path.is_file():
        return None, ["retained_insufficient_evidence_artifact_missing"]
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None, ["retained_insufficient_evidence_artifact_unreadable"]
    if not isinstance(document, dict) or document.get("stage") != "solution_optioning":
        return None, ["retained_insufficient_evidence_artifact_stage_invalid"]
    exact_ref = {"name": ref_name, "path": ref_path}
    if exact_ref not in _flatten_artifact_refs(document.get("artifacts")):
        return None, ["retained_insufficient_evidence_artifact_self_ref_mismatch"]

    raw_items = document.get("items")
    if not isinstance(raw_items, list):
        return None, ["retained_insufficient_evidence_artifact_items_invalid"]
    if any(
        isinstance(item, Mapping)
        and (
            _text(item.get("case_id")) == case_id
            or _text(item.get("problem_id")) == problem_id
        )
        for item in raw_items
    ):
        return None, ["retained_insufficient_evidence_artifact_has_case_options"]

    input_meta_raw = document.get("input_meta")
    input_meta = input_meta_raw if isinstance(input_meta_raw, Mapping) else {}
    outcomes_raw = input_meta.get("optioning_outcomes")
    if not isinstance(outcomes_raw, list):
        return None, ["retained_insufficient_evidence_artifact_outcomes_invalid"]
    matches = [
        dict(outcome)
        for outcome in outcomes_raw
        if isinstance(outcome, Mapping)
        and (
            _text(outcome.get("case_id")) == case_id
            or _text(outcome.get("problem_id")) == problem_id
        )
    ]
    if len(matches) != 1:
        return None, [
            "retained_insufficient_evidence_outcome_missing"
            if not matches
            else "retained_insufficient_evidence_outcome_ambiguous"
        ]
    outcome = matches[0]
    if _canonical_sha256(outcome) != expected_outcome_digest.casefold():
        return None, ["retained_insufficient_evidence_outcome_digest_mismatch"]
    if _text(outcome.get("optioning_status")) != "insufficient_evidence":
        return None, ["retained_insufficient_evidence_outcome_status_invalid"]
    if outcome.get("research_readiness_blockers") != readiness_blockers:
        return None, ["retained_insufficient_evidence_outcome_blockers_mismatch"]
    if outcome.get("option_count") != 0 or outcome.get("rejected_option_count") != 0:
        return None, ["retained_insufficient_evidence_outcome_counts_invalid"]
    if _text(outcome.get("decision_rationale")) is None:
        return None, ["retained_insufficient_evidence_outcome_rationale_missing"]

    return {
        "contract_revision": "runner_retained_insufficient_evidence_v1",
        "case_id": case_id,
        "problem_id": problem_id,
        "optioning_status": "insufficient_evidence",
        "research_status": "insufficient_evidence",
        "research_readiness_blockers": list(readiness_blockers),
        "research_dossier_sha256": research_digest,
        "input_chain_sha256": option_summary["input_chain_sha256"],
        "optioning_outcome_sha256": expected_outcome_digest.casefold(),
    }, []


def hydrate_retained_no_change_disposition(
    record: Mapping[str, Any],
    *,
    research_dossier: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any] | None, list[str]]:
    """Authenticate one exact Stage-4 no-change disposition.

    ``already_addressed`` and ``non_actionable`` are nonterminal research findings.
    They may park a case only when the current complete research proof and the exact
    zero-option Stage-4 artifact agree.  A missing or stale disposition is a normal
    downstream cache miss and must be rebuilt; it never resolves the case.
    """

    case_id = _text(record.get("case_id"))
    problem_id = _text(record.get("problem_id"))
    if case_id is None or problem_id is None:
        return None, ["retained_no_change_case_identity_missing"]

    research = dict(research_dossier) if isinstance(research_dossier, Mapping) else None
    if research is None:
        research, research_errors = hydrate_retained_research_proof(record)
        if research is None or research_errors:
            return None, [
                "retained_no_change_research_invalid",
                *(f"retained_no_change_research:{error}" for error in research_errors),
            ]
    if _text(research.get("case_id")) != case_id or _text(research.get("problem_id")) != problem_id:
        return None, ["retained_no_change_research_identity_mismatch"]

    actionability = research_actionability_assessment(research)
    disposition = _text(actionability.get("disposition"))
    if disposition not in {"already_addressed", "non_actionable"}:
        return None, []
    evidence_refs = actionability.get("evidence_refs")
    expected_evidence_refs = (
        [value.strip() for value in evidence_refs if isinstance(value, str) and value.strip()]
        if isinstance(evidence_refs, list)
        else []
    )
    if not expected_evidence_refs or len(expected_evidence_refs) != len(evidence_refs):
        return None, ["retained_no_change_research_evidence_refs_invalid"]

    context = _prior_context(record)
    research_context_raw = context.get("research")
    research_context = research_context_raw if isinstance(research_context_raw, Mapping) else {}
    research_summary_raw = research_context.get("current")
    research_summary = research_summary_raw if isinstance(research_summary_raw, Mapping) else {}
    research_digest = _text(research_summary.get("full_dossier_sha256"))
    if research_digest is None or _canonical_sha256(research) != research_digest:
        return None, ["retained_no_change_research_digest_mismatch"]

    option_summary_raw = context.get("optioning")
    option_summary = option_summary_raw if isinstance(option_summary_raw, Mapping) else {}
    if not option_summary:
        return None, ["retained_no_change_optioning_summary_missing"]
    if option_summary.get("downstream_contract_revision") != DOWNSTREAM_CHAIN_CONTRACT_REVISION:
        return None, ["retained_no_change_contract_revision_missing_or_stale"]
    if (
        _text(option_summary.get("case_id")) != case_id
        or _text(option_summary.get("problem_id")) != problem_id
    ):
        return None, ["retained_no_change_summary_identity_mismatch"]
    if _text(option_summary.get("optioning_status")) != "not_required":
        return None, ["retained_no_change_optioning_status_invalid"]
    if option_summary.get("input_chain_sha256") != _expected_input_sha256(
        stage="solution_optioning",
        record=record,
        research_dossier_sha256=research_digest,
    ):
        return None, ["retained_no_change_input_chain_mismatch"]
    if option_summary.get("full_records_sha256") != _canonical_sha256([]):
        return None, ["retained_no_change_option_records_digest_invalid"]
    if option_summary.get("option_ids") != [] or option_summary.get("family_ids") != []:
        return None, ["retained_no_change_option_identity_set_not_empty"]
    if option_summary.get("optioning_outcome_count") != 1:
        return None, ["retained_no_change_outcome_count_invalid"]
    expected_outcome_digest = _text(option_summary.get("optioning_outcome_sha256"))
    if (
        expected_outcome_digest is None
        or len(expected_outcome_digest) != 64
        or any(
            character not in "0123456789abcdef"
            for character in expected_outcome_digest.casefold()
        )
    ):
        return None, ["retained_no_change_outcome_digest_missing_or_invalid"]
    if _text(option_summary.get("research_actionability_disposition")) != disposition:
        return None, ["retained_no_change_summary_disposition_mismatch"]
    if option_summary.get("actionability_evidence_refs") != expected_evidence_refs:
        return None, ["retained_no_change_summary_evidence_refs_mismatch"]
    summary_blockers = option_summary.get("research_readiness_blockers")
    if summary_blockers not in (None, []):
        return None, ["retained_no_change_summary_has_blockers"]

    ref_raw = option_summary.get("stage_artifact_ref")
    ref = dict(ref_raw) if isinstance(ref_raw, Mapping) else {}
    ref_name = _text(ref.get("name"))
    ref_path = _text(ref.get("path"))
    if ref_name != "solution_options_json" or ref_path is None:
        return None, ["retained_no_change_artifact_ref_missing_or_invalid"]
    path = Path(ref_path).expanduser()
    if not path.is_file():
        return None, ["retained_no_change_artifact_missing"]
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None, ["retained_no_change_artifact_unreadable"]
    if not isinstance(document, dict) or document.get("stage") != "solution_optioning":
        return None, ["retained_no_change_artifact_stage_invalid"]
    exact_ref = {"name": ref_name, "path": ref_path}
    if exact_ref not in _flatten_artifact_refs(document.get("artifacts")):
        return None, ["retained_no_change_artifact_self_ref_mismatch"]

    raw_items = document.get("items")
    if not isinstance(raw_items, list):
        return None, ["retained_no_change_artifact_items_invalid"]
    case_options = [
        item
        for item in raw_items
        if isinstance(item, Mapping)
        and (_text(item.get("case_id")) == case_id or _text(item.get("problem_id")) == problem_id)
    ]
    if case_options:
        return None, ["retained_no_change_artifact_has_case_options"]

    input_meta_raw = document.get("input_meta")
    input_meta = input_meta_raw if isinstance(input_meta_raw, Mapping) else {}
    outcomes_raw = input_meta.get("optioning_outcomes")
    if not isinstance(outcomes_raw, list):
        return None, ["retained_no_change_artifact_outcomes_invalid"]
    matches = [
        dict(outcome)
        for outcome in outcomes_raw
        if isinstance(outcome, Mapping)
        and (
            _text(outcome.get("case_id")) == case_id
            or _text(outcome.get("problem_id")) == problem_id
        )
    ]
    if len(matches) != 1:
        return None, [
            "retained_no_change_outcome_missing"
            if not matches
            else "retained_no_change_outcome_ambiguous"
        ]
    outcome = matches[0]
    if _canonical_sha256(outcome) != expected_outcome_digest.casefold():
        return None, ["retained_no_change_outcome_digest_mismatch"]
    if _text(outcome.get("optioning_status")) != "not_required":
        return None, ["retained_no_change_outcome_status_invalid"]
    if _text(outcome.get("research_actionability_disposition")) != disposition:
        return None, ["retained_no_change_outcome_disposition_mismatch"]
    if outcome.get("evidence_refs") != expected_evidence_refs:
        return None, ["retained_no_change_outcome_evidence_refs_mismatch"]
    if outcome.get("research_readiness_blockers") != []:
        return None, ["retained_no_change_outcome_has_blockers"]
    if outcome.get("option_count") != 0 or outcome.get("rejected_option_count") != 0:
        return None, ["retained_no_change_outcome_counts_invalid"]
    if _text(outcome.get("decision_rationale")) is None:
        return None, ["retained_no_change_outcome_rationale_missing"]

    return {
        "contract_revision": "runner_retained_no_change_v1",
        "case_id": case_id,
        "problem_id": problem_id,
        "optioning_status": "not_required",
        "research_actionability_disposition": disposition,
        "evidence_refs": expected_evidence_refs,
        "research_dossier_sha256": research_digest,
        "input_chain_sha256": option_summary["input_chain_sha256"],
        "optioning_outcome_sha256": expected_outcome_digest.casefold(),
        "live_verification_status": "unverified",
    }, []


def hydrate_retained_downstream_chain(
    record: Mapping[str, Any],
    *,
    research_dossier: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any] | None, list[str]]:
    """Hydrate one exact, current research-to-plan chain or return why it must rerun.

    Missing and legacy summaries are normal cache misses. They never terminate the case and
    never advance stale output; the caller simply follows the ordinary downstream model path.
    """

    case_id = _text(record.get("case_id"))
    problem_id = _text(record.get("problem_id"))
    if case_id is None or problem_id is None:
        return None, ["retained_downstream_case_identity_missing"]

    research = dict(research_dossier) if isinstance(research_dossier, Mapping) else None
    if research is None:
        research, research_errors = hydrate_retained_research_proof(record)
        if research is None or research_errors:
            return None, [
                "retained_downstream_research_invalid",
                *(f"retained_downstream_research:{error}" for error in research_errors),
            ]
    if _text(research.get("case_id")) != case_id or _text(research.get("problem_id")) != problem_id:
        return None, ["retained_downstream_research_identity_mismatch"]

    context = _prior_context(record)
    research_context_raw = context.get("research")
    research_context = (
        research_context_raw if isinstance(research_context_raw, Mapping) else {}
    )
    research_summary_raw = research_context.get("current")
    research_summary = (
        research_summary_raw if isinstance(research_summary_raw, Mapping) else {}
    )
    research_digest = _text(research_summary.get("full_dossier_sha256"))
    if research_digest is None or _canonical_sha256(research) != research_digest:
        return None, ["retained_downstream_research_digest_mismatch"]

    option_summary_raw = context.get("optioning")
    option_summary = option_summary_raw if isinstance(option_summary_raw, Mapping) else {}
    options, errors = _load_stage_case_records(
        summary=option_summary,
        stage="solution_optioning",
        allowed_ref_names={"solution_options_json"},
        case_id=case_id,
        problem_id=problem_id,
    )
    if options is None:
        return None, errors
    option_digest = _canonical_sha256(options)
    if option_summary.get("input_chain_sha256") != _expected_input_sha256(
        stage="solution_optioning",
        record=record,
        research_dossier_sha256=research_digest,
    ):
        return None, ["retained_solution_optioning_input_chain_mismatch"]
    option_errors = [
        error
        for option in options
        for ready, reasons in [assess_solution_option_readiness(option, research=research)]
        if not ready
        for error in reasons
    ]
    if option_errors:
        return None, [
            "retained_solution_optioning_not_ready",
            *(f"retained_solution_optioning_readiness:{error}" for error in option_errors),
        ]

    selection_summary_raw = context.get("selection")
    selection_summary = (
        selection_summary_raw if isinstance(selection_summary_raw, Mapping) else {}
    )
    selections, errors = _load_stage_case_records(
        summary=selection_summary,
        stage="solution_selection",
        allowed_ref_names={"solution_selection_json"},
        case_id=case_id,
        problem_id=problem_id,
    )
    if selections is None:
        return None, errors
    if len(selections) != 1:
        return None, ["retained_solution_selection_record_ambiguous"]
    selection = selections[0]
    selection_digest = _canonical_sha256(selections)
    if selection_summary.get("input_chain_sha256") != _expected_input_sha256(
        stage="solution_selection",
        record=record,
        research_dossier_sha256=research_digest,
        option_records_sha256=option_digest,
    ):
        return None, ["retained_solution_selection_input_chain_mismatch"]
    selection_ready, selection_errors = assess_selection_readiness(
        selection,
        options=options,
        research=research,
    )
    if not selection_ready:
        return None, [
            "retained_solution_selection_not_ready",
            *(f"retained_solution_selection_readiness:{error}" for error in selection_errors),
        ]

    planning_summary_raw = context.get("planning")
    planning_summary = (
        planning_summary_raw if isinstance(planning_summary_raw, Mapping) else {}
    )
    plans, errors = _load_stage_case_records(
        summary=planning_summary,
        stage="implementation_planning",
        allowed_ref_names={"change_plans_json"},
        case_id=case_id,
        problem_id=problem_id,
    )
    if plans is None:
        return None, errors
    if planning_summary.get("input_chain_sha256") != _expected_input_sha256(
        stage="implementation_planning",
        record=record,
        research_dossier_sha256=research_digest,
        option_records_sha256=option_digest,
        selection_records_sha256=selection_digest,
    ):
        return None, ["retained_implementation_planning_input_chain_mismatch"]
    expected_revision_ids = sorted(
        value
        for value in planning_summary.get("plan_revision_ids", [])
        if isinstance(value, str) and value.strip()
    )
    observed_revision_ids = sorted(
        value
        for plan in plans
        for value in [_text(plan.get("plan_revision_id"))]
        if value is not None
    )
    if not expected_revision_ids or observed_revision_ids != expected_revision_ids:
        return None, ["retained_implementation_planning_revision_set_mismatch"]
    for plan in plans:
        if (
            plan.get("plan_revision_source") != "server_content_addressed_v1"
            or _text(plan.get("plan_revision_id")) != plan_revision_id_for(plan)
        ):
            return None, ["retained_implementation_planning_revision_invalid"]
        plan_ready, plan_errors = assess_change_plan_readiness(
            plan,
            problem=record,
            research=research,
            selection=selection,
        )
        if not plan_ready:
            return None, [
                "retained_implementation_planning_not_ready",
                *(f"retained_implementation_planning_readiness:{error}" for error in plan_errors),
            ]

    chain = {
        "contract_revision": DOWNSTREAM_CHAIN_CONTRACT_REVISION,
        "case_id": case_id,
        "problem_id": problem_id,
        "research_dossier": research,
        "solution_options": options,
        "selection_decisions": selections,
        "change_plans": plans,
        "research_dossier_sha256": research_digest,
        "option_records_sha256": option_digest,
        "selection_records_sha256": selection_digest,
        "planning_records_sha256": _canonical_sha256(plans),
    }
    chain["chain_sha256"] = _canonical_sha256(chain)
    return chain, []


def chain_matches_research_dossier(
    chain: Mapping[str, Any], dossier: Mapping[str, Any]
) -> bool:
    return (
        _text(chain.get("case_id")) == _text(dossier.get("case_id"))
        and _text(chain.get("problem_id")) == _text(dossier.get("problem_id"))
        and _text(chain.get("research_dossier_sha256")) == _canonical_sha256(dossier)
    )


def flatten_chain_items(
    chains: Sequence[Mapping[str, Any]], field: str
) -> list[dict[str, Any]]:
    return [
        dict(item)
        for chain in chains
        for item in (
            chain.get(field) if isinstance(chain.get(field), list) else []
        )
        if isinstance(item, Mapping)
    ]


__all__ = [
    "chain_matches_research_dossier",
    "flatten_chain_items",
    "hydrate_retained_downstream_chain",
    "hydrate_retained_insufficient_evidence_disposition",
    "hydrate_retained_no_change_disposition",
]
