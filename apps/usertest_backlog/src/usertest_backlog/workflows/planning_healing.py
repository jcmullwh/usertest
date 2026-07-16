"""Same-author correction and evidence routing for implementation planning.

Stage 6 is not an all-or-nothing parser.  A planner keeps its exact conversation and
read-only revision while repairing arbitrary structural or quality findings.  Valid plan
units are retained at the correction frontier, but an incomplete response never becomes an
implementation-ready ticket.  Evidence gaps that the server proves cannot be repaired by
planner prose are routed back to research instead of being mislabeled as bad planning.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Mapping
from copy import deepcopy
from hashlib import sha256
from pathlib import Path
from typing import Any

from backlog_core import assign_plan_revision_id, bind_plan_outcome_oracle
from backlog_core.stage_contracts import parse_change_plan_list
from backlog_repo.plan_scope import build_plan_target_contract
from runner_core import RunnerConfig

from usertest_backlog.workflows.depth_contracts import change_plan_quality_errors
from usertest_backlog.workflows.selection_healing import (
    _canonical_sha256,
    _role_run_record,
    _run_role_conversation,
)


def _nonempty(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _string_list(value: Any, *, require_nonempty: bool = False) -> list[str] | None:
    if not isinstance(value, list):
        return None
    result = [item.strip() for item in value if isinstance(item, str) and item.strip()]
    if len(result) != len(value) or (require_nonempty and not result):
        return None
    return result


def _json_value(response: str) -> Any:
    text = response.strip()
    if text.startswith("```") and text.endswith("```"):
        first_newline = text.find("\n")
        if first_newline >= 0:
            text = text[first_newline + 1 : -3].strip()
    return json.loads(text)


def _research_reference_ids(research: Mapping[str, Any]) -> set[str]:
    """Collect upstream identifiers a planner can use to ground a research return."""

    refs: set[str] = set()

    def visit(value: Any, *, key: str | None = None) -> None:
        if isinstance(value, Mapping):
            for child_key, child in value.items():
                visit(child, key=str(child_key))
            return
        if isinstance(value, list):
            for child in value:
                visit(child, key=key)
            return
        if not isinstance(value, str) or not value.strip():
            return
        text = value.strip()
        if key is not None and (
            key.endswith("_id")
            or key.endswith("_ids")
            or key in {"unknown", "evidence_boundaries"}
        ):
            refs.add(text)

    visit(research)
    return refs


def _validate_research_required(
    item: Mapping[str, Any],
    *,
    expected_case_id: str,
    expected_problem_id: str,
    expected_option_id: str,
    research_reference_ids: set[str],
) -> tuple[dict[str, Any], list[str]]:
    errors: list[str] = []
    if item.get("planning_status") != "research_required":
        errors.append("planner_research_return_status_invalid")
    for field, expected in (
        ("case_id", expected_case_id),
        ("problem_id", expected_problem_id),
        ("selected_option_id", expected_option_id),
    ):
        if item.get(field) != expected:
            errors.append(f"planner_research_return_{field}_mismatch")
    if item.get("return_to_stage") != "repro_research":
        errors.append("planner_research_return_stage_invalid")
    gaps_raw = item.get("evidence_gaps")
    gaps = gaps_raw if isinstance(gaps_raw, list) else []
    if not gaps:
        errors.append("planner_research_return_evidence_gaps_missing")
    normalized_gaps: list[dict[str, Any]] = []
    for index, gap_raw in enumerate(gaps):
        if not isinstance(gap_raw, Mapping):
            errors.append(f"planner_research_gap_not_an_object:{index}")
            continue
        gap = _nonempty(gap_raw.get("gap"))
        evidence_phase = _nonempty(gap_raw.get("evidence_phase"))
        evidence_needed = _nonempty(gap_raw.get("evidence_needed"))
        blocks = _string_list(gap_raw.get("blocks"), require_nonempty=True)
        refs = _string_list(gap_raw.get("evidence_refs"), require_nonempty=True)
        if gap is None:
            errors.append(f"planner_research_gap_description_missing:{index}")
        if evidence_phase != "pre_change_decision_evidence":
            errors.append(f"planner_research_gap_evidence_phase_invalid:{index}")
        if evidence_needed is None:
            errors.append(f"planner_research_gap_evidence_needed_missing:{index}")
        # ``blocks`` is deliberately an open descriptive vocabulary.  Requiring a
        # hand-authored category list would reject unforeseen compatibility, boundary,
        # failure-mode, or platform decisions even when their evidence reference is bound.
        if blocks is None:
            errors.append(f"planner_research_gap_material_decision_missing:{index}")
        if refs is None or not set(refs).issubset(research_reference_ids):
            errors.append(f"planner_research_gap_refs_unbound:{index}")
        if (
            gap is not None
            and evidence_phase == "pre_change_decision_evidence"
            and evidence_needed is not None
            and blocks is not None
            and refs is not None
        ):
            normalized_gaps.append(
                {
                    "gap": gap,
                    "evidence_phase": evidence_phase,
                    "blocks": blocks,
                    "evidence_needed": evidence_needed,
                    "evidence_refs": refs,
                }
            )
    normalized = {
        "planning_status": "research_required",
        "case_id": expected_case_id,
        "problem_id": expected_problem_id,
        "selected_option_id": expected_option_id,
        "return_to_stage": "repro_research",
        "evidence_gaps": normalized_gaps,
        "rationale": _nonempty(item.get("rationale")),
        "source": "planner_grounded_research_return",
    }
    if normalized["rationale"] is None:
        errors.append("planner_research_return_rationale_missing")
    return normalized, errors


def _server_research_return(
    *,
    expected_case_id: str,
    expected_problem_id: str,
    expected_option_id: str,
    plan_key: str,
    reasons: list[str],
) -> dict[str, Any]:
    return {
        "planning_status": "research_required",
        "case_id": expected_case_id,
        "problem_id": expected_problem_id,
        "selected_option_id": expected_option_id,
        "return_to_stage": "repro_research",
        "source": "server_evidence_binding",
        "plan_candidate_key": plan_key,
        "reasons": list(dict.fromkeys(reasons)),
    }


def _bind_stage5_selected_option_contracts(
    plan: Mapping[str, Any],
    *,
    selection_decision: Mapping[str, Any],
) -> dict[str, Any]:
    """Copy immutable selected-option contracts onto a Stage 6 plan.

    Stage 6 owns implementation detail, not prose rewrites of the causal-coverage and
    scope-evidence contracts already selected and falsified in Stage 5. Binding the exact
    upstream JSON here keeps strict downstream linkage while avoiding correction turns for
    semantically irrelevant copy drift. Deep copies prevent plan enrichment from mutating
    Stage 5.
    """

    selected_option = selection_decision.get("selected_option")
    if not isinstance(selected_option, Mapping):
        return dict(plan)
    bound = dict(plan)
    for field in ("causal_coverage", "scope_evidence"):
        immutable_contract = selected_option.get(field)
        if isinstance(immutable_contract, Mapping):
            bound[field] = deepcopy(dict(immutable_contract))
    return bound


def _finalize_plan_candidate(
    item: Mapping[str, Any],
    *,
    item_index: int,
    duplicate_plan_ids: set[str],
    expected_case_id: str,
    expected_problem_id: str,
    expected_option_id: str,
    expected_revision: str,
    target_repo_root: Path,
    problem_record: Mapping[str, Any],
    research_dossier: Mapping[str, Any],
    selection_decision: Mapping[str, Any],
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, list[str]]:
    model_plan_id = _nonempty(item.get("change_plan_id"))
    plan_key = model_plan_id or f"index:{item_index}"
    parsed, parse_warnings = parse_change_plan_list(
        json.dumps([dict(item)], ensure_ascii=False),
        allow_pending_target_contract=True,
    )
    errors = [str(warning) for warning in parse_warnings if str(warning).strip()]
    plan = dict(parsed[0]) if parsed and isinstance(parsed[0], dict) else dict(item)
    plan = _bind_stage5_selected_option_contracts(
        plan,
        selection_decision=selection_decision,
    )
    if model_plan_id is not None and model_plan_id in duplicate_plan_ids:
        errors.append(f"change_plan_duplicate_model_plan_id:{model_plan_id}")
    try:
        plan = {
            **plan,
            "target_contract": build_plan_target_contract(plan, repo_root=target_repo_root),
        }
    except (OSError, UnicodeError, ValueError) as exc:
        errors.append(
            f"change_plan_target_contract_invalid:{expected_problem_id}:"
            f"{type(exc).__name__}:{exc}"
        )
    try:
        plan = bind_plan_outcome_oracle(
            plan,
            research=research_dossier,
            selection=selection_decision,
        )
    except ValueError as exc:
        # Outcome semantics are server-bound exclusively from Stage 3/5 evidence.  No
        # planner rewrite can mint a missing contract or repair an unbound selection.
        return (
            None,
            _server_research_return(
                expected_case_id=expected_case_id,
                expected_problem_id=expected_problem_id,
                expected_option_id=expected_option_id,
                plan_key=plan_key,
                reasons=[f"change_plan_outcome_oracle_binding_requires_research:{exc}"],
            ),
            [],
        )
    plan = assign_plan_revision_id(plan)
    if plan.get("problem_id") != expected_problem_id:
        errors.append(
            "change_plan_problem_id_mismatch:"
            f"expected={expected_problem_id}:got={plan.get('problem_id')}"
        )
    if plan.get("selected_option_id") != expected_option_id:
        errors.append(
            "change_plan_selected_option_id_mismatch:"
            f"expected={expected_option_id}:got={plan.get('selected_option_id')}"
        )
    errors.extend(
        change_plan_quality_errors(
            plan,
            expected_revision=expected_revision,
            expected_case_id=expected_case_id,
            repo_root=target_repo_root,
            problem_record=problem_record,
            research_dossier=research_dossier,
            selection_decision=selection_decision,
        )
    )
    errors = list(dict.fromkeys(str(error) for error in errors if str(error).strip()))
    material_errors = [error for error in errors if "research_required" in error]
    if material_errors:
        return (
            None,
            _server_research_return(
                expected_case_id=expected_case_id,
                expected_problem_id=expected_problem_id,
                expected_option_id=expected_option_id,
                plan_key=plan_key,
                reasons=material_errors,
            ),
            [],
        )
    return (plan if not errors else None), None, errors


def _planning_response_projection(
    response: str,
    *,
    expected_case_id: str,
    expected_problem_id: str,
    expected_option_id: str,
    expected_revision: str,
    target_repo_root: Path,
    problem_record: Mapping[str, Any],
    research_dossier: Mapping[str, Any],
    selection_decision: Mapping[str, Any],
) -> tuple[dict[str, Any], list[str], list[str]]:
    try:
        raw = _json_value(response)
    except Exception as exc:  # noqa: BLE001 - arbitrary parser feedback is repairable
        return (
            {"valid_plans": [], "research_required": [], "invalid_plans": []},
            [f"{type(exc).__name__}: {exc}"],
            [],
        )
    items = raw if isinstance(raw, list) else [raw]
    if not items:
        return (
            {"valid_plans": [], "research_required": [], "invalid_plans": []},
            ["planner_response_empty"],
            [],
        )
    plan_ids = [
        plan_id
        for item in items
        if isinstance(item, Mapping) and item.get("planning_status") != "research_required"
        for plan_id in [_nonempty(item.get("change_plan_id"))]
        if plan_id is not None
    ]
    duplicate_plan_ids = {plan_id for plan_id, count in Counter(plan_ids).items() if count > 1}
    research_refs = _research_reference_ids(research_dossier)
    valid_plans: list[dict[str, Any]] = []
    research_required: list[dict[str, Any]] = []
    invalid_plans: list[dict[str, Any]] = []
    errors: list[str] = []
    valid_keys: list[str] = []
    for index, raw_item in enumerate(items):
        if not isinstance(raw_item, Mapping):
            item_errors = [f"change_plan_not_an_object:index={index}"]
            errors.extend(item_errors)
            invalid_plans.append({"item_index": index, "errors": item_errors})
            continue
        if raw_item.get("planning_status") == "research_required":
            research_item, item_errors = _validate_research_required(
                raw_item,
                expected_case_id=expected_case_id,
                expected_problem_id=expected_problem_id,
                expected_option_id=expected_option_id,
                research_reference_ids=research_refs,
            )
            if item_errors:
                bound_errors = [f"planner_item:{index}:{error}" for error in item_errors]
                errors.extend(bound_errors)
                invalid_plans.append(
                    {"item_index": index, "candidate": dict(raw_item), "errors": bound_errors}
                )
            else:
                research_required.append(research_item)
                valid_keys.append(
                    f"planner_research_return:{expected_problem_id}:"
                    f"{_canonical_sha256(research_item)}"
                )
            continue
        plan, research_item, item_errors = _finalize_plan_candidate(
            raw_item,
            item_index=index,
            duplicate_plan_ids=duplicate_plan_ids,
            expected_case_id=expected_case_id,
            expected_problem_id=expected_problem_id,
            expected_option_id=expected_option_id,
            expected_revision=expected_revision,
            target_repo_root=target_repo_root,
            problem_record=problem_record,
            research_dossier=research_dossier,
            selection_decision=selection_decision,
        )
        if research_item is not None:
            research_required.append(research_item)
            valid_keys.append(
                f"planner_research_return:{expected_problem_id}:"
                f"{_canonical_sha256(research_item)}"
            )
        elif plan is not None:
            valid_plans.append(plan)
            valid_keys.append(
                "change_plan_revision:" + str(plan.get("plan_revision_id") or "missing")
            )
        else:
            bound_errors = [f"planner_item:{index}:{error}" for error in item_errors]
            errors.extend(bound_errors)
            invalid_plans.append(
                {"item_index": index, "candidate": dict(raw_item), "errors": bound_errors}
            )
    if valid_plans and research_required:
        errors.append("planner_response_mixes_ready_plans_with_research_return")
    return (
        {
            "valid_plans": valid_plans,
            "research_required": research_required,
            "invalid_plans": invalid_plans,
        },
        list(dict.fromkeys(errors)),
        valid_keys,
    )


def _retained_partial_plans(run: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Keep the latest verified revision of each plan unit across a paused conversation."""

    by_model_plan_id: dict[str, dict[str, Any]] = {}
    payloads = run.get("attempt_payloads")
    for payload in payloads if isinstance(payloads, list) else []:
        if not isinstance(payload, Mapping):
            continue
        plans = payload.get("valid_plans")
        for plan in plans if isinstance(plans, list) else []:
            if not isinstance(plan, Mapping):
                continue
            plan_id = _nonempty(plan.get("change_plan_id"))
            revision_id = _nonempty(plan.get("plan_revision_id"))
            key = plan_id or revision_id
            if key is not None:
                by_model_plan_id[key] = dict(plan)
    return list(by_model_plan_id.values())


def run_stage6_live_case(
    *,
    problem_id: str,
    case_id: str,
    selected_option_id: str,
    index: int,
    planner_prompt: str,
    stage_artifacts_dir: Path,
    target_repo_root: Path,
    repo_revision: str,
    problem_record: Mapping[str, Any],
    research_dossier: Mapping[str, Any],
    selection_decision: Mapping[str, Any],
    agent: str,
    model: str | None,
    cfg: RunnerConfig,
    initial_resume_session_id: str | None = None,
    author_cost_seconds: float | None = None,
    external_feedback: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Run one exact planner conversation and return only complete ready plans."""

    tag = f"implementation_planning_{index:03d}"
    effective_prompt = planner_prompt
    if external_feedback is not None:
        effective_prompt = (
            "INDEPENDENT QUALIFICATION CORRECTION\n\n"
            "Continue this exact planner conversation and return the complete Stage 6 "
            "response. Preserve valid grounded work while correcting the bound independent "
            "finding. This correction is not self-certifying; a separate adjudicator will "
            "evaluate the revised result.\n\nBOUND FEEDBACK:\n"
            + json.dumps(external_feedback, ensure_ascii=False, indent=2, sort_keys=True)
            + "\n\nCURRENT FULL PLANNING INPUT AND RESPONSE CONTRACT:\n"
            + planner_prompt
        )
    run = _run_role_conversation(
        role="planner",
        invocation_stage="implementation_planning",
        initial_prompt=effective_prompt,
        author_origin_prompt_sha256=sha256(planner_prompt.encode("utf-8")).hexdigest(),
        out_dir=stage_artifacts_dir / tag,
        base_tag=tag,
        workspace_dir=target_repo_root,
        repo_revision=repo_revision,
        agent=agent,
        model=model,
        cfg=cfg,
        initial_resume_session_id=initial_resume_session_id,
        author_cost_seconds=author_cost_seconds,
        validator=lambda response: _planning_response_projection(
            response,
            expected_case_id=case_id,
            expected_problem_id=problem_id,
            expected_option_id=selected_option_id,
            expected_revision=repo_revision,
            target_repo_root=target_repo_root,
            problem_record=problem_record,
            research_dossier=research_dossier,
            selection_decision=selection_decision,
        ),
    )
    payload = run.get("payload") if isinstance(run.get("payload"), Mapping) else {}
    plans = [
        dict(item)
        for item in payload.get("valid_plans", [])
        if isinstance(item, Mapping)
    ]
    research_required = [
        dict(item)
        for item in payload.get("research_required", [])
        if isinstance(item, Mapping)
    ]
    partial_plans = _retained_partial_plans(run)
    accepted = bool(run.get("accepted"))
    if accepted and plans and not research_required:
        planning_status = "planned"
        emitted_plans = plans
    elif accepted and research_required and not plans:
        planning_status = "research_required"
        emitted_plans = []
    else:
        planning_status = str(run.get("status") or "repairable_paused:planner_incomplete")
        emitted_plans = []
    return {
        "status": planning_status,
        "plans": emitted_plans,
        "partial_valid_plans": partial_plans if not emitted_plans else [],
        "research_required": research_required,
        "role_run": _role_run_record(run),
        "metrics": {
            **dict(run.get("metrics") or {}),
            "emitted_plan_count": len(emitted_plans),
            "retained_partial_plan_count": len(partial_plans if not emitted_plans else []),
            "research_required_count": len(research_required),
            "runtime_ground_truth": "unavailable",
        },
    }


__all__ = ["run_stage6_live_case"]
