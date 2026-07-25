from __future__ import annotations

import json
import sys
from hashlib import sha256
from pathlib import Path
from typing import Any

import pytest
from backlog_core.case_lineage import (
    atom_is_idea_originated,
    eligible_problem_mining_atoms,
    normalize_atom_lineage,
)
from backlog_core.relation_review import canonicalize_problem_cases
from backlog_core.stage_contracts import (
    assess_research_readiness,
    parse_change_plan_list,
    parse_problem_record_list,
    parse_research_dossier_list,
)
from backlog_miner.pipeline import load_pipeline_prompt_manifest
from backlog_repo import (
    outcome_suppresses_new_case_discovery,
    ticket_export_fingerprint,
    validate_outcome_record,
    write_case_relation_receipt,
)
from runner_core import RunnerConfig
from runner_core.runner import _run_verification_commands
from runner_core.shell_capability import _resolve_shell_capability

from usertest_backlog.workflows.depth_contracts import change_plan_quality_errors
from usertest_backlog.workflows.prioritization import (
    _apply_provisional_research_unit_schedule,
    _enforce_full_drain_research_policy,
    _priority_response_projection,
    _research_dispatch_sort_key,
    _runner_research_route,
    _server_normalize_priority_decisions,
)
from usertest_backlog.workflows.problem_mining import (
    _run_problem_mining_stage,
    _verified_relation_edges_from_case_registry,
)

_BENCHMARK_PATH = Path(__file__).parent / "fixtures" / "historical_depth_benchmark.json"
_BENCHMARK = json.loads(_BENCHMARK_PATH.read_text(encoding="utf-8"))
_CASES = {str(item["benchmark_id"]): item for item in _BENCHMARK["cases"] if isinstance(item, dict)}
_REPO_ROOT = Path(__file__).resolve().parents[3]


def _benchmark_case(benchmark_id: str) -> dict[str, Any]:
    return dict(_CASES[benchmark_id])


def _canonical_sha256(value: object) -> str:
    return sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _source_atoms(benchmark_id: str) -> list[dict[str, Any]]:
    case = _benchmark_case(benchmark_id)
    return [
        dict(item["snapshot"])
        for item in case["source_evidence"]
        if isinstance(item, dict)
        and isinstance(item.get("snapshot"), dict)
        and isinstance(item["snapshot"].get("atom_id"), str)
    ]


def _run_dry_problem_mining(
    tmp_path: Path,
    *,
    atoms: list[dict[str, Any]],
    case_registry: dict[str, Any] | None = None,
) -> dict[str, Any]:
    manifest = load_pipeline_prompt_manifest(_REPO_ROOT / "configs" / "backlog_prompts")
    return _run_problem_mining_stage(
        repo_root=_REPO_ROOT,
        atoms=atoms,
        pipeline_manifest=manifest,
        artifacts_dir=tmp_path / "artifacts",
        out_json=tmp_path / "compiled" / "problem_records.json",
        out_md=tmp_path / "compiled" / "problem_records.md",
        agent="codex",
        model=None,
        cfg=RunnerConfig(
            repo_root=_REPO_ROOT,
            runs_dir=tmp_path / "runs",
            agents={},
            policies={},
        ),
        dry_run=True,
        stage_guidance_text="Mine concrete observed failures without inferring root cause.",
        case_registry=case_registry,
    )


def _outcome(case_id: str, state: str, *, requires_live: bool) -> dict[str, object]:
    return {
        "schema_version": 1,
        "case_id": case_id,
        "plan_revision_id": f"plan:{case_id}:v1",
        "state": state,
        "recorded_at": "2026-07-09T00:00:00Z",
        "requires_live_verification": requires_live,
        "target_branch": None,
        "merged_commit": None,
        "test_evidence": [],
        "original_scenario_evidence": [],
        "live_evidence": [],
        "remaining_risks": ["Historical outcome proof is incomplete."],
        "recurrence_check": {"status": "not_run"},
    }


def test_benchmark_is_hash_bound_non_idea_evidence_with_honest_integrity_labels() -> None:
    assert _BENCHMARK["schema_version"] == 2
    assert len(_CASES) == 7
    assert _BENCHMARK["captured_from"]["problem_records"].endswith("usertest.problem_records.json")
    manifest = _BENCHMARK["source_manifest"]
    assert manifest["raw_corpus_status"] == "partially_unavailable_integrity_unknown"
    assert manifest["excluded_origins"] == ["IDEA", "external_idea"]
    assert len(manifest["captured_artifact_bindings"]) == 3
    assert all(
        len(binding["sha256"]) == 64
        and binding["size_bytes"] > 0
        and binding["retention"] == "observed_in_external_source_checkout_not_vendored"
        for binding in manifest["captured_artifact_bindings"]
    )
    assert manifest["cases_sha256"] == _canonical_sha256(_BENCHMARK["cases"])
    assert manifest["retained_replay_sha256"] == _canonical_sha256(_BENCHMARK["retained_replay"])
    assert {item["kind"] for item in _CASES.values()} == {
        "canonical_grouping",
        "derived_lineage",
        "research_gate",
        "outcome_gate",
        "plan_revision",
    }
    for case in _CASES.values():
        expected_case_hash = case["case_sha256"]
        case_without_hash = {key: value for key, value in case.items() if key != "case_sha256"}
        assert expected_case_hash == _canonical_sha256(case_without_hash)
        assert case["source_evidence"]
        for evidence in case["source_evidence"]:
            assert evidence["origin_category"] != "IDEA"
            assert evidence["snapshot_sha256"] == _canonical_sha256(evidence["snapshot"])
            assert atom_is_idea_originated(evidence["snapshot"]) is False


def test_priority_bucket_cannot_permanently_suppress_a_canonical_problem() -> None:
    decisions = [
        {
            "problem_id": "problem:single-run-real-failure",
            "priority_bucket": "watch",
            "selected_for_research": False,
            "priority_status": "prioritized",
            "research_route": "research_new",
        },
        {
            "problem_id": "problem:malformed-output",
            "priority_bucket": "watch",
            "selected_for_research": False,
            "priority_status": "prioritized",
            "research_route": "await_evidence",
            "reconsider_when": "New source evidence changes the frontier.",
        },
    ]

    _enforce_full_drain_research_policy(decisions)

    assert decisions[0]["selected_for_research"] is True
    assert decisions[1]["selected_for_research"] is False
    assert {decision["priority_status"] for decision in decisions} == {"prioritized"}


def test_priority_model_cannot_block_or_omit_a_canonical_case() -> None:
    records = [
        {
            "problem_id": "problem:blocked",
            "evidence_atom_ids": ["atom:blocked"],
        },
        {
            "problem_id": "problem:omitted",
            "evidence_atom_ids": ["atom:omitted"],
            "_carried_forward_case": True,
            "prior_stage_context": {
                "research": {
                    "current": {
                        "research_status": "blocked",
                        "root_cause_status": "blocked",
                        "blocking_reasons": ["required_artifact_unavailable"],
                    }
                }
            },
        },
    ]
    normalized, warnings = _server_normalize_priority_decisions(
        decisions=[
            {
                "problem_id": "problem:blocked",
                "priority_bucket": "watch",
                "selected_for_research": False,
                "priority_rationale": "The model attempted to defer it.",
                "priority_status": "blocked",
            }
        ],
        problem_records=records,
        signals_by_problem_id={
            "problem:blocked": {"bucket_candidate": "p2"},
            "problem:omitted": {"bucket_candidate": "p1"},
        },
    )

    assert [item["problem_id"] for item in normalized] == [
        "problem:blocked",
        "problem:omitted",
    ]
    assert normalized[0]["selected_for_research"] is True
    assert normalized[0]["research_route"] == "research_new"
    assert normalized[1]["selected_for_research"] is True
    assert normalized[1]["research_route"] == "reassess_actionability"
    assert normalized[1]["reconsider_when"] is None
    assert all(item["priority_status"] == "prioritized" for item in normalized)
    assert normalized[0]["model_priority_accepted"] is True
    assert normalized[1]["model_priority_accepted"] is False
    assert "prioritizer_missing_problem_id:problem:omitted" in warnings


def test_prioritizer_does_not_waste_correction_turn_on_runner_routed_wait() -> None:
    response = json.dumps(
        [
            {
                "problem_id": "problem:waiting",
                "priority_bucket": "watch",
                "selected_for_research": False,
                "priority_rationale": "The runner route names the missing evidence trigger.",
                "evidence_atom_ids_used": ["atom:waiting"],
                "priority_status": "prioritized",
            }
        ]
    )

    _parsed, errors, valid_keys = _priority_response_projection(
        response,
        problem_records=[
            {
                "problem_id": "problem:waiting",
                "evidence_atom_ids": ["atom:waiting"],
            }
        ],
    )

    assert errors == []
    assert valid_keys == ["priority_decision:problem:waiting"]


def test_legacy_malformed_research_gets_one_reassessment_then_waits_unchanged() -> None:
    record: dict[str, Any] = {
        "problem_id": "problem:legacy-malformed",
        "case_id": "case:legacy-malformed",
        "case_revision": 1,
        "evidence_atom_ids": ["atom:source"],
        "source_evidence_atom_ids": ["atom:source"],
        "_carried_forward_case": True,
        "prior_stage_context": {
            "research": {
                "current": {
                    "research_schema_version": 3,
                    "stage_snapshot_id": "stagesnap:before-reassessment",
                    "repo_revision": "abc123",
                    "research_status": "blocked",
                    "reproduction_status": "blocked",
                    "root_cause_status": "blocked",
                    "blocking_reasons": ["research_dossier_malformed:ValueError"],
                }
            }
        },
    }

    first = _runner_research_route(record)
    assert first["research_route"] == "reassess_actionability"
    assert first["selected_for_research"] is True

    record["prior_stage_context"]["artifact_refs"] = {
        "problem_prioritization": {
            "item_refs": [
                {
                    "problem_id": record["problem_id"],
                    "case_id": record["case_id"],
                    "research_route": first["research_route"],
                    "research_route_revision": first["research_route_revision"],
                    "research_frontier_sha256": first["research_frontier_sha256"],
                    "research_snapshot_id": first["research_snapshot_id"],
                }
            ]
        }
    }
    record["prior_stage_context"]["research"]["current"][
        "stage_snapshot_id"
    ] = "stagesnap:after-reassessment"
    second = _runner_research_route(record)

    assert second["research_route"] == "await_evidence"
    assert second["selected_for_research"] is False
    assert second["reconsider_when"]
    assert second["research_frontier_sha256"] == first["research_frontier_sha256"]


def test_research_dispatch_order_uses_route_bucket_score_then_identity() -> None:
    decisions = [
        {
            "problem_id": "problem:reassess",
            "case_id": "case:z",
            "research_route": "reassess_actionability",
            "priority_bucket": "p0",
            "pre_score": 99.0,
        },
        {
            "problem_id": "problem:new-low",
            "case_id": "case:b",
            "research_route": "research_new",
            "priority_bucket": "p1",
            "pre_score": 3.0,
        },
        {
            "problem_id": "problem:update",
            "case_id": "case:c",
            "research_route": "research_update",
            "priority_bucket": "p2",
            "pre_score": 1.0,
        },
        {
            "problem_id": "problem:new-high",
            "case_id": "case:a",
            "research_route": "research_new",
            "priority_bucket": "p1",
            "pre_score": 8.0,
        },
    ]

    assert [
        item["problem_id"] for item in sorted(decisions, key=_research_dispatch_sort_key)
    ] == [
        "problem:update",
        "problem:new-high",
        "problem:new-low",
        "problem:reassess",
    ]


def _provisional_priority_group_records() -> list[dict[str, Any]]:
    group = {
        "schema_version": 1,
        "status": "research_hypothesis",
        "group_id": "provisional:shell-panic",
        "member_case_ids": ["case:member", "case:unit"],
        "member_problem_ids": ["problem:member", "problem:unit"],
        "research_unit_case_id": "case:unit",
        "member_facets": [
            {
                "case_id": "case:member",
                "problem_id": "problem:member",
                "evidence_atom_ids": ["atom:member"],
                "source_evidence_atom_ids": ["atom:member"],
            },
            {
                "case_id": "case:unit",
                "problem_id": "problem:unit",
                "evidence_atom_ids": ["atom:unit"],
                "source_evidence_atom_ids": ["atom:unit"],
            },
        ],
    }
    return [
        {
            "problem_id": "problem:member",
            "case_id": "case:member",
            "case_identity_status": "provisional_same_cause",
            "case_identity_candidate_ids": ["case:member", "case:unit"],
            "case_member_problem_ids": ["problem:member", "problem:unit"],
            "evidence_atom_ids": ["atom:member", "atom:unit"],
            "source_evidence_atom_ids": ["atom:member", "atom:unit"],
            "provisional_same_cause_group": group,
        },
        {
            "problem_id": "problem:unit",
            "case_id": "case:unit",
            "case_identity_status": "provisional_same_cause",
            "case_identity_candidate_ids": ["case:member", "case:unit"],
            "case_member_problem_ids": ["problem:member", "problem:unit"],
            "evidence_atom_ids": ["atom:unit", "atom:member"],
            "source_evidence_atom_ids": ["atom:unit", "atom:member"],
            "provisional_same_cause_group": group,
        },
    ]


def test_provisional_same_cause_group_schedules_one_evidence_complete_unit() -> None:
    records = _provisional_priority_group_records()
    decisions = [
        {
            "problem_id": "problem:member",
            "research_route": "research_new",
            "selected_for_research": True,
            "eligible_for_downstream": True,
        },
        {
            "problem_id": "problem:unit",
            "research_route": "research_update",
            "selected_for_research": True,
            "eligible_for_downstream": True,
        },
    ]

    warnings = _apply_provisional_research_unit_schedule(
        decisions=decisions,
        problem_records=records,
    )

    assert warnings == []
    by_id = {decision["problem_id"]: decision for decision in decisions}
    unit = by_id["problem:unit"]
    member = by_id["problem:member"]
    assert unit["selected_for_research"] is True
    assert unit["research_route"] == "research_update"
    assert unit["provisional_research_schedule"] == {
        "schema_version": 1,
        "group_id": "provisional:shell-panic",
        "research_unit_case_id": "case:unit",
        "research_unit_problem_id": "problem:unit",
        "member_case_ids": ["case:member", "case:unit"],
        "member_problem_ids": ["problem:member", "problem:unit"],
        "source_evidence_atom_ids": ["atom:member", "atom:unit"],
        "status": "research_unit",
    }
    assert member["selected_for_research"] is False
    assert member["eligible_for_downstream"] is False
    assert member["research_route"] == "await_provisional_research_unit"
    assert member["individual_research_route"] == "research_new"
    assert member["individual_selected_for_research"] is True
    assert member["provisional_research_schedule"]["status"] == (
        "represented_by_research_unit"
    )
    assert set(unit["provisional_research_schedule"]["source_evidence_atom_ids"]) == {
        "atom:member",
        "atom:unit",
    }


def test_provisional_schedule_preserves_independent_work_when_unit_lacks_evidence() -> None:
    records = _provisional_priority_group_records()
    records[1]["evidence_atom_ids"] = ["atom:unit"]
    records[1]["source_evidence_atom_ids"] = ["atom:unit"]
    decisions = [
        {
            "problem_id": "problem:member",
            "research_route": "research_new",
            "selected_for_research": True,
        },
        {
            "problem_id": "problem:unit",
            "research_route": "research_update",
            "selected_for_research": True,
        },
    ]

    warnings = _apply_provisional_research_unit_schedule(
        decisions=decisions,
        problem_records=records,
    )

    assert any("research_unit_source_evidence_incomplete" in item for item in warnings)
    assert all(decision["selected_for_research"] is True for decision in decisions)
    assert all("provisional_research_schedule" not in decision for decision in decisions)


def test_provisional_schedule_does_not_transfer_nonunit_retained_research() -> None:
    records = _provisional_priority_group_records()
    decisions = [
        {
            "problem_id": "problem:member",
            "research_route": "research_update",
            "selected_for_research": True,
        },
        {
            "problem_id": "problem:unit",
            "research_route": "research_update",
            "selected_for_research": True,
        },
    ]

    warnings = _apply_provisional_research_unit_schedule(
        decisions=decisions,
        problem_records=records,
    )

    assert any(
        "nonunit_retained_research_state_requires_independent_dispatch" in item
        for item in warnings
    )
    assert all(decision["selected_for_research"] is True for decision in decisions)
    assert all("provisional_research_schedule" not in decision for decision in decisions)


def test_retained_artifacts_replay_through_current_depth_gates() -> None:
    """Replay immutable historical excerpts through real current parsers and gates."""
    replay = _BENCHMARK["retained_replay"]
    problems, problem_warnings = parse_problem_record_list(json.dumps([replay["problem_record"]]))
    research, research_warnings = parse_research_dossier_list(
        json.dumps([replay["research_dossier"]]),
        legacy=True,
    )
    plans, plan_warnings = parse_change_plan_list(json.dumps([replay["change_plan"]]))

    assert len(problems) == len(research) == len(plans) == 1
    assert problem_warnings == []
    assert research_warnings == []
    assert any("missing_required_field" in warning for warning in plan_warnings)

    research_ready, research_reasons = assess_research_readiness(research[0])
    plan_errors = change_plan_quality_errors(
        plans[0],
        expected_revision="retained-revision-unavailable",
        problem_record=problems[0],
        research_dossier=research[0],
    )

    assert research_ready is replay["expected_current_disposition"]["research_ready"]
    assert "research_proof_invalid" in research_reasons
    assert bool(plan_errors) is not replay["expected_current_disposition"]["plan_ready"]
    assert any("discovery_first_step" in error for error in plan_errors)
    assert any("missing_change_targets" in error for error in plan_errors)
    assert any("repo_revision_mismatch" in error for error in plan_errors)


def test_verification_path_research_crosses_production_mining_without_remining(
    tmp_path: Path,
) -> None:
    case = _benchmark_case("verification-artifact-paths")
    atoms = normalize_atom_lineage(
        _source_atoms("verification-artifact-paths"),
        case_registry={
            "problem_id_to_case_id": {case["problem_id"]: case["case_id"]},
            "atom_id_to_case_id": {case["source_atom_id"]: case["case_id"]},
            "ticket_fingerprint_to_case_id": {},
        },
        strict_new_output=True,
    )

    by_id = {atom["atom_id"]: atom for atom in atoms}
    derived = by_id[case["derived_atom_id"]]
    assert derived["case_id"] == case["case_id"]
    assert derived["disposition"] == case["expected_disposition"]
    assert len(eligible_problem_mining_atoms(atoms)) == case["expected_new_cases"]
    stage = _run_dry_problem_mining(
        tmp_path,
        atoms=atoms,
        case_registry={
            "problem_id_to_case_id": {case["problem_id"]: case["case_id"]},
            "atom_id_to_case_id": {
                case["source_atom_id"]: case["case_id"],
                case["derived_atom_id"]: case["case_id"],
            },
            "ticket_fingerprint_to_case_id": {},
        },
    )
    assert stage["items"] == []
    assert stage["input_meta"]["eligible_problem_origin_atom_count"] == 0

    # Execute the exact agent-readable path through the production verification runner.
    workspace = tmp_path / "verification-path-workspace"
    artifact = workspace / "agent-readable" / "verification.json"
    artifact.parent.mkdir(parents=True)
    artifact.write_text('{"status":"passed"}\n', encoding="utf-8")
    probe = workspace / "verify_artifact.py"
    probe.write_text(
        "import json\n"
        "from pathlib import Path\n"
        "payload = json.loads(Path('agent-readable/verification.json').read_text())\n"
        "assert payload['status'] == 'passed'\n"
        "print('agent_readable_path=ok')\n",
        encoding="utf-8",
    )
    summary = _run_verification_commands(
        run_dir=tmp_path / "verification-path-run",
        attempt_number=1,
        commands=["python verify_artifact.py"],
        command_prefix=[],
        cwd=workspace,
        timeout_seconds=None,
        python_executable=sys.executable,
        artifacts_dir_rel=Path("."),
    )
    assert summary["passed"] is True
    assert summary["commands"][0]["timed_out"] is False
    assert "agent_readable_path=ok" in summary["commands"][0]["stdout_tail"]


@pytest.mark.parametrize("benchmark_id", ["lifecycle-classification", "storage-exhaustion"])
def test_historical_similarity_forms_only_a_provisional_research_unit(
    benchmark_id: str,
) -> None:
    benchmark = _benchmark_case(benchmark_id)
    records = []
    for raw in benchmark["records"]:
        record = dict(raw)
        record["canonical_problem_id"] = record["problem_id"]
        record["case_member_problem_ids"] = [record["problem_id"]]
        records.append(record)
    first, second = records
    evidence_ids = [*first["evidence_atom_ids"], *second["evidence_atom_ids"]]
    decisions = [
        {
            "focus_id": item["problem_id"],
            "action": benchmark["relation"],
            "group_id": f"cause:{benchmark_id}",
            "member_ids": [peer["problem_id"]],
            "rationale": "A reviewer proposed one cause; the runner still requires identity proof.",
            "review_confidence": 0.95,
            "evidence_atom_ids": evidence_ids,
        }
        for item, peer in ((first, second), (second, first))
    ]

    canonical = canonicalize_problem_cases(records, decisions, strict_review=True)

    assert len(canonical) == 1
    unit = canonical[0]
    assert unit["case_identity_status"] == "provisional_same_cause"
    assert set(unit["case_identity_candidate_ids"]) == {
        first["case_id"],
        second["case_id"],
    }
    assert "absorbed_case_ids" not in unit
    assert "same_cause_group_id" not in unit
    assert {
        facet["case_id"]
        for facet in unit["provisional_same_cause_group"]["member_facets"]
    } == {first["case_id"], second["case_id"]}


def test_lifecycle_relation_applies_only_from_hash_verified_runner_receipt(
    tmp_path: Path,
) -> None:
    benchmark = _benchmark_case("lifecycle-classification")
    records = [
        {
            **raw,
            "canonical_problem_id": raw["problem_id"],
            "case_member_problem_ids": [raw["problem_id"]],
        }
        for raw in benchmark["records"]
    ]
    first, second = records
    response = tmp_path / "relation-review.response.json"
    response.write_text(
        json.dumps(
            {
                "decision": "same_cause_group",
                "source": second["case_id"],
                "target": first["case_id"],
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    _receipt, refs = write_case_relation_receipt(
        tmp_path / "relations.json",
        stage="repro_research",
        relation_review_response_path=response,
        relations=[
            {
                "source_case_id": second["case_id"],
                "target_case_id": first["case_id"],
                "direction": "source_to_canonical",
                "relation_kind": "canonical_absorption",
                "decision_actions": ["same_cause_group"],
            }
        ],
    )
    registry = {
        "cases": {
            first["case_id"]: {
                "case_id": first["case_id"],
                "incoming_relation_receipts": [refs[second["case_id"]]],
            },
            second["case_id"]: {
                "case_id": second["case_id"],
                "relation_receipt": refs[second["case_id"]],
            },
        }
    }
    edges = _verified_relation_edges_from_case_registry(registry)
    assert edges == {tuple(sorted((first["case_id"], second["case_id"])))}
    evidence_ids = [*first["evidence_atom_ids"], *second["evidence_atom_ids"]]
    decisions = [
        {
            "focus_id": item["problem_id"],
            "action": "same_cause_group",
            "group_id": "cause:lifecycle-classification",
            "member_ids": [peer["problem_id"]],
            "rationale": "The runner persisted the reviewed canonical absorption.",
            "review_confidence": 1.0,
            "evidence_atom_ids": evidence_ids,
        }
        for item, peer in ((first, second), (second, first))
    ]

    canonical = canonicalize_problem_cases(
        records,
        decisions,
        strict_review=True,
        verified_relation_edges=edges,
    )

    assert len(canonical) == benchmark["expected_with_verified_relation_receipt"]
    assert set(canonical[0]["case_member_problem_ids"]) == {
        first["problem_id"],
        second["problem_id"],
    }


@pytest.mark.parametrize("benchmark_id", ["lifecycle-classification", "windows-shell"])
def test_representative_observed_cases_cross_the_production_mining_boundary(
    tmp_path: Path,
    benchmark_id: str,
) -> None:
    atoms = _source_atoms(benchmark_id)

    stage = _run_dry_problem_mining(tmp_path, atoms=atoms)

    assert stage["input_meta"]["eligible_problem_origin_atom_count"] == len(atoms)
    cited = {atom_id for record in stage["items"] for atom_id in record["evidence_atom_ids"]}
    assert cited == {atom["atom_id"] for atom in atoms}
    assert all(record["_dry_run_synthesized"] is True for record in stage["items"])
    assert all(atom_is_idea_originated(atom) is False for atom in atoms)


def test_historical_apply_patch_excerpt_is_not_upgraded_into_research_proof() -> None:
    benchmark = _benchmark_case("apply-patch-context")
    ready, reasons = assess_research_readiness(
        {
            "problem_id": benchmark["problem_id"],
            "research_status": "insufficient_evidence",
            "reproduction_status": benchmark["historical_reproduction_status"],
            "material_unknowns": [
                {
                    "unknown": benchmark["historical_material_unknown"],
                    "affects": ["root_cause", "change_surface"],
                }
            ],
        }
    )

    assert ready is benchmark["expected_ready"]
    assert benchmark["historical_evidence_status"] == "retained_compiled_excerpt_only"
    assert "research_proof_invalid" in reasons


def test_shell_probe_failure_is_not_misreported_as_policy_block_or_resolution() -> None:
    benchmark = _benchmark_case("windows-shell")
    capability = _resolve_shell_capability(
        agent="claude",
        operating_system="Windows",
        backend="local",
        sandbox_mode="workspace-write",
        policy_status="allowed",
        policy_reason="The effective policy permits shell execution.",
        allowed_tools=["Bash"],
        probe_result={
            "kind": "agent_shell_payload",
            "ok": False,
            "exit_code": 1,
            "stderr_excerpt": "PowerShell process launch failed before payload execution.",
        },
    ).to_dict()

    assert capability["state"] == "blocked"
    assert capability["policy_status"] == "allowed"
    assert capability["reason_code"] == "shell_probe_failed"

    shell_outcome = _outcome(
        "case:windows-shell",
        benchmark["expected_current_outcome_state_without_provenance"],
        requires_live=benchmark["requires_live_verification"],
    )

    validated = validate_outcome_record(shell_outcome)
    assert validated["state"] == "unverified"
    assert (
        outcome_suppresses_new_case_discovery(validated)
        is benchmark["expected_suppresses_discovery"]
    )

    shell_outcome["state"] = "resolved"
    with pytest.raises(ValueError, match="outcome_record_evidence_required"):
        validate_outcome_record(shell_outcome)


def test_unverified_claude_stderr_recurrence_remains_open() -> None:
    benchmark = _benchmark_case("claude-empty-stderr")
    outcome = validate_outcome_record(
        _outcome(
            "case:claude-stderr",
            benchmark["outcome_state"],
            requires_live=benchmark["requires_live_verification"],
        )
    )

    assert (
        outcome_suppresses_new_case_discovery(outcome) is benchmark["expected_suppresses_discovery"]
    )


def test_python_toolchain_replanning_requires_explicit_revision() -> None:
    benchmark = _benchmark_case("python-toolchain")
    identities = {
        ticket_export_fingerprint(
            {
                "case_id": benchmark["case_id"],
                "plan_revision_id": revision,
            }
        )
        for revision in benchmark["plan_revisions"]
    }

    assert len(identities) == benchmark["expected_distinct_export_identities"]
