from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path
from typing import Any

import pytest

from usertest_backlog.workflows import qualification_repair_runtime as runtime
from usertest_backlog.workflows import shadow_validation


def _hash(value: Any) -> str:
    return sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _route() -> dict[str, Any]:
    provenance = {
        "authoring_stage": "solution_selection",
        "problem_id": "problem:one",
        "case_id": "case:one",
        "agent_session_id": "11111111-1111-4111-8111-111111111111",
        "workspace_dir": "C:/repo",
        "exact_session_continuation": True,
        "workspace_continuity_verified": True,
        "original_author_cost_seconds": 50.0,
    }
    route: dict[str, Any] = {
        "schema_version": 1,
        "feedback_kind": "accepted_output_quality",
        "authoring_stage": "solution_selection",
        "target_identity": "selection:a",
        "output_kind": "selection",
        "output_sha256": "a" * 64,
        "quality": "bad",
        "bad_severity": "noncritical",
        "bad_categories": ["root_cause_not_addressed"],
        "rationale": "The selection leaves the verified recurrence path intact.",
        "actionable_label_ids": ["label:one"],
        "correctability": "correctable",
        "route_status": "same_author_resume",
        "agent_session_id": provenance["agent_session_id"],
        "workspace_dir": provenance["workspace_dir"],
        "author_attempt_identity": {"attempt_number": 1},
        "author_provenance": provenance,
        "restart_from_stage": "solution_selection",
        "rerun_downstream_stages": [
            "solution_selection",
            "implementation_planning",
            "ticket_assembly",
        ],
        "consumption_status": "pending_orchestration",
        "consumption_receipt": None,
    }
    route["route_sha256"] = _hash(route)
    return route


def _rehash(route: dict[str, Any]) -> dict[str, Any]:
    route.pop("route_sha256", None)
    route["route_sha256"] = _hash(route)
    return route


def _route_for_stage(
    stage: str,
    *,
    output_kind: str,
    target_identity: str,
    downstream: list[str],
) -> dict[str, Any]:
    route = _route()
    route.pop("route_sha256")
    route.update(
        {
            "authoring_stage": stage,
            "restart_from_stage": stage,
            "output_kind": output_kind,
            "target_identity": target_identity,
            "rerun_downstream_stages": downstream,
        }
    )
    route["author_provenance"] = {
        **route["author_provenance"],
        "authoring_stage": stage,
    }
    route["route_sha256"] = _hash(route)
    return route


def _doc(stage: str, items: list[dict[str, Any]]) -> dict[str, Any]:
    return {"schema_version": 1, "stage": stage, "items": items, "input_meta": {}}


@pytest.mark.parametrize(
    ("decision", "expected"),
    [
        (
            {"focus_id": "problem:one", "action": "merge", "target_ids": ["problem:two"]},
            {"problem:one", "problem:two"},
        ),
        (
            {
                "focus_id": "problem:one",
                "action": "alias",
                "alias_target_id": "problem:two",
            },
            {"problem:one", "problem:two"},
        ),
        (
            {
                "focus_id": "problem:one",
                "action": "same_cause_group",
                "member_ids": ["problem:one", "problem:two", "problem:three"],
            },
            {"problem:one", "problem:two", "problem:three"},
        ),
    ],
)
def test_relation_affected_problem_ids_uses_exact_relation_schema(
    decision: dict[str, Any],
    expected: set[str],
) -> None:
    assert runtime._relation_affected_problem_ids(
        seed_problem_ids={"problem:one"},
        before_records=[],
        after_records=[],
        before_decisions=[],
        after_decisions=[decision],
        before_registry={"cases": {}},
        after_registry={"cases": {}},
    ) == expected


def test_relation_affected_problem_ids_closes_split_through_case_lineage() -> None:
    result = runtime._relation_affected_problem_ids(
        seed_problem_ids={"problem:one"},
        before_records=[
            {
                "problem_id": "problem:one",
                "evidence_atom_ids": ["atom:one", "atom:two"],
            }
        ],
        after_records=[
            {"problem_id": "problem:split-a", "evidence_atom_ids": ["atom:one"]},
            {"problem_id": "problem:split-b", "evidence_atom_ids": ["atom:two"]},
        ],
        before_decisions=[],
        after_decisions=[
            {
                "focus_id": "problem:one",
                "action": "split",
                "split_groups": [
                    {"evidence_atom_ids": ["atom:one"]},
                    {"evidence_atom_ids": ["atom:two"]},
                ],
            }
        ],
        before_registry={
            "cases": {
                "case:one": {
                    "case_id": "case:one",
                    "canonical_problem_id": "problem:one",
                    "problem_ids": ["problem:one"],
                }
            }
        },
        after_registry={
            "cases": {
                "case:split-a": {
                    "case_id": "case:split-a",
                    "canonical_problem_id": "problem:split-a",
                    "problem_ids": ["problem:split-a"],
                    "split_from_case_id": "case:one",
                },
                "case:split-b": {
                    "case_id": "case:split-b",
                    "canonical_problem_id": "problem:split-b",
                    "problem_ids": ["problem:split-b"],
                    "split_from_case_id": "case:one",
                },
                "case:one": {
                    "case_id": "case:one",
                    "canonical_problem_id": "problem:one",
                    "problem_ids": ["problem:one"],
                },
            }
        },
    )
    assert result == {"problem:one", "problem:split-a", "problem:split-b"}


def test_runtime_resumes_exact_stage5_author_then_runs_only_downstream(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    route = _route()
    calls: list[tuple[str, dict[str, Any]]] = []

    def fake_stage5(**kwargs: Any) -> dict[str, Any]:
        calls.append(("stage5", kwargs))
        correction = kwargs["external_corrections_by_problem"]["problem:one"]
        assert correction["agent_session_id"] == route["agent_session_id"]
        assert correction["feedback"]["route_sha256"] == route["route_sha256"]
        return {
            "schema_version": 1,
            "stage": "solution_selection",
            "items": [
                {
                    "problem_id": "problem:one",
                    "case_id": "case:one",
                    "selected_option_id": "option:fixed",
                }
            ],
            "input_meta": {
                "role_healing_runs": [
                    {
                        "problem_id": "problem:one",
                        "role": "selector",
                        "session_id": route["agent_session_id"],
                        "attempt_history": [
                            {
                                "status": "verified",
                                "agent_session_id": route["agent_session_id"],
                                "workspace_dir": route["workspace_dir"],
                            }
                        ],
                    }
                ]
            },
        }

    def fake_stage6(**kwargs: Any) -> dict[str, Any]:
        calls.append(("stage6", kwargs))
        assert kwargs.get("external_corrections_by_problem") is None
        return _doc(
            "implementation_planning",
            [
                {
                    "problem_id": "problem:one",
                    "case_id": "case:one",
                    "plan_revision_id": "plan:fixed",
                }
            ],
        )

    monkeypatch.setattr(runtime, "_run_solution_selection_stage", fake_stage5)
    monkeypatch.setattr(runtime, "_run_implementation_planning_stage", fake_stage6)
    monkeypatch.setattr(
        runtime,
        "_run_solution_optioning_stage",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("stage4 must not rerun")),
    )
    monkeypatch.setattr(
        runtime,
        "assemble_backlog_tickets",
        lambda **_kwargs: [{"problem_id": "problem:one", "stage": "ready_for_ticket"}],
    )

    result = runtime.run_stage456_qualification_repairs(
        routes=[route],
        source_pending_run_sha256="d" * 64,
        source_adjudication_sha256="e" * 64,
        repo_root=tmp_path,
        atoms=[{"atom_id": "atom:one"}],
        stage1=_doc(
            "problem_mining",
            [{"problem_id": "problem:one", "case_id": "case:one"}],
        ),
        stage2=_doc(
            "problem_prioritization",
            [{"problem_id": "problem:one", "selected_for_research": True}],
        ),
        stage3=_doc(
            "repro_research",
            [
                {
                    "problem_id": "problem:one",
                    "case_id": "case:one",
                    "repo_workspace": "C:/repo",
                }
            ],
        ),
        stage4=_doc(
            "solution_optioning",
            [{"problem_id": "problem:one", "option_id": "option:old"}],
        ),
        stage5=_doc(
            "solution_selection",
            [
                {
                    "problem_id": "problem:one",
                    "case_id": "case:one",
                    "selected_option_id": "option:old",
                }
            ],
        ),
        stage6=_doc(
            "implementation_planning",
            [
                {
                    "problem_id": "problem:one",
                    "case_id": "case:one",
                    "plan_revision_id": "plan:old",
                }
            ],
        ),
        pipeline_manifest=type(
            "Manifest",
            (),
            {"load_stage_guidance": lambda _self, stage: f"guidance:{stage}"},
        )(),
        repair_artifacts_dir=tmp_path / "repair",
        agent="codex",
        model=None,
        cfg=object(),
        breadth_profile="default",
    )

    assert [name for name, _kwargs in calls] == ["stage5", "stage6"]
    assert result.consumption["accepted_repair_count"] == 1
    assert result.affected_problem_ids == ["problem:one"]
    assert result.stage_documents["solution_selection"]["items"][0][
        "selected_option_id"
    ] == "option:fixed"
    assert result.stage_documents["implementation_planning"]["items"][0][
        "plan_revision_id"
    ] == "plan:fixed"
    assert result.tickets == [
        {"problem_id": "problem:one", "stage": "ready_for_ticket"}
    ]


def test_stage3_continuation_receives_complete_bound_independent_feedback(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    route = _route_for_stage(
        "repro_research",
        output_kind="research",
        target_identity="research:contradictory-origin",
        downstream=[
            "repro_research",
            "solution_optioning",
            "solution_selection",
            "implementation_planning",
            "ticket_assembly",
        ],
    )
    route.pop("route_sha256")
    route.update(
        {
            "rationale": (
                "The alternative hypothesis contradicts the cited origin atom."
            ),
            "bad_categories": ["contradictory_origin_atom"],
            "actionable_label_ids": ["label:origin-contradiction"],
            "causal_target": {
                "problem_ids": ["problem:one"],
                "case_ids": ["case:one"],
                "evidence_atom_ids": ["atom:origin"],
                "actionable_label_ids": ["label:origin-contradiction"],
                "expected_item_keys": ["research:problem:one"],
            },
        }
    )
    route["author_provenance"] = {
        **route["author_provenance"],
        "problem_id": "problem:one",
        "case_id": "case:one",
        "evidence_atom_ids": ["atom:origin"],
    }
    route["route_sha256"] = _hash(route)
    captured: list[dict[str, Any]] = []

    def continue_research(**kwargs: Any) -> dict[str, Any]:
        captured.append(kwargs)
        return {
            "status": "corrected",
            "dossier": {
                "problem_id": "problem:one",
                "case_id": "case:one",
                "repo_workspace": str(tmp_path),
            },
            "validation_errors": [],
            "agent_session_id": route["agent_session_id"],
            "workspace_dir": route["workspace_dir"],
        }

    monkeypatch.setattr(
        runtime,
        "continue_research_dossier_from_independent_feedback",
        continue_research,
    )
    monkeypatch.setattr(
        runtime,
        "_run_solution_optioning_stage",
        lambda **_kwargs: _doc(
            "solution_optioning",
            [{"problem_id": "problem:one", "option_id": "option:one"}],
        ),
    )
    monkeypatch.setattr(
        runtime,
        "_run_solution_selection_stage",
        lambda **_kwargs: _doc(
            "solution_selection",
            [{"problem_id": "problem:one", "selected_option_id": "option:one"}],
        ),
    )
    monkeypatch.setattr(
        runtime,
        "_run_implementation_planning_stage",
        lambda **_kwargs: _doc(
            "implementation_planning",
            [{"problem_id": "problem:one", "plan_revision_id": "plan:one"}],
        ),
    )
    monkeypatch.setattr(runtime, "assemble_backlog_tickets", lambda **_kwargs: [])

    result = runtime.run_stage456_qualification_repairs(
        routes=[route],
        source_pending_run_sha256="d" * 64,
        source_adjudication_sha256="e" * 64,
        repo_root=tmp_path,
        atoms=[{"atom_id": "atom:origin"}],
        stage1=_doc(
            "problem_mining",
            [{"problem_id": "problem:one", "case_id": "case:one"}],
        ),
        stage2=_doc(
            "problem_prioritization",
            [{"problem_id": "problem:one", "selected_for_research": True}],
        ),
        stage3=_doc(
            "repro_research",
            [
                {
                    "problem_id": "problem:one",
                    "case_id": "case:one",
                    "repo_workspace": str(tmp_path),
                }
            ],
        ),
        stage4=_doc("solution_optioning", []),
        stage5=_doc("solution_selection", []),
        stage6=_doc("implementation_planning", []),
        pipeline_manifest=type(
            "Manifest",
            (),
            {"load_stage_guidance": lambda _self, stage: f"guidance:{stage}"},
        )(),
        repair_artifacts_dir=tmp_path / "stage3-feedback",
        agent="codex",
        model=None,
        cfg=object(),
        breadth_profile="standard",
        repo_input=str(tmp_path),
        research_ref="dev",
        replay_executor=object(),
        case_registry={"cases": {}},
    )

    assert result.consumption["accepted_repair_count"] == 1
    assert len(captured) == 1
    feedback = captured[0]["independent_feedback"]
    assert feedback["route_sha256"] == route["route_sha256"]
    assert feedback["source_pending_run_sha256"] == "d" * 64
    assert feedback["source_adjudication_sha256"] == "e" * 64
    assert feedback["rationale"] == route["rationale"]
    assert feedback["bad_categories"] == ["contradictory_origin_atom"]
    assert feedback["actionable_label_ids"] == ["label:origin-contradiction"]
    assert feedback["evidence_atom_ids"] == ["atom:origin"]
    assert feedback["causal_target"] == route["causal_target"]


def test_runtime_stage1_miss_resumes_reviewer_then_runs_stage2_through_stage6(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    provenance = {
        "authoring_stage": "problem_mining",
        "problem_id": None,
        "case_id": None,
        "agent_session_id": "11111111-1111-4111-8111-111111111111",
        "workspace_dir": "C:/review-workspace",
        "exact_session_continuation": True,
        "workspace_continuity_verified": True,
        "original_author_cost_seconds": 25.0,
    }
    route: dict[str, Any] = {
        "schema_version": 1,
        "feedback_kind": "false_rejection",
        "authoring_stage": "problem_mining",
        "target_identity": "actionable_label:label:miss",
        "output_kind": None,
        "output_sha256": None,
        "quality": "bad",
        "bad_severity": "noncritical",
        "bad_categories": ["false_rejection"],
        "rationale": "The actionable observation was not mined.",
        "actionable_label_ids": ["label:miss"],
        "correctability": "correctable",
        "route_status": "same_author_resume",
        "agent_session_id": provenance["agent_session_id"],
        "workspace_dir": provenance["workspace_dir"],
        "author_attempt_identity": {"attempt_number": 1},
        "author_provenance": provenance,
        "restart_from_stage": "problem_mining",
        "rerun_downstream_stages": [
            "problem_mining",
            "problem_prioritization",
            "repro_research",
            "solution_optioning",
            "solution_selection",
            "implementation_planning",
            "ticket_assembly",
        ],
        "consumption_status": "pending_orchestration",
        "consumption_receipt": None,
    }
    route["route_sha256"] = _hash(route)
    calls: list[str] = []
    new_problem = {
        "problem_id": "problem:mined",
        "case_id": "case:mined",
        "evidence_atom_ids": ["atom:miss"],
    }

    def stage1(**_kwargs: Any) -> dict[str, Any]:
        calls.append("stage1")
        if calls.count("stage1") == 1:
            return {
                "status": "repairable_paused:stage1_correction_invalid",
                "stage_doc": _doc("problem_mining", []),
                "atoms": [{"atom_id": "atom:miss"}],
                "validation_errors": ["stage1_first_correction_invalid"],
                "attempt_record": {
                    "attempt_number": 2,
                    "status": "invalid",
                    "agent_session_id": route["agent_session_id"],
                    "workspace_dir": route["workspace_dir"],
                    "attempt_elapsed_seconds": 1.0,
                },
                "agent_session_id": route["agent_session_id"],
                "workspace_dir": route["workspace_dir"],
            }
        assert len(_kwargs["prior_correction_attempts"]) == 1
        assert _kwargs["correction_attempt_number"] >= 3
        evidence = tmp_path / "repair" / "problem_records.evidence_receipt.json"
        evidence.parent.mkdir(parents=True, exist_ok=True)
        evidence.write_text("{}\n", encoding="utf-8")
        return {
            "status": "corrected",
            "stage_doc": {
                "stage": "problem_mining",
                "items": [new_problem],
                "input_meta": {},
                "artifacts": {"problem_mining_evidence_receipt": str(evidence)},
            },
            "atoms": [
                {
                    "atom_id": "atom:miss",
                    "disposition": "supports_case",
                    "case_id": "case:mined",
                }
            ],
            "case_registry": {"cases": {"case:mined": {"case_id": "case:mined"}}},
            "validation_errors": [],
            "agent_session_id": route["agent_session_id"],
            "workspace_dir": route["workspace_dir"],
        }

    monkeypatch.setattr(
        runtime,
        "continue_problem_mining_from_independent_feedback",
        stage1,
    )

    def stage2(**_kwargs: Any) -> dict[str, Any]:
        calls.append("stage2")
        return _doc(
            "problem_prioritization",
            [{"problem_id": "problem:mined", "selected_for_research": True}],
        )

    def stage3(**_kwargs: Any) -> dict[str, Any]:
        calls.append("stage3")
        return _doc(
            "repro_research",
            [
                {
                    "problem_id": "problem:mined",
                    "case_id": "case:mined",
                    "repo_workspace": str(tmp_path),
                }
            ],
        )

    def stage4(**_kwargs: Any) -> dict[str, Any]:
        calls.append("stage4")
        return _doc(
            "solution_optioning",
            [{"problem_id": "problem:mined", "option_id": "option:mined"}],
        )

    def stage5(**_kwargs: Any) -> dict[str, Any]:
        calls.append("stage5")
        return _doc(
            "solution_selection",
            [
                {
                    "problem_id": "problem:mined",
                    "selected_option_id": "option:mined",
                }
            ],
        )

    def stage6(**_kwargs: Any) -> dict[str, Any]:
        calls.append("stage6")
        return _doc(
            "implementation_planning",
            [{"problem_id": "problem:mined", "plan_revision_id": "plan:mined"}],
        )

    monkeypatch.setattr(runtime, "_run_problem_prioritization_stage", stage2)
    monkeypatch.setattr(runtime, "_run_repro_research_stage", stage3)
    monkeypatch.setattr(runtime, "_run_solution_optioning_stage", stage4)
    monkeypatch.setattr(runtime, "_run_solution_selection_stage", stage5)
    monkeypatch.setattr(runtime, "_run_implementation_planning_stage", stage6)
    monkeypatch.setattr(runtime, "assemble_backlog_tickets", lambda **_kwargs: [])
    result = runtime.run_stage456_qualification_repairs(
        routes=[route],
        source_pending_run_sha256="d" * 64,
        source_adjudication_sha256="e" * 64,
        repo_root=tmp_path,
        atoms=[{"atom_id": "atom:miss"}],
        stage1=_doc("problem_mining", []),
        stage2=_doc("problem_prioritization", []),
        stage3=_doc("repro_research", []),
        stage4=_doc("solution_optioning", []),
        stage5=_doc("solution_selection", []),
        stage6=_doc("implementation_planning", []),
        pipeline_manifest=type(
            "Manifest",
            (),
            {"load_stage_guidance": lambda _self, stage: f"guidance:{stage}"},
        )(),
        repair_artifacts_dir=tmp_path / "runtime",
        agent="codex",
        model=None,
        cfg=object(),
        breadth_profile="standard",
        repo_input=str(tmp_path),
        research_ref="dev",
        replay_timeout_seconds=None,
        replay_executor=object(),
        replay_executor_metadata={"executor": "test"},
        target_slug="target",
        case_registry={"cases": {}},
        qualification_manifest={
            "atom_labels": [
                {
                    "label_id": "label:miss",
                    "classification": "actionable",
                    "atom_ids": ["atom:miss"],
                }
            ]
        },
    )

    assert calls == [
        "stage1",
        "stage1",
        "stage2",
        "stage3",
        "stage4",
        "stage5",
        "stage6",
    ]
    assert result.consumption["accepted_repair_count"] == 1
    assert result.affected_problem_ids == ["problem:mined"]
    assert result.atoms == [
        {
            "atom_id": "atom:miss",
            "disposition": "supports_case",
            "case_id": "case:mined",
        }
    ]
    assert result.case_registry == {
        "cases": {"case:mined": {"case_id": "case:mined"}}
    }
    receipt_stages = {
        item["stage"]
        for item in result.consumption["downstream_result"][
            "materialized_stage_receipts"
        ]
    }
    assert {"case_registry", "problem_mining_evidence"}.issubset(receipt_stages)


def test_runtime_shallow_option_can_be_retracted_as_no_safe_option(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    route = _route_for_stage(
        "solution_optioning",
        output_kind="option",
        target_identity="option:shallow",
        downstream=[
            "solution_optioning",
            "solution_selection",
            "implementation_planning",
            "ticket_assembly",
        ],
    )
    calls: list[str] = []

    def stage4(**kwargs: Any) -> dict[str, Any]:
        calls.append("stage4")
        correction = kwargs["external_corrections_by_problem"]["problem:one"]
        assert correction["feedback"]["bad_categories"] == [
            "root_cause_not_addressed"
        ]
        return {
            "schema_version": 1,
            "stage": "solution_optioning",
            "items": [],
            "input_meta": {
                "optioning_outcomes": [
                    {
                        "problem_id": "problem:one",
                        "optioning_status": "no_safe_option",
                    }
                ],
                "optioning_correction_runs": [
                    {
                        "problem_id": "problem:one",
                        "session_id": route["agent_session_id"],
                        "attempt_history": [
                            {
                                "attempt_number": 2,
                                "attempt_tag": "same-author-option-retraction",
                                "status": "verified",
                                "agent_session_id": route["agent_session_id"],
                                "workspace_dir": route["workspace_dir"],
                                "elapsed_seconds": 2.5,
                            }
                        ],
                    }
                ],
            },
        }

    def stage5(**kwargs: Any) -> dict[str, Any]:
        calls.append("stage5")
        assert kwargs["solution_options"] == []
        return {
            "schema_version": 1,
            "stage": "solution_selection",
            "items": [],
            "input_meta": {
                "selection_outcomes": [
                    {
                        "problem_id": "problem:one",
                        "selection_status": "no_safe_option",
                    }
                ]
            },
        }

    def stage6(**kwargs: Any) -> dict[str, Any]:
        calls.append("stage6")
        assert kwargs["selection_decisions"] == []
        return _doc("implementation_planning", [])

    monkeypatch.setattr(runtime, "_run_solution_optioning_stage", stage4)
    monkeypatch.setattr(runtime, "_run_solution_selection_stage", stage5)
    monkeypatch.setattr(runtime, "_run_implementation_planning_stage", stage6)
    monkeypatch.setattr(runtime, "assemble_backlog_tickets", lambda **_kwargs: [])
    result = runtime.run_stage456_qualification_repairs(
        routes=[route],
        source_pending_run_sha256="d" * 64,
        source_adjudication_sha256="e" * 64,
        repo_root=tmp_path,
        atoms=[{"atom_id": "atom:one"}],
        stage1=_doc(
            "problem_mining",
            [{"problem_id": "problem:one", "case_id": "case:one"}],
        ),
        stage2=_doc(
            "problem_prioritization",
            [{"problem_id": "problem:one", "selected_for_research": True}],
        ),
        stage3=_doc(
            "repro_research",
            [
                {
                    "problem_id": "problem:one",
                    "case_id": "case:one",
                    "repo_workspace": str(tmp_path),
                }
            ],
        ),
        stage4=_doc(
            "solution_optioning",
            [{"problem_id": "problem:one", "option_id": "option:shallow"}],
        ),
        stage5=_doc(
            "solution_selection",
            [{"problem_id": "problem:one", "selected_option_id": "option:shallow"}],
        ),
        stage6=_doc(
            "implementation_planning",
            [{"problem_id": "problem:one", "plan_revision_id": "plan:shallow"}],
        ),
        pipeline_manifest=type(
            "Manifest",
            (),
            {"load_stage_guidance": lambda _self, stage: f"guidance:{stage}"},
        )(),
        repair_artifacts_dir=tmp_path / "option-repair",
        agent="codex",
        model=None,
        cfg=object(),
        breadth_profile="standard",
    )

    assert calls == ["stage4", "stage5", "stage6"]
    assert result.consumption["accepted_repair_count"] == 1
    assert result.stage_documents["solution_optioning"]["items"] == []
    assert result.stage_documents["solution_selection"]["items"] == []
    assert result.stage_documents["implementation_planning"]["items"] == []
    assert result.consumption["route_receipts"][0]["attempts"][0][
        "cost_seconds"
    ] == 0.0
    assert result.consumption["route_receipts"][0]["attempts"][1][
        "cost_seconds"
    ] >= 2.5


def test_runtime_stage2_exact_author_reruns_research_and_uses_repaired_workspace(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    route = _route_for_stage(
        "problem_prioritization",
        output_kind="priority",
        target_identity="priority:problem:one",
        downstream=[
            "problem_prioritization",
            "repro_research",
            "solution_optioning",
            "solution_selection",
            "implementation_planning",
            "ticket_assembly",
        ],
    )
    repaired_workspace = tmp_path / "repaired-workspace"
    repaired_workspace.mkdir()
    calls: list[str] = []

    def stage2(**kwargs: Any) -> dict[str, Any]:
        calls.append("stage2")
        correction = kwargs["external_correction"]
        assert correction["agent_session_id"] == route["agent_session_id"]
        assert correction["current_payload"][0]["priority_bucket"] == "watch"
        return {
            "schema_version": 1,
            "stage": "problem_prioritization",
            "items": [
                {
                    "problem_id": "problem:one",
                    "priority_bucket": "p1",
                    "selected_for_research": True,
                }
            ],
            "input_meta": {
                "prioritizer_attempt_history": [
                    {
                        "attempt_number": 2,
                        "status": "verified",
                        "agent_session_id": route["agent_session_id"],
                        "workspace_dir": route["workspace_dir"],
                        "attempt_elapsed_seconds": 1.25,
                    }
                ]
            },
        }

    def stage3(**_kwargs: Any) -> dict[str, Any]:
        calls.append("stage3")
        return _doc(
            "repro_research",
            [
                {
                    "problem_id": "problem:one",
                    "case_id": "case:one",
                    "repo_workspace": str(repaired_workspace),
                }
            ],
        )

    def stage4(**kwargs: Any) -> dict[str, Any]:
        calls.append("stage4")
        assert kwargs["target_repo_roots_by_problem"] == {
            "problem:one": repaired_workspace.resolve()
        }
        return _doc(
            "solution_optioning",
            [{"problem_id": "problem:one", "option_id": "option:repaired"}],
        )

    monkeypatch.setattr(runtime, "_run_problem_prioritization_stage", stage2)
    monkeypatch.setattr(runtime, "_run_repro_research_stage", stage3)
    monkeypatch.setattr(runtime, "_run_solution_optioning_stage", stage4)
    monkeypatch.setattr(
        runtime,
        "_run_solution_selection_stage",
        lambda **_kwargs: _doc(
            "solution_selection",
            [
                {
                    "problem_id": "problem:one",
                    "selected_option_id": "option:repaired",
                }
            ],
        ),
    )
    monkeypatch.setattr(
        runtime,
        "_run_implementation_planning_stage",
        lambda **_kwargs: _doc(
            "implementation_planning",
            [{"problem_id": "problem:one", "plan_revision_id": "plan:repaired"}],
        ),
    )
    monkeypatch.setattr(runtime, "assemble_backlog_tickets", lambda **_kwargs: [])
    result = runtime.run_stage456_qualification_repairs(
        routes=[route],
        source_pending_run_sha256="d" * 64,
        source_adjudication_sha256="e" * 64,
        repo_root=tmp_path,
        atoms=[{"atom_id": "atom:one"}],
        stage1=_doc(
            "problem_mining",
            [{"problem_id": "problem:one", "case_id": "case:one"}],
        ),
        stage2=_doc(
            "problem_prioritization",
            [
                {
                    "problem_id": "problem:one",
                    "priority_bucket": "watch",
                    "selected_for_research": True,
                }
            ],
        ),
        stage3=_doc(
            "repro_research",
            [
                {
                    "problem_id": "problem:one",
                    "case_id": "case:one",
                    "repo_workspace": str(tmp_path / "stale-workspace"),
                }
            ],
        ),
        stage4=_doc(
            "solution_optioning",
            [{"problem_id": "problem:one", "option_id": "option:old"}],
        ),
        stage5=_doc(
            "solution_selection",
            [{"problem_id": "problem:one", "selected_option_id": "option:old"}],
        ),
        stage6=_doc(
            "implementation_planning",
            [{"problem_id": "problem:one", "plan_revision_id": "plan:old"}],
        ),
        pipeline_manifest=type(
            "Manifest",
            (),
            {"load_stage_guidance": lambda _self, stage: f"guidance:{stage}"},
        )(),
        repair_artifacts_dir=tmp_path / "priority-repair",
        agent="codex",
        model=None,
        cfg=object(),
        breadth_profile="standard",
        repo_input=str(tmp_path),
        research_ref="dev",
        replay_executor=object(),
    )

    assert calls == ["stage2", "stage3", "stage4"]
    assert result.consumption["accepted_repair_count"] == 1
    assert result.stage_documents["problem_prioritization"]["items"][0][
        "priority_bucket"
    ] == "p1"


def test_runtime_groups_global_prioritizer_feedback_and_preserves_cardinality(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    route_one = _route_for_stage(
        "problem_prioritization",
        output_kind="priority",
        target_identity="priority:problem:one",
        downstream=[
            "problem_prioritization",
            "repro_research",
            "solution_optioning",
            "solution_selection",
            "implementation_planning",
            "ticket_assembly",
        ],
    )
    route_one.pop("route_sha256")
    route_one["author_provenance"].update(
        {
            "author_role": "prioritizer",
            "assignment_id": "global_problem_prioritization",
        }
    )
    route_one["route_sha256"] = _hash(route_one)
    route_two = json.loads(json.dumps(route_one))
    route_two.pop("route_sha256")
    route_two.update(
        {
            "target_identity": "priority:problem:two",
            "output_sha256": "b" * 64,
            "rationale": "The second priority decision also understates impact.",
            "actionable_label_ids": ["label:two"],
        }
    )
    route_two["author_provenance"].update(
        {"problem_id": "problem:two", "case_id": "case:two"}
    )
    route_two["route_sha256"] = _hash(route_two)
    stage2_calls: list[dict[str, Any]] = []

    def stage2(**kwargs: Any) -> dict[str, Any]:
        stage2_calls.append(kwargs)
        feedback = kwargs["external_correction"]["feedback"]
        assert len(feedback["findings"]) == 2
        assert {
            item["problem_id"] for item in feedback["findings"]
        } == {"problem:one", "problem:two"}
        return {
            "schema_version": 1,
            "stage": "problem_prioritization",
            "items": [
                {
                    "problem_id": "problem:one",
                    "priority_bucket": "p1",
                    "selected_for_research": True,
                },
                {
                    "problem_id": "problem:two",
                    "priority_bucket": "p2",
                    "selected_for_research": True,
                },
                {
                    "problem_id": "problem:unrelated",
                    "priority_bucket": "watch",
                    "selected_for_research": True,
                },
            ],
            "input_meta": {
                "prioritizer_attempt_history": [
                    {
                        "attempt_number": 2,
                        "status": "verified",
                        "agent_session_id": route_one["agent_session_id"],
                        "workspace_dir": route_one["workspace_dir"],
                    }
                ]
            },
        }

    def stage3(**kwargs: Any) -> dict[str, Any]:
        pid = kwargs["selected_priority_decisions"][0]["problem_id"]
        return _doc(
            "repro_research",
            [
                {
                    "problem_id": pid,
                    "case_id": pid.replace("problem:", "case:"),
                    "repo_workspace": str(tmp_path / pid.replace(":", "_")),
                }
            ],
        )

    def one_pid_doc(stage: str, kwargs: dict[str, Any]) -> dict[str, Any]:
        records = kwargs.get("problem_records", [])
        pid = records[0]["problem_id"]
        payload: dict[str, Any] = {"problem_id": pid}
        if stage == "solution_optioning":
            payload["option_id"] = "option:" + pid
        elif stage == "solution_selection":
            payload["selected_option_id"] = "option:" + pid
        else:
            payload["plan_revision_id"] = "plan:" + pid
        return _doc(stage, [payload])

    monkeypatch.setattr(runtime, "_run_problem_prioritization_stage", stage2)
    monkeypatch.setattr(runtime, "_run_repro_research_stage", stage3)
    monkeypatch.setattr(
        runtime,
        "_run_solution_optioning_stage",
        lambda **kwargs: one_pid_doc("solution_optioning", kwargs),
    )
    monkeypatch.setattr(
        runtime,
        "_run_solution_selection_stage",
        lambda **kwargs: one_pid_doc("solution_selection", kwargs),
    )
    monkeypatch.setattr(
        runtime,
        "_run_implementation_planning_stage",
        lambda **kwargs: one_pid_doc("implementation_planning", kwargs),
    )
    monkeypatch.setattr(runtime, "assemble_backlog_tickets", lambda **_kwargs: [])
    problem_records = [
        {
            "problem_id": pid,
            "case_id": pid.replace("problem:", "case:"),
        }
        for pid in ("problem:one", "problem:two", "problem:unrelated")
    ]
    old_priorities = [
        {
            "problem_id": "problem:one",
            "priority_bucket": "watch",
            "selected_for_research": True,
        },
        {
            "problem_id": "problem:two",
            "priority_bucket": "watch",
            "selected_for_research": True,
        },
        {
            "problem_id": "problem:unrelated",
            "priority_bucket": "watch",
            "selected_for_research": True,
        },
    ]
    result = runtime.run_stage456_qualification_repairs(
        routes=[route_one, route_two],
        source_pending_run_sha256="d" * 64,
        source_adjudication_sha256="e" * 64,
        repo_root=tmp_path,
        atoms=[],
        stage1=_doc("problem_mining", problem_records),
        stage2=_doc("problem_prioritization", old_priorities),
        stage3=_doc("repro_research", []),
        stage4=_doc("solution_optioning", []),
        stage5=_doc("solution_selection", []),
        stage6=_doc("implementation_planning", []),
        pipeline_manifest=type(
            "Manifest",
            (),
            {"load_stage_guidance": lambda _self, stage: f"guidance:{stage}"},
        )(),
        repair_artifacts_dir=tmp_path / "grouped-priority-repair",
        agent="codex",
        model=None,
        cfg=object(),
        breadth_profile="standard",
        repo_input=str(tmp_path),
        research_ref="dev",
        replay_executor=object(),
    )

    assert len(stage2_calls) == 1
    priorities = result.stage_documents["problem_prioritization"]["items"]
    assert len(priorities) == 3
    assert len({item["problem_id"] for item in priorities}) == 3
    by_id = {item["problem_id"]: item["priority_bucket"] for item in priorities}
    assert by_id == {
        "problem:one": "p1",
        "problem:two": "p2",
        "problem:unrelated": "watch",
    }
    assert result.consumption["accepted_repair_count"] == 2
    assert result.consumption["accepted_repair_group_count"] == 1
    repair_meta = result.stage_documents["problem_prioritization"]["input_meta"][
        "qualification_repair"
    ]
    assert len(repair_meta["route_consumption_receipts"]) == 2


def test_runtime_composes_disjoint_stage1_authors_but_blocks_stale_downstream(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    routes: list[dict[str, Any]] = []
    for suffix in ("one", "two"):
        route = _route_for_stage(
            "problem_mining",
            output_kind="relation",
            target_identity=f"relation:problem:{suffix}",
            downstream=[
                "problem_mining",
                "problem_prioritization",
                "repro_research",
                "solution_optioning",
                "solution_selection",
                "implementation_planning",
                "ticket_assembly",
            ],
        )
        route.pop("route_sha256")
        route.update(
            {
                "agent_session_id": f"session:{suffix}",
                "workspace_dir": f"C:/workspace/{suffix}",
                "output_sha256": ("a" if suffix == "one" else "b") * 64,
            }
        )
        route["author_provenance"].update(
            {
                "problem_id": f"problem:{suffix}",
                "case_id": f"case:{suffix}",
                "agent_session_id": f"session:{suffix}",
                "workspace_dir": f"C:/workspace/{suffix}",
                "author_role": "relation_reviewer",
                "stage1_correction_adapter": "relation_review",
                "relation_review_batch_tag": f"batch:{suffix}",
                "relation_review_focus_ids": [f"problem:{suffix}"],
            }
        )
        route["route_sha256"] = _hash(route)
        routes.append(route)
    selected = min(routes, key=lambda item: item["route_sha256"])
    pending = next(item for item in routes if item is not selected)
    planner_route = _route_for_stage(
        "implementation_planning",
        output_kind="plan",
        target_identity="plan:stale-downstream",
        downstream=["implementation_planning", "ticket_assembly"],
    )
    planner_route.pop("route_sha256")
    planner_route["author_provenance"].update(
        {
            "problem_id": selected["author_provenance"]["problem_id"],
            "case_id": selected["author_provenance"]["case_id"],
            "author_role": "planner",
        }
    )
    planner_route["route_sha256"] = _hash(planner_route)
    routes.append(planner_route)
    stage1_calls: list[dict[str, Any]] = []

    def stage1(**kwargs: Any) -> dict[str, Any]:
        stage1_calls.append(kwargs)
        provenance = kwargs["author_provenance"]
        pid = provenance["problem_id"]
        prior_atoms = [dict(item) for item in kwargs["atoms"]]
        prior_registry = dict(kwargs["previous_case_registry"])
        prior_cases = dict(prior_registry.get("cases") or {})
        prior_cases[provenance["case_id"]] = {
            "case_id": provenance["case_id"],
            "canonical_problem_id": pid,
            "problem_ids": [pid],
        }
        return {
            "status": "corrected",
            "stage_doc": _doc(
                "problem_mining",
                [
                    {"problem_id": "problem:one", "case_id": "case:one"},
                    {"problem_id": "problem:two", "case_id": "case:two"},
                ],
            ),
            "atoms": [*prior_atoms, {"atom_id": f"atom:{pid}", "disposition": "supports_case"}],
            "case_registry": {"cases": prior_cases},
            "validation_errors": [],
            "attempt_record": {"elapsed_seconds": 1.0},
            "agent_session_id": provenance["agent_session_id"],
            "workspace_dir": provenance["workspace_dir"],
            "problem_records": [{"problem_id": pid}],
        }

    monkeypatch.setattr(
        runtime,
        "continue_problem_relation_review_from_independent_feedback",
        stage1,
    )
    monkeypatch.setattr(
        runtime,
        "_run_problem_prioritization_stage",
        lambda **kwargs: _doc(
            "problem_prioritization",
            [
                {
                    "problem_id": kwargs["problem_records"][0]["problem_id"],
                    "selected_for_research": True,
                }
            ],
        ),
    )
    monkeypatch.setattr(
        runtime,
        "_run_repro_research_stage",
        lambda **kwargs: _doc(
            "repro_research",
            [
                {
                    "problem_id": kwargs["problem_records"][0]["problem_id"],
                    "repo_workspace": str(tmp_path),
                }
            ],
        ),
    )
    monkeypatch.setattr(
        runtime,
        "_run_solution_optioning_stage",
        lambda **kwargs: _doc(
            "solution_optioning",
            [{"problem_id": kwargs["problem_records"][0]["problem_id"]}],
        ),
    )
    monkeypatch.setattr(
        runtime,
        "_run_solution_selection_stage",
        lambda **kwargs: _doc(
            "solution_selection",
            [{"problem_id": kwargs["problem_records"][0]["problem_id"]}],
        ),
    )
    planning_calls: list[dict[str, Any]] = []

    def planning(**kwargs: Any) -> dict[str, Any]:
        planning_calls.append(kwargs)
        return _doc(
            "implementation_planning",
            [{"problem_id": kwargs["problem_records"][0]["problem_id"]}],
        )

    monkeypatch.setattr(runtime, "_run_implementation_planning_stage", planning)
    monkeypatch.setattr(runtime, "assemble_backlog_tickets", lambda **_kwargs: [])
    source_records = [
        {"problem_id": "problem:one", "case_id": "case:one"},
        {"problem_id": "problem:two", "case_id": "case:two"},
    ]
    result = runtime.run_stage456_qualification_repairs(
        routes=routes,
        source_pending_run_sha256="d" * 64,
        source_adjudication_sha256="e" * 64,
        repo_root=tmp_path,
        atoms=[],
        stage1=_doc("problem_mining", source_records),
        stage2=_doc(
            "problem_prioritization",
            [
                {"problem_id": "problem:one", "selected_for_research": True},
                {"problem_id": "problem:two", "selected_for_research": True},
            ],
        ),
        stage3=_doc("repro_research", []),
        stage4=_doc("solution_optioning", []),
        stage5=_doc("solution_selection", []),
        stage6=_doc("implementation_planning", []),
        pipeline_manifest=type(
            "Manifest",
            (),
            {"load_stage_guidance": lambda _self, stage: f"guidance:{stage}"},
        )(),
        repair_artifacts_dir=tmp_path / "stage1-scheduling",
        agent="codex",
        model=None,
        cfg=object(),
        breadth_profile="standard",
        repo_input=str(tmp_path),
        research_ref="dev",
        replay_executor=object(),
        case_registry={"cases": {}},
    )

    assert len(stage1_calls) == 2
    assert {
        call["author_provenance"]["agent_session_id"] for call in stage1_calls
    } == {route["agent_session_id"] for route in routes[:2]}
    assert stage1_calls[0]["atoms"] == []
    assert len(stage1_calls[1]["atoms"]) == 1
    assert len(stage1_calls[1]["previous_case_registry"]["cases"]) == 1
    assert len(result.atoms or []) == 2
    assert len((result.case_registry or {}).get("cases", {})) == 2
    assert result.consumption["accepted_repair_count"] == 2
    assert result.consumption["unresolved_route_count"] == 1
    assert result.consumption["pending_not_invoked_route_count"] == 1
    receipts = {
        item["route_sha256"]: item for item in result.consumption["route_receipts"]
    }
    assert receipts[selected["route_sha256"]]["status"] == "corrected"
    assert receipts[pending["route_sha256"]]["status"] == "corrected"
    assert receipts[planner_route["route_sha256"]]["status"] == "retained_pending_not_invoked"
    assert receipts[planner_route["route_sha256"]]["attempts"] == []
    assert planning_calls
    assert all(call.get("external_corrections_by_problem") is None for call in planning_calls)
    assert result.consumption["downstream_result"][
        "retained_pending_not_invoked"
    ][0]["route_sha256"] == planner_route["route_sha256"]


@pytest.mark.parametrize("adapter", ["relation_review", "problem_miner"])
def test_stage1_merge_reconciles_both_prior_case_descendants_for_every_adapter(
    monkeypatch: Any,
    tmp_path: Path,
    adapter: str,
) -> None:
    route = _route_for_stage(
        "problem_mining",
        output_kind="relation",
        target_identity="relation:problem:one",
        downstream=[
            "problem_mining",
            "problem_prioritization",
            "repro_research",
            "solution_optioning",
            "solution_selection",
            "implementation_planning",
            "ticket_assembly",
        ],
    )
    route.pop("route_sha256")
    route["author_provenance"].update(
        {
            "author_role": (
                "relation_reviewer" if adapter == "relation_review" else "problem_miner"
            ),
            "stage1_correction_adapter": adapter,
            "relation_review_batch_tag": "batch:one-two",
            "relation_review_focus_ids": ["problem:one", "problem:two"],
        }
    )
    route["route_sha256"] = _hash(route)
    stale_planner_route = _causal_route(
        stage="implementation_planning",
        problem_id="problem:two",
        case_id="case:two",
        session="stale-planner-two",
    )
    corrected_stage1 = {
        "schema_version": 1,
        "stage": "problem_mining",
        "items": [
            {
                "problem_id": "problem:one",
                "case_id": "case:one",
                "case_member_problem_ids": ["problem:one", "problem:two"],
                "evidence_atom_ids": ["atom:one", "atom:two"],
            }
        ],
        "input_meta": {
            "relation_review_decisions": [
                {
                    "focus_id": "problem:one",
                    "action": "merge",
                    "target_ids": ["problem:two"],
                }
            ]
        },
    }
    corrected_registry = {
        "cases": {
            "case:one": {
                "case_id": "case:one",
                "canonical_problem_id": "problem:one",
                "problem_ids": ["problem:one", "problem:two"],
                "absorbed_case_ids": ["case:two"],
            }
        }
    }
    def corrected_result(**_kwargs: Any) -> dict[str, Any]:
        return {
            "status": "corrected",
            "stage_doc": corrected_stage1,
            "problem_records": corrected_stage1["items"],
            "atoms": [
                {"atom_id": "atom:one"},
                {"atom_id": "atom:two"},
            ],
            "case_registry": corrected_registry,
            "validation_errors": [],
            "attempt_record": {"elapsed_seconds": 1.0},
            "agent_session_id": route["agent_session_id"],
            "workspace_dir": route["workspace_dir"],
        }
    monkeypatch.setattr(
        runtime,
        (
            "continue_problem_relation_review_from_independent_feedback"
            if adapter == "relation_review"
            else "continue_problem_mining_from_independent_feedback"
        ),
        corrected_result,
    )
    stage2_calls: list[str] = []

    def stage2(**kwargs: Any) -> dict[str, Any]:
        pid = kwargs["problem_records"][0]["problem_id"]
        stage2_calls.append(pid)
        return _doc(
            "problem_prioritization",
            [{"problem_id": pid, "selected_for_research": True}],
        )

    monkeypatch.setattr(runtime, "_run_problem_prioritization_stage", stage2)
    monkeypatch.setattr(
        runtime,
        "_run_repro_research_stage",
        lambda **kwargs: _doc(
            "repro_research",
            [
                {
                    "problem_id": kwargs["problem_records"][0]["problem_id"],
                    "repo_workspace": str(tmp_path),
                }
            ],
        ),
    )
    for name, stage in (
        ("_run_solution_optioning_stage", "solution_optioning"),
        ("_run_solution_selection_stage", "solution_selection"),
    ):
        monkeypatch.setattr(
            runtime,
            name,
            lambda _stage=stage, **kwargs: _doc(
                _stage,
                [{"problem_id": kwargs["problem_records"][0]["problem_id"]}],
            ),
        )
    stage6_calls: list[dict[str, Any]] = []

    def stage6(**kwargs: Any) -> dict[str, Any]:
        stage6_calls.append(kwargs)
        return _doc(
            "implementation_planning",
            [{"problem_id": kwargs["problem_records"][0]["problem_id"]}],
        )

    monkeypatch.setattr(runtime, "_run_implementation_planning_stage", stage6)
    monkeypatch.setattr(runtime, "assemble_backlog_tickets", lambda **_kwargs: [])
    source_problem_records = [
        {
            "problem_id": "problem:one",
            "case_id": "case:one",
            "evidence_atom_ids": ["atom:one"],
        },
        {
            "problem_id": "problem:two",
            "case_id": "case:two",
            "evidence_atom_ids": ["atom:two"],
        },
    ]
    source_stage1 = _doc("problem_mining", source_problem_records)
    source_stage1["input_meta"] = {
        "relation_review_decisions": [
            {"focus_id": "problem:one", "action": "keep_separate"},
            {"focus_id": "problem:two", "action": "keep_separate"},
        ]
    }
    old_items = [
        {"problem_id": "problem:one", "value": "old-one"},
        {"problem_id": "problem:two", "value": "old-two"},
    ]
    result = runtime.run_stage456_qualification_repairs(
        routes=[route, stale_planner_route],
        source_pending_run_sha256="d" * 64,
        source_adjudication_sha256="e" * 64,
        repo_root=tmp_path,
        atoms=[{"atom_id": "atom:one"}, {"atom_id": "atom:two"}],
        stage1=source_stage1,
        stage2=_doc("problem_prioritization", old_items),
        stage3=_doc("repro_research", old_items),
        stage4=_doc("solution_optioning", old_items),
        stage5=_doc("solution_selection", old_items),
        stage6=_doc("implementation_planning", old_items),
        pipeline_manifest=type(
            "Manifest",
            (),
            {"load_stage_guidance": lambda _self, stage: f"guidance:{stage}"},
        )(),
        repair_artifacts_dir=tmp_path / "relation-merge",
        agent="codex",
        model=None,
        cfg=object(),
        breadth_profile="standard",
        repo_input=str(tmp_path),
        research_ref="dev",
        replay_executor=object(),
        case_registry={
            "cases": {
                "case:one": {
                    "case_id": "case:one",
                    "canonical_problem_id": "problem:one",
                    "problem_ids": ["problem:one"],
                },
                "case:two": {
                    "case_id": "case:two",
                    "canonical_problem_id": "problem:two",
                    "problem_ids": ["problem:two"],
                },
            }
        },
    )

    assert result.affected_problem_ids == ["problem:one", "problem:two"]
    assert stage2_calls == ["problem:one"]
    planner_receipt = next(
        receipt
        for receipt in result.consumption["route_receipts"]
        if receipt["route_sha256"] == stale_planner_route["route_sha256"]
    )
    assert planner_receipt["status"] == "retained_pending_not_invoked"
    assert planner_receipt["attempts"] == []
    assert stage6_calls
    assert all(
        call.get("external_corrections_by_problem") is None for call in stage6_calls
    )
    assert result.stage_documents["problem_mining"]["items"] == corrected_stage1["items"]
    for stage in (
        "problem_prioritization",
        "repro_research",
        "solution_optioning",
        "solution_selection",
        "implementation_planning",
    ):
        assert [
            item["problem_id"] for item in result.stage_documents[stage]["items"]
        ] == ["problem:one"]


def test_stage1_scheduler_advances_after_first_author_group_stalls(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    routes: list[dict[str, Any]] = []
    for suffix in ("one", "two"):
        route = _route_for_stage(
            "problem_mining",
            output_kind="relation",
            target_identity=f"relation:{suffix}",
            downstream=[
                "problem_mining",
                "problem_prioritization",
                "repro_research",
                "solution_optioning",
                "solution_selection",
                "implementation_planning",
                "ticket_assembly",
            ],
        )
        route.pop("route_sha256")
        route.update(
            {
                "agent_session_id": f"session:{suffix}",
                "workspace_dir": f"C:/workspace/{suffix}",
                "actionable_label_ids": [f"label:{suffix}"],
            }
        )
        route["author_provenance"].update(
            {
                "problem_id": f"problem:{suffix}",
                "case_id": f"case:{suffix}",
                "agent_session_id": f"session:{suffix}",
                "workspace_dir": f"C:/workspace/{suffix}",
                "author_role": "relation_reviewer",
                "stage1_correction_adapter": "relation_review",
                "relation_review_batch_tag": f"batch:{suffix}",
                "relation_review_focus_ids": [f"problem:{suffix}"],
            }
        )
        route["route_sha256"] = _hash(route)
        routes.append(route)
    first = min(routes, key=lambda item: item["route_sha256"])
    second = next(item for item in routes if item is not first)
    calls: list[str] = []

    def stage1(**kwargs: Any) -> dict[str, Any]:
        provenance = kwargs["author_provenance"]
        session = provenance["agent_session_id"]
        calls.append(session)
        failed = session == first["agent_session_id"]
        records = [
            {"problem_id": "problem:one", "case_id": "case:one"},
            {"problem_id": "problem:two", "case_id": "case:two"},
        ]
        return {
            "status": (
                "repairable_paused:independent_finding_remains"
                if failed
                else "corrected"
            ),
            "stage_doc": _doc("problem_mining", records),
            "problem_records": records,
            "atoms": [],
            "case_registry": {"cases": {}},
            "validation_errors": (
                ["independent_finding_remains"] if failed else []
            ),
            "attempt_record": {"elapsed_seconds": 1.0},
            "agent_session_id": session,
            "workspace_dir": provenance["workspace_dir"],
        }

    monkeypatch.setattr(
        runtime,
        "continue_problem_relation_review_from_independent_feedback",
        stage1,
    )
    monkeypatch.setattr(
        runtime,
        "_run_problem_prioritization_stage",
        lambda **kwargs: _doc(
            "problem_prioritization",
            [
                {
                    "problem_id": kwargs["problem_records"][0]["problem_id"],
                    "selected_for_research": True,
                }
            ],
        ),
    )
    monkeypatch.setattr(
        runtime,
        "_run_repro_research_stage",
        lambda **kwargs: _doc(
            "repro_research",
            [
                {
                    "problem_id": kwargs["problem_records"][0]["problem_id"],
                    "repo_workspace": str(tmp_path),
                }
            ],
        ),
    )
    for name, stage in (
        ("_run_solution_optioning_stage", "solution_optioning"),
        ("_run_solution_selection_stage", "solution_selection"),
        ("_run_implementation_planning_stage", "implementation_planning"),
    ):
        monkeypatch.setattr(
            runtime,
            name,
            lambda _stage=stage, **kwargs: _doc(
                _stage,
                [{"problem_id": kwargs["problem_records"][0]["problem_id"]}],
            ),
        )
    monkeypatch.setattr(runtime, "assemble_backlog_tickets", lambda **_kwargs: [])
    source_records = [
        {"problem_id": "problem:one", "case_id": "case:one"},
        {"problem_id": "problem:two", "case_id": "case:two"},
    ]
    result = runtime.run_stage456_qualification_repairs(
        routes=routes,
        source_pending_run_sha256="d" * 64,
        source_adjudication_sha256="e" * 64,
        repo_root=tmp_path,
        atoms=[],
        stage1=_doc("problem_mining", source_records),
        stage2=_doc("problem_prioritization", []),
        stage3=_doc("repro_research", []),
        stage4=_doc("solution_optioning", []),
        stage5=_doc("solution_selection", []),
        stage6=_doc("implementation_planning", []),
        pipeline_manifest=type(
            "Manifest",
            (),
            {"load_stage_guidance": lambda _self, stage: f"guidance:{stage}"},
        )(),
        repair_artifacts_dir=tmp_path / "stage1-failover",
        agent="codex",
        model=None,
        cfg=object(),
        breadth_profile="standard",
        repo_input=str(tmp_path),
        research_ref="dev",
        replay_executor=object(),
        case_registry={"cases": {}},
    )

    assert calls[0] == first["agent_session_id"]
    assert second["agent_session_id"] in calls
    assert calls.index(second["agent_session_id"]) > 0
    assert result.consumption["accepted_repair_count"] == 1
    assert result.consumption["downstream_result"]["stage1_group_attempt_count"] == 2
    assert result.consumption["downstream_result"]["stage1_group_accepted"] is True
    receipts = {
        item["route_sha256"]: item for item in result.consumption["route_receipts"]
    }
    assert receipts[first["route_sha256"]]["status"] != "accepted"
    assert receipts[second["route_sha256"]]["status"] in {"accepted", "corrected"}


def _causal_route(
    *,
    stage: str,
    problem_id: str,
    case_id: str,
    session: str,
    adapter: str | None = None,
) -> dict[str, Any]:
    route = _route_for_stage(
        stage,
        output_kind=("problem" if stage == "problem_mining" else "plan"),
        target_identity=f"{stage}:{problem_id}",
        downstream=[stage, "ticket_assembly"],
    )
    route.update(
        {
            "agent_session_id": session,
            "workspace_dir": f"C:/workspace/{session}",
            "actionable_label_ids": [],
            "author_attempt_identity": {
                "attempt_number": 1,
                "response_sha256": _hash(session),
            },
            "causal_target": {
                "problem_ids": [problem_id],
                "case_ids": [case_id],
                "evidence_atom_ids": [],
                "actionable_label_ids": [],
                "expected_item_keys": [f"problem:{problem_id}"],
            },
        }
    )
    route["author_provenance"] = {
        **route["author_provenance"],
        "authoring_stage": stage,
        "problem_id": problem_id,
        "case_id": case_id,
        "agent_session_id": session,
        "workspace_dir": route["workspace_dir"],
        "repository_revision": "a" * 40,
        "workspace_manifest_sha256": "b" * 64,
        **(
            {"stage1_correction_adapter": adapter, "miner_tag": f"miner:{session}"}
            if adapter is not None
            else {}
        ),
    }
    return _rehash(route)


def test_stage1_failure_blocks_same_case_authors_but_advances_disjoint_work(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    primary = _causal_route(
        stage="problem_mining",
        problem_id="problem:one",
        case_id="case:one",
        session="primary-one",
        adapter="problem_miner",
    )
    overlapping_review = _causal_route(
        stage="problem_mining",
        problem_id="problem:one",
        case_id="case:one",
        session="review-one",
        adapter="coverage_review",
    )
    disjoint = _causal_route(
        stage="problem_mining",
        problem_id="problem:two",
        case_id="case:two",
        session="primary-two",
        adapter="problem_miner",
    )
    downstream = _causal_route(
        stage="implementation_planning",
        problem_id="problem:one",
        case_id="case:one",
        session="planner-one",
    )
    routes = [primary, overlapping_review, disjoint, downstream]
    invoked: list[list[str]] = []

    def paused_consumption(**kwargs: Any) -> dict[str, Any]:
        group_routes = kwargs["routes"]
        invoked.append([route["route_sha256"] for route in group_routes])
        return {
            "route_receipts": [
                {
                    "route_sha256": route["route_sha256"],
                    "status": "repairable_paused:independent_finding_remains",
                    "attempts": [{"attempt_number": 1}],
                }
                for route in group_routes
            ],
            "accepted_repair_count": 0,
            "accepted_repair_group_count": 0,
            "unresolved_route_count": len(group_routes),
            "rerun_downstream_stages": [],
            "downstream_result": {},
        }

    monkeypatch.setattr(runtime, "consume_qualification_corrections", paused_consumption)
    monkeypatch.setattr(runtime, "assemble_backlog_tickets", lambda **_kwargs: [])
    result = runtime.run_stage456_qualification_repairs(
        routes=routes,
        source_pending_run_sha256="d" * 64,
        source_adjudication_sha256="e" * 64,
        repo_root=tmp_path,
        atoms=[],
        stage1=_doc(
            "problem_mining",
            [
                {"problem_id": "problem:one", "case_id": "case:one"},
                {"problem_id": "problem:two", "case_id": "case:two"},
            ],
        ),
        stage2=_doc("problem_prioritization", []),
        stage3=_doc("repro_research", []),
        stage4=_doc("solution_optioning", []),
        stage5=_doc("solution_selection", []),
        stage6=_doc("implementation_planning", []),
        pipeline_manifest=type(
            "Manifest",
            (),
            {"load_stage_guidance": lambda _self, stage: f"guidance:{stage}"},
        )(),
        repair_artifacts_dir=tmp_path / "causal-scheduler",
        agent="codex",
        model=None,
        cfg=object(),
        breadth_profile="standard",
        case_registry={"cases": {}},
    )

    invoked_routes = {route_sha for group in invoked for route_sha in group}
    assert invoked_routes == {primary["route_sha256"], disjoint["route_sha256"]}
    receipts = {
        item["route_sha256"]: item for item in result.consumption["route_receipts"]
    }
    assert receipts[overlapping_review["route_sha256"]]["attempts"] == []
    assert receipts[downstream["route_sha256"]]["attempts"] == []
    assert receipts[overlapping_review["route_sha256"]]["status"] == (
        "retained_pending_not_invoked"
    )


def test_provider_wait_stops_all_qualification_groups_after_one_invocation(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    first = _causal_route(
        stage="problem_mining",
        problem_id="problem:one",
        case_id="case:one",
        session="miner-one",
        adapter="problem_miner",
    )
    second = _causal_route(
        stage="problem_mining",
        problem_id="problem:two",
        case_id="case:two",
        session="miner-two",
        adapter="problem_miner",
    )
    invocations: list[list[str]] = []
    wait = {
        "schema_version": 1,
        "status": "parked_external_wait",
        "scope": "backlog_model_pipeline",
        "reason": "codex_chatgpt_subscription_usage_limit",
        "provider": "codex",
        "state": "parked",
        "retry_mode": "resume_same_session",
        "route": "chatgpt_subscription",
        "api_fallback_allowed": False,
    }

    def park(**kwargs: Any) -> dict[str, Any]:
        invocations.append(
            [str(route["route_sha256"]) for route in kwargs["routes"]]
        )
        raise runtime.BacklogProviderExternalWait(wait)

    monkeypatch.setattr(runtime, "consume_qualification_corrections", park)
    monkeypatch.setattr(runtime, "assemble_backlog_tickets", lambda **_kwargs: [])
    result = runtime.run_stage456_qualification_repairs(
        routes=[first, second],
        source_pending_run_sha256="d" * 64,
        source_adjudication_sha256="e" * 64,
        repo_root=tmp_path,
        atoms=[],
        stage1=_doc(
            "problem_mining",
            [
                {"problem_id": "problem:one", "case_id": "case:one"},
                {"problem_id": "problem:two", "case_id": "case:two"},
            ],
        ),
        stage2=_doc("problem_prioritization", []),
        stage3=_doc("repro_research", []),
        stage4=_doc("solution_optioning", []),
        stage5=_doc("solution_selection", []),
        stage6=_doc("implementation_planning", []),
        pipeline_manifest=type(
            "Manifest",
            (),
            {"load_stage_guidance": lambda _self, stage: f"guidance:{stage}"},
        )(),
        repair_artifacts_dir=tmp_path / "provider-global-stop",
        agent="codex",
        model=None,
        cfg=object(),
        breadth_profile="standard",
        case_registry={"cases": {}},
    )

    assert len(invocations) == 1
    assert len(invocations[0]) == 1
    assert result.consumption["status"] == "parked_external_wait"
    assert result.consumption["external_wait"] == wait
    assert result.consumption["accepted_repair_count"] == 0
    assert result.consumption["pending_not_invoked_group_count"] == 2
    assert result.consumption["pending_not_invoked_route_count"] == 2
    assert all(
        receipt["status"] == "retained_pending_not_invoked"
        and receipt["attempts"] == []
        for receipt in result.consumption["route_receipts"]
    )


@pytest.mark.parametrize("lineage_field", ["alias_of", "duplicate_of"])
def test_causal_plan_resolves_old_registry_lineage_without_labels(
    lineage_field: str,
) -> None:
    upstream = _causal_route(
        stage="problem_mining",
        problem_id="problem:old-relation-id",
        case_id="case:old",
        session="unavailable-stage1",
        adapter="relation_review",
    )
    upstream.update(
        {
            "route_status": "author_provenance_unavailable",
            "agent_session_id": None,
            "workspace_dir": None,
            "author_provenance": None,
            "actionable_label_ids": [],
            "causal_target": {
                "problem_ids": ["problem:old-relation-id"],
                "case_ids": [],
                "evidence_atom_ids": [],
                "actionable_label_ids": [],
                "expected_item_keys": ["problem:old-relation-id"],
            },
        }
    )
    _rehash(upstream)
    downstream = _causal_route(
        stage="implementation_planning",
        problem_id="problem:canonical",
        case_id="case:canonical",
        session="planner",
    )
    stage1 = _doc(
        "problem_mining",
        [
            {
                "problem_id": "problem:canonical",
                "canonical_problem_id": "problem:canonical",
                "case_id": "case:canonical",
                "case_member_problem_ids": [
                    "problem:canonical",
                    "problem:old-relation-id",
                ],
            }
        ],
    )
    registry = {
        "problem_id_to_case_id": {
            "problem:old-relation-id": "case:old",
            "problem:canonical": "case:canonical",
        },
        "cases": {
            "case:old": {
                "case_id": "case:old",
                "state": "alias",
                lineage_field: "case:canonical",
                "problem_ids": ["problem:old-relation-id"],
            },
            "case:canonical": {
                "case_id": "case:canonical",
                "canonical_problem_id": "problem:canonical",
                "problem_ids": [
                    "problem:canonical",
                    "problem:old-relation-id",
                ],
            },
        },
    }

    plan = runtime.plan_qualification_repair_route_groups(
        [downstream, upstream],
        stage1=stage1,
        case_registry=registry,
    )
    by_route = {
        route_sha: group
        for group in plan
        for route_sha in group["route_sha256s"]
    }
    assert by_route[upstream["route_sha256"]]["invocable"] is False
    assert by_route[upstream["route_sha256"]]["disposition"] == (
        "selected_causal_frontier"
    )
    assert by_route[downstream["route_sha256"]]["disposition"] == (
        "retained_pending_causal_predecessor"
    )
    assert by_route[downstream["route_sha256"]]["component_id"] == by_route[
        upstream["route_sha256"]
    ]["component_id"]


def test_problem_merge_preserves_sibling_author_meta_and_scopes_repair_provenance() -> None:
    original = {
        "stage": "solution_selection",
        "items": [
            {"problem_id": "problem:a", "selected_option_id": "option:a-old"},
            {"problem_id": "problem:b", "selected_option_id": "option:b-old"},
            {"problem_id": "problem:sibling", "selected_option_id": "option:sibling"},
        ],
        "input_meta": {
            "role_healing_runs": [{"problem_id": "problem:sibling", "session": "sibling"}],
            "shared_author_marker": "original",
        },
        "artifacts": {"shared_response": "original-response.json"},
    }
    replacement_a = {
        "stage": "solution_selection",
        "items": [{"problem_id": "problem:a", "selected_option_id": "option:a-new"}],
        "input_meta": {
            "role_healing_runs": [{"problem_id": "problem:a", "session": "repair-a"}]
        },
        "artifacts": {"shared_response": "repair-a.json"},
    }
    replacement_b = {
        "stage": "solution_selection",
        "items": [{"problem_id": "problem:b", "selected_option_id": "option:b-new"}],
        "input_meta": {
            "role_healing_runs": [{"problem_id": "problem:b", "session": "repair-b"}]
        },
        "artifacts": {"shared_response": "repair-b.json"},
    }

    first = runtime._merge_problem_items(
        original,
        replacement_a,
        problem_ids={"problem:a"},
        repair_receipts=[],
    )
    second = runtime._merge_problem_items(
        first,
        replacement_b,
        problem_ids={"problem:b"},
        repair_receipts=[],
    )

    assert second["input_meta"]["shared_author_marker"] == "original"
    assert second["input_meta"]["role_healing_runs"] == [
        {"problem_id": "problem:sibling", "session": "sibling"}
    ]
    assert second["artifacts"]["shared_response"] == "original-response.json"
    assert second["artifacts"]["qualification_repair_artifacts_by_problem"] == {
        "problem:a": {"shared_response": "repair-a.json"},
        "problem:b": {"shared_response": "repair-b.json"},
    }
    a_meta, a_frontier = shadow_validation._retained_repair_author_meta(
        second,
        problem_ids={"problem:a"},
    )
    b_meta, b_frontier = shadow_validation._retained_repair_author_meta(
        second,
        problem_ids={"problem:b"},
    )
    sibling_meta, sibling_frontier = shadow_validation._retained_repair_author_meta(
        second,
        problem_ids={"problem:sibling"},
    )
    assert a_meta["role_healing_runs"][0]["session"] == "repair-a"
    assert b_meta["role_healing_runs"][0]["session"] == "repair-b"
    assert a_frontier is not None and b_frontier is not None
    assert sibling_meta["shared_author_marker"] == "original"
    assert sibling_frontier is None


def test_stage1_evidence_receipt_tracks_explicit_correction_recency_p1_p2_p1(
    tmp_path: Path,
) -> None:
    evidence_paths: dict[str, Path] = {}
    for marker in ("p1-v1", "p2-v1", "p1-v2"):
        path = tmp_path / f"{marker}.json"
        path.write_text(json.dumps({"marker": marker}) + "\n", encoding="utf-8")
        evidence_paths[marker] = path
    source = {
        "stage": "problem_mining",
        "items": [
            {"problem_id": "problem:p1"},
            {"problem_id": "problem:p2"},
        ],
        "input_meta": {},
        "artifacts": {},
    }

    def replacement(problem_id: str, marker: str) -> dict[str, Any]:
        return {
            "stage": "problem_mining",
            "items": [{"problem_id": problem_id}],
            "input_meta": {},
            "artifacts": {
                "problem_mining_evidence_receipt": str(evidence_paths[marker])
            },
        }

    p1_first = runtime._merge_problem_items(
        source,
        replacement("problem:p1", "p1-v1"),
        problem_ids={"problem:p1"},
        repair_receipts=[],
    )
    p2 = runtime._merge_problem_items(
        p1_first,
        replacement("problem:p2", "p2-v1"),
        problem_ids={"problem:p2"},
        repair_receipts=[],
    )
    p1_latest = runtime._merge_problem_items(
        p2,
        replacement("problem:p1", "p1-v2"),
        problem_ids={"problem:p1"},
        repair_receipts=[],
    )

    assert p1_latest["artifacts"][
        "qualification_repair_current_evidence_receipt"
    ] == str(evidence_paths["p1-v2"])
    assert p1_latest["artifacts"]["qualification_repair_artifacts_by_problem"][
        "problem:p2"
    ]["problem_mining_evidence_receipt"] == str(evidence_paths["p2-v1"])


def test_later_stage_acceptance_carries_current_stage1_evidence_and_registry(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    route = _route()
    evidence_path = tmp_path / "latest-full-corpus-evidence.json"
    evidence_doc = {"marker": "latest-stage1-full-corpus"}
    evidence_path.write_text(json.dumps(evidence_doc) + "\n", encoding="utf-8")
    corrected_registry = {
        "cases": {
            "case:one": {
                "case_id": "case:one",
                "canonical_problem_id": "problem:one",
                "problem_ids": ["problem:one"],
                "marker": "corrected-stage1-registry",
            }
        }
    }

    monkeypatch.setattr(
        runtime,
        "_run_solution_selection_stage",
        lambda **_kwargs: {
            "stage": "solution_selection",
            "items": [
                {
                    "problem_id": "problem:one",
                    "case_id": "case:one",
                    "selected_option_id": "option:fixed",
                }
            ],
            "input_meta": {
                "role_healing_runs": [
                    {
                        "problem_id": "problem:one",
                        "role": "selector",
                        "attempt_history": [
                            {
                                "status": "verified",
                                "agent_session_id": route["agent_session_id"],
                                "workspace_dir": route["workspace_dir"],
                            }
                        ],
                    }
                ]
            },
        },
    )
    monkeypatch.setattr(
        runtime,
        "_run_implementation_planning_stage",
        lambda **_kwargs: _doc(
            "implementation_planning",
            [{"problem_id": "problem:one", "plan_revision_id": "plan:fixed"}],
        ),
    )
    monkeypatch.setattr(runtime, "assemble_backlog_tickets", lambda **_kwargs: [])
    stage1 = _doc(
        "problem_mining",
        [{"problem_id": "problem:one", "case_id": "case:one"}],
    )
    stage1["artifacts"] = {
        "qualification_repair_current_evidence_receipt": str(evidence_path)
    }

    result = runtime.run_stage456_qualification_repairs(
        routes=[route],
        source_pending_run_sha256="d" * 64,
        source_adjudication_sha256="e" * 64,
        repo_root=tmp_path,
        atoms=[{"atom_id": "atom:one", "disposition": "supports_case"}],
        stage1=stage1,
        stage2=_doc(
            "problem_prioritization",
            [{"problem_id": "problem:one", "selected_for_research": True}],
        ),
        stage3=_doc(
            "repro_research",
            [
                {
                    "problem_id": "problem:one",
                    "case_id": "case:one",
                    "repo_workspace": str(tmp_path),
                }
            ],
        ),
        stage4=_doc(
            "solution_optioning",
            [{"problem_id": "problem:one", "option_id": "option:one"}],
        ),
        stage5=_doc(
            "solution_selection",
            [{"problem_id": "problem:one", "selected_option_id": "option:old"}],
        ),
        stage6=_doc(
            "implementation_planning",
            [{"problem_id": "problem:one", "plan_revision_id": "plan:old"}],
        ),
        pipeline_manifest=type(
            "Manifest",
            (),
            {"load_stage_guidance": lambda _self, stage: f"guidance:{stage}"},
        )(),
        repair_artifacts_dir=tmp_path / "later-stage-carry",
        agent="codex",
        model=None,
        cfg=object(),
        breadth_profile="standard",
        case_registry=corrected_registry,
    )

    receipt_by_stage = {
        receipt["stage"]: receipt
        for receipt in result.consumption["downstream_result"][
            "materialized_stage_receipts"
        ]
    }
    assert Path(receipt_by_stage["problem_mining_evidence"]["path"]) == evidence_path
    registry_path = Path(receipt_by_stage["case_registry"]["path"])
    assert json.loads(registry_path.read_text(encoding="utf-8")) == corrected_registry
