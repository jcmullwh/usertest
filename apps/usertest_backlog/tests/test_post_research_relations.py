from __future__ import annotations

import json
from copy import deepcopy
from hashlib import sha256
from pathlib import Path

from backlog_core.case_lineage import build_case_registry

from usertest_backlog.workflows.post_research_relations import (
    collapse_post_research_verified_mechanisms,
)
from usertest_backlog.workflows.problem_mining import _persist_canonical_relation_receipts


def _digest(value: object) -> str:
    return sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _provenance(*, slot: str = "keyword:request") -> dict[str, object]:
    return {
        "schema_version": 1,
        "primary_hypothesis_id": "h1",
        "mechanism_evidence_ids": ["mechanism:one"],
        "causal_control_ids": ["control:one"],
        "falsification_intervention_ids": [],
        "deterministic_closure_ids": [],
        "research_probe_control_points": [
            {
                "verification_method": "python_ast_explicit_argument_delta_v1",
                "mechanism_symbols": ["router.dispatch"],
                "mechanism_symbol": "router.dispatch",
                "slot": slot,
            }
        ],
    }


def _registry(case_ids: list[str]) -> tuple[dict[str, object], str]:
    mechanism = {
        "schema_version": 2,
        "mechanism_symbols": ["router.dispatch"],
        "code_paths": [{"symbol": "router.dispatch", "path": "src/router.py"}],
    }
    provenance = _provenance()
    identity = _digest(mechanism)
    cases = {
        case_id: {
            "case_id": case_id,
            "root_cause_status": "established",
            "verified_mechanism": mechanism,
            "verified_mechanism_sha256": identity,
            "verified_mechanism_provenance": provenance,
            "verified_mechanism_provenance_sha256": _digest(provenance),
            "verified_mechanism_receipt_sha256": "a" * 64,
            "verified_mechanism_source": "runner_research_evidence_verification_v1",
        }
        for case_id in case_ids
    }
    return {"schema_version": 1, "cases": cases}, identity


def _inputs(*, revisions: tuple[str, str] = ("abc", "abc")) -> dict[str, object]:
    registry, identity = _registry(["case:a", "case:b"])
    problems = [
        {
            "case_id": "case:a",
            "problem_id": "problem:a",
            "title": "CLI symptom",
            "problem": "The CLI drops the request.",
            "user_impact": "CLI work fails.",
            "evidence_atom_ids": ["atom:a"],
            "source_evidence_atom_ids": ["atom:a"],
            "canonical_symptoms": ["CLI request disappears"],
        },
        {
            "case_id": "case:b",
            "problem_id": "problem:b",
            "title": "Broker symptom",
            "problem": "The broker drops the same request.",
            "user_impact": "Broker work fails.",
            "evidence_atom_ids": ["atom:b"],
            "source_evidence_atom_ids": ["atom:b"],
            "canonical_symptoms": ["Broker request disappears"],
        },
    ]
    priorities = [
        {
            "case_id": case_id,
            "problem_id": problem_id,
            "priority_bucket": bucket,
            "selected_for_research": True,
            "priority_status": "prioritized",
            "priority_rationale": "Research this observed failure.",
        }
        for case_id, problem_id, bucket in (
            ("case:a", "problem:a", "p2"),
            ("case:b", "problem:b", "p1"),
        )
    ]
    dossiers = [
        {
            "case_id": case_id,
            "problem_id": problem_id,
            "repo_revision": revision,
            "evidence_verification": {
                "status": "verified",
                "verified_mechanism_sha256": identity,
                "verified_mechanism_provenance": _provenance(),
                "verified_mechanism_provenance_sha256": _digest(_provenance()),
                "control_verifications": [
                    {
                        "hypothesis_id": "h1",
                        "verification_method": "pytest_ast_controlled_difference_v2",
                        "mechanism_symbols": ["router.dispatch"],
                        "controlled_input_difference": {
                            "verification_method": "python_ast_explicit_argument_delta_v1",
                            "difference": {
                                "mechanism_symbol": "router.dispatch",
                                "slot": "keyword:request",
                                "difference_kind": "changed_value",
                            },
                        },
                        "observed_polarity": {
                            "verification_method": "runner_replay_falsification_polarity_v1",
                            "polarity": "failure_persists_after_intervention",
                            "baseline": {
                                "exit_code": 1,
                                "stdout_sha256": "1" * 64,
                                "stderr_sha256": "2" * 64,
                            },
                            "challenge": {
                                "exit_code": 1,
                                "stdout_sha256": "3" * 64,
                                "stderr_sha256": "4" * 64,
                            },
                        },
                        "relationship_sha256": "b" * 64,
                    }
                ],
                "falsification_interventions": [],
                "deterministic_mechanism_closures": [],
                "outcome_oracles": [
                    {"outcome_oracle_id": f"outcome:{case_id}", "case_id": case_id}
                ],
            },
        }
        for case_id, problem_id, revision in (
            ("case:a", "problem:a", revisions[0]),
            ("case:b", "problem:b", revisions[1]),
        )
    ]
    return {
        "problem_records": problems,
        "priority_decisions": priorities,
        "research_dossiers": dossiers,
        "case_registry": registry,
    }


def test_same_verified_mechanism_becomes_one_optioning_unit_without_losing_facets() -> None:
    result = collapse_post_research_verified_mechanisms(
        **_inputs(),
        verify_dossier=lambda _dossier: (True, []),
    )

    assert result["case_aliases"] == {"case:b": "case:a"}
    assert len(result["problem_records"]) == 1
    assert len(result["priority_decisions"]) == 1
    assert len(result["research_dossiers"]) == 1
    problem = result["problem_records"][0]
    assert {facet["case_id"] for facet in problem["symptom_facets"]} == {
        "case:a",
        "case:b",
    }
    outcome_ids = {
        oracle["outcome_oracle_id"] for oracle in problem["same_mechanism_outcome_oracles"]
    }
    assert outcome_ids == {
        "outcome:case:a",
        "outcome:case:b",
    }
    assert result["priority_decisions"][0]["priority_bucket"] == "p1"
    bundle = result["research_dossiers"][0]["post_research_same_mechanism_bundle"]
    assert bundle["member_case_ids"] == ["case:a", "case:b"]
    assert {item["case_id"] for item in bundle["member_research_dossiers"]} == {
        "case:a",
        "case:b",
    }
    assert len(bundle["bundle_sha256"]) == 64


def _provisional_inputs() -> dict[str, object]:
    inputs = _inputs()
    first_problem = dict(inputs["problem_records"][0])
    facets = [
        {
            "case_id": item["case_id"],
            "problem_id": item["problem_id"],
            "title": item["title"],
            "problem": item["problem"],
            "user_impact": item["user_impact"],
            "canonical_symptoms": item["canonical_symptoms"],
            "evidence_atom_ids": item["evidence_atom_ids"],
            "source_evidence_atom_ids": item["source_evidence_atom_ids"],
        }
        for item in inputs["problem_records"]
    ]
    first_problem.update(
        {
            "case_identity_status": "provisional_same_cause",
            "case_identity_candidate_ids": ["case:a", "case:b"],
            "case_member_problem_ids": ["problem:a", "problem:b"],
            "evidence_atom_ids": ["atom:a", "atom:b"],
            "source_evidence_atom_ids": ["atom:a", "atom:b"],
            "provisional_same_cause_group": {
                "schema_version": 1,
                "status": "research_hypothesis",
                "group_id": "cause:provisional",
                "member_case_ids": ["case:a", "case:b"],
                "member_problem_ids": ["problem:a", "problem:b"],
                "member_facets": facets,
            },
        }
    )
    dossier = dict(inputs["research_dossiers"][0])
    dossier["research_status"] = "evidence_sufficient"
    dossier["evidence_assignment"] = {
        "status": "complete",
        "expected_atom_ids": ["atom:a", "atom:b"],
    }
    dossier["evidence_verification"] = deepcopy(dossier["evidence_verification"])
    dossier["evidence_verification"]["mechanism_evidence"] = [
        {
            "mechanism_evidence_id": "mechanism:one",
            "hypothesis_id": "h1",
            "adversarial_effect": "supports_selection",
            "origin_atom_ids": ["atom:a", "atom:b"],
        }
    ]
    return {
        "problem_records": [first_problem],
        "priority_decisions": [inputs["priority_decisions"][0]],
        "research_dossiers": [dossier],
        "case_registry": inputs["case_registry"],
    }


def test_verified_provisional_group_finalizes_only_after_all_member_evidence() -> None:
    inputs = _provisional_inputs()
    for entry in inputs["case_registry"]["cases"].values():
        entry["case_identity_status"] = "provisional_same_cause"
        entry["case_identity_candidate_ids"] = ["case:a", "case:b"]
        entry["provisional_same_cause_group"] = {
            "status": "research_hypothesis",
            "group_id": "cause:provisional",
        }
    result = collapse_post_research_verified_mechanisms(
        **inputs,
        verify_dossier=lambda _dossier: (True, []),
        assess_dossier=lambda _dossier: (True, []),
    )

    assert result["case_aliases"] == {"case:b": "case:a"}
    assert len(result["groups"]) == 1
    assert result["groups"][0]["relation_kind"] == "verified_provisional_same_cause"
    problem = result["problem_records"][0]
    assert problem["case_identity_status"] == "resolved"
    assert problem["absorbed_case_ids"] == ["case:b"]
    assert "provisional_same_cause_group" not in problem
    assert {facet["case_id"] for facet in problem["symptom_facets"]} == {
        "case:a",
        "case:b",
    }
    registry = build_case_registry(
        result["problem_records"],
        previous=inputs["case_registry"],
    )
    assert registry["cases"]["case:b"]["alias_of"] == "case:a"
    assert "provisional_same_cause_group" not in registry["cases"]["case:a"]
    assert "provisional_same_cause_group" not in registry["cases"]["case:b"]


def test_partial_provisional_group_preserves_original_ids_and_blocks_alias() -> None:
    inputs = _provisional_inputs()
    inputs["research_dossiers"][0]["evidence_assignment"]["expected_atom_ids"] = [
        "atom:a"
    ]

    result = collapse_post_research_verified_mechanisms(
        **inputs,
        verify_dossier=lambda _dossier: (True, []),
        assess_dossier=lambda _dossier: (True, []),
    )

    assert result["case_aliases"] == {}
    assert result["groups"] == []
    assert result["problem_records"][0]["case_identity_status"] == (
        "provisional_same_cause"
    )
    assert "absorbed_case_ids" not in result["problem_records"][0]


def test_provisional_group_requires_evidence_sufficient_readiness() -> None:
    inputs = _provisional_inputs()

    result = collapse_post_research_verified_mechanisms(
        **inputs,
        verify_dossier=lambda _dossier: (True, []),
        assess_dossier=lambda _dossier: (False, ["research_blocked"]),
    )

    assert result["case_aliases"] == {}
    assert result["problem_records"][0]["case_identity_status"] == (
        "provisional_same_cause"
    )


def test_provisional_group_assignment_must_equal_member_source_evidence() -> None:
    inputs = _provisional_inputs()
    problem = inputs["problem_records"][0]
    problem["evidence_atom_ids"].append("atom:derived")
    problem["derived_evidence_atom_ids"] = ["atom:derived"]
    problem["provisional_same_cause_group"]["member_facets"][0][
        "evidence_atom_ids"
    ].append("atom:derived")
    inputs["research_dossiers"][0]["evidence_assignment"]["expected_atom_ids"].append(
        "atom:derived"
    )

    result = collapse_post_research_verified_mechanisms(
        **inputs,
        verify_dossier=lambda _dossier: (True, []),
        assess_dossier=lambda _dossier: (True, []),
    )

    assert result["case_aliases"] == {}
    assert result["groups"] == []


def test_provisional_group_requires_primary_mechanism_coverage_for_every_source_atom() -> None:
    inputs = _provisional_inputs()
    inputs["research_dossiers"][0]["evidence_verification"]["mechanism_evidence"][0][
        "origin_atom_ids"
    ] = ["atom:a"]

    result = collapse_post_research_verified_mechanisms(
        **inputs,
        verify_dossier=lambda _dossier: (True, []),
        assess_dossier=lambda _dossier: (True, []),
    )

    assert result["case_aliases"] == {}
    assert result["groups"] == []


def test_provisional_group_and_independent_verified_case_collapse_transitively() -> None:
    inputs = _provisional_inputs()
    registry, _identity = _registry(["case:a", "case:b", "case:c"])
    independent = _inputs()["research_dossiers"][1]
    independent["case_id"] = "case:c"
    independent["problem_id"] = "problem:c"
    independent_problem = {
        "case_id": "case:c",
        "problem_id": "problem:c",
        "title": "Worker symptom",
        "problem": "The worker drops the same request.",
        "user_impact": "Worker work fails.",
        "evidence_atom_ids": ["atom:c"],
        "source_evidence_atom_ids": ["atom:c"],
        "canonical_symptoms": ["Worker request disappears"],
    }
    independent_priority = {
        "case_id": "case:c",
        "problem_id": "problem:c",
        "priority_bucket": "p0",
        "selected_for_research": True,
        "priority_status": "prioritized",
        "priority_rationale": "Research this observed failure.",
    }
    inputs["problem_records"].append(independent_problem)
    inputs["priority_decisions"].append(independent_priority)
    inputs["research_dossiers"].append(independent)
    inputs["case_registry"] = registry

    result = collapse_post_research_verified_mechanisms(
        **inputs,
        verify_dossier=lambda _dossier: (True, []),
        assess_dossier=lambda _dossier: (True, []),
    )

    assert result["case_aliases"] == {"case:b": "case:a", "case:c": "case:a"}
    assert len(result["problem_records"]) == 1
    problem = result["problem_records"][0]
    assert problem["absorbed_case_ids"] == ["case:b", "case:c"]
    assert {facet["case_id"] for facet in problem["symptom_facets"]} == {
        "case:a",
        "case:b",
        "case:c",
    }
    bundle = result["research_dossiers"][0]["post_research_same_mechanism_bundle"]
    assert bundle["member_case_ids"] == ["case:a", "case:b", "case:c"]
    rebuilt = build_case_registry(result["problem_records"], previous=registry)
    assert rebuilt["cases"]["case:b"]["alias_of"] == "case:a"
    assert rebuilt["cases"]["case:c"]["alias_of"] == "case:a"


def test_same_symbol_with_different_controlled_branch_does_not_collapse() -> None:
    inputs = _inputs()
    second = inputs["research_dossiers"][1]
    provenance = _provenance(slot="keyword:fixture")
    second["evidence_verification"]["verified_mechanism_provenance"] = provenance
    second["evidence_verification"]["verified_mechanism_provenance_sha256"] = _digest(provenance)
    second["evidence_verification"]["control_verifications"][0]["controlled_input_difference"][
        "difference"
    ]["slot"] = "keyword:fixture"

    result = collapse_post_research_verified_mechanisms(
        **inputs,
        verify_dossier=lambda _dossier: (True, []),
    )

    assert result["groups"] == []
    assert len(result["research_dossiers"]) == 2


def test_relationship_rephrasing_does_not_split_runner_identical_cause() -> None:
    inputs = _inputs()
    first_control = inputs["research_dossiers"][0]["evidence_verification"][
        "control_verifications"
    ][0]
    second_control = inputs["research_dossiers"][1]["evidence_verification"][
        "control_verifications"
    ][0]
    first_control["controlled_variable"] = "request routing mode"
    first_control["expected_difference"] = "the control preserves the request"
    first_control["relationship_sha256"] = "1" * 64
    second_control["controlled_variable"] = "mode used by request routing"
    second_control["expected_difference"] = "the request remains present under control"
    second_control["relationship_sha256"] = "2" * 64

    result = collapse_post_research_verified_mechanisms(
        **inputs,
        verify_dossier=lambda _dossier: (True, []),
    )

    assert result["case_aliases"] == {"case:b": "case:a"}
    assert len(result["research_dossiers"]) == 1


def test_same_polarity_with_different_symptom_output_hashes_still_collapses() -> None:
    inputs = _inputs()
    second_polarity = inputs["research_dossiers"][1]["evidence_verification"][
        "control_verifications"
    ][0]["observed_polarity"]
    second_polarity["baseline"]["stdout_sha256"] = "6" * 64
    second_polarity["baseline"]["stderr_sha256"] = "7" * 64
    second_polarity["challenge"]["stdout_sha256"] = "8" * 64
    second_polarity["challenge"]["stderr_sha256"] = "9" * 64

    result = collapse_post_research_verified_mechanisms(
        **inputs,
        verify_dossier=lambda _dossier: (True, []),
    )

    assert result["case_aliases"] == {"case:b": "case:a"}
    assert len(result["research_dossiers"]) == 1


def test_different_runner_observed_polarity_stays_separate() -> None:
    inputs = _inputs()
    second = inputs["research_dossiers"][1]["evidence_verification"]["control_verifications"][0]
    second["observed_polarity"]["challenge"]["exit_code"] = 0
    second["observed_polarity"]["challenge"]["stderr_sha256"] = "5" * 64

    result = collapse_post_research_verified_mechanisms(
        **inputs,
        verify_dossier=lambda _dossier: (True, []),
    )

    assert result["groups"] == []
    assert len(result["research_dossiers"]) == 2


def test_same_hash_at_different_repository_revisions_stays_separate() -> None:
    result = collapse_post_research_verified_mechanisms(
        **_inputs(revisions=("abc", "def")),
        verify_dossier=lambda _dossier: (True, []),
    )

    assert result["groups"] == []
    assert len(result["research_dossiers"]) == 2


def test_post_research_collapse_persists_exact_registry_alias_receipt(
    tmp_path: Path,
) -> None:
    inputs = _inputs()
    result = collapse_post_research_verified_mechanisms(
        **inputs,
        verify_dossier=lambda _dossier: (True, []),
    )
    registry = build_case_registry(
        result["problem_records"],
        previous=inputs["case_registry"],
    )
    response_path = tmp_path / "post-research.response.txt"
    response_path.write_text(json.dumps(result["groups"]) + "\n", encoding="utf-8")

    refs, receipt_path = _persist_canonical_relation_receipts(
        canonical_records=result["problem_records"],
        registry=registry,
        review_response_path=response_path,
        receipt_path=tmp_path / "post-research.relations.json",
        stage="repro_research",
    )

    assert registry["cases"]["case:b"]["alias_of"] == "case:a"
    assert refs["case:b"]["target_case_id"] == "case:a"
    payload = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert payload["stage"] == "repro_research"


def test_model_hash_without_registry_attestation_cannot_collapse() -> None:
    inputs = _inputs()
    inputs["case_registry"] = {"schema_version": 1, "cases": {}}
    result = collapse_post_research_verified_mechanisms(
        **inputs,
        verify_dossier=lambda _dossier: (True, []),
    )

    assert result["groups"] == []
    assert len(result["problem_records"]) == 2
