from __future__ import annotations

from pathlib import Path
from typing import Any

from backlog_repo import write_case_relation_receipt

from usertest_backlog.workflows.problem_mining import (
    _persist_canonical_relation_receipts,
)
from usertest_backlog.workflows.shadow_validation import _terminal_outcome_errors
from usertest_backlog.workflows.staged import _sync_case_registry_outcomes


def _runner_receipt(plan_revision_id: str, evidence_kind: str) -> dict[str, object]:
    common = {
        "producer": "usertest_implement",
        "verification_producer": "runner_core",
        "evidence_kind": evidence_kind,
        "case_id": "case:aggregate",
        "plan_revision_id": plan_revision_id,
        "fingerprint": "1" * 16,
        "ticket_body_sha256": "4" * 64,
        "local_plan_sha256": "5" * 64,
        "local_plan_filename": "ticket.md",
        "verification_contract_sha256": "6" * 64,
        "target_contract_sha256": "8" * 64,
        "verified_implementation_head": "9" * 40,
    }
    if evidence_kind == "test":
        return {
            **common,
            "receipt_schema_version": 2,
            "run_dir": "runs/test",
            "verification_path": "runs/test/verification.json",
            "verification_sha256": "2" * 64,
            "ticket_ref_path": "runs/test/ticket.json",
            "ticket_ref_sha256": "3" * 64,
            "verification_binding_sha256": "7" * 64,
            "commands": ["pytest -q tests/test_x.py"],
        }
    return {
        **common,
        "receipt_schema_version": 3,
        "role_artifact_path": f"runs/{evidence_kind}/outcome_role.json",
        "role_artifact_sha256": "2" * 64,
        "role_contract_sha256": "7" * 64,
        "merged_commit": "abc123",
    }


def _resolved_outcome(plan_revision_id: str) -> dict[str, object]:
    return {
        "schema_version": 1,
        "case_id": "case:aggregate",
        "plan_revision_id": plan_revision_id,
        "state": "resolved",
        "outcome_scope": "case",
        "recorded_at": "2026-07-10T00:00:00Z",
        "requires_live_verification": False,
        "target_branch": "dev",
        "merged_commit": "abc123",
        "test_evidence": [
            {
                "kind": "pytest",
                "reference": "tests/test_x.py",
                "result": "passed",
                "runner_receipt": _runner_receipt(plan_revision_id, "test"),
            }
        ],
        "original_scenario_evidence": [
            {
                "kind": "replay",
                "reference": "runs/replay",
                "result": "passed",
                "runner_receipt": _runner_receipt(
                    plan_revision_id,
                    "original_scenario",
                ),
            }
        ],
        "live_evidence": [],
        "mitigation_evidence": [],
        "remaining_risks": [],
        "recurrence_check": {
            "status": "completed",
            "result": "passed",
            "evidence": [
                {
                    "kind": "replay",
                    "reference": "runs/recurrence",
                    "result": "passed",
                    "runner_receipt": _runner_receipt(
                        plan_revision_id,
                        "recurrence",
                    ),
                }
            ],
        },
    }


def _registry() -> dict[str, object]:
    return {
        "schema_version": 1,
        "cases": {
            "case:aggregate": {
                "case_id": "case:aggregate",
                "state": "active",
            }
        },
    }


def test_required_planned_revision_keeps_case_open_when_another_is_resolved() -> None:
    resolved = _resolved_outcome("plan:v1")
    registry = _registry()
    atom_actions = {
        "atom:1": {
            "case_id": "case:aggregate",
            "plan_outcomes": {
                "plan:v1": {
                    "state": "resolved",
                    "recorded_at": resolved["recorded_at"],
                    "required": True,
                    "outcome_record": resolved,
                },
                "plan:v2": {
                    "state": "planned",
                    "recorded_at": "2026-07-11T00:00:00Z",
                    "required": True,
                },
            },
        }
    }

    summary = _sync_case_registry_outcomes(
        case_registry=registry,
        atom_actions=atom_actions,
    )

    case = registry["cases"]["case:aggregate"]
    assert case["state"] == "planned"
    assert summary["terminal_cases"] == 0
    assert summary["nonterminal_cases"] == 1


def test_raw_terminal_projection_without_full_record_fails_open() -> None:
    registry = _registry()
    atom_actions = {
        "atom:1": {
            "case_id": "case:aggregate",
            "plan_outcomes": {
                "plan:v1": {
                    "state": "resolved",
                    "recorded_at": "2026-07-10T00:00:00Z",
                    "required": True,
                }
            },
        }
    }

    summary = _sync_case_registry_outcomes(
        case_registry=registry,
        atom_actions=atom_actions,
    )

    assert registry["cases"]["case:aggregate"]["state"] == "unverified"
    assert summary["terminal_cases"] == 0


def test_same_class_recurrence_keeps_old_resolved_plan_reopened(
    monkeypatch: Any,
) -> None:
    registry = _registry()
    case = registry["cases"]["case:aggregate"]
    case["state"] = "active"
    case["recurrence_reopen"] = {
        "from_state": "resolved",
        "against_plan_revision_id": "plan:v1",
        "case_revision": 3,
        "new_evidence_atom_ids": ["atom:new-recurrence"],
    }
    resolved = _resolved_outcome("plan:v1")
    atom_actions = {
        "atom:1": {
            "case_id": "case:aggregate",
            "plan_outcomes": {
                "plan:v1": {
                    "state": "resolved",
                    "recorded_at": resolved["recorded_at"],
                    "required": True,
                    "outcome_record": resolved,
                }
            },
        }
    }

    monkeypatch.setattr(
        "usertest_backlog.workflows.staged.verify_outcome_record_provenance",
        lambda record, **_: {
            "structural_status": "valid",
            "provenance_status": "verified",
            "verified": True,
            "errors": [],
            "outcome_record": record,
        },
    )
    summary = _sync_case_registry_outcomes(
        case_registry=registry,
        atom_actions=atom_actions,
    )

    updated = registry["cases"]["case:aggregate"]
    assert updated["state"] == "unverified"
    assert updated["last_outcome_state"] == "resolved"
    assert updated["current_lifecycle"]["outcome_reference"]["source"] == (
        "same_class_recurrence_reopen"
    )
    assert summary["terminal_cases"] == 0


def test_structurally_valid_terminal_record_without_retained_proof_stays_open() -> None:
    registry = _registry()
    resolved = _resolved_outcome("plan:v1")
    atom_actions = {
        "atom:1": {
            "case_id": "case:aggregate",
            "plan_outcomes": {
                "plan:v1": {
                    "state": "resolved",
                    "recorded_at": resolved["recorded_at"],
                    "required": True,
                    "outcome_record": resolved,
                }
            },
        }
    }

    summary = _sync_case_registry_outcomes(
        case_registry=registry,
        atom_actions=atom_actions,
    )

    assert registry["cases"]["case:aggregate"]["state"] == "unverified"
    assert summary["terminal_cases"] == 0
    assert summary["provenance_failed_outcome_records"] == 1


def test_plan_copy_outcome_never_changes_case_state() -> None:
    registry = _registry()
    plan_copy = {
        "schema_version": 1,
        "case_id": "case:aggregate",
        "plan_revision_id": "plan:v1",
        "state": "duplicate",
        "outcome_scope": "plan_copy",
        "recorded_at": "2026-07-10T00:00:00Z",
        "requires_live_verification": False,
        "target_branch": None,
        "merged_commit": None,
        "test_evidence": [],
        "original_scenario_evidence": [],
        "live_evidence": [],
        "remaining_risks": ["Duplicate file"],
        "recurrence_check": {"status": "not_run"},
        "related_case_id": "case:aggregate",
    }
    atom_actions = {
        "atom:1": {
            "case_id": "case:aggregate",
            "plan_outcomes": {
                "plan:v1": {
                    "state": "duplicate",
                    "recorded_at": "2026-07-10T00:00:00Z",
                    "required": True,
                    "outcome_record": plan_copy,
                }
            },
        }
    }

    summary = _sync_case_registry_outcomes(
        case_registry=registry,
        atom_actions=atom_actions,
    )

    assert registry["cases"]["case:aggregate"]["state"] == "active"
    assert summary["cases_updated"] == 0
    assert summary["terminal_cases"] == 0


def _relationship_outcome(
    *,
    source_case_id: str,
    target_case_id: str,
    receipt: dict[str, object] | None,
) -> dict[str, object]:
    outcome: dict[str, object] = {
        "schema_version": 1,
        "case_id": source_case_id,
        "plan_revision_id": "plan:relation:v1",
        "state": "duplicate",
        "outcome_scope": "case",
        "recorded_at": "2026-07-10T00:00:00Z",
        "requires_live_verification": False,
        "target_branch": None,
        "merged_commit": None,
        "test_evidence": [],
        "original_scenario_evidence": [],
        "live_evidence": [],
        "remaining_risks": [],
        "recurrence_check": {"status": "not_run"},
        "related_case_id": target_case_id,
    }
    if receipt is not None:
        outcome["relation_receipt"] = receipt
    return outcome


def _write_relation_receipt(
    tmp_path: Path,
    *,
    source_case_id: str,
    target_case_id: str,
) -> dict[str, object]:
    response_path = tmp_path / "runs" / "relation.response.txt"
    response_path.parent.mkdir(parents=True)
    response_path.write_text("[]\n", encoding="utf-8")
    _payload, references = write_case_relation_receipt(
        tmp_path / "runs" / "relation.relations.json",
        stage="problem_mining",
        relation_review_response_path=response_path,
        relations=[
            {
                "source_case_id": source_case_id,
                "target_case_id": target_case_id,
                "direction": "source_to_canonical",
                "relation_kind": "canonical_absorption",
                "decision_actions": ["merge"],
            }
        ],
    )
    return references[source_case_id]


def test_forged_duplicate_of_arbitrary_active_case_stays_open_and_fails_shadow(
    tmp_path: Path,
) -> None:
    source_case_id = "case:source"
    target_case_id = "case:arbitrary-active"
    receipt = _write_relation_receipt(
        tmp_path,
        source_case_id=source_case_id,
        target_case_id=target_case_id,
    )
    registry: dict[str, object] = {
        "schema_version": 1,
        "cases": {
            source_case_id: {"case_id": source_case_id, "state": "active"},
            target_case_id: {"case_id": target_case_id, "state": "active"},
        },
    }
    outcome = _relationship_outcome(
        source_case_id=source_case_id,
        target_case_id=target_case_id,
        receipt=receipt,
    )
    atom_actions = {
        "atom:source": {
            "case_id": source_case_id,
            "plan_outcomes": {
                "plan:relation:v1": {
                    "state": "duplicate",
                    "recorded_at": outcome["recorded_at"],
                    "required": True,
                    "outcome_record": outcome,
                }
            },
        }
    }

    summary = _sync_case_registry_outcomes(
        case_registry=registry,
        atom_actions=atom_actions,
        trusted_runs_roots=(tmp_path / "runs",),
    )

    source = registry["cases"][source_case_id]
    assert source["state"] == "unverified"
    assert summary["terminal_cases"] == 0
    assert summary["provenance_failed_outcome_records"] == 1
    shadow_errors = _terminal_outcome_errors(
        registry,
        trusted_runs_roots=(tmp_path / "runs",),
        owner_roots=(),
    )
    assert any(
        error.startswith(
            f"outcome_provenance_revalidation_failed:{source_case_id}:plan:relation:v1:"
        )
        for error in shadow_errors
    )


def test_problem_mining_persists_runner_relation_receipt_on_exact_registry_edge(
    tmp_path: Path,
) -> None:
    source_case_id = "case:absorbed"
    target_case_id = "case:canonical"
    response_path = tmp_path / "runs" / "relation.response.txt"
    response_path.parent.mkdir(parents=True)
    response_path.write_text('[{"action":"merge"}]\n', encoding="utf-8")
    receipt_path = tmp_path / "runs" / "relation.relations.json"
    registry: dict[str, object] = {
        "schema_version": 1,
        "cases": {
            source_case_id: {
                "case_id": source_case_id,
                "state": "alias",
                "alias_of": target_case_id,
            },
            target_case_id: {"case_id": target_case_id, "state": "active"},
        },
    }

    references, immutable_receipt_path = _persist_canonical_relation_receipts(
        canonical_records=[
            {
                "case_id": target_case_id,
                "absorbed_case_ids": [source_case_id],
                "case_relation_actions": [
                    {"action": "merge", "target_case_id": source_case_id}
                ],
            }
        ],
        registry=registry,
        review_response_path=response_path,
        receipt_path=receipt_path,
    )

    assert immutable_receipt_path.is_file()
    assert immutable_receipt_path != receipt_path
    assert references[source_case_id]["source_case_id"] == source_case_id
    assert references[source_case_id]["target_case_id"] == target_case_id
    assert registry["cases"][source_case_id]["relation_receipt"] == references[
        source_case_id
    ]
    assert registry["cases"][target_case_id]["incoming_relation_receipts"] == [
        references[source_case_id]
    ]
