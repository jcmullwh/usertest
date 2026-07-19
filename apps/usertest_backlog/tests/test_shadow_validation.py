from __future__ import annotations

import json
from copy import deepcopy
from hashlib import sha256
from pathlib import Path

import pytest
from backlog_core import assign_plan_revision_id
from backlog_core.case_lineage import apply_atom_disposition_decision
from backlog_repo import write_case_relation_receipt

import usertest_backlog.workflows.qualification as qualification_mod
import usertest_backlog.workflows.shadow_validation as shadow_mod
from usertest_backlog.commands.export_tickets import _pipeline_source_config_bindings
from usertest_backlog.workflows.problem_mining_evidence import (
    build_problem_mining_evidence_draft,
    finalize_problem_mining_evidence_receipt,
    problem_mining_evidence_receipt_ref,
)
from usertest_backlog.workflows.qualification import (
    build_no_actionable_evidence_receipt,
    build_qualification_corpus_manifest,
    build_qualification_output_adjudication,
)
from usertest_backlog.workflows.shadow_validation import (
    evaluate_shadow_invariants,
    normalize_shadow_gate_config,
    operational_shadow_pending_run_path,
    shadow_pending_run_path,
    shadow_state_path,
    validate_pending_operational_shadow_run,
    validate_pending_shadow_run,
    validate_shadow_export_state,
    write_pending_operational_shadow_run,
    write_pending_shadow_run,
)
from usertest_backlog.workflows.shadow_validation import (
    record_shadow_cycle as _record_shadow_cycle_impl,
)


def test_runtime_compatibility_anchor_ignores_unrelated_target_source_drift(
    tmp_path: Path,
) -> None:
    bundle_path = tmp_path / "qualification_input_bundle.json"
    bundle_path.write_text(
        json.dumps(
            {
                "pipeline": {
                    "runtime_compatibility_sha256": "a" * 64,
                }
            }
        )
        + "\n",
        encoding="utf-8",
    )

    def receipts(unrelated_sha: str) -> list[dict[str, object]]:
        return [
            {
                "name": "qualification.input_bundle",
                "source_path": str(bundle_path),
                "snapshot_path": None,
                "exists": True,
                "sha256": sha256(bundle_path.read_bytes()).hexdigest(),
                "content_sha256": sha256(bundle_path.read_bytes()).hexdigest(),
                "size_bytes": bundle_path.stat().st_size,
            },
            {
                "name": "pipeline.manifest:apps/usertest/src/usertest/cli.py",
                "source_path": str(tmp_path / "unrelated.py"),
                "snapshot_path": None,
                "exists": True,
                "sha256": unrelated_sha,
                "content_sha256": unrelated_sha,
                "size_bytes": 10,
            },
        ]

    assert shadow_mod._stability_input_projection(receipts("b" * 64)) == (
        shadow_mod._stability_input_projection(receipts("c" * 64))
    )

    bundle_path.write_text(
        json.dumps(
            {
                "pipeline": {
                    "runtime_compatibility_sha256": "d" * 64,
                }
            }
        )
        + "\n",
        encoding="utf-8",
    )
    assert shadow_mod._stability_input_projection(receipts("c" * 64)) == [
        {"name": "pipeline.runtime_compatibility", "sha256": "d" * 64}
    ]


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def _target_contract(
    plan: dict[str, object],
    *,
    schema_version: int = 3,
) -> dict[str, object]:
    targets: list[dict[str, object]] = []
    for raw in plan["change_targets"]:
        target = dict(raw)
        change = str(target["change"])
        contract_target: dict[str, object] = {
            "action": target["action"],
            "path": target["path"],
            "symbols": target.get("symbols", []),
            "change": change,
            "change_sha256": sha256(change.encode()).hexdigest(),
            "target_role": "test" if str(target["path"]).startswith("tests/") else "production",
        }
        if schema_version == 3:
            contract_target["destination_path"] = target.get("destination_path")
        targets.append(contract_target)
    payload: dict[str, object] = {
        "schema_version": schema_version,
        "contract_source": f"runner_stage6_target_intent_v{schema_version}",
        "case_id": plan["case_id"],
        "problem_id": plan["problem_id"],
        "selected_option_id": plan["selected_option_id"],
        "repo_revision": plan["repo_revision"],
        "targets": targets,
    }
    contract_sha256 = sha256(
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode()
    ).hexdigest()
    return {**payload, "contract_sha256": contract_sha256}


def _relation_review_artifacts(
    tmp_path: Path,
    *,
    name: str,
    decisions: list[dict[str, object]],
    relations: list[dict[str, object]],
) -> dict[str, str]:
    response_path = tmp_path / f"{name}.response.json"
    _write_json(response_path, decisions)
    requested_receipt_path = tmp_path / f"{name}.relations.json"
    payload, _ = write_case_relation_receipt(
        requested_receipt_path,
        stage="problem_mining",
        relation_review_response_path=response_path,
        relations=relations,
    )
    immutable_receipt_path = requested_receipt_path.with_name(
        f"{requested_receipt_path.stem}.{payload['content_sha256'][:16]}"
        f"{requested_receipt_path.suffix}"
    )
    return {
        "relation_review_response": str(response_path),
        "relation_review_receipt": str(immutable_receipt_path),
    }


def _decided_atom(atom: dict[str, object], *, rationale: str) -> dict[str, object]:
    return apply_atom_disposition_decision(
        atom,
        disposition=str(atom["disposition"]),
        source="atom_action_ledger",
        rationale=rationale,
    )


def _proposal_noise_proof(atom_id: str) -> dict[str, object]:
    support = {
        "field": "$.evidence_class",
        "value": "proposal",
        "value_sha256": sha256(
            json.dumps(
                "proposal",
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
        ).hexdigest(),
    }
    proof: dict[str, object] = {
        "schema_version": 1,
        "producer": "usertest_backlog.problem_mining",
        "proof_kind": "runner_expected_noise_rule_v1",
        "rule_id": "proposal_evidence_class_v1",
        "rule_version": 1,
        "atom_id": atom_id,
        "support": support,
    }
    proof["proof_sha256"] = sha256(
        json.dumps(
            proof,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()
    return proof


def _cycle_artifacts(tmp_path: Path) -> dict[str, Path]:
    paths: dict[str, Path] = {}
    for name in (
        "atoms",
        "problem_records",
        "problem_mining_evidence",
        "prioritized_problems",
        "research",
        "solution_options",
        "solution_selection",
        "change_plans",
        "case_registry",
    ):
        path = tmp_path / "cycle-inputs" / f"{name}.json"
        if not path.exists():
            _write_json(path, {"artifact": name})
        paths[name] = path
    return paths


def _record_cycle(tmp_path: Path, **kwargs: object) -> dict[str, object]:
    invariant_report = kwargs.get("invariant_report")
    if isinstance(invariant_report, dict):
        invariant_report = dict(invariant_report)
        invariant_report.setdefault("export_projection_sha256", "e" * 64)
        kwargs["invariant_report"] = invariant_report
    return record_shadow_cycle(
        **kwargs,  # type: ignore[arg-type]
        artifact_paths=_cycle_artifacts(tmp_path),
    )


def record_shadow_cycle(**kwargs: object) -> dict[str, object]:
    """Give non-qualification state tests an explicit positive release fixture."""

    report_raw = kwargs.get("invariant_report")
    if isinstance(report_raw, dict):
        report = deepcopy(report_raw)
        report.setdefault("cycle_mode", "release")
        report.setdefault("schema_version", 4)
        qualification_raw = report.get("qualification")
        qualification = deepcopy(qualification_raw) if isinstance(qualification_raw, dict) else None
        if (
            report.get("passed") is True
            and report.get("cycle_mode") == "release"
            and isinstance(qualification, dict)
            and qualification.get("status") == "missing"
            and qualification.get("qualification_class") == "unqualified"
        ):
            policy = qualification.get("policy")
            counts = qualification.get("counts")
            assert isinstance(policy, dict)
            assert isinstance(counts, dict)
            policy["positive_throughput_required"] = True
            counts["actionable_cases"] = 1
            counts["positive_qualifying_corpus"] = 1
            counts["exhausted_corpus"] = 0
            qualification["status"] = "verified"
            qualification["qualification_class"] = "positive_throughput"
            qualification["failures"] = []
            qualification["correction_routing_status"] = "not_required"
            report["qualification"] = qualification
            kwargs["invariant_report"] = report
    return _record_shadow_cycle_impl(**kwargs)  # type: ignore[arg-type]


def _proof_basis_dossier() -> dict[str, object]:
    image_id = "sha256:" + "1" * 64
    isolation = {
        "executor": "docker",
        "os_sandbox": True,
        "network": "none",
        "filesystem_isolation": "dedicated_clone_bind_mount",
        "trust_decision": "explicit_image",
        "trust_reason": "mutable-image:latest",
        "source_workspace": "C:/volatile/research-workspace",
        "sanitized_environment_keys": ["CI"],
    }
    verified_mechanism = {
        "schema_version": 2,
        "mechanism_symbols": ["core.run"],
        "code_paths": [{"symbol": "core.run", "path": "src/core.py"}],
    }
    verified_mechanism_sha256 = shadow_mod._canonical_hash(verified_mechanism)
    verified_mechanism_provenance = {
        "schema_version": 1,
        "primary_hypothesis_id": "h1",
        "research_probe_control_points": [
            {
                "verification_method": "python_ast_explicit_argument_delta_v1",
                "mechanism_symbols": ["core.run"],
                "mechanism_symbol": "core.run",
                "slot": "keyword:guard",
            }
        ],
    }
    return {
        "case_id": "case:proof",
        "problem_id": "problem:proof",
        "repo_revision": "a" * 40,
        "evidence_assignment": {
            "status": "complete",
            "errors": [],
            "case_id": "case:proof",
            "problem_id": "problem:proof",
            "expected_atom_ids": ["atom:proof"],
            "assignment_sha256": "2" * 64,
            "atom_receipts": [
                {
                    "atom_id": "atom:proof",
                    "atom_sha256": "3" * 64,
                    "atom_snapshot": {"atom_id": "atom:proof", "text": "failure"},
                    "artifact_receipts": [
                        {
                            "path": "C:/runs/origin/report.json",
                            "sha256": "4" * 64,
                            "size_bytes": 42,
                        }
                    ],
                }
            ],
        },
        "evidence_verification": {
            "verification_method": "runner_artifact_binding_v1",
            "status": "verified",
            "repo_revision": "a" * 40,
            "requested_repo_ref": "origin/dev",
            "resolved_repo_ref": "a" * 40,
            "workspace_dir": "C:/volatile/research-workspace",
            "workspace_head": "a" * 40,
            "run_dir": "C:/volatile/run",
            "planning_workspace_head": "a" * 40,
            "planning_workspace_clean": True,
            "workspace_overlay": {
                "baseline_manifest_sha256": "5" * 64,
                "research_manifest_sha256": "6" * 64,
                "baseline_state_sha256": "7" * 64,
                "research_state_sha256": "8" * 64,
                "baseline_git_index_sha256": "9" * 64,
                "research_git_index_sha256": "a" * 64,
                "research_overlay_manifest_sha256": "b" * 64,
                "git_index_changed": False,
                "changed_baseline_paths": [],
                "research_overlay_paths": [".usertest_research/repro.txt"],
                "suspicious_extra_paths": [],
                "research_overlay_manifest": {
                    ".usertest_research/repro.txt": {
                        "kind": "file",
                        "sha256": "c" * 64,
                        "size_bytes": 12,
                    }
                },
            },
            "replay_isolation": isolation,
            "origin_atom_ids": ["atom:proof"],
            "assignment_sha256": "2" * 64,
            "verified_mechanism": verified_mechanism,
            "verified_mechanism_sha256": verified_mechanism_sha256,
            "verified_mechanism_provenance": verified_mechanism_provenance,
            "verified_mechanism_provenance_sha256": shadow_mod._canonical_hash(
                verified_mechanism_provenance
            ),
            "artifacts": [
                {
                    "artifact_id": "artifact:repro",
                    "kind": "test_output",
                    "declared_path": "C:/volatile/run/artifacts/repro.txt",
                    "path": "C:/volatile/run/artifacts/repro.txt",
                    "sha256": "d" * 64,
                    "size_bytes": 20,
                },
                {
                    "artifact_id": "runner:codex_subscription_auth",
                    "kind": "codex_subscription_auth",
                    "declared_path": "C:/volatile/run/codex_subscription_auth.json",
                    "path": "C:/volatile/run/codex_subscription_auth.json",
                    "sha256": "e" * 64,
                    "size_bytes": 128,
                },
            ],
            "experiments": [
                {
                    "experiment_id": "exp-support",
                    "command": "pdm run pytest tests/test_repro.py -q",
                    "executed_argv": [
                        "pdm",
                        "run",
                        "pytest",
                        "tests/test_repro.py",
                        "-q",
                    ],
                    "exit_code": 1,
                    "scenario_kind": "original_replay",
                    "addresses_atom_ids": ["atom:proof"],
                    "outcome": "supports",
                    "workspace_dir": "C:/volatile/replay-workspace",
                    "workspace_head": "a" * 40,
                    "baseline_state_sha256": "e" * 64,
                    "pre_replay_state_sha256": "f" * 64,
                    "post_replay_state_sha256": "f" * 64,
                    "post_replay_mutations": False,
                    "overlay_manifest_sha256": "0" * 64,
                    "execution_isolation": isolation,
                    "execution_metadata": {
                        "executor": "docker",
                        "backend": "docker",
                        "image_tag": "mutable-image:latest",
                        "image_hash": "docker-context-hash",
                        "image_id": image_id,
                        "network": "none",
                        "container_name": "volatile-container-name",
                        "cleanup_attempted": True,
                        "cleanup_confirmed": True,
                    },
                    "stdout_path": "C:/volatile/replay/stdout.txt",
                    "stderr_path": "C:/volatile/replay/stderr.txt",
                    "stdout_sha256": "1" * 64,
                    "stderr_sha256": "2" * 64,
                    "observable_assertion": {
                        "source": "exit_code",
                        "operator": "equals",
                        "expected": 1,
                    },
                    "assertion_passed": True,
                    "artifact_refs": ["C:/volatile/run/artifacts/repro.txt"],
                }
            ],
            "inspected_files": [
                {
                    "path": "src/core.py",
                    "sha256": "3" * 64,
                    "git_blob_sha": "4" * 40,
                    "size_bytes": 50,
                    "bytes_observed": 50,
                    "whole_file_observed": True,
                    "observed_content_sha256": "3" * 64,
                    "observed_start_line": 1,
                    "observed_end_line": 5,
                }
            ],
            "inspected_symbols": [{"symbol": "core.run", "path": "src/core.py"}],
            "hypothesis_refs": [
                {
                    "hypothesis_id": "h1",
                    "supporting_refs": ["exp-support"],
                    "counterevidence_refs": ["exp-control"],
                    "mechanism_symbols": ["core.run"],
                    "control_links": [
                        {
                            "control_experiment_id": "exp-control",
                            "supports_experiment_id": "exp-support",
                            "mechanism_symbols": ["core.run"],
                            "shared_atom_ids": ["atom:proof"],
                            "shared_artifact_refs": ["C:/volatile/run/artifacts/repro.txt"],
                            "controlled_variable": "guard enabled",
                            "expected_difference": "control passes",
                        }
                    ],
                }
            ],
            "causal_links": [
                {
                    "hypothesis_id": "h1",
                    "experiment_id": "exp-support",
                    "symbol": "core.run",
                    "path": "src/core.py",
                    "stream": "stderr",
                    "trace_kind": "python_traceback",
                    "trace_excerpt_sha256": "5" * 64,
                    "stream_sha256": "2" * 64,
                }
            ],
            "control_verifications": [
                {
                    "hypothesis_id": "h1",
                    "control_verification_id": "control:case-local-id",
                    "verification_method": "pytest_ast_controlled_difference_v2",
                    "mechanism_symbols": ["core.run"],
                    "controlled_input_difference": {
                        "verification_method": "python_ast_explicit_argument_delta_v1",
                        "difference_count": 1,
                        "difference": {
                            "mechanism_symbol": "core.run",
                            "slot": "keyword:guard",
                            "difference_kind": "changed",
                            "support_argument": {
                                "slot": "keyword:guard",
                                "expression": "False",
                                "ast_sha256": "6" * 64,
                            },
                            "control_argument": {
                                "slot": "keyword:guard",
                                "expression": "True",
                                "ast_sha256": "7" * 64,
                            },
                        },
                    },
                    "observable_difference": {
                        "verification_method": "runner_replay_complement_v1",
                        "source": "exit_code",
                        "difference_kind": "failing_exit_to_zero",
                        "support": {
                            "exit_code": 1,
                            "observed_sha256": "8" * 64,
                        },
                        "control": {
                            "exit_code": 0,
                            "observed_sha256": "9" * 64,
                        },
                    },
                    "relationship_sha256": "a" * 64,
                }
            ],
            "falsification_interventions": [],
            "deterministic_mechanism_closures": [],
            "atom_bindings": [
                {
                    "experiment_id": "exp-support",
                    "atom_id": "atom:proof",
                    "match_kind": "symptom_text",
                }
            ],
        },
    }


def _proof_basis_sha256(dossier: dict[str, object]) -> str:
    projection, errors = shadow_mod._research_proof_basis_projection([dossier])
    assert errors == []
    return shadow_mod._canonical_hash(projection)


def _passing_inputs(tmp_path: Path) -> dict[str, object]:
    relation_response = tmp_path / "relation.json"
    _write_json(relation_response, [])
    atoms = [
        _decided_atom(
            {
                "atom_id": "atom:high",
                "severity_hint": "high",
                "evidence_role": "observation",
                "evidence_class": "observed_failure",
                "source": "run_failure",
                "text": "The original automated run failed before producing its result.",
                "disposition": "supports_case",
                "case_id": "case:one",
            },
            rationale="The retained observed failure supports the active canonical case.",
        )
    ]
    evidence_draft = build_problem_mining_evidence_draft(
        atoms=atoms,
        eligible_atoms=[],
        mode="live",
    )
    evidence_receipt_path = tmp_path / "problem_mining_evidence.json"
    evidence_receipt = finalize_problem_mining_evidence_receipt(
        draft=evidence_draft,
        atoms=atoms,
        receipt_path=evidence_receipt_path,
    )
    return {
        # Most tests below isolate an existing invariant. Throughput-specific tests
        # override this fixture setting and exercise the repository default contract.
        "qualification_contract": {
            "require_nonempty_throughput": False,
            "fail_on_systemic_research_blockers": False,
        },
        "backlog": {
            "tickets": [
                {
                    "case_id": "case:one",
                    "problem_id": "problem:one",
                    "stage": "research_required",
                    "evidence_atom_ids": ["atom:high"],
                }
            ]
        },
        "atoms": atoms,
        "stage1": {
            "items": [
                {
                    "case_id": "case:one",
                    "problem_id": "problem:one",
                    "case_member_problem_ids": ["problem:one"],
                    "evidence_atom_ids": ["atom:high"],
                }
            ],
            "input_meta": {
                "relation_review_decision_count": 0,
                "problem_mining_evidence_receipt": problem_mining_evidence_receipt_ref(
                    receipt=evidence_receipt,
                    receipt_path=evidence_receipt_path,
                ),
            },
            "artifacts": {
                "relation_review_response": str(relation_response),
                "problem_mining_evidence_receipt": str(evidence_receipt_path),
            },
        },
        "stage2": {
            "items": [
                {
                    "case_id": "case:one",
                    "problem_id": "problem:one",
                    "priority_bucket": "p1",
                    "selected_for_research": True,
                    "priority_rationale": "The observed failure is retained for causal research.",
                }
            ]
        },
        "stage3": {
            "items": [
                {
                    "case_id": "case:one",
                    "problem_id": "problem:one",
                    "research_status": "blocked",
                    "blocking_reasons": [
                        "The faithful external runtime needed for the original replay "
                        "is unavailable."
                    ],
                    "material_unknowns": [
                        "Whether the runtime failure reproduces at the recorded revision."
                    ],
                }
            ]
        },
        "stage4": {"items": [], "input_meta": {"optioning_outcomes": []}},
        "stage5": {"items": [], "input_meta": {"selection_outcomes": []}},
        "stage6": {"items": []},
        "case_registry": {
            "schema_version": 1,
            "cases": {
                "case:one": {
                    "state": "active",
                    "canonical_problem_id": "problem:one",
                    "problem_ids": ["problem:one"],
                    "evidence_atom_ids": ["atom:high"],
                }
            },
            "problem_id_to_case_id": {"problem:one": "case:one"},
            "atom_id_to_case_id": {"atom:high": "case:one"},
            "ticket_fingerprint_to_case_id": {},
        },
    }


def _complete_conservation_inputs(tmp_path: Path) -> dict[str, object]:
    inputs = _passing_inputs(tmp_path)
    base_stage1 = inputs["stage1"]
    inputs["stage1"] = {
        "items": [
            {
                "case_id": "case:one",
                "problem_id": "problem:one",
                "case_member_problem_ids": ["problem:one"],
                "evidence_atom_ids": ["atom:high"],
            }
        ],
        "input_meta": dict(base_stage1["input_meta"]),
        "artifacts": {
            "problem_mining_evidence_receipt": base_stage1["artifacts"][
                "problem_mining_evidence_receipt"
            ]
        },
    }
    inputs["stage2"] = {
        "items": [
            {
                "case_id": "case:one",
                "problem_id": "problem:one",
                "priority_bucket": "p1",
                "selected_for_research": True,
                "priority_rationale": "The observed failure has high impact.",
            }
        ]
    }
    inputs["stage3"] = {
        "items": [
            {
                "case_id": "case:one",
                "problem_id": "problem:one",
                "research_status": "evidence_sufficient",
            }
        ]
    }
    inputs["stage4"] = {
        "items": [
            {
                "case_id": "case:one",
                "problem_id": "problem:one",
                "option_id": "option:one",
            }
        ],
        "input_meta": {
            "optioning_outcomes": [
                {
                    "case_id": "case:one",
                    "problem_id": "problem:one",
                    "optioning_status": "options_produced",
                    "decision_rationale": "One mechanism is supported.",
                }
            ]
        },
    }
    inputs["stage5"] = {
        "items": [
            {
                "case_id": "case:one",
                "problem_id": "problem:one",
                "selected_option_id": "option:one",
            }
        ],
        "input_meta": {
            "selection_outcomes": [
                {
                    "case_id": "case:one",
                    "problem_id": "problem:one",
                    "selection_status": "selected",
                    "selected_option_id": "option:one",
                }
            ]
        },
    }
    inputs["stage6"] = {
        "items": [
            {
                "case_id": "case:one",
                "problem_id": "problem:one",
                "plan_revision_id": "plan:one",
            }
        ],
        "input_meta": {"rejected_plans": []},
    }
    inputs["backlog"] = {
        "tickets": [
            {
                "case_id": "case:one",
                "problem_id": "problem:one",
                "plan_revision_id": "plan:one",
                "stage": "research_required",
            }
        ]
    }
    inputs["case_registry"] = {
        "schema_version": 1,
        "cases": {"case:one": {"state": "active"}},
        "problem_id_to_case_id": {"problem:one": "case:one"},
        "atom_id_to_case_id": {"atom:high": "case:one"},
        "ticket_fingerprint_to_case_id": {},
    }
    return inputs


def _applied_merge_inputs(
    tmp_path: Path,
    *,
    name: str,
    receipt_relations: list[dict[str, object]],
) -> dict[str, object]:
    inputs = _passing_inputs(tmp_path)
    inputs["atoms"] = [
        _decided_atom(
            {
                "atom_id": "atom:high",
                "severity_hint": "high",
                "evidence_role": "observation",
                "evidence_class": "observed_failure",
                "source": "run_failure",
                "disposition": "supports_case",
                "case_id": "case:a",
            },
            rationale="The observed failure supports the merged canonical case.",
        )
    ]
    decisions: list[dict[str, object]] = [
        {
            "focus_id": "problem:a",
            "action": "merge",
            "target_ids": ["problem:b"],
        }
    ]
    relation_artifacts = _relation_review_artifacts(
        tmp_path,
        name=name,
        decisions=decisions,
        relations=receipt_relations,
    )
    base_stage1 = inputs["stage1"]
    inputs["stage1"] = {
        "items": [
            {
                "problem_id": "problem:a",
                "case_id": "case:a",
                "case_member_problem_ids": ["problem:a", "problem:b"],
                "absorbed_case_ids": ["case:b"],
                "case_relation_actions": [{"action": "merge"}],
                "evidence_atom_ids": ["atom:high"],
            }
        ],
        "input_meta": {
            **base_stage1["input_meta"],
            "relation_review_decision_count": 1,
        },
        "artifacts": {
            **relation_artifacts,
            "problem_mining_evidence_receipt": base_stage1["artifacts"][
                "problem_mining_evidence_receipt"
            ],
        },
    }
    inputs["stage2"] = {
        "items": [
            {
                "problem_id": "problem:a",
                "case_id": "case:a",
                "priority_bucket": "p1",
                "selected_for_research": True,
                "priority_rationale": "The merged observed failure remains active work.",
            }
        ]
    }
    inputs["stage3"] = {
        "items": [
            {
                "problem_id": "problem:a",
                "case_id": "case:a",
                "research_status": "blocked",
                "blocking_reasons": ["The faithful runtime is unavailable."],
            }
        ]
    }
    inputs["backlog"] = {
        "tickets": [
            {
                "problem_id": "problem:a",
                "case_id": "case:a",
                "stage": "research_required",
                "evidence_atom_ids": ["atom:high"],
            }
        ]
    }
    inputs["case_registry"] = {
        "schema_version": 1,
        "cases": {
            "case:a": {
                "state": "active",
                "canonical_problem_id": "problem:a",
                "problem_ids": ["problem:a", "problem:b"],
                "evidence_atom_ids": ["atom:high"],
            },
            "case:b": {"state": "alias", "alias_of": "case:a"},
        },
        "problem_id_to_case_id": {
            "problem:a": "case:a",
            "problem:b": "case:a",
        },
        "atom_id_to_case_id": {"atom:high": "case:a"},
        "ticket_fingerprint_to_case_id": {},
    }
    return inputs


def _mixed_productive_inputs(tmp_path: Path) -> dict[str, object]:
    inputs = _passing_inputs(tmp_path)
    inputs["qualification_contract"] = {}
    positive_atom = _decided_atom(
        {
            "atom_id": "atom:positive",
            "severity_hint": "medium",
            "evidence_role": "observation",
            "evidence_class": "observed_failure",
            "source": "run_failure",
            "text": "A second automated failure has a reproducible causal mechanism.",
            "disposition": "supports_case",
            "case_id": "case:positive",
        },
        rationale="The observed failure supports the separately researched positive case.",
    )
    inputs["atoms"].append(positive_atom)
    evidence_draft = build_problem_mining_evidence_draft(
        atoms=inputs["atoms"],
        eligible_atoms=[],
        mode="live",
    )
    evidence_receipt_path = tmp_path / "productive_problem_mining_evidence.json"
    evidence_receipt = finalize_problem_mining_evidence_receipt(
        draft=evidence_draft,
        atoms=inputs["atoms"],
        receipt_path=evidence_receipt_path,
    )
    inputs["stage1"]["input_meta"]["problem_mining_evidence_receipt"] = (
        problem_mining_evidence_receipt_ref(
            receipt=evidence_receipt,
            receipt_path=evidence_receipt_path,
        )
    )
    inputs["stage1"]["artifacts"]["problem_mining_evidence_receipt"] = str(evidence_receipt_path)
    inputs["stage1"]["items"].append(
        {
            "case_id": "case:positive",
            "problem_id": "problem:positive",
            "case_member_problem_ids": ["problem:positive"],
            "evidence_atom_ids": ["atom:positive"],
        }
    )
    inputs["stage2"]["items"].append(
        {
            "case_id": "case:positive",
            "problem_id": "problem:positive",
            "priority_bucket": "p1",
            "selected_for_research": True,
            "priority_rationale": "The reproduced failure is actionable.",
        }
    )
    positive_dossier = _proof_basis_dossier()
    positive_dossier.update(
        {
            "case_id": "case:positive",
            "problem_id": "problem:positive",
            "research_status": "evidence_sufficient",
        }
    )
    attempted_dossier = deepcopy(positive_dossier)
    positive_dossier["research_attempts"] = [
        {
            "attempt_number": 1,
            "outcome": "output_contract_valid",
            "attempted_dossier": attempted_dossier,
            "attempted_dossier_sha256": shadow_mod._canonical_hash(attempted_dossier),
        }
    ]
    inputs["stage3"]["items"].append(positive_dossier)
    inputs["stage4"]["items"].append(
        {
            "case_id": "case:positive",
            "problem_id": "problem:positive",
            "option_id": "option:positive",
        }
    )
    inputs["stage5"]["items"].append(
        {
            "case_id": "case:positive",
            "problem_id": "problem:positive",
            "selected_option_id": "option:positive",
        }
    )
    plan_draft: dict[str, object] = {
        "case_id": "case:positive",
        "problem_id": "problem:positive",
        "selected_option_id": "option:positive",
        "repo_revision": "a" * 40,
        "change_targets": [
            {
                "action": "modify",
                "path": "src/core.py",
                "symbols": ["core.run"],
                "change": "Correct the verified failure mechanism at core.run.",
            }
        ],
        "verification_commands": ["pdm run pytest tests/test_core.py -q"],
    }
    plan_draft["target_contract"] = _target_contract(plan_draft)
    plan = assign_plan_revision_id(plan_draft)
    inputs["stage6"]["items"].append(plan)
    inputs["backlog"]["tickets"].append(
        {
            "case_id": "case:positive",
            "problem_id": "problem:positive",
            "plan_revision_id": plan["plan_revision_id"],
            "change_plan": plan,
            "stage": "ready_for_ticket",
        }
    )
    inputs["case_registry"]["cases"]["case:positive"] = {
        "state": "active",
        "canonical_problem_id": "problem:positive",
        "problem_ids": ["problem:positive"],
        "evidence_atom_ids": ["atom:positive"],
    }
    inputs["case_registry"]["problem_id_to_case_id"]["problem:positive"] = "case:positive"
    inputs["case_registry"]["atom_id_to_case_id"]["atom:positive"] = "case:positive"
    accepted_outputs_by_kind = {
        "research": [positive_dossier],
        "selection": [inputs["stage5"]["items"][0]],
        "plan": [plan],
        "ticket": [inputs["backlog"]["tickets"][-1]],
    }
    _attach_independent_qualification(
        inputs,
        atom_labels=[
            _actionable_label(
                "label:blocked",
                ["atom:high"],
                classification="non_actionable",
            ),
            _actionable_label("label:positive", ["atom:positive"]),
        ],
        accepted_outputs_by_kind=accepted_outputs_by_kind,
        output_ratings=[
            (
                output_kind,
                output,
                "good",
                "not_repaired",
                ["label:positive"],
            )
            for output_kind, outputs in accepted_outputs_by_kind.items()
            for output in outputs
        ],
        false_rejections=[],
    )
    return inputs


def _accept_productive_fixture_contracts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        shadow_mod,
        "assess_research_readiness",
        lambda item: (item.get("research_status") == "evidence_sufficient", []),
    )
    monkeypatch.setattr(
        shadow_mod,
        "verify_persisted_research_evidence",
        lambda item: (item.get("research_status") == "evidence_sufficient", []),
    )
    monkeypatch.setattr(shadow_mod, "assess_ticket_readiness", lambda _item: (True, []))


def _attach_independent_qualification(
    inputs: dict[str, object],
    *,
    atom_labels: list[dict[str, object]],
    accepted_outputs_by_kind: dict[str, list[dict[str, object]]],
    output_ratings: list[tuple[str, dict[str, object], str, str, list[str]]],
    false_rejections: list[str] | None = None,
    no_actionable_receipt: bool = False,
) -> None:
    expanded_outputs = {
        kind: list(accepted_outputs_by_kind.get(kind, []))
        for kind in (
            "problem",
            "relation",
            "priority",
            "research",
            "option",
            "selection",
            "plan",
            "ticket",
        )
    }
    expanded_outputs["problem"] = list(inputs["stage1"].get("items", []))
    stage1_meta = inputs["stage1"].get("input_meta", {})
    expanded_outputs["relation"] = list(
        stage1_meta.get("relation_review_decisions", [])
        if isinstance(stage1_meta, dict)
        else []
    )
    expanded_outputs["priority"] = list(inputs["stage2"].get("items", []))
    expanded_outputs["option"] = list(inputs["stage4"].get("items", []))
    rated_identities = {
        (kind, shadow_mod._canonical_hash(output))
        for kind, output, _quality, _repair, _labels in output_ratings
    }
    expanded_ratings = list(output_ratings)
    actionable_label_ids = [
        str(label["label_id"])
        for label in atom_labels
        if label.get("classification") == "actionable"
    ]
    for kind in ("problem", "relation", "priority", "option"):
        for output in expanded_outputs[kind]:
            identity = (kind, shadow_mod._canonical_hash(output))
            if identity in rated_identities:
                continue
            expanded_ratings.append(
                (
                    kind,
                    output,
                    "good" if actionable_label_ids else "unknown",
                    "not_repaired",
                    actionable_label_ids,
                )
            )
    manifest = build_qualification_corpus_manifest(
        atoms=inputs["atoms"],
        atom_labels=atom_labels,
        adjudicator="held-out-shadow-reviewer",
        method="independent fixture evidence review",
    )
    adjudication = build_qualification_output_adjudication(
        manifest=manifest,
        accepted_outputs_by_kind=expanded_outputs,
        output_adjudications=[
            {
                "output_kind": output_kind,
                "output_sha256": shadow_mod._canonical_hash(output),
                "quality": quality,
                "repair_status": repair_status,
                "actionable_label_ids": label_ids,
                "rationale": f"Independent {output_kind} output assessment.",
                **(
                    {
                        "bad_severity": "noncritical",
                        "bad_categories": ["limited_causal_coverage"],
                    }
                    if quality == "bad"
                    else {}
                ),
            }
            for output_kind, output, quality, repair_status, label_ids in expanded_ratings
        ],
        false_rejections=[
            {
                "label_id": label_id,
                "rationale": "The useful held-out case did not reach an accepted ticket.",
            }
            for label_id in (false_rejections or [])
        ],
        pending_run_sha256="d" * 64,
        adjudicator="held-out-shadow-reviewer",
        method="independent post-run artifact review",
    )
    inputs["qualification_manifest"] = manifest
    inputs["qualification_manifest_sha256_expected"] = "a" * 64
    inputs["qualification_manifest_sha256_observed"] = "a" * 64
    inputs["qualification_output_adjudication"] = adjudication
    inputs["qualification_output_adjudication_sha256_pre_run"] = None
    inputs["qualification_output_adjudication_sha256_post_run"] = "b" * 64
    inputs["qualification_pending_run_sha256"] = "d" * 64
    if no_actionable_receipt:
        inputs["no_actionable_evidence_receipt"] = build_no_actionable_evidence_receipt(
            manifest=manifest,
            adjudicator="held-out-shadow-reviewer",
            method="complete clean-corpus review",
        )


def _actionable_label(
    label_id: str,
    atom_ids: list[str],
    *,
    classification: str = "actionable",
) -> dict[str, object]:
    return {
        "label_id": label_id,
        "classification": classification,
        "atom_ids": atom_ids,
        "rationale": f"Independent classification of {label_id}.",
    }


def test_shadow_invariants_reject_all_blocked_nonempty_cycle(
    tmp_path: Path,
) -> None:
    inputs = _passing_inputs(tmp_path)
    inputs["qualification_contract"] = {}
    _attach_independent_qualification(
        inputs,
        atom_labels=[_actionable_label("label:one", ["atom:high"])],
        accepted_outputs_by_kind={},
        output_ratings=[],
        false_rejections=["label:one"],
    )

    report = evaluate_shadow_invariants(**inputs)

    assert report["passed"] is False
    assert set(report["failures"]) == {
        "shadow_qualification_evidence_sufficient_research_throughput_"
        "below_minimum:observed=0:required=1",
        "shadow_qualification_authoritative_ready_ticket_throughput_"
        "below_minimum:observed=0:required=1",
        "independent_qualification_actionable_zero_output",
    }
    assert report["counts"]["qualifying_observed_atoms"] == 1
    assert report["counts"]["actionable_nonterminal_cases"] == 1
    assert report["counts"]["independent_actionable_cases"] == 1
    assert report["counts"]["cases"] == 1
    assert report["counts"]["research_proofs"] == 1
    assert report["counts"]["honest_case_specific_blocked_research"] == 1
    assert report["counts"]["systemic_research_blockers"] == 0
    assert inputs["stage3"]["items"][0]["research_status"] == "blocked"
    assert inputs["backlog"]["tickets"][0]["stage"] == "research_required"


def test_shadow_stage2_cannot_fabricate_actionability_for_independently_clean_corpus(
    tmp_path: Path,
) -> None:
    inputs = _passing_inputs(tmp_path)
    inputs["qualification_contract"] = {}
    _attach_independent_qualification(
        inputs,
        atom_labels=[
            _actionable_label(
                "label:clean",
                ["atom:high"],
                classification="non_actionable",
            )
        ],
        accepted_outputs_by_kind={},
        output_ratings=[],
        no_actionable_receipt=True,
    )

    report = evaluate_shadow_invariants(**inputs)

    assert report["failures"] == ["shadow_verified_exhaustion_backlog_not_empty:ticket_count=1"]
    assert report["qualification"]["qualification_class"] == "verified_exhaustion"
    assert report["counts"]["actionable_nonterminal_cases"] == 1
    assert report["counts"]["independent_actionable_cases"] == 0
    assert not any("throughput_below_minimum" in item for item in report["failures"])


def test_shadow_stage2_cannot_erase_independently_actionable_zero_output(
    tmp_path: Path,
) -> None:
    inputs = _passing_inputs(tmp_path)
    inputs["qualification_contract"] = {}
    inputs["stage2"]["items"][0].update(
        {
            "priority_bucket": "watch",
            "selected_for_research": False,
            "priority_rationale": "The model tried to defer the held-out actionable case.",
        }
    )
    _attach_independent_qualification(
        inputs,
        atom_labels=[_actionable_label("label:one", ["atom:high"])],
        accepted_outputs_by_kind={},
        output_ratings=[],
        false_rejections=["label:one"],
    )

    report = evaluate_shadow_invariants(**inputs)

    assert "independent_qualification_actionable_zero_output" in report["failures"]
    assert (
        "shadow_qualification_authoritative_ready_ticket_throughput_"
        "below_minimum:observed=0:required=1" in report["failures"]
    )
    assert report["counts"]["actionable_nonterminal_cases"] == 0
    assert report["counts"]["independent_actionable_cases"] == 1


def test_shadow_rejects_missing_codex_stage_invocation_provenance(
    tmp_path: Path,
) -> None:
    inputs = _passing_inputs(tmp_path)
    inputs["backlog"]["input_meta"] = {"agent": "codex", "dry_run": False}
    for key, stage in (
        ("stage1", "problem_mining"),
        ("stage2", "problem_prioritization"),
        ("stage4", "solution_optioning"),
        ("stage5", "solution_selection"),
        ("stage6", "implementation_planning"),
    ):
        inputs[key]["stage"] = stage

    report = evaluate_shadow_invariants(**inputs)

    provenance_failures = [
        failure
        for failure in report["failures"]
        if failure.startswith("stage_model_invocation_provenance_invalid:")
    ]
    assert len(provenance_failures) == 5
    assert all(
        failure.endswith("stage_model_invocation_contract_missing")
        for failure in provenance_failures
    )


def test_shadow_invariants_accept_mixed_honest_block_and_productive_case(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inputs = _mixed_productive_inputs(tmp_path)
    _accept_productive_fixture_contracts(monkeypatch)

    report = evaluate_shadow_invariants(**inputs)

    assert report["passed"] is True
    assert report["failures"] == []
    assert report["counts"]["honest_case_specific_blocked_research"] == 1
    assert report["counts"]["systemic_research_blockers"] == 0
    assert report["counts"]["model_produced_evidence_sufficient_research_proofs"] == 1
    assert report["counts"]["code_grounded_plans"] == 1
    assert report["counts"]["authoritative_ready_tickets"] == 1
    assert report["qualification"]["end_to_end"]["counts"]["good"] == 1


def test_shadow_green_artifacts_do_not_infer_good_end_to_end_quality(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inputs = _mixed_productive_inputs(tmp_path)
    _accept_productive_fixture_contracts(monkeypatch)
    accepted_outputs_by_kind = {
        "problem": list(inputs["stage1"]["items"]),
        "relation": list(
            inputs["stage1"].get("input_meta", {}).get(
                "relation_review_decisions", []
            )
        ),
        "priority": list(inputs["stage2"]["items"]),
        "research": [inputs["stage3"]["items"][1]],
        "option": list(inputs["stage4"]["items"]),
        "selection": [inputs["stage5"]["items"][0]],
        "plan": [inputs["stage6"]["items"][0]],
        "ticket": [inputs["backlog"]["tickets"][-1]],
    }
    inputs["qualification_output_adjudication"] = build_qualification_output_adjudication(
        manifest=inputs["qualification_manifest"],
        accepted_outputs_by_kind=accepted_outputs_by_kind,
        output_adjudications=[
            {
                "output_kind": output_kind,
                "output_sha256": shadow_mod._canonical_hash(output),
                "quality": "bad" if output_kind == "ticket" else "good",
                "repair_status": "not_repaired",
                "actionable_label_ids": ["label:positive"],
                "rationale": "Independent semantic review, not readiness inference.",
                **(
                    {
                        "bad_severity": "noncritical",
                        "bad_categories": ["limited_causal_coverage"],
                    }
                    if output_kind == "ticket"
                    else {}
                ),
            }
            for output_kind, outputs in accepted_outputs_by_kind.items()
            for output in outputs
        ],
        false_rejections=[
            {
                "label_id": "label:positive",
                "rationale": "The bad ticket did not recover the actionable case.",
            }
        ],
        pending_run_sha256="d" * 64,
        adjudicator="held-out-shadow-reviewer",
        method="independent post-run artifact review",
    )

    report = evaluate_shadow_invariants(**inputs)

    assert report["counts"]["authoritative_ready_tickets"] == 1
    assert report["qualification"]["end_to_end"]["counts"]["bad"] == 1
    assert (
        "independent_qualification_good_ticket_count_below_minimum:"
        "observed=0:required=1" in report["failures"]
    )


def test_shadow_invariants_reject_systemic_research_blocker_despite_throughput(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inputs = _mixed_productive_inputs(tmp_path)
    blocked = inputs["stage3"]["items"][0]
    blocked["blocking_reasons"] = [
        "research_runner_exception:RuntimeError:"
        "codex_execpolicy_chatgpt_login_status_failed:not_logged_in"
    ]
    blocked["research_attempts"] = [
        {
            "attempt_number": 1,
            "outcome": "invocation_failed",
            "attempted_dossier": {},
        }
    ]
    _accept_productive_fixture_contracts(monkeypatch)

    report = evaluate_shadow_invariants(**inputs)

    assert report["passed"] is False
    assert "shadow_qualification_systemic_research_blocker:case:one:auth" in report["failures"]
    assert report["counts"]["systemic_research_blockers"] == 1
    assert report["counts"]["authoritative_ready_tickets"] == 1


@pytest.mark.parametrize(
    ("record", "retained_errors", "expected"),
    [
        (
            {
                "research_status": "blocked",
                "blocking_reasons": ["codex_execpolicy_chatgpt_login_status_failed:not_logged_in"],
            },
            [],
            "auth",
        ),
        (
            {
                "research_status": "blocked",
                "blocking_reasons": ["research_runner_exception:RuntimeError:boom"],
            },
            [],
            "invocation",
        ),
        (
            {
                "research_status": "blocked",
                "blocking_reasons": ["research_dossier_output_contract_invalid"],
            },
            [],
            "runner_contract",
        ),
        (
            {
                "research_status": "blocked",
                "blocking_reasons": ["research_output_contract_retry_result_missing"],
            },
            [],
            "produced_artifact_loss",
        ),
        (
            {"research_status": "evidence_sufficient"},
            ["research_artifact_changed:artifact:repro"],
            "produced_artifact_loss",
        ),
    ],
)
def test_systemic_research_blocker_failure_codes(
    record: dict[str, object],
    retained_errors: list[str],
    expected: str,
) -> None:
    assert (
        shadow_mod._systemic_research_blocker_code(
            record,
            retained_validation_errors=retained_errors,
        )
        == expected
    )


def test_final_same_session_repair_is_retained_as_model_produced_research(
    tmp_path: Path,
) -> None:
    inputs = _mixed_productive_inputs(tmp_path)
    dossier = inputs["stage3"]["items"][-1]
    repaired = {
        key: deepcopy(value) for key, value in dossier.items() if key != "research_attempts"
    }
    for runner_owned_field in (
        "research_schema_version",
        "repo_revision",
        "diff_classification",
        "evidence_assignment",
    ):
        repaired.pop(runner_owned_field, None)
    repaired["artifact_refs"] = []
    dossier["artifact_refs"] = []
    dossier["research_attempts"] = [
        {
            "attempt_number": 1,
            "attempt_kind": "full_research",
            "outcome": "output_contract_invalid",
            "validation_errors": ["positive_outcome_contract_invalid"],
            "attempted_dossier": {},
            "attempted_dossier_sha256": shadow_mod._canonical_hash({}),
        },
        {
            "attempt_number": 2,
            "attempt_kind": "model_output_repair",
            "outcome": "repair_contract_valid",
            "validation_errors": [],
            "attempted_dossier": repaired,
            "attempted_dossier_sha256": shadow_mod._canonical_hash(repaired),
            "agent_session_id": "session:author",
            "observed_agent_session_id": "session:author",
            "resumed_from_session_id": "session:author",
            "repair_progress": {"decision": "accepted"},
        },
    ]
    dossier["artifact_refs"].append(
        {
            "kind": "runner_report",
            "path": "C:/volatile/run/report.json",
            "sha256": "f" * 64,
            "size_bytes": 123,
        }
    )

    assert shadow_mod._model_produced_evidence_sufficient_proof(dossier) is True
    assert not any(
        signal.startswith("attempt_outcome:")
        for signal in shadow_mod._research_blocker_signals(dossier)
    )


def test_superseded_invalid_attempt_is_telemetry_after_honest_repair_downgrade() -> None:
    repaired = {
        "case_id": "case:one",
        "problem_id": "problem:one",
        "research_status": "insufficient_evidence",
        "blocking_reasons": ["origin_evidence_atoms_unresolved:atom:missing"],
    }
    dossier = {
        **repaired,
        "research_attempts": [
            {
                "attempt_number": 1,
                "outcome": "output_contract_invalid",
                "validation_errors": ["research_dossier_output_contract_invalid"],
            },
            {
                "attempt_number": 2,
                "attempt_kind": "model_output_repair",
                "outcome": "repair_contract_valid",
                "validation_errors": [],
                "attempted_dossier": repaired,
                "attempted_dossier_sha256": shadow_mod._canonical_hash(repaired),
                "agent_session_id": "session:author",
                "observed_agent_session_id": "session:author",
                "resumed_from_session_id": "session:author",
                "repair_progress": {"decision": "accepted"},
            },
        ],
    }

    signals = shadow_mod._research_blocker_signals(dossier)
    assert "attempt_outcome:output_contract_invalid" not in signals
    assert "research_dossier_output_contract_invalid" not in signals
    assert shadow_mod._systemic_research_blocker_code(dossier) is None


def test_qualification_author_index_uses_stage_owned_role_histories() -> None:
    role_run = {
        "role": "planner",
        "status": "corrected",
        "accepted": True,
        "session_id": "session:planner",
        "response_sha256": "a" * 64,
        "attempt_history": [
            {
                "attempt_number": 1,
                "attempt_tag": "planner_001",
                "status": "verified",
                "agent_session_id": "session:planner",
                "prompt_sha256": "b" * 64,
                "response_sha256": "a" * 64,
            }
        ],
    }
    selector_run = {
        **role_run,
        "role": "selector",
        "session_id": "session:selector",
        "attempt_history": [
            {
                **role_run["attempt_history"][0],
                "agent_session_id": "session:selector",
            }
        ],
    }
    selection = {
        "case_id": "case:one",
        "problem_id": "problem:one",
        "selected_option_id": "option:one",
    }
    plan = {
        **selection,
        "plan_revision_id": "planrev:one",
    }
    ticket = {
        **selection,
        "plan_revision_id": "planrev:one",
        "change_plan": plan,
    }
    index = shadow_mod._qualification_output_author_provenance(
        stage1={"items": [], "input_meta": {}},
        stage2={"items": [], "input_meta": {}},
        stage3={"items": []},
        stage4={"items": [], "input_meta": {}},
        stage5={
            "items": [selection],
            "input_meta": {
                "selection_outcomes": [
                    {
                        "problem_id": "problem:one",
                        "role_runs": [selector_run],
                    }
                ]
            },
        },
        stage6={
            "items": [plan],
            "input_meta": {
                "planning_correction_runs": [
                    {
                        "case_id": "case:one",
                        "problem_id": "problem:one",
                        "selected_option_id": "option:one",
                        **role_run,
                    }
                ]
            },
        },
        accepted_outputs_by_kind={
            "research": [],
            "selection": [selection],
            "plan": [plan],
            "ticket": [ticket],
        },
    )

    assert (
        index[f"selection:{shadow_mod._canonical_hash(selection)}"]["agent_session_id"]
        == "session:selector"
    )
    assert (
        index[f"plan:{shadow_mod._canonical_hash(plan)}"]["agent_session_id"] == "session:planner"
    )
    assert (
        index[f"ticket:{shadow_mod._canonical_hash(ticket)}"]["exact_session_continuation"] is True
    )


def test_relation_priority_and_problem_component_routes_bind_exact_authors() -> None:
    def attempt(session: str, workspace: str) -> dict[str, object]:
        return {
            "attempt_number": 1,
            "status": "verified",
            "agent_session_id": session,
            "workspace_dir": workspace,
        }

    problem = {
        "case_id": "case:one",
        "problem_id": "problem:one",
        "evidence_atom_ids": ["atom:one"],
    }
    relation = {
        "focus_id": "problem:one",
        "action": "keep_separate",
        "rationale": "The initial reviewer treated the mechanisms as distinct.",
        "review_confidence": 0.7,
    }
    priority = {
        "case_id": "case:one",
        "problem_id": "problem:one",
        "priority_bucket": "watch",
        "selected_for_research": True,
    }
    accepted = {
        "problem": [problem],
        "relation": [relation],
        "priority": [priority],
    }
    index = shadow_mod._qualification_output_author_provenance(
        stage1={
            "items": [problem],
            "input_meta": {
                "miner_results": [
                    {
                        "tag": "problem_mining_001",
                        "assigned_atom_ids": ["atom:one"],
                        "attempt_history": [
                            attempt("session:miner", "C:/workspace/miner")
                        ],
                        "coverage_depth_review_attempt_history": [
                            attempt("session:coverage", "C:/workspace/coverage")
                        ],
                    }
                ],
                "relation_review_batches": [
                    {
                        "tag": "relation_batch_001",
                        "focus_ids": ["problem:one", "problem:two"],
                        "attempt_history": [
                            attempt("session:relation", "C:/workspace/relation")
                        ],
                    }
                ],
            },
        },
        stage2={
            "items": [priority],
            "input_meta": {
                "prioritizer_attempt_history": [
                    attempt("session:priority", "C:/workspace/priority")
                ]
            },
        },
        stage3={"items": []},
        stage4={"items": [], "input_meta": {}},
        stage5={"items": [], "input_meta": {}},
        stage6={"items": [], "input_meta": {}},
        accepted_outputs_by_kind=accepted,
    )
    problem_identity = "problem:" + shadow_mod._canonical_hash(problem)
    relation_identity = "relation:" + shadow_mod._canonical_hash(relation)
    priority_identity = "priority:" + shadow_mod._canonical_hash(priority)
    assert {
        item["component_id"]
        for item in index[problem_identity]["author_component_frontiers"]
    } == {
        "problem_miner:problem_mining_001",
        "coverage_review:problem_mining_001",
    }
    assert index[relation_identity]["agent_session_id"] == "session:relation"
    assert index[priority_identity]["agent_session_id"] == "session:priority"
    missed = shadow_mod._false_rejection_author_provenance(
        manifest={
            "atom_labels": [
                {
                    "label_id": "label:missed",
                    "classification": "actionable",
                    "atom_ids": ["atom:one"],
                }
            ]
        },
        stage1={
            "items": [],
            "input_meta": {
                "miner_results": [
                    {
                        "tag": "problem_mining_001",
                        "assigned_atom_ids": ["atom:one"],
                        "attempt_history": [
                            attempt("session:miner", "C:/workspace/miner")
                        ],
                        "coverage_depth_review_attempt_history": [
                            attempt("session:coverage", "C:/workspace/coverage")
                        ],
                    }
                ]
            },
        },
        stage2={"items": [], "input_meta": {}},
        stage3={"items": []},
        stage4={"items": [], "input_meta": {}},
        stage5={"items": [], "input_meta": {}},
        stage6={"items": [], "input_meta": {}},
    )["label:missed"]
    assert missed["agent_session_id"] == "session:coverage"
    assert missed["stage1_correction_adapter"] == "coverage_review"
    assert missed["miner_tag"] == "problem_mining_001"
    assert missed["evidence_atom_ids"] == ["atom:one"]

    def bad_item(
        kind: str,
        output: dict[str, object],
        *,
        component: str | None = None,
    ) -> dict[str, object]:
        item: dict[str, object] = {
            "output_kind": kind,
            "output_sha256": shadow_mod._canonical_hash(output),
            "quality": "bad",
            "repair_status": "not_repaired",
            "correctability": "correctable",
            "bad_severity": "noncritical",
            "bad_categories": ["independent_semantic_finding"],
            "rationale": "The retained authored decision is semantically wrong.",
            "actionable_label_ids": ["label:one"],
        }
        if component is not None:
            item["author_component_target"] = component
        return item

    routes = qualification_mod._qualification_correction_routes(
        [
            bad_item(
                "problem",
                problem,
                component="problem_miner:problem_mining_001",
            ),
            bad_item("relation", relation),
            bad_item("priority", priority),
        ],
        output_author_provenance=index,
    )
    by_kind = {route["output_kind"]: route for route in routes}
    assert by_kind["problem"]["agent_session_id"] == "session:miner"
    assert (
        by_kind["problem"]["author_component_resolution_status"]
        == "explicit_component"
    )
    assert by_kind["relation"]["agent_session_id"] == "session:relation"
    assert by_kind["relation"]["author_provenance"][
        "stage1_correction_adapter"
    ] == "relation_review"
    assert by_kind["priority"]["agent_session_id"] == "session:priority"
    assert by_kind["priority"]["restart_from_stage"] == "problem_prioritization"

    invalid = qualification_mod._qualification_correction_routes(
        [bad_item("problem", problem, component="relation_review:not-a-frontier")],
        output_author_provenance=index,
    )[0]
    assert invalid["route_status"] == "author_provenance_unavailable"
    assert invalid["agent_session_id"] is None
    assert invalid["selected_author_component_target"] is None
    assert invalid["author_component_resolution_status"] == "invalid_component_target"
    assert set(invalid["available_author_component_targets"]) == {
        "problem_miner:problem_mining_001",
        "coverage_review:problem_mining_001",
    }


def test_false_rejection_spanning_assignments_returns_every_exact_stage1_author() -> None:
    def attempt(session: str, workspace: str) -> dict[str, object]:
        return {
            "attempt_number": 1,
            "status": "verified",
            "agent_session_id": session,
            "workspace_dir": workspace,
        }

    provenance = shadow_mod._false_rejection_author_provenance(
        manifest={
            "atom_labels": [
                {
                    "label_id": "label:spanning",
                    "classification": "actionable",
                    "atom_ids": ["atom:one", "atom:two"],
                }
            ]
        },
        stage1={
            "items": [],
            "input_meta": {
                "miner_results": [
                    {
                        "tag": "assignment:one",
                        "assigned_atom_ids": ["atom:one"],
                        "coverage_depth_review_attempt_history": [
                            attempt("session:one", "C:/workspace/one")
                        ],
                    },
                    {
                        "tag": "assignment:two",
                        "assigned_atom_ids": ["atom:two"],
                        "coverage_depth_review_attempt_history": [
                            attempt("session:two", "C:/workspace/two")
                        ],
                    },
                ]
            },
        },
        stage2={"items": [], "input_meta": {}},
        stage3={"items": []},
        stage4={"items": [], "input_meta": {}},
        stage5={"items": [], "input_meta": {}},
        stage6={"items": [], "input_meta": {}},
    )["label:spanning"]

    assert isinstance(provenance, list)
    assert {item["agent_session_id"] for item in provenance} == {
        "session:one",
        "session:two",
    }
    assert {tuple(item["evidence_atom_ids"]) for item in provenance} == {
        ("atom:one",),
        ("atom:two",),
    }
    assert {
        tuple(item["causal_target"]["expected_item_keys"])
        for item in provenance
    } == {("atom:atom:one",), ("atom:atom:two",)}

def test_second_cycle_routes_to_earlier_per_problem_repair_author() -> None:
    option = {
        "case_id": "case:one",
        "problem_id": "problem:one",
        "option_id": "option:one",
    }

    def option_meta(problem_id: str, session: str, workspace: str) -> dict[str, object]:
        return {
            "optioning_correction_runs": [
                {
                    "problem_id": problem_id,
                    "attempt_history": [
                        {
                            "attempt_number": 2,
                            "status": "verified",
                            "agent_session_id": session,
                            "workspace_dir": workspace,
                        }
                    ],
                }
            ]
        }

    stage4 = {
        "items": [option, {"problem_id": "problem:two", "option_id": "option:two"}],
        "input_meta": {
            **option_meta("problem:two", "session:latest", "C:/workspace/latest"),
            "qualification_repair_history": [
                {
                    "affected_problem_ids": ["problem:one"],
                    "replacement_stage_document_sha256": "a" * 64,
                    "replacement_author_input_meta": option_meta(
                        "problem:one",
                        "session:earlier",
                        "C:/workspace/earlier",
                    ),
                    "route_consumption_receipts": [
                        {
                            "route_sha256": "b" * 64,
                            "consumption_receipt_sha256": "c" * 64,
                        }
                    ],
                },
                {
                    "affected_problem_ids": ["problem:two"],
                    "replacement_stage_document_sha256": "d" * 64,
                    "replacement_author_input_meta": option_meta(
                        "problem:two",
                        "session:latest",
                        "C:/workspace/latest",
                    ),
                    "route_consumption_receipts": [],
                },
            ],
        },
    }
    index = shadow_mod._qualification_output_author_provenance(
        stage1={"items": [], "input_meta": {}},
        stage2={"items": [], "input_meta": {}},
        stage3={"items": []},
        stage4=stage4,
        stage5={"items": [], "input_meta": {}},
        stage6={"items": [], "input_meta": {}},
        accepted_outputs_by_kind={"option": [option]},
    )
    identity = "option:" + shadow_mod._canonical_hash(option)
    provenance = index[identity]
    assert provenance["agent_session_id"] == "session:earlier"
    assert provenance["workspace_dir"] == "C:/workspace/earlier"
    assert provenance["qualification_repair_frontier"] == {
        "source": "qualification_repair_history",
        "affected_problem_ids": ["problem:one"],
        "replacement_stage_document_sha256": "a" * 64,
        "route_consumption_receipts": [
            {
                "route_sha256": "b" * 64,
                "consumption_receipt_sha256": "c" * 64,
            }
        ],
    }
    route = qualification_mod._qualification_correction_routes(
        [
            {
                "output_kind": "option",
                "output_sha256": shadow_mod._canonical_hash(option),
                "quality": "bad",
                "repair_status": "not_repaired",
                "correctability": "correctable",
                "bad_severity": "noncritical",
                "bad_categories": ["residual_recurrence_path"],
                "rationale": "The earlier repaired option still leaves a recurrence path.",
                "actionable_label_ids": ["label:one"],
            }
        ],
        output_author_provenance=index,
    )[0]
    assert route["route_status"] == "same_author_resume"
    assert route["agent_session_id"] == "session:earlier"


def test_shadow_invariants_require_code_grounded_persisted_ready_plan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inputs = _mixed_productive_inputs(tmp_path)
    del inputs["stage6"]["items"][0]["change_targets"][0]["symbols"]
    _accept_productive_fixture_contracts(monkeypatch)

    report = evaluate_shadow_invariants(**inputs)

    assert report["passed"] is False
    assert any(
        failure.startswith("ready_ticket_persisted_plan_not_code_grounded:")
        and "plan_target_contract_targets_mismatch" in failure
        for failure in report["failures"]
    )
    assert (
        "shadow_qualification_authoritative_ready_ticket_throughput_"
        "below_minimum:observed=0:required=1" in report["failures"]
    )
    assert report["counts"]["code_grounded_plans"] == 0
    assert report["counts"]["authoritative_ready_tickets"] == 0


@pytest.mark.parametrize(
    ("schema_version", "action", "destination_path", "symbols"),
    [
        (2, "modify", None, ["core.run"]),
        (3, "modify", None, []),
        (3, "create", None, []),
        (3, "delete", None, []),
        (3, "rename", "src/renamed.py", []),
        (3, "move", "src/moved.py", []),
    ],
)
def test_persisted_plan_grounding_uses_authoritative_v2_v3_target_contract(
    schema_version: int,
    action: str,
    destination_path: str | None,
    symbols: list[str],
) -> None:
    target: dict[str, object] = {
        "action": action,
        "path": "src/core.py",
        "symbols": symbols,
        "change": "Apply the researched intervention to the exact target.",
    }
    if destination_path is not None:
        target["destination_path"] = destination_path
    draft: dict[str, object] = {
        "case_id": "case:one",
        "problem_id": "problem:one",
        "selected_option_id": "option:one",
        "repo_revision": "a" * 40,
        "change_targets": [target],
        "verification_commands": ["pdm run pytest tests/test_core.py -q"],
    }
    draft["target_contract"] = _target_contract(
        draft,
        schema_version=schema_version,
    )
    plan = assign_plan_revision_id(draft)

    assert shadow_mod._persisted_plan_grounding_errors(plan) == []


def test_honest_missing_origin_evidence_is_not_a_systemic_blocker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inputs = _mixed_productive_inputs(tmp_path)
    blocked = inputs["stage3"]["items"][0]
    blocked["blocking_reasons"] = [
        "origin_evidence_atoms_unresolved:atom:historical-missing",
        "origin_attachment_materialization_failed",
    ]
    _accept_productive_fixture_contracts(monkeypatch)

    report = evaluate_shadow_invariants(**inputs)

    assert report["passed"] is True
    assert report["failures"] == []
    assert report["counts"]["honest_case_specific_blocked_research"] == 1
    assert report["counts"]["systemic_research_blockers"] == 0


@pytest.mark.parametrize("corpus_kind", ["empty", "proposal_only"])
def test_empty_or_proposal_only_cycles_are_recorded_but_never_qualify(
    tmp_path: Path,
    corpus_kind: str,
) -> None:
    inputs = _passing_inputs(tmp_path)
    inputs["qualification_contract"] = {"require_nonempty_throughput": False}
    inputs["atoms"] = (
        []
        if corpus_kind == "empty"
        else [
            _decided_atom(
                {
                    "atom_id": "atom:proposal",
                    "severity_hint": "high",
                    "evidence_role": "observation",
                    "evidence_class": "proposal",
                    "source": "suggested_change",
                    "disposition": "expected_noise",
                    "disposition_proof": _proposal_noise_proof("atom:proposal"),
                },
                rationale="Proposal-only input is not observed automated failure evidence.",
            )
        ]
    )
    inputs["backlog"] = {"tickets": []}
    inputs["stage1"]["items"] = []
    inputs["stage2"] = {"items": []}
    inputs["stage3"] = {"items": []}
    inputs["stage4"] = {"items": [], "input_meta": {"optioning_outcomes": []}}
    inputs["stage5"] = {"items": [], "input_meta": {"selection_outcomes": []}}
    inputs["stage6"] = {"items": []}
    inputs["case_registry"] = {
        "schema_version": 1,
        "cases": {},
        "problem_id_to_case_id": {},
        "atom_id_to_case_id": {},
        "ticket_fingerprint_to_case_id": {},
    }

    report = evaluate_shadow_invariants(**inputs)

    assert report["passed"] is False
    assert report["failures"] == ["shadow_qualification_no_observed_automated_evidence"]
    assert not any("throughput_below_minimum" in failure for failure in report["failures"])
    assert report["counts"]["actionable_nonterminal_cases"] == 0

    backlog_path = tmp_path / f"{corpus_kind}.backlog.json"
    _write_json(backlog_path, inputs["backlog"])
    state_path = shadow_state_path(backlog_path)
    for hour in (0, 1):
        state = _record_cycle(
            tmp_path,
            state_path=state_path,
            backlog_path=backlog_path,
            invariant_report=report,
            generated_at=f"2026-07-09T0{hour}:00:00Z",
        )

    assert len(state["cycles"]) == 2
    assert state["consecutive_stable_passes"] == 0
    assert state["ready_for_export"] is False


def test_exhausted_terminal_corpus_does_not_require_fabricated_throughput(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inputs = _passing_inputs(tmp_path)
    inputs["qualification_contract"] = {"require_nonempty_throughput": False}
    inputs["backlog"] = {"tickets": []}
    inputs["stage2"] = {"items": []}
    inputs["stage3"] = {"items": []}
    inputs["case_registry"]["cases"]["case:one"]["state"] = "resolved"
    _attach_independent_qualification(
        inputs,
        atom_labels=[
            _actionable_label(
                "label:clean",
                ["atom:high"],
                classification="non_actionable",
            )
        ],
        accepted_outputs_by_kind={},
        output_ratings=[],
        no_actionable_receipt=True,
    )
    monkeypatch.setattr(shadow_mod, "_terminal_outcome_errors", lambda *_args, **_kwargs: [])

    report = evaluate_shadow_invariants(**inputs)

    assert report["passed"] is True
    assert report["failures"] == []
    assert report["counts"]["qualifying_observed_atoms"] == 1
    assert report["counts"]["actionable_nonterminal_cases"] == 0
    assert report["counts"]["independent_actionable_cases"] == 0
    assert report["qualification"]["counts"]["exhausted_corpus"] == 1
    assert report["qualification"]["qualification_class"] == "verified_exhaustion"
    assert report["counts"]["model_produced_evidence_sufficient_research_proofs"] == 0
    assert report["counts"]["authoritative_ready_tickets"] == 0

    backlog_path = tmp_path / "exhausted.backlog.json"
    _write_json(backlog_path, inputs["backlog"])
    state_path = shadow_state_path(backlog_path)
    state = _record_cycle(
        tmp_path,
        state_path=state_path,
        backlog_path=backlog_path,
        invariant_report=report,
        generated_at="2026-07-09T00:00:00Z",
    )

    assert state["ready_for_export"] is True
    assert state["consecutive_stable_passes"] == 0
    ready, reasons, _ = validate_shadow_export_state(
        state_path=state_path,
        backlog_path=backlog_path,
    )
    assert ready is True
    assert reasons == []


def test_two_stable_shadow_cycles_unlock_only_the_exact_backlog(tmp_path: Path) -> None:
    inputs = _passing_inputs(tmp_path)
    inputs["case_registry"] = {
        "schema_version": 1,
        "cases": {
            "case:one": {
                "state": "active",
                "canonical_problem_id": "problem:one",
                "problem_ids": ["problem:one"],
                "evidence_atom_ids": ["atom:high"],
            }
        },
        "problem_id_to_case_id": {"problem:one": "case:one"},
        "atom_id_to_case_id": {"atom:high": "case:one"},
        "ticket_fingerprint_to_case_id": {},
    }
    first_report = evaluate_shadow_invariants(**inputs)
    second_inputs = deepcopy(inputs)
    second_inputs["atoms"].append(
        _decided_atom(
            {
                "atom_id": "atom:derived-research",
                "severity_hint": "high",
                "evidence_role": "research",
                "disposition": "supports_case",
                "case_id": "case:one",
                "parent_case_id": "case:one",
                "supporting_case_ids": ["case:one"],
            },
            rationale="Runner lineage explicitly attaches the derived evidence.",
        )
    )
    second_inputs["case_registry"]["cases"]["case:one"]["evidence_atom_ids"].append(
        "atom:derived-research"
    )
    second_inputs["case_registry"]["atom_id_to_case_id"]["atom:derived-research"] = "case:one"
    second_report = evaluate_shadow_invariants(**second_inputs)
    backlog_path = tmp_path / "target.backlog.json"
    _write_json(backlog_path, inputs["backlog"])
    state_path = shadow_state_path(backlog_path)

    first = _record_cycle(
        tmp_path,
        state_path=state_path,
        backlog_path=backlog_path,
        invariant_report=first_report,
        generated_at="2026-07-09T00:00:00Z",
    )
    second = _record_cycle(
        tmp_path,
        state_path=state_path,
        backlog_path=backlog_path,
        invariant_report=second_report,
        generated_at="2026-07-09T01:00:00Z",
    )

    assert first["ready_for_export"] is False
    assert second["ready_for_export"] is True
    assert first["cycles"][-1]["cycle_id"] != second["cycles"][-1]["cycle_id"]
    receipts = {receipt["name"]: receipt for receipt in second["cycles"][-1]["artifact_receipts"]}
    for name in ("problem_records", "research", "change_plans"):
        assert len(receipts[name]["sha256"]) == 64
        assert len(receipts[name]["content_sha256"]) == 64
    assert first_report["atom_corpus_sha256"] != second_report["atom_corpus_sha256"]
    assert first_report["source_atom_corpus_sha256"] == second_report["source_atom_corpus_sha256"]
    assert first_report["case_graph_sha256"] == second_report["case_graph_sha256"]
    assert (
        second["validated_research_proof_basis_sha256"]
        == second["cycles"][-1]["research_proof_basis_sha256"]
    )
    ready, reasons, _ = validate_shadow_export_state(
        state_path=state_path,
        backlog_path=backlog_path,
    )
    assert ready is True
    assert reasons == []

    _write_json(backlog_path, {"tickets": [{"case_id": "case:new"}]})
    ready, reasons, _ = validate_shadow_export_state(
        state_path=state_path,
        backlog_path=backlog_path,
    )
    assert ready is False
    assert "backlog_changed_since_shadow_validation" in reasons

    changed_report = dict(second_report)
    changed_report["export_projection_sha256"] = "f" * 64
    changed_report["ticket_set_sha256"] = "d" * 64
    third = _record_cycle(
        tmp_path,
        state_path=state_path,
        backlog_path=backlog_path,
        invariant_report=changed_report,
        generated_at="2026-07-09T02:00:00Z",
    )
    ready, reasons, _ = validate_shadow_export_state(
        state_path=state_path,
        backlog_path=backlog_path,
    )
    assert third["ready_for_export"] is True
    assert third["consecutive_stable_passes"] == 3
    assert ready is True
    assert reasons == []

    fourth = _record_cycle(
        tmp_path,
        state_path=state_path,
        backlog_path=backlog_path,
        invariant_report=changed_report,
        generated_at="2026-07-09T03:00:00Z",
    )
    ready, reasons, _ = validate_shadow_export_state(
        state_path=state_path,
        backlog_path=backlog_path,
    )
    assert fourth["ready_for_export"] is True
    assert fourth["consecutive_stable_passes"] == 4
    assert ready is True
    assert reasons == []


def test_shadow_latest_file_binding_detects_generation_timestamp_edit(
    tmp_path: Path,
) -> None:
    inputs = _passing_inputs(tmp_path)
    report = evaluate_shadow_invariants(**inputs)
    backlog_path = tmp_path / "target.backlog.json"
    state_path = shadow_state_path(backlog_path)

    _write_json(
        backlog_path,
        {"generated_at_utc": "2026-07-09T00:00:00Z", "tickets": []},
    )
    first = _record_cycle(
        tmp_path,
        state_path=state_path,
        backlog_path=backlog_path,
        invariant_report=report,
        generated_at="2026-07-09T00:00:00Z",
    )
    first_byte_hash = first["cycles"][-1]["backlog_sha256"]

    _write_json(
        backlog_path,
        {"generated_at_utc": "2026-07-09T01:00:00Z", "tickets": []},
    )
    second = _record_cycle(
        tmp_path,
        state_path=state_path,
        backlog_path=backlog_path,
        invariant_report=report,
        generated_at="2026-07-09T01:00:00Z",
    )

    assert second["ready_for_export"] is True
    assert second["cycles"][-1]["backlog_sha256"] != first_byte_hash
    assert (
        second["cycles"][-1]["backlog_content_sha256"]
        == second["cycles"][-2]["backlog_content_sha256"]
    )
    ready, reasons, _ = validate_shadow_export_state(
        state_path=state_path,
        backlog_path=backlog_path,
    )
    assert ready is True
    assert reasons == []

    # A byte edit after validation remains locked even when it only changes the
    # ignored generation timestamp; exports must use the exact latest validated file.
    _write_json(
        backlog_path,
        {"generated_at_utc": "2026-07-09T02:00:00Z", "tickets": []},
    )
    ready, reasons, _ = validate_shadow_export_state(
        state_path=state_path,
        backlog_path=backlog_path,
    )
    assert ready is False
    assert "backlog_changed_since_shadow_validation" in reasons


def test_volatile_backlog_provenance_does_not_reset_semantic_stability(
    tmp_path: Path,
) -> None:
    inputs = _passing_inputs(tmp_path)
    report = evaluate_shadow_invariants(**inputs)
    backlog_path = tmp_path / "target.backlog.json"
    state_path = shadow_state_path(backlog_path)

    _write_json(
        backlog_path,
        {
            "tickets": [],
            "artifacts": {"research": {"run_dir": "C:/runs/first"}},
        },
    )
    first = _record_cycle(
        tmp_path,
        state_path=state_path,
        backlog_path=backlog_path,
        invariant_report=report,
        generated_at="2026-07-09T00:00:00Z",
    )
    _write_json(
        backlog_path,
        {
            "tickets": [],
            "artifacts": {"research": {"run_dir": "C:/runs/second"}},
        },
    )
    second = _record_cycle(
        tmp_path,
        state_path=state_path,
        backlog_path=backlog_path,
        invariant_report=report,
        generated_at="2026-07-09T01:00:00Z",
    )

    assert (
        first["cycles"][-1]["backlog_content_sha256"]
        != second["cycles"][-1]["backlog_content_sha256"]
    )
    assert (
        first["cycles"][-1]["export_projection_sha256"]
        == second["cycles"][-1]["export_projection_sha256"]
    )
    assert second["consecutive_stable_passes"] == 2
    assert second["ready_for_export"] is True
    ready, reasons, _ = validate_shadow_export_state(
        state_path=state_path,
        backlog_path=backlog_path,
    )
    assert ready is True
    assert reasons == []


def test_shadow_gate_honors_configured_cycle_count(tmp_path: Path) -> None:
    inputs = _passing_inputs(tmp_path)
    report = evaluate_shadow_invariants(**inputs)
    backlog_path = tmp_path / "target.backlog.json"
    _write_json(backlog_path, inputs["backlog"])
    state_path = shadow_state_path(backlog_path)

    states = [
        _record_cycle(
            tmp_path,
            state_path=state_path,
            backlog_path=backlog_path,
            invariant_report=report,
            generated_at=f"2026-07-09T0{index}:00:00Z",
            required_consecutive_cycles=3,
            require_exact_export_projection=True,
        )
        for index in range(3)
    ]

    assert [state["ready_for_export"] for state in states] == [False, False, True]
    ready, reasons, state = validate_shadow_export_state(
        state_path=state_path,
        backlog_path=backlog_path,
        required_consecutive_cycles=3,
        require_exact_export_projection=True,
    )
    assert ready is True
    assert reasons == []
    assert state is not None
    assert state["required_consecutive_cycles"] == 3


def test_shadow_gate_can_allow_projection_drift_when_configured(tmp_path: Path) -> None:
    inputs = _passing_inputs(tmp_path)
    report = evaluate_shadow_invariants(**inputs)
    report["export_projection_sha256"] = "1" * 64
    backlog_path = tmp_path / "target.backlog.json"
    _write_json(backlog_path, {"tickets": [], "generation": 1})
    state_path = shadow_state_path(backlog_path)

    first = _record_cycle(
        tmp_path,
        state_path=state_path,
        backlog_path=backlog_path,
        invariant_report=report,
        generated_at="2026-07-09T00:00:00Z",
        require_exact_export_projection=False,
    )
    _write_json(backlog_path, {"tickets": [], "generation": 2})
    changed_report = dict(report)
    changed_report["export_projection_sha256"] = "2" * 64
    second = _record_cycle(
        tmp_path,
        state_path=state_path,
        backlog_path=backlog_path,
        invariant_report=changed_report,
        generated_at="2026-07-09T01:00:00Z",
        require_exact_export_projection=False,
    )

    assert first["ready_for_export"] is False
    assert second["ready_for_export"] is True
    ready, reasons, _ = validate_shadow_export_state(
        state_path=state_path,
        backlog_path=backlog_path,
        require_exact_export_projection=False,
    )
    assert ready is True
    assert reasons == []


def test_stability_streak_ignores_per_cycle_qualification_hashes_but_not_semantics() -> None:
    common = {
        "cycle_mode": "release",
        "passed": True,
        "qualification": {"qualification_class": "positive_throughput"},
        "required_consecutive_cycles": 2,
        "require_exact_export_projection": True,
        "source_atom_corpus_sha256": "a" * 64,
        "case_graph_sha256": "b" * 64,
        "ticket_set_sha256": "c" * 64,
        "research_proof_basis_sha256": "d" * 64,
        "qualification_stability_sha256": "e" * 64,
        "stability_inputs_sha256": "f" * 64,
    }
    first = {
        **common,
        "qualification_basis_sha256": "1" * 64,
        "export_inputs_sha256": "2" * 64,
    }
    second = {
        **common,
        "qualification_basis_sha256": "3" * 64,
        "export_inputs_sha256": "4" * 64,
    }

    assert (
        shadow_mod._consecutive_stable_passes(
            [first, second],
            required_consecutive_cycles=2,
            require_exact_export_projection=True,
        )
        == 2
    )

    changed_actionability = {**second, "qualification_stability_sha256": "9" * 64}
    assert (
        shadow_mod._consecutive_stable_passes(
            [first, changed_actionability],
            required_consecutive_cycles=2,
            require_exact_export_projection=True,
        )
        == 1
    )

    bad_ratio_cycle = {**second, "passed": False}
    assert (
        shadow_mod._consecutive_stable_passes(
            [first, bad_ratio_cycle],
            required_consecutive_cycles=2,
            require_exact_export_projection=True,
        )
        == 0
    )


def test_stability_input_projection_excludes_qualification_bytes_only() -> None:
    base = {
        "name": "config.policy",
        "source_path": "C:/repo/config.yaml",
        "exists": True,
        "sha256": "a" * 64,
        "content_sha256": None,
        "size_bytes": 10,
    }
    first = [
        base,
        {
            **base,
            "name": "qualification.output_adjudication",
            "source_path": "C:/held-out/first.json",
            "sha256": "1" * 64,
        },
    ]
    second = [
        base,
        {
            **base,
            "name": "qualification.output_adjudication",
            "source_path": "C:/held-out/second.json",
            "sha256": "2" * 64,
        },
    ]

    assert shadow_mod._export_input_projection(first) != shadow_mod._export_input_projection(second)
    assert shadow_mod._stability_input_projection(first) == shadow_mod._stability_input_projection(
        second
    )


def test_stability_input_projection_ignores_custody_path_but_not_bytes() -> None:
    first = [
        {
            "name": "config.policy",
            "source_path": "C:/cycle-one/snapshot/config.yaml",
            "exists": True,
            "sha256": "a" * 64,
            "content_sha256": None,
            "size_bytes": 10,
        }
    ]
    relocated = [
        {
            **first[0],
            "source_path": "D:/cycle-two/snapshot/config.yaml",
        }
    ]
    changed = [
        {
            **relocated[0],
            "sha256": "b" * 64,
        }
    ]

    assert shadow_mod._stability_input_projection(first) == (
        shadow_mod._stability_input_projection(relocated)
    )
    assert shadow_mod._stability_input_projection(first) != (
        shadow_mod._stability_input_projection(changed)
    )


def test_rendered_export_projection_drift_does_not_reset_semantic_streak(
    tmp_path: Path,
) -> None:
    inputs = _passing_inputs(tmp_path)
    first_report = evaluate_shadow_invariants(**inputs)
    first_report["export_projection_sha256"] = "1" * 64
    second_report = dict(first_report)
    second_report["export_projection_sha256"] = "2" * 64
    backlog_path = tmp_path / "target.backlog.json"
    _write_json(backlog_path, inputs["backlog"])
    state_path = shadow_state_path(backlog_path)
    _record_cycle(
        tmp_path,
        state_path=state_path,
        backlog_path=backlog_path,
        invariant_report=first_report,
        generated_at="2026-07-09T00:00:00Z",
    )

    state = _record_cycle(
        tmp_path,
        state_path=state_path,
        backlog_path=backlog_path,
        invariant_report=second_report,
        generated_at="2026-07-09T01:00:00Z",
    )

    assert state["ready_for_export"] is True
    assert state["consecutive_stable_passes"] == 2


def _semantic_plan_ticket() -> dict[str, object]:
    return {
        "case_id": "case:semantic",
        "problem_id": "problem:model-wording-a",
        "plan_revision_id": "planrev:model-output-a",
        "stage": "ready_for_ticket",
        "evidence_atom_ids": ["atom:source"],
        "title": "Model-authored wording A",
        "change_plan": {
            "change_plan_id": "plan:model-a",
            "plan_revision_id": "planrev:model-output-a",
            "proposed_fix": "Preserve the result at the boundary.",
            "causal_coverage": {
                "research_binding": {
                    "hypothesis_id": "h1",
                    "mechanism_symbols": ["pipeline.prepare", "pipeline.commit"],
                    "supporting_evidence_refs": ["experiment:support"],
                    "counterevidence_refs": ["experiment:counter"],
                    "intervention_points": [
                        {
                            "mechanism_symbol": "pipeline.commit",
                            "controls_mechanism_symbols": [
                                "pipeline.prepare",
                                "pipeline.commit",
                            ],
                            "causal_role": "sufficient_control_point",
                            "sufficiency_rationale": "Wording A",
                            "target_path": "src/pipeline.py",
                            "target_symbol": "pipeline.commit",
                            "intervention": "Wording A",
                        }
                    ],
                }
            },
            "scope_evidence": {
                "scope_level": "single_path",
                "independent_consumers_or_failure_paths": [
                    {"name": "wording A", "evidence_refs": ["mechanism:e1"]}
                ],
            },
            "change_targets": [
                {
                    "action": "modify",
                    "path": "src/pipeline.py",
                    "symbols": ["pipeline.commit"],
                    "change": "Wording A",
                }
            ],
            "before_after_reproduction": {
                "research_experiment_id": "experiment:support",
                "expected_outcome_state": "resolved",
                "before_change": {
                    "command": "pytest tests/test_pipeline.py::test_original -q",
                    "expected_exit_code": 0,
                    "observable_assertion": {
                        "source": "stdout",
                        "operator": "equals",
                        "expected": "wrong",
                    },
                },
                "after_change": {
                    "command": "pytest tests/test_pipeline.py::test_original -q",
                    "expected_exit_code": 0,
                    "observable_assertions": [
                        {"source": "stdout", "operator": "equals", "expected": "correct"}
                    ],
                },
            },
            "outcome_verification_roles": {
                "original_scenario": {
                    "research_experiment_id": "experiment:support",
                    "commands": ["pytest tests/test_pipeline.py::test_original -q"],
                    "predicates": [
                        {"type": "command_stdout_equals", "command_index": 0, "value": "correct"}
                    ],
                }
            },
            "requires_live_verification": False,
        },
    }


def test_ticket_stability_uses_canonical_plan_intent_not_model_prose() -> None:
    first = _semantic_plan_ticket()
    second = deepcopy(first)
    second["problem_id"] = "problem:model-wording-b"
    second["plan_revision_id"] = "planrev:model-output-b"
    second["title"] = "Completely different generated title"
    plan = second["change_plan"]
    assert isinstance(plan, dict)
    plan["change_plan_id"] = "plan:model-b"
    plan["plan_revision_id"] = "planrev:model-output-b"
    plan["proposed_fix"] = "Different prose describing the same causal intervention."
    plan["change_targets"][0]["change"] = "Paraphrased target wording"
    binding = plan["causal_coverage"]["research_binding"]
    binding["intervention_points"][0]["intervention"] = "Paraphrased intervention"
    binding["intervention_points"][0]["sufficiency_rationale"] = "Paraphrased rationale"
    plan["scope_evidence"]["independent_consumers_or_failure_paths"][0]["name"] = (
        "paraphrased path label"
    )

    assert shadow_mod._ticket_projection({"tickets": [first]}) == (
        shadow_mod._ticket_projection({"tickets": [second]})
    )


@pytest.mark.parametrize("change_kind", ["mechanism", "target", "oracle"])
def test_ticket_stability_resets_for_material_plan_intent_change(
    change_kind: str,
) -> None:
    first = _semantic_plan_ticket()
    second = deepcopy(first)
    plan = second["change_plan"]
    assert isinstance(plan, dict)
    if change_kind == "mechanism":
        plan["causal_coverage"]["research_binding"]["mechanism_symbols"] = ["pipeline.unrelated"]
    elif change_kind == "target":
        plan["change_targets"][0]["path"] = "src/unrelated.py"
    else:
        plan["before_after_reproduction"]["after_change"]["observable_assertions"][0][
            "expected"
        ] = "still-wrong"

    assert shadow_mod._ticket_projection({"tickets": [first]}) != (
        shadow_mod._ticket_projection({"tickets": [second]})
    )


@pytest.mark.parametrize(
    "changed_evidence",
    [
        "origin_artifact_hash",
        "experiment_receipt",
        "controlled_delta",
        "causal_receipt",
        "mechanism_identity",
        "closure_receipt",
        "auth_receipt",
        "repo_revision",
        "image_id",
    ],
)
def test_research_proof_basis_change_remains_bound_without_resetting_semantic_streak(
    tmp_path: Path,
    changed_evidence: str,
) -> None:
    dossier = _proof_basis_dossier()
    first_basis = _proof_basis_sha256(dossier)
    changed = deepcopy(dossier)
    verification = changed["evidence_verification"]
    assignment = changed["evidence_assignment"]
    assert isinstance(verification, dict)
    assert isinstance(assignment, dict)
    if changed_evidence == "origin_artifact_hash":
        assignment["atom_receipts"][0]["artifact_receipts"][0]["sha256"] = "6" * 64
    elif changed_evidence == "experiment_receipt":
        verification["experiments"][0]["stdout_sha256"] = "6" * 64
    elif changed_evidence == "controlled_delta":
        verification["control_verifications"][0]["controlled_input_difference"]["difference"][
            "support_argument"
        ]["ast_sha256"] = "b" * 64
    elif changed_evidence == "causal_receipt":
        verification["causal_links"][0]["trace_excerpt_sha256"] = "6" * 64
    elif changed_evidence == "mechanism_identity":
        verification["verified_mechanism"]["code_paths"][0]["path"] = "src/other.py"
        verification["verified_mechanism_sha256"] = shadow_mod._canonical_hash(
            verification["verified_mechanism"]
        )
    elif changed_evidence == "closure_receipt":
        verification["deterministic_mechanism_closures"] = [
            {
                "hypothesis_id": "h1",
                "closure_receipt_id": "closure:case-local-id",
                "verification_method": "runner_deterministic_mechanism_closure_v1",
                "scenario_kind": "static_trace",
                "closure_basis": "deterministic_static_trace",
                "mechanism_symbols": ["core.run"],
                "code_path": [
                    {
                        "symbol": "core.run",
                        "path": "src/core.py",
                        "trace_excerpt_sha256": "c" * 64,
                    }
                ],
                "observed_result": {
                    "exit_code": 1,
                    "stdout_sha256": "d" * 64,
                    "stderr_sha256": "e" * 64,
                },
            }
        ]
    elif changed_evidence == "auth_receipt":
        verification["artifacts"][1]["sha256"] = "f" * 64
    elif changed_evidence == "repo_revision":
        changed["repo_revision"] = "b" * 40
        verification["repo_revision"] = "b" * 40
        verification["resolved_repo_ref"] = "b" * 40
        verification["workspace_head"] = "b" * 40
        verification["planning_workspace_head"] = "b" * 40
        verification["experiments"][0]["workspace_head"] = "b" * 40
    else:
        verification["experiments"][0]["execution_metadata"]["image_id"] = "sha256:" + "9" * 64
    second_basis = _proof_basis_sha256(changed)
    assert second_basis != first_basis

    inputs = _passing_inputs(tmp_path)
    first_report = evaluate_shadow_invariants(**inputs)
    first_report["research_proof_basis_sha256"] = first_basis
    second_report = dict(first_report)
    second_report["research_proof_basis_sha256"] = second_basis
    backlog_path = tmp_path / "target.backlog.json"
    _write_json(backlog_path, inputs["backlog"])
    state_path = shadow_state_path(backlog_path)
    _record_cycle(
        tmp_path,
        state_path=state_path,
        backlog_path=backlog_path,
        invariant_report=first_report,
        generated_at="2026-07-09T00:00:00Z",
    )

    state = _record_cycle(
        tmp_path,
        state_path=state_path,
        backlog_path=backlog_path,
        invariant_report=second_report,
        generated_at="2026-07-09T01:00:00Z",
    )

    assert state["cycles"][0]["research_proof_basis_sha256"] == first_basis
    assert state["cycles"][1]["research_proof_basis_sha256"] == second_basis
    assert state["ready_for_export"] is True
    assert state["consecutive_stable_passes"] == 2


def test_two_cycle_shadow_stability_ignores_rephrased_causal_narration(
    tmp_path: Path,
) -> None:
    dossier = _proof_basis_dossier()
    dossier["root_cause_hypotheses"] = [
        {
            "hypothesis_id": "h1",
            "falsification_attempts": [
                {
                    "attempt_id": "attempt:one",
                    "claim": "The guard controls whether the failure appears.",
                    "baseline_experiment_id": "exp-support",
                    "challenge_experiment_id": "exp-control",
                    "outcome": "survived",
                }
            ],
        }
    ]
    first_basis = _proof_basis_sha256(dossier)
    changed = deepcopy(dossier)
    changed["root_cause_hypotheses"][0]["falsification_attempts"][0]["claim"] = (
        "Whether the failure appears is controlled by the guard."
    )
    verification = changed["evidence_verification"]
    verification["hypothesis_refs"][0]["control_links"][0]["controlled_variable"] = (
        "the guard input"
    )
    verification["hypothesis_refs"][0]["control_links"][0]["expected_difference"] = (
        "the control run succeeds"
    )
    verification["control_verifications"][0]["relationship_sha256"] = "f" * 64

    second_basis = _proof_basis_sha256(changed)
    assert second_basis == first_basis

    inputs = _passing_inputs(tmp_path)
    first_report = evaluate_shadow_invariants(**inputs)
    first_report["research_proof_basis_sha256"] = first_basis
    second_report = dict(first_report)
    second_report["research_proof_basis_sha256"] = second_basis
    backlog_path = tmp_path / "target.backlog.json"
    _write_json(backlog_path, inputs["backlog"])
    state_path = shadow_state_path(backlog_path)
    _record_cycle(
        tmp_path,
        state_path=state_path,
        backlog_path=backlog_path,
        invariant_report=first_report,
        generated_at="2026-07-09T00:00:00Z",
    )
    state = _record_cycle(
        tmp_path,
        state_path=state_path,
        backlog_path=backlog_path,
        invariant_report=second_report,
        generated_at="2026-07-09T01:00:00Z",
    )

    assert state["consecutive_stable_passes"] == 2
    assert state["ready_for_export"] is True


def test_proof_basis_uses_image_id_not_mutable_tag_or_run_paths() -> None:
    dossier = _proof_basis_dossier()
    first_basis = _proof_basis_sha256(dossier)
    changed = deepcopy(dossier)
    verification = changed["evidence_verification"]
    assert isinstance(verification, dict)
    verification["replay_isolation"]["trust_reason"] = "other-mutable-tag:latest"
    verification["workspace_dir"] = "D:/another-cycle/research-workspace"
    verification["run_dir"] = "D:/another-cycle/run"
    experiment = verification["experiments"][0]
    experiment["workspace_dir"] = "D:/another-cycle/replay-workspace"
    experiment["stdout_path"] = "D:/another-cycle/stdout.txt"
    experiment["stderr_path"] = "D:/another-cycle/stderr.txt"
    experiment["execution_metadata"]["image_tag"] = "other-mutable-tag:latest"
    experiment["execution_metadata"]["container_name"] = "another-container"
    verification["artifacts"][0]["declared_path"] = "D:/another-cycle/run/artifacts/repro.txt"
    verification["artifacts"][0]["path"] = "D:/another-cycle/run/artifacts/repro.txt"
    experiment["artifact_refs"] = ["D:/another-cycle/run/artifacts/repro.txt"]
    verification["hypothesis_refs"][0]["control_links"][0]["shared_artifact_refs"] = [
        "D:/another-cycle/run/artifacts/repro.txt"
    ]

    assert _proof_basis_sha256(changed) == first_basis


def test_proof_basis_rejects_docker_receipt_without_immutable_image_id() -> None:
    dossier = _proof_basis_dossier()
    verification = dossier["evidence_verification"]
    assert isinstance(verification, dict)
    verification["experiments"][0]["execution_metadata"]["image_id"] = None

    _, errors = shadow_mod._research_proof_basis_projection([dossier])

    assert errors == ["research_proof_basis_docker_image_unresolved:case:proof:exp-support"]


def test_shadow_invariant_hashes_each_ready_runner_proof(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dossier = _proof_basis_dossier()
    dossier["case_id"] = "case:one"
    dossier["problem_id"] = "problem:one"
    dossier["research_status"] = "blocked"
    dossier["blocking_reasons"] = ["Fixture stops after proof-basis validation."]
    inputs = _passing_inputs(tmp_path)
    inputs["stage3"] = {"items": [dossier]}
    monkeypatch.setattr(shadow_mod, "assess_research_readiness", lambda _: (True, []))
    monkeypatch.setattr(
        shadow_mod,
        "verify_persisted_research_evidence",
        lambda _: (True, []),
    )

    report = evaluate_shadow_invariants(**inputs)

    assert report["passed"] is True
    assert report["research_proof_basis_sha256"] == _proof_basis_sha256(dossier)


def test_shadow_invariant_fails_closed_for_ready_mutable_docker_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dossier = _proof_basis_dossier()
    dossier["case_id"] = "case:one"
    dossier["problem_id"] = "problem:one"
    dossier["research_status"] = "blocked"
    dossier["blocking_reasons"] = ["Fixture stops after proof-basis validation."]
    verification = dossier["evidence_verification"]
    assert isinstance(verification, dict)
    verification["experiments"][0]["execution_metadata"]["image_id"] = None
    inputs = _passing_inputs(tmp_path)
    inputs["stage3"] = {"items": [dossier]}
    monkeypatch.setattr(shadow_mod, "assess_research_readiness", lambda _: (True, []))
    monkeypatch.setattr(
        shadow_mod,
        "verify_persisted_research_evidence",
        lambda _: (True, []),
    )

    report = evaluate_shadow_invariants(**inputs)

    assert report["passed"] is False
    assert "research_proof_basis_docker_image_unresolved:case:one:exp-support" in report["failures"]


def test_bound_export_input_change_resets_stability_streak(tmp_path: Path) -> None:
    inputs = _passing_inputs(tmp_path)
    report = evaluate_shadow_invariants(**inputs)
    report["export_projection_sha256"] = "1" * 64
    backlog_path = tmp_path / "target.backlog.json"
    _write_json(backlog_path, inputs["backlog"])
    state_path = shadow_state_path(backlog_path)
    artifacts = _cycle_artifacts(tmp_path)
    policy_path = tmp_path / "backlog_policy.yaml"
    policy_path.write_text("backlog_policy:\n  version: 1\n", encoding="utf-8")
    artifacts["config.policy"] = policy_path
    first = record_shadow_cycle(
        state_path=state_path,
        backlog_path=backlog_path,
        invariant_report=report,
        artifact_paths=artifacts,
        generated_at="2026-07-09T00:00:00Z",
    )
    policy_path.write_text("backlog_policy:\n  version: 2\n", encoding="utf-8")

    second = record_shadow_cycle(
        state_path=state_path,
        backlog_path=backlog_path,
        invariant_report=report,
        artifact_paths=artifacts,
        generated_at="2026-07-09T01:00:00Z",
    )

    assert (
        first["cycles"][-1]["export_inputs_sha256"] != second["cycles"][-1]["export_inputs_sha256"]
    )
    assert second["ready_for_export"] is False
    assert second["consecutive_stable_passes"] == 1


def test_generated_stage_byte_drift_does_not_make_stability_impossible(
    tmp_path: Path,
) -> None:
    inputs = _passing_inputs(tmp_path)
    report = evaluate_shadow_invariants(**inputs)
    report["export_projection_sha256"] = "1" * 64
    backlog_path = tmp_path / "target.backlog.json"
    _write_json(backlog_path, inputs["backlog"])
    state_path = shadow_state_path(backlog_path)
    artifacts = _cycle_artifacts(tmp_path)
    config_path = tmp_path / "backlog_policy.yaml"
    config_path.write_text("backlog_policy:\n  version: 1\n", encoding="utf-8")
    artifacts["config.policy"] = config_path
    first = record_shadow_cycle(
        state_path=state_path,
        backlog_path=backlog_path,
        invariant_report=report,
        artifact_paths=artifacts,
        generated_at="2026-07-09T00:00:00Z",
    )
    _write_json(
        artifacts["research"],
        {"artifact": "research", "generated_at": "2026-07-09T01:00:00Z"},
    )

    second = record_shadow_cycle(
        state_path=state_path,
        backlog_path=backlog_path,
        invariant_report=report,
        artifact_paths=artifacts,
        generated_at="2026-07-09T01:00:00Z",
    )

    assert first["cycles"][-1]["run_identity_sha256"] != second["cycles"][-1]["run_identity_sha256"]
    assert (
        first["cycles"][-1]["export_inputs_sha256"] == second["cycles"][-1]["export_inputs_sha256"]
    )
    assert second["ready_for_export"] is True
    assert second["consecutive_stable_passes"] == 2


def test_operational_cycle_preserves_but_does_not_extend_release_qualification(
    tmp_path: Path,
) -> None:
    inputs = _passing_inputs(tmp_path)
    release_report = evaluate_shadow_invariants(**inputs)
    release_report["export_projection_sha256"] = "1" * 64
    operational_report = evaluate_shadow_invariants(**inputs, cycle_mode="operational")
    operational_report["export_projection_sha256"] = "2" * 64
    backlog_path = tmp_path / "target.backlog.json"
    _write_json(backlog_path, inputs["backlog"])
    state_path = shadow_state_path(backlog_path)
    artifacts = _cycle_artifacts(tmp_path)
    config_path = tmp_path / "backlog_policy.yaml"
    config_path.write_text("backlog_policy:\n  version: 1\n", encoding="utf-8")
    artifacts["config.policy"] = config_path

    first = record_shadow_cycle(
        state_path=state_path,
        backlog_path=backlog_path,
        invariant_report=release_report,
        artifact_paths=artifacts,
        generated_at="2026-07-09T00:00:00Z",
    )
    second = record_shadow_cycle(
        state_path=state_path,
        backlog_path=backlog_path,
        invariant_report=release_report,
        artifact_paths=artifacts,
        generated_at="2026-07-09T01:00:00Z",
    )
    operational = record_shadow_cycle(
        state_path=state_path,
        backlog_path=backlog_path,
        invariant_report=operational_report,
        artifact_paths=artifacts,
        generated_at="2026-07-09T02:00:00Z",
    )

    assert first["ready_for_export"] is False
    assert second["ready_for_export"] is True
    assert operational["ready_for_export"] is True
    assert operational["activation_mode"] == "operational_bound"
    assert operational["consecutive_stable_passes"] == 2
    assert operational["release_anchor_cycle_ids"] == [
        first["cycles"][-1]["cycle_id"],
        second["cycles"][-1]["cycle_id"],
    ]
    assert operational["cycles"][-1]["cycle_mode"] == "operational"
    assert operational["cycles"][-1]["qualification"]["qualification_class"] == (
        "unqualified"
    )


def test_operational_cycle_refuses_export_after_pipeline_config_drift(tmp_path: Path) -> None:
    inputs = _passing_inputs(tmp_path)
    release_report = evaluate_shadow_invariants(**inputs)
    release_report["export_projection_sha256"] = "1" * 64
    operational_report = evaluate_shadow_invariants(**inputs, cycle_mode="operational")
    operational_report["export_projection_sha256"] = "2" * 64
    backlog_path = tmp_path / "target.backlog.json"
    _write_json(backlog_path, inputs["backlog"])
    state_path = shadow_state_path(backlog_path)
    artifacts = _cycle_artifacts(tmp_path)
    config_path = tmp_path / "backlog_policy.yaml"
    config_path.write_text("backlog_policy:\n  version: 1\n", encoding="utf-8")
    artifacts["config.policy"] = config_path

    record_shadow_cycle(
        state_path=state_path,
        backlog_path=backlog_path,
        invariant_report=release_report,
        artifact_paths=artifacts,
        generated_at="2026-07-09T00:00:00Z",
    )
    record_shadow_cycle(
        state_path=state_path,
        backlog_path=backlog_path,
        invariant_report=release_report,
        artifact_paths=artifacts,
        generated_at="2026-07-09T01:00:00Z",
    )
    config_path.write_text("backlog_policy:\n  version: 2\n", encoding="utf-8")
    operational = record_shadow_cycle(
        state_path=state_path,
        backlog_path=backlog_path,
        invariant_report=operational_report,
        artifact_paths=artifacts,
        generated_at="2026-07-09T02:00:00Z",
    )

    assert operational["ready_for_export"] is False
    assert operational["activation_mode"] is None
    assert operational["consecutive_stable_passes"] == 0
    assert operational["release_anchor_cycle_ids"] == []


def test_release_anchor_survives_operational_cycle_retention_window(tmp_path: Path) -> None:
    inputs = _passing_inputs(tmp_path)
    release_report = evaluate_shadow_invariants(**inputs)
    release_report["export_projection_sha256"] = "1" * 64
    operational_report = evaluate_shadow_invariants(**inputs, cycle_mode="operational")
    operational_report["export_projection_sha256"] = "2" * 64
    backlog_path = tmp_path / "target.backlog.json"
    _write_json(backlog_path, inputs["backlog"])
    state_path = shadow_state_path(backlog_path)
    artifacts = _cycle_artifacts(tmp_path)
    first = record_shadow_cycle(
        state_path=state_path,
        backlog_path=backlog_path,
        invariant_report=release_report,
        artifact_paths=artifacts,
        generated_at="2026-07-09T00:00:00Z",
    )
    second = record_shadow_cycle(
        state_path=state_path,
        backlog_path=backlog_path,
        invariant_report=release_report,
        artifact_paths=artifacts,
        generated_at="2026-07-09T00:01:00Z",
    )
    state = second
    for index in range(12):
        state = record_shadow_cycle(
            state_path=state_path,
            backlog_path=backlog_path,
            invariant_report=operational_report,
            artifact_paths=artifacts,
            generated_at=f"2026-07-09T01:{index:02d}:00Z",
        )

    assert state["ready_for_export"] is True
    assert state["activation_mode"] == "operational_bound"
    assert state["release_anchor_cycle_ids"] == [
        first["cycles"][-1]["cycle_id"],
        second["cycles"][-1]["cycle_id"],
    ]
    retained_modes = [cycle["cycle_mode"] for cycle in state["cycles"]]
    assert retained_modes.count("release") == 2
    assert retained_modes.count("operational") == 10


def test_new_release_failure_invalidates_an_older_positive_anchor(tmp_path: Path) -> None:
    inputs = _passing_inputs(tmp_path)
    release_report = evaluate_shadow_invariants(**inputs)
    release_report["export_projection_sha256"] = "1" * 64
    failed_release_report = deepcopy(release_report)
    failed_release_report["passed"] = False
    failed_release_report["failures"] = ["independent_adjudication_rejected"]
    operational_report = evaluate_shadow_invariants(**inputs, cycle_mode="operational")
    operational_report["export_projection_sha256"] = "2" * 64
    backlog_path = tmp_path / "target.backlog.json"
    _write_json(backlog_path, inputs["backlog"])
    state_path = shadow_state_path(backlog_path)
    artifacts = _cycle_artifacts(tmp_path)

    for index in range(2):
        record_shadow_cycle(
            state_path=state_path,
            backlog_path=backlog_path,
            invariant_report=release_report,
            artifact_paths=artifacts,
            generated_at=f"2026-07-09T00:0{index}:00Z",
        )
    failed = record_shadow_cycle(
        state_path=state_path,
        backlog_path=backlog_path,
        invariant_report=failed_release_report,
        artifact_paths=artifacts,
        generated_at="2026-07-09T00:02:00Z",
    )
    operational = record_shadow_cycle(
        state_path=state_path,
        backlog_path=backlog_path,
        invariant_report=operational_report,
        artifact_paths=artifacts,
        generated_at="2026-07-09T00:03:00Z",
    )

    assert failed["ready_for_export"] is False
    assert operational["ready_for_export"] is False
    assert operational["release_anchor_cycle_ids"] == []


def test_operational_pending_receipt_allows_ux_but_rejects_stage_drift(
    tmp_path: Path,
) -> None:
    backlog_path = tmp_path / "target.backlog.json"
    _write_json(backlog_path, {"generated_at_utc": "2026-07-09T00:00:00Z", "tickets": []})
    artifact_paths = _cycle_artifacts(tmp_path)
    ux_path = tmp_path / "target.ux_review.json"
    artifact_paths["ux.review_json"] = ux_path
    pending_path = operational_shadow_pending_run_path(backlog_path)
    write_pending_operational_shadow_run(
        pending_path=pending_path,
        backlog_path=backlog_path,
        artifact_paths=artifact_paths,
        generated_at="2026-07-09T00:00:00Z",
    )

    _write_json(ux_path, {"status": "reviewed"})
    _pending, errors = validate_pending_operational_shadow_run(
        pending_path=pending_path,
        backlog_path=backlog_path,
        artifact_paths=artifact_paths,
    )
    assert errors == []

    _write_json(artifact_paths["research"], {"artifact": "research", "changed": True})
    _pending, errors = validate_pending_operational_shadow_run(
        pending_path=pending_path,
        backlog_path=backlog_path,
        artifact_paths=artifact_paths,
    )
    assert "pending_operational_shadow_run_materialized_artifacts_changed" in errors


@pytest.mark.parametrize(
    "config",
    [
        {"enabled": "yes"},
        {"required_consecutive_shadow_cycles": 0},
        {"required_consecutive_shadow_cycles": True},
        {"require_exact_export_projection": "yes"},
        {"require_nonempty_throughput": "yes"},
        {"minimum_evidence_sufficient_research_proofs": 0},
        {"minimum_evidence_sufficient_research_proofs": True},
        {"minimum_authoritative_ready_tickets": 0},
        {"minimum_authoritative_ready_tickets": True},
        {"minimum_good_ticket_count": 0},
        {"minimum_good_ticket_count": True},
        {"minimum_good_to_bad_ratio": 1.0},
        {"minimum_good_to_bad_ratio": True},
        {"minimum_recovered_to_missed_ratio": 1.0},
        {"minimum_recovered_to_missed_ratio": True},
        {"require_zero_unknown_authoritative_tickets": "yes"},
        {"fail_on_systemic_research_blockers": "yes"},
        {"qualification_corpus_manifest_path": 42},
        {"qualification_output_adjudication_path": ""},
        {"no_actionable_evidence_receipt_path": []},
    ],
)
def test_shadow_gate_rejects_invalid_config(config: object) -> None:
    with pytest.raises(ValueError):
        normalize_shadow_gate_config(config)


def test_shadow_gate_defaults_require_productive_depth() -> None:
    config = normalize_shadow_gate_config({"enabled": True})

    assert config["require_nonempty_throughput"] is True
    assert config["minimum_evidence_sufficient_research_proofs"] == 1
    assert config["minimum_authoritative_ready_tickets"] == 1
    assert config["minimum_good_ticket_count"] == 1
    assert config["minimum_good_to_bad_ratio"] == 2.0
    assert config["minimum_recovered_to_missed_ratio"] == 2.0
    assert config["require_zero_unknown_authoritative_tickets"] is True
    assert config["fail_on_systemic_research_blockers"] is True
    assert config["qualification_corpus_manifest_path"] is None
    assert config["qualification_output_adjudication_path"] is None
    assert config["no_actionable_evidence_receipt_path"] is None


@pytest.mark.parametrize(
    ("config", "message"),
    [
        (
            {"require_exact_backlog_hash": True},
            "require_exact_backlog_hash was replaced by require_exact_export_projection",
        ),
        ({"unexpected_switch": True}, "contains unknown fields: unexpected_switch"),
    ],
)
def test_shadow_gate_rejects_legacy_and_unknown_fields(
    config: dict[str, object], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        normalize_shadow_gate_config(config)


def test_shadow_cycle_rejects_duplicate_runner_provenance(tmp_path: Path) -> None:
    inputs = _passing_inputs(tmp_path)
    report = evaluate_shadow_invariants(**inputs)
    backlog_path = tmp_path / "target.backlog.json"
    _write_json(backlog_path, inputs["backlog"])
    kwargs = {
        "state_path": shadow_state_path(backlog_path),
        "backlog_path": backlog_path,
        "invariant_report": report,
        "generated_at": "2026-07-09T00:00:00Z",
    }
    _record_cycle(tmp_path, **kwargs)

    with pytest.raises(ValueError, match="shadow_cycle_duplicate"):
        _record_cycle(tmp_path, **kwargs)


def test_pending_shadow_run_allows_fresh_adjudication_but_binds_model_artifacts(
    tmp_path: Path,
) -> None:
    backlog_path = tmp_path / "target.backlog.json"
    _write_json(backlog_path, {"tickets": []})
    artifacts = _cycle_artifacts(tmp_path)
    output_adjudication_path = tmp_path / "held-out" / "output.json"
    _write_json(output_adjudication_path, {"version": "pre-run"})
    pending_path = shadow_pending_run_path(backlog_path)
    artifacts["qualification.output_adjudication"] = output_adjudication_path
    artifacts["qualification.pending_run_receipt"] = pending_path
    pre_hash = sha256(output_adjudication_path.read_bytes()).hexdigest()
    write_pending_shadow_run(
        pending_path=pending_path,
        backlog_path=backlog_path,
        artifact_paths=artifacts,
        qualification_manifest_sha256_expected="a" * 64,
        output_adjudication_sha256_pre_run=pre_hash,
        generated_at="2026-07-11T12:00:00Z",
    )

    _write_json(output_adjudication_path, {"version": "post-run-independent"})
    _pending, errors = validate_pending_shadow_run(
        pending_path=pending_path,
        backlog_path=backlog_path,
        artifact_paths=artifacts,
    )

    assert errors == []

    _write_json(artifacts["research"], {"artifact": "mutated-research"})
    _pending, errors = validate_pending_shadow_run(
        pending_path=pending_path,
        backlog_path=backlog_path,
        artifact_paths=artifacts,
    )

    assert "pending_shadow_run_materialized_artifacts_changed" in errors


def test_shadow_state_rejects_duplicate_cycle_rows(tmp_path: Path) -> None:
    inputs = _passing_inputs(tmp_path)
    report = evaluate_shadow_invariants(**inputs)
    backlog_path = tmp_path / "target.backlog.json"
    _write_json(backlog_path, inputs["backlog"])
    state_path = shadow_state_path(backlog_path)
    state = _record_cycle(
        tmp_path,
        state_path=state_path,
        backlog_path=backlog_path,
        invariant_report=report,
        generated_at="2026-07-09T00:00:00Z",
    )
    state["cycles"].append(deepcopy(state["cycles"][0]))
    state["ready_for_export"] = True
    state["consecutive_stable_passes"] = 2
    state["validated_cycle_id"] = state["cycles"][-1]["cycle_id"]
    state["validated_backlog_sha256"] = state["cycles"][-1]["backlog_sha256"]
    state["validated_backlog_content_sha256"] = state["cycles"][-1]["backlog_content_sha256"]
    state["validated_research_proof_basis_sha256"] = state["cycles"][-1][
        "research_proof_basis_sha256"
    ]
    _write_json(state_path, state)

    ready, reasons, _ = validate_shadow_export_state(
        state_path=state_path,
        backlog_path=backlog_path,
    )

    assert ready is False
    assert "shadow_state_duplicate_cycle_ids" in reasons


def test_shadow_state_rejects_non_object_cycle_without_crashing(tmp_path: Path) -> None:
    backlog_path = tmp_path / "target.backlog.json"
    _write_json(backlog_path, {"tickets": []})
    state_path = shadow_state_path(backlog_path)
    _write_json(
        state_path,
        {
            "schema_version": 5,
            "backlog_path": str(backlog_path.resolve()),
            "ready_for_export": False,
            "required_consecutive_cycles": 2,
            "require_exact_export_projection": True,
            "consecutive_stable_passes": 0,
            "validated_cycle_id": None,
            "validated_backlog_sha256": None,
            "validated_backlog_content_sha256": None,
            "validated_export_inputs_sha256": None,
            "validated_research_proof_basis_sha256": None,
            "cycles": ["not-an-object"],
        },
    )

    ready, reasons, _ = validate_shadow_export_state(
        state_path=state_path,
        backlog_path=backlog_path,
    )

    assert ready is False
    assert "shadow_state_cycles_invalid" in reasons


def test_shadow_state_rejects_pre_proof_basis_schema_without_migration(
    tmp_path: Path,
) -> None:
    inputs = _passing_inputs(tmp_path)
    report = evaluate_shadow_invariants(**inputs)
    backlog_path = tmp_path / "target.backlog.json"
    _write_json(backlog_path, inputs["backlog"])
    state_path = shadow_state_path(backlog_path)
    state = _record_cycle(
        tmp_path,
        state_path=state_path,
        backlog_path=backlog_path,
        invariant_report=report,
        generated_at="2026-07-09T00:00:00Z",
    )
    state["schema_version"] = 4
    _write_json(state_path, state)

    ready, reasons, _ = validate_shadow_export_state(
        state_path=state_path,
        backlog_path=backlog_path,
    )

    assert ready is False
    assert "shadow_state_schema_unsupported" in reasons


@pytest.mark.parametrize(
    ("mutation", "expected_reason"),
    [
        (
            lambda cycle: cycle.__setitem__("backlog_sha256", "not-a-sha"),
            "shadow_cycle_backlog_hash_invalid",
        ),
        (
            lambda cycle: cycle.update({"passed": True, "failures": ["forced"]}),
            "shadow_invariant_passed_failures_contradictory",
        ),
        (
            lambda cycle: cycle.__setitem__("cycle_schema_version", 999),
            "shadow_cycle_schema_invalid",
        ),
    ],
)
def test_shadow_state_rejects_malformed_or_contradictory_cycle_rows(
    tmp_path: Path,
    mutation: object,
    expected_reason: str,
) -> None:
    inputs = _passing_inputs(tmp_path)
    report = evaluate_shadow_invariants(**inputs)
    backlog_path = tmp_path / "target.backlog.json"
    _write_json(backlog_path, inputs["backlog"])
    state_path = shadow_state_path(backlog_path)
    state = _record_cycle(
        tmp_path,
        state_path=state_path,
        backlog_path=backlog_path,
        invariant_report=report,
        generated_at="2026-07-09T00:00:00Z",
    )
    assert callable(mutation)
    mutation(state["cycles"][-1])
    _write_json(state_path, state)

    ready, reasons, _ = validate_shadow_export_state(
        state_path=state_path,
        backlog_path=backlog_path,
    )

    assert ready is False
    assert any(reason.startswith(expected_reason) for reason in reasons)


def test_forged_top_hash_cannot_rebind_latest_qualifying_cycle(tmp_path: Path) -> None:
    inputs = _passing_inputs(tmp_path)
    report = evaluate_shadow_invariants(**inputs)
    backlog_path = tmp_path / "target.backlog.json"
    _write_json(backlog_path, inputs["backlog"])
    state_path = shadow_state_path(backlog_path)
    _record_cycle(
        tmp_path,
        state_path=state_path,
        backlog_path=backlog_path,
        invariant_report=report,
        generated_at="2026-07-09T00:00:00Z",
    )
    state = _record_cycle(
        tmp_path,
        state_path=state_path,
        backlog_path=backlog_path,
        invariant_report=report,
        generated_at="2026-07-09T01:00:00Z",
    )
    _write_json(backlog_path, {"tickets": [{"case_id": "case:forged"}]})
    current_bytes = backlog_path.read_bytes()
    state["validated_backlog_sha256"] = sha256(current_bytes).hexdigest()
    state["validated_backlog_content_sha256"] = shadow_mod._backlog_content_sha256(backlog_path)
    _write_json(state_path, state)

    ready, reasons, _ = validate_shadow_export_state(
        state_path=state_path,
        backlog_path=backlog_path,
    )

    assert ready is False
    assert "shadow_state_validated_backlog_sha256_mismatch" in reasons
    assert "shadow_state_validated_backlog_content_sha256_mismatch" in reasons
    assert "backlog_changed_since_shadow_validation" in reasons


def test_forged_top_proof_basis_hash_cannot_rebind_qualifying_cycles(
    tmp_path: Path,
) -> None:
    inputs = _passing_inputs(tmp_path)
    report = evaluate_shadow_invariants(**inputs)
    backlog_path = tmp_path / "target.backlog.json"
    _write_json(backlog_path, inputs["backlog"])
    state_path = shadow_state_path(backlog_path)
    _record_cycle(
        tmp_path,
        state_path=state_path,
        backlog_path=backlog_path,
        invariant_report=report,
        generated_at="2026-07-09T00:00:00Z",
    )
    state = _record_cycle(
        tmp_path,
        state_path=state_path,
        backlog_path=backlog_path,
        invariant_report=report,
        generated_at="2026-07-09T01:00:00Z",
    )
    state["validated_research_proof_basis_sha256"] = "f" * 64
    _write_json(state_path, state)

    ready, reasons, _ = validate_shadow_export_state(
        state_path=state_path,
        backlog_path=backlog_path,
    )

    assert ready is False
    assert "shadow_state_validated_research_proof_basis_sha256_mismatch" in reasons


def test_shadow_state_rejects_tampered_retained_stage_artifact(tmp_path: Path) -> None:
    inputs = _passing_inputs(tmp_path)
    report = evaluate_shadow_invariants(**inputs)
    backlog_path = tmp_path / "target.backlog.json"
    _write_json(backlog_path, inputs["backlog"])
    state_path = shadow_state_path(backlog_path)
    _record_cycle(
        tmp_path,
        state_path=state_path,
        backlog_path=backlog_path,
        invariant_report=report,
        generated_at="2026-07-09T00:00:00Z",
    )
    state = _record_cycle(
        tmp_path,
        state_path=state_path,
        backlog_path=backlog_path,
        invariant_report=report,
        generated_at="2026-07-09T01:00:00Z",
    )
    snapshot = Path(state["cycles"][-1]["artifact_receipts"][0]["snapshot_path"])
    snapshot.write_text("tampered\n", encoding="utf-8")

    ready, reasons, _ = validate_shadow_export_state(
        state_path=state_path,
        backlog_path=backlog_path,
    )

    assert ready is False
    assert any("shadow_cycle_artifact_snapshot_changed" in reason for reason in reasons)


def test_shadow_state_rejects_latest_stage_source_drift(tmp_path: Path) -> None:
    inputs = _passing_inputs(tmp_path)
    report = evaluate_shadow_invariants(**inputs)
    report["export_projection_sha256"] = "1" * 64
    backlog_path = tmp_path / "target.backlog.json"
    _write_json(backlog_path, inputs["backlog"])
    state_path = shadow_state_path(backlog_path)
    artifacts = _cycle_artifacts(tmp_path)
    for hour in (0, 1):
        record_shadow_cycle(
            state_path=state_path,
            backlog_path=backlog_path,
            invariant_report=report,
            artifact_paths=artifacts,
            generated_at=f"2026-07-09T0{hour}:00:00Z",
        )
    _write_json(artifacts["research"], {"artifact": "research", "tampered": True})

    ready, reasons, _ = validate_shadow_export_state(
        state_path=state_path,
        backlog_path=backlog_path,
        artifact_paths=artifacts,
    )

    assert ready is False
    assert "shadow_cycle_source_artifact_changed:research" in reasons
    assert "shadow_cycle_expected_artifacts_mismatch" in reasons


def test_shadow_state_binds_absence_of_optional_input(tmp_path: Path) -> None:
    inputs = _passing_inputs(tmp_path)
    report = evaluate_shadow_invariants(**inputs)
    report["export_projection_sha256"] = "1" * 64
    backlog_path = tmp_path / "target.backlog.json"
    _write_json(backlog_path, inputs["backlog"])
    state_path = shadow_state_path(backlog_path)
    artifacts = _cycle_artifacts(tmp_path)
    optional = tmp_path / "ux-review.json"
    artifacts["ux_review"] = optional
    for hour in (0, 1):
        record_shadow_cycle(
            state_path=state_path,
            backlog_path=backlog_path,
            invariant_report=report,
            artifact_paths=artifacts,
            generated_at=f"2026-07-09T0{hour}:00:00Z",
        )
    optional.write_text("{}\n", encoding="utf-8")

    ready, reasons, _ = validate_shadow_export_state(
        state_path=state_path,
        backlog_path=backlog_path,
        artifact_paths=artifacts,
    )

    assert ready is False
    assert "shadow_cycle_absent_artifact_now_exists:ux_review" in reasons


def test_shadow_invariants_reject_unreviewed_derived_case_creation(tmp_path: Path) -> None:
    inputs = _passing_inputs(tmp_path)
    inputs["atoms"] = [
        _decided_atom(
            {
                "atom_id": "atom:derived",
                "severity_hint": "high",
                "evidence_role": "research",
                "disposition": "novel_case",
            },
            rationale="A runner classifier selected a distinct failure.",
        )
    ]

    report = evaluate_shadow_invariants(**inputs)

    assert report["passed"] is False
    assert set(report["failures"]) == {
        "derived_atom_novel_without_decision:atom:derived",
        "shadow_qualification_no_observed_automated_evidence",
    }


def test_shadow_requires_durable_work_for_receipted_novel_derived_failure(
    tmp_path: Path,
) -> None:
    inputs = _passing_inputs(tmp_path)
    inputs["atoms"] = [
        apply_atom_disposition_decision(
            {
                "atom_id": "atom:derived-novel",
                "severity_hint": "high",
                "evidence_role": "research",
                "evidence_class": "observed",
                "origin_run_id": "run:research-infrastructure",
                "origin_stage": "repro_research",
                "parent_case_id": "case:original",
                "case_id": "case:research-infrastructure",
                "derived_from_atom_ids": ["atom:original"],
                "supporting_case_ids": ["case:research-infrastructure"],
                "disposition": "novel_case",
                "novel_case_rationale": (
                    "The research runner failed independently before inspecting the parent case."
                ),
            },
            disposition="novel_case",
            source="runner_novel_case_classification",
            rationale="The runner recorded a distinct research-infrastructure failure.",
        )
    ]

    report = evaluate_shadow_invariants(**inputs)

    assert report["passed"] is False
    assert report["failures"] == [
        "high_severity_unresolved_without_active_work:atom:derived-novel"
    ]


def test_shadow_invariants_reject_high_atom_with_default_unresolved_disposition(
    tmp_path: Path,
) -> None:
    inputs = _passing_inputs(tmp_path)
    inputs["atoms"] = [
        {
            "atom_id": "atom:omitted-high",
            "severity_hint": "high",
            "evidence_role": "observation",
            "disposition": "unresolved",
            "disposition_status": "pending",
            "disposition_receipt": None,
        }
    ]

    report = evaluate_shadow_invariants(**inputs)

    assert report["passed"] is False
    assert set(report["failures"]) == {
        "source_atom_without_explicit_disposition:atom:omitted-high:disposition_decision_pending",
        "high_severity_atom_without_explicit_disposition:atom:omitted-high:"
        "disposition_decision_pending",
    }


def test_shadow_invariants_reject_low_source_atom_without_explicit_disposition(
    tmp_path: Path,
) -> None:
    inputs = _passing_inputs(tmp_path)
    inputs["atoms"] = [
        {
            "atom_id": "atom:omitted-low",
            "severity_hint": "low",
            "evidence_role": "observation",
            "disposition": "unresolved",
            "disposition_status": "pending",
            "disposition_receipt": None,
        }
    ]

    report = evaluate_shadow_invariants(**inputs)

    assert report["passed"] is False
    assert report["failures"] == [
        "source_atom_without_explicit_disposition:atom:omitted-low:disposition_decision_pending"
    ]


def test_shadow_invariants_reject_explicit_unresolved_without_active_work(
    tmp_path: Path,
) -> None:
    inputs = _passing_inputs(tmp_path)
    inputs["atoms"] = [
        _decided_atom(
            {
                "atom_id": "atom:explicit-high",
                "severity_hint": "high",
                "evidence_role": "observation",
                "disposition": "unresolved",
            },
            rationale="Relation review explicitly retained this atom for another cycle.",
        )
    ]

    report = evaluate_shadow_invariants(**inputs)

    assert report["passed"] is False
    assert report["failures"] == ["high_severity_unresolved_without_active_work:atom:explicit-high"]


def test_shadow_invariants_accept_unresolved_when_durable_mitigated_work_remains(
    tmp_path: Path,
) -> None:
    inputs = _passing_inputs(tmp_path)
    inputs["atoms"] = [
        _decided_atom(
            {
                "atom_id": "atom:high",
                "severity_hint": "high",
                "evidence_role": "observation",
                "evidence_class": "observed_failure",
                "source": "run_failure",
                "disposition": "unresolved",
                "case_id": "case:one",
            },
            rationale=(
                "The case remains selected for another faithful replay; the current "
                "mitigation is not resolution."
            ),
        )
    ]
    inputs["case_registry"]["cases"]["case:one"]["state"] = "mitigated"

    report = evaluate_shadow_invariants(**inputs)

    assert report["passed"] is True
    assert report["failures"] == []


def test_shadow_invariants_reject_terminal_case_without_validated_outcome(
    tmp_path: Path,
) -> None:
    inputs = _passing_inputs(tmp_path)
    inputs["case_registry"] = {
        "schema_version": 1,
        "cases": {"case:closed": {"state": "resolved", "plan_outcomes": {}}},
        "problem_id_to_case_id": {},
        "atom_id_to_case_id": {},
        "ticket_fingerprint_to_case_id": {},
    }

    report = evaluate_shadow_invariants(**inputs)

    assert report["passed"] is False
    assert report["failures"] == ["terminal_case_missing_validated_outcome:case:closed:resolved"]


def _qualification_evidence_retracted_case(case_id: str) -> dict[str, object]:
    receipt: dict[str, object] = {
        "schema_version": 1,
        "producer": "usertest_backlog.problem_mining",
        "receipt_kind": "case_evidence_retraction",
        "case_id": case_id,
        "prior_case_revision": 2,
        "prior_state": "active",
        "retracted_atom_ids": ["atom:retracted"],
        "remaining_source_evidence_atom_ids": [],
        "disposition_receipt_sha256_by_atom_id": {
            "atom:retracted": "f" * 64,
        },
        "qualification_feedback_sha256": "a" * 64,
        "corrected_author_response_sha256": "b" * 64,
        "author_workspace_manifest_sha256": "c" * 64,
        "source_problem_mining_evidence_receipt_file_sha256": "d" * 64,
        "source_problem_mining_evidence_receipt_sha256": "e" * 64,
        "resulting_state": "superseded",
        "resulting_case_revision": 3,
    }
    receipt["content_sha256"] = sha256(
        json.dumps(
            receipt,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return {
        "case_id": case_id,
        "state": "superseded",
        "superseded_reason": "qualification_evidence_retracted",
        "case_revision": 3,
        "evidence_atom_ids": [],
        "source_evidence_atom_ids": [],
        "derived_evidence_atom_ids": [],
        "occurrence_evidence_atom_ids": [],
        "evidence_retraction_receipts": [receipt],
        "plan_outcomes": {},
    }


def test_shadow_invariants_accept_hash_bound_qualification_evidence_retraction(
    tmp_path: Path,
) -> None:
    inputs = _passing_inputs(tmp_path)
    inputs["case_registry"]["cases"]["case:retracted"] = (
        _qualification_evidence_retracted_case("case:retracted")
    )

    report = evaluate_shadow_invariants(**inputs)

    assert report["passed"] is True
    assert report["failures"] == []


def test_shadow_invariants_reject_tampered_qualification_evidence_retraction(
    tmp_path: Path,
) -> None:
    inputs = _passing_inputs(tmp_path)
    retracted_case = _qualification_evidence_retracted_case("case:retracted")
    retracted_case["evidence_retraction_receipts"][0]["content_sha256"] = "0" * 64
    inputs["case_registry"]["cases"]["case:retracted"] = retracted_case

    report = evaluate_shadow_invariants(**inputs)

    assert report["passed"] is False
    assert report["failures"] == [
        "terminal_case_missing_validated_outcome:case:retracted:superseded"
    ]


def test_shadow_invariants_still_require_outcome_for_ordinary_superseded_case(
    tmp_path: Path,
) -> None:
    inputs = _passing_inputs(tmp_path)
    inputs["case_registry"]["cases"]["case:ordinary"] = {
        "case_id": "case:ordinary",
        "state": "superseded",
        "case_revision": 2,
        "superseded_reason": "implementation_replaced",
        "plan_outcomes": {},
    }

    report = evaluate_shadow_invariants(**inputs)

    assert report["passed"] is False
    assert report["failures"] == [
        "terminal_case_missing_validated_outcome:case:ordinary:superseded"
    ]


def test_shadow_relation_check_uses_action_specific_target_fields(tmp_path: Path) -> None:
    inputs = _passing_inputs(tmp_path)
    decisions: list[dict[str, object]] = [
        {
            "focus_id": "problem:a",
            "action": "merge",
            "target_ids": ["problem:b"],
        }
    ]
    relation_artifacts = _relation_review_artifacts(
        tmp_path,
        name="merge-relation",
        decisions=decisions,
        relations=[
            {
                "source_case_id": "case:b",
                "target_case_id": "case:a",
                "direction": "source_to_canonical",
                "relation_kind": "canonical_absorption",
                "decision_actions": ["merge"],
            }
        ],
    )
    base_stage1 = inputs["stage1"]
    inputs["stage1"] = {
        "items": [
            {
                "problem_id": "problem:a",
                "case_id": "case:a",
                "case_member_problem_ids": ["problem:a"],
            },
            {
                "problem_id": "problem:b",
                "case_id": "case:b",
                "case_member_problem_ids": ["problem:b"],
            },
        ],
        "input_meta": {
            **base_stage1["input_meta"],
            "relation_review_decision_count": 1,
        },
        "artifacts": {
            **relation_artifacts,
            "problem_mining_evidence_receipt": base_stage1["artifacts"][
                "problem_mining_evidence_receipt"
            ],
        },
    }
    inputs["stage2"] = {
        "items": [
            {
                "problem_id": f"problem:{suffix}",
                "case_id": f"case:{suffix}",
                "priority_bucket": "watch",
                "selected_for_research": True,
                "priority_rationale": "Lower urgency, but retained for causal research.",
            }
            for suffix in ("a", "b")
        ]
    }
    inputs["stage3"] = {
        "items": [
            {
                "problem_id": f"problem:{suffix}",
                "case_id": f"case:{suffix}",
                "research_status": "blocked",
                "blocking_reasons": ["Fixture stops after relation validation."],
            }
            for suffix in ("a", "b")
        ]
    }

    report = evaluate_shadow_invariants(**inputs)

    assert "relation_decision_not_applied:0:merge" in report["failures"]


def test_shadow_relation_check_accepts_exact_controller_downgrade(
    tmp_path: Path,
) -> None:
    decision: dict[str, object] = {
        "focus_id": "problem:a",
        "action": "same_cause_group",
        "group_id": "provisional:shared-mechanism",
        "member_ids": ["problem:a", "problem:b"],
        "evidence_atom_ids": ["atom:a", "atom:b"],
        "rationale": "The symptoms support a shared-mechanism research hypothesis.",
        "review_confidence": 0.9,
    }
    relation_artifacts = _relation_review_artifacts(
        tmp_path,
        name="downgraded-relation",
        decisions=[decision],
        relations=[],
    )
    suggestion = {
        **decision,
        "group_id": "cause:provisional:canonical",
        "_submitted_group_id": "provisional:shared-mechanism",
        "_provisional_same_cause": True,
    }
    stage1 = {
        "items": [
            {
                "problem_id": "problem:a",
                "case_id": "case:a",
                "case_member_problem_ids": ["problem:a"],
                "case_relation_actions": [
                    {
                        "action": "keep_separate",
                        "provisional_relation_suggestion": suggestion,
                        "relation_validation_errors": [
                            "collapse_not_reciprocal:case:b"
                        ],
                    }
                ],
            },
            {
                "problem_id": "problem:b",
                "case_id": "case:b",
                "case_member_problem_ids": ["problem:b"],
            },
        ],
        "input_meta": {"relation_review_decision_count": 1},
        "artifacts": relation_artifacts,
    }

    assert shadow_mod._relation_application_errors(stage1) == []

    suggestion["member_ids"] = ["problem:a", "problem:c"]
    assert shadow_mod._relation_application_errors(stage1) == [
        "relation_decision_not_applied:0:same_cause_group"
    ]


def test_shadow_relation_check_accepts_exact_applied_split_groups(tmp_path: Path) -> None:
    inputs = _passing_inputs(tmp_path)
    split_groups = [
        {"evidence_atom_ids": ["atom:one"]},
        {"evidence_atom_ids": ["atom:two"]},
    ]
    decisions: list[dict[str, object]] = [
        {
            "focus_id": "problem:parent",
            "action": "split",
            "split_groups": split_groups,
        }
    ]
    relation_artifacts = _relation_review_artifacts(
        tmp_path,
        name="split-relation",
        decisions=decisions,
        relations=[],
    )
    base_stage1 = inputs["stage1"]
    inputs["stage1"] = {
        "items": [
            {
                "problem_id": "problem:parent:split:1",
                "case_id": "case:child-1",
                "case_member_problem_ids": ["problem:parent:split:1"],
                "split_parent_problem_ids": ["problem:parent"],
                "evidence_atom_ids": ["atom:one"],
            },
            {
                "problem_id": "problem:parent:split:2",
                "case_id": "case:child-2",
                "case_member_problem_ids": ["problem:parent:split:2"],
                "split_parent_problem_ids": ["problem:parent"],
                "evidence_atom_ids": ["atom:two"],
            },
        ],
        "input_meta": {
            **base_stage1["input_meta"],
            "relation_review_decision_count": 1,
        },
        "artifacts": {
            **relation_artifacts,
            "problem_mining_evidence_receipt": base_stage1["artifacts"][
                "problem_mining_evidence_receipt"
            ],
        },
    }
    inputs["stage2"] = {
        "items": [
            {
                "problem_id": f"problem:parent:split:{index}",
                "case_id": f"case:child-{index}",
                "priority_bucket": "watch",
                "selected_for_research": True,
                "priority_rationale": "Lower urgency, but retained for causal research.",
            }
            for index in (1, 2)
        ]
    }
    inputs["stage3"] = {
        "items": [
            {
                "problem_id": f"problem:parent:split:{index}",
                "case_id": f"case:child-{index}",
                "research_status": "blocked",
                "blocking_reasons": ["Fixture stops after relation validation."],
            }
            for index in (1, 2)
        ]
    }

    report = evaluate_shadow_invariants(**inputs)

    assert report["passed"] is True
    assert not any("relation_split_not_applied" in error for error in report["failures"])


def test_shadow_relation_check_accepts_derived_split_returned_to_parent_lineage(
    tmp_path: Path,
) -> None:
    inputs = _passing_inputs(tmp_path)
    decisions: list[dict[str, object]] = [
        {
            "focus_id": "problem:parent",
            "action": "split",
            "split_groups": [
                {"evidence_atom_ids": ["atom:source"]},
                {"evidence_atom_ids": ["atom:derived"]},
            ],
        }
    ]
    relation_artifacts = _relation_review_artifacts(
        tmp_path,
        name="derived-return-split-relation",
        decisions=decisions,
        relations=[],
    )
    returned_group = {
        "schema_version": 1,
        "return_kind": "derived_evidence_parent_lineage",
        "split_from_case_id": "case:parent",
        "split_parent_problem_ids": ["problem:parent"],
        "returned_child_case_id": "case:derived-child",
        "returned_child_problem_id": "problem:parent:split:2",
        "evidence_atom_ids": ["atom:derived"],
        "parent_case_ids": ["case:existing-parent"],
    }
    returned_group["content_sha256"] = shadow_mod._canonical_hash(returned_group)
    base_stage1 = inputs["stage1"]
    inputs["stage1"] = {
        "items": [
            {
                "problem_id": "problem:parent:split:1",
                "case_id": "case:source-child",
                "case_member_problem_ids": ["problem:parent:split:1"],
                "split_parent_problem_ids": ["problem:parent"],
                "evidence_atom_ids": ["atom:source"],
            }
        ],
        "input_meta": {
            **base_stage1["input_meta"],
            "relation_review_decision_count": 1,
            "relation_review_derived_split_returns": [returned_group],
        },
        "artifacts": {
            **relation_artifacts,
            "problem_mining_evidence_receipt": base_stage1["artifacts"][
                "problem_mining_evidence_receipt"
            ],
        },
    }
    inputs["stage2"] = {
        "items": [
            {
                "problem_id": "problem:parent:split:1",
                "case_id": "case:source-child",
                "priority_bucket": "watch",
                "selected_for_research": True,
                "priority_rationale": "Source-backed child remains actionable.",
            }
        ]
    }
    inputs["stage3"] = {
        "items": [
            {
                "problem_id": "problem:parent:split:1",
                "case_id": "case:source-child",
                "research_status": "blocked",
                "blocking_reasons": ["Fixture stops after relation validation."],
            }
        ]
    }

    report = evaluate_shadow_invariants(**inputs)

    assert report["passed"] is True
    assert not any("relation_split_not_applied" in error for error in report["failures"])


def test_shadow_relation_check_accepts_applied_merge_with_exact_runner_edge(
    tmp_path: Path,
) -> None:
    inputs = _applied_merge_inputs(
        tmp_path,
        name="applied-merge",
        receipt_relations=[
            {
                "source_case_id": "case:b",
                "target_case_id": "case:a",
                "direction": "source_to_canonical",
                "relation_kind": "canonical_absorption",
                "decision_actions": ["merge"],
            }
        ],
    )

    report = evaluate_shadow_invariants(**inputs)

    assert report["passed"] is True
    assert report["failures"] == []


def test_shadow_relation_check_rejects_merge_without_objective_receipt_edge(
    tmp_path: Path,
) -> None:
    inputs = _applied_merge_inputs(
        tmp_path,
        name="edge-less-merge",
        receipt_relations=[],
    )

    report = evaluate_shadow_invariants(**inputs)

    assert report["passed"] is False
    assert report["failures"] == ["relation_review_receipt_edges_not_applied"]


def test_shadow_relation_check_rejects_raw_response_without_runner_receipt(
    tmp_path: Path,
) -> None:
    inputs = _applied_merge_inputs(
        tmp_path,
        name="missing-receipt",
        receipt_relations=[
            {
                "source_case_id": "case:b",
                "target_case_id": "case:a",
                "direction": "source_to_canonical",
                "relation_kind": "canonical_absorption",
                "decision_actions": ["merge"],
            }
        ],
    )
    del inputs["stage1"]["artifacts"]["relation_review_receipt"]

    report = evaluate_shadow_invariants(**inputs)

    assert report["passed"] is False
    assert report["failures"] == ["relation_review_receipt_missing"]


def test_shadow_relation_check_rejects_mutated_immutable_response_snapshot(
    tmp_path: Path,
) -> None:
    inputs = _applied_merge_inputs(
        tmp_path,
        name="mutated-response",
        receipt_relations=[
            {
                "source_case_id": "case:b",
                "target_case_id": "case:a",
                "direction": "source_to_canonical",
                "relation_kind": "canonical_absorption",
                "decision_actions": ["merge"],
            }
        ],
    )
    receipt_path = Path(inputs["stage1"]["artifacts"]["relation_review_receipt"])
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    response_snapshot_path = Path(receipt["relation_review_response_path"])
    _write_json(response_snapshot_path, [{"action": "keep_separate"}])

    report = evaluate_shadow_invariants(**inputs)

    assert report["passed"] is False
    assert report["failures"] == ["relation_review_response_snapshot_hash_mismatch"]


def test_shadow_revalidates_retained_research_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inputs = _passing_inputs(tmp_path)
    inputs["stage3"] = {"items": [{"case_id": "case:one", "problem_id": "problem:one"}]}
    inputs["stage6"] = {"items": [{"plan_revision_id": "plan:one"}]}
    inputs["backlog"] = {
        "tickets": [
            {
                "case_id": "case:one",
                "problem_id": "problem:one",
                "plan_revision_id": "plan:one",
                "stage": "ready_for_ticket",
            }
        ]
    }
    monkeypatch.setattr(shadow_mod, "assess_research_readiness", lambda _item: (True, []))
    monkeypatch.setattr(shadow_mod, "assess_ticket_readiness", lambda _item: (True, []))
    monkeypatch.setattr(
        shadow_mod,
        "verify_persisted_research_evidence",
        lambda _item: (False, ["research_artifact_changed:artifact:source"]),
    )

    report = evaluate_shadow_invariants(**inputs)

    assert report["passed"] is False
    assert "ready_ticket_without_ready_research:case:one" in report["failures"]


@pytest.mark.parametrize(
    ("missing_stage", "expected_failure"),
    [
        (
            "stage2",
            "case_conservation_missing:case:one:problem_mining->problem_prioritization",
        ),
        (
            "stage3",
            "case_conservation_missing:case:one:problem_prioritization->repro_research",
        ),
        (
            "stage4",
            "case_conservation_missing:case:one:repro_research->solution_optioning",
        ),
        (
            "stage5",
            "case_conservation_missing:case:one:solution_optioning->solution_selection",
        ),
        (
            "stage6",
            "case_conservation_missing:case:one:solution_selection->implementation_planning",
        ),
        (
            "backlog",
            "case_conservation_missing:case:one:implementation_planning->backlog",
        ),
    ],
)
def test_shadow_case_conservation_rejects_silent_stage_disappearance(
    tmp_path: Path, missing_stage: str, expected_failure: str
) -> None:
    inputs = _complete_conservation_inputs(tmp_path)
    if missing_stage == "backlog":
        inputs["backlog"] = {"tickets": []}
    else:
        document = inputs[missing_stage]
        assert isinstance(document, dict)
        document["items"] = []

    report = evaluate_shadow_invariants(**inputs)

    assert report["passed"] is False
    assert expected_failure in report["failures"]


@pytest.mark.parametrize(
    "explicit_stop",
    ["research_block", "no_safe_option", "not_required", "selection_reject", "plan_block"],
)
def test_shadow_case_conservation_accepts_only_explicit_stops(
    tmp_path: Path, explicit_stop: str
) -> None:
    inputs = _complete_conservation_inputs(tmp_path)
    if explicit_stop == "research_block":
        inputs["stage3"]["items"][0]["research_status"] = "blocked"  # type: ignore[index]
        inputs["stage3"]["items"][0]["blocking_reasons"] = [  # type: ignore[index]
            "The original artifact is unavailable."
        ]
    elif explicit_stop == "no_safe_option":
        inputs["stage4"] = {
            "items": [],
            "input_meta": {
                "optioning_outcomes": [
                    {
                        "case_id": "case:one",
                        "problem_id": "problem:one",
                        "optioning_status": "no_safe_option",
                        "decision_rationale": "Every mechanism has unacceptable risk.",
                    }
                ]
            },
        }
    elif explicit_stop == "not_required":
        inputs["stage3"]["items"][0]["actionability_assessment"] = {  # type: ignore[index]
            "disposition": "already_addressed",
            "rationale": "The pinned revision already contains the verified fix.",
            "evidence_refs": ["experiment:current"],
        }
        inputs["stage4"] = {
            "items": [],
            "input_meta": {
                "optioning_outcomes": [
                    {
                        "case_id": "case:one",
                        "problem_id": "problem:one",
                        "optioning_status": "not_required",
                        "research_actionability_disposition": "already_addressed",
                        "decision_rationale": "The pinned revision already contains the fix.",
                        "evidence_refs": ["experiment:current"],
                    }
                ]
            },
        }
    elif explicit_stop == "selection_reject":
        inputs["stage5"] = {
            "items": [],
            "input_meta": {
                "selection_outcomes": [
                    {
                        "case_id": "case:one",
                        "problem_id": "problem:one",
                        "selection_status": "reject",
                        "reasons": ["The falsifier disproved the causal coverage."],
                    }
                ]
            },
        }
    else:
        inputs["stage6"] = {
            "items": [],
            "input_meta": {
                "rejected_plans": [
                    {
                        "case_id": "case:one",
                        "problem_id": "problem:one",
                        "planning_status": "blocked",
                        "reasons": ["The exact change surface is not grounded."],
                    }
                ]
            },
        }

    report = evaluate_shadow_invariants(**inputs)

    assert not any(failure.startswith("case_conservation_") for failure in report["failures"])


def test_shadow_case_conservation_rejects_indefinite_priority_defer(tmp_path: Path) -> None:
    inputs = _complete_conservation_inputs(tmp_path)
    inputs["stage2"]["items"][0].update(  # type: ignore[index]
        {
            "priority_bucket": "watch",
            "selected_for_research": False,
            "priority_rationale": "Would otherwise strand a canonical problem.",
        }
    )

    report = evaluate_shadow_invariants(**inputs)

    assert "case_conservation_priority_disposition_invalid:case:one" in report["failures"]


@pytest.mark.parametrize("implicit_stop", ["priority", "research", "option", "selection", "plan"])
def test_shadow_case_conservation_rejects_reasonless_stop_markers(
    tmp_path: Path, implicit_stop: str
) -> None:
    inputs = _complete_conservation_inputs(tmp_path)
    if implicit_stop == "priority":
        inputs["stage2"]["items"][0].update(  # type: ignore[index]
            {
                "priority_bucket": "watch",
                "selected_for_research": False,
                "priority_rationale": "",
            }
        )
        expected = "case_conservation_priority_disposition_invalid:case:one"
    elif implicit_stop == "research":
        inputs["stage3"]["items"][0]["research_status"] = "blocked"  # type: ignore[index]
        expected = "case_conservation_research_disposition_invalid:case:one"
    elif implicit_stop == "option":
        inputs["stage4"] = {
            "items": [],
            "input_meta": {
                "optioning_outcomes": [
                    {
                        "case_id": "case:one",
                        "problem_id": "problem:one",
                        "optioning_status": "no_safe_option",
                        "decision_rationale": "",
                    }
                ]
            },
        }
        expected = "case_conservation_missing:case:one:repro_research->solution_optioning"
    elif implicit_stop == "selection":
        inputs["stage5"] = {
            "items": [],
            "input_meta": {
                "selection_outcomes": [
                    {
                        "case_id": "case:one",
                        "problem_id": "problem:one",
                        "selection_status": "reject",
                        "reasons": [],
                    }
                ]
            },
        }
        expected = "case_conservation_missing:case:one:solution_optioning->solution_selection"
    else:
        inputs["stage6"] = {
            "items": [],
            "input_meta": {
                "rejected_plans": [
                    {
                        "case_id": "case:one",
                        "problem_id": "problem:one",
                        "planning_status": "blocked",
                        "reasons": [],
                    }
                ]
            },
        }
        expected = "case_conservation_missing:case:one:solution_selection->implementation_planning"

    report = evaluate_shadow_invariants(**inputs)

    assert expected in report["failures"]


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("text", "A materially different observed failure."),
        ("severity_hint", "blocker"),
        ("parent_case_id", "case:different-parent"),
    ],
)
def test_atom_stability_hash_binds_evidence_content_severity_and_lineage(
    tmp_path: Path, field: str, replacement: str
) -> None:
    inputs = _passing_inputs(tmp_path)
    first = evaluate_shadow_invariants(**inputs)
    changed = deepcopy(inputs)
    changed["atoms"][0][field] = replacement  # type: ignore[index]

    second = evaluate_shadow_invariants(**changed)

    assert first["atom_corpus_sha256"] != second["atom_corpus_sha256"]
    assert first["source_atom_corpus_sha256"] != second["source_atom_corpus_sha256"]


def test_complete_pipeline_manifest_addition_resets_shadow_streak(tmp_path: Path) -> None:
    source_root = tmp_path / "source"
    config_root = source_root / "configs"
    app_source = source_root / "apps" / "usertest_backlog" / "src" / "app.py"
    package_source = source_root / "packages" / "dependency" / "src" / "dep.py"
    for path, content in (
        (app_source, "APP = 1\n"),
        (package_source, "DEP = 1\n"),
        (config_root / "backlog.yaml", "enabled: true\n"),
        (source_root / "apps" / "usertest_backlog" / "pyproject.toml", "[project]\n"),
        (source_root / ".git" / "HEAD", "0123456789abcdef\n"),
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    inputs = _passing_inputs(tmp_path)
    report = evaluate_shadow_invariants(**inputs)
    report["export_projection_sha256"] = "1" * 64
    backlog_path = tmp_path / "target.backlog.json"
    _write_json(backlog_path, inputs["backlog"])
    state_path = shadow_state_path(backlog_path)
    artifacts = _cycle_artifacts(tmp_path)
    source_bindings = _pipeline_source_config_bindings(
        source_root=source_root,
        config_root=config_root,
    )
    assert app_source.resolve() in source_bindings.values()
    assert package_source.resolve() in source_bindings.values()
    assert (source_root / ".git" / "HEAD").resolve() in source_bindings.values()
    artifacts.update(source_bindings)
    first = record_shadow_cycle(
        state_path=state_path,
        backlog_path=backlog_path,
        invariant_report=report,
        artifact_paths=artifacts,
        generated_at="2026-07-09T00:00:00Z",
    )

    added_source = source_root / "packages" / "dependency" / "src" / "new_path.py"
    added_source.write_text("NEW_PATH = True\n", encoding="utf-8")
    next_artifacts = _cycle_artifacts(tmp_path)
    next_artifacts.update(
        _pipeline_source_config_bindings(
            source_root=source_root,
            config_root=config_root,
        )
    )
    second = record_shadow_cycle(
        state_path=state_path,
        backlog_path=backlog_path,
        invariant_report=report,
        artifact_paths=next_artifacts,
        generated_at="2026-07-09T01:00:00Z",
    )

    assert (
        first["cycles"][-1]["export_inputs_sha256"] != second["cycles"][-1]["export_inputs_sha256"]
    )
    assert second["ready_for_export"] is False
    assert second["consecutive_stable_passes"] == 1
