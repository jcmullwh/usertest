from __future__ import annotations

from typing import Any

import pytest

from backlog_core.causal_proof import (
    CAUSAL_PROOF_SCHEMA_VERSION,
    canonical_json_sha256,
    content_bound_payload,
    evaluate_proof_predicate,
    intervention_id_for,
    material_unknowns_block_advancement,
    proof_receipt_id_for,
    register_proof_predicate,
    validate_causal_proof_receipt,
)


@pytest.mark.parametrize(
    ("predicate", "observed"),
    [
        ({"kind": "equals", "expected": {"status": "ready"}}, {"status": "ready"}),
        ({"kind": "membership", "members": ["linux", "windows"]}, "windows"),
        (
            {"kind": "contains", "expected": "windows-sandbox-rs"},
            "retained stderr from windows-sandbox-rs worker panic",
        ),
        ({"kind": "range", "minimum": 3, "maximum": 7}, 5),
        (
            {
                "kind": "schema",
                "schema": {
                    "type": "object",
                    "required": ["status", "items"],
                    "properties": {
                        "status": {"type": "string"},
                        "items": {"type": "array", "items": {"type": "integer"}},
                    },
                },
            },
            {"status": "ready", "items": [1, 2]},
        ),
        ({"kind": "existence", "expected": True}, {"exists": True}),
        (
            {"kind": "state_transition", "from": "broken", "to": "ready"},
            {"before": "broken", "after": "ready"},
        ),
        (
            {"kind": "event_sequence", "events": ["started", "validated", "published"]},
            ["started", "validated", "published"],
        ),
        (
            {
                "kind": "event_sequence",
                "events": ["started", "published"],
                "mode": "ordered_subsequence",
            },
            ["started", "validated", "published"],
        ),
    ],
)
def test_generic_predicates_accept_runner_observed_values(
    predicate: dict[str, Any], observed: Any
) -> None:
    assert evaluate_proof_predicate(predicate, observed) == (True, [])


def _valid_receipt() -> dict[str, Any]:
    positive_basis = content_bound_payload(
        {
            "basis_kind": "origin_exact_value",
            "origin_atom_ids": ["atom:source"],
            "field_path": "$.expected_status",
            "expected_value_sha256": canonical_json_sha256({"status": "ready"}),
            "predicate_sha256": canonical_json_sha256(
                {"kind": "equals", "expected": {"status": "ready"}}
            ),
            "runner_attested": True,
        },
        hash_field="basis_sha256",
    )
    source_root = content_bound_payload(
        {
            "root_kind": "origin_symptom",
            "origin_atom_ids": ["atom:source"],
            "runner_attested": True,
            "symptom_observation_sha256": "a" * 64,
            "positive_basis": positive_basis,
        },
        hash_field="source_root_sha256",
    )
    baseline = content_bound_payload(
        {
            "experiment_id": "exp:baseline",
            "runner_attested": True,
            "observed": {"status": "broken"},
            "artifact_sha256s": ["b" * 64],
        },
        hash_field="observation_sha256",
    )
    challenge = content_bound_payload(
        {
            "experiment_id": "exp:challenge",
            "runner_attested": True,
            "observed": {"status": "ready"},
            "artifact_sha256s": ["c" * 64],
        },
        hash_field="observation_sha256",
    )
    intervention = {
        "kind": "configuration_value",
        "target": "config:/runtime/mode",
        "baseline_experiment_id": "exp:baseline",
        "challenge_experiment_id": "exp:challenge",
        "predicted_polarity": "failure_to_success",
        "before": "bad",
        "after": "good",
    }
    receipt: dict[str, Any] = {
        "schema_version": CAUSAL_PROOF_SCHEMA_VERSION,
        "adapter_id": "test.conforming",
        "adapter_version": "1",
        "case_id": "case:test",
        "problem_id": "problem:test",
        "hypothesis_id": "hypothesis:test",
        "source_root": source_root,
        "observations": {"baseline": baseline, "challenge": challenge},
        "intervention": intervention,
        "mechanism_graph": {
            "root_node_id": "node:source",
            "outcome_node_id": "node:outcome",
            "nodes": [
                {
                    "node_id": "node:source",
                    "kind": "source_symptom",
                    "locator": "atom:source",
                    "runner_attested": True,
                    "evidence_sha256": "a" * 64,
                },
                {
                    "node_id": "node:config",
                    "kind": "configuration",
                    "locator": "config:/runtime/mode",
                    "runner_attested": True,
                    "evidence_sha256": "b" * 64,
                },
                {
                    "node_id": "node:outcome",
                    "kind": "outcome",
                    "locator": "stdout:/status",
                    "runner_attested": True,
                    "evidence_sha256": "c" * 64,
                },
            ],
            "edges": [
                {
                    "from_node_id": "node:source",
                    "to_node_id": "node:config",
                    "kind": "binds_input",
                    "runner_attested": True,
                    "evidence_sha256": "d" * 64,
                },
                {
                    "from_node_id": "node:config",
                    "to_node_id": "node:outcome",
                    "kind": "changes_observable",
                    "runner_attested": True,
                    "evidence_sha256": "e" * 64,
                },
            ],
        },
        "artifacts": [
            {
                "artifact_id": "artifact:challenge-output",
                "path": "challenge.json",
                "sha256": "c" * 64,
                "size_bytes": 42,
                "runner_attested": True,
            }
        ],
        "positive_outcome": {
            "problem_binding": {
                "origin_atom_ids": ["atom:source"],
                "basis_kind": positive_basis["basis_kind"],
                "basis_sha256": positive_basis["basis_sha256"],
            },
            "predicate": {"kind": "equals", "expected": {"status": "ready"}},
            "observed": {"status": "ready"},
            "observation_source": "artifact_json",
            "runner_evaluated": True,
            "passed": True,
        },
        "adapter_evidence": {"runner_observation": "test-only contract input"},
    }
    receipt["intervention_id"] = intervention_id_for(
        source_root=source_root,
        baseline_observation=baseline,
        challenge_observation=challenge,
        intervention=intervention,
    )
    receipt["proof_receipt_id"] = proof_receipt_id_for(receipt)
    return receipt


def test_causal_proof_contract_accepts_adapter_without_method_whitelist() -> None:
    receipt = _valid_receipt()
    receipt["adapter_id"] = "future.adapter.registered.only.in.test"
    receipt["proof_receipt_id"] = proof_receipt_id_for(receipt)

    assert validate_causal_proof_receipt(receipt) == []


def test_causal_proof_accepts_only_the_explicit_causal_contrast_role() -> None:
    receipt = _valid_receipt()
    receipt["positive_outcome"]["contract_role"] = "causal_contrast"
    receipt["proof_receipt_id"] = proof_receipt_id_for(receipt)

    assert validate_causal_proof_receipt(receipt) == []

    receipt["positive_outcome"]["contract_role"] = "looks_positive_but_is_not_registered"
    receipt["proof_receipt_id"] = proof_receipt_id_for(receipt)

    assert "causal_proof_positive_contract_role_invalid" in (
        validate_causal_proof_receipt(receipt)
    )


def test_replay_inputs_and_portable_observation_are_content_bound() -> None:
    receipt = _valid_receipt()
    baseline = receipt["observations"]["baseline"]
    challenge = receipt["observations"]["challenge"]
    receipt["replay_inputs"] = content_bound_payload(
        {
            "schema_version": 1,
            "source_experiment_id": baseline["experiment_id"],
            "environment": {"DEPTH_MODE": "broken"},
            "disposable_state_paths": [".tmp/depth-state"],
            "runner_approved": True,
        },
        hash_field="replay_inputs_sha256",
    )
    receipt["replay_observation"] = content_bound_payload(
        {
            "schema_version": 1,
            "source_experiment_id": baseline["experiment_id"],
            "selector": {"source": "stdout_json", "json_pointer": "/status"},
            "source_observation_sha256": baseline["observation_sha256"],
            "positive_reference_experiment_id": challenge["experiment_id"],
            "positive_reference_selector": {
                "source": "workspace_state",
                "path": ".tmp/depth-state/result.json",
                "json_pointer": "/status",
            },
            "positive_reference_observation_sha256": challenge["observation_sha256"],
            "predicate_input_mode": "post_change_observation",
            "runner_attested": True,
        },
        hash_field="replay_observation_sha256",
    )
    receipt["proof_receipt_id"] = proof_receipt_id_for(receipt)

    assert validate_causal_proof_receipt(receipt) == []

    receipt["replay_observation"]["selector"] = {
        "source": "workspace_state",
        "path": "../../outside.json",
    }
    receipt["replay_observation"] = content_bound_payload(
        receipt["replay_observation"],
        hash_field="replay_observation_sha256",
    )
    receipt["proof_receipt_id"] = proof_receipt_id_for(receipt)

    assert "causal_proof_replay_selector_workspace_path_invalid" in (
        validate_causal_proof_receipt(receipt)
    )


@pytest.mark.parametrize(
    ("mutation", "expected_error"),
    [
        (
            lambda receipt: receipt["source_root"].update(runner_attested=False),
            "causal_proof_source_root_not_attested",
        ),
        (
            lambda receipt: receipt["observations"]["challenge"].update(runner_attested=False),
            "causal_proof_challenge_not_runner_attested",
        ),
        (
            lambda receipt: receipt["mechanism_graph"]["edges"][0].update(
                runner_attested=False
            ),
            "causal_proof_mechanism_edge_not_attested",
        ),
        (
            lambda receipt: receipt["positive_outcome"].update(
                observed=0,
                predicate={"kind": "equals", "expected": 0},
                observation_source="exit_code",
            ),
            "causal_proof_positive_outcome_not_challenge_bound",
        ),
        (
            lambda receipt: receipt["positive_outcome"].update(
                predicate={"kind": "existence", "expected": False},
                observed={"exists": False},
            ),
            "causal_proof_positive_outcome_not_challenge_bound",
        ),
    ],
)
def test_causal_proof_contract_rejects_fabricated_or_surface_evidence(
    mutation: Any, expected_error: str
) -> None:
    receipt = _valid_receipt()
    mutation(receipt)
    receipt["proof_receipt_id"] = proof_receipt_id_for(receipt)

    assert expected_error in validate_causal_proof_receipt(receipt)


def test_content_bound_ids_change_with_runner_observation() -> None:
    receipt = _valid_receipt()
    original_intervention = receipt["intervention_id"]
    challenge = dict(receipt["observations"]["challenge"])
    challenge["observed"] = {"status": "different"}
    challenge = content_bound_payload(challenge, hash_field="observation_sha256")

    assert intervention_id_for(
        source_root=receipt["source_root"],
        baseline_observation=receipt["observations"]["baseline"],
        challenge_observation=challenge,
        intervention=receipt["intervention"],
    ) != original_intervention
    assert canonical_json_sha256(challenge) != canonical_json_sha256(
        receipt["observations"]["challenge"]
    )


def test_causal_proof_binds_predicate_to_authenticated_positive_basis() -> None:
    receipt = _valid_receipt()
    receipt["positive_outcome"]["predicate"] = {
        "kind": "membership",
        "members": [{"status": "broken"}, {"status": "ready"}],
    }
    receipt["proof_receipt_id"] = proof_receipt_id_for(receipt)

    assert "causal_proof_positive_basis_predicate_mismatch" in (
        validate_causal_proof_receipt(receipt)
    )


def test_causal_proof_requires_positive_contract_to_fail_at_baseline() -> None:
    receipt = _valid_receipt()
    predicate = {"kind": "existence", "expected": True}
    positive_basis = content_bound_payload(
        {
            **receipt["source_root"]["positive_basis"],
            "predicate_sha256": canonical_json_sha256(predicate),
        },
        hash_field="basis_sha256",
    )
    receipt["source_root"] = content_bound_payload(
        {
            **receipt["source_root"],
            "positive_basis": positive_basis,
        },
        hash_field="source_root_sha256",
    )
    for label, observed in (
        ("baseline", {"exists": True}),
        ("challenge", {"exists": True, "detail": "changed but already successful"}),
    ):
        observation = {
            **receipt["observations"][label],
            "observed": observed,
        }
        receipt["observations"][label] = content_bound_payload(
            observation,
            hash_field="observation_sha256",
        )
    receipt["positive_outcome"].update(
        problem_binding={
            "origin_atom_ids": ["atom:source"],
            "basis_kind": positive_basis["basis_kind"],
            "basis_sha256": positive_basis["basis_sha256"],
        },
        predicate=predicate,
        observed=receipt["observations"]["challenge"]["observed"],
        passed=True,
    )
    receipt["intervention_id"] = intervention_id_for(
        source_root=receipt["source_root"],
        baseline_observation=receipt["observations"]["baseline"],
        challenge_observation=receipt["observations"]["challenge"],
        intervention=receipt["intervention"],
    )
    receipt["proof_receipt_id"] = proof_receipt_id_for(receipt)

    assert "causal_proof_baseline_already_satisfies_positive_outcome" in (
        validate_causal_proof_receipt(receipt)
    )


@pytest.mark.parametrize(
    ("before", "after", "predicate", "source"),
    [
        (1, 0, {"kind": "equals", "expected": 0}, "exit_code"),
        (
            {"exists": True},
            {"exists": False},
            {"kind": "existence", "expected": False},
            "filesystem_existence",
        ),
    ],
)
def test_surface_observable_is_valid_when_it_is_the_controlled_problem_bound_delta(
    before: Any,
    after: Any,
    predicate: dict[str, Any],
    source: str,
) -> None:
    receipt = _valid_receipt()
    positive_basis = content_bound_payload(
        {
            **receipt["source_root"]["positive_basis"],
            "predicate_sha256": canonical_json_sha256(predicate),
        },
        hash_field="basis_sha256",
    )
    receipt["source_root"] = content_bound_payload(
        {
            **receipt["source_root"],
            "positive_basis": positive_basis,
        },
        hash_field="source_root_sha256",
    )
    for label, observed in (("baseline", before), ("challenge", after)):
        observation = dict(receipt["observations"][label])
        observation["observed"] = observed
        receipt["observations"][label] = content_bound_payload(
            observation,
            hash_field="observation_sha256",
        )
    receipt["positive_outcome"].update(
        problem_binding={
            "origin_atom_ids": ["atom:source"],
            "basis_kind": positive_basis["basis_kind"],
            "basis_sha256": positive_basis["basis_sha256"],
        },
        predicate=predicate,
        observed=after,
        observation_source=source,
        passed=True,
    )
    receipt["intervention_id"] = intervention_id_for(
        source_root=receipt["source_root"],
        baseline_observation=receipt["observations"]["baseline"],
        challenge_observation=receipt["observations"]["challenge"],
        intervention=receipt["intervention"],
    )
    receipt["proof_receipt_id"] = proof_receipt_id_for(receipt)

    assert validate_causal_proof_receipt(receipt) == []


def test_adapter_owned_semantic_types_are_not_central_whitelists() -> None:
    receipt = _valid_receipt()
    receipt["intervention"]["predicted_polarity"] = "vendor.example/reconciled@v2"
    receipt["mechanism_graph"]["nodes"][1]["kind"] = "vendor.example/state-machine"
    receipt["mechanism_graph"]["edges"][1]["kind"] = "vendor.example/emits"
    positive_basis = content_bound_payload(
        {
            **receipt["source_root"]["positive_basis"],
            "basis_kind": "vendor.example/domain-contract",
        },
        hash_field="basis_sha256",
    )
    receipt["source_root"] = content_bound_payload(
        {
            **receipt["source_root"],
            "positive_basis": positive_basis,
        },
        hash_field="source_root_sha256",
    )
    receipt["positive_outcome"]["problem_binding"].update(
        basis_kind=positive_basis["basis_kind"],
        basis_sha256=positive_basis["basis_sha256"],
    )
    receipt["intervention_id"] = intervention_id_for(
        source_root=receipt["source_root"],
        baseline_observation=receipt["observations"]["baseline"],
        challenge_observation=receipt["observations"]["challenge"],
        intervention=receipt["intervention"],
    )
    receipt["proof_receipt_id"] = proof_receipt_id_for(receipt)

    assert validate_causal_proof_receipt(receipt) == []


def test_material_unknowns_use_explicit_materiality_not_category_whitelist() -> None:
    assert material_unknowns_block_advancement([]) is False
    assert material_unknowns_block_advancement(
        [
            {
                "unknown": "A future domain uncertainty",
                "affects": ["vendor_specific_decision"],
                "material": True,
            }
        ]
    ) is True
    assert material_unknowns_block_advancement(
        [
            {
                "unknown": "An explicitly non-blocking observation",
                "affects": ["telemetry_only"],
                "material": False,
            }
        ]
    ) is False


def test_registered_domain_predicate_and_source_root_type_need_no_core_edit() -> None:
    kind = "test.vendor.casefold-equals"
    register_proof_predicate(
        kind,
        contract_validator=lambda predicate: (
            [] if isinstance(predicate.get("expected"), str) else ["expected_invalid"]
        ),
        evaluator=lambda predicate, observed: (
            isinstance(observed, str)
            and observed.casefold() == str(predicate["expected"]).casefold(),
            [],
        ),
    )
    receipt = _valid_receipt()
    predicate = {"kind": kind, "expected": "READY"}
    positive_basis = content_bound_payload(
        {
            **receipt["source_root"]["positive_basis"],
            "predicate_sha256": canonical_json_sha256(predicate),
        },
        hash_field="basis_sha256",
    )
    receipt["source_root"] = content_bound_payload(
        {
            **receipt["source_root"],
            "root_kind": "vendor.example/authenticated-origin",
            "positive_basis": positive_basis,
        },
        hash_field="source_root_sha256",
    )
    receipt["positive_outcome"].update(
        predicate=predicate,
        observed="ready",
        problem_binding={
            "origin_atom_ids": ["atom:source"],
            "basis_kind": positive_basis["basis_kind"],
            "basis_sha256": positive_basis["basis_sha256"],
        },
    )
    for label, observed in (("baseline", "broken"), ("challenge", "ready")):
        receipt["observations"][label] = content_bound_payload(
            {**receipt["observations"][label], "observed": observed},
            hash_field="observation_sha256",
        )
    receipt["intervention_id"] = intervention_id_for(
        source_root=receipt["source_root"],
        baseline_observation=receipt["observations"]["baseline"],
        challenge_observation=receipt["observations"]["challenge"],
        intervention=receipt["intervention"],
    )
    receipt["proof_receipt_id"] = proof_receipt_id_for(receipt)

    assert validate_causal_proof_receipt(receipt) == []
