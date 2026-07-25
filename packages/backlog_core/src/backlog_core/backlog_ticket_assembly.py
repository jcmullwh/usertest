"""Stage-backed backlog ticket assembly (Milestone 6).

The six-stage backlog pipeline produces stage artifacts that are easy to inspect
in isolation:

1) problem_mining            -> problem records
2) problem_prioritization    -> priority decisions
3) repro_research            -> research dossiers
4) solution_optioning        -> solution option sets (flattened options)
5) solution_selection        -> selection decisions (+ post-selection labeler output)
6) implementation_planning   -> change plans

This module is the single authoritative place that maps those artifacts into the
final export-compatible ticket schema used by ``backlog_core.backlog`` and the
``usertest-backlog reports export-tickets`` flow.

Notes
-----
- The assembled ticket list intentionally mirrors important legacy top-level
  fields (title/problem/user_impact/etc.) while also embedding nested stage-backed
  objects (problem_record/priority/research/solution_options/selected_solution/
  change_plan).
- If a selected solution yields multiple change plans, each change plan becomes
  its own final ticket (implementation unit). Those tickets share the same
  upstream nested artifacts and are linked via ``related_change_plan_ids``.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Sequence
from typing import Any

from backlog_core.stage_contracts import (
    assess_research_readiness,
    research_actionability_assessment,
)
from backlog_core.ticket_readiness import assess_ticket_readiness

_LOG = logging.getLogger(__name__)


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


def _index_by_problem_id(
    items: Sequence[dict[str, Any]],
    *,
    kind: str,
) -> dict[str, dict[str, Any]]:
    indexed: dict[str, dict[str, Any]] = {}
    duplicates: list[str] = []
    for item in items:
        pid = _coerce_string(item.get("problem_id"))
        if pid is None:
            continue
        if pid in indexed:
            duplicates.append(pid)
            continue
        indexed[pid] = dict(item)
    if duplicates:
        _LOG.warning(
            "%s: duplicate problem_id values (keeping first): %s",
            kind,
            sorted(duplicates),
        )
    return indexed


def _group_by_problem_id(
    items: Sequence[dict[str, Any]],
    *,
    kind: str,
) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    missing = 0
    for item in items:
        pid = _coerce_string(item.get("problem_id"))
        if pid is None:
            missing += 1
            continue
        grouped[pid].append(dict(item))
    if missing:
        _LOG.warning("%s: %d item(s) missing problem_id", kind, missing)
    return dict(grouped)


def _ticket_base_from_problem_record(record: dict[str, Any]) -> dict[str, Any]:
    """Return the stable top-level ticket fields derived from a stage-1 problem record."""
    ticket: dict[str, Any] = {}
    for key in (
        "case_id",
        "canonical_problem_id",
        "case_member_problem_ids",
        "same_cause_group_id",
        "title",
        "problem",
        "user_impact",
        "severity",
        "confidence",
        "evidence_atom_ids",
        "evidence_summary",
    ):
        if key in record:
            ticket[key] = record.get(key)
    return ticket


def _apply_observed_problem_refinement(
    ticket: dict[str, Any],
    research: dict[str, Any] | None,
) -> None:
    """Promote Stage 3's verified symptom/impact account without rewriting Stage 1."""

    if not isinstance(research, dict):
        return
    raw = research.get("observed_problem_refinement")
    if not isinstance(raw, dict):
        return
    refinement = dict(raw)
    for field in ("problem", "user_impact", "evidence_summary"):
        value = _coerce_string(refinement.get(field))
        if value is not None:
            ticket[field] = value
    ticket["observed_problem_refinement"] = refinement


def _material_unknown_investigation_steps(research: dict[str, Any] | None) -> list[str]:
    """Render unresolved research-proof decisions without discarding their evidence needs."""
    if not isinstance(research, dict):
        return []
    steps: list[str] = []
    material_unknowns = research.get("material_unknowns")
    if isinstance(material_unknowns, list):
        for item in material_unknowns[:8]:
            if not isinstance(item, dict):
                continue
            # The proof retains useful uncertainty even after it is shown not to
            # block the implementation decision.  Preserve that context below in
            # the full research section, but do not turn it back into mandatory
            # investigation work for the implementer.  Legacy entries without an
            # explicit flag remain material for compatibility with the readiness
            # contract.
            if item.get("material") is False:
                continue
            unknown = _coerce_string(item.get("unknown"))
            evidence_needed = _coerce_string(item.get("evidence_needed"))
            if unknown is None:
                continue
            suffix = f" Evidence needed: {evidence_needed}" if evidence_needed else ""
            steps.append(f"Resolve material unknown: {unknown}.{suffix}".strip())
    if steps:
        return steps

    # Historical compatibility: old dossiers used an unstructured `unknowns` list.
    return [
        f"Resolve unknown: {unknown}"
        for unknown in _coerce_string_list(research.get("unknowns"))[:8]
    ]


def _research_contract_view(research: dict[str, Any] | None) -> dict[str, Any] | None:
    """Remove only the runner-owned case-lineage envelope from persisted research."""

    if not isinstance(research, dict):
        return None
    return {
        key: value
        for key, value in research.items()
        if key not in {"canonical_problem_id", "case_member_problem_ids"}
    }


def _research_stage_and_evidence(
    research: dict[str, Any] | None,
    *,
    needs_ux_review: bool,
) -> tuple[str, dict[str, Any]]:
    """Return the safe ticket stage and the complete research-readiness assessment."""
    contract_research = _research_contract_view(research)
    ready, reasons = assess_research_readiness(contract_research)
    evidence = {
        "ready": ready,
        "reasons": reasons,
        "research_schema_version": (
            research.get("research_schema_version") if isinstance(research, dict) else None
        ),
    }
    if not ready:
        status = research.get("research_status") if isinstance(research, dict) else None
        return ("blocked" if status == "blocked" else "research_required"), evidence
    if needs_ux_review:
        return "research_required", evidence
    return "ready_for_ticket", evidence


def _research_is_terminal_no_change(research: dict[str, Any] | None) -> bool:
    """Return whether verified research established that no product change is due."""

    contract_research = _research_contract_view(research)
    ready, _reasons = assess_research_readiness(contract_research)
    if not ready or contract_research is None:
        return False
    disposition = research_actionability_assessment(contract_research).get("disposition")
    return disposition in {"already_addressed", "non_actionable"}


def assemble_backlog_tickets(
    *,
    problem_records: Sequence[dict[str, Any]],
    priority_decisions: Sequence[dict[str, Any]],
    research_dossiers: Sequence[dict[str, Any]],
    solution_option_sets: Sequence[dict[str, Any]],
    selection_decisions: Sequence[dict[str, Any]],
    change_plans: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Assemble final backlog tickets from stage artifacts.

    Parameters
    ----------
    problem_records:
        Stage-1 problem records.
    priority_decisions:
        Stage-2 prioritization decisions.
    research_dossiers:
        Stage-3 repro+research dossiers.
    solution_option_sets:
        Stage-4 option objects (flattened; one per family per problem).
    selection_decisions:
        Stage-5 selection decisions (should include post-selection labeler output).
    change_plans:
        Stage-6 change plans (may contain multiple plans per problem).

    Returns
    -------
    list[dict[str, Any]]
        Export-compatible ticket objects with nested stage-backed fields.

    Raises
    ------
    ValueError
        When change plans are present for unknown problems or without a selection decision.
    """

    records_by_id = _index_by_problem_id(problem_records, kind="problem_records")
    priority_by_id = _index_by_problem_id(priority_decisions, kind="priority_decisions")
    research_by_id = _index_by_problem_id(research_dossiers, kind="research_dossiers")
    selection_by_id = _index_by_problem_id(selection_decisions, kind="selection_decisions")
    options_by_problem = _group_by_problem_id(solution_option_sets, kind="solution_option_sets")
    plans_by_problem = _group_by_problem_id(change_plans, kind="change_plans")

    terminal_no_change_problem_ids = {
        pid
        for pid, research in research_by_id.items()
        if _research_is_terminal_no_change(research)
    }
    contradictory_downstream_problem_ids = sorted(
        pid
        for pid in terminal_no_change_problem_ids
        if plans_by_problem.get(pid)
        or options_by_problem.get(pid)
        or pid in selection_by_id
    )
    if contradictory_downstream_problem_ids:
        raise ValueError(
            "assemble_backlog_tickets: terminal no-change research has downstream "
            "implementation artifacts: " + ", ".join(contradictory_downstream_problem_ids)
        )

    plan_problem_ids = sorted(plans_by_problem)
    missing_for_plans: list[str] = []
    missing_selection_for_plans: list[str] = []
    for pid in plan_problem_ids:
        if pid not in records_by_id:
            missing_for_plans.append(pid)
        if pid not in selection_by_id:
            missing_selection_for_plans.append(pid)
    if missing_for_plans or missing_selection_for_plans:
        parts: list[str] = []
        if missing_for_plans:
            parts.append(
                "missing problem records for change plans: "
                + ", ".join(missing_for_plans)
            )
        if missing_selection_for_plans:
            parts.append(
                "missing selection decisions for change plans: "
                + ", ".join(missing_selection_for_plans)
            )
        raise ValueError("assemble_backlog_tickets: invalid stage chain: " + " | ".join(parts))

    tickets: list[dict[str, Any]] = []
    ticketed_problem_ids: set[str] = set()

    # 1) Primary path: each change plan is an implementation unit -> becomes a ticket.
    for pid in plan_problem_ids:
        record = records_by_id[pid]
        priority = priority_by_id.get(pid)
        research = research_by_id.get(pid)
        options = options_by_problem.get(pid, [])
        selected_solution = selection_by_id.get(pid)
        assert selected_solution is not None

        plans = plans_by_problem.get(pid, [])
        plans_sorted = sorted(plans, key=lambda p: _coerce_string(p.get("change_plan_id")) or "")
        for plan in plans_sorted:
            ticket: dict[str, Any] = {}
            ticket.update(_ticket_base_from_problem_record(record))

            # Top-level mapping from stage 6.
            for key in (
                "change_plan_id",
                "plan_revision_id",
                "plan_revision_source",
                "case_id",
                "selected_option_id",
                "title",
                "problem",
                "user_impact",
                "proposed_fix",
                "implementation_steps",
                "verification_steps",
                "success_criteria",
                "rollback_notes",
                "suggested_owner",
                "repo_revision",
                "change_targets",
                "verification_commands",
                "outcome_verification_roles",
                "before_after_reproduction",
                "compatibility_and_failure_modes",
                "causal_coverage",
                "requires_live_verification",
                "live_verification_rationale",
            ):
                if key in plan:
                    ticket[key] = plan.get(key)

            _apply_observed_problem_refinement(ticket, research)

            # Carry labeler output and other stage-5 selection fields.
            for key in (
                "selected_family_id",
                "selection_rationale",
                "repo_intent_alignment",
                "why_other_options_were_not_selected",
                "needs_ux_review",
                "change_surface",
                "component",
                "intent_risk",
                "labeler_confidence",
                "evidence_atom_ids_used",
                "breadth",
                "problem_breadth",
                "breadth_profile",
                "decision_basis",
                "review_domain",
                "batch_breadth",
                "structurally_constant_batch_dimensions",
            ):
                if key in selected_solution:
                    ticket[key] = selected_solution.get(key)

            # Preserve the research assessment separately for diagnostics.
            ticket["investigation_steps"] = _material_unknown_investigation_steps(research)
            needs_ux = bool(ticket.get("needs_ux_review") is True)
            _research_stage, ticket["research_readiness"] = _research_stage_and_evidence(
                research,
                needs_ux_review=needs_ux,
            )

            # Nested stage artifacts.
            ticket["problem_record"] = record
            if isinstance(priority, dict):
                ticket["priority"] = priority
            if isinstance(research, dict):
                ticket["research"] = research
            if options:
                ticket["solution_options"] = options
            ticket["selected_solution"] = selected_solution
            ticket["change_plan"] = plan

            ready, readiness_reasons = assess_ticket_readiness(ticket)
            ticket["ticket_readiness"] = {
                "ready": ready,
                "reasons": readiness_reasons,
            }
            if ready and not needs_ux:
                ticket["stage"] = "ready_for_ticket"
            elif isinstance(research, dict) and research.get("research_status") == "blocked":
                ticket["stage"] = "blocked"
            else:
                ticket["stage"] = "research_required"

            tickets.append(ticket)
        ticketed_problem_ids.add(pid)

    # 2) Secondary path: problems without a change plan remain as triage/research tickets.
    for pid in sorted(records_by_id):
        if pid in ticketed_problem_ids:
            continue
        # A ready Stage-3 proof can establish that the reported problem was already
        # addressed or is genuinely non-actionable. Stage 4 records that terminal
        # disposition without options. Re-emitting it as a triage/research ticket here
        # would undo the evidence-backed result merely because no plan exists.
        if pid in terminal_no_change_problem_ids:
            continue
        record = records_by_id[pid]
        priority = priority_by_id.get(pid)
        research = research_by_id.get(pid)
        options = options_by_problem.get(pid, [])
        selected_solution = selection_by_id.get(pid)
        plans = plans_by_problem.get(pid, [])
        if plans:
            raise AssertionError(
                "assemble_backlog_tickets: unexpected plans for problem already considered"
            )

        ticket: dict[str, Any] = {}
        ticket.update(_ticket_base_from_problem_record(record))
        _apply_observed_problem_refinement(ticket, research)

        if isinstance(selected_solution, dict):
            for key in (
                "selected_option_id",
                "selected_family_id",
                "needs_ux_review",
                "change_surface",
                "component",
                "intent_risk",
                "labeler_confidence",
                "breadth",
                "problem_breadth",
                "breadth_profile",
                "decision_basis",
                "review_domain",
                "batch_breadth",
                "structurally_constant_batch_dimensions",
            ):
                if key in selected_solution:
                    ticket[key] = selected_solution.get(key)

        # If stage 5 exists but stage 6 does not, prefer leaving `proposed_fix` unset
        # (stage 6 is the first stage to commit to an implementation plan).
        ticket["investigation_steps"] = _material_unknown_investigation_steps(research)
        ticket["success_criteria"] = []
        if isinstance(research, dict):
            research_stage, ticket["research_readiness"] = _research_stage_and_evidence(
                research,
                needs_ux_review=bool(ticket.get("needs_ux_review") is True),
            )
            ticket["stage"] = (
                "blocked" if research_stage == "blocked" else "research_required"
            )
        elif isinstance(selected_solution, dict) or options:
            ticket["stage"] = "research_required"
        else:
            ticket["stage"] = "triage"

        # Nested stage artifacts (when present).
        ticket["problem_record"] = record
        if isinstance(priority, dict):
            ticket["priority"] = priority
        if isinstance(research, dict):
            ticket["research"] = research
        if options:
            ticket["solution_options"] = options
        if isinstance(selected_solution, dict):
            ticket["selected_solution"] = selected_solution

        ready, readiness_reasons = assess_ticket_readiness(ticket)
        ticket["ticket_readiness"] = {
            "ready": ready,
            "reasons": readiness_reasons,
        }

        tickets.append(ticket)

    return tickets
