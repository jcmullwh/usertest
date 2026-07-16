from __future__ import annotations

import json
from copy import deepcopy
from hashlib import sha256
from pathlib import Path

from backlog_core.case_lineage import (
    build_case_registry,
    eligible_problem_mining_atoms,
    problem_case_records_from_registry,
)

from usertest_backlog.workflows.post_research_relations import (
    apply_post_research_relation_assessments,
    authenticated_split_child_occurrence_evidence,
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
    inputs["research_dossiers"][0]["evidence_assignment"]["expected_atom_ids"] = ["atom:a"]

    result = collapse_post_research_verified_mechanisms(
        **inputs,
        verify_dossier=lambda _dossier: (True, []),
        assess_dossier=lambda _dossier: (True, []),
    )

    assert result["case_aliases"] == {}
    assert result["groups"] == []
    assert result["problem_records"][0]["case_identity_status"] == ("provisional_same_cause")
    assert "absorbed_case_ids" not in result["problem_records"][0]


def test_provisional_group_requires_evidence_sufficient_readiness() -> None:
    inputs = _provisional_inputs()

    result = collapse_post_research_verified_mechanisms(
        **inputs,
        verify_dossier=lambda _dossier: (True, []),
        assess_dossier=lambda _dossier: (False, ["research_blocked"]),
    )

    assert result["case_aliases"] == {}
    assert result["problem_records"][0]["case_identity_status"] == ("provisional_same_cause")


def test_provisional_group_assignment_must_equal_member_source_evidence() -> None:
    inputs = _provisional_inputs()
    problem = inputs["problem_records"][0]
    problem["evidence_atom_ids"].append("atom:derived")
    problem["derived_evidence_atom_ids"] = ["atom:derived"]
    problem["provisional_same_cause_group"]["member_facets"][0]["evidence_atom_ids"].append(
        "atom:derived"
    )
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


def _split_inputs() -> dict[str, object]:
    aggregate_id = "atom:aggregate"
    checkout_id = "atom:checkout"
    writer_id = "atom:writer"
    assignment = {
        "status": "complete",
        "errors": [],
        "case_id": "case:broad",
        "problem_id": "problem:broad",
        "expected_atom_ids": [aggregate_id, checkout_id, writer_id],
        "case_evidence_atom_ids": [aggregate_id],
        "occurrence_evidence_atom_ids": [checkout_id, writer_id],
        "atom_receipts": [
            {
                "atom_id": aggregate_id,
                "atom_snapshot": {"atom_id": aggregate_id, "text": "broad storage signal"},
            },
            {
                "atom_id": checkout_id,
                "atom_snapshot": {
                    "atom_id": checkout_id,
                    "action": "create implementation checkout",
                    "text": "checkout failed",
                },
            },
            {
                "atom_id": writer_id,
                "atom_snapshot": {
                    "atom_id": writer_id,
                    "action": "append provider state",
                    "text": "append failed",
                },
            },
        ],
        "assignment_sha256": "a" * 64,
    }
    assessment = {
        "disposition": "split",
        "rationale": "The signed occurrences fail at distinct action boundaries.",
        "material_unknowns": ["Each child still needs root-cause research."],
        "facets": [
            {
                "facet_id": "facet:checkout",
                "title": "Implementation checkout cannot be created",
                "problem": "The implementation runner cannot create its checkout.",
                "user_impact": "Implementation work cannot begin.",
                "occurrence_evidence_atom_ids": [checkout_id],
                "boundary": {
                    "kind": "action",
                    "statement": "Failure occurs while creating an implementation checkout.",
                    "citations": [
                        {
                            "atom_id": checkout_id,
                            "field_path": "/action",
                            "exact_value": "create implementation checkout",
                            "relation": "This is the failed action named by the occurrence.",
                        }
                    ],
                },
            },
            {
                "facet_id": "facet:writer",
                "title": "Provider state cannot be appended",
                "problem": "The provider adapter cannot append its state.",
                "user_impact": "The active agent turn terminates before completion.",
                "occurrence_evidence_atom_ids": [writer_id],
                "boundary": {
                    "kind": "action",
                    "statement": "Failure occurs while appending provider state.",
                    "citations": [
                        {
                            "atom_id": writer_id,
                            "field_path": "/action",
                            "exact_value": "append provider state",
                            "relation": "This is the failed action named by the occurrence.",
                        }
                    ],
                },
            },
        ],
    }
    parent_atom = {
        "atom_id": aggregate_id,
        "origin_run_id": "run:aggregate",
        "origin_stage": "operational_failure_classification",
        "evidence_role": "observation",
        "evidence_class": "observed",
        "source": "operational_failure_candidate",
        "derived_from_atom_ids": [checkout_id, writer_id],
        "parent_case_id": None,
        "case_id": "case:broad",
        "supporting_case_ids": ["case:broad"],
        "disposition": "supports_case",
        "disposition_status": "pending",
        "disposition_receipt": None,
        "text": "Broad storage signal",
    }
    occurrence_atoms = [
        {
            "atom_id": checkout_id,
            "run_id": "run:checkout",
            "run_rel": "run/checkout",
            "origin_run_id": "run:checkout",
            "origin_stage": "implementation",
            "evidence_role": "implementation",
            "evidence_class": "observed",
            "source": "run_failure_event",
            "status": "error",
            "action": "create implementation checkout",
            "text": "checkout failed",
            "derived_from_atom_ids": [],
            "parent_case_id": None,
            "case_id": None,
            "supporting_case_ids": [],
            "disposition": "unresolved",
            "disposition_status": "pending",
            "disposition_receipt": None,
        },
        {
            "atom_id": writer_id,
            "run_id": "run:writer",
            "run_rel": "run/writer",
            "origin_run_id": "run:writer",
            "origin_stage": "implementation",
            "evidence_role": "implementation",
            "evidence_class": "observed",
            "source": "run_failure_event",
            "status": "error",
            "action": "append provider state",
            "text": "append failed",
            "derived_from_atom_ids": [],
            "parent_case_id": None,
            "case_id": None,
            "supporting_case_ids": [],
            "disposition": "unresolved",
            "disposition_status": "pending",
            "disposition_receipt": None,
        },
    ]
    problem = {
        "case_id": "case:broad",
        "case_identity_status": "resolved",
        "problem_id": "problem:broad",
        "canonical_problem_id": "problem:broad",
        "case_member_problem_ids": ["problem:broad"],
        "case_revision": 1,
        "title": "Broad storage failure",
        "problem": "Multiple operations reported storage failures.",
        "user_impact": "Automation stopped.",
        "severity": "high",
        "confidence": 0.8,
        "problem_status": "identified",
        "evidence_atom_ids": [aggregate_id],
        "source_evidence_atom_ids": [aggregate_id],
        "derived_evidence_atom_ids": [],
    }
    priority = {
        "case_id": "case:broad",
        "problem_id": "problem:broad",
        "priority_bucket": "p1",
        "selected_for_research": True,
        "eligible_for_downstream": False,
        "priority_status": "prioritized",
        "priority_rationale": "Automation is blocked.",
    }
    dossier = {
        "case_id": "case:broad",
        "problem_id": "problem:broad",
        "repo_revision": "abc123",
        "research_status": "insufficient_evidence",
        "reproduction_status": "partial",
        "case_relation_assessment": assessment,
        "evidence_assignment": assignment,
        "evidence_verification": {"receipt_sha256": "b" * 64},
    }
    return {
        "problem_records": [problem],
        "priority_decisions": [priority],
        "research_dossiers": [dossier],
        "atoms": [parent_atom, *occurrence_atoms],
    }


def test_partial_research_split_creates_unresearched_children_and_immutable_receipt(
    tmp_path: Path,
) -> None:
    inputs = _split_inputs()

    result = apply_post_research_relation_assessments(
        **inputs,
        receipt_dir=tmp_path,
        verify_dossier=lambda _dossier: (True, []),
    )

    assert result["research_dossiers"] == []
    assert result["split_parent_dossiers"] == inputs["research_dossiers"]
    assert len(result["problem_records"]) == 2
    assert all(
        record["root_cause_status"] == "unestablished" for record in result["problem_records"]
    )
    assert all(
        decision["eligible_for_downstream"] is False
        and decision["research_route"] == "research_required"
        for decision in result["priority_decisions"]
    )
    child_occurrences = {
        tuple(record["occurrence_evidence_atom_ids"]) for record in result["problem_records"]
    }
    assert child_occurrences == {("atom:checkout",), ("atom:writer",)}
    assert all(
        record["evidence_atom_ids"] != ["atom:aggregate"] for record in result["problem_records"]
    )
    facet_atoms = [
        atom for atom in result["atoms"] if atom.get("source") == "post_research_facet_context"
    ]
    assert {tuple(atom["derived_from_atom_ids"]) for atom in facet_atoms} == child_occurrences
    assert all(
        atom["disposition"] == "novel_case"
        and atom["evidence_role"] == "research"
        and atom["evidence_class"] == "proposal"
        for atom in facet_atoms
    )
    assert eligible_problem_mining_atoms(facet_atoms) == []
    atoms_by_id = {atom["atom_id"]: atom for atom in result["atoms"]}
    for child in result["problem_records"]:
        occurrence_ids, errors = authenticated_split_child_occurrence_evidence(
            child,
            atoms_by_id=atoms_by_id,
        )
        assert errors == []
        assert occurrence_ids == child["occurrence_evidence_atom_ids"]

    [receipt_ref] = result["split_receipts"]
    receipt_path = Path(receipt_ref["receipt_path"])
    payload = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert payload["research_status"] == "insufficient_evidence"
    assert payload["occurrence_evidence_atom_ids"] == ["atom:checkout", "atom:writer"]
    assert payload["content_sha256"] == _digest(
        {key: value for key, value in payload.items() if key != "content_sha256"}
    )

    previous_registry = build_case_registry(
        inputs["problem_records"],
        supporting_atoms=inputs["atoms"],
    )
    registry = build_case_registry(
        result["problem_records"],
        previous=previous_registry,
        supporting_atoms=result["atoms"],
    )
    assert registry["cases"]["case:broad"]["state"] == "split"
    assert registry["cases"]["case:broad"]["evidence_atom_ids"] == ["atom:aggregate"]
    assert set(registry["cases"]["case:broad"]["child_case_ids"]) == {
        record["case_id"] for record in result["problem_records"]
    }
    carried = problem_case_records_from_registry(registry)
    assert {record["case_id"] for record in carried} == {
        record["case_id"] for record in result["problem_records"]
    }
    assert all(
        record["occurrence_evidence_atom_ids"]
        and record["post_research_split_receipt"]
        == result["problem_records"][0]["post_research_split_receipt"]
        for record in carried
    )

    # Completed-cache resume deterministically reapplies the sealed Stage-3 split;
    # a later cycle carries only the durable children from the registry.
    repeated = apply_post_research_relation_assessments(
        **inputs,
        receipt_dir=tmp_path,
        verify_dossier=lambda _dossier: (True, []),
    )
    assert repeated["split_groups"] == result["split_groups"]
    assert repeated["split_receipts"] == result["split_receipts"]
    assert [record["case_id"] for record in repeated["problem_records"]] == [
        record["case_id"] for record in result["problem_records"]
    ]


def test_revised_split_replaces_old_active_children_but_preserves_registry_history(
    tmp_path: Path,
) -> None:
    inputs = _split_inputs()
    publisher_id = "atom:publisher"
    publisher_snapshot = {
        "atom_id": publisher_id,
        "action": "publish implementation manifest",
        "text": "manifest publish failed",
    }
    assignment = inputs["research_dossiers"][0]["evidence_assignment"]
    assignment["expected_atom_ids"].append(publisher_id)
    assignment["occurrence_evidence_atom_ids"].append(publisher_id)
    assignment["atom_receipts"].append(
        {"atom_id": publisher_id, "atom_snapshot": publisher_snapshot}
    )
    inputs["atoms"][0]["derived_from_atom_ids"].append(publisher_id)
    inputs["atoms"].append(
        {
            "atom_id": publisher_id,
            "run_id": "run:publisher",
            "run_rel": "run/publisher",
            "origin_run_id": "run:publisher",
            "origin_stage": "implementation",
            "evidence_role": "implementation",
            "evidence_class": "observed",
            "source": "run_failure_event",
            "status": "error",
            "action": publisher_snapshot["action"],
            "text": publisher_snapshot["text"],
            "derived_from_atom_ids": [],
            "parent_case_id": None,
            "case_id": None,
            "supporting_case_ids": [],
            "disposition": "unresolved",
            "disposition_status": "pending",
            "disposition_receipt": None,
        }
    )

    first_assessment = inputs["research_dossiers"][0]["case_relation_assessment"]
    writer_facet = first_assessment["facets"][1]
    writer_facet["occurrence_evidence_atom_ids"].append(publisher_id)
    writer_facet["boundary"]["citations"].append(
        {
            "atom_id": publisher_id,
            "field_path": "/action",
            "exact_value": publisher_snapshot["action"],
            "relation": "This is the second signed action in this facet.",
        }
    )
    first = apply_post_research_relation_assessments(
        **inputs,
        receipt_dir=tmp_path,
        verify_dossier=lambda _dossier: (True, []),
    )
    parent_registry = build_case_registry(
        inputs["problem_records"],
        supporting_atoms=inputs["atoms"],
    )
    first_registry = build_case_registry(
        first["problem_records"],
        previous=parent_registry,
        supporting_atoms=first["atoms"],
    )
    old_case_ids = {record["case_id"] for record in first["problem_records"]}
    old_receipts = {
        case_id: deepcopy(first_registry["cases"][case_id]["post_research_split_receipt"])
        for case_id in old_case_ids
    }

    revised_inputs = deepcopy(inputs)
    revised_inputs["problem_records"] = [
        *revised_inputs["problem_records"],
        *deepcopy(first["problem_records"]),
    ]
    revised_inputs["priority_decisions"] = [
        *revised_inputs["priority_decisions"],
        *deepcopy(first["priority_decisions"]),
    ]
    revised_inputs["atoms"] = deepcopy(first["atoms"])
    revised_inputs["research_dossiers"][0]["case_relation_assessment"]["facets"] = [
        {
            "facet_id": "facet:runner-setup",
            "title": "Runner setup operations fail",
            "problem": "The runner cannot complete checkout and provider-state setup.",
            "user_impact": "Implementation work cannot start reliably.",
            "occurrence_evidence_atom_ids": ["atom:checkout", "atom:writer"],
            "boundary": {
                "kind": "action",
                "statement": "Both signed runner-setup actions fail.",
                "citations": [
                    {
                        "atom_id": "atom:checkout",
                        "field_path": "/action",
                        "exact_value": "create implementation checkout",
                        "relation": "This is a signed runner-setup action.",
                    },
                    {
                        "atom_id": "atom:writer",
                        "field_path": "/action",
                        "exact_value": "append provider state",
                        "relation": "This is a signed runner-setup action.",
                    },
                ],
            },
        },
        {
            "facet_id": "facet:publisher",
            "title": "Implementation manifest cannot be published",
            "problem": "The completed implementation manifest cannot be published.",
            "user_impact": "Completed work is not made available downstream.",
            "occurrence_evidence_atom_ids": [publisher_id],
            "boundary": {
                "kind": "action",
                "statement": "Failure occurs while publishing the manifest.",
                "citations": [
                    {
                        "atom_id": publisher_id,
                        "field_path": "/action",
                        "exact_value": publisher_snapshot["action"],
                        "relation": "This is the signed publishing action.",
                    }
                ],
            },
        },
    ]

    revised = apply_post_research_relation_assessments(
        **revised_inputs,
        receipt_dir=tmp_path,
        verify_dossier=lambda _dossier: (True, []),
    )
    latest_case_ids = set(revised["split_groups"][0]["child_case_ids"])
    assert old_case_ids.isdisjoint(latest_case_ids)
    assert {record["case_id"] for record in revised["problem_records"]} == latest_case_ids
    assert {decision["case_id"] for decision in revised["priority_decisions"]} == latest_case_ids

    revised_registry = build_case_registry(
        revised["problem_records"],
        previous=first_registry,
        supporting_atoms=revised["atoms"],
    )
    parent_entry = revised_registry["cases"]["case:broad"]
    assert set(parent_entry["child_case_ids"]) == latest_case_ids
    assert set(parent_entry["historical_child_case_ids"]) == old_case_ids | latest_case_ids
    for old_case_id in old_case_ids:
        old_entry = revised_registry["cases"][old_case_id]
        assert old_entry["state"] == "superseded"
        assert old_entry["post_research_split_receipt"] == old_receipts[old_case_id]
    assert {
        record["case_id"] for record in problem_case_records_from_registry(revised_registry)
    } == latest_case_ids


def test_split_rejects_model_partition_without_authenticated_boundary(
    tmp_path: Path,
) -> None:
    inputs = _split_inputs()
    dossier = inputs["research_dossiers"][0]
    dossier["case_relation_assessment"]["facets"][0]["boundary"]["citations"][0]["exact_value"] = (
        "invented action"
    )

    import pytest

    with pytest.raises(ValueError, match="citation_value_mismatch"):
        apply_post_research_relation_assessments(
            **inputs,
            receipt_dir=tmp_path,
            verify_dossier=lambda _dossier: (True, []),
        )


def test_retain_assessment_does_not_rewrite_case_graph(tmp_path: Path) -> None:
    inputs = _split_inputs()
    dossier = inputs["research_dossiers"][0]
    dossier["case_relation_assessment"] = {
        "disposition": "retain",
        "rationale": "The evidence still describes one work unit.",
        "facets": [],
        "material_unknowns": [],
    }

    result = apply_post_research_relation_assessments(
        **inputs,
        receipt_dir=tmp_path,
        verify_dossier=lambda _dossier: (True, []),
    )

    assert result["split_groups"] == []
    assert result["problem_records"] == inputs["problem_records"]
    assert result["research_dossiers"] == inputs["research_dossiers"]
