"""Positive acceptance benchmark for useful automated-backlog throughput.

These tests intentionally exercise successful paths across the depth contracts.  The
suite is not another collection of rejection checks: it proves that a real symptom can
move from assigned source evidence through causal research, option/selection, and a
decision-complete plan with an executable original-scenario oracle.
"""

from __future__ import annotations

import json
import subprocess
from hashlib import sha256
from pathlib import Path
from typing import Any

import backlog_miner.research_runner as production_research_runner
import pytest
from agent_adapters.read_attestation import observed_read_attestation
from backlog_core.case_lineage import (
    attach_supporting_atoms_to_problem_cases,
    eligible_problem_mining_atoms,
    normalize_atom_lineage,
)
from backlog_core.stage_contracts import (
    assess_research_readiness,
    evidence_assignment_sha256,
    evidence_verification_sha256,
    parse_problem_record_list,
    research_claims_sha256,
)
from backlog_core.ticket_readiness import (
    assess_solution_option_readiness,
    assess_ticket_readiness,
    assign_plan_revision_id,
    bind_falsification_review,
    bind_plan_outcome_oracle,
    verified_outcome_oracles,
)
from backlog_miner.research_evidence import (
    TrustedHostReplayExecutor,
    verify_persisted_research_evidence,
)
from runner_core import RunnerConfig, RunResult

from usertest_backlog.workflows.post_research_relations import (
    collapse_post_research_verified_mechanisms,
)

_REVISION = "a" * 40
_ATOM_ID = "run/lifecycle/codex/0:wrong_output:1"


def _canonical_sha256(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return sha256(encoded).hexdigest()


def _content_id(prefix: str, value: dict[str, Any], id_field: str) -> str:
    projection = {key: item for key, item in value.items() if key != id_field}
    return f"{prefix}:{_canonical_sha256(projection)}"


def _trusted_host_isolation() -> dict[str, object]:
    return {
        "executor": "trusted_host",
        "platform": "windows",
        "os_sandbox": False,
        "network": "not_enforced",
        "filesystem_isolation": "dedicated_clone_only_not_os_sandbox",
        "trust_decision": "approved_local_source_root",
        "trust_reason": "C:/runs/source",
        "source_workspace": "C:/runs/research-workspace",
        "sanitized_environment_keys": ["CI"],
    }


def _experiment(
    experiment_id: str,
    *,
    command: str,
    outcome: str,
    assertion: dict[str, object],
    result: str,
    scenario_kind: str = "faithful_replay",
    exit_code: int = 0,
    atom_id: str = _ATOM_ID,
    artifact_refs: list[str] | None = None,
    static_trace: dict[str, object] | None = None,
) -> dict[str, object]:
    item: dict[str, object] = {
        "experiment_id": experiment_id,
        "scenario_kind": scenario_kind,
        "addresses_atom_ids": [atom_id],
        "command": command,
        "result": result,
        "outcome": outcome,
        "exit_code": exit_code,
        "observable_assertion": assertion,
        "artifact_refs": artifact_refs or ["artifact:source", "artifact:observed"],
        "platform_requirement": "any",
    }
    if scenario_kind == "faithful_replay":
        item["fidelity_mapping"] = {
            "original_condition": "Retained incomplete-run metadata reaches classification.",
            "retained_differences": "The probe replaces only the original run directory lookup.",
            "why_mechanism_equivalent": (
                "It calls the production classifier with the retained metadata unchanged."
            ),
        }
    if scenario_kind == "static_trace":
        item["static_trace"] = static_trace
    return item


def _assignment(
    *,
    case_id: str,
    problem_id: str,
    atom_id: str,
    atom_snapshot: dict[str, object],
) -> dict[str, object]:
    assignment: dict[str, object] = {
        "status": "complete",
        "errors": [],
        "case_id": case_id,
        "problem_id": problem_id,
        "expected_atom_ids": [atom_id],
        "atom_receipts": [
            {
                "atom_id": atom_id,
                "atom_sha256": _canonical_sha256(atom_snapshot),
                "atom_snapshot": atom_snapshot,
                "artifact_receipts": [
                    {
                        "path": "C:/runs/origin.json",
                        "sha256": "5" * 64,
                        "size_bytes": 97,
                    }
                ],
            }
        ],
    }
    assignment["assignment_sha256"] = evidence_assignment_sha256(assignment)
    return assignment


def _mechanism_evidence(
    *,
    evidence_type: str,
    symbol: str,
    path: str,
    experiment_id: str,
    atom_id: str,
    assertion: dict[str, object],
    mechanism_link: dict[str, object],
    harness_path: str | None,
    consumer_kind: str = "production_entrypoint",
) -> dict[str, object]:
    consumer_identity = {"kind": consumer_kind, "entrypoint": symbol}
    receipt: dict[str, object] = {
        "evidence_type": evidence_type,
        "hypothesis_id": "h1",
        "mechanism_symbols": [symbol],
        "code_paths": [{"symbol": symbol, "path": path}],
        "experiment_ids": [experiment_id],
        "artifact_refs": ["artifact:source", "artifact:observed"],
        "origin_atom_ids": [atom_id],
        "path_name": symbol,
        "consumer_identity": consumer_identity,
        "independence_key": _canonical_sha256(consumer_identity),
        "observed_result": {
            "exit_code": 0,
            "stdout_sha256": "f" * 64,
            "stderr_sha256": "1" * 64,
            "assertion": assertion,
        },
        "harness_path": harness_path,
        "mechanism_link": mechanism_link,
        "platform_requirement": "any",
        "observed_platform": "windows",
        # A successful counter-scenario limits the fix to the evidenced boundary.
        "adversarial_effect": "limits_scope",
    }
    receipt["mechanism_evidence_id"] = _content_id(
        "mechanism_evidence", receipt, "mechanism_evidence_id"
    )
    return receipt


def _verified_research_proof(
    *,
    case_id: str = "case:lifecycle-wrong-output",
    problem_id: str = "problem:lifecycle-wrong-output",
    atom_id: str = _ATOM_ID,
    symbol: str = "lifecycle.classify_incomplete",
    path: str = "src/lifecycle.py",
    evidence_type: str = "temporary_harness",
    scenario_kind: str = "faithful_replay",
    support_command: str = (
        "python .usertest_research/replay_lifecycle.py --fixture retained-incomplete"
    ),
    assertion: dict[str, object] | None = None,
    mechanism_link: dict[str, object] | None = None,
    harness_path: str | None = ".usertest_research/replay_lifecycle.py",
    consumer_kind: str = "production_entrypoint",
    research_method: str = "reproduction",
    reproduction_status: str = "reproduced",
    static_trace: dict[str, object] | None = None,
    primary_statement: str | None = None,
    alternative_statement: str | None = None,
    atom_text: str | None = None,
) -> dict[str, Any]:
    baseline_assertion = assertion or {
        "source": "stdout",
        "operator": "contains",
        "expected": "classification=policy_block",
    }
    counter_assertion = {
        "source": str(baseline_assertion["source"]),
        "operator": "not_contains",
        "expected": str(baseline_assertion["expected"]),
    }
    experiments = [
        _experiment(
            "exp-support",
            command=support_command,
            outcome="supports",
            assertion=baseline_assertion,
            result="The production mechanism emits the retained wrong classification.",
            scenario_kind=scenario_kind,
            atom_id=atom_id,
            static_trace=static_trace,
        ),
        _experiment(
            "exp-counter",
            command=f"{support_command} --complete-metadata",
            outcome="supports",
            assertion=counter_assertion,
            result="Complete metadata does not produce the wrong classification.",
            scenario_kind=scenario_kind,
            atom_id=atom_id,
            static_trace=static_trace,
        ),
        _experiment(
            "exp-alt-refute",
            command=f"{support_command} --raw-output",
            outcome="refutes",
            assertion=counter_assertion,
            result="The raw metadata is correct before classification, refuting formatting.",
            scenario_kind=scenario_kind,
            atom_id=atom_id,
            static_trace=static_trace,
        ),
    ]
    if research_method != "static_trace":
        experiments[1]["scenario_kind"] = "control"
        experiments[1].pop("fidelity_mapping", None)
        experiments[1]["control_relationship"] = {
            "supports_experiment_id": "exp-support",
            "mechanism_symbols": [symbol],
            "controlled_variable": "complete metadata",
            "expected_difference": ("The wrong classification is absent with complete metadata."),
        }
    atom_snapshot = {
        "atom_id": atom_id,
        "text": atom_text
        or "An incomplete run is reported as blocked by policy although policy is allowed.",
        "command": support_command,
        "exit_code": 0,
        "evidence_role": "observation",
        "origin_stage": "runtime",
        "expected_output": "expected_behavior_confirmed",
    }
    expected_output = atom_snapshot["expected_output"]
    experiments[0]["origin_evidence_bindings"] = [
        {
            "atom_id": atom_id,
            "role": "expected_behavior",
            "field_path": "$.expected_output",
            "value": expected_output,
            "value_sha256": _canonical_sha256(expected_output),
        }
    ]
    positive_postcondition: dict[str, object]
    if research_method == "static_trace":
        positive_postcondition = {
            "type": "config_state_equals",
            "mechanism_symbol": symbol,
            "exists": True,
            "equals": expected_output,
        }
    else:
        positive_postcondition = {
            "type": "command_stdout_contains",
            "value": expected_output,
        }
    experiments[0]["positive_outcome_contract"] = {
        "contract_kind": "origin_atom_exact_value",
        "atom_id": atom_id,
        "field_path": "$.expected_output",
        "postcondition": positive_postcondition,
    }
    assignment = _assignment(
        case_id=case_id,
        problem_id=problem_id,
        atom_id=atom_id,
        atom_snapshot=atom_snapshot,
    )
    proof: dict[str, Any] = {
        "research_schema_version": 3,
        "case_id": case_id,
        "problem_id": problem_id,
        "repo_revision": _REVISION,
        "research_method": research_method,
        "reproduction_status": reproduction_status,
        "research_status": "evidence_sufficient",
        "writes_used": harness_path is not None,
        "writes_purpose": ["temporary_harness"] if harness_path else ["none"],
        "implementation_performed": False,
        "diff_classification": ("allowed_research_edits" if harness_path else "no_changes"),
        "artifact_refs": [
            {
                "artifact_id": "artifact:source",
                "kind": "source",
                "path": path,
                "description": "The exact production mechanism inspected by research.",
            },
            {
                "artifact_id": "artifact:observed",
                "kind": "test_output",
                "path": ".usertest_research/observed.txt",
                "description": "Captured wrong-output and counter-scenario results.",
            },
        ],
        "experiments": experiments,
        "inspected_files": [path],
        "inspected_symbols": [symbol],
        "root_cause_hypotheses": [
            {
                "hypothesis_id": "h1",
                "statement": primary_statement
                or (
                    "The classifier maps missing completion metadata to policy_block "
                    "before checking the actual lifecycle state."
                ),
                "supporting_evidence": ["exp-support", "exp-counter"],
                "counterevidence": [],
                "falsification_attempts": [
                    {
                        "attempt_id": "falsify-h1-complete-metadata",
                        "hypothesis_id": "h1",
                        "claim": primary_statement
                        or (
                            "The classifier maps missing completion metadata to policy_block "
                            "before checking the actual lifecycle state."
                        ),
                        "baseline_experiment_id": "exp-support",
                        "challenge_experiment_id": "exp-counter",
                        "disproof_condition": baseline_assertion,
                        "outcome": "survived",
                    }
                ],
                "mechanism_symbols": [symbol],
                "disposition": "primary",
                "disposition_evidence": ["exp-support", "exp-counter"],
            },
            {
                "hypothesis_id": "h2",
                "statement": alternative_statement
                or "Output formatting changes the classification after the mechanism.",
                "supporting_evidence": ["artifact:observed"],
                "counterevidence": ["exp-alt-refute"],
                "falsification_attempts": [
                    {
                        "attempt_id": "falsify-h2-raw-output",
                        "hypothesis_id": "h2",
                        "claim": alternative_statement
                        or "Output formatting changes the classification after the mechanism.",
                        "baseline_experiment_id": "exp-support",
                        "challenge_experiment_id": "exp-alt-refute",
                        "disproof_condition": counter_assertion,
                        "outcome": "disproved",
                    }
                ],
                "mechanism_symbols": [symbol],
                "disposition": "refuted",
                "disposition_evidence": ["exp-alt-refute"],
            },
        ],
        "root_cause_confidence": 0.93,
        "broader_class_assessment": "isolated_instance",
        "material_unknowns": [],
        "blocking_reasons": [],
        "evidence_boundaries": [
            "The result proves this classifier boundary, not every lifecycle consumer."
        ],
        "evidence_assignment": assignment,
    }
    proof["root_cause_hypotheses"] = proof["root_cause_hypotheses"][:1]
    if research_method == "static_trace":
        proof["root_cause_hypotheses"][0]["falsification_attempts"] = []
    link = mechanism_link or {
        "verification_method": "runner_harness_observable_dataflow_v1",
        "entrypoint": harness_path or symbol,
        "observable_source": baseline_assertion["source"],
        "symbol_sinks": [{"symbol": symbol, "sink": str(baseline_assertion["source"])}],
    }
    mechanism = _mechanism_evidence(
        evidence_type=evidence_type,
        symbol=symbol,
        path=path,
        experiment_id="exp-support",
        atom_id=atom_id,
        assertion=baseline_assertion,
        mechanism_link=link,
        harness_path=harness_path,
        consumer_kind=consumer_kind,
    )
    mechanism["experiment_ids"] = ["exp-support", "exp-counter"]
    mechanism["mechanism_evidence_id"] = _content_id(
        "mechanism_evidence", mechanism, "mechanism_evidence_id"
    )
    isolation = _trusted_host_isolation()
    overlay_manifest = (
        {
            str(harness_path): {
                "kind": "file",
                "mode": 420,
                "sha256": "c" * 64,
                "size_bytes": 128,
            }
        }
        if harness_path
        else {}
    )
    receipt: dict[str, Any] = {
        "verification_method": "runner_artifact_binding_v1",
        "status": "verified",
        "case_id": case_id,
        "problem_id": problem_id,
        "repo_revision": _REVISION,
        "requested_repo_ref": "origin/dev",
        "resolved_repo_ref": _REVISION,
        "workspace_dir": "C:/runs/research-workspace",
        "workspace_head": _REVISION,
        "workspace_overlay": {
            "baseline_manifest_sha256": "6" * 64,
            "research_manifest_sha256": "7" * 64,
            "baseline_state_sha256": "8" * 64,
            "research_state_sha256": "9" * 64,
            "baseline_git_index_sha256": "a" * 64,
            "research_git_index_sha256": "b" * 64,
            "changed_baseline_paths": [],
            "research_overlay_paths": list(overlay_manifest),
            "research_overlay_manifest": overlay_manifest,
            "research_overlay_manifest_sha256": "d" * 64,
            "suspicious_extra_paths": [],
            "git_index_changed": False,
        },
        "replay_isolation": isolation,
        "planning_workspace_dir": "C:/runs/planning-workspace",
        "planning_workspace_head": _REVISION,
        "planning_workspace_clean": True,
        "run_dir": "C:/runs/research",
        "origin_atom_ids": [atom_id],
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
                "size_bytes": 128,
            }
            for artifact in proof["artifact_refs"]
        ],
        "experiments": [
            {
                "experiment_id": experiment["experiment_id"],
                "command": experiment["command"],
                "executed_argv": str(experiment["command"]).split(),
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
                "workspace_head": _REVISION,
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
            for index, experiment in enumerate(experiments)
        ],
        "inspected_files": [
            {
                "path": path,
                "sha256": "d" * 64,
                "git_blob_sha": "2" * 40,
                "size_bytes": 512,
                "read_event_index": 2,
                "read_event_sha256": "3" * 64,
                "read_source": "tool",
                "bytes_observed": 512,
                "whole_file_observed": True,
                "observed_content_sha256": "4" * 64,
                "observed_start_line": 1,
                "observed_end_line": 20,
            }
        ],
        "inspected_symbols": [{"symbol": symbol, "path": path}],
        "hypothesis_refs": [
            {
                "hypothesis_id": hypothesis["hypothesis_id"],
                "supporting_refs": hypothesis["supporting_evidence"],
                "counterevidence_refs": hypothesis["counterevidence"],
                "mechanism_symbols": hypothesis["mechanism_symbols"],
                "disposition": hypothesis["disposition"],
                "disposition_evidence_refs": hypothesis["disposition_evidence"],
                "control_links": [],
            }
            for hypothesis in proof["root_cause_hypotheses"]
        ],
        "causal_links": [],
        "mechanism_evidence": [mechanism],
        "verified_mechanism": None,
        "verified_mechanism_sha256": None,
        "verified_mechanism_provenance": None,
        "verified_mechanism_provenance_sha256": None,
        "outcome_oracles": [],
        "test_selections": [],
        "control_verifications": [],
        "falsification_interventions": [],
        "deterministic_mechanism_closures": [],
        "failure_paths": [],
        "atom_bindings": [
            {
                "experiment_id": "exp-support",
                "atom_id": atom_id,
                "match_kind": "command_and_exit_code",
            },
            {
                "experiment_id": "exp-support",
                "atom_id": atom_id,
                "binding_role": "expected_behavior",
                "match_kind": "explicit_field_binding",
                "origin_atom_sha256": assignment["atom_receipts"][0]["atom_sha256"],
                "origin_atom_field_path": "$.expected_output",
                "origin_atom_value_sha256": _canonical_sha256(expected_output),
            },
        ],
        "errors": [],
    }
    replay_by_id = {replay["experiment_id"]: replay for replay in receipt["experiments"]}
    primary = proof["root_cause_hypotheses"][0]
    if research_method == "static_trace":
        closure = {
            "verification_method": "runner_deterministic_mechanism_closure_v1",
            "hypothesis_id": "h1",
            "support_experiment_id": "exp-support",
            "scenario_kind": "static_trace",
            "mechanism_symbols": [symbol],
            "code_path": list(static_trace["code_path"] if static_trace else []),
            "closure_basis": "deterministic_static_trace",
            "alternatives_disposed": [],
            "origin_atom_ids": [atom_id],
            "observed_result": {
                "exit_code": replay_by_id["exp-support"]["exit_code"],
                "stdout_sha256": replay_by_id["exp-support"]["stdout_sha256"],
                "stderr_sha256": replay_by_id["exp-support"]["stderr_sha256"],
                "assertion": experiments[0]["observable_assertion"],
            },
        }
        closure["closure_receipt_id"] = _content_id(
            "deterministic_mechanism_closure",
            closure,
            "closure_receipt_id",
        )
        receipt["deterministic_mechanism_closures"] = [closure]
    else:
        relationship = experiments[1]["control_relationship"]
        intervention_argument = {
            "slot": "keyword:complete_metadata",
            "expression": "True",
            "ast_sha256": sha256(b"Constant(value=True)").hexdigest(),
        }
        intervention = {
            "verification_method": "pytest_ast_falsification_intervention_v1",
            "hypothesis_id": "h1",
            "attempt_id": "falsify-h1-complete-metadata",
            "baseline_experiment_id": "exp-support",
            "challenge_experiment_id": "exp-counter",
            "mechanism_symbols": [symbol],
            "baseline_selection_id": "h1:exp-support",
            "challenge_selection_id": "h1:exp-counter",
            "controlled_input_difference": {
                "verification_method": "python_ast_explicit_argument_delta_v1",
                "difference_count": 1,
                "difference": {
                    "mechanism_symbol": symbol,
                    "slot": "keyword:complete_metadata",
                    "difference_kind": "added_in_control",
                    "support_argument": None,
                    "control_argument": intervention_argument,
                },
            },
            "observed_polarity": {
                "verification_method": "runner_replay_falsification_polarity_v1",
                "polarity": "failure_persists_after_intervention",
                "baseline": {
                    "exit_code": replay_by_id["exp-support"]["exit_code"],
                    "stdout_sha256": replay_by_id["exp-support"]["stdout_sha256"],
                    "stderr_sha256": replay_by_id["exp-support"]["stderr_sha256"],
                },
                "challenge": {
                    "exit_code": replay_by_id["exp-counter"]["exit_code"],
                    "stdout_sha256": replay_by_id["exp-counter"]["stdout_sha256"],
                    "stderr_sha256": replay_by_id["exp-counter"]["stderr_sha256"],
                },
            },
            "relationship_sha256": _canonical_sha256(
                {
                    "controlled_variable": relationship["controlled_variable"],
                    "expected_difference": relationship["expected_difference"],
                    "mechanism_symbols": relationship["mechanism_symbols"],
                }
            ),
        }
        intervention["intervention_receipt_id"] = _content_id(
            "falsification_intervention",
            intervention,
            "intervention_receipt_id",
        )
        receipt["falsification_interventions"] = [intervention]
        receipt["hypothesis_refs"][0]["falsification_attempts"] = [
            {
                "attempt_id": "falsify-h1-complete-metadata",
                "hypothesis_id": "h1",
                "claim": primary["statement"],
                "baseline_experiment_id": "exp-support",
                "challenge_experiment_id": "exp-counter",
                "disproof_condition": baseline_assertion,
                "outcome": "survived",
                "scenario_kind": "control",
                "command": experiments[1]["command"],
                "declared_result": experiments[1]["result"],
                "observable_assertion": experiments[1]["observable_assertion"],
                "exit_code": replay_by_id["exp-counter"]["exit_code"],
                "stdout_sha256": replay_by_id["exp-counter"]["stdout_sha256"],
                "stderr_sha256": replay_by_id["exp-counter"]["stderr_sha256"],
                "mechanism_evidence_ids": [mechanism["mechanism_evidence_id"]],
                "intervention_receipt_id": intervention["intervention_receipt_id"],
            }
        ]
    receipt["verified_mechanism"] = {
        "schema_version": 2,
        "mechanism_symbols": [symbol],
        "code_paths": [{"symbol": symbol, "path": path}],
    }
    probe_points = []
    if receipt["falsification_interventions"]:
        difference = receipt["falsification_interventions"][0]["controlled_input_difference"]
        probe_points.append(
            {
                "verification_method": difference["verification_method"],
                "mechanism_symbols": [symbol],
                "slot": difference["difference"]["slot"],
                "mechanism_symbol": symbol,
            }
        )
    receipt["verified_mechanism_provenance"] = {
        "schema_version": 1,
        "primary_hypothesis_id": "h1",
        "mechanism_evidence_ids": [mechanism["mechanism_evidence_id"]],
        "causal_control_ids": [],
        "falsification_intervention_ids": [
            value["intervention_receipt_id"] for value in receipt["falsification_interventions"]
        ],
        "deterministic_closure_ids": [
            value["closure_receipt_id"] for value in receipt["deterministic_mechanism_closures"]
        ],
        "research_probe_control_points": probe_points,
    }
    receipt["verified_mechanism_sha256"] = _canonical_sha256(receipt["verified_mechanism"])
    receipt["verified_mechanism_provenance_sha256"] = _canonical_sha256(
        receipt["verified_mechanism_provenance"]
    )
    common_oracle = {
        "schema_version": 1,
        "case_id": case_id,
        "repo_revision": _REVISION,
        "research_experiment_id": "exp-support",
        "scenario_kind": scenario_kind,
        "origin_atom_ids": [atom_id],
        "mechanism_evidence_ids": [mechanism["mechanism_evidence_id"]],
        "baseline": {
            "exit_code": replay_by_id["exp-support"]["exit_code"],
            "observable_assertion": experiments[0]["observable_assertion"],
            "stdout_sha256": replay_by_id["exp-support"]["stdout_sha256"],
            "stderr_sha256": replay_by_id["exp-support"]["stderr_sha256"],
        },
    }
    if research_method == "static_trace":
        baseline_value = str(baseline_assertion["expected"]).split("=", 1)[-1]
        target = {
            "path": path,
            "format": "yaml",
            "json_pointer": "/" + symbol.removeprefix("config:/"),
            "source_file_sha256": "d" * 64,
            "baseline_exists": True,
            "baseline_value": baseline_value,
            "baseline_value_sha256": _canonical_sha256(baseline_value),
        }
        target["target_id"] = _content_id("config_state", target, "target_id")
        oracle = {
            **common_oracle,
            "kind": "config_state",
            "proof_scope": "configuration_state",
            "state_targets": [target],
        }
    else:
        argv = str(experiments[0]["command"]).split()
        oracle = {
            **common_oracle,
            "kind": "staged_replay",
            "proof_scope": "behavioral",
            "execution": {
                "argv": argv,
                "command_authorization": {
                    "authorization_kind": "standard_test_or_research_harness",
                    "executed_argv_sha256": _canonical_sha256(argv),
                    "shell": False,
                    "workspace_confined": True,
                },
                "platform_requirement": "any",
                "shell": False,
            },
            "asset": None,
        }
    grounded_postconditions = (
        [
            {
                "type": "oracle_state_equals",
                "target_id": oracle["state_targets"][0]["target_id"],
                "exists": True,
                "equals": expected_output,
            }
        ]
        if research_method == "static_trace"
        else [
            {"type": "command_exit_code", "command_index": 0, "equals": 0},
            {
                "type": "command_stdout_contains",
                "command_index": 0,
                "value": expected_output,
            },
        ]
    )
    positive_contract: dict[str, object] = {
        "schema_version": 1,
        "kind": "origin_evidence_semantic_contract",
        "research_experiment_id": "exp-support",
        "mechanism_evidence_ids": [mechanism["mechanism_evidence_id"]],
        "origin_evidence": {
            "atom_id": atom_id,
            "atom_sha256": assignment["atom_receipts"][0]["atom_sha256"],
            "field_path": "$.expected_output",
            "value_sha256": _canonical_sha256(expected_output),
        },
        "postconditions": grounded_postconditions,
    }
    positive_contract["positive_outcome_contract_id"] = _content_id(
        "positive_outcome_contract",
        positive_contract,
        "positive_outcome_contract_id",
    )
    oracle["positive_outcome_contracts"] = [positive_contract]
    oracle["outcome_oracle_id"] = _content_id("outcome_oracle", oracle, "outcome_oracle_id")
    receipt["outcome_oracles"] = [oracle]
    receipt["receipt_sha256"] = evidence_verification_sha256(receipt)
    proof["evidence_verification"] = receipt
    return proof


def _option(research: dict[str, Any]) -> dict[str, Any]:
    hypothesis = research["root_cause_hypotheses"][0]
    mechanism = research["evidence_verification"]["mechanism_evidence"][0]
    symbol = hypothesis["mechanism_symbols"][0]
    path = research["inspected_files"][0]
    intervention = "Classify missing completion metadata from lifecycle state before policy status."
    return {
        "option_id": "option:lifecycle:causal-boundary",
        "case_id": research["case_id"],
        "problem_id": research["problem_id"],
        "family_id": "most_direct",
        "summary": "Correct the evidenced lifecycle classification boundary.",
        "tradeoffs": "The change intentionally remains local to the proven consumer.",
        "recurrence_prevention": (
            "Every incomplete result crossing this consumer uses lifecycle state first."
        ),
        "change_surface_hypothesis": path,
        "test_implications": "Replay the retained wrong-output fixture and its counter-case.",
        "rationale": "The safe harness calls this exact production mechanism.",
        "causal_coverage": {
            "mechanism_addressed": hypothesis["statement"],
            "research_binding": {
                "hypothesis_id": "h1",
                "hypothesis_statement": hypothesis["statement"],
                "mechanism_symbols": [symbol],
                "supporting_evidence_refs": ["exp-support", "exp-counter"],
                "counterevidence_refs": [],
                "falsification_attempt_refs": ["falsify-h1-complete-metadata"],
                "deterministic_closure_refs": [],
                "intervention_points": [
                    {
                        "mechanism_symbol": symbol,
                        "target_path": path,
                        "target_symbol": symbol,
                        "intervention": intervention,
                    }
                ],
            },
            "symptoms_covered": ["Wrong policy_block classification for incomplete runs"],
            "unsupported_assumptions": [],
            "residual_recurrence_paths": [],
            "compatibility_risks": ["True policy blocks must retain their classification."],
            "testability": {
                "before": "The retained fixture prints classification=policy_block.",
                "after": "The same fixture no longer prints that classification.",
            },
        },
        "scope_evidence": {
            "scope_level": "single_path",
            "independent_consumers_or_failure_paths": [
                {
                    "name": mechanism["path_name"],
                    "evidence_refs": [mechanism["mechanism_evidence_id"]],
                }
            ],
        },
    }


def _selection(research: dict[str, Any], option: dict[str, Any]) -> dict[str, Any]:
    evidence_id = research["evidence_verification"]["mechanism_evidence"][0][
        "mechanism_evidence_id"
    ]
    compatibility_risks = option["causal_coverage"]["compatibility_risks"]
    material_risk = compatibility_risks[0]
    positive_contract = research["evidence_verification"]["outcome_oracles"][0][
        "positive_outcome_contracts"
    ][0]
    positive_contract_id = positive_contract["positive_outcome_contract_id"]
    review = bind_falsification_review(
        {
            "problem_id": research["problem_id"],
            "selected_option_id": option["option_id"],
            "verdict": "accept",
            "strongest_counterargument": (
                "The symptom could be formatting, or the fix could hide true policy blocks."
            ),
            "evidence_that_would_change_verdict": (
                "A retained input showing the wrong value before classification."
            ),
            "unsupported_assumptions": [],
            "residual_risks": [],
            "critical_findings": [],
            "evidence_refs": [
                {
                    "ref": evidence_id,
                    "finding": (
                        "The counter-case confines the intervention to the classifier boundary."
                    ),
                }
            ],
            "material_risk_dispositions": [
                {
                    "risk": material_risk,
                    "disposition": "mitigated",
                    "evidence_refs": [evidence_id],
                    "rationale": "The counter-case remains an explicit regression oracle.",
                }
            ],
            "selected_positive_outcome_contract_id": positive_contract_id,
            "outcome_contract_reviews": [
                {
                    "positive_outcome_contract_id": positive_contract_id,
                    "verdict": "sufficient",
                    "semantic_relation_assessment": (
                        "The grounded expected behavior reverses the source failure at the "
                        "same mechanism boundary."
                    ),
                    "proves_intended_operation": True,
                    "problem_coverage": "full",
                    "residual_untested_paths": [],
                    "evidence_refs": [evidence_id],
                }
            ],
        },
        problem_id=research["problem_id"],
        selected_option=option,
        research=research,
    )
    return {
        "case_id": research["case_id"],
        "problem_id": research["problem_id"],
        "selected_option_id": option["option_id"],
        "selected_family_id": option["family_id"],
        "selection_rationale": "It changes the proven mechanism without claiming wider scope.",
        "repo_intent_alignment": "Preserves honest lifecycle diagnostics.",
        "why_other_options_were_not_selected": "No broader mechanism has evidence.",
        "needs_ux_review": False,
        "causal_coverage_evaluation": {
            "mechanism_fit": "The intervention matches the runner-observed symbol.",
            "accepted_unsupported_assumptions": [],
            "accepted_residual_risks": [],
            "class_level_evidence_sufficient": False,
        },
        "falsification_review": review,
        "change_surface": {"user_visible": False, "kinds": ["internal_behavior"]},
    }


def _plan(
    research: dict[str, Any],
    option: dict[str, Any],
    *,
    create_integration_fixture: bool = False,
) -> dict[str, Any]:
    experiment = research["experiments"][0]
    binding = option["causal_coverage"]["research_binding"]
    intervention = binding["intervention_points"][0]
    command = experiment["command"]
    targets: list[dict[str, object]] = [
        {
            "action": "modify",
            "path": intervention["target_path"],
            "symbols": [intervention["target_symbol"]],
            "change": intervention["intervention"],
        }
    ]
    if create_integration_fixture:
        targets.append(
            {
                "action": "create",
                "path": "tests/fixtures/lifecycle_wrong_output.json",
                "symbols": ["retained_incomplete_run"],
                "change": "Add the retained integration fixture used by the replay oracle.",
            }
        )
    plan = {
        "change_plan_id": "plan:lifecycle:causal-boundary",
        "case_id": research["case_id"],
        "problem_id": research["problem_id"],
        "selected_option_id": option["option_id"],
        "title": "Correct incomplete-run lifecycle classification",
        "problem": "Incomplete runs are mislabeled as policy-blocked.",
        "user_impact": "Operators receive a false cause and take the wrong recovery action.",
        "proposed_fix": intervention["intervention"],
        "repo_revision": research["repo_revision"],
        "change_targets": targets,
        "target_contract": {
            "case_id": research["case_id"],
            "problem_id": research["problem_id"],
            "selected_option_id": option["option_id"],
            "repo_revision": research["repo_revision"],
            "targets": targets,
        },
        "implementation_steps": [
            "Update `lifecycle.classify_incomplete` at the verified decision boundary.",
            "Add the retained wrong-output fixture to the existing integration replay.",
        ],
        "verification_steps": [
            "Run the exact retained scenario and assert the original wrong value disappears."
        ],
        "verification_commands": [command],
        "outcome_verification_roles": {
            "original_scenario": {
                "description": "Replay the exact retained incomplete-run scenario.",
                "research_experiment_id": "exp-support",
                "commands": [command],
                "predicates": [
                    {"type": "command_exit_code", "command_index": 0, "equals": 0},
                    {
                        "type": "command_stdout_not_contains",
                        "command_index": 0,
                        "value": "classification=policy_block",
                    },
                    {
                        "type": "command_stdout_contains",
                        "command_index": 0,
                        "value": "classification=incomplete",
                    },
                ],
            },
            "live": None,
            "mitigation_effect": None,
            "recurrence": {
                "description": "Check later canonical-case cycles for recurrence.",
                "commands": [],
                "predicates": [],
            },
        },
        "before_after_reproduction": {
            "original_scenario": "Replay the retained incomplete-run metadata.",
            "research_experiment_id": "exp-support",
            "expected_outcome_state": "resolved",
            "before_change": {
                "command": command,
                "expected_exit_code": experiment["exit_code"],
                "expected_result": experiment["result"],
                "observable_assertion": experiment["observable_assertion"],
            },
            "after_change": {
                "command": command,
                "expected_exit_code": 0,
                "expected_result": "The original wrong classification is absent.",
                "observable_assertions": [
                    {
                        "source": "stdout",
                        "operator": "not_contains",
                        "expected": "classification=policy_block",
                    },
                    {
                        "source": "stdout",
                        "operator": "contains",
                        "expected": "classification=incomplete",
                    },
                ],
            },
            "proof_limitation": None,
            "alternate_verification": None,
        },
        "compatibility_and_failure_modes": {
            "preserved_behaviors": ["Verified policy blocks remain policy_block."],
            "intentional_changes": ["Missing completion metadata uses lifecycle state."],
            "failure_modes": ["Unknown states remain explicitly unknown."],
            "migration_required": False,
        },
        "causal_coverage": option["causal_coverage"],
        "scope_evidence": option["scope_evidence"],
        "requires_live_verification": False,
        "live_verification_rationale": "The classifier is fully exercised by the faithful replay.",
        "success_criteria": ["The wrong value is absent and classification=incomplete is emitted."],
        "rollback_notes": "Revert the classifier branch and retained fixture together.",
        "suggested_owner": "runner_core",
        "related_change_plan_ids": [],
    }
    plan = bind_plan_outcome_oracle(plan, research=research)
    return assign_plan_revision_id(plan)


def _source_problem_record(
    research: dict[str, Any],
    *,
    title: str,
    problem: str,
    user_impact: str,
) -> dict[str, Any]:
    """Build a stage-1 record and prove it retains the assigned source atom."""

    atom_ids = research["evidence_assignment"]["expected_atom_ids"]
    parsed, warnings = parse_problem_record_list(
        json.dumps(
            [
                {
                    "case_id": research["case_id"],
                    "problem_id": research["problem_id"],
                    "canonical_problem_id": research["problem_id"],
                    "case_member_problem_ids": [research["problem_id"]],
                    "title": title,
                    "problem": problem,
                    "user_impact": user_impact,
                    "severity": "high",
                    "confidence": 0.91,
                    "evidence_atom_ids": atom_ids,
                    "evidence_summary": (
                        "The assigned source atom records the exact observed symptom."
                    ),
                    "problem_status": "identified",
                }
            ]
        )
    )
    assert warnings == []
    assert parsed[0]["evidence_atom_ids"] == atom_ids
    return parsed[0]


def _run_production_research_acceptance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    material_unknowns: list[dict[str, object]] | None = None,
    repository_assertion: bool = True,
) -> tuple[dict[str, Any], list[float | None]]:
    """Run real stage-3 replay/receipt code around a deterministic fake agent report."""

    original_command = "python -m pytest -q --tb=native tests/test_core.py::test_reported_failure"

    def run_git(repository: Path, *args: str) -> str:
        completed = subprocess.run(
            ["git", *args],
            cwd=repository,
            check=True,
            capture_output=True,
            text=True,
        )
        return completed.stdout.strip()

    def write_json(path: Path, value: object) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")

    workspace = tmp_path / "owner"
    (workspace / "src").mkdir(parents=True)
    (workspace / "src" / "core.py").write_text(
        "def run(*, guarded=False, alternative=True):\n"
        "    if not guarded:\n"
        "        raise RuntimeError('reported failure')\n"
        "    return True\n",
        encoding="utf-8",
    )
    (workspace / "tests").mkdir()
    original_test_body = (
        "    assert run() is True\n\n" if repository_assertion else "    run()\n\n"
    )
    (workspace / "tests" / "test_core.py").write_text(
        "from src.core import run\n\n"
        f"def test_reported_failure():\n{original_test_body}"
        "def test_guarded_control():\n"
        "    assert run(guarded=True) is True\n\n"
        "def test_alternative_removed():\n"
        "    run(alternative=False)\n",
        encoding="utf-8",
    )
    (workspace / "repro.txt").write_text(
        "captured reproduction\n",
        encoding="utf-8",
    )
    run_git(workspace, "init")
    run_git(workspace, "config", "user.email", "tests@example.invalid")
    run_git(workspace, "config", "user.name", "Tests")
    run_git(workspace, "add", "-A")
    run_git(workspace, "commit", "-m", "reproduced problem")
    revision = run_git(workspace, "rev-parse", "HEAD")

    statement = "core.run raises instead of returning on its required default path."
    claims: dict[str, object] = {
        "research_schema_version": 3,
        "case_id": "case:production-research-acceptance",
        "problem_id": "problem:production-research-acceptance",
        "repo_revision": revision,
        "research_method": "reproduction",
        "reproduction_status": "reproduced",
        "research_status": "evidence_sufficient",
        "writes_used": False,
        "writes_purpose": ["none"],
        "implementation_performed": False,
        "diff_classification": "allowed_research_edits",
        "artifact_refs": [
            {"artifact_id": "artifact:repro", "kind": "repro", "path": "repro.txt"},
            {"artifact_id": "artifact:source", "kind": "source", "path": "src/core.py"},
        ],
        "experiments": [
            {
                "experiment_id": "experiment:original",
                "scenario_kind": "original_replay",
                "addresses_atom_ids": ["atom:origin"],
                "command": original_command,
                "result": "The original scenario fails at core.run.",
                "outcome": "supports",
                "exit_code": 1,
                "observable_assertion": {
                    "source": "exit_code",
                    "operator": "equals",
                    "expected": 1,
                },
                "artifact_refs": ["artifact:repro", "artifact:source"],
            },
            {
                "experiment_id": "experiment:control",
                "scenario_kind": "control",
                "control_relationship": {
                    "supports_experiment_id": "experiment:original",
                    "mechanism_symbols": ["core.run"],
                    "controlled_variable": "guard enabled",
                    "expected_difference": "The guarded call returns the expected value.",
                },
                "addresses_atom_ids": ["atom:origin"],
                "command": "python -m pytest -q tests/test_core.py::test_guarded_control",
                "result": "The guarded control passes.",
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
                "experiment_id": "experiment:challenge",
                "scenario_kind": "control",
                "control_relationship": {
                    "supports_experiment_id": "experiment:original",
                    "mechanism_symbols": ["core.run"],
                    "controlled_variable": "alternative input disabled",
                    "expected_difference": (
                        "The failure disappears only if that alternative is causal."
                    ),
                },
                "addresses_atom_ids": ["atom:origin"],
                "command": ("python -m pytest -q tests/test_core.py::test_alternative_removed"),
                "result": "The failure survives removal of the alternative.",
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
                "hypothesis_id": "hypothesis:default-path",
                "statement": statement,
                "supporting_evidence": [
                    "experiment:original",
                    "experiment:challenge",
                ],
                "counterevidence": ["experiment:control"],
                "mechanism_symbols": ["core.run"],
                "disposition": "primary",
                "disposition_evidence": [
                    "experiment:original",
                    "experiment:control",
                ],
                "falsification_attempts": [
                    {
                        "attempt_id": "attempt:alternative-cause",
                        "hypothesis_id": "hypothesis:default-path",
                        "claim": statement,
                        "baseline_experiment_id": "experiment:original",
                        "challenge_experiment_id": "experiment:challenge",
                        "disproof_condition": {
                            "source": "exit_code",
                            "operator": "equals",
                            "expected": 0,
                        },
                        "outcome": "survived",
                    }
                ],
            }
        ],
        "root_cause_confidence": 0.9,
        "broader_class_assessment": "unknown",
        "material_unknowns": list(material_unknowns or []),
        "blocking_reasons": [],
        "evidence_boundaries": [],
    }
    assert "evidence_verification" not in claims

    origin = tmp_path / "origin.json"
    origin.write_text('{"failure": true}\n', encoding="utf-8")
    atom = {
        "atom_id": "atom:origin",
        "text": "Default core.run raises instead of returning.",
        "command": original_command,
        "exit_code": 1,
        "evidence_role": "observation",
        "origin_stage": "runtime",
    }
    assignment: dict[str, object] = {
        "status": "complete",
        "errors": [],
        "case_id": claims["case_id"],
        "problem_id": claims["problem_id"],
        "expected_atom_ids": ["atom:origin"],
        "atom_receipts": [
            {
                "atom_id": "atom:origin",
                "atom_sha256": _canonical_sha256(atom),
                "atom_snapshot": atom,
                "artifact_receipts": [
                    {
                        "path": str(origin),
                        "sha256": sha256(origin.read_bytes()).hexdigest(),
                        "size_bytes": origin.stat().st_size,
                    }
                ],
            }
        ],
    }
    assignment["assignment_sha256"] = evidence_assignment_sha256(assignment)
    selected_problem = {
        "case_id": claims["case_id"],
        "problem_id": claims["problem_id"],
        "evidence_atoms": [{"atom_id": "atom:origin"}],
        "evidence_assignment": assignment,
    }

    guidance = tmp_path / "configs" / "backlog_stage_guidance" / "repro_research.md"
    guidance.parent.mkdir(parents=True)
    guidance.write_text("# Research the causal mechanism\n", encoding="utf-8")

    def fake_run_once(*, config: RunnerConfig, request: object) -> RunResult:
        del config
        assert request.keep_workspace is True
        run_dir = tmp_path / "research-agent-run"
        run_dir.mkdir()
        write_json(
            run_dir / "report.json",
            {
                "schema_version": 1,
                "kind": "troubleshoot_v1",
                "status": "success",
                "goal": "Establish the causal mechanism.",
                "failure_point": "core.run default path",
                "evidence": {"what_happened": "The default path raises."},
                "attempted_fixes": [],
                "recommended_fix_path": ["Correct the default return path."],
                "extensions": {"backlog_repro_research": claims},
            },
        )
        write_json(run_dir / "diff_numstat.json", [])
        write_json(
            run_dir / "target_ref.json",
            {"commit_sha": revision, "ref": request.ref},
        )
        write_json(run_dir / "workspace_ref.json", {"workspace_dir": str(workspace)})
        events = [
            {"type": "run_command", "data": {"command": original_command, "exit_code": 1}},
            {
                "type": "run_command",
                "data": {
                    "command": ("python -m pytest -q tests/test_core.py::test_alternative_removed"),
                    "exit_code": 1,
                },
            },
            {
                "type": "run_command",
                "data": {
                    "command": ("python -m pytest -q tests/test_core.py::test_guarded_control"),
                    "exit_code": 0,
                },
            },
            {
                "type": "read_file",
                "data": {
                    "path": "src/core.py",
                    "bytes": (workspace / "src" / "core.py").stat().st_size,
                    "read_source": "tool",
                    "source_exit_code": 0,
                    **observed_read_attestation(
                        path=workspace / "src" / "core.py",
                        observed_text=(workspace / "src" / "core.py").read_text(encoding="utf-8"),
                        source_exit_code=0,
                        allow_partial=True,
                    ),
                },
            },
        ]
        (run_dir / "normalized_events.jsonl").write_text(
            "\n".join(json.dumps(event) for event in events) + "\n",
            encoding="utf-8",
        )
        return RunResult(run_dir=run_dir, exit_code=0, report_validation_errors=[])

    monkeypatch.setattr(production_research_runner, "run_once", fake_run_once)
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")
    monkeypatch.setenv("PYTEST_ADDOPTS", "-p no:cacheprovider")

    class RecordingUnboundedExecutor:
        def __init__(self) -> None:
            self.timeouts: list[float | None] = []
            self.delegate = TrustedHostReplayExecutor(
                approved_source_roots=[workspace],
                source_identity=workspace,
            )

        def isolation_receipt(self, *, source_workspace: Path) -> dict[str, Any]:
            return self.delegate.isolation_receipt(source_workspace=source_workspace)

        def execute(
            self,
            argv: list[str],
            *,
            cwd: Path,
            source_workspace: Path,
            timeout_seconds: float | None,
        ) -> object:
            self.timeouts.append(timeout_seconds)
            assert timeout_seconds is None
            return self.delegate.execute(
                argv,
                cwd=cwd,
                source_workspace=source_workspace,
                timeout_seconds=timeout_seconds,
            )

    executor = RecordingUnboundedExecutor()
    config = RunnerConfig(
        repo_root=tmp_path,
        runs_dir=tmp_path / "runs",
        agents={},
        policies={},
    )
    result = production_research_runner.run_repro_research_stage(
        repo_root=tmp_path,
        repo_input=str(workspace),
        repo_ref="HEAD",
        target_slug="production_research_acceptance",
        selected_problems=[selected_problem],
        artifacts_dir=tmp_path / "compiled" / "backlog_artifacts",
        agent="codex",
        model=None,
        cfg=config,
        dry_run=False,
        replay_timeout_seconds=None,
        replay_executor=executor,
        replay_executor_metadata={
            "executor": "trusted_host",
            "approved_source_roots": [str(workspace.resolve())],
            "source_identity": str(workspace.resolve()),
        },
    )
    dossier = result["items"][0]
    persisted_path = tmp_path / "persisted-research.json"
    write_json(persisted_path, dossier)
    persisted = json.loads(persisted_path.read_text(encoding="utf-8"))
    return persisted, executor.timeouts


def test_production_research_runner_mints_persisted_ready_proof(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    persisted, timeouts = _run_production_research_acceptance(
        tmp_path,
        monkeypatch,
    )

    ready, reasons = assess_research_readiness(persisted)
    assert ready is True, reasons
    assert reasons == []
    assert verify_persisted_research_evidence(persisted) == (True, [])
    receipt = persisted["evidence_verification"]
    assert receipt["status"] == "verified"
    assert receipt["mechanism_evidence"]
    assert receipt["falsification_interventions"]
    assert receipt["outcome_oracles"]
    assert receipt["verified_mechanism_sha256"]
    assert receipt["verified_mechanism_provenance_sha256"]
    assert timeouts and set(timeouts) == {None}


def test_production_research_material_unknown_blocks_specific_progression_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    persisted, _ = _run_production_research_acceptance(
        tmp_path,
        monkeypatch,
        material_unknowns=[
            {
                "unknown": "A second caller may bypass the verified default-path boundary.",
                "affects": ["root_cause", "change_surface"],
                "evidence_needed": "Trace the second caller through the same mechanism.",
            }
        ],
    )

    ready, reasons = assess_research_readiness(persisted)
    assert ready is False
    assert "research_proof_invalid" not in reasons
    assert reasons == ["material_unknown_blocks_implementation_decision"]


def test_production_research_without_positive_contract_stays_research_required(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    persisted, _ = _run_production_research_acceptance(
        tmp_path,
        monkeypatch,
        repository_assertion=False,
    )

    assert verify_persisted_research_evidence(persisted) == (True, [])
    assert persisted["evidence_verification"]["status"] == "verified"
    assert persisted["evidence_verification"]["outcome_oracles"]
    assert persisted["evidence_verification"]["outcome_oracles"][0][
        "positive_outcome_contracts"
    ] == []
    ready, reasons = assess_research_readiness(persisted)
    assert ready is False
    assert "research_proof_invalid" not in reasons
    assert reasons == ["research_positive_outcome_contract_missing"]


def test_synthetic_wrong_output_contract_reaches_a_root_cause_plan() -> None:
    research = _verified_research_proof()
    research_ready, research_reasons = assess_research_readiness(research)
    assert research_ready is True, research_reasons

    option = _option(research)
    option_ready, option_reasons = assess_solution_option_readiness(option, research=research)
    assert option_ready is True, option_reasons

    selection = _selection(research, option)
    plan = _plan(research, option)
    problem = _source_problem_record(
        research,
        title="Incomplete runs are assigned the wrong cause",
        problem="A pure classifier maps incomplete metadata to policy_block.",
        user_impact="Operators receive a false cause and pursue the wrong recovery.",
    )
    ready, reasons = assess_ticket_readiness(
        {
            "problem_record": problem,
            "priority": {
                "case_id": research["case_id"],
                "problem_id": research["problem_id"],
                "priority_bucket": "p1",
                "selected_for_research": True,
                "priority_rationale": "The false diagnosis prevents correct recovery.",
                "priority_status": "prioritized",
            },
            "research": research,
            "solution_options": [option],
            "selected_solution": selection,
            "change_plan": plan,
        }
    )

    assert ready is True, reasons
    assert (
        plan["before_after_reproduction"]["before_change"]["observable_assertion"]
        == research["experiments"][0]["observable_assertion"]
    )
    assert plan["before_after_reproduction"]["after_change"]["observable_assertions"] == [
        {
            "source": "stdout",
            "operator": "not_contains",
            "expected": "classification=policy_block",
        },
        {
            "source": "exit_code",
            "operator": "equals",
            "expected": 0,
        },
        {
            "source": "stdout",
            "operator": "contains",
            "expected": "expected_behavior_confirmed",
        },
    ]


def test_action_create_through_an_inspected_boundary_reaches_ready_plan() -> None:
    research = _verified_research_proof(
        case_id="case:declared-adapter-missing",
        problem_id="problem:declared-adapter-missing",
        atom_id="run/adapter/codex/0:missing_capability:1",
        symbol="adapters.registry.load_declared",
        path="src/adapters/registry.py",
        support_command=("python .usertest_research/replay_registry.py --adapter native_probe"),
        assertion={
            "source": "stderr",
            "operator": "contains",
            "expected": "adapter_missing:native_probe",
        },
        primary_statement=(
            "The registry recognizes native_probe but its inspected delegation boundary "
            "has no implementation target."
        ),
        alternative_statement=("The adapter name is malformed before registry resolution."),
        atom_text=(
            "The registry accepts native_probe, then reports that its declared adapter "
            "implementation is missing."
        ),
    )
    hypothesis = research["root_cause_hypotheses"][0]
    evidence = research["evidence_verification"]["mechanism_evidence"][0]
    evidence_id = evidence["mechanism_evidence_id"]
    intervention = "Delegate the declared native_probe entry to its adapter implementation."
    option = {
        "option_id": "option:adapter:create-declared-target",
        "case_id": research["case_id"],
        "problem_id": research["problem_id"],
        "family_id": "most_direct",
        "summary": "Implement the one declared adapter missing behind the proven registry hook.",
        "tradeoffs": "Adds one implementation while preserving the existing registry contract.",
        "recurrence_prevention": (
            "The registry resolves the declared adapter through its normal delegation path."
        ),
        "change_surface_hypothesis": (
            "The inspected registry hook plus the missing native_probe target."
        ),
        "test_implications": "Replay the retained registry request and a present-adapter control.",
        "rationale": "The harness proves resolution reaches the existing hook before failing.",
        "causal_coverage": {
            "mechanism_addressed": hypothesis["statement"],
            "research_binding": {
                "hypothesis_id": "h1",
                "hypothesis_statement": hypothesis["statement"],
                "mechanism_symbols": hypothesis["mechanism_symbols"],
                "supporting_evidence_refs": ["exp-support", "exp-counter"],
                "counterevidence_refs": [],
                "falsification_attempt_refs": ["falsify-h1-complete-metadata"],
                "deterministic_closure_refs": [],
                "intervention_points": [
                    {
                        "mechanism_symbol": "adapters.registry.load_declared",
                        "target_path": "src/adapters/registry.py",
                        "target_symbol": "adapters.registry.load_declared",
                        "intervention": intervention,
                    }
                ],
            },
            "symptoms_covered": ["Declared native_probe adapter cannot be loaded"],
            "unsupported_assumptions": [],
            "residual_recurrence_paths": [],
            "compatibility_risks": ["Existing declared adapters must keep their resolution path."],
            "testability": {
                "before": "The retained request emits adapter_missing:native_probe.",
                "after": "The same request no longer emits the missing-adapter value.",
            },
        },
        "scope_evidence": {
            "scope_level": "single_path",
            "independent_consumers_or_failure_paths": [
                {
                    "name": evidence["path_name"],
                    "evidence_refs": [evidence_id],
                }
            ],
        },
    }
    selection = _selection(research, option)
    command = research["experiments"][0]["command"]
    targets = [
        {
            "action": "modify",
            "path": "src/adapters/registry.py",
            "symbols": ["adapters.registry.load_declared"],
            "change": intervention,
        },
        {
            "action": "create",
            "path": "src/adapters/native_probe.py",
            "symbols": ["run_native_probe"],
            "change": "Implement the declared native_probe adapter contract.",
            "rationale_kind": "causal_propagation",
            "rationale": ("The verified registry boundary delegates native_probe to this target."),
            "evidence_refs": [evidence_id],
            "integration_binding": {
                "path": "src/adapters/registry.py",
                "symbol": "adapters.registry.load_declared",
                "evidence_refs": [evidence_id],
                "relationship": (
                    "load_declared dispatches native_probe to the new adapter after lookup."
                ),
            },
        },
    ]
    plan = assign_plan_revision_id(
        {
            "change_plan_id": "plan:adapter:create-declared-target",
            "case_id": research["case_id"],
            "problem_id": research["problem_id"],
            "selected_option_id": option["option_id"],
            "title": "Implement the declared native_probe adapter",
            "problem": "The registry resolves native_probe to a missing implementation.",
            "user_impact": "A declared capability cannot be used.",
            "proposed_fix": (
                "Add the adapter at the exact inspected registry delegation boundary."
            ),
            "repo_revision": research["repo_revision"],
            "change_targets": targets,
            "target_contract": {
                "case_id": research["case_id"],
                "problem_id": research["problem_id"],
                "selected_option_id": option["option_id"],
                "repo_revision": research["repo_revision"],
                "targets": targets,
            },
            "implementation_steps": [
                "Add `run_native_probe` with the contract consumed by the registry.",
                "Update `adapters.registry.load_declared` to delegate native_probe.",
            ],
            "verification_steps": [
                "Replay the retained registry request and assert the missing value is absent."
            ],
            "verification_commands": [command],
            "outcome_verification_roles": {
                "original_scenario": {
                    "description": "Replay the retained native_probe registry request.",
                    "research_experiment_id": "exp-support",
                    "commands": [command],
                    "predicates": [
                        {"type": "command_exit_code", "command_index": 0, "equals": 0},
                        {
                            "type": "command_stderr_not_contains",
                            "command_index": 0,
                            "value": "adapter_missing:native_probe",
                        },
                        {
                            "type": "command_stdout_contains",
                            "command_index": 0,
                            "value": "adapter_loaded:native_probe",
                        },
                    ],
                },
                "live": None,
                "mitigation_effect": None,
                "recurrence": {
                    "description": "Check later canonical-case cycles for recurrence.",
                    "commands": [],
                    "predicates": [],
                },
            },
            "before_after_reproduction": {
                "original_scenario": "Replay the retained native_probe request.",
                "research_experiment_id": "exp-support",
                "expected_outcome_state": "resolved",
                "before_change": {
                    "command": command,
                    "expected_exit_code": 0,
                    "expected_result": research["experiments"][0]["result"],
                    "observable_assertion": research["experiments"][0]["observable_assertion"],
                },
                "after_change": {
                    "command": command,
                    "expected_exit_code": 0,
                    "expected_result": "The registry no longer reports a missing adapter.",
                    "observable_assertions": [
                        {
                            "source": "stderr",
                            "operator": "not_contains",
                            "expected": "adapter_missing:native_probe",
                        },
                        {
                            "source": "stdout",
                            "operator": "contains",
                            "expected": "adapter_loaded:native_probe",
                        },
                    ],
                },
                "proof_limitation": None,
                "alternate_verification": None,
            },
            "compatibility_and_failure_modes": {
                "preserved_behaviors": ["Existing declared adapters resolve unchanged."],
                "intentional_changes": ["native_probe now resolves to an implementation."],
                "failure_modes": ["Unknown adapter names remain explicit failures."],
                "migration_required": False,
            },
            "causal_coverage": option["causal_coverage"],
            "scope_evidence": option["scope_evidence"],
            "requires_live_verification": False,
            "live_verification_rationale": (
                "The pure registry dispatch is covered by the faithful replay."
            ),
            "success_criteria": [
                "The missing value is absent and adapter_loaded:native_probe is emitted."
            ],
            "rollback_notes": "Remove native_probe and its registry delegation together.",
            "suggested_owner": "agent_adapters",
            "related_change_plan_ids": [],
        }
    )
    plan = assign_plan_revision_id(bind_plan_outcome_oracle(plan, research=research))
    problem = _source_problem_record(
        research,
        title="Declared adapter implementation is absent",
        problem="Registry lookup reaches a declared but absent adapter target.",
        user_impact="A declared capability cannot be used.",
    )
    ready, reasons = assess_ticket_readiness(
        {
            "problem_record": problem,
            "priority": {
                "case_id": research["case_id"],
                "problem_id": research["problem_id"],
                "priority_bucket": "p1",
                "selected_for_research": True,
                "priority_rationale": "The declared capability is unusable.",
                "priority_status": "prioritized",
            },
            "research": research,
            "solution_options": [option],
            "selected_solution": selection,
            "change_plan": plan,
        }
    )

    assert ready is True, reasons
    created = [target for target in plan["change_targets"] if target["action"] == "create"]
    assert created == [targets[1]]
    assert created[0]["integration_binding"]["symbol"] == ("adapters.registry.load_declared")


def test_non_python_config_static_trace_has_a_supported_ready_path() -> None:
    symbol = "config:/backlog/lifecycle/default_state"
    path = "configs/backlog_lifecycle.yaml"
    static_trace = {
        "deterministic": True,
        "environment_dependencies": [],
        "code_path": [
            {
                "path": path,
                "symbol": symbol,
                "observation": "The literal policy_block value is selected for missing state.",
            }
        ],
    }
    research = _verified_research_proof(
        case_id="case:config-default",
        problem_id="problem:config-default",
        atom_id="run/config/codex/0:wrong_output:1",
        symbol=symbol,
        path=path,
        evidence_type="static_trace",
        scenario_kind="static_trace",
        support_command="python tools/trace_config.py configs/backlog_lifecycle.yaml",
        assertion={
            "source": "stdout",
            "operator": "contains",
            "expected": "default_state=policy_block",
        },
        mechanism_link=(
            lambda link: {
                **link,
                "static_trace_sha256": _canonical_sha256(link),
            }
        )(
            {
                "verification_method": "runner_deterministic_static_trace_v1",
                "entrypoint": symbol,
                "code_path": static_trace["code_path"],
                "environment_dependencies": [],
            }
        ),
        harness_path=None,
        consumer_kind="config_consumer",
        research_method="static_trace",
        reproduction_status="reproduction_failed",
        static_trace=static_trace,
    )

    ready, reasons = assess_research_readiness(research)

    assert ready is True, reasons


def test_derived_verification_research_updates_parent_without_false_recurrence() -> None:
    parent_case = {
        "case_id": "case:verification-paths",
        "problem_id": "problem:verification-paths",
        "canonical_problem_id": "problem:verification-paths",
        "case_member_problem_ids": ["problem:verification-paths"],
        "evidence_atom_ids": ["atom:origin"],
    }
    derived = normalize_atom_lineage(
        [
            {
                "atom_id": "run/research/codex/0:confusion_point:1",
                "run_id": "research/codex/0",
                "source": "confusion_point",
                "severity_hint": "high",
                "mission_id": "backlog_repro_research",
                "parent_problem_id": "problem:verification-paths",
                "derived_from_atom_ids": ["atom:origin"],
            }
        ],
        case_registry={
            "problem_id_to_case_id": {"problem:verification-paths": "case:verification-paths"},
            "atom_id_to_case_id": {"atom:origin": "case:verification-paths"},
            "ticket_fingerprint_to_case_id": {},
        },
        strict_new_output=True,
    )

    updated = attach_supporting_atoms_to_problem_cases([parent_case], derived)

    assert eligible_problem_mining_atoms(derived) == []
    assert updated[0]["derived_evidence_atom_ids"] == ["run/research/codex/0:confusion_point:1"]
    assert updated[0]["case_id"] == parent_case["case_id"]


def test_consolidated_research_remains_ready_and_retains_every_outcome_oracle() -> None:
    proof_a = _verified_research_proof(
        case_id="case:consolidated-a",
        problem_id="problem:consolidated-a",
        atom_id="atom:consolidated-a",
    )
    proof_b = _verified_research_proof(
        case_id="case:consolidated-b",
        problem_id="problem:consolidated-b",
        atom_id="atom:consolidated-b",
    )
    proofs = [proof_a, proof_b]
    registry_cases: dict[str, object] = {}
    for proof in proofs:
        verification = proof["evidence_verification"]
        registry_cases[str(proof["case_id"])] = {
            "case_id": proof["case_id"],
            "root_cause_status": "established",
            "verified_mechanism": verification["verified_mechanism"],
            "verified_mechanism_sha256": verification[
                "verified_mechanism_sha256"
            ],
            "verified_mechanism_provenance": verification[
                "verified_mechanism_provenance"
            ],
            "verified_mechanism_provenance_sha256": verification[
                "verified_mechanism_provenance_sha256"
            ],
            "verified_mechanism_receipt_sha256": verification["receipt_sha256"],
            "verified_mechanism_source": "runner_research_evidence_verification_v1",
        }
    problems = [
        {
            "case_id": proof["case_id"],
            "problem_id": proof["problem_id"],
            "title": "Same lifecycle mechanism",
            "problem": "The same verified lifecycle branch emits the wrong result.",
            "user_impact": "The workflow cannot recover correctly.",
            "evidence_atom_ids": [proof["evidence_assignment"]["expected_atom_ids"][0]],
            "source_evidence_atom_ids": [
                proof["evidence_assignment"]["expected_atom_ids"][0]
            ],
            "canonical_symptoms": [f"symptom:{proof['case_id']}"],
        }
        for proof in proofs
    ]
    priorities = [
        {
            "case_id": proof["case_id"],
            "problem_id": proof["problem_id"],
            "priority_bucket": "p1",
            "selected_for_research": True,
            "priority_status": "prioritized",
            "priority_rationale": "Retain the verified case.",
        }
        for proof in proofs
    ]

    collapsed = collapse_post_research_verified_mechanisms(
        problem_records=problems,
        priority_decisions=priorities,
        research_dossiers=proofs,
        case_registry={"schema_version": 1, "cases": registry_cases},
        verify_dossier=lambda dossier: assess_research_readiness(dossier),
    )

    assert len(collapsed["research_dossiers"]) == 1
    canonical = collapsed["research_dossiers"][0]
    ready, reasons = assess_research_readiness(canonical)
    assert ready is True, reasons
    assert len(verified_outcome_oracles(canonical)) == 2
    assert len(
        canonical["post_research_same_mechanism_bundle"]["member_research_dossiers"]
    ) == 2
    tampered = json.loads(json.dumps(canonical))
    tampered["post_research_same_mechanism_bundle"]["member_research_dossiers"][1][
        "problem_id"
    ] = "problem:tampered"
    tampered_ready, tampered_reasons = assess_research_readiness(tampered)
    assert tampered_ready is False
    assert any("research_post_relation_bundle_hash_invalid" in value for value in tampered_reasons)


def test_partial_apply_patch_stays_in_research_instead_of_surface_planning() -> None:
    ready, reasons = assess_research_readiness(
        {
            "problem_id": "problem:apply-patch-context",
            "research_status": "insufficient_evidence",
            "reproduction_status": "partial",
            "material_unknowns": [
                {
                    "unknown": "The rejection mechanism has not been isolated.",
                    "affects": ["root_cause", "change_surface"],
                    "evidence_needed": "Replay the exact patch at the parser boundary.",
                }
            ],
        }
    )

    assert ready is False
    assert "research_proof_invalid" in reasons
