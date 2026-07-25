from __future__ import annotations

import json
from pathlib import Path

import pytest
from backlog_core.ticket_readiness import assess_ticket_readiness

from usertest_backlog.workflows.staged import _persist_downstream_case_lineage


def _problem() -> dict[str, object]:
    return {
        "problem_id": "problem:one",
        "case_id": "case:one",
        "canonical_problem_id": "problem:one",
        "case_member_problem_ids": ["problem:one", "problem:one-symptom"],
    }


def _priority(**overrides: object) -> dict[str, object]:
    decision: dict[str, object] = {
        "problem_id": "problem:one",
        "priority_bucket": "p1",
        "selected_for_research": True,
        "eligible_for_downstream": True,
        "priority_status": "prioritized",
        "priority_rationale": "The retained evidence warrants research.",
    }
    decision.update(overrides)
    return decision


def test_stage2_orchestration_attaches_server_owned_lineage_before_ticket_readiness(
    tmp_path: Path,
) -> None:
    model_contract = {"sentinel": "preserve-authenticated-stage-contract"}
    stage_doc = {
        "stage": "problem_prioritization",
        "items": [_priority()],
        "input_meta": {"model_invocation_contract": model_contract},
    }

    persisted, decisions = _persist_downstream_case_lineage(
        stage_doc=stage_doc,
        out_json=tmp_path / "prioritized.json",
        problem_cases=[_problem()],
    )

    assert decisions == [
        {
            **_priority(),
            "case_id": "case:one",
            "canonical_problem_id": "problem:one",
            "case_member_problem_ids": ["problem:one", "problem:one-symptom"],
        }
    ]
    assert persisted["input_meta"]["model_invocation_contract"] == model_contract
    assert persisted["input_meta"]["case_lineage_propagated"] is True
    assert persisted["input_meta"]["canonical_case_count"] == 1
    assert json.loads((tmp_path / "prioritized.json").read_text(encoding="utf-8")) == persisted

    _, readiness_reasons = assess_ticket_readiness(
        {
            "problem_record": _problem(),
            "priority": decisions[0],
        }
    )
    assert not [
        reason
        for reason in readiness_reasons
        if reason.startswith("priority_decision_")
    ]


def test_stage2_orchestration_rejects_model_minted_wrong_case_lineage(
    tmp_path: Path,
) -> None:
    stage_doc = {
        "stage": "problem_prioritization",
        "items": [_priority(case_id="case:invented")],
        "input_meta": {},
    }

    with pytest.raises(ValueError, match="case_id mismatch"):
        _persist_downstream_case_lineage(
            stage_doc=stage_doc,
            out_json=tmp_path / "prioritized.json",
            problem_cases=[_problem()],
        )
