from __future__ import annotations

from typing import Any

import pytest

from backlog_core.backlog_ticket_assembly import assemble_backlog_tickets


def _problem_record(pid: str, *, title: str = "T") -> dict[str, Any]:
    return {
        "problem_id": pid,
        "title": title,
        "problem": "P",
        "user_impact": "U",
        "severity": "medium",
        "confidence": 0.5,
        "evidence_atom_ids": ["a1", "a2"],
        "evidence_summary": "E",
    }


def test_assemble_backlog_tickets_splits_by_change_plan() -> None:
    problem_records = [
        _problem_record("problem:one", title="One"),
        _problem_record("problem:two", title="Two"),
    ]
    priority_decisions = [
        {
            "problem_id": "problem:one",
            "priority_bucket": "p1",
            "selected_for_research": True,
            "priority_rationale": "R",
        }
    ]
    research_dossiers = [
        {
            "problem_id": "problem:one",
            "reproduction_status": "partial",
            "writes_used": False,
            "writes_purpose": [],
            "implementation_performed": False,
            "root_cause_hypotheses": ["H"],
            "broader_class_assessment": "unknown",
            "unknowns": ["Need more evidence"],
        }
    ]
    solution_option_sets = [
        {
            "option_id": "option:one:direct",
            "problem_id": "problem:one",
            "family_id": "most_direct",
            "summary": "S",
            "tradeoffs": "T",
            "recurrence_prevention": "R",
            "change_surface_hypothesis": "docs",
            "test_implications": "TI",
            "rationale": "RA",
        }
    ]
    selection_decisions = [
        {
            "problem_id": "problem:one",
            "selected_option_id": "option:one:direct",
            "selected_family_id": "most_direct",
            "selection_rationale": "SR",
            "repo_intent_alignment": "RIA",
            "why_other_options_were_not_selected": "WO",
            "needs_ux_review": False,
            "change_surface": {"user_visible": True, "kinds": ["docs_change"], "notes": "n"},
            "component": "docs",
            "intent_risk": "low",
            "labeler_confidence": 0.7,
            "breadth": {"runs": 2},
        }
    ]
    change_plans = [
        {
            "change_plan_id": "plan:one:1",
            "problem_id": "problem:one",
            "selected_option_id": "option:one:direct",
            "title": "Plan A",
            "problem": "P",
            "user_impact": "U",
            "proposed_fix": "Fix A",
            "implementation_steps": ["Do A"],
            "verification_steps": ["Check A"],
            "success_criteria": ["Done A"],
            "rollback_notes": "Rollback A",
            "suggested_owner": "docs",
            "change_plan_status": "planned",
            "related_change_plan_ids": ["plan:one:2"],
        },
        {
            "change_plan_id": "plan:one:2",
            "problem_id": "problem:one",
            "selected_option_id": "option:one:direct",
            "title": "Plan B",
            "problem": "P",
            "user_impact": "U",
            "proposed_fix": "Fix B",
            "implementation_steps": ["Do B"],
            "verification_steps": ["Check B"],
            "success_criteria": ["Done B"],
            "rollback_notes": "Rollback B",
            "suggested_owner": "docs",
            "change_plan_status": "planned",
            "related_change_plan_ids": ["plan:one:1"],
        },
    ]

    tickets = assemble_backlog_tickets(
        problem_records=problem_records,
        priority_decisions=priority_decisions,
        research_dossiers=research_dossiers,
        solution_option_sets=solution_option_sets,
        selection_decisions=selection_decisions,
        change_plans=change_plans,
    )

    # Two plans -> two tickets, plus a triage ticket for the untouched problem record.
    assert len(tickets) == 3

    planned = [t for t in tickets if t.get("change_plan_id") in {"plan:one:1", "plan:one:2"}]
    assert len(planned) == 2
    for ticket in planned:
        assert ticket["stage"] == "ready_for_ticket"
        assert ticket["selected_option_id"] == "option:one:direct"
        assert ticket["suggested_owner"] == "docs"
        assert isinstance(ticket.get("problem_record"), dict)
        assert isinstance(ticket.get("selected_solution"), dict)
        assert isinstance(ticket.get("change_plan"), dict)

        # Research unknowns should appear as investigation prompts for planned tickets.
        assert any(
            isinstance(step, str) and "Resolve unknown:" in step
            for step in ticket.get("investigation_steps", [])
        )

    triage = [t for t in tickets if t.get("problem_record", {}).get("problem_id") == "problem:two"]
    assert len(triage) == 1
    assert triage[0]["stage"] == "triage"


def test_assemble_backlog_tickets_requires_selection_for_plans() -> None:
    with pytest.raises(ValueError) as exc:
        assemble_backlog_tickets(
            problem_records=[_problem_record("problem:one")],
            priority_decisions=[],
            research_dossiers=[],
            solution_option_sets=[],
            selection_decisions=[],
            change_plans=[
                {
                    "change_plan_id": "plan:one:1",
                    "problem_id": "problem:one",
                    "selected_option_id": "option:one:direct",
                    "title": "Plan A",
                    "problem": "P",
                    "user_impact": "U",
                    "proposed_fix": "Fix A",
                    "implementation_steps": ["Do A"],
                    "verification_steps": ["Check A"],
                    "success_criteria": ["Done A"],
                    "rollback_notes": "Rollback A",
                    "suggested_owner": "docs",
                    "change_plan_status": "planned",
                    "related_change_plan_ids": [],
                }
            ],
        )
    assert "missing selection decisions for change plans" in str(exc.value)

