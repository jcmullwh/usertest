from __future__ import annotations

import hashlib
import json
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


def _synthetic_positive_contract(evidence_id: str) -> dict[str, object]:
    contract: dict[str, object] = {
        "schema_version": 1,
        "kind": "origin_evidence_semantic_contract",
        "research_experiment_id": "experiment:support",
        "mechanism_evidence_ids": [evidence_id],
        "postconditions": [
            {"type": "command_exit_code", "command_index": 0, "equals": 0}
        ],
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
    positive_contract = _synthetic_positive_contract(evidence_id)
    oracle: dict[str, object] = {
        "schema_version": 1,
        "research_experiment_id": "experiment:support",
        "positive_outcome_contracts": [positive_contract],
    }
    oracle["outcome_oracle_id"] = _content_id(
        "outcome_oracle", oracle, "outcome_oracle_id"
    )
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
        consumer_identity = {
            "kind": "production_entrypoint",
            "entrypoint": consumer_name,
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
            "inspected_symbols": [
                {"symbol": "shared.apply", "path": "src/shared.py"}
            ],
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
                    "declared_result": (
                        "The failure remains when the alternative is removed"
                    ),
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
        "observed_polarity": {
            "polarity": "failure_persists_after_intervention"
        },
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
    assert plan_revision_id_for({**plan, "proposed_fix": "Different fix"}) != assigned[
        "plan_revision_id"
    ]


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
            "original_scenario": {
                "oracle": {"mechanism_evidence_ids": [evidence_ids[0]]}
            }
        }
    }

    assert ticket_readiness._broad_scope_outcome_coverage_reasons(
        plan,
        selected_option=option,
        research=research,
    ) == ["change_plan_broad_scope_outcome_path_coverage_missing"]

    plan["outcome_verification_roles"]["original_scenario"]["oracle"][
        "mechanism_evidence_ids"
    ] = evidence_ids
    assert ticket_readiness._broad_scope_outcome_coverage_reasons(
        plan,
        selected_option=option,
        research=research,
    ) == []


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

    assert ticket_readiness._broad_scope_outcome_coverage_reasons(
        plan,
        selected_option=option,
        research=research,
    ) == []


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
        path["failure_path_id"] = _content_id(
            "failure_path", path, "failure_path_id"
        )

    evidence_items = verification["mechanism_evidence"]
    assert isinstance(evidence_items, list)
    for evidence in evidence_items:
        assert isinstance(evidence, dict)
        old_control_id = str(evidence["strong_pytest_control_id"])
        evidence["mechanism_symbols"] = symbols
        evidence["code_paths"] = [
            {"symbol": symbol, "path": "src/shared.py"} for symbol in symbols
        ]
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
            "inspected_symbols": [
                {"symbol": "shared.apply", "path": "src/shared.py"}
            ],
        },
    }

    ready, reasons = assess_solution_option_readiness(option, research=research)

    assert ready is False
    assert "solution_option_research_hypothesis_unbound" in reasons


def test_broad_scope_cannot_count_experiment_and_its_artifact_as_independent() -> None:
    research = {
        "artifact_refs": [
            {"artifact_id": "artifact:stdout", "path": "evidence/stdout.txt"}
        ],
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


def _falsification_review(control_id: str) -> dict[str, object]:
    contract_id = str(
        _synthetic_positive_contract(control_id)["positive_outcome_contract_id"]
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
    }


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
    review = _falsification_review(evidence_id)
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
    assert bound["adversarial_evidence_receipt"][
        "selected_positive_outcome_contract_ids"
    ] == selected_ids
    assert (
        falsification_review_receipt_errors(
            bound,
            problem_id="problem:test",
            selected_option=option,
            research=research,
        )
        == []
    )


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
        _falsification_review(evidence_id),
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
        _falsification_review(evidence_id),
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
        item
        for item in research["experiments"]
        if item["experiment_id"] == "experiment:support"
    )
    closure: dict[str, object] = {
        "verification_method": "runner_deterministic_mechanism_closure_v1",
        "hypothesis_id": "h1",
        "support_experiment_id": "experiment:support",
        "scenario_kind": "original_replay",
        "mechanism_symbols": ["shared.apply"],
        "code_path": [{"symbol": "shared.apply", "path": "src/shared.py"}],
        "closure_basis": "complete_runner_dataflow",
        "alternatives_disposed": [],
        "origin_atom_ids": ["atom:a"],
        "observed_result": {
            "exit_code": replay["exit_code"],
            "stdout_sha256": replay["stdout_sha256"],
            "stderr_sha256": replay["stderr_sha256"],
            "assertion": experiment["observable_assertion"],
        },
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
        _falsification_review(str(evidence[0]["mechanism_evidence_id"])),
        problem_id="problem:test",
        selected_option=option,
        research=research,
    )
    assert falsification_acceptance_has_adversarial_basis(bound) is True
    receipt = bound["adversarial_evidence_receipt"]
    assert receipt["falsification_attempts"] == []
    assert receipt["deterministic_mechanism_closures"] == [closure]
    assert falsification_review_receipt_errors(
        bound,
        problem_id="problem:test",
        selected_option=option,
        research=research,
    ) == []
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
        _falsification_review(evidence_id),
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
    review = _falsification_review(valid_evidence_id)
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
        _falsification_review(str(evidence[0]["mechanism_evidence_id"])),
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
        "implementation_steps": [
            "Update `classifier.classify` to preserve the verified cause."
        ],
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
    mechanism_evidence_ids = [
        str(item["mechanism_evidence_id"])
        for item in (
            mechanism_evidence if isinstance(mechanism_evidence, list) else []
        )
        if isinstance(item, dict) and "mechanism_evidence_id" in item
    ] or ["mechanism_evidence:oracle"]
    oracle: dict[str, object] = {
        "schema_version": 1,
        "case_id": plan["case_id"],
        "repo_revision": research["repo_revision"],
        "research_experiment_id": experiment["experiment_id"],
        "scenario_kind": experiment["scenario_kind"],
        "origin_atom_ids": ["atom:oracle"],
        "mechanism_evidence_ids": mechanism_evidence_ids,
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
    }
    positive_contract["positive_outcome_contract_id"] = _content_id(
        "positive_outcome_contract",
        positive_contract,
        "positive_outcome_contract_id",
    )
    if include_positive_contract:
        oracle["positive_outcome_contracts"] = [positive_contract]
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
            "control_expected_sha256": _canonical_hash(
                "classification=incomplete"
            ),
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
        assertion.get("expected")
        not in {"planner-invented-success", "classification=incomplete"}
        for assertion in bound_after["observable_assertions"]
    )
    roles = bound["outcome_verification_roles"]
    assert isinstance(roles, dict)
    original = roles["original_scenario"]
    assert isinstance(original, dict)
    assert all(
        predicate.get("type") != "command_stdout_equals"
        for predicate in original["predicates"]
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
    members = [
        {
            "case_id": "case:oracle",
            "problem_id": "problem:oracle",
            "repo_revision": research["repo_revision"],
            "evidence_verification": {
                "status": "verified",
                "outcome_oracles": [oracle_one],
            },
        },
        {
            "case_id": "case:oracle-two",
            "problem_id": "problem:oracle-two",
            "repo_revision": research["repo_revision"],
            "evidence_verification": {
                "status": "verified",
                "outcome_oracles": [oracle_two],
            },
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
    after["observable_assertions"] = [
        {"source": "exit_code", "operator": "equals", "expected": 0}
    ]
    roles = plan["outcome_verification_roles"]
    assert isinstance(roles, dict)
    original = roles["original_scenario"]
    assert isinstance(original, dict)
    original["predicates"] = [
        {"type": "command_exit_code", "command_index": 0, "equals": 0}
    ]
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
        match="research_positive_outcome_contract_missing",
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
        predicate.get("type") == "artifact_json_value"
        for predicate in original["predicates"]
    )
    assert "artifact_expectations" not in plan["before_after_reproduction"][
        "after_change"
    ]


def test_runner_addressed_config_state_is_a_positive_postcondition() -> None:
    assert ticket_readiness._positive_outcome_predicate(
        {
            "type": "oracle_state_equals",
            "target_id": "config_state:verified",
            "exists": True,
            "equals": "safe",
        }
    ) is True
    assert ticket_readiness._positive_outcome_predicate(
        {"type": "command_exit_code", "command_index": 0, "equals": 0}
    ) is False
    assert ticket_readiness._positive_outcome_predicate(
        {
            "type": "command_stderr_not_contains",
            "command_index": 0,
            "value": "failure",
        }
    ) is False


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


def test_change_plan_allows_new_production_target_via_verified_integration_boundary() -> None:
    plan, problem, research, selection = _observable_change_plan_fixture()
    plan = _bind_staged_outcome_oracle(plan, research)
    existing_verification = research["evidence_verification"]
    assert isinstance(existing_verification, dict)
    evidence: dict[str, object] = {
        "evidence_type": "observed_output",
        "hypothesis_id": "h-classifier",
        "mechanism_symbols": ["classifier.classify"],
        "code_paths": [
            {"path": "src/classifier.py", "symbol": "classifier.classify"}
        ],
    }
    evidence["mechanism_evidence_id"] = _content_id(
        "mechanism_evidence", evidence, "mechanism_evidence_id"
    )
    evidence_id = str(evidence["mechanism_evidence_id"])
    research["evidence_verification"] = {
        "status": "verified",
        "inspected_symbols": [
            {"path": "src/classifier.py", "symbol": "classifier.classify"}
        ],
        "mechanism_evidence": [evidence],
        "outcome_oracles": existing_verification["outcome_oracles"],
    }
    causal_coverage = {
        "mechanism_addressed": "False classification",
        "research_binding": {
            "intervention_points": [
                {
                    "target_path": "src/classifier.py",
                    "target_symbol": "classifier.classify",
                    "intervention": (
                        "Classify the retained condition from its actual cause."
                    ),
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
    assert reasons == []

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
        assert reasons == []

    required, reasons = infer_live_verification_requirement(
        {
            "title": "Local provider validation parser rejects valid config",
            "problem": "A static parser branch rejects a local configuration value.",
        },
        {"research_method": "static_trace", "artifact_refs": []},
    )
    assert required is False
    assert reasons == []

    required, reasons = infer_live_verification_requirement(
        {
            "title": "Codex agent configuration parser picks the wrong default",
            "problem": "A static local configuration branch selects the wrong value.",
        },
        {"research_method": "static_trace", "artifact_refs": []},
    )
    assert required is False
    assert reasons == []

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
    assert reasons == []


def test_research_prompts_expose_retained_harness_positive_contract() -> None:
    repo_root = Path(__file__).resolve().parents[3]
    prompt_paths = [
        repo_root / "configs" / "missions" / "builtin" / "backlog_repro_research.mission.md",
        repo_root / "configs" / "backlog_stage_guidance" / "repro_research.md",
    ]
    required_fragments = {
        "retained_harness_semantic_assertion",
        "semantic_relation",
        "semantic_rationale",
        "semantic_basis",
        "source_atom_quote",
        "repository_contract_quote",
        "adversarial_review_reference",
        "contract_subject",
        "json_pointer",
        "symbol",
        "stage 5",
    }
    for prompt_path in prompt_paths:
        text = prompt_path.read_text(encoding="utf-8").casefold()
        missing = sorted(
            fragment for fragment in required_fragments if fragment.casefold() not in text
        )
        assert missing == [], f"{prompt_path}: missing retained-harness guidance: {missing}"
        assert "complementary control establishes the exact corrected value" not in text
