"""Tests for backlog_core.stage_contracts.

These tests assert observable behavior for each stage parser:
- Problem records must not carry solution fields.
- Research dossiers must reject implementation_performed=true.
- Solution options must not carry selected_solution.
- Each parser injects the canonical status field when absent.
- build_stage_document produces a consistent envelope.
"""

from __future__ import annotations

import json
import pytest

from backlog_core.stage_contracts import (
    _extract_json,
    build_stage_document,
    parse_change_plan_list,
    parse_priority_decision_list,
    parse_problem_record_list,
    parse_research_dossier_list,
    parse_selection_decisions,
    parse_solution_option_sets,
)


# ---------------------------------------------------------------------------
# _extract_json helpers
# ---------------------------------------------------------------------------


def test_extract_json_from_plain_text() -> None:
    data = [{"a": 1}]
    assert _extract_json(json.dumps(data)) == data


def test_extract_json_from_fenced_block() -> None:
    data = [{"a": 1}]
    text = f"```json\n{json.dumps(data)}\n```"
    assert _extract_json(text) == data


def test_extract_json_from_surrounding_prose() -> None:
    data = [{"x": 2}]
    text = f"Here is the output:\n{json.dumps(data)}\nDone."
    assert _extract_json(text) == data


def test_extract_json_raises_on_empty() -> None:
    with pytest.raises(ValueError, match="empty_response"):
        _extract_json("")


def test_extract_json_raises_on_no_json() -> None:
    with pytest.raises(ValueError, match="no_valid_json"):
        _extract_json("this is just plain text with no JSON")


# ---------------------------------------------------------------------------
# parse_problem_record_list
# ---------------------------------------------------------------------------


def _valid_problem_record(**overrides: object) -> dict:
    base = {
        "problem_id": "problem:test-issue",
        "title": "Test issue",
        "problem": "Something is broken",
        "user_impact": "Users cannot proceed",
        "severity": "high",
        "confidence": 0.8,
        "evidence_atom_ids": ["run/20260101/codex/0:confusion_point:1"],
        "evidence_summary": "Confusion point observed",
        "problem_status": "identified",
    }
    base.update(overrides)
    return base


def test_parse_problem_record_list_accepts_valid_record() -> None:
    records = [_valid_problem_record()]
    text = json.dumps(records)
    result, warnings = parse_problem_record_list(text)
    assert len(result) == 1
    assert warnings == []
    assert result[0]["problem_id"] == "problem:test-issue"
    assert result[0]["problem_status"] == "identified"


def test_parse_problem_record_list_rejects_proposed_fix() -> None:
    """Problem records must not contain proposed_fix."""
    records = [_valid_problem_record(proposed_fix="add a quickstart")]
    text = json.dumps(records)
    result, warnings = parse_problem_record_list(text)
    assert len(result) == 1
    assert any("proposed_fix" in w for w in warnings)
    assert "_parse_warning" in result[0]


def test_parse_problem_record_list_rejects_selected_solution() -> None:
    records = [_valid_problem_record(selected_solution="most_direct")]
    text = json.dumps(records)
    result, warnings = parse_problem_record_list(text)
    assert any("selected_solution" in w for w in warnings)
    assert "_parse_warning" in result[0]


def test_parse_problem_record_list_rejects_family_id() -> None:
    records = [_valid_problem_record(family_id="most_robust")]
    text = json.dumps(records)
    result, warnings = parse_problem_record_list(text)
    assert any("family_id" in w for w in warnings)


def test_parse_problem_record_list_rejects_implementation_steps() -> None:
    records = [_valid_problem_record(implementation_steps=["step 1"])]
    text = json.dumps(records)
    result, warnings = parse_problem_record_list(text)
    assert any("implementation_steps" in w for w in warnings)


def test_parse_problem_record_list_warns_missing_required_fields() -> None:
    # Missing problem, user_impact, evidence_summary, etc.
    minimal = {"problem_id": "problem:minimal"}
    text = json.dumps([minimal])
    result, warnings = parse_problem_record_list(text)
    assert len(result) == 1
    assert len(warnings) > 0
    # Should flag missing required fields.
    assert any("problem_record_missing_required_field" in w for w in warnings)


def test_parse_problem_record_list_injects_status() -> None:
    """Records without problem_status get 'identified' injected."""
    record = _valid_problem_record()
    del record["problem_status"]
    text = json.dumps([record])
    result, warnings = parse_problem_record_list(text)
    assert result[0]["problem_status"] == "identified"


def test_parse_problem_record_list_warns_empty_evidence() -> None:
    records = [_valid_problem_record(evidence_atom_ids=[])]
    text = json.dumps(records)
    result, warnings = parse_problem_record_list(text)
    assert any("empty_evidence_atom_ids" in w for w in warnings)


def test_parse_problem_record_list_handles_multiple_records() -> None:
    records = [
        _valid_problem_record(problem_id="problem:a"),
        _valid_problem_record(problem_id="problem:b"),
    ]
    text = json.dumps(records)
    result, warnings = parse_problem_record_list(text)
    assert len(result) == 2
    assert warnings == []


def test_parse_problem_record_list_handles_model_wrapping() -> None:
    """Some models wrap the list in an object."""
    records = [_valid_problem_record()]
    wrapped = {"problem_records": records}
    text = json.dumps(wrapped)
    result, warnings = parse_problem_record_list(text)
    assert len(result) == 1


# ---------------------------------------------------------------------------
# parse_priority_decision_list
# ---------------------------------------------------------------------------


def _valid_priority_decision(**overrides: object) -> dict:
    base = {
        "problem_id": "problem:test-issue",
        "priority_bucket": "p1",
        "selected_for_research": True,
        "priority_rationale": "Recurring high-severity issue",
        "evidence_atom_ids_used": ["run/20260101/codex/0:confusion_point:1"],
        "priority_status": "prioritized",
    }
    base.update(overrides)
    return base


def test_parse_priority_decision_list_accepts_valid() -> None:
    text = json.dumps([_valid_priority_decision()])
    result, warnings = parse_priority_decision_list(text)
    assert len(result) == 1
    assert warnings == []
    assert result[0]["priority_status"] == "prioritized"


def test_parse_priority_decision_list_accepts_single_object() -> None:
    """Some models return a single decision object instead of a JSON list."""
    text = json.dumps(_valid_priority_decision())
    result, warnings = parse_priority_decision_list(text)
    assert len(result) == 1
    assert warnings == []
    assert result[0]["problem_id"] == "problem:test-issue"


def test_parse_priority_decision_list_injects_status() -> None:
    d = _valid_priority_decision()
    del d["priority_status"]
    text = json.dumps([d])
    result, _ = parse_priority_decision_list(text)
    assert result[0]["priority_status"] == "prioritized"


def test_parse_priority_decision_list_warns_invalid_bucket() -> None:
    d = _valid_priority_decision(priority_bucket="urgent")
    text = json.dumps([d])
    _, warnings = parse_priority_decision_list(text)
    assert any("invalid_bucket" in w for w in warnings)


def test_parse_priority_decision_list_warns_non_bool_selected_for_research() -> None:
    d = _valid_priority_decision(selected_for_research="yes")
    text = json.dumps([d])
    _, warnings = parse_priority_decision_list(text)
    assert any("selected_for_research" in w for w in warnings)


# ---------------------------------------------------------------------------
# parse_research_dossier_list
# ---------------------------------------------------------------------------


def _valid_dossier(**overrides: object) -> dict:
    base = {
        "problem_id": "problem:test-issue",
        "reproduction_status": "reproduced",
        "writes_used": True,
        "writes_purpose": ["failing_test"],
        "implementation_performed": False,
        "diff_classification": "allowed_research_edits",
        "root_cause_hypotheses": ["Missing validation in parser"],
        "broader_class_assessment": "isolated_instance",
        "unknowns": [],
        "research_status": "researched",
    }
    base.update(overrides)
    return base


def test_parse_research_dossier_list_accepts_valid() -> None:
    text = json.dumps([_valid_dossier()])
    result, warnings = parse_research_dossier_list(text)
    assert len(result) == 1
    assert warnings == []


def test_parse_research_dossier_list_raises_on_implementation_performed_true() -> None:
    """implementation_performed=true must raise ValueError, not just warn."""
    text = json.dumps([_valid_dossier(implementation_performed=True)])
    with pytest.raises(ValueError, match="implementation_performed_true"):
        parse_research_dossier_list(text)


def test_parse_research_dossier_list_warns_invalid_reproduction_status() -> None:
    text = json.dumps([_valid_dossier(reproduction_status="fixed")])
    _, warnings = parse_research_dossier_list(text)
    assert any("reproduction_status" in w for w in warnings)


def test_parse_research_dossier_list_injects_status() -> None:
    d = _valid_dossier()
    del d["research_status"]
    text = json.dumps([d])
    result, _ = parse_research_dossier_list(text)
    assert result[0]["research_status"] == "researched"


# ---------------------------------------------------------------------------
# parse_solution_option_sets
# ---------------------------------------------------------------------------


def _valid_option(**overrides: object) -> dict:
    base = {
        "option_id": "option:test:most_direct",
        "problem_id": "problem:test-issue",
        "family_id": "most_direct",
        "summary": "Add validation",
        "tradeoffs": "Minimal change",
        "recurrence_prevention": "Prevents this instance",
        "change_surface_hypothesis": "docs_change",
        "test_implications": "Add unit test",
        "rationale": "Grounded in research dossier",
        "option_status": "optioned",
    }
    base.update(overrides)
    return base


def test_parse_solution_option_sets_accepts_valid() -> None:
    text = json.dumps([_valid_option()])
    result, warnings = parse_solution_option_sets(text)
    assert len(result) == 1
    assert warnings == []


def test_parse_solution_option_sets_rejects_selected_solution() -> None:
    text = json.dumps([_valid_option(selected_solution="most_direct")])
    _, warnings = parse_solution_option_sets(text)
    assert any("selected_solution" in w for w in warnings)


def test_parse_solution_option_sets_warns_unknown_family_id() -> None:
    text = json.dumps([_valid_option(family_id="invented_family")])
    _, warnings = parse_solution_option_sets(
        text, known_family_ids={"most_direct", "most_robust", "most_comprehensive"}
    )
    assert any("unknown_family_id" in w for w in warnings)


def test_parse_solution_option_sets_accepts_all_three_families() -> None:
    options = [
        _valid_option(family_id="most_direct", option_id="opt:a"),
        _valid_option(family_id="most_robust", option_id="opt:b"),
        _valid_option(family_id="most_comprehensive", option_id="opt:c"),
    ]
    text = json.dumps(options)
    result, warnings = parse_solution_option_sets(
        text, known_family_ids={"most_direct", "most_robust", "most_comprehensive"}
    )
    assert len(result) == 3
    assert warnings == []


# ---------------------------------------------------------------------------
# parse_selection_decisions
# ---------------------------------------------------------------------------


def _valid_selection(**overrides: object) -> dict:
    base = {
        "problem_id": "problem:test-issue",
        "selected_option_id": "option:test:most_direct",
        "selected_family_id": "most_direct",
        "selection_rationale": "Best fit for repo style",
        "repo_intent_alignment": "Matches composable-command philosophy",
        "why_other_options_were_not_selected": "Most robust overkill for this case",
        "needs_ux_review": False,
        "selection_status": "selected",
    }
    base.update(overrides)
    return base


def test_parse_selection_decisions_accepts_valid() -> None:
    text = json.dumps([_valid_selection()])
    result, warnings = parse_selection_decisions(text)
    assert len(result) == 1
    assert warnings == []


def test_parse_selection_decisions_injects_status() -> None:
    d = _valid_selection()
    del d["selection_status"]
    text = json.dumps([d])
    result, _ = parse_selection_decisions(text)
    assert result[0]["selection_status"] == "selected"


# ---------------------------------------------------------------------------
# parse_change_plan_list
# ---------------------------------------------------------------------------


def _valid_change_plan(**overrides: object) -> dict:
    base = {
        "change_plan_id": "plan:test-issue:1",
        "problem_id": "problem:test-issue",
        "selected_option_id": "option:test:most_direct",
        "title": "Add quickstart docs",
        "problem": "No quickstart section exists",
        "user_impact": "Onboarding blocked",
        "proposed_fix": "Add quickstart section to README",
        "implementation_steps": ["Write quickstart section", "Add to README"],
        "verification_steps": ["Run smoke test"],
        "success_criteria": ["User can complete first run in <5 minutes"],
        "rollback_notes": "Revert README change",
        "suggested_owner": "docs",
        "change_plan_status": "planned",
        "related_change_plan_ids": [],
    }
    base.update(overrides)
    return base


def test_parse_change_plan_list_accepts_valid() -> None:
    text = json.dumps([_valid_change_plan()])
    result, warnings = parse_change_plan_list(text)
    assert len(result) == 1
    assert warnings == []


def test_parse_change_plan_list_warns_empty_implementation_steps() -> None:
    text = json.dumps([_valid_change_plan(implementation_steps=[])])
    _, warnings = parse_change_plan_list(text)
    assert any("empty_implementation_steps" in w for w in warnings)


def test_parse_change_plan_list_injects_status() -> None:
    d = _valid_change_plan()
    del d["change_plan_status"]
    text = json.dumps([d])
    result, _ = parse_change_plan_list(text)
    assert result[0]["change_plan_status"] == "planned"


# ---------------------------------------------------------------------------
# build_stage_document
# ---------------------------------------------------------------------------


def test_build_stage_document_structure() -> None:
    items = [_valid_problem_record()]
    doc = build_stage_document(
        "problem_mining",
        items,
        input_meta={"atom_count": 5},
        artifacts={"problem_records_json": "/tmp/foo.json"},
    )
    assert doc["stage"] == "problem_mining"
    assert doc["item_count"] == 1
    assert doc["warning_count"] == 0
    assert doc["warnings"] == []
    assert doc["input_meta"]["atom_count"] == 5
    assert doc["artifacts"]["problem_records_json"] == "/tmp/foo.json"
    assert len(doc["items"]) == 1
    assert "generated_at" in doc


def test_build_stage_document_counts_warnings() -> None:
    items = [
        _valid_problem_record(proposed_fix="fix"),
    ]
    # Inject a parse warning to simulate a failed item.
    items[0]["_parse_warning"] = "some warning"
    doc = build_stage_document("problem_mining", items, input_meta={})
    assert doc["warning_count"] == 1
    assert len(doc["warnings"]) == 1
