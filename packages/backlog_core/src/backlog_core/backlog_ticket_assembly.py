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
from typing import Any, Sequence

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
        _LOG.warning("%s: duplicate problem_id values (keeping first): %s", kind, sorted(duplicates))
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
            parts.append("missing problem records for change plans: " + ", ".join(missing_for_plans))
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
            ):
                if key in plan:
                    ticket[key] = plan.get(key)

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
            ):
                if key in selected_solution:
                    ticket[key] = selected_solution.get(key)

            # Default investigation_steps: if research exposes unknowns and we are not ready,
            # surface them as investigation prompts. Avoid inventing new steps.
            investigation_steps: list[str] = []
            if isinstance(research, dict):
                unknowns = _coerce_string_list(research.get("unknowns"))
                investigation_steps.extend([f"Resolve unknown: {u}" for u in unknowns[:8]])
            ticket["investigation_steps"] = investigation_steps

            # Stage gating: planned tickets are ready unless explicitly marked for UX review.
            needs_ux = bool(ticket.get("needs_ux_review") is True)
            ticket["stage"] = "research_required" if needs_ux else "ready_for_ticket"

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

            tickets.append(ticket)
        ticketed_problem_ids.add(pid)

    # 2) Secondary path: problems without a change plan remain as triage/research tickets.
    for pid in sorted(records_by_id):
        if pid in ticketed_problem_ids:
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
            ):
                if key in selected_solution:
                    ticket[key] = selected_solution.get(key)

        # If stage 5 exists but stage 6 does not, prefer leaving `proposed_fix` unset
        # (stage 6 is the first stage to commit to an implementation plan).
        ticket["investigation_steps"] = []
        ticket["success_criteria"] = []
        ticket["stage"] = "research_required" if isinstance(selected_solution, dict) else "triage"

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

        tickets.append(ticket)

    return tickets

