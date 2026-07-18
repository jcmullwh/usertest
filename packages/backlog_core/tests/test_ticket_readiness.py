from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from pathlib import Path

import pytest

import backlog_core.ticket_readiness as ticket_readiness
from backlog_core.ticket_readiness import (
    assess_change_plan_readiness,
    assess_selection_readiness,
    assess_solution_option_readiness,
    assess_ticket_readiness,
    assign_plan_revision_id,
    bind_falsification_review,
    bind_plan_outcome_oracle,
    falsification_acceptance_has_adversarial_basis,
    falsification_review_receipt_errors,
    infer_live_verification_requirement,
    plan_revision_id_for,
)


def _content_id(prefix: str, value: dict[str, object], id_field: str) -> str:
    projection = {key: item for key, item in value.items() if key != id_field}
    canonical = json.dumps(
        projection,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return f"{prefix}:{hashlib.sha256(canonical).hexdigest()}"


def _canonical_hash(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _set_verified_primary_context(
    verification: dict[str, object],
    *,
    primary_hypothesis_id: str,
    mechanism_evidence_ids: list[str],
    causal_root_evidence_ids: list[str],
) -> None:
    verified_mechanism: dict[str, object] = {
        "primary_hypothesis_id": primary_hypothesis_id,
        "mechanism_evidence_ids": sorted(mechanism_evidence_ids),
    }
    provenance: dict[str, object] = {
        "schema_version": 1,
        "primary_hypothesis_id": primary_hypothesis_id,
        "mechanism_evidence_ids": sorted(mechanism_evidence_ids),
        "causal_root_evidence_ids": sorted(causal_root_evidence_ids),
    }
    verification["verified_mechanism"] = verified_mechanism
    verification["verified_mechanism_sha256"] = _canonical_hash(verified_mechanism)
    verification["verified_mechanism_provenance"] = provenance
    verification["verified_mechanism_provenance_sha256"] = _canonical_hash(provenance)


def _primary_binding_fields(verification: dict[str, object]) -> dict[str, object]:
    provenance = verification["verified_mechanism_provenance"]
    assert isinstance(provenance, dict)
    return {
        "primary_hypothesis_id": provenance["primary_hypothesis_id"],
        "primary_verified_mechanism_sha256": verification["verified_mechanism_sha256"],
        "primary_verified_mechanism_provenance_sha256": verification[
            "verified_mechanism_provenance_sha256"
        ],
    }


def _synthetic_positive_contract(
    evidence_id: str,
    *,
    verification: dict[str, object],
) -> dict[str, object]:
    contract: dict[str, object] = {
        "schema_version": 1,
        "kind": "origin_evidence_semantic_contract",
        "research_experiment_id": "experiment:support",
        "mechanism_evidence_ids": [evidence_id],
        "postconditions": [{"type": "command_exit_code", "command_index": 0, "equals": 0}],
        **_primary_binding_fields(verification),
    }
    contract["positive_outcome_contract_id"] = _content_id(
        "positive_outcome_contract",
        contract,
        "positive_outcome_contract_id",
    )
    return contract


def _set_synthetic_positive_contract(
    research: dict[str, object],
    evidence_id: str,
) -> None:
    verification = research["evidence_verification"]
    assert isinstance(verification, dict)
    mechanism_evidence = verification.get("mechanism_evidence")
    primary_hypothesis_id = (
        next(
            (
                str(item["hypothesis_id"])
                for item in mechanism_evidence
                if isinstance(item, dict)
                and item.get("mechanism_evidence_id") == evidence_id
                and isinstance(item.get("hypothesis_id"), str)
            ),
            "h1",
        )
        if isinstance(mechanism_evidence, list)
        else "h1"
    )
    selected_evidence_ids = [
        str(item["mechanism_evidence_id"])
        for item in (mechanism_evidence if isinstance(mechanism_evidence, list) else [])
        if isinstance(item, dict)
        and item.get("hypothesis_id") == primary_hypothesis_id
        and isinstance(item.get("mechanism_evidence_id"), str)
    ]
    if evidence_id not in selected_evidence_ids:
        selected_evidence_ids.append(evidence_id)
    _set_verified_primary_context(
        verification,
        primary_hypothesis_id=primary_hypothesis_id,
        mechanism_evidence_ids=selected_evidence_ids,
        causal_root_evidence_ids=[evidence_id],
    )
    positive_contract = _synthetic_positive_contract(
        evidence_id,
        verification=verification,
    )
    oracle: dict[str, object] = {
        "schema_version": 1,
        "research_experiment_id": "experiment:support",
        "mechanism_evidence_ids": [evidence_id],
        "positive_outcome_contracts": [positive_contract],
        **_primary_binding_fields(verification),
    }
    oracle["outcome_oracle_id"] = _content_id("outcome_oracle", oracle, "outcome_oracle_id")
    verification["outcome_oracles"] = [oracle]


def _runner_research(
    *,
    same_consumer: bool = False,
    second_atom_ids: list[str] | None = None,
) -> tuple[dict[str, object], list[dict[str, object]]]:
    controls: list[dict[str, object]] = []
    paths: list[dict[str, object]] = []
    mechanism_evidence: list[dict[str, object]] = []
    for suffix, path_name, consumer_name, atom_ids in (
        ("a", "consumer.a", "consumer.a", ["atom:a"]),
        (
            "b",
            "consumer.a" if same_consumer else "consumer.b",
            "consumer.a" if same_consumer else "consumer.b",
            second_atom_ids or ["atom:b"],
        ),
    ):
        consumer_projection = {
            "kind": "runner_observed_entrypoint",
            "entrypoint": consumer_name,
            "attestation_basis": "runner_mechanism_link",
            "runner_attested": True,
        }
        consumer_identity = {
            **consumer_projection,
            "consumer_identity_sha256": _canonical_hash(consumer_projection),
        }
        independence_key = _canonical_hash(consumer_identity)
        control: dict[str, object] = {
            "verification_method": "pytest_ast_controlled_difference_v2",
            "hypothesis_id": "h1",
            "support_experiment_id": f"experiment:support:{suffix}",
            "control_experiment_id": f"experiment:control:{suffix}",
            "mechanism_symbols": ["shared.apply"],
            "shared_verified_mechanism_symbols": ["shared.apply"],
            "controlled_input_difference": {"difference_count": 1},
            "observable_difference": {"difference_kind": "failing_exit_to_zero"},
            "adversarial_effect": "limits_scope",
        }
        control["control_verification_id"] = _content_id(
            "control_verification",
            control,
            "control_verification_id",
        )
        controls.append(control)
        path: dict[str, object] = {
            "verification_method": "runner_controlled_failure_path_v1",
            "path_name": path_name,
            "consumer_identity": consumer_identity,
            "independence_key": independence_key,
            "hypothesis_id": "h1",
            "support_experiment_id": f"experiment:support:{suffix}",
            "support_selection_id": f"h1:experiment:support:{suffix}",
            "control_verification_id": control["control_verification_id"],
            "mechanism_symbols": ["shared.apply"],
            "origin_atom_ids": atom_ids,
            "observed_failure": {"source": "exit_code", "exit_code": 1},
        }
        path["failure_path_id"] = _content_id(
            "failure_path",
            path,
            "failure_path_id",
        )
        paths.append(path)
        evidence: dict[str, object] = {
            "evidence_type": "controlled_scenario",
            "hypothesis_id": "h1",
            "mechanism_symbols": ["shared.apply"],
            "code_paths": [{"symbol": "shared.apply", "path": "src/shared.py"}],
            "experiment_ids": [
                f"experiment:support:{suffix}",
                f"experiment:control:{suffix}",
            ],
            "artifact_refs": [],
            "origin_atom_ids": atom_ids,
            "path_name": path_name,
            "consumer_identity": consumer_identity,
            "independence_key": independence_key,
            "controlled_condition": {"variable": "consumer", "expected_difference": "pass"},
            "observable_difference": {"difference_kind": "failing_exit_to_zero"},
            "strong_pytest_control_id": control["control_verification_id"],
            "adversarial_effect": "limits_scope",
        }
        evidence["mechanism_evidence_id"] = _content_id(
            "mechanism_evidence", evidence, "mechanism_evidence_id"
        )
        mechanism_evidence.append(evidence)
    challenge_assertion = {
        "source": "stderr",
        "operator": "contains",
        "expected": "bad",
    }
    baseline_assertion = {
        "source": "stderr",
        "operator": "not_contains",
        "expected": "bad",
    }
    primary_evidence = mechanism_evidence[0]
    primary_evidence["experiment_ids"] = [
        *primary_evidence["experiment_ids"],
        "experiment:support",
        "experiment:challenge",
    ]
    primary_evidence["mechanism_evidence_id"] = _content_id(
        "mechanism_evidence", primary_evidence, "mechanism_evidence_id"
    )
    research: dict[str, object] = {
        "experiments": [
            {
                "experiment_id": "experiment:support",
                "scenario_kind": "original_replay",
                "command": "pytest tests/test_shared.py::test_failure -q",
                "result": "The failure is reproduced",
                "outcome": "supports",
                "exit_code": 1,
                "addresses_atom_ids": ["atom:a"],
                "observable_assertion": challenge_assertion,
                "artifact_refs": ["artifact:mechanism"],
            },
            {
                "experiment_id": "experiment:challenge",
                "scenario_kind": "faithful_replay",
                "command": "pytest tests/test_shared.py::test_alternative -q",
                "result": "The failure remains when the alternative is removed",
                "outcome": "supports",
                "exit_code": 1,
                "addresses_atom_ids": ["atom:a"],
                "observable_assertion": challenge_assertion,
                "artifact_refs": ["artifact:mechanism"],
            },
            {"experiment_id": "experiment:control", "artifact_refs": []},
        ],
        "root_cause_hypotheses": [
            {
                "hypothesis_id": "h1",
                "statement": "The shared result loses error provenance.",
                "mechanism_symbols": ["shared.apply"],
                "supporting_evidence": [
                    "experiment:support",
                    "experiment:challenge",
                ],
                "counterevidence": ["experiment:control"],
                "falsification_attempts": [
                    {
                        "attempt_id": "falsify:h1:alternative",
                        "hypothesis_id": "h1",
                        "claim": "The shared result loses error provenance.",
                        "baseline_experiment_id": "experiment:support",
                        "challenge_experiment_id": "experiment:challenge",
                        "disproof_condition": baseline_assertion,
                        "outcome": "survived",
                    }
                ],
            }
        ],
        "evidence_verification": {
            "status": "verified",
            "receipt_sha256": "a" * 64,
            "inspected_symbols": [{"symbol": "shared.apply", "path": "src/shared.py"}],
            "control_verifications": controls,
            "failure_paths": paths,
            "mechanism_evidence": mechanism_evidence,
            "experiments": [
                {
                    "experiment_id": "experiment:support",
                    "command": "pytest tests/test_shared.py::test_failure -q",
                    "declared_result": "The failure is reproduced",
                    "exit_code": 1,
                    "outcome": "supports",
                    "scenario_kind": "original_replay",
                    "observable_assertion": challenge_assertion,
                    "assertion_passed": True,
                    "stdout_sha256": "1" * 64,
                    "stderr_sha256": "2" * 64,
                },
                {
                    "experiment_id": "experiment:challenge",
                    "command": "pytest tests/test_shared.py::test_alternative -q",
                    "declared_result": ("The failure remains when the alternative is removed"),
                    "exit_code": 1,
                    "outcome": "supports",
                    "scenario_kind": "faithful_replay",
                    "observable_assertion": challenge_assertion,
                    "assertion_passed": True,
                    "stdout_sha256": "3" * 64,
                    "stderr_sha256": "4" * 64,
                },
            ],
        },
    }
    intervention: dict[str, object] = {
        "verification_method": "pytest_ast_falsification_intervention_v1",
        "hypothesis_id": "h1",
        "attempt_id": "falsify:h1:alternative",
        "baseline_experiment_id": "experiment:support",
        "challenge_experiment_id": "experiment:challenge",
        "mechanism_symbols": ["shared.apply"],
        "controlled_input_difference": {"difference_count": 1},
        "observed_polarity": {"polarity": "failure_persists_after_intervention"},
    }
    intervention["intervention_receipt_id"] = _content_id(
        "falsification_intervention",
        intervention,
        "intervention_receipt_id",
    )
    verification = research["evidence_verification"]
    assert isinstance(verification, dict)
    verification["falsification_interventions"] = [intervention]
    _set_synthetic_positive_contract(
        research,
        str(primary_evidence["mechanism_evidence_id"]),
    )
    return research, paths


def _append_outcome_oracle(
    research: dict[str, object],
    *,
    evidence_id: str,
    experiment_id: str,
) -> tuple[str, str]:
    verification = research["evidence_verification"]
    assert isinstance(verification, dict)
    contract = _synthetic_positive_contract(
        evidence_id,
        verification=verification,
    )
    contract["research_experiment_id"] = experiment_id
    contract["positive_outcome_contract_id"] = _content_id(
        "positive_outcome_contract",
        contract,
        "positive_outcome_contract_id",
    )
    oracle: dict[str, object] = {
        "schema_version": 1,
        "research_experiment_id": experiment_id,
        "mechanism_evidence_ids": [evidence_id],
        "positive_outcome_contracts": [contract],
        **_primary_binding_fields(verification),
    }
    oracle["outcome_oracle_id"] = _content_id(
        "outcome_oracle",
        oracle,
        "outcome_oracle_id",
    )
    outcome_oracles = verification["outcome_oracles"]
    assert isinstance(outcome_oracles, list)
    outcome_oracles.append(oracle)
    return (
        str(oracle["outcome_oracle_id"]),
        str(contract["positive_outcome_contract_id"]),
    )


def test_verified_outcomes_keep_nonroot_scenario_after_root_is_proven() -> None:
    research, _ = _runner_research()
    verification = research["evidence_verification"]
    assert isinstance(verification, dict)
    mechanisms = verification["mechanism_evidence"]
    assert isinstance(mechanisms, list)
    nonroot_evidence_id = str(mechanisms[1]["mechanism_evidence_id"])
    secondary_oracle_id, _ = _append_outcome_oracle(
        research,
        evidence_id=nonroot_evidence_id,
        experiment_id="experiment:secondary",
    )

    oracles = ticket_readiness.verified_outcome_oracles(research)

    assert set(oracles) == {"experiment:support", "experiment:secondary"}
    assert oracles["experiment:secondary"]["outcome_oracle_id"] == secondary_oracle_id


def test_verified_outcomes_reject_member_without_root_bound_contract() -> None:
    research, _ = _runner_research()
    verification = research["evidence_verification"]
    assert isinstance(verification, dict)
    mechanisms = verification["mechanism_evidence"]
    oracles = verification["outcome_oracles"]
    assert isinstance(mechanisms, list)
    assert isinstance(oracles, list)
    nonroot_evidence_id = str(mechanisms[1]["mechanism_evidence_id"])
    oracle = oracles[0]
    assert isinstance(oracle, dict)
    contract = oracle["positive_outcome_contracts"][0]
    assert isinstance(contract, dict)
    contract["mechanism_evidence_ids"] = [nonroot_evidence_id]
    contract["positive_outcome_contract_id"] = _content_id(
        "positive_outcome_contract",
        contract,
        "positive_outcome_contract_id",
    )
    oracle["mechanism_evidence_ids"] = [nonroot_evidence_id]
    oracle["outcome_oracle_id"] = _content_id(
        "outcome_oracle",
        oracle,
        "outcome_oracle_id",
    )

    assert ticket_readiness.verified_outcome_oracles(research) == {}


def test_rejected_hypothesis_contract_is_not_indexed_or_selectable() -> None:
    research, _ = _runner_research()
    verification = research["evidence_verification"]
    assert isinstance(verification, dict)
    oracles = verification["outcome_oracles"]
    assert isinstance(oracles, list)
    oracle = oracles[0]
    assert isinstance(oracle, dict)
    valid_contract = oracle["positive_outcome_contracts"][0]
    assert isinstance(valid_contract, dict)
    rejected_contract = dict(valid_contract)
    rejected_contract["primary_hypothesis_id"] = "h-rejected-alternative"
    rejected_contract["positive_outcome_contract_id"] = _content_id(
        "positive_outcome_contract",
        rejected_contract,
        "positive_outcome_contract_id",
    )
    oracle["positive_outcome_contracts"].append(rejected_contract)
    oracle["outcome_oracle_id"] = _content_id(
        "outcome_oracle",
        oracle,
        "outcome_oracle_id",
    )
    rejected_id = str(rejected_contract["positive_outcome_contract_id"])

    assert rejected_id not in ticket_readiness._research_positive_contract_index(research)
    with pytest.raises(
        ValueError,
        match="change_plan_selected_positive_outcome_contract_unbound",
    ):
        bind_plan_outcome_oracle(
            {},
            research=research,
            selection={
                "falsification_review": {"selected_positive_outcome_contract_ids": [rejected_id]}
            },
        )


def _broad_option(
    *,
    first_ref: str,
    second_ref: str,
    first_name: str = "consumer A",
    second_name: str = "consumer B",
) -> dict[str, object]:
    return {
        "option_id": "option:test:shared",
        "problem_id": "problem:test",
        "family_id": "most_direct",
        "summary": "Introduce a shared contract for both consumers.",
        "tradeoffs": "The shared boundary increases coordination cost.",
        "recurrence_prevention": "Both evidenced paths use the same invariant.",
        "change_surface_hypothesis": "Update the shared boundary and both callers.",
        "test_implications": "Replay each caller independently.",
        "rationale": "Two consumers exhibit the same mechanism.",
        "causal_coverage": {
            "mechanism_addressed": "The shared result loses error provenance.",
            "research_binding": {
                "hypothesis_id": "h1",
                "hypothesis_statement": "The shared result loses error provenance.",
                "mechanism_symbols": ["shared.apply"],
                "supporting_evidence_refs": [
                    "experiment:support",
                    "experiment:challenge",
                ],
                "counterevidence_refs": ["experiment:control"],
                "falsification_attempt_refs": ["falsify:h1:alternative"],
                "deterministic_closure_refs": [],
                "intervention_points": [
                    {
                        "mechanism_symbol": "shared.apply",
                        "target_path": "src/shared.py",
                        "target_symbol": "shared.apply",
                        "intervention": "Preserve provenance at the verified shared boundary.",
                    }
                ],
            },
            "symptoms_covered": ["consumer A failure", "consumer B failure"],
            "unsupported_assumptions": [],
            "residual_recurrence_paths": [],
            "compatibility_risks": [],
            "testability": {"before": "both fail", "after": "both pass"},
            "outcome_strategy": {
                "intended_operation": "Both consumers preserve and expose error provenance.",
                "success_properties": [
                    "The original replay completes with the expected provenance value."
                ],
                "safety_constraints": ["Existing successful results remain unchanged."],
                "original_scenario_experiment_ids": ["experiment:support"],
            },
        },
        "scope_evidence": {
            "scope_level": "shared_abstraction",
            "independent_consumers_or_failure_paths": [
                {"name": first_name, "evidence_refs": [first_ref]},
                {"name": second_name, "evidence_refs": [second_ref]},
            ],
        },
    }


def test_plan_revision_content_address_is_stable_and_server_owned() -> None:
    plan = {
        "change_plan_id": "plan:test:1",
        "case_id": "case:test",
        "problem_id": "problem:test",
        "selected_option_id": "option:test:direct",
        "proposed_fix": "Apply the guard",
    }
    assigned = assign_plan_revision_id({**plan, "plan_revision_id": "model:v99"})
    assert assigned["plan_revision_id"] == plan_revision_id_for(plan)
    assert assigned["plan_revision_source"] == "server_content_addressed_v1"
    assert assigned["plan_revision_id"].startswith("planrev:sha256:")
    assert (
        plan_revision_id_for({**plan, "proposed_fix": "Different fix"})
        != assigned["plan_revision_id"]
    )


def test_ticket_readiness_rejects_problem_and_priority_parse_or_lineage_gaps() -> None:
    ticket = {
        "problem_record": {
            "problem_id": "problem:test",
            "case_id": "case:test",
            "canonical_problem_id": "problem:other",
            "case_member_problem_ids": ["problem:other"],
            "_parse_warning": "malformed model output",
        },
        "priority": {
            "problem_id": "problem:test",
            "case_id": "case:other",
            "selected_for_research": False,
            "_parse_warning": "missing priority rationale",
        },
        "selected_solution": {"_parse_warning": "bad selection"},
    }
    ready, reasons = assess_ticket_readiness(ticket)
    assert ready is False
    assert "problem_record_parse_warning_present" in reasons
    assert "problem_record_canonical_problem_mismatch" in reasons
    assert "problem_record_case_membership_invalid" in reasons
    assert "priority_decision_parse_warning_present" in reasons
    assert "priority_decision_not_selected_for_research" in reasons
    assert "priority_decision_case_mismatch" in reasons
    assert "selection_parse_warning_present" in reasons


def test_broad_scope_cannot_count_artifact_id_and_path_as_independent_evidence() -> None:
    research = {
        "artifact_refs": [
            {
                "artifact_id": "artifact:shared-trace",
                "path": "evidence/shared-trace.json",
            }
        ]
    }
    ready, reasons = assess_solution_option_readiness(
        _broad_option(
            first_ref="artifact:shared-trace",
            second_ref="evidence/shared-trace.json",
        ),
        research=research,
    )

    assert ready is False
    assert any("solution_option_scope_path_receipt_unbound" in reason for reason in reasons)


def test_broad_scope_accepts_two_runner_verified_independent_failure_paths() -> None:
    research, paths = _runner_research()
    ready, reasons = assess_solution_option_readiness(
        _broad_option(
            first_ref=str(paths[0]["failure_path_id"]),
            second_ref=str(paths[1]["failure_path_id"]),
            first_name=str(paths[0]["path_name"]),
            second_name=str(paths[1]["path_name"]),
        ),
        research=research,
    )

    assert ready is True
    assert reasons == []


def test_solution_option_family_label_is_optional_telemetry() -> None:
    research, paths = _runner_research()
    option = _broad_option(
        first_ref=str(paths[0]["failure_path_id"]),
        second_ref=str(paths[1]["failure_path_id"]),
        first_name=str(paths[0]["path_name"]),
        second_name=str(paths[1]["path_name"]),
    )
    option.pop("family_id", None)

    ready, reasons = assess_solution_option_readiness(option, research=research)

    assert ready is True
    assert reasons == []


def _generic_adapter_option_fixture() -> tuple[
    dict[str, object],
    dict[str, object],
    str,
    str,
    str,
]:
    research, _paths = _runner_research()
    locator = "env:USERTEST_MODE"
    implementation_path = "config/runtime.toml"
    intervention = "Read USERTEST_MODE through the retained configuration boundary."
    hypothesis = research["root_cause_hypotheses"][0]
    assert isinstance(hypothesis, dict)
    hypothesis["mechanism_symbols"] = [locator]
    verification = research["evidence_verification"]
    assert isinstance(verification, dict)
    file_sha256 = "9" * 64
    verification["inspected_files"] = [
        {
            "path": implementation_path,
            "sha256": file_sha256,
            "observed_content_sha256": file_sha256,
        }
    ]
    verification["inspected_symbols"] = []
    interventions = verification["falsification_interventions"]
    assert isinstance(interventions, list)
    for receipt in interventions:
        assert isinstance(receipt, dict)
        receipt["mechanism_symbols"] = [locator]
        receipt["intervention_receipt_id"] = _content_id(
            "falsification_intervention",
            receipt,
            "intervention_receipt_id",
        )
    evidence_items = verification["mechanism_evidence"]
    assert isinstance(evidence_items, list)
    evidence = evidence_items[0]
    assert isinstance(evidence, dict)
    node_evidence_sha256 = "8" * 64
    touchpoint_projection: dict[str, object] = {
        "causal_locator": locator,
        "path": implementation_path,
        "symbols": [],
        "relationship": "This inspected config file defines the environment binding.",
        "runner_attested": True,
        "inspected_content_sha256": file_sha256,
    }
    touchpoint_hash = _canonical_hash(touchpoint_projection)
    touchpoint = {
        "touchpoint_id": f"implementation_touchpoint:{touchpoint_hash}",
        **touchpoint_projection,
        "evidence_sha256": touchpoint_hash,
    }
    evidence.update(
        {
            "evidence_type": "adapter_proof",
            "mechanism_symbols": [locator],
            "mechanism_targets": [
                {
                    "node_id": "proof:environment",
                    "kind": "environment",
                    "locator": locator,
                    "runner_attested": True,
                    "evidence_sha256": node_evidence_sha256,
                }
            ],
            "code_paths": [{"symbol": locator, "path": locator}],
            "mechanism_link": {
                "verification_method": "runner_causal_proof_adapter_v1",
                "code_path": [{"symbol": locator, "path": locator}],
            },
            "intervention_targets": [
                {
                    "intervention_id": "intervention:environment",
                    "kind": "environment",
                    "target": locator,
                }
            ],
            "implementation_touchpoints": [touchpoint],
            "proof_receipt_id": "causal_proof:environment",
        }
    )
    evidence["mechanism_evidence_id"] = _content_id(
        "mechanism_evidence",
        evidence,
        "mechanism_evidence_id",
    )
    option = _broad_option(
        first_ref=str(evidence["mechanism_evidence_id"]),
        second_ref=str(evidence["mechanism_evidence_id"]),
        first_name=str(evidence["path_name"]),
        second_name=str(evidence["path_name"]),
    )
    option["summary"] = "Correct the evidenced environment-backed configuration path."
    option["rationale"] = "The adapter intervention proves this one configuration path."
    option["recurrence_prevention"] = (
        "The retained setting is read through the evidenced configuration boundary."
    )
    option["scope_evidence"] = {
        "scope_level": "single_path",
        "independent_consumers_or_failure_paths": [
            {
                "name": evidence["path_name"],
                "evidence_refs": [evidence["mechanism_evidence_id"]],
            }
        ],
    }
    coverage = option["causal_coverage"]
    assert isinstance(coverage, dict)
    binding = coverage["research_binding"]
    assert isinstance(binding, dict)
    binding["mechanism_symbols"] = [locator]
    binding["intervention_points"] = [
        {
            "causal_locator": locator,
            "implementation_touchpoint_ids": [touchpoint["touchpoint_id"]],
            "intervention": intervention,
        }
    ]
    return research, option, locator, implementation_path, intervention


def test_generic_adapter_locator_resolves_to_attested_repo_plan_target() -> None:
    research, option, locator, implementation_path, intervention = (
        _generic_adapter_option_fixture()
    )

    ready, reasons = assess_solution_option_readiness(option, research=research)

    assert ready is True, reasons
    coverage = option["causal_coverage"]
    assert isinstance(coverage, dict)
    binding = coverage["research_binding"]
    assert isinstance(binding, dict)
    required = ticket_readiness._required_plan_intervention_targets(
        binding,
        research=research,
    )
    assert required == {(implementation_path, None): intervention}
    assert all(not path.startswith(("env:", "fs:", "platform:")) for path, _ in required)


def test_generic_adapter_without_connected_repo_touchpoint_returns_to_research() -> None:
    research, option, _locator, _implementation_path, _intervention = (
        _generic_adapter_option_fixture()
    )
    verification = research["evidence_verification"]
    assert isinstance(verification, dict)
    evidence = verification["mechanism_evidence"]
    assert isinstance(evidence, list)
    evidence[0].pop("implementation_touchpoints")
    evidence[0]["mechanism_evidence_id"] = _content_id(
        "mechanism_evidence",
        evidence[0],
        "mechanism_evidence_id",
    )

    ready, reasons = assess_solution_option_readiness(option, research=research)

    assert ready is False
    assert any("intervention_touchpoint_unbound" in reason for reason in reasons)


def test_broad_scope_outcome_oracle_must_cover_every_independent_path() -> None:
    research, paths = _runner_research()
    option = _broad_option(
        first_ref=str(paths[0]["failure_path_id"]),
        second_ref=str(paths[1]["failure_path_id"]),
        first_name=str(paths[0]["path_name"]),
        second_name=str(paths[1]["path_name"]),
    )
    verification = research["evidence_verification"]
    assert isinstance(verification, dict)
    evidence = verification["mechanism_evidence"]
    assert isinstance(evidence, list)
    evidence_ids = [str(value["mechanism_evidence_id"]) for value in evidence]
    plan = {
        "outcome_verification_roles": {
            "original_scenario": {"oracle": {"mechanism_evidence_ids": [evidence_ids[0]]}}
        }
    }

    assert ticket_readiness._broad_scope_outcome_coverage_reasons(
        plan,
        selected_option=option,
        research=research,
    ) == ["change_plan_broad_scope_outcome_path_coverage_missing"]

    plan["before_after_reproduction"] = {"expected_outcome_state": "mitigated"}
    bounded_selection = {
        "falsification_review": {"outcome_claim_status": "mitigated"}
    }
    assert (
        ticket_readiness._broad_scope_outcome_coverage_reasons(
            plan,
            selected_option=option,
            research=research,
            selection=bounded_selection,
        )
        == []
    )

    plan["outcome_verification_roles"]["original_scenario"]["oracle"]["mechanism_evidence_ids"] = (
        evidence_ids
    )
    assert (
        ticket_readiness._broad_scope_outcome_coverage_reasons(
            plan,
            selected_option=option,
            research=research,
        )
        == []
    )


def test_single_path_outcome_oracle_does_not_require_unclaimed_breadth() -> None:
    research, paths = _runner_research()
    option = _broad_option(
        first_ref=str(paths[0]["failure_path_id"]),
        second_ref=str(paths[1]["failure_path_id"]),
    )
    option["scope_evidence"] = {
        "scope_level": "single_path",
        "independent_consumers_or_failure_paths": [
            {
                "name": paths[0]["path_name"],
                "evidence_refs": [paths[0]["failure_path_id"]],
            }
        ],
    }
    plan = {"outcome_verification_roles": {"original_scenario": {}}}

    assert (
        ticket_readiness._broad_scope_outcome_coverage_reasons(
            plan,
            selected_option=option,
            research=research,
        )
        == []
    )


def _multi_symbol_option_fixture() -> tuple[dict[str, object], dict[str, object]]:
    research, paths = _runner_research()
    symbols = ["shared.prepare", "shared.apply"]
    hypothesis = research["root_cause_hypotheses"][0]
    assert isinstance(hypothesis, dict)
    hypothesis["mechanism_symbols"] = symbols
    verification = research["evidence_verification"]
    assert isinstance(verification, dict)
    inspected = verification["inspected_symbols"]
    assert isinstance(inspected, list)
    inspected.insert(0, {"symbol": "shared.prepare", "path": "src/shared.py"})

    controls = verification["control_verifications"]
    assert isinstance(controls, list)
    control_ids: dict[str, str] = {}
    for control in controls:
        assert isinstance(control, dict)
        old_id = str(control["control_verification_id"])
        control["mechanism_symbols"] = symbols
        control["shared_verified_mechanism_symbols"] = symbols
        control["control_verification_id"] = _content_id(
            "control_verification", control, "control_verification_id"
        )
        control_ids[old_id] = str(control["control_verification_id"])

    for path in paths:
        old_control_id = str(path["control_verification_id"])
        path["control_verification_id"] = control_ids[old_control_id]
        path["mechanism_symbols"] = symbols
        path["failure_path_id"] = _content_id("failure_path", path, "failure_path_id")

    evidence_items = verification["mechanism_evidence"]
    assert isinstance(evidence_items, list)
    for evidence in evidence_items:
        assert isinstance(evidence, dict)
        old_control_id = str(evidence["strong_pytest_control_id"])
        evidence["mechanism_symbols"] = symbols
        evidence["code_paths"] = [{"symbol": symbol, "path": "src/shared.py"} for symbol in symbols]
        evidence["strong_pytest_control_id"] = control_ids[old_control_id]
        evidence["mechanism_evidence_id"] = _content_id(
            "mechanism_evidence", evidence, "mechanism_evidence_id"
        )

    option = _broad_option(
        first_ref=str(paths[0]["failure_path_id"]),
        second_ref=str(paths[1]["failure_path_id"]),
        first_name=str(paths[0]["path_name"]),
        second_name=str(paths[1]["path_name"]),
    )
    coverage = option["causal_coverage"]
    assert isinstance(coverage, dict)
    binding = coverage["research_binding"]
    assert isinstance(binding, dict)
    binding["mechanism_symbols"] = symbols
    binding["intervention_points"] = [
        {
            "mechanism_symbol": "shared.apply",
            "controls_mechanism_symbols": symbols,
            "causal_role": "sufficient_control_point",
            "sufficiency_rationale": (
                "shared.apply is the sole state-commit boundary reached through "
                "shared.prepare, so preserving provenance there reverses both observed paths."
            ),
            "target_path": "src/shared.py",
            "target_symbol": "shared.apply",
            "intervention": "Preserve provenance at the verified shared boundary.",
        }
    ]
    return research, option


def test_multi_symbol_mechanism_accepts_one_causally_sufficient_control_point() -> None:
    research, option = _multi_symbol_option_fixture()

    ready, reasons = assess_solution_option_readiness(option, research=research)

    assert ready is True
    assert reasons == []
    coverage = option["causal_coverage"]
    assert isinstance(coverage, dict)
    binding = coverage["research_binding"]
    assert isinstance(binding, dict)
    assert len(binding["intervention_points"]) == 1


def test_multi_symbol_sufficiency_must_cover_every_selected_runner_path() -> None:
    research, option = _multi_symbol_option_fixture()
    verification = research["evidence_verification"]
    assert isinstance(verification, dict)
    evidence_items = verification["mechanism_evidence"]
    assert isinstance(evidence_items, list)
    second = evidence_items[1]
    assert isinstance(second, dict)
    second["strong_pytest_control_id"] = "control_verification:forged"
    second["mechanism_evidence_id"] = _content_id(
        "mechanism_evidence", second, "mechanism_evidence_id"
    )

    ready, reasons = assess_solution_option_readiness(option, research=research)

    assert ready is False
    assert "solution_option_intervention_sufficiency_unverified:0" in reasons

    second["mechanism_link"] = {
        "verification_method": "runner_harness_observable_dataflow_v1",
        "entrypoint": "consumer.b",
        "observable_source": "stdout",
        "symbol_sinks": [
            {"symbol": "shared.prepare", "sink": "prepared"},
            {"symbol": "shared.apply", "sink": "result"},
        ],
    }
    second["mechanism_evidence_id"] = _content_id(
        "mechanism_evidence", second, "mechanism_evidence_id"
    )

    ready, reasons = assess_solution_option_readiness(option, research=research)

    assert ready is True
    assert reasons == []


def test_multi_symbol_mechanism_rejects_point_that_does_not_control_full_path() -> None:
    research, paths = _runner_research()
    option = _broad_option(
        first_ref=str(paths[0]["failure_path_id"]),
        second_ref=str(paths[1]["failure_path_id"]),
        first_name=str(paths[0]["path_name"]),
        second_name=str(paths[1]["path_name"]),
    )
    hypothesis = research["root_cause_hypotheses"][0]
    assert isinstance(hypothesis, dict)
    hypothesis["mechanism_symbols"] = ["shared.prepare", "shared.apply"]
    coverage = option["causal_coverage"]
    assert isinstance(coverage, dict)
    binding = coverage["research_binding"]
    assert isinstance(binding, dict)
    binding["mechanism_symbols"] = ["shared.prepare", "shared.apply"]
    binding["intervention_points"] = [
        {
            "mechanism_symbol": "shared.apply",
            "controls_mechanism_symbols": ["shared.apply"],
            "causal_role": "sufficient_control_point",
            "sufficiency_rationale": "This does not bind the upstream mechanism.",
            "target_path": "src/shared.py",
            "target_symbol": "shared.apply",
            "intervention": "Preserve provenance at the verified shared boundary.",
        }
    ]

    ready, reasons = assess_solution_option_readiness(option, research=research)

    assert ready is False
    assert "solution_option_intervention_control_point_not_sufficient:0" in reasons
    assert "solution_option_causally_sufficient_intervention_missing" in reasons


def test_option_cannot_substitute_unrelated_mechanism_for_research_proof() -> None:
    option = _broad_option(first_ref="artifact:a", second_ref="artifact:b")
    coverage = option["causal_coverage"]
    assert isinstance(coverage, dict)
    binding = coverage["research_binding"]
    assert isinstance(binding, dict)
    binding["hypothesis_id"] = "h-unrelated"
    binding["hypothesis_statement"] = "An unrelated cache path is stale."
    binding["mechanism_symbols"] = ["cache.refresh"]
    binding["supporting_evidence_refs"] = ["experiment:unrelated"]
    binding["counterevidence_refs"] = ["experiment:unrelated-control"]
    binding["intervention_points"] = [
        {
            "mechanism_symbol": "cache.refresh",
            "target_path": "src/cache.py",
            "target_symbol": "cache.refresh",
            "intervention": "Refresh the unrelated cache.",
        }
    ]
    research = {
        "root_cause_hypotheses": [
            {
                "hypothesis_id": "h1",
                "statement": "The shared result loses error provenance.",
                "mechanism_symbols": ["shared.apply"],
                "supporting_evidence": ["experiment:support"],
                "counterevidence": ["experiment:control"],
            }
        ],
        "artifact_refs": [
            {"artifact_id": "artifact:a", "path": "evidence/a.json"},
            {"artifact_id": "artifact:b", "path": "evidence/b.json"},
        ],
        "evidence_verification": {
            "status": "verified",
            "inspected_symbols": [{"symbol": "shared.apply", "path": "src/shared.py"}],
        },
    }

    ready, reasons = assess_solution_option_readiness(option, research=research)

    assert ready is False
    assert "solution_option_research_hypothesis_unbound" in reasons


def test_broad_scope_cannot_count_experiment_and_its_artifact_as_independent() -> None:
    research = {
        "artifact_refs": [{"artifact_id": "artifact:stdout", "path": "evidence/stdout.txt"}],
        "experiments": [
            {
                "experiment_id": "experiment:replay",
                "artifact_refs": ["artifact:stdout"],
            }
        ],
    }
    ready, reasons = assess_solution_option_readiness(
        _broad_option(
            first_ref="experiment:replay",
            second_ref="evidence/stdout.txt",
        ),
        research=research,
    )

    assert ready is False
    assert any("solution_option_scope_path_receipt_unbound" in reason for reason in reasons)


def test_broad_scope_cannot_count_two_artifacts_from_one_experiment_as_independent() -> None:
    research = {
        "artifact_refs": [
            {"artifact_id": "artifact:stdout", "path": "evidence/stdout.txt"},
            {"artifact_id": "artifact:stderr", "path": "evidence/stderr.txt"},
        ],
        "experiments": [
            {
                "experiment_id": "experiment:replay",
                "artifact_refs": ["artifact:stdout", "artifact:stderr"],
            }
        ],
    }
    ready, reasons = assess_solution_option_readiness(
        _broad_option(first_ref="artifact:stdout", second_ref="artifact:stderr"),
        research=research,
    )

    assert ready is False
    assert any("solution_option_scope_path_receipt_unbound" in reason for reason in reasons)


def test_broad_scope_cannot_count_qualified_symbol_and_its_file_as_independent() -> None:
    research = {
        "inspected_files": ["src/shared.py"],
        "inspected_symbols": ["src/shared.py:SharedContract.apply"],
    }
    ready, reasons = assess_solution_option_readiness(
        _broad_option(
            first_ref="src/shared.py",
            second_ref="src/shared.py:SharedContract.apply",
        ),
        research=research,
    )

    assert ready is False
    assert any("solution_option_scope_path_receipt_unbound" in reason for reason in reasons)


def test_broad_scope_rejects_duplicate_runner_independence_key() -> None:
    research, paths = _runner_research(same_consumer=True)
    ready, reasons = assess_solution_option_readiness(
        _broad_option(
            first_ref=str(paths[0]["failure_path_id"]),
            second_ref=str(paths[1]["failure_path_id"]),
            first_name=str(paths[0]["path_name"]),
            second_name=str(paths[1]["path_name"]),
        ),
        research=research,
    )

    assert ready is False
    assert "solution_option_broad_scope_requires_independent_failure_paths" in reasons


@pytest.mark.parametrize(
    ("same_consumer", "expected_ready"),
    [(False, True), (True, False)],
)
def test_broad_scope_uses_open_runner_consumer_identity_not_causal_target(
    same_consumer: bool,
    expected_ready: bool,
) -> None:
    research, paths = _runner_research()
    for index, path in enumerate(paths):
        entrypoint = "tools/consumer-a" if same_consumer or index == 0 else "tools/consumer-b"
        identity_projection = {
            "kind": "domain_specific_executed_consumer",
            "entrypoint": entrypoint,
            "attestation_basis": "executed_entrypoint_and_inspected_change_surface",
            "runner_attested": True,
        }
        identity = {
            **identity_projection,
            "consumer_identity_sha256": _canonical_hash(identity_projection),
        }
        path["path_name"] = entrypoint
        path["consumer_identity"] = identity
        path["independence_key"] = _canonical_hash(identity)
        # Both consumers read the same causal setting.  The target remains useful
        # causal context but must not collapse two independently executed consumers.
        path["causal_target"] = "domain:shared-setting"
        path["failure_path_id"] = _content_id(
            "failure_path",
            path,
            "failure_path_id",
        )

    ready, reasons = assess_solution_option_readiness(
        _broad_option(
            first_ref=str(paths[0]["failure_path_id"]),
            second_ref=str(paths[1]["failure_path_id"]),
            first_name=str(paths[0]["path_name"]),
            second_name=str(paths[1]["path_name"]),
        ),
        research=research,
    )

    assert ready is expected_ready, reasons
    if same_consumer:
        assert "solution_option_broad_scope_requires_independent_failure_paths" in reasons
    else:
        assert reasons == []


def test_broad_scope_cannot_count_support_and_control_for_one_consumer_twice() -> None:
    research, paths = _runner_research()
    verification = research["evidence_verification"]
    assert isinstance(verification, dict)
    mechanism_evidence = verification["mechanism_evidence"]
    assert isinstance(mechanism_evidence, list)
    same_consumer_evidence = mechanism_evidence[0]
    assert isinstance(same_consumer_evidence, dict)

    ready, reasons = assess_solution_option_readiness(
        _broad_option(
            first_ref=str(paths[0]["failure_path_id"]),
            second_ref=str(same_consumer_evidence["mechanism_evidence_id"]),
            first_name=str(paths[0]["path_name"]),
            second_name=str(same_consumer_evidence["path_name"]),
        ),
        research=research,
    )

    assert ready is False
    assert "solution_option_broad_scope_requires_independent_failure_paths" in reasons


def test_broad_scope_allows_independent_paths_from_one_origin_atom() -> None:
    research, paths = _runner_research(second_atom_ids=["atom:a"])
    ready, reasons = assess_solution_option_readiness(
        _broad_option(
            first_ref=str(paths[0]["failure_path_id"]),
            second_ref=str(paths[1]["failure_path_id"]),
            first_name=str(paths[0]["path_name"]),
            second_name=str(paths[1]["path_name"]),
        ),
        research=research,
    )

    assert ready is True
    assert reasons == []


def test_scope_path_name_cannot_relabel_runner_receipt() -> None:
    research, paths = _runner_research()
    ready, reasons = assess_solution_option_readiness(
        _broad_option(
            first_ref=str(paths[0]["failure_path_id"]),
            second_ref=str(paths[1]["failure_path_id"]),
            first_name="invented consumer label",
            second_name=str(paths[1]["path_name"]),
        ),
        research=research,
    )

    assert ready is False
    assert "solution_option_scope_path_name_mismatch:0" in reasons


def _falsification_review(
    control_id: str,
    *,
    research: dict[str, object],
) -> dict[str, object]:
    contract_id = next(
        str(contract["positive_outcome_contract_id"])
        for oracle in ticket_readiness.verified_outcome_oracles(research).values()
        for contract in oracle.get("positive_outcome_contracts", [])
        if isinstance(contract, dict) and control_id in contract.get("mechanism_evidence_ids", [])
    )
    return {
        "problem_id": "problem:test",
        "selected_option_id": "option:test:shared",
        "verdict": "accept",
        "strongest_counterargument": "The verified control bounds the claimed scope.",
        "evidence_refs": [
            {
                "ref": control_id,
                "finding": "The control changes one input and removes the failure.",
                "effect": "limits_scope",
            }
        ],
        "unsupported_assumptions": [],
        "residual_risks": [],
        "critical_findings": [],
        "material_risk_dispositions": [],
        "evidence_that_would_change_verdict": "A failing controlled replay.",
        "selected_positive_outcome_contract_id": contract_id,
        "outcome_contract_reviews": [
            {
                "positive_outcome_contract_id": contract_id,
                "verdict": "sufficient",
                "semantic_relation_assessment": (
                    "The runner-bound postcondition demonstrates intended operation."
                ),
                "proves_intended_operation": True,
                "problem_coverage": "full",
                "residual_untested_paths": [],
                "evidence_refs": [control_id],
            }
        ],
        "outcome_strategy_review": {
            "verdict": "sufficient",
            "semantic_relation_assessment": (
                "The option strategy requires the intended provenance value on the retained "
                "original replay rather than merely removing the failure marker."
            ),
            "proves_intended_operation": True,
            "problem_coverage": "full",
            "residual_untested_paths": [],
            "evidence_refs": [control_id],
        },
    }


def _research_without_future_outcome() -> tuple[
    dict[str, object], dict[str, object], str
]:
    research, paths = _runner_research()
    research["repo_revision"] = "a" * 40
    verification = research["evidence_verification"]
    assert isinstance(verification, dict)
    verification["outcome_oracles"] = []
    option = _broad_option(
        first_ref=str(paths[0]["failure_path_id"]),
        second_ref=str(paths[1]["failure_path_id"]),
        first_name=str(paths[0]["path_name"]),
        second_name=str(paths[1]["path_name"]),
    )
    evidence_id = str(verification["verified_mechanism_provenance"]["mechanism_evidence_ids"][0])
    return research, option, evidence_id


def _outcome_strategy_falsification_review(evidence_id: str) -> dict[str, object]:
    return {
        "problem_id": "problem:test",
        "selected_option_id": "option:test:shared",
        "verdict": "accept",
        "strongest_counterargument": (
            "Preserving a value at only one caller could leave the second path unchanged."
        ),
        "evidence_refs": [
            {
                "ref": evidence_id,
                "finding": "The controlled replay binds the failure to the shared boundary.",
            }
        ],
        "unsupported_assumptions": [],
        "residual_risks": [],
        "critical_findings": [],
        "material_risk_dispositions": [],
        "evidence_that_would_change_verdict": (
            "A retained replay showing that either caller bypasses the selected boundary."
        ),
        "outcome_strategy_review": {
            "verdict": "sufficient",
            "semantic_relation_assessment": (
                "The strategy requires the useful provenance value on the original replay, "
                "rather than only removal of the failure marker."
            ),
            "proves_intended_operation": True,
            "problem_coverage": "full",
            "residual_untested_paths": [],
            "evidence_refs": [evidence_id],
        },
    }


def test_optioning_owns_outcome_strategy_when_research_has_no_future_oracle() -> None:
    research, option, _evidence_id = _research_without_future_outcome()

    ready, reasons = assess_solution_option_readiness(option, research=research)
    assert ready is True
    assert reasons == []

    option["causal_coverage"].pop("outcome_strategy")
    ready, reasons = assess_solution_option_readiness(option, research=research)
    assert ready is False
    assert "solution_option_outcome_strategy_missing_or_unbound" in reasons


def test_optioning_requires_outcome_strategy_even_with_stage3_positive_contract() -> None:
    research, paths = _runner_research()
    option = _broad_option(
        first_ref=str(paths[0]["failure_path_id"]),
        second_ref=str(paths[1]["failure_path_id"]),
        first_name=str(paths[0]["path_name"]),
        second_name=str(paths[1]["path_name"]),
    )
    coverage = option["causal_coverage"]
    assert isinstance(coverage, dict)
    coverage.pop("outcome_strategy")

    ready, reasons = assess_solution_option_readiness(option, research=research)

    assert ready is False
    assert "solution_option_outcome_strategy_missing_or_unbound" in reasons


def test_stage5_binds_reviewed_option_outcome_without_stage3_future_oracle() -> None:
    research, option, evidence_id = _research_without_future_outcome()
    bound = bind_falsification_review(
        _outcome_strategy_falsification_review(evidence_id),
        problem_id="problem:test",
        selected_option=option,
        research=research,
    )

    contract = bound["selected_outcome_contract"]
    assert contract["kind"] == "selected_option_outcome_strategy"
    assert contract["strategy"] == option["causal_coverage"]["outcome_strategy"]
    assert contract["outcome_contract_id"].startswith("stage5_outcome_contract:")
    assert bound["outcome_claim_status"] == "resolved"
    assert (
        falsification_review_receipt_errors(
            bound,
            problem_id="problem:test",
            selected_option=option,
            research=research,
        )
        == []
    )


def test_stage5_rejects_surface_only_option_outcome_strategy() -> None:
    research, option, evidence_id = _research_without_future_outcome()
    review = _outcome_strategy_falsification_review(evidence_id)
    strategy_review = review["outcome_strategy_review"]
    assert isinstance(strategy_review, dict)
    strategy_review["verdict"] = "surface_only"
    strategy_review["proves_intended_operation"] = False

    with pytest.raises(
        ValueError,
        match="falsification_accepts_insufficient_outcome_semantics",
    ):
        bind_falsification_review(
            review,
            problem_id="problem:test",
            selected_option=option,
            research=research,
        )


def test_material_risk_disposition_names_missing_evidence_refs_field() -> None:
    research, paths = _runner_research()
    option = _broad_option(
        first_ref=str(paths[0]["failure_path_id"]),
        second_ref=str(paths[1]["failure_path_id"]),
    )
    verification = research["evidence_verification"]
    assert isinstance(verification, dict)
    evidence = verification["mechanism_evidence"]
    assert isinstance(evidence, list)
    evidence_id = str(evidence[0]["mechanism_evidence_id"])
    review = _falsification_review(evidence_id, research=research)
    risk = "The selected option retains one bounded compatibility risk."
    coverage = option["causal_coverage"]
    assert isinstance(coverage, dict)
    coverage["compatibility_risks"] = [risk]
    review["material_risk_dispositions"] = [
        {
            "risk": risk,
            "disposition": "accepted",
            "mechanism_evidence_ids": [evidence_id],
            "rationale": "The risk is explicit and bounded.",
        }
    ]

    with pytest.raises(
        ValueError,
        match="falsification_risk_evidence_refs_missing:0",
    ):
        bind_falsification_review(
            review,
            problem_id="problem:test",
            selected_option=option,
            research=research,
        )


def test_surface_only_stage3_baseline_does_not_veto_sufficient_stage4_strategy() -> None:
    research, paths = _runner_research()
    option = _broad_option(
        first_ref=str(paths[0]["failure_path_id"]),
        second_ref=str(paths[1]["failure_path_id"]),
        first_name=str(paths[0]["path_name"]),
        second_name=str(paths[1]["path_name"]),
    )
    verification = research["evidence_verification"]
    assert isinstance(verification, dict)
    evidence_id = str(verification["verified_mechanism_provenance"]["mechanism_evidence_ids"][0])
    review = _falsification_review(evidence_id, research=research)
    baseline_review = review["outcome_contract_reviews"][0]
    assert isinstance(baseline_review, dict)
    baseline_review.update(
        {
            "verdict": "surface_only",
            "semantic_relation_assessment": (
                "The Stage-3 contract authenticates the pre-change scalar baseline but does "
                "not express the option's prospective useful-operation recovery."
            ),
            "proves_intended_operation": False,
            "problem_coverage": "partial",
            "residual_untested_paths": [
                "The baseline contract does not prove future useful-operation recovery."
            ],
        }
    )

    bound = bind_falsification_review(
        review,
        problem_id="problem:test",
        selected_option=option,
        research=research,
    )

    assert bound["outcome_strategy_review"]["verdict"] == "sufficient"
    assert bound["outcome_contract_reviews"][0]["verdict"] == "surface_only"
    assert bound["selected_outcome_contract"]["strategy"] == option["causal_coverage"][
        "outcome_strategy"
    ]
    assert bound["selected_outcome_contract"]["post_change_evidence_status"] == "unverified"
    assert bound["outcome_contract_status"] == "approved_for_planning"
    assert bound["post_change_evidence_status"] == "unverified"
    assert (
        falsification_review_receipt_errors(
            bound,
            problem_id="problem:test",
            selected_option=option,
            research=research,
        )
        == []
    )


def test_stage3_baseline_review_is_optional_additional_evidence() -> None:
    research, paths = _runner_research()
    option = _broad_option(
        first_ref=str(paths[0]["failure_path_id"]),
        second_ref=str(paths[1]["failure_path_id"]),
        first_name=str(paths[0]["path_name"]),
        second_name=str(paths[1]["path_name"]),
    )
    verification = research["evidence_verification"]
    assert isinstance(verification, dict)
    evidence_id = str(verification["verified_mechanism_provenance"]["mechanism_evidence_ids"][0])
    review = _falsification_review(evidence_id, research=research)
    review.pop("selected_positive_outcome_contract_id")
    review.pop("outcome_contract_reviews")

    bound = bind_falsification_review(
        review,
        problem_id="problem:test",
        selected_option=option,
        research=research,
    )

    assert bound["selected_positive_outcome_contract_ids"] == []
    assert bound["outcome_contract_reviews"] == []
    assert bound["selected_outcome_contract"][
        "research_baseline_positive_outcome_contract_ids"
    ]


def test_stage6_binds_exact_plan_checks_to_stage5_outcome_contract() -> None:
    research, option, evidence_id = _research_without_future_outcome()
    review = bind_falsification_review(
        _outcome_strategy_falsification_review(evidence_id),
        problem_id="problem:test",
        selected_option=option,
        research=research,
    )
    selection = {
        "selected_option_id": option["option_id"],
        "selected_option": option,
        "falsification_review": review,
    }
    command = "pytest tests/test_shared.py::test_failure -q"
    plan = {
        "case_id": "case:test",
        "before_after_reproduction": {
            "research_experiment_id": "experiment:support",
            "after_change": {
                "command": command,
                "expected_exit_code": 0,
                "expected_result": "The original operation preserves provenance.",
                "observable_assertions": [
                    {
                        "source": "stdout",
                        "operator": "contains",
                        "expected": "provenance=preserved",
                    }
                ],
            },
        },
        "outcome_verification_roles": {
            "original_scenario": {
                "description": "Replay the original failing scenario after the change."
            },
            "live": None,
            "mitigation_effect": None,
            "recurrence": {
                "description": "Check later canonical cycles.",
                "verification_owner": "centralized_case_refresh",
                "commands": [],
                "predicates": [],
            },
        },
    }

    bound = bind_plan_outcome_oracle(plan, research=research, selection=selection)
    original = bound["outcome_verification_roles"]["original_scenario"]
    assert original["commands"] == [command]
    assert original["oracle"]["kind"] == "stage5_planned_outcome"
    assert original["oracle"]["selected_outcome_contract"] == review[
        "selected_outcome_contract"
    ]
    assert {
        "type": "command_stdout_contains",
        "command_index": 0,
        "value": "provenance=preserved",
    } in original["predicates"]


def _two_oracle_falsification_fixture() -> tuple[
    dict[str, object], dict[str, object], dict[str, object], list[str]
]:
    research, paths = _runner_research()
    option = _broad_option(
        first_ref=str(paths[0]["failure_path_id"]),
        second_ref=str(paths[1]["failure_path_id"]),
        first_name=str(paths[0]["path_name"]),
        second_name=str(paths[1]["path_name"]),
    )
    member_one = json.loads(json.dumps(research))
    member_one["case_id"] = "case:one"
    member_one["problem_id"] = "problem:test"
    member_two = json.loads(json.dumps(research))
    member_two["case_id"] = "case:two"
    member_two["problem_id"] = "problem:test:two"

    verification_one = member_one["evidence_verification"]
    verification_two = member_two["evidence_verification"]
    assert isinstance(verification_one, dict)
    assert isinstance(verification_two, dict)
    oracle_one = verification_one["outcome_oracles"][0]
    oracle_two = verification_two["outcome_oracles"][0]
    assert isinstance(oracle_one, dict)
    assert isinstance(oracle_two, dict)
    contract_one = oracle_one["positive_outcome_contracts"][0]
    contract_two = oracle_two["positive_outcome_contracts"][0]
    assert isinstance(contract_one, dict)
    assert isinstance(contract_two, dict)

    contract_two["research_experiment_id"] = "experiment:support:two"
    contract_two["positive_outcome_contract_id"] = _content_id(
        "positive_outcome_contract",
        contract_two,
        "positive_outcome_contract_id",
    )
    oracle_two["case_id"] = "case:two"
    oracle_two["research_experiment_id"] = "experiment:support:two"
    oracle_two["outcome_oracle_id"] = _content_id(
        "outcome_oracle",
        oracle_two,
        "outcome_oracle_id",
    )

    bundle: dict[str, object] = {
        "member_research_dossiers": [member_one, member_two],
    }
    bundle["bundle_sha256"] = _canonical_hash(bundle)
    research["post_research_same_mechanism_bundle"] = bundle

    selected_ids = [
        str(contract_one["positive_outcome_contract_id"]),
        str(contract_two["positive_outcome_contract_id"]),
    ]
    evidence_id = str(contract_one["mechanism_evidence_ids"][0])
    review = _falsification_review(evidence_id, research=research)
    review["selected_positive_outcome_contract_id"] = None
    review["selected_positive_outcome_contract_ids"] = selected_ids
    review["outcome_contract_reviews"] = [
        {
            "positive_outcome_contract_id": contract_id,
            "verdict": "sufficient",
            "semantic_relation_assessment": (
                "The runner-bound postcondition proves the retained scenario works."
            ),
            "proves_intended_operation": True,
            "problem_coverage": "full",
            "residual_untested_paths": [],
            "evidence_refs": [evidence_id],
        }
        for contract_id in selected_ids
    ]
    return research, option, review, selected_ids


def test_falsifier_rejects_missing_selected_contract_for_retained_oracle() -> None:
    research, option, review, selected_ids = _two_oracle_falsification_fixture()
    review["selected_positive_outcome_contract_ids"] = selected_ids[:1]

    with pytest.raises(
        ValueError,
        match="falsification_selected_outcome_contract_oracle_coverage_mismatch",
    ):
        bind_falsification_review(
            review,
            problem_id="problem:test",
            selected_option=option,
            research=research,
        )


def test_falsifier_binds_one_selected_contract_per_retained_oracle() -> None:
    research, option, review, selected_ids = _two_oracle_falsification_fixture()

    bound = bind_falsification_review(
        review,
        problem_id="problem:test",
        selected_option=option,
        research=research,
    )

    assert bound["selected_positive_outcome_contract_id"] is None
    assert bound["selected_positive_outcome_contract_ids"] == selected_ids
    assert bound["outcome_claim_status"] == "resolved"
    assert bound["outcome_confidence"] == "full"
    assert (
        bound["adversarial_evidence_receipt"]["selected_positive_outcome_contract_ids"]
        == selected_ids
    )
    assert (
        falsification_review_receipt_errors(
            bound,
            problem_id="problem:test",
            selected_option=option,
            research=research,
        )
        == []
    )


def test_bounded_noncritical_residual_accepts_only_as_mitigated() -> None:
    research, paths = _runner_research()
    option = _broad_option(
        first_ref=str(paths[0]["failure_path_id"]),
        second_ref=str(paths[1]["failure_path_id"]),
        first_name=str(paths[0]["path_name"]),
        second_name=str(paths[1]["path_name"]),
    )
    verification = research["evidence_verification"]
    assert isinstance(verification, dict)
    evidence = verification["mechanism_evidence"]
    assert isinstance(evidence, list)
    evidence_id = str(evidence[0]["mechanism_evidence_id"])
    risk = "The second platform-specific replay remains untested."
    review = _falsification_review(evidence_id, research=research)
    review["residual_risks"] = [risk]
    review["material_risk_dispositions"] = [
        {
            "risk": risk,
            "disposition": "accepted",
            "evidence_refs": [evidence_id],
            "rationale": "The retained evidence bounds this to the second platform path.",
        }
    ]
    strategy_review = review["outcome_strategy_review"]
    assert isinstance(strategy_review, dict)
    strategy_review["problem_coverage"] = "partial"
    strategy_review["residual_untested_paths"] = [risk]

    bound = bind_falsification_review(
        review,
        problem_id="problem:test",
        selected_option=option,
        research=research,
    )

    assert bound["outcome_claim_status"] == "mitigated"
    assert bound["outcome_confidence"] == "bounded"
    assert bound["adversarial_evidence_receipt"]["outcome_claim_status"] == (
        "mitigated"
    )
    assert (
        falsification_review_receipt_errors(
            bound,
            problem_id="problem:test",
            selected_option=option,
            research=research,
        )
        == []
    )
    selection = {
        "problem_id": "problem:test",
        "selected_option_id": "option:test:shared",
        "selected_family_id": "most_direct",
        "selection_rationale": "The verified path addresses the established mechanism.",
        "repo_intent_alignment": "The change remains within the existing boundary.",
        "why_other_options_were_not_selected": "No alternative mechanism was evidenced.",
        "needs_ux_review": False,
        "causal_coverage_evaluation": {
            "mechanism_fit": "The option targets the verified mechanism.",
            "accepted_unsupported_assumptions": [],
            "accepted_residual_risks": [risk],
            "class_level_evidence_sufficient": True,
        },
        "falsification_review": bound,
        "change_surface": {"user_visible": False, "kinds": ["internal"]},
    }
    ready, reasons = assess_selection_readiness(
        selection,
        options=[option],
        research=research,
    )
    assert ready is True
    assert reasons == []


def test_evidenced_compatibility_risk_bounds_confidence_not_root_cause_outcome() -> None:
    research, paths = _runner_research()
    option = _broad_option(
        first_ref=str(paths[0]["failure_path_id"]),
        second_ref=str(paths[1]["failure_path_id"]),
        first_name=str(paths[0]["path_name"]),
        second_name=str(paths[1]["path_name"]),
    )
    coverage = option["causal_coverage"]
    assert isinstance(coverage, dict)
    risk = "Existing successful consumers must preserve their result contract."
    coverage["compatibility_risks"] = [risk]
    verification = research["evidence_verification"]
    assert isinstance(verification, dict)
    evidence = verification["mechanism_evidence"]
    assert isinstance(evidence, list)
    evidence_id = str(evidence[0]["mechanism_evidence_id"])
    review = _falsification_review(evidence_id, research=research)
    review["material_risk_dispositions"] = [
        {
            "risk": risk,
            "disposition": "mitigated",
            "evidence_refs": [evidence_id],
            "rationale": "The selected compatibility replay is an explicit regression oracle.",
        }
    ]

    bound = bind_falsification_review(
        review,
        problem_id="problem:test",
        selected_option=option,
        research=research,
    )

    assert bound["outcome_claim_status"] == "resolved"
    assert bound["outcome_confidence"] == "bounded"
    assert (
        falsification_review_receipt_errors(
            bound,
            problem_id="problem:test",
            selected_option=option,
            research=research,
        )
        == []
    )


def test_full_outcome_remains_resolved_with_accepted_scope_and_future_risks() -> None:
    research, paths = _runner_research()
    option = _broad_option(
        first_ref=str(paths[0]["failure_path_id"]),
        second_ref=str(paths[1]["failure_path_id"]),
        first_name=str(paths[0]["path_name"]),
        second_name=str(paths[1]["path_name"]),
    )
    coverage = option["causal_coverage"]
    assert isinstance(coverage, dict)
    scope_risk = "Uninspected callers may rely on the previous exception contract."
    recurrence_risk = "A future edit may reintroduce the verified failure."
    compatibility_risk = "External callers may observe a compatibility change."
    review_scope_risk = "No complete caller inventory proves universal compatibility."
    review_recurrence_risk = "Removing the regression oracle could allow recurrence."
    coverage["unsupported_assumptions"] = [scope_risk]
    coverage["residual_recurrence_paths"] = [recurrence_risk]
    coverage["compatibility_risks"] = [compatibility_risk]
    option["scope_evidence"] = {
        "scope_level": "single_path",
        "independent_consumers_or_failure_paths": [
            {
                "name": str(paths[0]["path_name"]),
                "evidence_refs": [str(paths[0]["failure_path_id"])],
            }
        ],
    }
    verification = research["evidence_verification"]
    assert isinstance(verification, dict)
    evidence = verification["mechanism_evidence"]
    assert isinstance(evidence, list)
    evidence_id = str(evidence[0]["mechanism_evidence_id"])
    review = _falsification_review(evidence_id, research=research)
    review["unsupported_assumptions"] = [review_scope_risk]
    review["residual_risks"] = [review_recurrence_risk]
    review["material_risk_dispositions"] = [
        {
            "risk": risk,
            "disposition": disposition,
            "evidence_refs": [evidence_id],
            "rationale": rationale,
        }
        for risk, disposition, rationale in (
            (
                scope_risk,
                "accepted",
                "The verified claim is limited to the source problem rather than all callers.",
            ),
            (
                recurrence_risk,
                "mitigated",
                "The retained outcome oracle detects recurrence on the verified path.",
            ),
            (
                compatibility_risk,
                "accepted",
                "Compatibility remains a disclosed implementation risk outside the proof claim.",
            ),
            (
                review_scope_risk,
                "accepted",
                "The source problem is fully covered without claiming universal compatibility.",
            ),
            (
                review_recurrence_risk,
                "mitigated",
                "The retained oracle checks the verified failure path after implementation.",
            ),
        )
    ]

    bound = bind_falsification_review(
        review,
        problem_id="problem:test",
        selected_option=option,
        research=research,
    )

    assert bound["outcome_claim_status"] == "resolved"
    assert bound["outcome_confidence"] == "bounded"
    assert (
        falsification_review_receipt_errors(
            bound,
            problem_id="problem:test",
            selected_option=option,
            research=research,
        )
        == []
    )


def test_evidenced_mitigation_cannot_erase_unsupported_root_cause_assumption() -> None:
    research, paths = _runner_research()
    option = _broad_option(
        first_ref=str(paths[0]["failure_path_id"]),
        second_ref=str(paths[1]["failure_path_id"]),
        first_name=str(paths[0]["path_name"]),
        second_name=str(paths[1]["path_name"]),
    )
    coverage = option["causal_coverage"]
    assert isinstance(coverage, dict)
    risk = "The upstream producer may bypass the selected causal boundary."
    coverage["unsupported_assumptions"] = [risk]
    verification = research["evidence_verification"]
    assert isinstance(verification, dict)
    evidence = verification["mechanism_evidence"]
    assert isinstance(evidence, list)
    evidence_id = str(evidence[0]["mechanism_evidence_id"])
    review = _falsification_review(evidence_id, research=research)
    review["material_risk_dispositions"] = [
        {
            "risk": risk,
            "disposition": "mitigated",
            "evidence_refs": [evidence_id],
            "rationale": "The replay reduces but does not eliminate this root-cause gap.",
        }
    ]

    bound = bind_falsification_review(
        review,
        problem_id="problem:test",
        selected_option=option,
        research=research,
    )

    assert bound["outcome_claim_status"] == "mitigated"
    assert bound["outcome_confidence"] == "bounded"


def test_partial_outcome_with_undisposed_residual_cannot_be_accepted() -> None:
    research, paths = _runner_research()
    option = _broad_option(
        first_ref=str(paths[0]["failure_path_id"]),
        second_ref=str(paths[1]["failure_path_id"]),
    )
    verification = research["evidence_verification"]
    assert isinstance(verification, dict)
    evidence = verification["mechanism_evidence"]
    assert isinstance(evidence, list)
    evidence_id = str(evidence[0]["mechanism_evidence_id"])
    review = _falsification_review(evidence_id, research=research)
    strategy_review = review["outcome_strategy_review"]
    assert isinstance(strategy_review, dict)
    strategy_review["problem_coverage"] = "partial"
    strategy_review["residual_untested_paths"] = ["Unbounded platform path"]

    with pytest.raises(
        ValueError,
        match="falsification_accepts_undisposed_outcome_strategy_residual",
    ):
        bind_falsification_review(
            review,
            problem_id="problem:test",
            selected_option=option,
            research=research,
        )


def test_critical_finding_still_blocks_selection() -> None:
    research, paths = _runner_research()
    option = _broad_option(
        first_ref=str(paths[0]["failure_path_id"]),
        second_ref=str(paths[1]["failure_path_id"]),
    )
    verification = research["evidence_verification"]
    assert isinstance(verification, dict)
    evidence = verification["mechanism_evidence"]
    assert isinstance(evidence, list)
    evidence_id = str(evidence[0]["mechanism_evidence_id"])
    review = _falsification_review(evidence_id, research=research)
    review["critical_findings"] = [
        {
            "finding": "The proposed boundary does not control the root mechanism.",
            "affects": "root cause and change surface",
            "evidence_refs": [evidence_id],
        }
    ]
    bound = bind_falsification_review(
        review,
        problem_id="problem:test",
        selected_option=option,
        research=research,
    )
    selection = {
        "problem_id": "problem:test",
        "selected_option_id": "option:test:shared",
        "selected_family_id": "most_direct",
        "selection_rationale": "The boundary otherwise matches the mechanism.",
        "repo_intent_alignment": "The change stays within the repository intent.",
        "why_other_options_were_not_selected": "No alternative was evidenced.",
        "needs_ux_review": False,
        "causal_coverage_evaluation": {
            "mechanism_fit": "The candidate targets the verified mechanism.",
            "accepted_unsupported_assumptions": [],
            "accepted_residual_risks": [],
            "class_level_evidence_sufficient": True,
        },
        "falsification_review": bound,
        "change_surface": {"user_visible": False, "kinds": ["internal"]},
    }

    ready, reasons = assess_selection_readiness(
        selection,
        options=[option],
        research=research,
    )

    assert ready is False
    assert "selection_falsification_accepts_critical_finding" in reasons


def test_falsifier_binds_typed_mechanism_evidence() -> None:
    research, paths = _runner_research()
    option = _broad_option(
        first_ref=str(paths[0]["failure_path_id"]),
        second_ref=str(paths[1]["failure_path_id"]),
        first_name=str(paths[0]["path_name"]),
        second_name=str(paths[1]["path_name"]),
    )
    verification = research["evidence_verification"]
    assert isinstance(verification, dict)
    evidence = verification["mechanism_evidence"]
    assert isinstance(evidence, list)
    evidence_id = str(evidence[0]["mechanism_evidence_id"])

    bound = bind_falsification_review(
        _falsification_review(evidence_id, research=research),
        problem_id="problem:test",
        selected_option=option,
        research=research,
    )

    assert bound["evidence_refs"][0]["effect"] == "limits_scope"
    assert bound["adversarial_evidence_receipt"]["binding_method"] == (
        "runner_causal_falsification_binding_v1"
    )
    assert (
        falsification_review_receipt_errors(
            bound,
            problem_id="problem:test",
            selected_option=option,
            research=research,
        )
        == []
    )


def _typed_support_with_replayed_falsification() -> tuple[
    dict[str, object], dict[str, object], str
]:
    research, paths = _runner_research()
    option = _broad_option(
        first_ref=str(paths[0]["failure_path_id"]),
        second_ref=str(paths[1]["failure_path_id"]),
        first_name=str(paths[0]["path_name"]),
        second_name=str(paths[1]["path_name"]),
    )
    verification = research["evidence_verification"]
    assert isinstance(verification, dict)
    evidence_items = verification["mechanism_evidence"]
    assert isinstance(evidence_items, list)
    evidence = dict(evidence_items[0])
    evidence["evidence_type"] = "exception_trace"
    evidence["experiment_ids"] = ["experiment:support", "experiment:challenge"]
    evidence["adversarial_effect"] = "supports_selection"
    evidence.pop("controlled_condition", None)
    evidence.pop("observable_difference", None)
    evidence.pop("strong_pytest_control_id", None)
    evidence["mechanism_evidence_id"] = _content_id(
        "mechanism_evidence", evidence, "mechanism_evidence_id"
    )
    verification["mechanism_evidence"] = [evidence]
    _set_synthetic_positive_contract(
        research,
        str(evidence["mechanism_evidence_id"]),
    )
    return research, option, str(evidence["mechanism_evidence_id"])


def test_falsification_accepts_hypothesis_that_survived_replayed_challenge() -> None:
    research, option, evidence_id = _typed_support_with_replayed_falsification()

    bound = bind_falsification_review(
        _falsification_review(evidence_id, research=research),
        problem_id="problem:test",
        selected_option=option,
        research=research,
    )

    assert bound["evidence_refs"][0]["effect"] == "supports_selection"
    attempts = bound["adversarial_evidence_receipt"]["falsification_attempts"]
    assert len(attempts) == 1
    assert attempts[0]["attempt_id"] == "falsify:h1:alternative"
    assert attempts[0]["outcome"] == "survived"
    assert attempts[0]["command"] == "pytest tests/test_shared.py::test_alternative -q"
    assert falsification_acceptance_has_adversarial_basis(bound) is True


def test_deterministic_closure_advances_without_invented_falsification() -> None:
    research, paths = _runner_research()
    hypothesis = research["root_cause_hypotheses"][0]
    assert isinstance(hypothesis, dict)
    hypothesis["falsification_attempts"] = []
    verification = research["evidence_verification"]
    assert isinstance(verification, dict)
    verification["falsification_interventions"] = []
    replay = next(
        item
        for item in verification["experiments"]
        if item["experiment_id"] == "experiment:support"
    )
    experiment = next(
        item for item in research["experiments"] if item["experiment_id"] == "experiment:support"
    )
    executed_argv = ["pytest", "tests/test_shared.py::test_failure", "-q"]
    command_authorization = {
        "authorization_kind": "immutable_source_command",
        "executed_argv_sha256": _canonical_hash(executed_argv),
        "shell": False,
        "workspace_confined": True,
        "origin_atom_id": "atom:a",
        "origin_atom_sha256": "5" * 64,
        "origin_atom_field_path": "$.command",
        "origin_command_value_sha256": "6" * 64,
    }
    mechanism_link = {
        "verification_method": "runner_python_call_chain_v1",
        "entrypoint": "shared.apply",
        "code_path": [{"symbol": "shared.apply", "path": "src/shared.py"}],
    }
    causal_root = {
        "kind": "immutable_source_command",
        "experiment_ids": ["experiment:support"],
        "origin_atom_ids": ["atom:a"],
        "origin_atom_sha256": "5" * 64,
        "origin_atom_field_path": "$.command",
        "origin_command_value_sha256": "6" * 64,
        "executed_argv_sha256": _canonical_hash(executed_argv),
        "root_mechanism_symbol": "shared.apply",
    }
    mechanism: dict[str, object] = {
        "evidence_type": "observed_output",
        "hypothesis_id": "h1",
        "mechanism_symbols": ["shared.apply"],
        "code_paths": [{"symbol": "shared.apply", "path": "src/shared.py"}],
        "experiment_ids": ["experiment:support"],
        "origin_atom_ids": ["atom:a"],
        "executed_argv": executed_argv,
        "command_authorization": command_authorization,
        "mechanism_link": mechanism_link,
        "causal_root_bindings": [causal_root],
        "adversarial_effect": "supports_selection",
    }
    mechanism["mechanism_evidence_id"] = _content_id(
        "mechanism_evidence",
        mechanism,
        "mechanism_evidence_id",
    )
    verification["mechanism_evidence"] = [mechanism]
    mechanism_evidence_id = str(mechanism["mechanism_evidence_id"])
    _set_synthetic_positive_contract(research, mechanism_evidence_id)
    closure: dict[str, object] = {
        "verification_method": "runner_deterministic_mechanism_closure_v2",
        "hypothesis_id": "h1",
        "support_experiment_ids": ["experiment:support"],
        "mechanism_evidence_ids": [mechanism_evidence_id],
        "causal_root_evidence_ids": [mechanism_evidence_id],
        "mechanism_symbols": ["shared.apply"],
        "code_path": [{"symbol": "shared.apply", "path": "src/shared.py"}],
        "closure_basis": "rooted_connected_support_component",
        "support_connectivity": [
            {
                "mechanism_evidence_id": mechanism_evidence_id,
                "experiment_ids": ["experiment:support"],
                "connection_kind": "causal_root",
                "connected_from_mechanism_evidence_id": None,
                "shared_verified_symbols": [],
                "verified_causal_edge": None,
                "verified_causal_edges": [],
                "causal_root_kinds": ["immutable_source_command"],
            }
        ],
        "alternatives_disposed": [],
        "origin_atom_ids": ["atom:a"],
        "observed_results": [
            {
                "experiment_id": "experiment:support",
                "scenario_kind": "original_replay",
                "exit_code": replay["exit_code"],
                "stdout_sha256": replay["stdout_sha256"],
                "stderr_sha256": replay["stderr_sha256"],
                "assertion": experiment["observable_assertion"],
            }
        ],
    }
    closure["closure_receipt_id"] = _content_id(
        "deterministic_mechanism_closure",
        closure,
        "closure_receipt_id",
    )
    verification["deterministic_mechanism_closures"] = [closure]
    option = _broad_option(
        first_ref=str(paths[0]["failure_path_id"]),
        second_ref=str(paths[1]["failure_path_id"]),
        first_name=str(paths[0]["path_name"]),
        second_name=str(paths[1]["path_name"]),
    )
    binding = option["causal_coverage"]["research_binding"]
    binding["falsification_attempt_refs"] = []
    binding["deterministic_closure_refs"] = [closure["closure_receipt_id"]]

    ready, reasons = assess_solution_option_readiness(option, research=research)
    assert ready is True
    assert reasons == []

    evidence = verification["mechanism_evidence"]
    bound = bind_falsification_review(
        _falsification_review(
            str(evidence[0]["mechanism_evidence_id"]),
            research=research,
        ),
        problem_id="problem:test",
        selected_option=option,
        research=research,
    )
    assert falsification_acceptance_has_adversarial_basis(bound) is True
    receipt = bound["adversarial_evidence_receipt"]
    assert receipt["falsification_attempts"] == []
    assert receipt["deterministic_mechanism_closures"] == [closure]
    assert (
        falsification_review_receipt_errors(
            bound,
            problem_id="problem:test",
            selected_option=option,
            research=research,
        )
        == []
    )
    selection = {
        "problem_id": "problem:test",
        "selected_option_id": "option:test:shared",
        "selected_family_id": "most_direct",
        "selection_rationale": "The verified boundary addresses both paths.",
        "repo_intent_alignment": "The change stays at the existing boundary.",
        "why_other_options_were_not_selected": "No other mechanism is evidenced.",
        "needs_ux_review": False,
        "causal_coverage_evaluation": {
            "mechanism_fit": "Exact verified mechanism.",
            "accepted_unsupported_assumptions": [],
            "accepted_residual_risks": [],
            "class_level_evidence_sufficient": True,
        },
        "falsification_review": bound,
        "change_surface": {"user_visible": False, "kinds": ["internal"]},
    }

    ready, reasons = assess_selection_readiness(
        selection,
        options=[option],
        research=research,
    )

    assert ready is True
    assert reasons == []


def test_falsification_rejects_unverified_causal_challenge() -> None:
    research, option, evidence_id = _typed_support_with_replayed_falsification()
    verification = research["evidence_verification"]
    assert isinstance(verification, dict)
    verification["experiments"][1]["assertion_passed"] = False

    bound = bind_falsification_review(
        _falsification_review(evidence_id, research=research),
        problem_id="problem:test",
        selected_option=option,
        research=research,
    )

    assert bound["adversarial_evidence_receipt"]["falsification_attempts"] == []
    assert falsification_acceptance_has_adversarial_basis(bound) is False
    selection = {
        "problem_id": "problem:test",
        "selected_option_id": "option:test:shared",
        "selected_family_id": "most_direct",
        "selection_rationale": "The verified boundary addresses both paths.",
        "repo_intent_alignment": "The change stays at the existing boundary.",
        "why_other_options_were_not_selected": "No other mechanism is evidenced.",
        "needs_ux_review": False,
        "causal_coverage_evaluation": {
            "mechanism_fit": "Exact verified mechanism.",
            "accepted_unsupported_assumptions": [],
            "accepted_residual_risks": [],
            "class_level_evidence_sufficient": True,
        },
        "falsification_review": bound,
        "change_surface": {"user_visible": False, "kinds": ["internal"]},
    }

    ready, reasons = assess_selection_readiness(
        selection,
        options=[option],
        research=research,
    )

    assert ready is False
    assert "selection_falsification_accept_without_adversarial_evidence" in reasons


@pytest.mark.parametrize("bad_ref", ["artifact:a", "experiment:support", "src/shared.py"])
def test_falsifier_cannot_label_arbitrary_research_evidence_adversarial(
    bad_ref: str,
) -> None:
    research, paths = _runner_research()
    option = _broad_option(
        first_ref=str(paths[0]["failure_path_id"]),
        second_ref=str(paths[1]["failure_path_id"]),
        first_name=str(paths[0]["path_name"]),
        second_name=str(paths[1]["path_name"]),
    )
    verification = research["evidence_verification"]
    assert isinstance(verification, dict)
    evidence = verification["mechanism_evidence"]
    assert isinstance(evidence, list)
    valid_evidence_id = str(evidence[0]["mechanism_evidence_id"])
    review = _falsification_review(valid_evidence_id, research=research)
    review["evidence_refs"][0]["ref"] = bad_ref

    with pytest.raises(ValueError, match="falsification_evidence_ref_unbound"):
        bind_falsification_review(
            review,
            problem_id="problem:test",
            selected_option=option,
            research=research,
        )


def test_falsifier_receipt_detects_selected_option_and_receipt_tampering() -> None:
    research, paths = _runner_research()
    option = _broad_option(
        first_ref=str(paths[0]["failure_path_id"]),
        second_ref=str(paths[1]["failure_path_id"]),
        first_name=str(paths[0]["path_name"]),
        second_name=str(paths[1]["path_name"]),
    )
    verification = research["evidence_verification"]
    assert isinstance(verification, dict)
    evidence = verification["mechanism_evidence"]
    assert isinstance(evidence, list)
    bound = bind_falsification_review(
        _falsification_review(
            str(evidence[0]["mechanism_evidence_id"]),
            research=research,
        ),
        problem_id="problem:test",
        selected_option=option,
        research=research,
    )

    changed_option = dict(option)
    changed_option["summary"] = "tampered scope"
    assert "selection_falsification_server_receipt_changed" in (
        falsification_review_receipt_errors(
            bound,
            problem_id="problem:test",
            selected_option=changed_option,
            research=research,
        )
    )
    receipt = bound["adversarial_evidence_receipt"]
    assert isinstance(receipt, dict)
    receipt["selected_option_sha256"] = "0" * 64
    assert "selection_falsification_server_receipt_changed" in (
        falsification_review_receipt_errors(
            bound,
            problem_id="problem:test",
            selected_option=option,
            research=research,
        )
    )


def _observable_change_plan_fixture(
    *,
    baseline_exit: int = 0,
    after_exit: int = 0,
    expected_outcome_state: str = "resolved",
) -> tuple[dict[str, object], dict[str, object], dict[str, object], dict[str, object]]:
    command = "python scripts/replay_original.py"
    baseline_assertion = {
        "source": "stderr",
        "operator": "contains",
        "expected": "incorrect policy classification",
    }
    after_assertion = {
        "source": "stderr",
        "operator": "not_contains",
        "expected": "incorrect policy classification",
    }
    correct_assertion = {
        "source": "stdout",
        "operator": "contains",
        "expected": "classification=incomplete",
    }
    problem = {
        "case_id": "case:oracle",
        "problem_id": "problem:oracle",
        "title": "Classifier emits the wrong diagnostic",
        "problem": "A pure classifier reports the wrong reason.",
    }
    research = {
        "repo_revision": "abc123",
        "experiments": [
            {
                "experiment_id": "exp-original",
                "scenario_kind": "original_replay",
                "command": command,
                "outcome": "supports",
                "exit_code": baseline_exit,
                "observable_assertion": baseline_assertion,
            }
        ],
        "artifact_refs": [],
        "evidence_verification": {
            "status": "verified",
            "experiments": [
                {
                    "experiment_id": "exp-mitigation",
                    "command": "python scripts/verify_corrected_diagnostic.py",
                }
            ],
        },
    }
    selection = {"selected_option_id": "option:oracle"}
    original_predicates: list[dict[str, object]] = [
        {"type": "command_exit_code", "command_index": 0, "equals": after_exit},
        {
            "type": "command_stderr_not_contains",
            "command_index": 0,
            "value": "incorrect policy classification",
        },
        {
            "type": "command_stdout_contains",
            "command_index": 0,
            "value": "classification=incomplete",
        },
    ]
    mitigation_role = (
        {
            "description": "The remaining provider failure is diagnosed correctly.",
            "commands": ["python scripts/verify_corrected_diagnostic.py"],
            "command_bindings": [
                {
                    "command_index": 0,
                    "research_experiment_id": "exp-mitigation",
                }
            ],
            "predicates": [
                {"type": "command_exit_code", "command_index": 0, "equals": 0},
                {
                    "type": "command_stdout_contains",
                    "command_index": 0,
                    "value": "correct diagnosis",
                },
            ],
        }
        if expected_outcome_state == "mitigated"
        else None
    )
    targets = [
        {
            "action": "modify",
            "path": "src/classifier.py",
            "symbols": ["classifier.classify"],
            "change": "Classify the retained condition from its actual cause.",
        }
    ]
    plan = {
        "change_plan_id": "plan:oracle",
        "case_id": "case:oracle",
        "problem_id": "problem:oracle",
        "selected_option_id": "option:oracle",
        "title": "Correct the classifier mechanism",
        "problem": "The classifier emits a false policy diagnosis.",
        "user_impact": "Users pursue the wrong recovery action.",
        "proposed_fix": "Derive the diagnosis from the verified classifier input.",
        "repo_revision": "abc123",
        "change_targets": targets,
        "target_contract": {
            "case_id": "case:oracle",
            "problem_id": "problem:oracle",
            "selected_option_id": "option:oracle",
            "repo_revision": "abc123",
            "targets": targets,
        },
        "implementation_steps": ["Update `classifier.classify` to preserve the verified cause."],
        "verification_steps": ["Replay the original classifier scenario."],
        "verification_commands": [command],
        "outcome_verification_roles": {
            "original_scenario": {
                "description": "Replay the original classifier scenario.",
                "research_experiment_id": "exp-original",
                "commands": [command],
                "predicates": original_predicates,
            },
            "live": None,
            "mitigation_effect": mitigation_role,
            "recurrence": {
                "description": "Check later canonical-case cycles for recurrence.",
                "verification_owner": "centralized_case_refresh",
                "commands": [],
                "predicates": [],
            },
        },
        "before_after_reproduction": {
            "original_scenario": "Replay the retained classifier input.",
            "research_experiment_id": "exp-original",
            "expected_outcome_state": expected_outcome_state,
            "before_change": {
                "command": command,
                "expected_exit_code": baseline_exit,
                "expected_result": "The wrong diagnostic is emitted.",
                "observable_assertion": baseline_assertion,
            },
            "after_change": {
                "command": command,
                "expected_exit_code": after_exit,
                "expected_result": "The wrong diagnostic is absent.",
                "observable_assertions": [after_assertion, correct_assertion],
            },
            "proof_limitation": None,
            "alternate_verification": None,
        },
        "compatibility_and_failure_modes": {
            "preserved_behaviors": ["True provider failures remain failures."],
            "intentional_changes": ["The diagnostic classification changes."],
            "failure_modes": ["Unknown causes remain explicitly unknown."],
            "migration_required": False,
        },
        "causal_coverage": {"mechanism_addressed": "False classification"},
        "scope_evidence": {"scope_level": "single_path"},
        "requires_live_verification": False,
        "live_verification_rationale": "The behavior is a pure classifier contract.",
        "success_criteria": ["The original wrong diagnostic is absent."],
        "rollback_notes": "Revert the classifier change.",
        "suggested_owner": "runner_core",
        "related_change_plan_ids": [],
    }
    return assign_plan_revision_id(plan), problem, research, selection


def test_nonoriginal_outcome_commands_use_evidence_bindings_not_tool_allowlists() -> None:
    live_command = "dotnet test tests/Runtime.Tests.csproj --filter LiveProbe"
    research = {
        "evidence_verification": {
            "status": "verified",
            "experiments": [
                {
                    "experiment_id": "exp-live-runtime",
                    "command": live_command,
                }
            ],
        }
    }
    roles = {
        "original_scenario": {
            "description": "Replay the original scenario.",
            "commands": ["python scripts/replay_original.py"],
            "predicates": [
                {"type": "command_exit_code", "command_index": 0, "equals": 0}
            ],
        },
        "live": {
            "description": "Exercise the runner-attested live route.",
            "commands": [live_command],
            "command_bindings": [
                {
                    "command_index": 0,
                    "research_experiment_id": "exp-live-runtime",
                }
            ],
            "predicates": [
                {"type": "command_exit_code", "command_index": 0, "equals": 0}
            ],
        },
        "mitigation_effect": None,
        "recurrence": {
            "description": "Use later canonical-case refresh evidence.",
            "verification_owner": "centralized_case_refresh",
            "commands": [],
            "predicates": [],
        },
    }

    bound_reasons = ticket_readiness._outcome_role_contract_errors(
        roles,
        verification_commands=["python scripts/unit_contract.py"],
        reproduction=None,
        requires_live=True,
        research=research,
    )
    assert not any("command_binding" in reason for reason in bound_reasons)
    assert not any("generic_test" in reason for reason in bound_reasons)

    live = roles["live"]
    assert isinstance(live, dict)
    live.pop("command_bindings")
    unbound_reasons = ticket_readiness._outcome_role_contract_errors(
        roles,
        verification_commands=["python scripts/unit_contract.py"],
        reproduction=None,
        requires_live=True,
        research=research,
    )
    assert "change_plan_outcome_role_command_bindings_invalid:live" in unbound_reasons


def test_centralized_recurrence_owner_is_the_only_empty_role_exception() -> None:
    roles = {
        "original_scenario": None,
        "live": None,
        "mitigation_effect": None,
        "recurrence": {
            "description": "Use two later canonical-case shadow snapshots.",
            "verification_owner": "centralized_case_refresh",
            "commands": [],
            "predicates": [],
        },
    }

    accepted = ticket_readiness._outcome_role_contract_errors(
        roles,
        verification_commands=["python scripts/unit_contract.py"],
        reproduction=None,
        requires_live=False,
        research=None,
    )
    assert not any("recurrence" in reason for reason in accepted)

    unowned = json.loads(json.dumps(roles))
    unowned["recurrence"].pop("verification_owner")
    rejected = ticket_readiness._outcome_role_contract_errors(
        unowned,
        verification_commands=["python scripts/unit_contract.py"],
        reproduction=None,
        requires_live=False,
        research=None,
    )
    assert "change_plan_outcome_role_commands_invalid:recurrence" in rejected
    assert "change_plan_outcome_role_predicates_invalid:recurrence" in rejected


def test_plan_owned_recurrence_still_requires_verified_command_binding() -> None:
    command = "python scripts/recurrence_probe.py"
    research = {
        "evidence_verification": {
            "status": "verified",
            "experiments": [{"experiment_id": "exp-recurrence", "command": command}],
        }
    }
    roles = {
        "original_scenario": None,
        "live": None,
        "mitigation_effect": None,
        "recurrence": {
            "description": "Run a verified problem-specific recurrence observation.",
            "commands": [command],
            "command_bindings": [
                {"command_index": 0, "research_experiment_id": "exp-recurrence"}
            ],
            "predicates": [
                {"type": "command_exit_code", "command_index": 0, "equals": 0}
            ],
        },
    }

    accepted = ticket_readiness._outcome_role_contract_errors(
        roles,
        verification_commands=["python scripts/unit_contract.py"],
        reproduction=None,
        requires_live=False,
        research=research,
    )
    assert not any("recurrence" in reason for reason in accepted)

    roles["recurrence"].pop("command_bindings")
    rejected = ticket_readiness._outcome_role_contract_errors(
        roles,
        verification_commands=["python scripts/unit_contract.py"],
        reproduction=None,
        requires_live=False,
        research=research,
    )
    assert "change_plan_outcome_role_command_bindings_invalid:recurrence" in rejected


def test_stage6_can_bind_prospective_live_command_without_claiming_execution() -> None:
    research, option, evidence_id = _research_without_future_outcome()
    research["repo_revision"] = "a" * 40
    review = bind_falsification_review(
        _outcome_strategy_falsification_review(evidence_id),
        problem_id="problem:test",
        selected_option=option,
        research=research,
    )
    selection = {
        "selected_option_id": option["option_id"],
        "selected_option": option,
        "falsification_review": review,
    }
    original_command = "pytest tests/test_shared.py::test_failure -q"
    bound = bind_plan_outcome_oracle(
        {
            "case_id": "case:test",
            "before_after_reproduction": {
                "research_experiment_id": "experiment:support",
                "after_change": {
                    "command": original_command,
                    "expected_exit_code": 0,
                    "expected_result": "The original operation preserves provenance.",
                    "observable_assertions": [
                        {
                            "source": "stdout",
                            "operator": "contains",
                            "expected": "provenance=preserved",
                        }
                    ],
                },
            },
            "outcome_verification_roles": {
                "original_scenario": {
                    "description": "Replay the original failing scenario after the change."
                },
                "live": None,
                "mitigation_effect": None,
                "recurrence": {
                    "description": "Check later canonical cycles.",
                    "verification_owner": "centralized_case_refresh",
                    "commands": [],
                    "predicates": [],
                },
            },
        },
        research=research,
        selection=selection,
    )
    roles = bound["outcome_verification_roles"]
    assert isinstance(roles, dict)
    contract_id = review["selected_outcome_contract"]["outcome_contract_id"]
    repo_revision = research["repo_revision"]
    roles["live"] = {
        "description": "Execute the selected post-change capacity recovery.",
        "execution_status": "planned_unverified",
        "commands": ["python scripts/live_capacity_recovery.py --verify"],
        "command_bindings": [
            {
                "command_index": 0,
                "binding_kind": "stage6_planned_post_change",
                "selected_outcome_contract_id": contract_id,
                "repo_revision": repo_revision,
            }
        ],
        "predicates": [
            {"type": "command_exit_code", "command_index": 0, "equals": 0},
            {
                "type": "command_stdout_contains",
                "command_index": 0,
                "value": "capacity_recovered=true",
            },
        ],
    }

    reasons = ticket_readiness._outcome_role_contract_errors(
        roles,
        verification_commands=[original_command],
        reproduction=bound["before_after_reproduction"],
        requires_live=True,
        research=research,
    )

    assert not any("command_binding" in reason for reason in reasons)
    assert not any("execution_status" in reason for reason in reasons)

    wrong_contract_roles = json.loads(json.dumps(roles))
    wrong_contract_roles["live"]["command_bindings"][0][
        "selected_outcome_contract_id"
    ] = "stage5_outcome_contract:wrong"
    wrong_contract_reasons = ticket_readiness._outcome_role_contract_errors(
        wrong_contract_roles,
        verification_commands=[original_command],
        reproduction=bound["before_after_reproduction"],
        requires_live=True,
        research=research,
    )
    assert (
        "change_plan_outcome_role_planned_command_binding_invalid:live:0"
        in wrong_contract_reasons
    )

    wrong_revision_roles = json.loads(json.dumps(roles))
    wrong_revision_roles["live"]["command_bindings"][0]["repo_revision"] = "b" * 40
    wrong_revision_reasons = ticket_readiness._outcome_role_contract_errors(
        wrong_revision_roles,
        verification_commands=[original_command],
        reproduction=bound["before_after_reproduction"],
        requires_live=True,
        research=research,
    )
    assert (
        "change_plan_outcome_role_planned_command_binding_invalid:live:0"
        in wrong_revision_reasons
    )

    missing_status_roles = json.loads(json.dumps(roles))
    missing_status_roles["live"].pop("execution_status")
    missing_status_reasons = ticket_readiness._outcome_role_contract_errors(
        missing_status_roles,
        verification_commands=[original_command],
        reproduction=bound["before_after_reproduction"],
        requires_live=True,
        research=research,
    )
    assert (
        "change_plan_outcome_role_planned_execution_status_invalid:live"
        in missing_status_reasons
    )


def test_option_strategy_prefers_verified_fail_first_over_exit_zero_old_behavior() -> None:
    research, paths = _runner_research()
    option = _broad_option(
        first_ref=str(paths[0]["failure_path_id"]),
        second_ref=str(paths[1]["failure_path_id"]),
    )
    old_behavior = {
        "experiment_id": "experiment:old-behavior",
        "scenario_kind": "faithful_replay",
        "command": "pytest tests/test_shared.py::test_old_behavior -q",
        "result": "The old symptom remains observable.",
        "outcome": "supports",
        "exit_code": 0,
        "addresses_atom_ids": ["atom:a"],
        "observable_assertion": {
            "source": "stdout",
            "operator": "contains",
            "expected": "old behavior retained",
        },
        "artifact_refs": ["artifact:mechanism"],
    }
    research["experiments"].append(old_behavior)
    verification = research["evidence_verification"]
    assert isinstance(verification, dict)
    verification["experiments"].append(
        {
            **old_behavior,
            "assertion_passed": True,
            "stdout_sha256": "5" * 64,
            "stderr_sha256": "6" * 64,
        }
    )
    strategy = option["causal_coverage"]["outcome_strategy"]
    strategy["original_scenario_experiment_ids"] = ["experiment:old-behavior"]
    strategy["post_change_replay_mode"] = "verified_fail_first"

    assert ticket_readiness._option_outcome_strategy(option, research=research) is None

    strategy["post_change_replay_mode"] = "stage6_planned_unverified"
    assert ticket_readiness._option_outcome_strategy(option, research=research) is None

    # Without a clean fail-first command for the same source atom, the honest future-proof
    # mode remains available instead of blocking option throughput.
    for receipt in verification["experiments"]:
        if isinstance(receipt, dict) and receipt.get("experiment_id") != (
            "experiment:old-behavior"
        ):
            receipt["exit_code"] = 0
    strategy["post_change_replay_mode"] = "stage6_planned_unverified"
    normalized = ticket_readiness._option_outcome_strategy(option, research=research)
    assert normalized is not None
    assert normalized["post_change_replay_mode"] == "stage6_planned_unverified"


def _stage5_contract_fixture() -> dict[str, object]:
    contract: dict[str, object] = {
        "schema_version": 1,
        "kind": "selected_option_outcome_strategy",
        "outcome_contract_status": "approved_for_planning",
        "post_change_evidence_status": "unverified",
        "strategy": {"intended_operation": "Return the useful value."},
        "review": {"verdict": "sufficient"},
    }
    contract["outcome_contract_id"] = _content_id(
        "stage5_outcome_contract",
        contract,
        "outcome_contract_id",
    )
    return contract


def _ordinary_fail_first_oracle_fixture() -> dict[str, object]:
    positive_contract = {
        "positive_outcome_contract_id": "positive_outcome_contract:baseline",
        "kind": "repository_test_assertion",
    }
    argv = ["python", "-m", "pytest", "tests/test_feature.py::test_original"]
    oracle: dict[str, object] = {
        "schema_version": 1,
        "case_id": "case:test",
        "repo_revision": "a" * 40,
        "research_experiment_id": "experiment:original",
        "scenario_kind": "original_replay",
        "origin_atom_ids": ["atom:original"],
        "mechanism_evidence_ids": ["mechanism_evidence:original"],
        "baseline": {"exit_code": 1},
        "kind": "staged_replay",
        "proof_scope": "behavioral",
        "execution": {
            "argv": argv,
            "command_authorization": {
                "authorization_kind": "immutable_source_command",
                "executed_argv_sha256": _canonical_hash(argv),
                "shell": False,
                "workspace_confined": True,
            },
            "platform_requirement": "any",
            "shell": False,
        },
        "asset": None,
        "positive_outcome_contracts": [positive_contract],
    }
    oracle["outcome_oracle_id"] = _content_id(
        "outcome_oracle", oracle, "outcome_oracle_id"
    )
    return oracle


def _direct_fail_first_research_fixture() -> dict[str, object]:
    oracle = _ordinary_fail_first_oracle_fixture()
    execution = oracle["execution"]
    assert isinstance(execution, dict)
    argv = execution["argv"]
    assert isinstance(argv, list)
    command = " ".join(str(value) for value in argv)
    experiment = {
        "experiment_id": "experiment:original",
        "scenario_kind": "original_replay",
        "command": command,
        "outcome": "supports",
        "addresses_atom_ids": ["atom:original"],
    }
    receipt = {
        **experiment,
        "exit_code": 1,
        "assertion_passed": True,
        "executed_argv": argv,
        "post_replay_mutations": False,
        "command_authorization": {
            **execution["command_authorization"],
            "runner_attested": True,
        },
    }
    return {
        "experiments": [experiment],
        "evidence_verification": {
            "status": "verified",
            "experiments": [receipt],
            "outcome_oracles": [oracle],
        },
    }


def test_stage5_fail_first_bridge_preserves_stage3_oracle_without_relabeling_it() -> None:
    source = _ordinary_fail_first_oracle_fixture()
    retained_source = json.loads(json.dumps(source))
    contract = _stage5_contract_fixture()

    derived = ticket_readiness._derive_stage5_fail_first_oracle(
        source,
        selected_outcome_contract=contract,
    )

    assert source == retained_source
    assert derived["scenario_kind"] == "fail_first_contract"
    assert "positive_outcome_contracts" not in derived
    assert derived["selected_outcome_contract"] == contract
    assert derived["retained_stage3_oracle"] == retained_source
    assert derived["retained_stage3_oracle"]["scenario_kind"] == "original_replay"
    assert derived["stage5_fail_first_source"] == {
        "schema_version": 1,
        "kind": "verified_stage3_fail_first_source",
        "source_outcome_oracle_id": retained_source["outcome_oracle_id"],
        "source_scenario_kind": "original_replay",
        "source_positive_outcome_contract_ids": [
            "positive_outcome_contract:baseline"
        ],
    }


def test_stage5_fail_first_bridge_accepts_direct_repository_replay_without_asset() -> None:
    research = _direct_fail_first_research_fixture()
    source = research["evidence_verification"]["outcome_oracles"][0]
    contract = _stage5_contract_fixture()
    derived = ticket_readiness._derive_stage5_fail_first_oracle(
        source,
        selected_outcome_contract=contract,
    )
    derived["outcome_oracle_id"] = _content_id(
        "outcome_oracle", derived, "outcome_oracle_id"
    )

    binding = ticket_readiness._validated_fail_first_staged_replay(
        derived,
        research=research,
    )

    assert binding is not None
    assert binding["asset_paths"] == set()
    assert binding["oracle"] == derived


@pytest.mark.parametrize("tamper", ["outer_positive_contract", "retained_source"])
def test_stage5_fail_first_bridge_rejects_post_derivation_tampering(tamper: str) -> None:
    research = _direct_fail_first_research_fixture()
    source = research["evidence_verification"]["outcome_oracles"][0]
    derived = ticket_readiness._derive_stage5_fail_first_oracle(
        source,
        selected_outcome_contract=_stage5_contract_fixture(),
    )
    if tamper == "outer_positive_contract":
        derived["positive_outcome_contracts"] = [
            {"positive_outcome_contract_id": "positive_outcome_contract:reintroduced"}
        ]
    else:
        retained = derived["retained_stage3_oracle"]
        assert isinstance(retained, dict)
        retained["scenario_kind"] = "faithful_replay"
        retained["outcome_oracle_id"] = _content_id(
            "outcome_oracle", retained, "outcome_oracle_id"
        )
    derived["outcome_oracle_id"] = _content_id(
        "outcome_oracle", derived, "outcome_oracle_id"
    )

    assert (
        ticket_readiness._validated_fail_first_staged_replay(
            derived,
            research=research,
        )
        is None
    )


@pytest.mark.parametrize(
    ("mutation", "error"),
    [
        (
            lambda oracle: oracle["baseline"].update({"exit_code": 0}),
            "stage5_outcome_fail_first_source_oracle_invalid",
        ),
        (
            lambda oracle: oracle.update(
                {"selected_outcome_contract": _stage5_contract_fixture()}
            ),
            "stage5_outcome_fail_first_source_already_planned",
        ),
        (
            lambda oracle: oracle.update({"scenario_kind": "fail_first_contract"}),
            "stage5_outcome_fail_first_source_contract_shape_invalid",
        ),
    ],
)
def test_stage5_fail_first_bridge_rejects_non_source_or_ambiguous_oracles(
    mutation: Callable[[dict[str, object]], None],
    error: str,
) -> None:
    source = _ordinary_fail_first_oracle_fixture()
    mutation(source)
    source["outcome_oracle_id"] = _content_id(
        "outcome_oracle", source, "outcome_oracle_id"
    )

    with pytest.raises(ValueError, match=error):
        ticket_readiness._derive_stage5_fail_first_oracle(
            source,
            selected_outcome_contract=_stage5_contract_fixture(),
        )


def test_stage5_fail_first_mode_reuses_retained_oracle_without_rewriting_asset() -> None:
    research, paths = _runner_research()
    research["case_id"] = "case:test"
    research["repo_revision"] = "a" * 40
    option = _broad_option(
        first_ref=str(paths[0]["failure_path_id"]),
        second_ref=str(paths[1]["failure_path_id"]),
    )
    strategy = option["causal_coverage"]["outcome_strategy"]
    strategy["post_change_replay_mode"] = "verified_fail_first"
    command = "python .usertest_research/fail_first.py"
    argv = command.split()
    source_experiment = next(
        item
        for item in research["experiments"]
        if item["experiment_id"] == "experiment:support"
    )
    source_experiment["command"] = command
    source_experiment["scenario_kind"] = "fail_first_contract"
    verification = research["evidence_verification"]
    assert isinstance(verification, dict)
    source_receipt = next(
        item
        for item in verification["experiments"]
        if item["experiment_id"] == "experiment:support"
    )
    source_receipt["command"] = command
    source_receipt["scenario_kind"] = "fail_first_contract"
    source_receipt["executed_argv"] = argv
    source_receipt["command_authorization"] = {
        "authorization_kind": "attested_research_harness",
        "executed_argv_sha256": _canonical_hash(argv),
        "shell": False,
        "workspace_confined": True,
        "runner_attested": True,
    }
    manifest = {
        ".usertest_research/fail_first.py": {
            "kind": "file",
            "mode": 0o644,
            "sha256": "7" * 64,
            "size_bytes": 12,
        }
    }
    asset = {
        "asset_id": "outcome_asset:"
        + _canonical_hash({"schema_version": 1, "manifest": manifest}),
        "runs_relative_path": "research/outcome_asset/bundle",
        "manifest": manifest,
        "manifest_sha256": _canonical_hash(manifest),
    }
    oracle = {
        "schema_version": 1,
        "case_id": "case:test",
        "repo_revision": research["repo_revision"],
        "research_experiment_id": "experiment:asset-source",
        "scenario_kind": "retained_overlay_source",
        "baseline": {"exit_code": 1},
        "kind": "staged_replay",
        "proof_scope": "behavioral",
        "execution": {
            "argv": argv,
            "command_authorization": {
                "authorization_kind": "attested_research_harness",
                "executed_argv_sha256": _canonical_hash(argv),
                "shell": False,
                "workspace_confined": True,
            },
            "shell": False,
        },
        "asset": asset,
    }
    oracle["outcome_oracle_id"] = _content_id(
        "outcome_oracle", oracle, "outcome_oracle_id"
    )
    verification["outcome_oracles"].append(oracle)
    contract = {
        "schema_version": 1,
        "kind": "selected_option_outcome_strategy",
        "outcome_contract_status": "approved_for_planning",
        "post_change_evidence_status": "unverified",
        "problem_id": "problem:test",
        "selected_option_id": option["option_id"],
        "strategy": strategy,
        "review": {"verdict": "sufficient"},
    }
    contract["outcome_contract_id"] = _content_id(
        "stage5_outcome_contract", contract, "outcome_contract_id"
    )
    selection = {
        "selected_option_id": option["option_id"],
        "selected_option": option,
        "falsification_review": {"selected_outcome_contract": contract},
    }
    bound = bind_plan_outcome_oracle(
        {
            "case_id": "case:test",
            "before_after_reproduction": {
                "research_experiment_id": "experiment:support",
                "after_change": {
                    "command": command,
                    "expected_exit_code": 0,
                    "observable_assertions": [
                        {
                            "source": "stdout",
                            "operator": "contains",
                            "expected": "provenance=preserved",
                        }
                    ],
                },
            },
            "outcome_verification_roles": {
                "original_scenario": {"description": "Run the fail-first replay."},
                "live": None,
                "mitigation_effect": None,
                "recurrence": None,
            },
        },
        research=research,
        selection=selection,
    )

    original = bound["outcome_verification_roles"]["original_scenario"]
    assert original["oracle"]["kind"] == "staged_replay"
    assert original["oracle"]["asset"] == asset
    assert original["oracle"]["selected_outcome_contract"] == contract
    assert original["oracle"]["derived_replay_source"] == {
        "kind": "verified_fail_first_from_retained_stage3_overlay",
        "experiment_receipt_sha256": _canonical_hash(
            next(
                item
                for item in verification["experiments"]
                if item["experiment_id"] == "experiment:support"
            )
        ),
        "retained_asset_source_outcome_oracle_id": oracle["outcome_oracle_id"],
    }
    rebound = bind_plan_outcome_oracle(
        bound,
        research=research,
        selection=selection,
    )
    assert rebound["outcome_verification_roles"]["original_scenario"]["oracle"] == (
        original["oracle"]
    )
    assert "retained_stage3_oracle" not in original["oracle"][
        "retained_stage3_oracle"
    ]
    assert original["commands"] == []
    assert ticket_readiness.verified_staged_replay_command_asset_paths(
        bound,
        research=research,
    ) == {command: {".usertest_research/fail_first.py"}}

    planned_binding = {
        "command_index": 0,
        "binding_kind": "stage6_planned_post_change",
        "selected_outcome_contract_id": contract["outcome_contract_id"],
        "repo_revision": research["repo_revision"],
    }
    roles = bound["outcome_verification_roles"]
    assert isinstance(roles, dict)
    roles["live"] = {
        "description": "Exercise the selected post-change live route.",
        "execution_status": "planned_unverified",
        "commands": ["python scripts/live_recovery.py --verify"],
        "command_bindings": [planned_binding],
        "predicates": [
            {"type": "command_exit_code", "command_index": 0, "equals": 0}
        ],
    }
    roles["mitigation_effect"] = {
        "description": "Measure the selected mitigation effect.",
        "execution_status": "planned_unverified",
        "commands": ["python scripts/measure_mitigation.py --verify"],
        "command_bindings": [planned_binding],
        "predicates": [
            {"type": "command_exit_code", "command_index": 0, "equals": 0}
        ],
    }
    roles["recurrence"] = {
        "description": "Use later canonical-case refresh evidence.",
        "verification_owner": "centralized_case_refresh",
        "commands": [],
        "predicates": [],
    }
    role_reasons = ticket_readiness._outcome_role_contract_errors(
        roles,
        verification_commands=[command],
        reproduction=bound["before_after_reproduction"],
        requires_live=True,
        research=research,
    )
    assert not any("planned_command_binding_invalid" in reason for reason in role_reasons)

    wrong_contract_roles = json.loads(json.dumps(roles))
    wrong_contract_roles["live"]["command_bindings"][0][
        "selected_outcome_contract_id"
    ] = "stage5_outcome_contract:wrong"
    wrong_contract_reasons = ticket_readiness._outcome_role_contract_errors(
        wrong_contract_roles,
        verification_commands=[command],
        reproduction=bound["before_after_reproduction"],
        requires_live=True,
        research=research,
    )
    assert (
        "change_plan_outcome_role_planned_command_binding_invalid:live:0"
        in wrong_contract_reasons
    )
    wrong_revision_roles = json.loads(json.dumps(roles))
    wrong_revision_roles["mitigation_effect"]["command_bindings"][0][
        "repo_revision"
    ] = "b" * 40
    wrong_revision_reasons = ticket_readiness._outcome_role_contract_errors(
        wrong_revision_roles,
        verification_commands=[command],
        reproduction=bound["before_after_reproduction"],
        requires_live=True,
        research=research,
    )
    assert (
        "change_plan_outcome_role_planned_command_binding_invalid:mitigation_effect:0"
        in wrong_revision_reasons
    )

    reproduction = bound["before_after_reproduction"]
    assert isinstance(reproduction, dict)
    reproduction.update(
        {
            "original_scenario": "Replay the retained fail-first contract.",
            "expected_outcome_state": "mitigated",
            "before_change": {
                "command": command,
                "expected_exit_code": 1,
                "expected_result": "The retained fail-first assertion fails.",
                "observable_assertion": source_experiment["observable_assertion"],
            },
        }
    )
    after_change = reproduction["after_change"]
    assert isinstance(after_change, dict)
    after_change["expected_result"] = "The unchanged fail-first replay passes."
    after_change["observable_assertions"] = [
        source_experiment["observable_assertion"],
        {
            "source": "stdout",
            "operator": "contains",
            "expected": '"provenance_record_count": 5',
        },
    ]
    bound = bind_plan_outcome_oracle(
        bound,
        research=research,
        selection=selection,
    )
    bound = assign_plan_revision_id(bound)
    _ready, reasons = assess_change_plan_readiness(
        bound,
        problem={"case_id": "case:test", "problem_id": "problem:test"},
        research=research,
        selection=selection,
    )
    assert "change_plan_research_experiment_not_original_support" not in reasons
    assert "change_plan_after_oracle_does_not_reverse_original_symptom" not in reasons

    generic_positive = json.loads(json.dumps(bound))
    generic_after = generic_positive["before_after_reproduction"]["after_change"]
    generic_after["observable_assertions"] = [
        source_experiment["observable_assertion"],
        {
            "source": "stdout",
            "operator": "contains",
            "expected": "provenance=preserved",
        },
    ]
    generic_positive = bind_plan_outcome_oracle(
        generic_positive,
        research=research,
        selection=selection,
    )
    generic_positive = assign_plan_revision_id(generic_positive)
    _ready, generic_reasons = assess_change_plan_readiness(
        generic_positive,
        problem={"case_id": "case:test", "problem_id": "problem:test"},
        research=research,
        selection=selection,
    )
    assert "change_plan_after_oracle_does_not_reverse_original_symptom" in generic_reasons

    exit_only = json.loads(json.dumps(bound))
    exit_only_after = exit_only["before_after_reproduction"]["after_change"]
    exit_only_after["observable_assertions"] = [
        {"source": "exit_code", "operator": "equals", "expected": 0}
    ]
    with pytest.raises(ValueError, match="stage5_outcome_positive_predicate_missing"):
        bind_plan_outcome_oracle(
            exit_only,
            research=research,
            selection=selection,
        )

    tampered = json.loads(json.dumps(bound))
    tampered_oracle = tampered["outcome_verification_roles"]["original_scenario"]["oracle"]
    tampered_manifest = tampered_oracle["asset"]["manifest"]
    tampered_manifest[".usertest_research/forged.py"] = {
        "kind": "file",
        "mode": 0o644,
        "sha256": "9" * 64,
        "size_bytes": 10,
    }
    tampered_oracle["asset"]["manifest_sha256"] = _canonical_hash(tampered_manifest)
    tampered_oracle["asset"]["asset_id"] = "outcome_asset:" + _canonical_hash(
        {"schema_version": 1, "manifest": tampered_manifest}
    )
    tampered_oracle["outcome_oracle_id"] = _content_id(
        "outcome_oracle",
        tampered_oracle,
        "outcome_oracle_id",
    )
    tampered = assign_plan_revision_id(tampered)
    assert (
        ticket_readiness.verified_staged_replay_command_asset_paths(
            tampered,
            research=research,
        )
        == {}
    )
    _ready, tampered_reasons = assess_change_plan_readiness(
        tampered,
        problem={"case_id": "case:test", "problem_id": "problem:test"},
        research=research,
        selection=selection,
    )
    assert "change_plan_outcome_oracle_binding_changed" in tampered_reasons
    assert "change_plan_research_experiment_not_original_support" in tampered_reasons


def test_change_plan_cannot_create_or_move_over_retained_oracle_asset() -> None:
    plan, problem, research, selection = _observable_change_plan_fixture()
    manifest = {
        ".usertest_research/replay.py": {
            "kind": "file",
            "mode": 0o644,
            "sha256": "8" * 64,
            "size_bytes": 20,
        }
    }
    asset = {
        "asset_id": "outcome_asset:"
        + _canonical_hash({"schema_version": 1, "manifest": manifest}),
        "runs_relative_path": "research/outcome_asset/bundle",
        "manifest": manifest,
        "manifest_sha256": _canonical_hash(manifest),
    }
    oracle = {"schema_version": 1, "kind": "staged_replay", "asset": asset}
    oracle["outcome_oracle_id"] = _content_id(
        "outcome_oracle", oracle, "outcome_oracle_id"
    )
    verification = research["evidence_verification"]
    assert isinstance(verification, dict)
    verification["outcome_oracles"] = [oracle]
    targets = plan["change_targets"]
    assert isinstance(targets, list)
    targets.extend(
        [
            {
                "action": "create",
                "path": ".usertest_research/replay.py",
                "symbols": [],
                "change": "Rewrite the retained replay.",
            },
            {
                "action": "move",
                "path": "tests/new_replay.py",
                "destination_path": ".usertest_research/replay.py",
                "symbols": [],
                "change": "Move a replacement over the retained replay.",
            },
        ]
    )
    target_contract = plan["target_contract"]
    assert isinstance(target_contract, dict)
    target_contract["targets"] = targets
    plan = assign_plan_revision_id(plan)

    _ready, reasons = assess_change_plan_readiness(
        plan,
        problem=problem,
        research=research,
        selection=selection,
    )

    assert "change_plan_target_rewrites_retained_outcome_asset:1" in reasons
    assert "change_plan_target_rewrites_retained_outcome_asset:2" in reasons


def test_mitigated_falsification_bound_cannot_be_planned_as_resolved() -> None:
    resolved_plan, problem, research, selection = _observable_change_plan_fixture(
        expected_outcome_state="resolved"
    )
    selection["falsification_review"] = {
        "outcome_claim_status": "mitigated",
        "outcome_confidence": "bounded",
    }

    ready, reasons = assess_change_plan_readiness(
        resolved_plan,
        problem=problem,
        research=research,
        selection=selection,
    )

    assert ready is False
    assert "change_plan_outcome_overclaims_falsification_bound" in reasons

    mitigated_plan, problem, research, selection = _observable_change_plan_fixture(
        expected_outcome_state="mitigated"
    )
    selection["falsification_review"] = {
        "outcome_claim_status": "mitigated",
        "outcome_confidence": "bounded",
    }
    _ready, mitigated_reasons = assess_change_plan_readiness(
        mitigated_plan,
        problem=problem,
        research=research,
        selection=selection,
    )
    assert "change_plan_outcome_overclaims_falsification_bound" not in (
        mitigated_reasons
    )


def test_proof_limitation_cannot_upgrade_mitigated_falsification_to_resolved() -> None:
    plan, problem, research, selection = _observable_change_plan_fixture(
        expected_outcome_state="resolved"
    )
    selection["falsification_review"] = {
        "outcome_claim_status": "mitigated",
        "outcome_confidence": "bounded",
    }
    reproduction = plan["before_after_reproduction"]
    assert isinstance(reproduction, dict)
    reproduction["proof_limitation"] = (
        "The live provider cannot be reached from isolation."
    )

    ready, reasons = assess_change_plan_readiness(
        plan,
        problem=problem,
        research=research,
        selection=selection,
    )

    assert ready is False
    assert "change_plan_outcome_overclaims_falsification_bound" in reasons


def _attach_exact_origin_boundary_fixture(
    *,
    research: dict[str, object],
    verification: dict[str, object],
    oracle: dict[str, object],
    experiment: dict[str, object],
    mechanism_evidence_ids: list[str],
    positive_contract: dict[str, object],
) -> dict[str, object]:
    """Attach the same authenticated exact-origin receipt Stage 3 now mints."""

    experiment_id = str(experiment["experiment_id"])
    atom_id = "atom:oracle"
    command = str(experiment["command"])
    argv = command.split()
    atom_snapshot = {
        "atom_id": atom_id,
        "command": command,
        "exit_code": experiment["exit_code"],
        "evidence_role": "observation",
        "origin_stage": "runtime",
    }
    atom_sha256 = _canonical_hash(atom_snapshot)
    research["evidence_assignment"] = {
        "atom_receipts": [
            {
                "atom_id": atom_id,
                "atom_sha256": atom_sha256,
                "atom_snapshot": atom_snapshot,
            }
        ]
    }
    authorization = {
        "authorization_kind": "fixture_exact_origin_command",
        "executed_argv_sha256": _canonical_hash(argv),
        "shell": False,
        "workspace_confined": True,
        "origin_atom_id": atom_id,
        "origin_atom_sha256": atom_sha256,
        "origin_atom_field_path": "$.command",
        "origin_command_value_sha256": _canonical_hash(command),
        "runner_attested": True,
    }
    authorization["authorization_sha256"] = _canonical_hash(authorization)
    replay_inputs = {
        "schema_version": 1,
        "source_experiment_id": experiment_id,
        "environment": {},
        "disposable_state_paths": [],
        "runner_approved": True,
    }
    replay_inputs["replay_inputs_sha256"] = _canonical_hash(replay_inputs)
    contract_id = str(positive_contract["positive_outcome_contract_id"])
    replay_observation = {
        "schema_version": 1,
        "source_experiment_id": experiment_id,
        "selector": {"source": "exit_code"},
        "source_observation_sha256": _canonical_hash(
            {
                "exit_code": experiment["exit_code"],
                "stdout_sha256": "a" * 64,
                "stderr_sha256": "b" * 64,
            }
        ),
        "predicate_input_mode": "post_change_observation",
        "positive_outcome_contract_ids": [contract_id],
        "runner_attested": True,
    }
    replay_observation["replay_observation_sha256"] = _canonical_hash(
        replay_observation
    )
    oracle["execution"] = {
        "argv": argv,
        "command_authorization": authorization,
        "platform_requirement": "any",
        "shell": False,
        "replay_inputs": replay_inputs,
        "replay_observation": replay_observation,
    }
    oracle["origin_atom_ids"] = [atom_id]
    oracle["outcome_oracle_id"] = _content_id(
        "outcome_oracle", oracle, "outcome_oracle_id"
    )
    clean_replay = {
        "experiment_id": experiment_id,
        "command": command,
        "executed_argv": argv,
        "command_authorization": authorization,
        "exit_code": experiment["exit_code"],
        "scenario_kind": experiment["scenario_kind"],
        "assertion_passed": True,
        "stdout_sha256": "a" * 64,
        "stderr_sha256": "b" * 64,
        "replay_inputs": replay_inputs,
        "execution_isolation": {"executor": "fixture_isolated_replay"},
    }
    verification_experiments = verification.setdefault("experiments", [])
    assert isinstance(verification_experiments, list)
    verification["experiments"] = [
        value
        for value in verification_experiments
        if not isinstance(value, dict) or value.get("experiment_id") != experiment_id
    ] + [clean_replay]
    source_identity = {
        "schema_version": 1,
        "origin_atom_id": atom_id,
        "origin_atom_sha256": atom_sha256,
        "origin_atom_field_path": "$.command",
        "origin_command_value_sha256": _canonical_hash(command),
        "executed_argv_sha256": authorization["executed_argv_sha256"],
        "command_authorization_sha256": authorization["authorization_sha256"],
        "runner_attested": True,
    }
    source_identity["source_identity_sha256"] = _canonical_hash(source_identity)
    equivalence = {
        "schema_version": 1,
        "equivalence_mode": "exact_origin_scenario_identity",
        "source_experiment_id": experiment_id,
        "origin_atom_ids": [atom_id],
        "source_identity": source_identity,
        "source_identity_refs": [
            f"origin_command_identity:{source_identity['source_identity_sha256']}"
        ],
        "replay_inputs_sha256": replay_inputs["replay_inputs_sha256"],
        "replay_observation_sha256": replay_observation[
            "replay_observation_sha256"
        ],
        "positive_outcome_contract_ids": [contract_id],
        "selected_mechanism_evidence_ids": sorted(mechanism_evidence_ids),
        "outcome_oracle_id": oracle["outcome_oracle_id"],
        "runner_attested": True,
    }
    equivalence["equivalence_sha256"] = _canonical_hash(equivalence)
    replay_projection = {
        "experiment_id": experiment_id,
        "executed_argv_sha256": authorization["executed_argv_sha256"],
        "command_authorization_sha256": authorization["authorization_sha256"],
        "stdout_sha256": clean_replay["stdout_sha256"],
        "stderr_sha256": clean_replay["stderr_sha256"],
        "replay_inputs_sha256": replay_inputs["replay_inputs_sha256"],
        "execution_isolation_sha256": _canonical_hash(
            clean_replay["execution_isolation"]
        ),
    }
    boundary = {
        "schema_version": 1,
        "experiment_id": experiment_id,
        "boundary_kind": "fixture/repository-original-scenario",
        "requires_live_verification": False,
        "faithful_equivalence": True,
        "provenance_refs": sorted(
            {
                f"research_experiment:{experiment_id}",
                f"clean_replay:{_canonical_hash(replay_projection)}",
                *mechanism_evidence_ids,
                str(oracle["outcome_oracle_id"]),
                f"equivalence_proof:{equivalence['equivalence_sha256']}",
            }
        ),
        "rationale_sha256": _canonical_hash("exact original fixture identity"),
        "runner_attested": True,
        "equivalence_proof": equivalence,
    }
    boundary["boundary_sha256"] = _canonical_hash(boundary)
    verification["verification_boundaries"] = [boundary]
    return oracle


def _bind_staged_outcome_oracle(
    plan: dict[str, object],
    research: dict[str, object],
    *,
    include_positive_contract: bool = True,
) -> dict[str, object]:
    experiment = research["experiments"][0]
    assert isinstance(experiment, dict)
    command = str(experiment["command"])
    argv = command.split()
    verification = research.setdefault("evidence_verification", {})
    assert isinstance(verification, dict)
    mechanism_evidence = verification.get("mechanism_evidence")
    if not isinstance(mechanism_evidence, list) or not mechanism_evidence:
        mechanism_evidence = [
            {
                "evidence_type": "observed_output",
                "hypothesis_id": "h-classifier",
                "mechanism_symbols": ["classifier.classify"],
                "experiment_ids": [str(experiment["experiment_id"])],
                "origin_atom_ids": ["atom:oracle"],
                "code_paths": [
                    {
                        "path": "src/classifier.py",
                        "symbol": "classifier.classify",
                    }
                ],
                "adversarial_effect": "supports_selection",
            }
        ]
        mechanism_evidence[0]["mechanism_evidence_id"] = _content_id(
            "mechanism_evidence",
            mechanism_evidence[0],
            "mechanism_evidence_id",
        )
        verification["mechanism_evidence"] = mechanism_evidence
    primary_hypothesis_id = next(
        (
            str(item["hypothesis_id"])
            for item in mechanism_evidence
            if isinstance(item, dict) and isinstance(item.get("hypothesis_id"), str)
        ),
        "h-classifier",
    )
    for item in mechanism_evidence:
        if not isinstance(item, dict):
            continue
        if not isinstance(item.get("hypothesis_id"), str):
            item["hypothesis_id"] = primary_hypothesis_id
        item["experiment_ids"] = [str(experiment["experiment_id"])]
        item["origin_atom_ids"] = ["atom:oracle"]
        item["mechanism_evidence_id"] = _content_id(
            "mechanism_evidence",
            item,
            "mechanism_evidence_id",
        )
    mechanism_evidence_ids = [
        str(item["mechanism_evidence_id"])
        for item in (mechanism_evidence if isinstance(mechanism_evidence, list) else [])
        if isinstance(item, dict) and "mechanism_evidence_id" in item
    ]
    assert mechanism_evidence_ids
    _set_verified_primary_context(
        verification,
        primary_hypothesis_id=primary_hypothesis_id,
        mechanism_evidence_ids=mechanism_evidence_ids,
        causal_root_evidence_ids=mechanism_evidence_ids[:1],
    )
    primary_binding = _primary_binding_fields(verification)
    oracle: dict[str, object] = {
        "schema_version": 1,
        "case_id": plan["case_id"],
        "repo_revision": research["repo_revision"],
        "research_experiment_id": experiment["experiment_id"],
        "scenario_kind": experiment["scenario_kind"],
        "origin_atom_ids": ["atom:oracle"],
        "mechanism_evidence_ids": mechanism_evidence_ids,
        **primary_binding,
        "baseline": {
            "exit_code": experiment["exit_code"],
            "observable_assertion": experiment["observable_assertion"],
            "stdout_sha256": "a" * 64,
            "stderr_sha256": "b" * 64,
        },
        "kind": "staged_replay",
        "proof_scope": "behavioral",
        "execution": {
            "argv": argv,
            "command_authorization": {
                "authorization_kind": "declared_inspected_repository_entrypoint",
                "executed_argv_sha256": _canonical_hash(argv),
                "shell": False,
                "workspace_confined": True,
            },
            "platform_requirement": "any",
            "shell": False,
        },
        "asset": None,
    }
    postconditions: list[dict[str, object]] = [
        {"type": "command_exit_code", "command_index": 0, "equals": 0}
    ]
    positive_contract: dict[str, object] = {
        "schema_version": 1,
        "kind": "repository_test_assertion",
        "research_experiment_id": experiment["experiment_id"],
        "mechanism_evidence_ids": mechanism_evidence_ids,
        "postconditions": postconditions,
        **primary_binding,
    }
    positive_contract["positive_outcome_contract_id"] = _content_id(
        "positive_outcome_contract",
        positive_contract,
        "positive_outcome_contract_id",
    )
    if include_positive_contract:
        oracle["positive_outcome_contracts"] = [positive_contract]
        oracle = _attach_exact_origin_boundary_fixture(
            research=research,
            verification=verification,
            oracle=oracle,
            experiment=experiment,
            mechanism_evidence_ids=mechanism_evidence_ids,
            positive_contract=positive_contract,
        )
    else:
        oracle["outcome_oracle_id"] = _content_id(
            "outcome_oracle", oracle, "outcome_oracle_id"
        )
    verification["status"] = "verified"
    verification["outcome_oracles"] = [oracle]
    return bind_plan_outcome_oracle(plan, research=research)


def test_bound_outcome_does_not_project_different_control_value_onto_source() -> None:
    plan, _, research, _ = _observable_change_plan_fixture()
    reproduction = plan["before_after_reproduction"]
    assert isinstance(reproduction, dict)
    after = reproduction["after_change"]
    assert isinstance(after, dict)
    after["observable_assertions"] = [
        {
            "source": "stdout",
            "operator": "contains",
            "expected": "planner-invented-success",
        }
    ]
    research["experiments"].append(
        {
            "experiment_id": "exp-correct-control",
            "scenario_kind": "control",
            "observable_assertion": {
                "source": "stdout",
                "operator": "equals",
                "expected": "classification=incomplete",
            },
        }
    )
    control: dict[str, object] = {
        "verification_method": "pytest_ast_controlled_difference_v2",
        "control_experiment_id": "exp-correct-control",
        "controlled_input_difference": {"difference_count": 1},
        "observable_difference": {
            "source": "stdout",
            "difference_kind": "wrong_value_corrected",
            "control_expected_sha256": _canonical_hash("classification=incomplete"),
        },
        "adversarial_effect": "limits_scope",
    }
    control["control_verification_id"] = _content_id(
        "control_verification",
        control,
        "control_verification_id",
    )
    evidence: dict[str, object] = {
        "evidence_type": "controlled_scenario",
        "strong_pytest_control_id": control["control_verification_id"],
    }
    evidence["mechanism_evidence_id"] = _content_id(
        "mechanism_evidence",
        evidence,
        "mechanism_evidence_id",
    )
    research["evidence_verification"] = {
        "status": "verified",
        "control_verifications": [control],
        "mechanism_evidence": [evidence],
    }

    bound = _bind_staged_outcome_oracle(plan, research)

    bound_reproduction = bound["before_after_reproduction"]
    assert isinstance(bound_reproduction, dict)
    bound_after = bound_reproduction["after_change"]
    assert isinstance(bound_after, dict)
    assert all(
        assertion.get("expected") not in {"planner-invented-success", "classification=incomplete"}
        for assertion in bound_after["observable_assertions"]
    )
    roles = bound["outcome_verification_roles"]
    assert isinstance(roles, dict)
    original = roles["original_scenario"]
    assert isinstance(original, dict)
    assert all(
        predicate.get("type") != "command_stdout_equals" for predicate in original["predicates"]
    )


def test_plan_binds_only_falsifier_selected_positive_contract() -> None:
    plan, _, research, _ = _observable_change_plan_fixture()
    _bind_staged_outcome_oracle(plan, research)
    verification = research["evidence_verification"]
    assert isinstance(verification, dict)
    oracle = verification["outcome_oracles"][0]
    assert isinstance(oracle, dict)
    selected_contract = oracle["positive_outcome_contracts"][0]
    assert isinstance(selected_contract, dict)
    selected_id = str(selected_contract["positive_outcome_contract_id"])
    rejected_contract: dict[str, object] = {
        "schema_version": 1,
        "kind": "origin_evidence_semantic_contract",
        "research_experiment_id": "exp-original",
        "mechanism_evidence_ids": ["mechanism_evidence:oracle"],
        "origin_evidence": {
            "atom_id": "atom:oracle",
            "atom_sha256": "a" * 64,
            "field_path": "$.proposal_only_marker",
            "value_sha256": _canonical_hash("planner-marker"),
        },
        "postconditions": [
            {"type": "command_exit_code", "command_index": 0, "equals": 0},
            {
                "type": "command_stdout_contains",
                "command_index": 0,
                "value": "planner-marker",
            },
        ],
    }
    rejected_contract["positive_outcome_contract_id"] = _content_id(
        "positive_outcome_contract",
        rejected_contract,
        "positive_outcome_contract_id",
    )
    oracle["positive_outcome_contracts"].append(rejected_contract)
    oracle["outcome_oracle_id"] = _content_id(
        "outcome_oracle",
        oracle,
        "outcome_oracle_id",
    )
    selection = {
        "falsification_review": {
            "selected_positive_outcome_contract_id": selected_id,
            "selected_positive_outcome_contract_ids": [selected_id],
        }
    }

    bound = bind_plan_outcome_oracle(
        plan,
        research=research,
        selection=selection,
    )
    original = bound["outcome_verification_roles"]["original_scenario"]

    assert original["selected_positive_outcome_contract_ids"] == [selected_id]
    assert all(
        predicate.get("value") != "planner-marker"
        for predicate in original["predicates"]
        if isinstance(predicate, dict)
    )


def test_plan_binds_every_consolidated_original_scenario() -> None:
    plan, _, research, _ = _observable_change_plan_fixture()
    _bind_staged_outcome_oracle(plan, research)
    verification = research["evidence_verification"]
    assert isinstance(verification, dict)
    oracle_one = verification["outcome_oracles"][0]
    assert isinstance(oracle_one, dict)
    contract_one = oracle_one["positive_outcome_contracts"][0]
    assert isinstance(contract_one, dict)
    oracle_two = json.loads(json.dumps(oracle_one))
    contract_two = oracle_two["positive_outcome_contracts"][0]
    contract_two["research_experiment_id"] = "exp-original-two"
    contract_two["positive_outcome_contract_id"] = _content_id(
        "positive_outcome_contract",
        contract_two,
        "positive_outcome_contract_id",
    )
    oracle_two["case_id"] = "case:oracle-two"
    oracle_two["research_experiment_id"] = "exp-original-two"
    oracle_two["outcome_oracle_id"] = _content_id(
        "outcome_oracle",
        oracle_two,
        "outcome_oracle_id",
    )
    member_one_verification = json.loads(json.dumps(verification))
    member_one_verification["outcome_oracles"] = [oracle_one]
    member_two_verification = json.loads(json.dumps(verification))
    member_two_verification["outcome_oracles"] = [oracle_two]
    members = [
        {
            "case_id": "case:oracle",
            "problem_id": "problem:oracle",
            "repo_revision": research["repo_revision"],
            "evidence_verification": member_one_verification,
        },
        {
            "case_id": "case:oracle-two",
            "problem_id": "problem:oracle-two",
            "repo_revision": research["repo_revision"],
            "evidence_verification": member_two_verification,
        },
    ]
    bundle: dict[str, object] = {
        "member_research_dossiers": members,
    }
    bundle["bundle_sha256"] = _canonical_hash(bundle)
    research["post_research_same_mechanism_bundle"] = bundle
    selected_ids = [
        str(contract_one["positive_outcome_contract_id"]),
        str(contract_two["positive_outcome_contract_id"]),
    ]
    selection = {
        "falsification_review": {
            "selected_positive_outcome_contract_id": None,
            "selected_positive_outcome_contract_ids": selected_ids,
        }
    }

    bound = bind_plan_outcome_oracle(
        plan,
        research=research,
        selection=selection,
    )
    original = bound["outcome_verification_roles"]["original_scenario"]

    assert original["selected_positive_outcome_contract_ids"] == selected_ids
    assert original["oracle"]["kind"] == "multi_scenario"
    assert len(original["oracle"]["scenarios"]) == 2
    assert len(original["predicates"]) == 2


def test_change_plan_requires_problem_specific_original_scenario_oracle() -> None:
    plan, problem, research, selection = _observable_change_plan_fixture()
    reproduction = plan["before_after_reproduction"]
    assert isinstance(reproduction, dict)
    after = reproduction["after_change"]
    assert isinstance(after, dict)
    after["observable_assertions"] = [{"source": "exit_code", "operator": "equals", "expected": 0}]
    roles = plan["outcome_verification_roles"]
    assert isinstance(roles, dict)
    original = roles["original_scenario"]
    assert isinstance(original, dict)
    original["predicates"] = [{"type": "command_exit_code", "command_index": 0, "equals": 0}]
    plan = assign_plan_revision_id(plan)

    ready, reasons = assess_change_plan_readiness(
        plan,
        problem=problem,
        research=research,
        selection=selection,
    )

    assert ready is False
    assert "change_plan_after_oracle_does_not_reverse_original_symptom" in reasons


def test_zero_exit_wrong_output_requires_correct_behavior_not_only_suppression() -> None:
    plan, problem, research, selection = _observable_change_plan_fixture()
    reproduction = plan["before_after_reproduction"]
    assert isinstance(reproduction, dict)
    after = reproduction["after_change"]
    assert isinstance(after, dict)
    after["observable_assertions"] = [
        {
            "source": "stderr",
            "operator": "not_contains",
            "expected": "incorrect policy classification",
        }
    ]
    roles = plan["outcome_verification_roles"]
    assert isinstance(roles, dict)
    original = roles["original_scenario"]
    assert isinstance(original, dict)
    original["predicates"] = [
        {"type": "command_exit_code", "command_index": 0, "equals": 0},
        {
            "type": "command_stderr_not_contains",
            "command_index": 0,
            "value": "incorrect policy classification",
        },
    ]
    plan = assign_plan_revision_id(plan)

    ready, reasons = assess_change_plan_readiness(
        plan,
        problem=problem,
        research=research,
        selection=selection,
    )

    assert ready is False
    assert "change_plan_positive_outcome_contract_missing_research_required" in reasons


def test_nonzero_to_zero_requires_positive_behavior_not_only_swallowed_failure() -> None:
    plan, problem, research, selection = _observable_change_plan_fixture(
        baseline_exit=1,
        after_exit=0,
    )
    reproduction = plan["before_after_reproduction"]
    assert isinstance(reproduction, dict)
    after = reproduction["after_change"]
    assert isinstance(after, dict)
    after["observable_assertions"] = [
        {
            "source": "stderr",
            "operator": "not_contains",
            "expected": "incorrect policy classification",
        }
    ]
    with pytest.raises(
        ValueError,
        match="stage5_selection_missing",
    ):
        _bind_staged_outcome_oracle(
            plan,
            research,
            include_positive_contract=False,
        )


def test_bound_resolved_outcome_accepts_positive_stream_postcondition() -> None:
    plan, problem, research, selection = _observable_change_plan_fixture(
        baseline_exit=1,
        after_exit=0,
    )
    plan = assign_plan_revision_id(_bind_staged_outcome_oracle(plan, research))

    ready, reasons = assess_change_plan_readiness(
        plan,
        problem=problem,
        research=research,
        selection=selection,
    )

    assert ready is True
    assert reasons == []


def test_planner_artifact_postcondition_is_removed_without_research_contract() -> None:
    plan, problem, research, selection = _observable_change_plan_fixture(
        baseline_exit=1,
        after_exit=0,
    )
    reproduction = plan["before_after_reproduction"]
    assert isinstance(reproduction, dict)
    after = reproduction["after_change"]
    assert isinstance(after, dict)
    after["observable_assertions"] = [
        {
            "source": "stderr",
            "operator": "not_contains",
            "expected": "incorrect policy classification",
        }
    ]
    after["artifact_expectations"] = [
        {
            "path": "result.json",
            "json_pointer": "/status",
            "equals": "complete",
        }
    ]
    plan = assign_plan_revision_id(_bind_staged_outcome_oracle(plan, research))

    ready, reasons = assess_change_plan_readiness(
        plan,
        problem=problem,
        research=research,
        selection=selection,
    )

    assert ready is True
    assert reasons == []
    original = plan["outcome_verification_roles"]["original_scenario"]
    assert isinstance(original, dict)
    assert not any(
        predicate.get("type") == "artifact_json_value" for predicate in original["predicates"]
    )
    assert "artifact_expectations" not in plan["before_after_reproduction"]["after_change"]


def test_runner_addressed_config_state_is_a_positive_postcondition() -> None:
    assert (
        ticket_readiness._positive_outcome_predicate(
            {
                "type": "oracle_state_equals",
                "target_id": "config_state:verified",
                "exists": True,
                "equals": "safe",
            }
        )
        is True
    )
    assert (
        ticket_readiness._positive_outcome_predicate(
            {"type": "command_exit_code", "command_index": 0, "equals": 0}
        )
        is False
    )
    assert (
        ticket_readiness._positive_outcome_predicate(
            {
                "type": "command_stderr_not_contains",
                "command_index": 0,
                "value": "failure",
            }
        )
        is False
    )


def test_static_trace_can_ground_research_but_not_post_change_outcome_proof() -> None:
    plan, problem, research, selection = _observable_change_plan_fixture()
    experiment = research["experiments"][0]
    assert isinstance(experiment, dict)
    experiment["scenario_kind"] = "static_trace"

    ready, reasons = assess_change_plan_readiness(
        plan,
        problem=problem,
        research=research,
        selection=selection,
    )

    assert ready is False
    assert "change_plan_static_trace_cannot_prove_behavioral_outcome" in reasons


def test_change_plan_accepts_expected_nonzero_mitigation_with_observable_proof() -> None:
    plan, problem, research, selection = _observable_change_plan_fixture(
        baseline_exit=7,
        after_exit=7,
        expected_outcome_state="mitigated",
    )

    ready, reasons = assess_change_plan_readiness(
        plan,
        problem=problem,
        research=research,
        selection=selection,
    )

    assert ready is True
    assert reasons == []


def test_bound_runtime_limitation_allows_planning_but_keeps_outcome_unverified() -> None:
    plan, problem, research, selection = _observable_change_plan_fixture()
    command = plan["verification_commands"][0]
    reproduction = plan["before_after_reproduction"]
    assert isinstance(reproduction, dict)
    reproduction.update(
        {
            "before_change": None,
            "after_change": None,
            "proof_limitation": "The live provider cannot be reached from isolation.",
            "proof_limitation_refs": ["boundary:live-provider-unreachable"],
            "alternate_verification": command,
            "expected_outcome_state": "unverified",
        }
    )
    research["evidence_boundaries"] = ["boundary:live-provider-unreachable"]
    plan = assign_plan_revision_id(plan)

    ready, reasons = assess_change_plan_readiness(
        plan,
        problem=problem,
        research=research,
        selection=selection,
    )

    assert ready is True
    assert reasons == []


def test_material_plan_limitation_remains_research_required() -> None:
    plan, problem, research, selection = _observable_change_plan_fixture()
    command = plan["verification_commands"][0]
    reproduction = plan["before_after_reproduction"]
    assert isinstance(reproduction, dict)
    reproduction.update(
        {
            "before_change": None,
            "after_change": None,
            "proof_limitation": "The actual control boundary is unknown.",
            "proof_limitation_refs": ["unknown:control-boundary"],
            "alternate_verification": command,
            "expected_outcome_state": "unverified",
        }
    )
    research["material_unknowns"] = [
        {
            "unknown_id": "unknown:control-boundary",
            "unknown": "The actual control boundary is unknown.",
            "affects": ["change_surface"],
        }
    ]
    plan = assign_plan_revision_id(plan)

    ready, reasons = assess_change_plan_readiness(
        plan,
        problem=problem,
        research=research,
        selection=selection,
    )

    assert ready is False
    assert "change_plan_material_limitation_requires_research" in reasons


def test_readiness_accepts_file_level_move_without_fictional_symbol() -> None:
    plan, problem, research, selection = _observable_change_plan_fixture(
        baseline_exit=7,
        after_exit=7,
        expected_outcome_state="mitigated",
    )
    target = {
        "action": "move",
        "path": "assets/provider-schema.json",
        "destination_path": "schemas/provider-schema.json",
        "change": "Move the provider schema to the runtime-consumed location.",
    }
    plan["change_targets"] = [target]
    target_contract = plan["target_contract"]
    assert isinstance(target_contract, dict)
    target_contract["targets"] = [{**target, "symbols": []}]
    plan = assign_plan_revision_id(plan)

    ready, reasons = assess_change_plan_readiness(
        plan,
        problem=problem,
        research=research,
        selection=selection,
    )

    assert ready is True
    assert reasons == []


def test_change_plan_allows_new_production_target_via_verified_integration_boundary() -> None:
    plan, problem, research, selection = _observable_change_plan_fixture()
    plan = _bind_staged_outcome_oracle(plan, research)
    existing_verification = research["evidence_verification"]
    assert isinstance(existing_verification, dict)
    evidence: dict[str, object] = {
        "evidence_type": "observed_output",
        "hypothesis_id": "h-classifier",
        "mechanism_symbols": ["classifier.classify"],
        "code_paths": [{"path": "src/classifier.py", "symbol": "classifier.classify"}],
    }
    evidence["mechanism_evidence_id"] = _content_id(
        "mechanism_evidence", evidence, "mechanism_evidence_id"
    )
    evidence_id = str(evidence["mechanism_evidence_id"])
    existing_verification["inspected_symbols"] = [
        {"path": "src/classifier.py", "symbol": "classifier.classify"}
    ]
    existing_evidence = existing_verification["mechanism_evidence"]
    assert isinstance(existing_evidence, list)
    existing_evidence.append(evidence)
    causal_coverage = {
        "mechanism_addressed": "False classification",
        "research_binding": {
            "intervention_points": [
                {
                    "target_path": "src/classifier.py",
                    "target_symbol": "classifier.classify",
                    "intervention": ("Classify the retained condition from its actual cause."),
                }
            ]
        },
    }
    plan["causal_coverage"] = causal_coverage
    selection["selected_option"] = {
        "causal_coverage": causal_coverage,
        "scope_evidence": plan["scope_evidence"],
    }
    create_target = {
        "action": "create",
        "path": "src/diagnostics/cause_formatter.py",
        "symbols": ["format_verified_cause"],
        "change": "Render the already-classified cause without reclassifying it.",
        "rationale_kind": "causal_propagation",
        "rationale": "The verified classifier boundary delegates cause rendering here.",
        "evidence_refs": [evidence_id],
        "integration_binding": {
            "path": "src/classifier.py",
            "symbol": "classifier.classify",
            "relationship": "classifier.classify calls the new formatter after classification",
            "evidence_refs": [evidence_id],
        },
    }
    targets = plan["change_targets"]
    assert isinstance(targets, list)
    targets.append(create_target)
    plan = assign_plan_revision_id(plan)

    ready, reasons = assess_change_plan_readiness(
        plan,
        problem=problem,
        research=research,
        selection=selection,
    )

    assert ready is True
    assert reasons == []


def test_change_plan_rejects_new_production_target_without_verified_integration() -> None:
    plan, problem, research, selection = _observable_change_plan_fixture()
    target = {
        "action": "create",
        "path": "src/diagnostics/cause_formatter.py",
        "symbols": ["format_verified_cause"],
        "change": "Render a cause.",
        "rationale_kind": "causal_propagation",
        "rationale": "Use a new formatter.",
        "evidence_refs": ["mechanism_evidence:unverified"],
    }
    targets = plan["change_targets"]
    assert isinstance(targets, list)
    targets.append(target)
    selection["selected_option"] = {
        "causal_coverage": plan["causal_coverage"],
        "scope_evidence": plan["scope_evidence"],
    }
    plan = assign_plan_revision_id(plan)

    ready, reasons = assess_change_plan_readiness(
        plan,
        problem=problem,
        research=research,
        selection=selection,
    )

    assert ready is False
    assert "change_plan_create_target_integration_binding_missing:1" in reasons


def test_live_verification_inference_distinguishes_transport_from_runtime_provenance() -> None:
    required, reasons = infer_live_verification_requirement(
        {"title": "Parser guard is absent", "problem": "Static branch is incorrect"},
        {
            "research_method": "static_trace",
            "run_dir": "runs/research/1",
            "runner_exit_code": 0,
            "artifact_refs": [
                {"kind": "report_json", "path": "runs/research/1/report.json"},
                {"kind": "normalized_events", "path": "events.jsonl"},
            ],
        },
    )
    assert required is False
    assert reasons == ["verification_boundary_unverified_legacy"]

    for title in (
        "Integration parser bug",
        "Service registry default",
        "Network label parser",
    ):
        required, reasons = infer_live_verification_requirement(
            {
                "title": title,
                "problem": "A pure local static-code branch selects the wrong value.",
            },
            {"research_method": "static_trace", "artifact_refs": []},
        )
        assert required is False
        assert reasons == ["verification_boundary_unverified_legacy"]

    required, reasons = infer_live_verification_requirement(
        {
            "title": "Local provider validation parser rejects valid config",
            "problem": "A static parser branch rejects a local configuration value.",
        },
        {"research_method": "static_trace", "artifact_refs": []},
    )
    assert required is False
    assert reasons == ["verification_boundary_unverified_legacy"]

    required, reasons = infer_live_verification_requirement(
        {
            "title": "Codex agent configuration parser picks the wrong default",
            "problem": "A static local configuration branch selects the wrong value.",
        },
        {"research_method": "static_trace", "artifact_refs": []},
    )
    assert required is False
    assert reasons == ["verification_boundary_unverified_legacy"]

    required, reasons = infer_live_verification_requirement(
        {"title": "Shell command failure", "problem": "Execution fails at runtime"},
        {"research_method": "static_trace", "artifact_refs": []},
    )
    assert required is True
    assert "problem_narrative_identifies_runtime_boundary" in reasons

    required, reasons = infer_live_verification_requirement(
        {
            "title": "Claude provider errors can leave stderr empty",
            "problem": "Provider failure details were not preserved in stderr.",
        },
        {"research_method": "reproduction", "artifact_refs": []},
    )
    assert required is True
    assert "problem_narrative_identifies_external_provider_boundary" in reasons

    required, reasons = infer_live_verification_requirement(
        {
            "title": "Provider registry parser uses the wrong local default",
            "problem": "A static configuration branch selects the wrong registry entry.",
        },
        {"research_method": "static_trace", "artifact_refs": []},
    )
    assert required is False
    assert reasons == ["verification_boundary_unverified_legacy"]


def test_live_boundary_uses_runner_provenance_not_scenario_or_provider_labels() -> None:
    research, _ = _runner_research()
    verification = research["evidence_verification"]
    assert isinstance(verification, dict)
    experiment = verification["experiments"][0]
    assert isinstance(experiment, dict)
    argv = ["future-runtime", "probe", "--opaque-mode"]
    authorization = {
        "authorization_kind": "vendor.future/opaque-attestation@v9",
        "executed_argv_sha256": _canonical_hash(argv),
        "shell": False,
        "workspace_confined": True,
        "runner_attested": True,
    }
    authorization["authorization_sha256"] = _canonical_hash(authorization)
    experiment.update(
        {
            "executed_argv": argv,
            "command_authorization": authorization,
            "execution_isolation": {
                "executor": "future-sandbox",
                "boundary": "opaque",
            },
        }
    )
    replay_projection = {
        "experiment_id": experiment["experiment_id"],
        "executed_argv_sha256": _canonical_hash(argv),
        "command_authorization_sha256": authorization["authorization_sha256"],
        "stdout_sha256": experiment["stdout_sha256"],
        "stderr_sha256": experiment["stderr_sha256"],
        "replay_inputs_sha256": None,
        "execution_isolation_sha256": _canonical_hash(
            experiment["execution_isolation"]
        ),
    }
    provenance = verification["verified_mechanism_provenance"]
    assert isinstance(provenance, dict)
    selected_ids = [
        str(evidence["mechanism_evidence_id"])
        for evidence in verification["mechanism_evidence"]
        if evidence["mechanism_evidence_id"] in provenance["mechanism_evidence_ids"]
        and experiment["experiment_id"] in evidence.get("experiment_ids", [])
    ]
    oracle_ids = sorted(
        str(oracle["outcome_oracle_id"])
        for oracle in ticket_readiness.verified_outcome_oracles(research).values()
        if oracle.get("research_experiment_id") == experiment["experiment_id"]
    )
    boundary = {
        "schema_version": 1,
        "experiment_id": experiment["experiment_id"],
        "boundary_kind": "vendor.future/remote-runtime@v9",
        "requires_live_verification": True,
        "faithful_equivalence": False,
        "provenance_refs": sorted(
            {
                f"research_experiment:{experiment['experiment_id']}",
                f"clean_replay:{_canonical_hash(replay_projection)}",
                *selected_ids,
                *oracle_ids,
            }
        ),
        "rationale_sha256": _canonical_hash("runner-owned opaque boundary"),
        "runner_attested": True,
    }
    boundary["boundary_sha256"] = _canonical_hash(boundary)
    verification["verification_boundaries"] = [boundary]

    required, reasons = infer_live_verification_requirement(
        {
            "title": "状態デルタ",
            "problem": "La ruta observada cambia bajo una frontera no enumerada.",
        },
        research,
    )

    assert required is True
    assert reasons == ["runner_verified_external_verification_boundary"]

    tampered = json.loads(json.dumps(research))
    tampered_boundary = tampered["evidence_verification"]["verification_boundaries"][0]
    tampered_boundary["provenance_refs"] = [
        ref
        for ref in tampered_boundary["provenance_refs"]
        if not ref.startswith("clean_replay:")
    ]
    tampered_boundary["boundary_sha256"] = _canonical_hash(
        {
            key: value
            for key, value in tampered_boundary.items()
            if key != "boundary_sha256"
        }
    )
    assert infer_live_verification_requirement({}, tampered) == (
        False,
        ["verification_boundary_unverified_legacy"],
    )


def test_exact_origin_boundary_rejects_atom_snapshot_command_tampering() -> None:
    plan, problem, research, selection = _observable_change_plan_fixture(
        baseline_exit=1,
        after_exit=0,
    )
    plan = assign_plan_revision_id(_bind_staged_outcome_oracle(plan, research))
    assert infer_live_verification_requirement(problem, research) == (
        False,
        ["runner_verified_local_faithful_equivalence"],
    )

    assignment = research["evidence_assignment"]
    assert isinstance(assignment, dict)
    atom_receipt = assignment["atom_receipts"][0]
    atom_receipt["atom_snapshot"]["command"] = "future-runner changed-after-attestation"

    assert infer_live_verification_requirement(problem, research) == (
        False,
        ["verification_boundary_unverified_legacy"],
    )
    ready, reasons = assess_change_plan_readiness(
        plan,
        problem=problem,
        research=research,
        selection=selection,
    )
    assert ready is False
    assert "change_plan_resolution_requires_verified_boundary" in reasons


def test_research_prompts_expose_causal_boundary_and_correction_contract() -> None:
    repo_root = Path(__file__).resolve().parents[3]
    mission_path = (
        repo_root / "configs" / "missions" / "builtin" / "backlog_repro_research.mission.md"
    )
    guidance_path = repo_root / "configs" / "backlog_stage_guidance" / "repro_research.md"
    prompt_requirements = {
        mission_path: {
            "authenticated origin/repository meaning",
            "open registered proof adapter",
            "future solution oracle is optional",
            "system prompt's adapter and output contracts",
            "exact author session",
            "do not start over",
        },
        guidance_path: {
            "authenticated_semantic_citation",
            "future solution success is Stage-4/5 work",
            "json_pointer",
            "registered deterministic predicate",
            "same author session",
            "same workspace",
        },
    }
    for prompt_path, required_fragments in prompt_requirements.items():
        text = " ".join(prompt_path.read_text(encoding="utf-8").split()).casefold()
        missing = sorted(
            fragment for fragment in required_fragments if fragment.casefold() not in text
        )
        assert missing == [], f"{prompt_path}: missing causal-boundary guidance: {missing}"
        assert "complementary control establishes the exact corrected value" not in text
