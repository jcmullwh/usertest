from __future__ import annotations

import json
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
        oracle["outcome_oracle_id"]
        for oracle in problem["same_mechanism_outcome_oracles"]
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


def test_same_symbol_with_different_controlled_branch_does_not_collapse() -> None:
    inputs = _inputs()
    second = inputs["research_dossiers"][1]
    provenance = _provenance(slot="keyword:fixture")
    second["evidence_verification"]["verified_mechanism_provenance"] = provenance
    second["evidence_verification"]["verified_mechanism_provenance_sha256"] = _digest(
        provenance
    )
    second["evidence_verification"]["control_verifications"][0][
        "controlled_input_difference"
    ]["difference"]["slot"] = "keyword:fixture"

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
