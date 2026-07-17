from __future__ import annotations

import json
from pathlib import Path

import backlog_core.ticket_readiness as ticket_readiness
import pytest
from backlog_core import (
    SOURCE_EVIDENCE_PROJECTION_VERSION,
    assign_plan_revision_id,
    build_case_registry,
    build_stage_document,
    problem_case_records_from_registry,
    source_evidence_atom_projection,
    source_evidence_atom_sha256,
    update_case_registry_stage_lineage,
)
from backlog_miner.pipeline import verify_stage_model_invocation_contract

from usertest_backlog.workflows import (
    downstream_hydration,
    prioritization,
    research_hydration,
    solution_options,
    staged,
)


def _atom() -> dict:
    return {
        "atom_id": "atom:one",
        "source": "automated_test",
        "summary": "original evidence",
    }


def _dossier() -> dict:
    atom = _atom()
    return {
        "case_id": "case:one",
        "problem_id": "problem:one",
        "research_schema_version": 3,
        "repo_revision": "abc123",
        "research_method": "static_diagnosis",
        "reproduction_status": "not_reproduced",
        "research_status": "evidence_sufficient",
        "implementation_performed": False,
        "writes_used": False,
        "writes_purpose": ["none"],
        "broader_class_assessment": "isolated",
        "diff_classification": "no_changes",
        "artifact_refs": [{"kind": "source", "path": "evidence.txt"}],
        "experiments": [{"experiment_id": "experiment:one"}],
        "inspected_files": ["src/example.py"],
        "inspected_symbols": ["example.run"],
        "root_cause_hypotheses": [
            {
                "hypothesis_id": "hypothesis:one",
                "statement": "The failing path omits the required state transition.",
                "supporting_evidence": ["experiment:one"],
            }
        ],
        "root_cause_confidence": 0.9,
        "material_unknowns": [],
        "blocking_reasons": [],
        "evidence_boundaries": ["The live deployment was not inspected."],
        "evidence_assignment": {
            "status": "complete",
            "expected_atom_ids": ["atom:one"],
            "atom_receipts": [
                {
                    "atom_id": "atom:one",
                    "atom_sha256": source_evidence_atom_sha256(atom),
                    "atom_snapshot": source_evidence_atom_projection(atom),
                    "source_projection_version": SOURCE_EVIDENCE_PROJECTION_VERSION,
                }
            ],
        },
    }


def _stage_doc(
    path: Path,
    stage: str,
    items: list[dict],
    *,
    input_meta: dict | None = None,
) -> dict:
    artifact_name = {
        "repro_research": "research_json",
        "solution_optioning": "solution_options_json",
        "solution_selection": "solution_selection_json",
        "implementation_planning": "change_plans_json",
    }[stage]
    return build_stage_document(
        stage,
        items,
        input_meta=input_meta or {},
        artifacts={artifact_name: str(path.resolve())},
    )


def _persist_stage(
    registry: dict,
    path: Path,
    stage: str,
    items: list[dict],
    *,
    input_meta: dict | None = None,
) -> dict:
    document = _stage_doc(path, stage, items, input_meta=input_meta)
    registry = update_case_registry_stage_lineage(registry, stage_doc=document)
    path.write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return registry


def _retained_chain_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[dict, dict[str, Path], dict]:
    problem = {
        "case_id": "case:one",
        "problem_id": "problem:one",
        "title": "One causal problem",
        "evidence_atom_ids": ["atom:one"],
        "source_evidence_atom_ids": ["atom:one"],
    }
    option = {
        "case_id": "case:one",
        "problem_id": "problem:one",
        "option_id": "option:one",
        "summary": "Change the verified transition at its owning control point.",
    }
    selection = {
        "case_id": "case:one",
        "problem_id": "problem:one",
        "selected_option_id": "option:one",
        "selection_rationale": "It covers the verified mechanism.",
    }
    plan = assign_plan_revision_id(
        {
            "case_id": "case:one",
            "problem_id": "problem:one",
            "change_plan_id": "plan:one",
            "selected_option_id": "option:one",
            "change_plan_status": "planned",
            "proposed_fix": "Update the owning transition and replay the origin scenario.",
        }
    )
    paths = {
        "research": tmp_path / "research.json",
        "options": tmp_path / "options.json",
        "selection": tmp_path / "selection.json",
        "plans": tmp_path / "plans.json",
    }
    registry = build_case_registry([problem], supporting_atoms=[_atom()])
    registry = _persist_stage(
        registry,
        paths["research"],
        "repro_research",
        [_dossier()],
    )
    registry = _persist_stage(
        registry,
        paths["options"],
        "solution_optioning",
        [option],
    )
    registry = _persist_stage(
        registry,
        paths["selection"],
        "solution_selection",
        [selection],
    )
    registry = _persist_stage(
        registry,
        paths["plans"],
        "implementation_planning",
        [plan],
    )
    record = problem_case_records_from_registry(registry)[0]
    monkeypatch.setattr(
        research_hydration,
        "assess_research_readiness",
        lambda _item: (True, []),
    )
    monkeypatch.setattr(
        research_hydration,
        "verify_persisted_research_evidence",
        lambda _item: (True, []),
    )
    monkeypatch.setattr(
        downstream_hydration,
        "assess_solution_option_readiness",
        lambda _option, *, research: (True, []),
    )
    monkeypatch.setattr(
        downstream_hydration,
        "assess_selection_readiness",
        lambda _selection, *, options, research: (True, []),
    )
    monkeypatch.setattr(
        downstream_hydration,
        "assess_change_plan_readiness",
        lambda _plan, *, problem, research, selection: (True, []),
    )
    return record, paths, registry


def _retained_no_change_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    disposition: str = "already_addressed",
) -> tuple[dict, dict[str, Path], dict]:
    problem = {
        "case_id": "case:one",
        "problem_id": "problem:one",
        "title": "One causal problem",
        "evidence_atom_ids": ["atom:one"],
        "source_evidence_atom_ids": ["atom:one"],
    }
    dossier = _dossier()
    dossier["actionability_assessment"] = {
        "disposition": disposition,
        "rationale": "The exact pinned revision already contains the verified behavior.",
        "evidence_refs": ["experiment:one"],
    }
    paths = {
        "research": tmp_path / "research.json",
        "options": tmp_path / "options.json",
    }
    outcome = {
        "problem_id": "problem:one",
        "optioning_status": "not_required",
        "research_actionability_disposition": disposition,
        "decision_rationale": "The authenticated Stage 3 proof requires no product change.",
        "evidence_refs": ["experiment:one"],
        "research_readiness_blockers": [],
        "option_count": 0,
        "rejected_option_count": 0,
    }
    registry = build_case_registry([problem], supporting_atoms=[_atom()])
    registry = _persist_stage(
        registry,
        paths["research"],
        "repro_research",
        [dossier],
    )
    registry = _persist_stage(
        registry,
        paths["options"],
        "solution_optioning",
        [],
        input_meta={"optioning_outcomes": [outcome]},
    )
    record = problem_case_records_from_registry(registry)[0]
    monkeypatch.setattr(
        research_hydration,
        "assess_research_readiness",
        lambda _item: (True, []),
    )
    monkeypatch.setattr(
        research_hydration,
        "verify_persisted_research_evidence",
        lambda _item: (True, []),
    )
    return record, paths, registry


@pytest.mark.parametrize("disposition", ["already_addressed", "non_actionable"])
def test_exact_no_change_disposition_hydrates_and_routes_to_nonterminal_wait(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    disposition: str,
) -> None:
    record, _paths, registry = _retained_no_change_record(
        tmp_path,
        monkeypatch,
        disposition=disposition,
    )

    retained, errors = downstream_hydration.hydrate_retained_no_change_disposition(record)
    route = prioritization._runner_research_route(record)

    assert errors == []
    assert retained is not None
    assert retained["research_actionability_disposition"] == disposition
    assert retained["live_verification_status"] == "unverified"
    assert route["research_route"] == "await_evidence"
    assert route["selected_for_research"] is False
    assert route["eligible_for_downstream"] is False
    assert route["reconsider_when"]
    assert registry["cases"]["case:one"]["state"] == "active"


@pytest.mark.parametrize(
    ("mutation", "expected_error"),
    [
        ("duplicate_outcome", "retained_no_change_outcome_ambiguous"),
        ("case_option", "retained_no_change_artifact_has_case_options"),
        ("changed_evidence_refs", "retained_no_change_outcome_digest_mismatch"),
    ],
)
def test_untrusted_zero_option_disposition_rebuilds_instead_of_parking(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
    expected_error: str,
) -> None:
    record, paths, _registry = _retained_no_change_record(tmp_path, monkeypatch)
    document = json.loads(paths["options"].read_text(encoding="utf-8"))
    if mutation == "duplicate_outcome":
        document["input_meta"]["optioning_outcomes"].append(
            dict(document["input_meta"]["optioning_outcomes"][0])
        )
    elif mutation == "case_option":
        document["items"].append(
            {
                "case_id": "case:one",
                "problem_id": "problem:one",
                "option_id": "option:unexpected",
            }
        )
    else:
        document["input_meta"]["optioning_outcomes"][0]["evidence_refs"] = ["experiment:other"]
    paths["options"].write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    retained, errors = downstream_hydration.hydrate_retained_no_change_disposition(record)
    route = prioritization._runner_research_route(record)

    assert retained is None
    assert errors == [expected_error]
    assert route["research_route"] == "continue_downstream"
    assert route["selected_for_research"] is False
    assert route["eligible_for_downstream"] is True


def test_stale_no_change_input_chain_rebuilds_instead_of_parking(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record, _paths, _registry = _retained_no_change_record(tmp_path, monkeypatch)
    record["prior_stage_context"]["optioning"]["input_chain_sha256"] = "0" * 64

    retained, errors = downstream_hydration.hydrate_retained_no_change_disposition(record)
    route = prioritization._runner_research_route(record)

    assert retained is None
    assert errors == ["retained_no_change_input_chain_mismatch"]
    assert route["research_route"] == "continue_downstream"


def test_exact_current_chain_hydrates_and_routes_to_await_outcome(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _record, _paths, registry = _retained_chain_record(tmp_path, monkeypatch)
    record = staged._attach_current_case_registry_context(
        [
            {
                "case_id": "case:one",
                "problem_id": "problem:one",
                "evidence_atom_ids": ["atom:one"],
                "source_evidence_atom_ids": ["atom:one"],
            }
        ],
        case_registry=registry,
    )[0]

    chain, errors = downstream_hydration.hydrate_retained_downstream_chain(record)
    route = prioritization._runner_research_route(record)

    assert errors == []
    assert chain is not None
    assert chain["problem_id"] == "problem:one"
    assert len(chain["solution_options"]) == 1
    assert len(chain["selection_decisions"]) == 1
    assert len(chain["change_plans"]) == 1
    assert route["research_route"] == "await_outcome"
    assert route["selected_for_research"] is False
    assert route["eligible_for_downstream"] is True
    assert record.get("_carried_forward_case") is not True


def test_changed_research_reuses_proof_but_invalidates_downstream_chain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _record, paths, registry = _retained_chain_record(tmp_path, monkeypatch)
    changed = _dossier()
    changed["root_cause_confidence"] = 0.95
    registry = _persist_stage(
        registry,
        paths["research"],
        "repro_research",
        [changed],
    )
    changed_record = problem_case_records_from_registry(registry)[0]
    changed_record["_carried_forward_case"] = True

    chain, errors = downstream_hydration.hydrate_retained_downstream_chain(changed_record)
    route = prioritization._runner_research_route(changed_record)

    assert chain is None
    assert errors == ["retained_solution_optioning_input_chain_mismatch"]
    assert route["research_route"] == "continue_downstream"
    assert route["selected_for_research"] is False


def test_tampered_downstream_artifact_falls_back_to_normal_rebuild(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record, paths, _registry = _retained_chain_record(tmp_path, monkeypatch)
    document = json.loads(paths["options"].read_text(encoding="utf-8"))
    document["items"][0]["summary"] = "tampered after persistence"
    paths["options"].write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    chain, errors = downstream_hydration.hydrate_retained_downstream_chain(record)
    route = prioritization._runner_research_route(record)

    assert chain is None
    assert errors == ["retained_solution_optioning_records_digest_mismatch"]
    assert route["research_route"] == "continue_downstream"
    assert route["selected_for_research"] is False


def test_stale_chain_dispatches_stage4_through_stage6_without_stage3(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record, paths, _registry = _retained_chain_record(tmp_path, monkeypatch)
    document = json.loads(paths["options"].read_text(encoding="utf-8"))
    document["items"][0]["summary"] = "stale content"
    paths["options"].write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    route = prioritization._runner_research_route(record)
    priority = {
        **route,
        "case_id": "case:one",
        "problem_id": "problem:one",
    }
    blockers = solution_options._priority_progression_blockers(
        priority,
        problem_record=record,
        problem_id="problem:one",
    )
    calls: list[str] = []
    for stage_name in (
        "solution_optioning",
        "solution_selection",
        "implementation_planning",
    ):
        result = staged._run_fresh_downstream_stage(
            fresh_problem_records=[record],
            reused_chains=[],
            run_stage=lambda stage_name=stage_name: calls.append(stage_name)
            or build_stage_document(stage_name, [], input_meta={}, artifacts={}),
        )
        assert result is not None

    assert route["research_route"] == "continue_downstream"
    assert route["selected_for_research"] is False
    assert route["eligible_for_downstream"] is True
    assert blockers == []
    assert calls == [
        "solution_optioning",
        "solution_selection",
        "implementation_planning",
    ]


def test_current_plan_must_still_pass_current_readiness_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record, _paths, _registry = _retained_chain_record(tmp_path, monkeypatch)
    monkeypatch.setattr(
        downstream_hydration,
        "assess_change_plan_readiness",
        lambda _plan, *, problem, research, selection: (
            False,
            ["original_scenario_verification_missing"],
        ),
    )

    chain, errors = downstream_hydration.hydrate_retained_downstream_chain(record)
    route = prioritization._runner_research_route(record)

    assert chain is None
    assert errors == [
        "retained_implementation_planning_not_ready",
        "retained_implementation_planning_readiness:original_scenario_verification_missing",
    ]
    assert route["research_route"] == "continue_downstream"


def test_all_reused_ticket_remains_ready_without_stage3_dispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record, _paths, _registry = _retained_chain_record(tmp_path, monkeypatch)
    route = prioritization._runner_research_route(record)
    priority = {
        **route,
        "case_id": "case:one",
        "problem_id": "problem:one",
        "priority_bucket": "p1",
        "priority_rationale": "The retained causal chain is still actionable.",
        "priority_status": "prioritized",
    }
    ticket = {
        "problem_record": {
            "case_id": "case:one",
            "problem_id": "problem:one",
            "canonical_problem_id": "problem:one",
            "case_member_problem_ids": ["problem:one"],
        },
        "priority": priority,
        "research": {"case_id": "case:one", "problem_id": "problem:one"},
        "solution_options": [
            {
                "case_id": "case:one",
                "problem_id": "problem:one",
                "option_id": "option:one",
            }
        ],
        "selected_solution": {
            "case_id": "case:one",
            "problem_id": "problem:one",
            "selected_option_id": "option:one",
        },
        "change_plan": {
            "case_id": "case:one",
            "problem_id": "problem:one",
        },
    }
    monkeypatch.setattr(
        ticket_readiness,
        "assess_research_readiness",
        lambda _research: (True, []),
    )
    monkeypatch.setattr(
        ticket_readiness,
        "assess_selection_readiness",
        lambda _selection, *, options, research: (True, []),
    )
    monkeypatch.setattr(
        ticket_readiness,
        "assess_change_plan_readiness",
        lambda _plan, *, problem, research, selection: (True, []),
    )

    ready, reasons = ticket_readiness.assess_ticket_readiness(ticket)

    assert priority["selected_for_research"] is False
    assert priority["eligible_for_downstream"] is True
    assert ready is True
    assert reasons == []


def test_all_reused_downstream_work_skips_runner_and_records_no_invocation() -> None:
    calls: list[str] = []
    reused_chain = {"case_id": "case:one", "problem_id": "problem:one"}

    stage_doc = staged._run_fresh_downstream_stage(
        fresh_problem_records=[],
        reused_chains=[reused_chain],
        run_stage=lambda: calls.append("dispatched") or {},
    )
    merged = staged._merge_reused_downstream_stage_document(
        stage="implementation_planning",
        stage_doc=stage_doc,
        reused_items=[
            {
                "case_id": "case:one",
                "problem_id": "problem:one",
                "plan_revision_id": "planrev:one",
            }
        ],
        agent="codex",
        dry_run=False,
        artifacts={"change_plans_json": "plans.json"},
        count_updates={"change_plan_count": 1},
    )

    assert calls == []
    assert merged["input_meta"]["all_items_reused"] is True
    assert merged["input_meta"]["model_invocation_skipped"] == (
        "all_ready_downstream_chains_reused"
    )
    assert verify_stage_model_invocation_contract(merged) == []


def test_mixed_downstream_work_dispatches_once_and_merges_reused_items() -> None:
    calls: list[str] = []
    fresh_doc = build_stage_document(
        "solution_optioning",
        [
            {
                "case_id": "case:fresh",
                "problem_id": "problem:fresh",
                "option_id": "option:fresh",
            }
        ],
        input_meta={},
        artifacts={},
    )

    result = staged._run_fresh_downstream_stage(
        fresh_problem_records=[{"problem_id": "problem:fresh"}],
        reused_chains=[{"problem_id": "problem:reused"}],
        run_stage=lambda: calls.append("dispatched") or fresh_doc,
    )
    merged = staged._merge_reused_downstream_stage_document(
        stage="solution_optioning",
        stage_doc=result,
        reused_items=[
            {
                "case_id": "case:reused",
                "problem_id": "problem:reused",
                "option_id": "option:reused",
            }
        ],
        agent="codex",
        dry_run=False,
        artifacts={"solution_options_json": "options.json"},
        count_updates={"problem_record_count": 2},
    )

    assert calls == ["dispatched"]
    assert [item["problem_id"] for item in merged["items"]] == [
        "problem:fresh",
        "problem:reused",
    ]
    assert merged["input_meta"]["fresh_item_count"] == 1
    assert merged["input_meta"]["reused_item_count"] == 1
    assert merged["input_meta"]["all_items_reused"] is False
