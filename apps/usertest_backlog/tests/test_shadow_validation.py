from __future__ import annotations

import json
from copy import deepcopy
from hashlib import sha256
from pathlib import Path

import pytest
from backlog_core.case_lineage import apply_atom_disposition_decision
from backlog_repo import write_case_relation_receipt

import usertest_backlog.workflows.shadow_validation as shadow_mod
from usertest_backlog.commands.export_tickets import _pipeline_source_config_bindings
from usertest_backlog.workflows.problem_mining_evidence import (
    build_problem_mining_evidence_draft,
    finalize_problem_mining_evidence_receipt,
    problem_mining_evidence_receipt_ref,
)
from usertest_backlog.workflows.shadow_validation import (
    evaluate_shadow_invariants,
    normalize_shadow_gate_config,
    record_shadow_cycle,
    shadow_state_path,
    validate_shadow_export_state,
)


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


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
            "artifacts": [
                {
                    "artifact_id": "artifact:repro",
                    "kind": "test_output",
                    "declared_path": "C:/volatile/run/artifacts/repro.txt",
                    "path": "C:/volatile/run/artifacts/repro.txt",
                    "sha256": "d" * 64,
                    "size_bytes": 20,
                }
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


def test_shadow_invariants_qualify_observed_case_with_durable_blocked_research(
    tmp_path: Path,
) -> None:
    inputs = _passing_inputs(tmp_path)

    report = evaluate_shadow_invariants(**inputs)

    assert report["passed"] is True
    assert report["failures"] == []
    assert report["counts"]["qualifying_observed_atoms"] == 1
    assert report["counts"]["cases"] == 1
    assert report["counts"]["research_proofs"] == 1
    assert inputs["stage3"]["items"][0]["research_status"] == "blocked"
    assert inputs["backlog"]["tickets"][0]["stage"] == "research_required"


@pytest.mark.parametrize("corpus_kind", ["empty", "proposal_only"])
def test_empty_or_proposal_only_cycles_are_recorded_but_never_qualify(
    tmp_path: Path,
    corpus_kind: str,
) -> None:
    inputs = _passing_inputs(tmp_path)
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
    assert third["ready_for_export"] is False
    assert third["consecutive_stable_passes"] == 1
    assert ready is False
    assert "stable_shadow_cycles_required:1/2" in reasons

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
    assert fourth["consecutive_stable_passes"] == 2
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
        "control_receipt",
        "causal_receipt",
        "repo_revision",
        "image_id",
    ],
)
def test_research_proof_basis_change_resets_stability_streak(
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
    elif changed_evidence == "control_receipt":
        verification["hypothesis_refs"][0]["control_links"][0]["expected_difference"] = (
            "control now fails"
        )
    elif changed_evidence == "causal_receipt":
        verification["causal_links"][0]["trace_excerpt_sha256"] = "6" * 64
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

    assert state["ready_for_export"] is False
    assert state["consecutive_stable_passes"] == 1


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


@pytest.mark.parametrize(
    "config",
    [
        {"enabled": "yes"},
        {"required_consecutive_shadow_cycles": 0},
        {"required_consecutive_shadow_cycles": True},
        {"require_exact_export_projection": "yes"},
    ],
)
def test_shadow_gate_rejects_invalid_config(config: object) -> None:
    with pytest.raises(ValueError):
        normalize_shadow_gate_config(config)


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
    ["research_block", "no_safe_option", "selection_reject", "plan_block"],
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
