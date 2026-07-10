from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path

from backlog_core.backlog_policy import BacklogPolicyConfig, apply_backlog_policy
from backlog_core.stage_contracts import (
    evidence_assignment_sha256,
    evidence_verification_sha256,
    research_claims_sha256,
)
from backlog_core.ticket_readiness import (
    assign_plan_revision_id,
    bind_falsification_review,
    bind_plan_outcome_oracle,
)


def _canonical_sha256(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return sha256(encoded.encode()).hexdigest()


def _research_proof() -> dict[str, object]:
    proof: dict[str, object] = {
        "research_schema_version": 3,
        "case_id": "case:test",
        "problem_id": "problem:test",
        "repo_revision": "abc123",
        "research_method": "reproduction",
        "reproduction_status": "reproduced",
        "research_status": "evidence_sufficient",
        "writes_used": True,
        "writes_purpose": ["failing_test"],
        "implementation_performed": False,
        "diff_classification": "allowed_research_edits",
        "artifact_refs": [
            {"artifact_id": "artifact:repro", "kind": "test", "path": "repro.txt"},
            {"artifact_id": "artifact:source", "kind": "source", "path": "src/core.py"},
        ],
        "experiments": [
            {
                "experiment_id": "exp-1",
                "scenario_kind": "original_replay",
                "addresses_atom_ids": ["atom:test"],
                "command": "pytest -q tests/test_core.py::test_reported_failure",
                "result": "Failed as reported",
                "outcome": "supports",
                "exit_code": 1,
                "observable_assertion": {
                    "source": "exit_code",
                    "operator": "equals",
                    "expected": 1,
                },
                "origin_evidence_bindings": [
                    {
                        "atom_id": "atom:test",
                        "role": "expected_behavior",
                        "field_path": "$.expected_output",
                        "value": "guard applied",
                        "value_sha256": _canonical_sha256("guard applied"),
                    }
                ],
                "positive_outcome_contract": {
                    "contract_kind": "origin_atom_exact_value",
                    "atom_id": "atom:test",
                    "field_path": "$.expected_output",
                    "postcondition": {
                        "type": "command_stdout_contains",
                        "value": "guard applied",
                    },
                },
                "artifact_refs": ["artifact:repro", "artifact:source"],
            },
            {
                "experiment_id": "exp-control",
                "scenario_kind": "control",
                "control_relationship": {
                    "supports_experiment_id": "exp-1",
                    "mechanism_symbols": ["core.run"],
                    "controlled_variable": "guard enabled",
                    "expected_difference": "The guarded control succeeds without the symptom.",
                },
                "addresses_atom_ids": ["atom:test"],
                "command": "pytest -q tests/test_core.py::test_guarded_control",
                "result": "The guarded control path succeeds",
                "outcome": "refutes",
                "exit_code": 0,
                "observable_assertion": {
                    "source": "exit_code",
                    "operator": "equals",
                    "expected": 0,
                },
                "artifact_refs": ["artifact:repro", "artifact:source"],
            },
            {
                "experiment_id": "exp-challenge",
                "scenario_kind": "control",
                "control_relationship": {
                    "supports_experiment_id": "exp-1",
                    "mechanism_symbols": ["core.run"],
                    "controlled_variable": "the strongest alternative cause",
                    "expected_difference": (
                        "The failure disappears only if the alternative is causal."
                    ),
                },
                "addresses_atom_ids": ["atom:test"],
                "command": "pytest -q tests/test_core.py::test_alternative_removed",
                "result": "The reported failure remains",
                "outcome": "supports",
                "exit_code": 1,
                "observable_assertion": {
                    "source": "exit_code",
                    "operator": "equals",
                    "expected": 1,
                },
                "artifact_refs": ["artifact:repro", "artifact:source"],
            },
        ],
        "inspected_files": ["src/core.py"],
        "inspected_symbols": ["core.run"],
        "root_cause_hypotheses": [
            {
                "hypothesis_id": "h1",
                "statement": "Missing guard",
                "supporting_evidence": ["exp-1", "exp-challenge"],
                "counterevidence": ["exp-control"],
                "falsification_attempts": [
                    {
                        "attempt_id": "falsify-h1-alternative",
                        "hypothesis_id": "h1",
                        "claim": "Missing guard",
                        "baseline_experiment_id": "exp-1",
                        "challenge_experiment_id": "exp-challenge",
                        "disproof_condition": {
                            "source": "exit_code",
                            "operator": "equals",
                            "expected": 0,
                        },
                        "outcome": "survived",
                    }
                ],
                "mechanism_symbols": ["core.run"],
                "disposition": "primary",
                "disposition_evidence": ["exp-1", "exp-control"],
            }
        ],
        "root_cause_confidence": 0.9,
        "broader_class_assessment": "unknown",
        "material_unknowns": [],
        "blocking_reasons": [],
        "evidence_boundaries": [],
    }
    assignment: dict[str, object] = {
        "status": "complete",
        "errors": [],
        "case_id": proof["case_id"],
        "problem_id": proof["problem_id"],
        "expected_atom_ids": ["atom:test"],
        "atom_receipts": [
            {
                "atom_id": "atom:test",
                "atom_sha256": sha256(
                    json.dumps(
                        {
                            "atom_id": "atom:test",
                            "text": "failure",
                            "command": (
                                "pytest -q tests/test_core.py::test_reported_failure"
                            ),
                            "exit_code": 1,
                            "evidence_role": "observation",
                            "origin_stage": "runtime",
                            "expected_output": "guard applied",
                        },
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode()
                ).hexdigest(),
                "atom_snapshot": {
                    "atom_id": "atom:test",
                    "text": "failure",
                    "command": "pytest -q tests/test_core.py::test_reported_failure",
                    "exit_code": 1,
                    "evidence_role": "observation",
                    "origin_stage": "runtime",
                    "expected_output": "guard applied",
                },
                "artifact_receipts": [
                    {"path": "C:/runs/origin.json", "sha256": "5" * 64, "size_bytes": 7}
                ],
            }
        ],
    }
    assignment["assignment_sha256"] = evidence_assignment_sha256(assignment)
    proof["evidence_assignment"] = assignment
    isolation = {
        "executor": "trusted_host",
        "os_sandbox": False,
        "network": "not_enforced",
        "filesystem_isolation": "dedicated_clone_only_not_os_sandbox",
        "trust_decision": "approved_local_source_root",
        "trust_reason": "C:/runs/source",
        "source_workspace": "C:/runs/research-workspace",
        "sanitized_environment_keys": ["CI"],
    }
    verification = {
        "verification_method": "runner_artifact_binding_v1",
        "status": "verified",
        "case_id": proof["case_id"],
        "problem_id": proof["problem_id"],
        "repo_revision": proof["repo_revision"],
        "requested_repo_ref": "origin/dev",
        "resolved_repo_ref": proof["repo_revision"],
        "workspace_dir": "C:/runs/research-workspace",
        "workspace_head": proof["repo_revision"],
        "workspace_overlay": {
            "baseline_manifest_sha256": "6" * 64,
            "research_manifest_sha256": "7" * 64,
            "baseline_state_sha256": "8" * 64,
            "research_state_sha256": "9" * 64,
            "baseline_git_index_sha256": "a" * 64,
            "research_git_index_sha256": "b" * 64,
            "changed_baseline_paths": [],
            "research_overlay_paths": [".usertest_research/repro.txt"],
            "research_overlay_manifest": {
                ".usertest_research/repro.txt": {
                    "kind": "file",
                    "mode": 420,
                    "sha256": "c" * 64,
                    "size_bytes": 12,
                }
            },
            "research_overlay_manifest_sha256": "d" * 64,
            "suspicious_extra_paths": [],
            "git_index_changed": False,
        },
        "replay_isolation": isolation,
        "planning_workspace_dir": "C:/runs/planning-workspace",
        "planning_workspace_head": proof["repo_revision"],
        "planning_workspace_clean": True,
        "run_dir": "C:/runs/research",
        "origin_atom_ids": ["atom:test"],
        "assignment_sha256": assignment["assignment_sha256"],
        "claims_sha256": research_claims_sha256(proof),
        "normalized_events_sha256": "a" * 64,
        "run_report_sha256": "e" * 64,
        "artifacts": [
            {
                "artifact_id": "artifact:repro",
                "kind": "test",
                "path": "repro.txt",
                "sha256": "b" * 64,
                "size_bytes": 12,
            },
            {
                "artifact_id": "artifact:source",
                "kind": "source",
                "path": "src/core.py",
                "sha256": "d" * 64,
                "size_bytes": 42,
            },
        ],
        "experiments": [
            {
                "experiment_id": experiment["experiment_id"],
                "command": experiment["command"],
                "executed_argv": experiment["command"].split(),
                "exit_code": experiment["exit_code"],
                "event_index": index,
                "agent_event_index": index,
                "agent_event_sha256": "c" * 64,
                "agent_output_excerpt_sha256": None,
                "scenario_kind": experiment["scenario_kind"],
                "addresses_atom_ids": experiment["addresses_atom_ids"],
                "declared_result": experiment["result"],
                "outcome": experiment["outcome"],
                "workspace_dir": f"C:/runs/replay-{index}",
                "workspace_head": proof["repo_revision"],
                "baseline_state_sha256": "4" * 64,
                "pre_replay_state_sha256": "5" * 64,
                "post_replay_state_sha256": "5" * 64,
                "post_replay_mutations": False,
                "overlay_manifest_sha256": "d" * 64,
                "execution_isolation": isolation,
                "execution_metadata": {
                    "executor": "trusted_host",
                    "os_sandbox": False,
                    "network": "not_enforced",
                },
                "stdout_path": f"C:/runs/replay-{index}/stdout.txt",
                "stderr_path": f"C:/runs/replay-{index}/stderr.txt",
                "stdout_sha256": "f" * 64,
                "stderr_sha256": "1" * 64,
                "observable_assertion": experiment["observable_assertion"],
                "assertion_passed": True,
                "artifact_refs": experiment["artifact_refs"],
            }
            for index, experiment in enumerate(proof["experiments"])
            if isinstance(experiment, dict)
        ],
        "inspected_files": [
            {
                "path": "src/core.py",
                "sha256": "d" * 64,
                "git_blob_sha": "2" * 40,
                "size_bytes": 42,
                "read_event_index": 2,
                "read_event_sha256": "3" * 64,
                "read_source": "tool",
                "bytes_observed": 42,
                "whole_file_observed": True,
                "observed_content_sha256": "4" * 64,
                "observed_start_line": 1,
                "observed_end_line": 3,
            }
        ],
        "inspected_symbols": [{"symbol": "core.run", "path": "src/core.py"}],
        "hypothesis_refs": [
            {
                "hypothesis_id": "h1",
                "supporting_refs": ["exp-1", "exp-challenge"],
                "counterevidence_refs": ["exp-control"],
                "mechanism_symbols": ["core.run"],
                "disposition": "primary",
                "disposition_evidence_refs": ["exp-1", "exp-control"],
                "control_links": [
                    {
                        "control_experiment_id": "exp-control",
                        "supports_experiment_id": "exp-1",
                        "mechanism_symbols": ["core.run"],
                        "shared_atom_ids": ["atom:test"],
                        "shared_artifact_refs": ["artifact:repro", "artifact:source"],
                        "controlled_variable": "guard enabled",
                        "expected_difference": (
                            "The guarded control succeeds without the symptom."
                        ),
                    }
                ],
            }
        ],
        "causal_links": [
            {
                "hypothesis_id": "h1",
                "experiment_id": "exp-1",
                "symbol": "core.run",
                "path": "src/core.py",
                "stream": "stderr",
                "trace_kind": "python_traceback",
                "trace_excerpt_sha256": "8" * 64,
                "stream_sha256": "1" * 64,
            }
        ],
        "test_selections": [
            {
                "selection_id": f"h1:{experiment_id}",
                "hypothesis_id": "h1",
                "experiment_id": experiment_id,
                "runner": "pytest",
                "command_sha256": sha256(command.encode()).hexdigest(),
                "executed_argv_sha256": _canonical_sha256(command.split()),
                "test_path": "tests/test_core.py",
                "test_file_sha256": "7" * 64,
                "test_file_git_blob_sha": "2" * 40,
                "selector": selector,
                "selector_parts": [selector],
                "test_function": selector,
                "test_function_line": line,
                "test_function_source_sha256": "8" * 64,
                "reachable_functions": [selector],
                "mechanism_touches": [
                    {
                        "symbol": "core.run",
                        "source_path": "src/core.py",
                        "calls": [
                            {
                                "function": selector,
                                "line": line + 1,
                                "expression": "run",
                                "resolved_target": "core.run",
                            }
                        ],
                    }
                ],
            }
            for experiment_id, command, selector, line in (
                (
                    "exp-1",
                    "pytest -q tests/test_core.py::test_reported_failure",
                    "test_reported_failure",
                    4,
                ),
                (
                    "exp-control",
                    "pytest -q tests/test_core.py::test_guarded_control",
                    "test_guarded_control",
                    8,
                ),
            )
        ],
        "control_verifications": [
            {
                "verification_method": "pytest_ast_mechanism_call_v1",
                "hypothesis_id": "h1",
                "support_experiment_id": "exp-1",
                "control_experiment_id": "exp-control",
                "support_selection_id": "h1:exp-1",
                "control_selection_id": "h1:exp-control",
                "mechanism_symbols": ["core.run"],
                "shared_verified_mechanism_symbols": ["core.run"],
                "same_test_file": True,
                "relationship_sha256": _canonical_sha256(
                    {
                        "controlled_variable": "guard enabled",
                        "expected_difference": (
                            "The guarded control succeeds without the symptom."
                        ),
                        "mechanism_symbols": ["core.run"],
                    }
                ),
            }
        ],
        "atom_bindings": [
            {
                "experiment_id": "exp-1",
                "atom_id": "atom:test",
                "match_kind": "command_and_exit_code",
            },
            {
                "experiment_id": "exp-1",
                "atom_id": "atom:test",
                "binding_role": "expected_behavior",
                "match_kind": "explicit_field_binding",
                "origin_atom_sha256": assignment["atom_receipts"][0]["atom_sha256"],
                "origin_atom_field_path": "$.expected_output",
                "origin_atom_value_sha256": _canonical_sha256("guard applied"),
            },
        ],
        "errors": [],
    }
    support_selection = verification["test_selections"][0]
    control_selection = verification["test_selections"][1]
    support_call = support_selection["mechanism_touches"][0]["calls"][0]
    control_call = control_selection["mechanism_touches"][0]["calls"][0]
    support_call.update({"arguments": [], "arguments_complete": True})
    control_argument = {
        "slot": "keyword:guarded",
        "expression": "True",
        "ast_sha256": sha256(b"Constant(value=True)").hexdigest(),
    }
    control_call.update(
        {"arguments": [control_argument], "arguments_complete": True}
    )
    control_receipt = {
        "verification_method": "pytest_ast_controlled_difference_v2",
        "hypothesis_id": "h1",
        "support_experiment_id": "exp-1",
        "control_experiment_id": "exp-control",
        "support_selection_id": "h1:exp-1",
        "control_selection_id": "h1:exp-control",
        "mechanism_symbols": ["core.run"],
        "shared_verified_mechanism_symbols": ["core.run"],
        "same_test_file": True,
        "controlled_input_difference": {
            "verification_method": "python_ast_explicit_argument_delta_v1",
            "difference_count": 1,
            "difference": {
                "mechanism_symbol": "core.run",
                "slot": "keyword:guarded",
                "difference_kind": "added_in_control",
                "support_argument": None,
                "control_argument": control_argument,
            },
        },
        "observable_difference": {
            "verification_method": "runner_replay_complement_v1",
            "source": "exit_code",
            "difference_kind": "failing_exit_to_zero",
            "expected_sha256": None,
            "support": {
                "exit_code": 1,
                "observed_sha256": _canonical_sha256(1),
                "stdout_sha256": "f" * 64,
                "stderr_sha256": "1" * 64,
            },
            "control": {
                "exit_code": 0,
                "observed_sha256": _canonical_sha256(0),
                "stdout_sha256": "f" * 64,
                "stderr_sha256": "1" * 64,
            },
        },
        "adversarial_effect": "limits_scope",
        "relationship_sha256": _canonical_sha256(
            {
                "controlled_variable": "guard enabled",
                "expected_difference": "The guarded control succeeds without the symptom.",
                "mechanism_symbols": ["core.run"],
            }
        ),
    }
    control_receipt["control_verification_id"] = (
        "control_verification:" + _canonical_sha256(control_receipt)
    )
    verification["control_verifications"] = [control_receipt]
    production_consumer = {
        "kind": "production_entrypoint",
        "entrypoint": "core.run",
    }
    mechanism_evidence = {
        "evidence_type": "controlled_scenario",
        "hypothesis_id": "h1",
        "mechanism_symbols": ["core.run"],
        "code_paths": [{"symbol": "core.run", "path": "src/core.py"}],
        "experiment_ids": ["exp-1", "exp-control"],
        "artifact_refs": ["artifact:repro", "artifact:source"],
        "origin_atom_ids": ["atom:test"],
        "path_name": "core.run",
        "consumer_identity": production_consumer,
        "independence_key": _canonical_sha256(production_consumer),
        "controlled_condition": {
            "variable": "guarded",
            "expected_difference": "The guarded control removes the failure.",
        },
        "observable_difference": control_receipt["observable_difference"],
        "strong_pytest_control_id": control_receipt["control_verification_id"],
        "adversarial_effect": "limits_scope",
    }
    mechanism_evidence["mechanism_evidence_id"] = (
        "mechanism_evidence:" + _canonical_sha256(mechanism_evidence)
    )
    challenge_evidence = {
        "evidence_type": "exception_trace",
        "hypothesis_id": "h1",
        "mechanism_symbols": ["core.run"],
        "code_paths": [{"symbol": "core.run", "path": "src/core.py"}],
        "experiment_ids": ["exp-challenge"],
        "artifact_refs": ["artifact:repro", "artifact:source"],
        "origin_atom_ids": ["atom:test"],
        "path_name": "core.run",
        "consumer_identity": production_consumer,
        "independence_key": _canonical_sha256(production_consumer),
        "observed_result": {
            "exit_code": 1,
            "stdout_sha256": "f" * 64,
            "stderr_sha256": "1" * 64,
            "assertion": {
                "source": "exit_code",
                "operator": "equals",
                "expected": 1,
            },
        },
        "harness_path": None,
        "mechanism_link": {
            "verification_method": "runner_exception_symbol_trace_v1",
            "entrypoint": "core.run",
            "code_path": [
                {
                    "symbol": "core.run",
                    "path": "src/core.py",
                    "trace_excerpt_sha256": "8" * 64,
                }
            ],
        },
        "platform_requirement": "any",
        "observed_platform": "windows",
        "adversarial_effect": "supports_selection",
    }
    challenge_evidence["mechanism_evidence_id"] = (
        "mechanism_evidence:" + _canonical_sha256(challenge_evidence)
    )
    verification["mechanism_evidence"] = [mechanism_evidence, challenge_evidence]
    replay_by_id = {
        replay["experiment_id"]: replay for replay in verification["experiments"]
    }
    alternative_argument = {
        "slot": "keyword:alternative",
        "expression": "False",
        "ast_sha256": sha256(b"Constant(value=False)").hexdigest(),
    }
    intervention = {
        "verification_method": "pytest_ast_falsification_intervention_v1",
        "hypothesis_id": "h1",
        "attempt_id": "falsify-h1-alternative",
        "baseline_experiment_id": "exp-1",
        "challenge_experiment_id": "exp-challenge",
        "mechanism_symbols": ["core.run"],
        "baseline_selection_id": "h1:exp-1",
        "challenge_selection_id": "h1:exp-challenge",
        "controlled_input_difference": {
            "verification_method": "python_ast_explicit_argument_delta_v1",
            "difference_count": 1,
            "difference": {
                "mechanism_symbol": "core.run",
                "slot": "keyword:alternative",
                "difference_kind": "added_in_control",
                "support_argument": None,
                "control_argument": alternative_argument,
            },
        },
        "observed_polarity": {
            "verification_method": "runner_replay_falsification_polarity_v1",
            "polarity": "failure_persists_after_intervention",
            "baseline": {
                "exit_code": replay_by_id["exp-1"]["exit_code"],
                "stdout_sha256": replay_by_id["exp-1"]["stdout_sha256"],
                "stderr_sha256": replay_by_id["exp-1"]["stderr_sha256"],
            },
            "challenge": {
                "exit_code": replay_by_id["exp-challenge"]["exit_code"],
                "stdout_sha256": replay_by_id["exp-challenge"]["stdout_sha256"],
                "stderr_sha256": replay_by_id["exp-challenge"]["stderr_sha256"],
            },
        },
        "relationship_sha256": _canonical_sha256(
            {
                "controlled_variable": "the strongest alternative cause",
                "expected_difference": (
                    "The failure disappears only if the alternative is causal."
                ),
                "mechanism_symbols": ["core.run"],
            }
        ),
    }
    intervention["intervention_receipt_id"] = (
        "falsification_intervention:" + _canonical_sha256(intervention)
    )
    verification["falsification_interventions"] = [intervention]
    verification["deterministic_mechanism_closures"] = []
    verification["hypothesis_refs"][0]["falsification_attempts"] = [
        {
            "attempt_id": "falsify-h1-alternative",
            "hypothesis_id": "h1",
            "claim": "Missing guard",
            "baseline_experiment_id": "exp-1",
            "challenge_experiment_id": "exp-challenge",
            "disproof_condition": {
                "source": "exit_code",
                "operator": "equals",
                "expected": 0,
            },
            "outcome": "survived",
            "scenario_kind": "control",
            "command": proof["experiments"][2]["command"],
            "declared_result": proof["experiments"][2]["result"],
            "observable_assertion": proof["experiments"][2][
                "observable_assertion"
            ],
            "exit_code": replay_by_id["exp-challenge"]["exit_code"],
            "stdout_sha256": replay_by_id["exp-challenge"]["stdout_sha256"],
            "stderr_sha256": replay_by_id["exp-challenge"]["stderr_sha256"],
            "mechanism_evidence_ids": [
                challenge_evidence["mechanism_evidence_id"]
            ],
            "intervention_receipt_id": intervention["intervention_receipt_id"],
        }
    ]
    verification["verified_mechanism"] = {
        "schema_version": 2,
        "mechanism_symbols": ["core.run"],
        "code_paths": [{"symbol": "core.run", "path": "src/core.py"}],
    }
    probe_points = [
        {
            "verification_method": receipt["controlled_input_difference"][
                "verification_method"
            ],
            "mechanism_symbols": ["core.run"],
            "slot": receipt["controlled_input_difference"]["difference"]["slot"],
            "mechanism_symbol": "core.run",
        }
        for receipt in (control_receipt, intervention)
    ]
    verification["verified_mechanism_provenance"] = {
        "schema_version": 1,
        "primary_hypothesis_id": "h1",
        "mechanism_evidence_ids": sorted(
            [
                mechanism_evidence["mechanism_evidence_id"],
                challenge_evidence["mechanism_evidence_id"],
            ]
        ),
        "causal_control_ids": [control_receipt["control_verification_id"]],
        "falsification_intervention_ids": [
            intervention["intervention_receipt_id"]
        ],
        "deterministic_closure_ids": [],
        "research_probe_control_points": sorted(
            probe_points,
            key=lambda value: json.dumps(value, sort_keys=True, separators=(",", ":")),
        ),
    }
    verification["verified_mechanism_sha256"] = _canonical_sha256(
        verification["verified_mechanism"]
    )
    verification["verified_mechanism_provenance_sha256"] = _canonical_sha256(
        verification["verified_mechanism_provenance"]
    )
    oracle = {
        "schema_version": 1,
        "case_id": proof["case_id"],
        "repo_revision": proof["repo_revision"],
        "research_experiment_id": "exp-1",
        "scenario_kind": "original_replay",
        "origin_atom_ids": ["atom:test"],
        "mechanism_evidence_ids": [mechanism_evidence["mechanism_evidence_id"]],
        "baseline": {
            "exit_code": 1,
            "observable_assertion": proof["experiments"][0][
                "observable_assertion"
            ],
            "stdout_sha256": replay_by_id["exp-1"]["stdout_sha256"],
            "stderr_sha256": replay_by_id["exp-1"]["stderr_sha256"],
        },
        "kind": "staged_replay",
        "proof_scope": "behavioral",
        "execution": {
            "argv": replay_by_id["exp-1"]["executed_argv"],
            "command_authorization": {
                "authorization_kind": "standard_test_or_research_harness",
                "executed_argv_sha256": _canonical_sha256(
                    replay_by_id["exp-1"]["executed_argv"]
                ),
                "shell": False,
                "workspace_confined": True,
            },
            "platform_requirement": "any",
            "shell": False,
        },
        "asset": None,
    }
    positive_contract = {
        "schema_version": 1,
        "kind": "origin_evidence_semantic_contract",
        "research_experiment_id": "exp-1",
        "mechanism_evidence_ids": [mechanism_evidence["mechanism_evidence_id"]],
        "origin_evidence": {
            "atom_id": "atom:test",
            "atom_sha256": assignment["atom_receipts"][0]["atom_sha256"],
            "field_path": "$.expected_output",
            "value_sha256": _canonical_sha256("guard applied"),
        },
        "postconditions": [
            {"type": "command_exit_code", "command_index": 0, "equals": 0},
            {
                "type": "command_stdout_contains",
                "command_index": 0,
                "value": "guard applied",
            },
        ],
    }
    positive_contract["positive_outcome_contract_id"] = (
        "positive_outcome_contract:" + _canonical_sha256(positive_contract)
    )
    oracle["positive_outcome_contracts"] = [positive_contract]
    oracle["outcome_oracle_id"] = "outcome_oracle:" + _canonical_sha256(oracle)
    verification["outcome_oracles"] = [oracle]
    selector_consumer = {
        "kind": "evidence_selector",
        "entrypoint": "tests/test_core.py::test_reported_failure",
    }
    failure_path = {
        "verification_method": "runner_controlled_failure_path_v1",
        "path_name": selector_consumer["entrypoint"],
        "consumer_identity": selector_consumer,
        "independence_key": _canonical_sha256(selector_consumer),
        "hypothesis_id": "h1",
        "support_experiment_id": "exp-1",
        "support_selection_id": "h1:exp-1",
        "control_verification_id": control_receipt["control_verification_id"],
        "mechanism_symbols": ["core.run"],
        "origin_atom_ids": ["atom:test"],
        "observed_failure": {
            "source": "exit_code",
            "difference_kind": "failing_exit_to_zero",
            **control_receipt["observable_difference"]["support"],
        },
    }
    failure_path["failure_path_id"] = "failure_path:" + _canonical_sha256(failure_path)
    verification["failure_paths"] = [failure_path]
    verification["receipt_sha256"] = evidence_verification_sha256(verification)
    proof["evidence_verification"] = verification
    return proof


def _strict_ticket(*, kinds: list[str], breadth: dict[str, int]) -> dict[str, object]:
    research = _research_proof()
    verification = research["evidence_verification"]
    assert isinstance(verification, dict)
    failure_path = verification["failure_paths"][0]
    mechanism_evidence = verification["mechanism_evidence"][0]
    positive_contract = verification["outcome_oracles"][0][
        "positive_outcome_contracts"
    ][0]
    option = {
        "case_id": "case:test",
        "option_id": "option:test:direct",
        "problem_id": "problem:test",
        "family_id": "most_direct",
        "summary": "Apply the reproduced guard",
        "tradeoffs": "Local change",
        "recurrence_prevention": "Focused regression",
        "change_surface_hypothesis": "Existing behavior",
        "test_implications": "Replay exp-1",
        "rationale": "Matches the mechanism",
        "causal_coverage": {
            "mechanism_addressed": "Missing guard",
            "research_binding": {
                "hypothesis_id": "h1",
                "hypothesis_statement": "Missing guard",
                "mechanism_symbols": ["core.run"],
                "supporting_evidence_refs": ["exp-1", "exp-challenge"],
                "counterevidence_refs": ["exp-control"],
                "falsification_attempt_refs": ["falsify-h1-alternative"],
                "deterministic_closure_refs": [],
                "intervention_points": [
                    {
                        "mechanism_symbol": "core.run",
                        "target_path": "src/core.py",
                        "target_symbol": "core.run",
                        "intervention": "Apply the guard at the verified failing symbol.",
                    }
                ],
            },
            "symptoms_covered": ["Failure"],
            "unsupported_assumptions": [],
            "residual_recurrence_paths": [],
            "compatibility_risks": [],
            "testability": {"before": "fails", "after": "passes"},
        },
        "scope_evidence": {
            "scope_level": "single_path",
            "independent_consumers_or_failure_paths": [
                {
                    "name": failure_path["path_name"],
                    "evidence_refs": [failure_path["failure_path_id"]],
                }
            ],
        },
    }
    selection = {
        "case_id": "case:test",
        "problem_id": "problem:test",
        "selected_option_id": "option:test:direct",
        "selected_family_id": "most_direct",
        "selection_rationale": "Causal fit",
        "repo_intent_alignment": "Uses existing surface",
        "why_other_options_were_not_selected": "No broader mechanism is supported",
        "needs_ux_review": False,
        "causal_coverage_evaluation": {
            "mechanism_fit": "Direct",
            "accepted_unsupported_assumptions": [],
            "accepted_residual_risks": [],
            "class_level_evidence_sufficient": False,
        },
        "falsification_review": {
            "problem_id": "problem:test",
            "selected_option_id": "option:test:direct",
            "verdict": "accept",
            "strongest_counterargument": "Alternative parser path",
            "evidence_refs": [
                {
                    "ref": mechanism_evidence["mechanism_evidence_id"],
                    "finding": "The guard is absent",
                    "effect": "challenges_selection",
                }
            ],
            "unsupported_assumptions": [],
            "residual_risks": [],
            "evidence_that_would_change_verdict": "Contrary trace",
            "material_risk_dispositions": [],
            "critical_findings": [],
            "selected_positive_outcome_contract_id": positive_contract[
                "positive_outcome_contract_id"
            ],
            "outcome_contract_reviews": [
                {
                    "positive_outcome_contract_id": positive_contract[
                        "positive_outcome_contract_id"
                    ],
                    "verdict": "sufficient",
                    "semantic_relation_assessment": (
                        "The expected output proves the reproduced guard path completed."
                    ),
                    "proves_intended_operation": True,
                    "problem_coverage": "full",
                    "residual_untested_paths": [],
                    "evidence_refs": [mechanism_evidence["mechanism_evidence_id"]],
                }
            ],
        },
        "change_surface": {"user_visible": True, "kinds": kinds, "notes": "Grounded"},
    }
    selection["falsification_review"] = bind_falsification_review(
        selection["falsification_review"],
        problem_id="problem:test",
        selected_option=option,
        research=research,
    )
    plan = {
        "change_plan_id": "plan:test:1",
        "case_id": "case:test",
        "problem_id": "problem:test",
        "selected_option_id": "option:test:direct",
        "title": "Grounded change",
        "problem": "Missing guard",
        "user_impact": "Command fails",
        "proposed_fix": "Apply guard",
        "implementation_steps": ["Update `src/core.py` at `run`."],
        "verification_steps": ["Replay regression."],
        "success_criteria": ["Scenario passes."],
        "verification_commands": [
            "pytest -q tests/test_core.py::test_reported_failure"
        ],
        "outcome_verification_roles": {
            "original_scenario": {
                "description": "Replay the exact reported failure after the change.",
                "research_experiment_id": "exp-1",
                "commands": ["pytest -q tests/test_core.py::test_reported_failure"],
                "predicates": [
                    {"type": "command_exit_code", "command_index": 0, "equals": 0},
                    {
                        "type": "command_stdout_contains",
                        "command_index": 0,
                        "value": "guard applied",
                    },
                ],
            },
            "live": None,
            "mitigation_effect": None,
            "recurrence": {
                "description": "Inspect fresh same-class recurrence evidence.",
                "commands": ["python tools/recurrence_probe.py"],
                "predicates": [
                    {"type": "command_exit_code", "command_index": 0, "equals": 0}
                ],
            },
        },
        "change_targets": [
            {
                "action": "modify",
                "path": "src/core.py",
                "symbols": ["core.run"],
                "change": "Apply the guard at the verified failing symbol.",
            }
        ],
        "before_after_reproduction": {
            "original_scenario": "Run command",
            "research_experiment_id": "exp-1",
            "before_change": {
                "command": "pytest -q tests/test_core.py::test_reported_failure",
                "expected_exit_code": 1,
                "expected_result": "fails",
                "observable_assertion": {
                    "source": "exit_code",
                    "operator": "equals",
                    "expected": 1,
                },
            },
            "after_change": {
                "command": "pytest -q tests/test_core.py::test_reported_failure",
                "expected_exit_code": 0,
                "expected_result": "passes",
                "observable_assertions": [
                    {
                        "source": "exit_code",
                        "operator": "equals",
                        "expected": 0,
                    },
                    {
                        "source": "stdout",
                        "operator": "contains",
                        "expected": "guard applied",
                    },
                ],
            },
            "expected_outcome_state": "resolved",
            "proof_limitation": None,
            "alternate_verification": None,
        },
        "compatibility_and_failure_modes": {
            "preserved_behaviors": ["Valid calls pass"],
            "intentional_changes": [],
            "failure_modes": ["Invalid calls fail"],
            "migration_required": False,
        },
        "causal_coverage": option["causal_coverage"],
        "scope_evidence": option["scope_evidence"],
        "requires_live_verification": False,
        "live_verification_rationale": (
            "The retained proof is a repository-local controlled test with no live boundary."
        ),
        "rollback_notes": "Revert",
        "suggested_owner": "core",
        "repo_revision": "abc123",
    }
    plan["target_contract"] = {
        "case_id": plan["case_id"],
        "problem_id": plan["problem_id"],
        "selected_option_id": plan["selected_option_id"],
        "repo_revision": plan["repo_revision"],
        "targets": [dict(target) for target in plan["change_targets"]],
    }
    plan = bind_plan_outcome_oracle(plan, research=research)
    plan = assign_plan_revision_id(plan)
    return {
        "title": "Grounded change",
        "stage": "triage",
        "risks": [],
        "investigation_steps": [],
        "problem_record": {
            "case_id": "case:test",
            "canonical_problem_id": "problem:test",
            "case_member_problem_ids": ["problem:test"],
            "problem_id": "problem:test",
            "title": "Grounded change",
            "problem": "Missing guard",
            "user_impact": "Command fails",
            "evidence_summary": "exp-1",
        },
        "research": research,
        "priority": {
            "case_id": "case:test",
            "problem_id": "problem:test",
            "selected_for_research": True,
            "priority_bucket": "p1",
            "priority_rationale": "The mechanism has user impact.",
            "priority_status": "prioritized",
        },
        "solution_options": [option],
        "selected_solution": selection,
        "change_plan": plan,
        "change_surface": selection["change_surface"],
        "breadth": breadth,
    }


def _legacy_policy() -> BacklogPolicyConfig:
    return BacklogPolicyConfig.from_dict(
        {
            "surface_area_high": [
                "new_command",
                "breaking_change",
                "new_top_level_mode",
                "new_config_schema",
                "new_api",
            ],
            "breadth_min_for_surface_area_high": {"missions": 2, "targets": 2, "repo_inputs": 2},
            "default_stage_for_high_surface_low_breadth": "research_required",
            "default_stage_for_labeled": "ready_for_ticket",
        }
    )


def _grouped_policy() -> BacklogPolicyConfig:
    return BacklogPolicyConfig.from_dict(
        {
            "default_stage_for_labeled": "ready_for_ticket",
            "high_surface_rules": [
                {
                    "rule_id": "command_surface",
                    "applies_to_kinds": [
                        "new_command",
                        "new_top_level_mode",
                        "new_config_schema",
                    ],
                    "breadth_min": {"missions": 2, "targets": 2, "repo_inputs": 2},
                    "default_stage_for_low_breadth": "research_required",
                    "investigation_steps": [
                        "Validate repo intent",
                        "Check if existing commands or flags can be parameterized",
                    ],
                    "risk_tag": "overfitting_risk",
                    "review_domain": "command_surface",
                },
                {
                    "rule_id": "behavior_compat",
                    "applies_to_kinds": ["breaking_change", "new_api"],
                    "breadth_min": {"runs": 5, "agents": 2},
                    "default_stage_for_low_breadth": "research_required",
                    "investigation_steps": [
                        "Validate recurrence breadth across runs and agents",
                        "Check compatibility impact within existing surfaces",
                    ],
                    "risk_tag": "compatibility_risk",
                    "review_domain": "behavior_compat",
                },
            ],
        }
    )


def test_policy_legacy_flat_config_still_parses_and_exposes_surface_area() -> None:
    cfg = _legacy_policy()

    assert {
        "new_command",
        "breaking_change",
        "new_top_level_mode",
        "new_config_schema",
        "new_api",
    } == set(cfg.surface_area_high)
    assert cfg.breadth_min_for_surface_area_high == {"missions": 2, "targets": 2, "repo_inputs": 2}
    assert cfg.default_stage_for_high_surface_low_breadth == "research_required"


def test_policy_high_surface_low_breadth_routes_to_research_required() -> None:
    cfg = _legacy_policy()
    ticket = {
        "title": "Add a new top-level command for onboarding",
        "stage": "triage",
        "risks": [],
        "investigation_steps": [],
        "change_surface": {
            "user_visible": True,
            "kinds": ["new_command"],
            "notes": "Adds a new command.",
        },
        "breadth": {"runs": 1, "missions": 1, "targets": 1, "repo_inputs": 1, "agents": 1},
    }

    updated, meta = apply_backlog_policy([ticket], config=cfg)
    assert meta["tickets_total"] == 1
    assert updated[0]["stage"] == "research_required"
    assert "overfitting_risk" in updated[0]["risks"]
    assert "Validate repo intent" in updated[0]["investigation_steps"]


def test_policy_docs_change_requires_plan_before_ready() -> None:
    cfg = _legacy_policy()
    ticket = {
        "title": "Fix quickstart docs",
        "stage": "triage",
        "risks": [],
        "investigation_steps": [],
        "change_surface": {"user_visible": True, "kinds": ["docs_change"], "notes": "Docs only."},
        "breadth": {"runs": 1, "missions": 1, "targets": 1, "repo_inputs": 1, "agents": 1},
    }

    updated, _ = apply_backlog_policy([ticket], config=cfg)
    assert updated[0]["stage"] == "research_required"
    assert "overfitting_risk" not in updated[0]["risks"]


def test_policy_ready_ticket_without_strict_research_proof_is_downgraded() -> None:
    cfg = _legacy_policy()
    ticket = {
        "title": "Planned but under-researched change",
        "stage": "ready_for_ticket",
        "risks": [],
        "investigation_steps": [],
        "selected_option_id": "option:test:most_direct",
        "change_plan_id": "plan:test:1",
        "research": {
            "problem_id": "problem:legacy",
            "reproduction_status": "partial",
            "research_status": "researched",
        },
        "change_surface": {"kinds": ["docs_change"]},
        "breadth": {},
    }

    updated, _ = apply_backlog_policy([ticket], config=cfg)

    assert updated[0]["stage"] == "research_required"
    assert "research_evidence_incomplete" in updated[0]["risks"]


def test_policy_high_surface_high_breadth_can_be_ready() -> None:
    cfg = _legacy_policy()
    ticket = _strict_ticket(
        kinds=["new_command"],
        breadth={"runs": 6, "missions": 4, "targets": 2, "repo_inputs": 2, "agents": 3},
    )

    updated, _ = apply_backlog_policy([ticket], config=cfg)
    assert updated[0]["stage"] == "ready_for_ticket"
    assert "overfitting_risk" not in updated[0]["risks"]


def test_policy_grouped_rule_behavior_compat_can_pass_with_internal_observation_breadth() -> None:
    cfg = _grouped_policy()
    ticket = _strict_ticket(
        kinds=["breaking_change"],
        breadth={"runs": 6, "missions": 1, "targets": 1, "repo_inputs": 1, "agents": 2},
    )

    updated, meta = apply_backlog_policy([ticket], config=cfg)
    assert updated[0]["stage"] == "ready_for_ticket"
    assert "compatibility_risk" not in updated[0]["risks"]
    assert meta["rules_matched"]["behavior_compat"] == 1


def test_policy_grouped_rule_command_surface_still_requires_cross_context_breadth() -> None:
    cfg = _grouped_policy()
    ticket = {
        "title": "Add a new top-level shortcut",
        "stage": "triage",
        "risks": [],
        "investigation_steps": [],
        "selected_option_id": "option:test:most_comprehensive",
        "change_plan_id": "plan:test:1",
        "research": _research_proof(),
        "change_surface": {
            "user_visible": True,
            "kinds": ["new_command"],
            "notes": "New command surface.",
        },
        "breadth": {"runs": 12, "missions": 1, "targets": 1, "repo_inputs": 1, "agents": 3},
    }

    updated, _ = apply_backlog_policy([ticket], config=cfg)
    assert updated[0]["stage"] == "research_required"
    assert "overfitting_risk" in updated[0]["risks"]


def test_policy_grouped_rule_mixed_kinds_require_all_matching_rules_to_pass() -> None:
    cfg = _grouped_policy()
    ticket = {
        "title": "Add new command and tighten behavior",
        "stage": "triage",
        "risks": [],
        "investigation_steps": [],
        "selected_option_id": "option:test:most_comprehensive",
        "change_plan_id": "plan:test:1",
        "research": _research_proof(),
        "change_surface": {
            "user_visible": True,
            "kinds": ["new_command", "breaking_change"],
            "notes": "Mixed command-surface plus behavior-compat change.",
        },
        "breadth": {"runs": 9, "missions": 1, "targets": 1, "repo_inputs": 1, "agents": 2},
    }

    updated, _ = apply_backlog_policy([ticket], config=cfg)
    assert updated[0]["stage"] == "research_required"
    assert "overfitting_risk" in updated[0]["risks"]
    assert "compatibility_risk" not in updated[0]["risks"]


def test_policy_module_avoids_regex_gating_guardrail() -> None:
    import backlog_core.backlog_policy as mod

    path = Path(mod.__file__).resolve()
    text = path.read_text(encoding="utf-8")
    assert "re.compile(" not in text
    assert "\nimport re\n" not in text
