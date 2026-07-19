from __future__ import annotations

import json
from hashlib import sha256
from typing import Any

import pytest

from backlog_core.backlog_ticket_assembly import assemble_backlog_tickets
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


def _attach_exact_origin_boundary(
    *,
    proof: dict[str, object],
    verification: dict[str, object],
    oracle: dict[str, object],
    positive_contract: dict[str, object],
    mechanism_evidence_ids: list[str],
    atom_id: str,
) -> None:
    experiment_id = str(oracle["research_experiment_id"])
    replay = next(
        value
        for value in verification["experiments"]
        if value["experiment_id"] == experiment_id
    )
    atom_receipt = next(
        value
        for value in proof["evidence_assignment"]["atom_receipts"]
        if value["atom_id"] == atom_id
    )
    atom_snapshot = atom_receipt["atom_snapshot"]
    command = str(atom_snapshot["command"])
    argv = list(replay["executed_argv"])
    authorization = {
        "authorization_kind": "fixture_exact_origin_command",
        "executed_argv_sha256": _canonical_sha256(argv),
        "shell": False,
        "workspace_confined": True,
        "origin_atom_id": atom_id,
        "origin_atom_sha256": atom_receipt["atom_sha256"],
        "origin_atom_field_path": "$.command",
        "origin_command_value_sha256": _canonical_sha256(command),
        "runner_attested": True,
    }
    authorization["authorization_sha256"] = _canonical_sha256(authorization)
    replay_inputs = {
        "schema_version": 1,
        "source_experiment_id": experiment_id,
        "environment": {},
        "disposable_state_paths": [],
        "runner_approved": True,
    }
    replay_inputs["replay_inputs_sha256"] = _canonical_sha256(replay_inputs)
    contract_id = str(positive_contract["positive_outcome_contract_id"])
    replay_observation = {
        "schema_version": 1,
        "source_experiment_id": experiment_id,
        "selector": {"source": "exit_code"},
        "source_observation_sha256": _canonical_sha256(
            {
                "exit_code": replay["exit_code"],
                "stdout_sha256": replay["stdout_sha256"],
                "stderr_sha256": replay["stderr_sha256"],
            }
        ),
        "predicate_input_mode": "post_change_observation",
        "positive_outcome_contract_ids": [contract_id],
        "runner_attested": True,
    }
    replay_observation["replay_observation_sha256"] = _canonical_sha256(
        replay_observation
    )
    replay["command_authorization"] = authorization
    replay["replay_inputs"] = replay_inputs
    oracle["execution"] = {
        "argv": argv,
        "command_authorization": authorization,
        "platform_requirement": "any",
        "shell": False,
        "replay_inputs": replay_inputs,
        "replay_observation": replay_observation,
    }
    oracle["outcome_oracle_id"] = "outcome_oracle:" + _canonical_sha256(oracle)
    source_identity = {
        "schema_version": 1,
        "origin_atom_id": atom_id,
        "origin_atom_sha256": atom_receipt["atom_sha256"],
        "origin_atom_field_path": "$.command",
        "origin_command_value_sha256": _canonical_sha256(command),
        "executed_argv_sha256": authorization["executed_argv_sha256"],
        "command_authorization_sha256": authorization["authorization_sha256"],
        "runner_attested": True,
    }
    source_identity["source_identity_sha256"] = _canonical_sha256(source_identity)
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
    equivalence["equivalence_sha256"] = _canonical_sha256(equivalence)
    replay_projection = {
        "experiment_id": experiment_id,
        "executed_argv_sha256": authorization["executed_argv_sha256"],
        "command_authorization_sha256": authorization["authorization_sha256"],
        "stdout_sha256": replay["stdout_sha256"],
        "stderr_sha256": replay["stderr_sha256"],
        "replay_inputs_sha256": replay_inputs["replay_inputs_sha256"],
        "execution_isolation_sha256": _canonical_sha256(replay["execution_isolation"]),
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
                f"clean_replay:{_canonical_sha256(replay_projection)}",
                *mechanism_evidence_ids,
                str(oracle["outcome_oracle_id"]),
                f"equivalence_proof:{equivalence['equivalence_sha256']}",
            }
        ),
        "rationale_sha256": _canonical_sha256("exact original fixture identity"),
        "runner_attested": True,
        "equivalence_proof": equivalence,
    }
    boundary["boundary_sha256"] = _canonical_sha256(boundary)
    verification["verification_boundaries"] = [boundary]


def _problem_record(pid: str, *, title: str = "T") -> dict[str, Any]:
    return {
        "case_id": f"case:{pid.removeprefix('problem:')}",
        "canonical_problem_id": pid,
        "case_member_problem_ids": [pid],
        "problem_id": pid,
        "title": title,
        "problem": "P",
        "user_impact": "U",
        "severity": "medium",
        "confidence": 0.5,
        "evidence_atom_ids": ["a1", "a2"],
        "evidence_summary": "E",
    }


def _option(pid: str) -> dict[str, Any]:
    research = _research_proof(pid)
    verification = research["evidence_verification"]
    failure_path = verification["failure_paths"][0]
    return {
        "case_id": f"case:{pid.removeprefix('problem:')}",
        "option_id": "option:one:direct",
        "problem_id": pid,
        "family_id": "most_direct",
        "summary": "Apply the established guard at the failing path",
        "tradeoffs": "Keeps the change local",
        "recurrence_prevention": "The failing path is covered by a regression test",
        "change_surface_hypothesis": "Internal behavior change",
        "test_implications": "Replay the failing scenario",
        "rationale": "The reproduced mechanism is local",
        "causal_coverage": {
            "mechanism_addressed": "Missing guard",
            "research_binding": {
                "hypothesis_id": "h1",
                "hypothesis_statement": "The guard is missing",
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
            "symptoms_covered": ["Original failure"],
            "unsupported_assumptions": [],
            "residual_recurrence_paths": [],
            "compatibility_risks": [],
            "testability": {"before": "pytest fails", "after": "pytest passes"},
            "outcome_strategy": {
                "intended_operation": (
                    "The guarded original scenario completes and reports `guard applied`."
                ),
                "success_properties": [
                    "The unchanged original replay exits successfully.",
                    "The replay reports the origin-bound `guard applied` result.",
                ],
                "safety_constraints": ["The guarded control remains successful."],
                "post_change_replay_mode": "verified_fail_first",
                "original_scenario_experiment_ids": ["exp-1"],
            },
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


def _selection(pid: str) -> dict[str, Any]:
    research = _research_proof(pid)
    option = _option(pid)
    verification = research["evidence_verification"]
    positive_contract = verification["outcome_oracles"][0]["positive_outcome_contracts"][0]
    selection = {
        "case_id": f"case:{pid.removeprefix('problem:')}",
        "problem_id": pid,
        "selected_option_id": "option:one:direct",
        "selected_family_id": "most_direct",
        "selection_rationale": "Best causal fit",
        "repo_intent_alignment": "Preserves existing surface",
        "why_other_options_were_not_selected": "No other mechanism was needed",
        "needs_ux_review": False,
        "causal_coverage_evaluation": {
            "mechanism_fit": "Directly addresses missing guard",
            "accepted_unsupported_assumptions": [],
            "accepted_residual_risks": [],
            "class_level_evidence_sufficient": False,
        },
        "falsification_review": {
            "problem_id": pid,
            "selected_option_id": "option:one:direct",
            "verdict": "accept",
            "strongest_counterargument": "The guard might be downstream",
            "evidence_refs": [
                {
                    "ref": verification["mechanism_evidence"][0]["mechanism_evidence_id"],
                    "finding": "The local guard is absent",
                    "effect": "challenges_selection",
                }
            ],
            "unsupported_assumptions": [],
            "residual_risks": [],
            "evidence_that_would_change_verdict": "A trace showing an upstream failure",
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
                    "evidence_refs": [
                        verification["mechanism_evidence"][0]["mechanism_evidence_id"]
                    ],
                }
            ],
            "outcome_strategy_review": {
                "verdict": "sufficient",
                "semantic_relation_assessment": (
                    "The unchanged fail-first replay must both exit successfully and emit "
                    "the origin-bound result, proving the selected guard restores the "
                    "intended operation rather than merely hiding the failure."
                ),
                "proves_intended_operation": True,
                "problem_coverage": "full",
                "residual_untested_paths": [],
                "evidence_refs": [
                    verification["mechanism_evidence"][0]["mechanism_evidence_id"]
                ],
            },
        },
        "change_surface": {"user_visible": True, "kinds": ["docs_change"], "notes": "n"},
        "component": "docs",
        "intent_risk": "low",
        "labeler_confidence": 0.7,
        "breadth": {"runs": 2},
    }
    selection["falsification_review"] = bind_falsification_review(
        selection["falsification_review"],
        problem_id=pid,
        selected_option=option,
        research=research,
    )
    return selection


def _plan(pid: str, number: int) -> dict[str, Any]:
    option = _option(pid)
    plan = {
        "change_plan_id": f"plan:one:{number}",
        "case_id": "case:one",
        "problem_id": pid,
        "selected_option_id": "option:one:direct",
        "title": f"Plan {number}",
        "problem": "P",
        "user_impact": "U",
        "proposed_fix": "Apply the guard",
        "implementation_steps": ["Update `src/core.py` at `run` to apply the guard."],
        "verification_steps": ["Replay the focused regression."],
        "success_criteria": ["The original failure passes."],
        "rollback_notes": "Revert the guard.",
        "suggested_owner": "docs",
        "repo_revision": "abc123",
        "change_targets": [
            {
                "action": "modify",
                "path": "src/core.py",
                "symbols": ["core.run"],
                "change": "Apply the guard at the verified failing symbol.",
            }
        ],
        "verification_commands": ["pytest -q tests/test_core.py::test_reported_failure"],
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
                "verification_owner": "centralized_case_refresh",
                "commands": [],
                "predicates": [],
            },
        },
        "before_after_reproduction": {
            "original_scenario": "Invoke the unguarded path",
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
            "preserved_behaviors": ["Guarded calls still pass"],
            "intentional_changes": [],
            "failure_modes": ["Malformed input remains rejected"],
            "migration_required": False,
        },
        "causal_coverage": option["causal_coverage"],
        "scope_evidence": option["scope_evidence"],
        "requires_live_verification": False,
        "live_verification_rationale": (
            "The retained proof is a repository-local controlled test with no live boundary."
        ),
        "change_plan_status": "planned",
        "related_change_plan_ids": [f"plan:one:{3 - number}"],
    }
    plan["target_contract"] = {
        "case_id": plan["case_id"],
        "problem_id": plan["problem_id"],
        "selected_option_id": plan["selected_option_id"],
        "repo_revision": plan["repo_revision"],
        "targets": [dict(target) for target in plan["change_targets"]],
    }
    plan = bind_plan_outcome_oracle(
        plan,
        research=_research_proof(pid),
        selection=_selection(pid),
    )
    return assign_plan_revision_id(plan)


def _research_proof(pid: str, **overrides: object) -> dict[str, Any]:
    proof: dict[str, Any] = {
        "research_schema_version": 3,
        "case_id": f"case:{pid.removeprefix('problem:')}",
        "problem_id": pid,
        "repo_revision": "abc123",
        "research_method": "reproduction",
        "reproduction_status": "reproduced",
        "research_status": "evidence_sufficient",
        "case_relation_assessment": {
            "disposition": "retain",
            "rationale": (
                "The signed occurrences and reproduced guard mechanism remain one "
                "causal work unit."
            ),
            "facets": [],
            "material_unknowns": [],
        },
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
                "addresses_atom_ids": ["a1", "a2"],
                "command": "pytest -q tests/test_core.py::test_reported_failure",
                "result": "Original scenario failed",
                "outcome": "supports",
                "exit_code": 1,
                "observable_assertion": {
                    "source": "exit_code",
                    "operator": "equals",
                    "expected": 1,
                },
                "origin_evidence_bindings": [
                    {
                        "atom_id": "a1",
                        "role": "expected_behavior",
                        "field_path": "$.expected_output",
                        "value": "guard applied",
                        "value_sha256": _canonical_sha256("guard applied"),
                    }
                ],
                "positive_outcome_contract": {
                    "contract_kind": "origin_atom_exact_value",
                    "atom_id": "a1",
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
                "addresses_atom_ids": ["a1", "a2"],
                "command": "pytest -q tests/test_core.py::test_guarded_control",
                "result": "The guarded control succeeds",
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
                "addresses_atom_ids": ["a1", "a2"],
                "command": "pytest -q tests/test_core.py::test_alternative_removed",
                "result": "The original failure remains",
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
                "statement": "The guard is missing",
                "supporting_evidence": ["exp-1", "exp-challenge"],
                "counterevidence": ["exp-control"],
                "falsification_attempts": [
                    {
                        "attempt_id": "falsify-h1-alternative",
                        "hypothesis_id": "h1",
                        "claim": "The guard is missing",
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
    proof.update(overrides)
    assignment: dict[str, Any] = {
        "status": "complete",
        "errors": [],
        "case_id": proof["case_id"],
        "problem_id": proof["problem_id"],
        "expected_atom_ids": ["a1", "a2"],
        "atom_receipts": [
            {
                "atom_id": atom_id,
                "atom_sha256": sha256(
                    json.dumps(
                        {
                            "atom_id": atom_id,
                            "text": "failure",
                            "command": ("pytest -q tests/test_core.py::test_reported_failure"),
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
                    "atom_id": atom_id,
                    "text": "failure",
                    "command": "pytest -q tests/test_core.py::test_reported_failure",
                    "exit_code": 1,
                    "evidence_role": "observation",
                    "origin_stage": "runtime",
                    "expected_output": "guard applied",
                },
                "artifact_receipts": [
                    {
                        "path": f"C:/runs/{atom_id}.json",
                        "sha256": "5" * 64,
                        "size_bytes": 7,
                    }
                ],
            }
            for atom_id in ("a1", "a2")
        ],
    }
    assignment["assignment_sha256"] = evidence_assignment_sha256(assignment)
    proof["evidence_assignment"] = assignment
    if "evidence_verification" not in overrides:
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
            "origin_atom_ids": ["a1", "a2"],
            "assignment_sha256": assignment["assignment_sha256"],
            "claims_sha256": research_claims_sha256(proof),
            "normalized_events_sha256": "a" * 64,
            "run_report_sha256": "e" * 64,
            "artifacts": [
                {
                    "artifact_id": artifact["artifact_id"],
                    "kind": artifact["kind"],
                    "path": artifact["path"],
                    "sha256": "b" * 64,
                    "size_bytes": 12,
                }
                for artifact in proof["artifact_refs"]
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
            ],
            "inspected_files": [
                {
                    "path": path,
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
                for path in proof["inspected_files"]
            ],
            "inspected_symbols": [
                {"symbol": symbol, "path": proof["inspected_files"][0]}
                for symbol in proof["inspected_symbols"]
            ],
            "hypothesis_refs": [
                {
                    "hypothesis_id": hypothesis["hypothesis_id"],
                    "supporting_refs": hypothesis["supporting_evidence"],
                    "counterevidence_refs": hypothesis["counterevidence"],
                    "mechanism_symbols": hypothesis["mechanism_symbols"],
                    "disposition": hypothesis["disposition"],
                    "disposition_evidence_refs": hypothesis["disposition_evidence"],
                    "control_links": [
                        {
                            "control_experiment_id": "exp-control",
                            "supports_experiment_id": "exp-1",
                            "mechanism_symbols": ["core.run"],
                            "shared_atom_ids": ["a1", "a2"],
                            "shared_artifact_refs": [
                                "artifact:repro",
                                "artifact:source",
                            ],
                            "controlled_variable": "guard enabled",
                            "expected_difference": (
                                "The guarded control succeeds without the symptom."
                            ),
                        }
                    ],
                }
                for hypothesis in proof["root_cause_hypotheses"]
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
                    "atom_id": atom_id,
                    "match_kind": "command_and_exit_code",
                    "origin_atom_sha256": next(
                        receipt["atom_sha256"]
                        for receipt in assignment["atom_receipts"]
                        if receipt["atom_id"] == atom_id
                    ),
                }
                for atom_id in ("a1", "a2")
            ],
            "errors": [],
        }
        verification["atom_bindings"].append(
            {
                "experiment_id": "exp-1",
                "atom_id": "a1",
                "binding_role": "expected_behavior",
                "match_kind": "explicit_field_binding",
                "origin_atom_sha256": assignment["atom_receipts"][0]["atom_sha256"],
                "origin_atom_field_path": "$.expected_output",
                "origin_atom_value_sha256": _canonical_sha256("guard applied"),
            }
        )
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
        control_call.update({"arguments": [control_argument], "arguments_complete": True})
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
                    "expected_difference": ("The guarded control succeeds without the symptom."),
                    "mechanism_symbols": ["core.run"],
                }
            ),
        }
        control_receipt["control_verification_id"] = "control_verification:" + _canonical_sha256(
            control_receipt
        )
        verification["control_verifications"] = [control_receipt]
        production_consumer_projection = {
            "kind": "runner_observed_entrypoint",
            "entrypoint": "core.run",
            "attestation_basis": "runner_mechanism_link",
            "runner_attested": True,
        }
        production_consumer = {
            **production_consumer_projection,
            "consumer_identity_sha256": _canonical_sha256(
                production_consumer_projection
            ),
        }
        mechanism_evidence = {
            "evidence_type": "controlled_scenario",
            "hypothesis_id": "h1",
            "mechanism_symbols": ["core.run"],
            "code_paths": [{"symbol": "core.run", "path": "src/core.py"}],
            "experiment_ids": ["exp-1", "exp-control"],
            "artifact_refs": ["artifact:repro", "artifact:source"],
            "origin_atom_ids": ["a1", "a2"],
            "origin_symptom_bindings": [
                {
                    "experiment_id": "exp-1",
                    "atom_id": atom_id,
                    "match_kind": "command_and_exit_code",
                    "origin_atom_sha256": next(
                        receipt["atom_sha256"]
                        for receipt in assignment["atom_receipts"]
                        if receipt["atom_id"] == atom_id
                    ),
                }
                for atom_id in ("a1", "a2")
            ],
            "path_name": "core.run",
            "consumer_identity": production_consumer,
            "independence_key": _canonical_sha256(production_consumer),
            "controlled_condition": {
                "variable": "guarded",
                "expected_difference": "The guarded control removes the failure.",
            },
            "observable_difference": control_receipt["observable_difference"],
            "strong_pytest_control_id": control_receipt["control_verification_id"],
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
            "adversarial_effect": "supports_selection",
        }
        mechanism_evidence["causal_root_bindings"] = [
            {
                "kind": "origin_symptom_observation",
                "experiment_ids": ["exp-1", "exp-control"],
                "origin_atom_ids": ["a1", "a2"],
                "origin_bindings_sha256": _canonical_sha256(
                    mechanism_evidence["origin_symptom_bindings"]
                ),
                "mechanism_link_sha256": _canonical_sha256(mechanism_evidence["mechanism_link"]),
                "root_mechanism_symbol": "core.run",
            }
        ]
        mechanism_evidence["mechanism_evidence_id"] = "mechanism_evidence:" + _canonical_sha256(
            mechanism_evidence
        )
        challenge_evidence = {
            "evidence_type": "exception_trace",
            "hypothesis_id": "h1",
            "mechanism_symbols": ["core.run"],
            "code_paths": [{"symbol": "core.run", "path": "src/core.py"}],
            "experiment_ids": ["exp-challenge"],
            "artifact_refs": ["artifact:repro", "artifact:source"],
            "origin_atom_ids": ["a1", "a2"],
            "origin_symptom_bindings": [],
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
        challenge_evidence["mechanism_evidence_id"] = "mechanism_evidence:" + _canonical_sha256(
            challenge_evidence
        )
        verification["mechanism_evidence"] = [
            mechanism_evidence,
            challenge_evidence,
        ]
        replay_by_id = {replay["experiment_id"]: replay for replay in verification["experiments"]}
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
        intervention["intervention_receipt_id"] = "falsification_intervention:" + _canonical_sha256(
            intervention
        )
        verification["falsification_interventions"] = [intervention]
        verification["deterministic_mechanism_closures"] = []
        verification["hypothesis_refs"][0]["falsification_attempts"] = [
            {
                "attempt_id": "falsify-h1-alternative",
                "hypothesis_id": "h1",
                "claim": "The guard is missing",
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
                "observable_assertion": proof["experiments"][2]["observable_assertion"],
                "exit_code": replay_by_id["exp-challenge"]["exit_code"],
                "stdout_sha256": replay_by_id["exp-challenge"]["stdout_sha256"],
                "stderr_sha256": replay_by_id["exp-challenge"]["stderr_sha256"],
                "mechanism_evidence_ids": [challenge_evidence["mechanism_evidence_id"]],
                "intervention_receipt_id": intervention["intervention_receipt_id"],
            }
        ]
        verification["verified_mechanism"] = {
            "schema_version": 3,
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
            "schema_version": 2,
            "primary_hypothesis_id": "h1",
            "mechanism_evidence_ids": sorted(
                [
                    mechanism_evidence["mechanism_evidence_id"],
                    challenge_evidence["mechanism_evidence_id"],
                ]
            ),
            "causal_root_evidence_ids": [mechanism_evidence["mechanism_evidence_id"]],
            "support_connectivity": sorted(
                [
                    {
                        "mechanism_evidence_id": mechanism_evidence["mechanism_evidence_id"],
                        "experiment_ids": ["exp-1", "exp-control"],
                        "connection_kind": "causal_root",
                        "connected_from_mechanism_evidence_id": None,
                        "shared_verified_symbols": [],
                        "verified_causal_edge": None,
                        "verified_causal_edges": [],
                        "causal_root_kinds": ["origin_symptom_observation"],
                    },
                    {
                        "mechanism_evidence_id": challenge_evidence["mechanism_evidence_id"],
                        "experiment_ids": ["exp-challenge"],
                        "connection_kind": "shared_verified_symbol",
                        "connected_from_mechanism_evidence_id": mechanism_evidence[
                            "mechanism_evidence_id"
                        ],
                        "shared_verified_symbols": ["core.run"],
                        "verified_causal_edge": None,
                        "verified_causal_edges": [],
                        "causal_root_kinds": [],
                    },
                ],
                key=lambda value: value["mechanism_evidence_id"],
            ),
            "support_symbol_coverage": sorted(
                [
                    {
                        "experiment_ids": ["exp-1", "exp-control"],
                        "mechanism_symbols": ["core.run"],
                    },
                    {
                        "experiment_ids": ["exp-challenge"],
                        "mechanism_symbols": ["core.run"],
                    },
                ],
                key=lambda value: json.dumps(value, sort_keys=True, separators=(",", ":")),
            ),
            "causal_control_ids": [control_receipt["control_verification_id"]],
            "falsification_intervention_ids": [intervention["intervention_receipt_id"]],
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
        if proof.get("research_status") != "evidence_sufficient":
            verification["verified_mechanism"] = None
            verification["verified_mechanism_sha256"] = None
            verification["verified_mechanism_provenance"] = None
            verification["verified_mechanism_provenance_sha256"] = None
        oracle = {
            "schema_version": 1,
            "case_id": proof["case_id"],
            "repo_revision": proof["repo_revision"],
            "primary_hypothesis_id": "h1",
            "primary_verified_mechanism_sha256": verification["verified_mechanism_sha256"],
            "primary_verified_mechanism_provenance_sha256": verification[
                "verified_mechanism_provenance_sha256"
            ],
            "research_experiment_id": "exp-1",
            "scenario_kind": "original_replay",
            "origin_atom_ids": ["a1", "a2"],
            "mechanism_evidence_ids": [mechanism_evidence["mechanism_evidence_id"]],
            "baseline": {
                "exit_code": 1,
                "observable_assertion": proof["experiments"][0]["observable_assertion"],
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
            "primary_hypothesis_id": "h1",
            "primary_verified_mechanism_sha256": verification["verified_mechanism_sha256"],
            "primary_verified_mechanism_provenance_sha256": verification[
                "verified_mechanism_provenance_sha256"
            ],
            "origin_evidence": {
                "atom_id": "a1",
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
        _attach_exact_origin_boundary(
            proof=proof,
            verification=verification,
            oracle=oracle,
            positive_contract=positive_contract,
            mechanism_evidence_ids=[str(mechanism_evidence["mechanism_evidence_id"])],
            atom_id="a1",
        )
        verification["outcome_oracles"] = [oracle]
        if proof.get("research_status") != "evidence_sufficient":
            verification["outcome_oracles"] = []
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
            "origin_atom_ids": ["a1", "a2"],
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


def test_assemble_backlog_tickets_splits_by_change_plan() -> None:
    problem_records = [
        _problem_record("problem:one", title="One"),
        _problem_record("problem:two", title="Two"),
    ]
    priority_decisions = [
        {
            "case_id": "case:one",
            "problem_id": "problem:one",
            "priority_bucket": "p1",
            "selected_for_research": True,
            "priority_rationale": "R",
            "priority_status": "prioritized",
        }
    ]
    research_dossiers = [_research_proof("problem:one")]
    solution_option_sets = [_option("problem:one")]
    selection_decisions = [_selection("problem:one")]
    change_plans = [_plan("problem:one", 1), _plan("problem:one", 2)]

    tickets = assemble_backlog_tickets(
        problem_records=problem_records,
        priority_decisions=priority_decisions,
        research_dossiers=research_dossiers,
        solution_option_sets=solution_option_sets,
        selection_decisions=selection_decisions,
        change_plans=change_plans,
    )

    # Two plans -> two tickets, plus a triage ticket for the untouched problem record.
    assert len(tickets) == 3

    planned = [t for t in tickets if t.get("change_plan_id") in {"plan:one:1", "plan:one:2"}]
    assert len(planned) == 2
    for ticket in planned:
        assert ticket["stage"] == "ready_for_ticket"
        assert ticket["selected_option_id"] == "option:one:direct"
        assert ticket["suggested_owner"] == "docs"
        assert isinstance(ticket.get("problem_record"), dict)
        assert isinstance(ticket.get("selected_solution"), dict)
        assert isinstance(ticket.get("change_plan"), dict)

        assert ticket["research_readiness"] == {
            "ready": True,
            "reasons": [],
            "research_schema_version": 3,
        }
        assert ticket["ticket_readiness"] == {"ready": True, "reasons": []}
        assert ticket["investigation_steps"] == []

    triage = [t for t in tickets if t.get("problem_record", {}).get("problem_id") == "problem:two"]
    assert len(triage) == 1
    assert triage[0]["stage"] == "triage"


@pytest.mark.parametrize("disposition", ["already_addressed", "non_actionable"])
def test_assemble_backlog_tickets_does_not_reopen_terminal_no_change_research(
    disposition: str,
) -> None:
    problem_id = "problem:one"
    research = _research_proof(
        problem_id,
        actionability_assessment={
            "disposition": disposition,
            "rationale": "Verified evidence establishes that no product change is due.",
            "evidence_refs": ["exp-1"],
        },
    )
    research["canonical_problem_id"] = problem_id
    research["case_member_problem_ids"] = [problem_id]

    tickets = assemble_backlog_tickets(
        problem_records=[_problem_record(problem_id)],
        priority_decisions=[],
        research_dossiers=[research],
        solution_option_sets=[],
        selection_decisions=[],
        change_plans=[],
    )

    assert tickets == []


def test_assemble_backlog_tickets_rejects_downstream_work_for_terminal_no_change() -> None:
    problem_id = "problem:one"
    research = _research_proof(
        problem_id,
        actionability_assessment={
            "disposition": "already_addressed",
            "rationale": "Verified evidence establishes that no product change is due.",
            "evidence_refs": ["exp-1"],
        },
    )

    with pytest.raises(ValueError, match="terminal no-change research"):
        assemble_backlog_tickets(
            problem_records=[_problem_record(problem_id)],
            priority_decisions=[],
            research_dossiers=[research],
            solution_option_sets=[_option(problem_id)],
            selection_decisions=[],
            change_plans=[],
        )


def test_assemble_backlog_tickets_does_not_trust_no_change_on_unready_research() -> None:
    problem_id = "problem:one"
    research = _research_proof(
        problem_id,
        reproduction_status="partial",
        research_status="insufficient_evidence",
        root_cause_confidence=0.4,
        material_unknowns=[
            {
                "unknown": "Whether the claimed fix covers the original failure",
                "affects": ["root_cause"],
                "evidence_needed": "Replay the original scenario",
            }
        ],
        actionability_assessment={
            "disposition": "already_addressed",
            "rationale": "The model claimed the issue was addressed without sufficient proof.",
            "evidence_refs": ["exp-1"],
        },
    )

    tickets = assemble_backlog_tickets(
        problem_records=[_problem_record(problem_id)],
        priority_decisions=[],
        research_dossiers=[research],
        solution_option_sets=[],
        selection_decisions=[],
        change_plans=[],
    )

    assert len(tickets) == 1
    assert tickets[0]["stage"] == "research_required"
    assert tickets[0]["research_readiness"]["ready"] is False


def test_research_required_ticket_accepts_runner_owned_research_lineage_envelope() -> None:
    problem_id = "problem:one"
    research = _research_proof(
        problem_id,
        reproduction_status="partial",
        research_status="insufficient_evidence",
        root_cause_confidence=0.4,
        material_unknowns=[
            {
                "unknown": "Which producer exhausted the workspace volume",
                "affects": ["root_cause", "actionability"],
                "evidence_needed": "Contemporaneous capacity and ownership evidence",
            }
        ],
    )
    research["canonical_problem_id"] = problem_id
    research["case_member_problem_ids"] = [problem_id]

    tickets = assemble_backlog_tickets(
        problem_records=[_problem_record(problem_id)],
        priority_decisions=[
            {
                "case_id": "case:one",
                "problem_id": problem_id,
                "priority_bucket": "p2",
                "selected_for_research": True,
                "priority_rationale": "The material unknown requires research.",
                "priority_status": "prioritized",
            }
        ],
        research_dossiers=[research],
        solution_option_sets=[],
        selection_decisions=[],
        change_plans=[],
    )

    assert len(tickets) == 1
    ticket = tickets[0]
    assert ticket["stage"] == "research_required"
    reasons = ticket["ticket_readiness"]["reasons"]
    assert "research_status_insufficient_evidence" in reasons
    assert "research_proof_invalid" not in reasons
    assert not any("research_dossier_unknown_fields" in reason for reason in reasons)


def test_plan_cannot_abandon_verified_intervention_for_unbound_target() -> None:
    problem = _problem_record("problem:one", title="One")
    option = _option("problem:one")
    plan = _plan("problem:one", 1)
    plan["change_targets"] = [
        {
            "action": "modify",
            "path": "src/unrelated.py",
            "symbols": ["unrelated.refresh"],
            "change": "Replace the verified local intervention with an unrelated cache change.",
        }
    ]
    plan["scope_evidence"] = {
        "scope_level": "shared_abstraction",
        "independent_consumers_or_failure_paths": [
            {"name": "unrelated A", "evidence_refs": ["exp-1"]},
            {"name": "unrelated B", "evidence_refs": ["exp-control"]},
        ],
    }
    plan = assign_plan_revision_id(plan)

    tickets = assemble_backlog_tickets(
        problem_records=[problem],
        priority_decisions=[
            {
                "case_id": "case:one",
                "problem_id": "problem:one",
                "priority_bucket": "p1",
                "selected_for_research": True,
                "priority_rationale": "Research required",
                "priority_status": "prioritized",
            }
        ],
        research_dossiers=[_research_proof("problem:one")],
        solution_option_sets=[option],
        selection_decisions=[_selection("problem:one")],
        change_plans=[plan],
    )

    assert tickets[0]["stage"] == "research_required"
    reasons = tickets[0]["ticket_readiness"]["reasons"]
    assert "change_plan_scope_evidence_linkage_mismatch" in reasons
    assert "change_plan_intervention_targets_missing" in reasons
    assert "change_plan_additional_target_causal_binding_missing:0" in reasons


def test_generic_tests_cannot_substitute_for_recurrence_role() -> None:
    problem = _problem_record("problem:one", title="One")
    plan = _plan("problem:one", 1)
    roles = plan["outcome_verification_roles"]
    assert isinstance(roles, dict)
    recurrence = roles["recurrence"]
    assert isinstance(recurrence, dict)
    recurrence.pop("verification_owner")
    recurrence["commands"] = ["pytest -q tests/test_core.py::test_reported_failure"]
    recurrence["command_bindings"] = [
        {"command_index": 0, "research_experiment_id": "exp-1"}
    ]
    recurrence["predicates"] = [
        {"type": "command_exit_code", "command_index": 0, "equals": 0}
    ]
    plan = assign_plan_revision_id(plan)

    tickets = assemble_backlog_tickets(
        problem_records=[problem],
        priority_decisions=[
            {
                "case_id": "case:one",
                "problem_id": "problem:one",
                "priority_bucket": "p1",
                "selected_for_research": True,
                "priority_rationale": "Research required",
                "priority_status": "prioritized",
            }
        ],
        research_dossiers=[_research_proof("problem:one")],
        solution_option_sets=[_option("problem:one")],
        selection_decisions=[_selection("problem:one")],
        change_plans=[plan],
    )

    assert tickets[0]["stage"] == "research_required"
    assert (
        "change_plan_outcome_role_reuses_generic_verification:recurrence"
        in tickets[0]["ticket_readiness"]["reasons"]
    )


def test_assemble_backlog_tickets_requires_selection_for_plans() -> None:
    with pytest.raises(ValueError) as exc:
        assemble_backlog_tickets(
            problem_records=[_problem_record("problem:one")],
            priority_decisions=[],
            research_dossiers=[],
            solution_option_sets=[],
            selection_decisions=[],
            change_plans=[
                {
                    "change_plan_id": "plan:one:1",
                    "problem_id": "problem:one",
                    "selected_option_id": "option:one:direct",
                    "title": "Plan A",
                    "problem": "P",
                    "user_impact": "U",
                    "proposed_fix": "Fix A",
                    "implementation_steps": ["Do A"],
                    "verification_steps": ["Check A"],
                    "success_criteria": ["Done A"],
                    "rollback_notes": "Rollback A",
                    "suggested_owner": "docs",
                    "change_plan_status": "planned",
                    "related_change_plan_ids": [],
                }
            ],
        )
    assert "missing selection decisions for change plans" in str(exc.value)


def test_assemble_backlog_tickets_does_not_ready_partial_research() -> None:
    problem_id = "problem:one"
    research = _research_proof(
        problem_id,
        reproduction_status="partial",
        research_status="insufficient_evidence",
        root_cause_confidence=0.4,
        material_unknowns=[
            {
                "unknown": "The failing interface is not known",
                "affects": ["root_cause", "interface", "change_surface"],
                "evidence_needed": "Capture the original failing command",
            }
        ],
    )
    selection = {
        "problem_id": problem_id,
        "selected_option_id": "option:one:direct",
        "selected_family_id": "most_direct",
        "selection_rationale": "R",
        "repo_intent_alignment": "R",
        "why_other_options_were_not_selected": "R",
        "needs_ux_review": False,
    }
    plan = {
        "change_plan_id": "plan:one:1",
        "problem_id": problem_id,
        "selected_option_id": "option:one:direct",
        "title": "Plan",
        "problem": "P",
        "user_impact": "U",
        "proposed_fix": "F",
        "implementation_steps": ["Do"],
        "verification_steps": ["Check"],
        "success_criteria": ["Done"],
        "rollback_notes": "Rollback",
        "suggested_owner": "core",
        "related_change_plan_ids": [],
    }

    tickets = assemble_backlog_tickets(
        problem_records=[_problem_record(problem_id)],
        priority_decisions=[],
        research_dossiers=[research],
        solution_option_sets=[],
        selection_decisions=[selection],
        change_plans=[plan],
    )

    assert tickets[0]["stage"] == "research_required"
    assert tickets[0]["research_readiness"]["ready"] is False
    assert "research_status_insufficient_evidence" in tickets[0]["research_readiness"]["reasons"]
    assert tickets[0]["research"] == research
    assert tickets[0]["investigation_steps"] == [
        "Resolve material unknown: The failing interface is not known. "
        "Evidence needed: Capture the original failing command"
    ]
