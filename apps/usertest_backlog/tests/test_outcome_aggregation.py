from __future__ import annotations

import hashlib
import json
import subprocess
from copy import deepcopy
from pathlib import Path
from typing import Any

from backlog_repo import (
    canonical_plan_sha256,
    canonical_ticket_body_sha256,
    parse_verification_contract_markdown,
    render_verification_contract_markdown,
    write_case_relation_receipt,
)

from usertest_backlog.workflows.derived_evidence import inferred_implementation_runs_root
from usertest_backlog.workflows.problem_mining import (
    _persist_canonical_relation_receipts,
)
from usertest_backlog.workflows.shadow_validation import _terminal_outcome_errors
from usertest_backlog.workflows.staged import (
    _outcome_trusted_runs_roots,
    _sync_case_registry_outcomes,
)


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


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _sha256_json(value: object) -> str:
    rendered = json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(rendered.encode("utf-8")).hexdigest()


def _retained_implemented_outcome(
    tmp_path: Path,
    *,
    implementation_runs_root: Path,
) -> tuple[Path, dict[str, object]]:
    owner_root = tmp_path / "owner"
    owner_root.mkdir()
    _git(owner_root, "init", "-b", "dev")
    _git(owner_root, "config", "user.email", "tests@example.com")
    _git(owner_root, "config", "user.name", "Tests")
    (owner_root / "implemented.txt").write_text("implemented\n", encoding="utf-8")
    _git(owner_root, "add", "implemented.txt")
    _git(owner_root, "commit", "-m", "implemented")
    merged_commit = _git(owner_root, "rev-parse", "HEAD")

    case_id = "case:aggregate"
    plan_revision_id = "plan:aggregate:implemented:v1"
    fingerprint = "0123456789abcdef"
    verification_markdown = render_verification_contract_markdown(["pytest -q"])
    plan_markdown = (
        "# Retained implementation plan\n\n"
        f"- Fingerprint: `{fingerprint}`\n"
        f"- Case ID: `{case_id}`\n"
        f"- Plan revision ID: `{plan_revision_id}`\n\n"
        "### Verification command contract\n\n"
        f"{verification_markdown}\n"
    )
    plan_path = (
        owner_root
        / ".agents"
        / "plans"
        / "5 - complete"
        / f"20260711_{fingerprint}_retained-plan.md"
    )
    plan_path.parent.mkdir(parents=True)
    plan_path.write_text(plan_markdown, encoding="utf-8")
    verification_contract = parse_verification_contract_markdown(plan_markdown)
    assert verification_contract is not None
    ticket_provenance = {
        "schema_version": 1,
        "fingerprint": fingerprint,
        "case_id": case_id,
        "plan_revision_id": plan_revision_id,
        "ticket_body_sha256": canonical_ticket_body_sha256(plan_markdown),
        "local_plan_sha256": canonical_plan_sha256(plan_markdown),
        "local_plan_filename": plan_path.name,
        "verification_contract_sha256": verification_contract["contract_sha256"],
        "target_contract_sha256": None,
        "verified_implementation_head": merged_commit,
    }

    binding = {
        "schema_version": 1,
        "fingerprint": fingerprint,
        "case_id": case_id,
        "plan_revision_id": plan_revision_id,
        "ticket_body_sha256": ticket_provenance["ticket_body_sha256"],
        "local_plan_sha256": ticket_provenance["local_plan_sha256"],
        "plan_verification_contract_sha256": verification_contract["contract_sha256"],
        "plan_target_contract_sha256": None,
        "configured_commands": ["pytest -q"],
    }
    binding["binding_sha256"] = _sha256_json(binding)
    implementation_run_dir = (
        implementation_runs_root / "target_a" / "20260711T120000Z" / "codex" / "0"
    )
    ticket_ref_path = implementation_run_dir / "ticket_ref.json"
    _write_json(
        ticket_ref_path,
        {
            "schema_version": 2,
            "fingerprint": fingerprint,
            "case_id": case_id,
            "plan_revision_id": plan_revision_id,
            "ticket_provenance": ticket_provenance,
            "verification_binding": binding,
        },
    )

    review_run_dir = (
        implementation_runs_root / "target_a" / "20260711T130000Z" / "codex_review" / "0"
    )
    implementation_ticket_ref_sha256 = _sha256_file(ticket_ref_path)
    _write_json(
        review_run_dir / "review_ref.json",
        {
            "schema_version": 2,
            "implementation_run_dir": str(implementation_run_dir),
            "implementation_ticket_ref_sha256": implementation_ticket_ref_sha256,
            "reviewed_head_oid": merged_commit,
            "ticket_provenance": ticket_provenance,
        },
    )
    _write_json(
        review_run_dir / "review_summary.json",
        {
            "implementation_ticket_ref_sha256": implementation_ticket_ref_sha256,
            "reviewed_head_oid": merged_commit,
            "ticket_provenance": ticket_provenance,
        },
    )
    _write_json(
        review_run_dir / "merge_ref.json",
        {
            "merged": True,
            "target_branch": "dev",
            "merged_commit": merged_commit,
        },
    )
    outcome = {
        "schema_version": 1,
        "case_id": case_id,
        "plan_revision_id": plan_revision_id,
        "state": "implemented",
        "outcome_scope": "case",
        "recorded_at": "2026-07-11T13:00:00Z",
        "requires_live_verification": False,
        "target_branch": "dev",
        "merged_commit": merged_commit,
        "review_run_dir": str(review_run_dir),
        "ticket_provenance": ticket_provenance,
        "test_evidence": [],
        "original_scenario_evidence": [],
        "live_evidence": [],
        "mitigation_evidence": [],
        "remaining_risks": ["Original scenario verification remains pending."],
        "recurrence_check": {"status": "not_run"},
    }
    return owner_root, outcome


def test_sibling_implementation_outcome_uses_same_trust_boundary_for_sync_and_shadow(
    tmp_path: Path,
) -> None:
    primary_runs_root = tmp_path / "runs" / "usertest"
    implementation_runs_root = inferred_implementation_runs_root(primary_runs_root)
    trusted_roots = _outcome_trusted_runs_roots(
        primary_runs_dir=primary_runs_root,
        configured_runs_dir=primary_runs_root,
        implementation_runs_root=implementation_runs_root,
    )
    assert set(trusted_roots) == {
        primary_runs_root.resolve(),
        implementation_runs_root.resolve(),
    }
    owner_root, outcome = _retained_implemented_outcome(
        tmp_path,
        implementation_runs_root=implementation_runs_root,
    )
    plan_revision_id = str(outcome["plan_revision_id"])
    atom_actions = {
        "atom:implementation": {
            "case_id": "case:aggregate",
            "plan_outcomes": {
                plan_revision_id: {
                    "state": "implemented",
                    "recorded_at": outcome["recorded_at"],
                    "required": True,
                    "outcome_record": outcome,
                }
            },
        }
    }

    trusted_registry = _registry()
    trusted_summary = _sync_case_registry_outcomes(
        case_registry=trusted_registry,
        atom_actions=atom_actions,
        trusted_runs_roots=trusted_roots,
        owner_roots=(owner_root,),
    )

    assert trusted_summary["provenance_failed_outcome_records"] == 0
    assert trusted_registry["cases"]["case:aggregate"]["state"] == "implemented"
    assert (
        trusted_registry["cases"]["case:aggregate"]["plan_outcomes"][plan_revision_id][
            "outcome_verification"
        ]["verified"]
        is True
    )
    assert (
        _terminal_outcome_errors(
            trusted_registry,
            trusted_runs_roots=trusted_roots,
            owner_roots=(owner_root,),
        )
        == []
    )

    primary_only_roots = (primary_runs_root.resolve(),)
    untrusted_registry = _registry()
    untrusted_summary = _sync_case_registry_outcomes(
        case_registry=untrusted_registry,
        atom_actions=atom_actions,
        trusted_runs_roots=primary_only_roots,
        owner_roots=(owner_root,),
    )
    assert untrusted_summary["provenance_failed_outcome_records"] == 1
    assert untrusted_registry["cases"]["case:aggregate"]["state"] == "unverified"
    shadow_errors = _terminal_outcome_errors(
        trusted_registry,
        trusted_runs_roots=primary_only_roots,
        owner_roots=(owner_root,),
    )
    assert any(
        error.startswith(
            f"outcome_provenance_revalidation_failed:case:aggregate:{plan_revision_id}:"
        )
        and "outcome_review_run_dir_outside_trusted_roots" in error
        for error in shadow_errors
    )


def test_verified_outcome_materializes_missing_case_once(
    monkeypatch: Any,
) -> None:
    case_id = "case:maintenance"
    problem_id = "problem:maintenance"
    revision_id = "plan:maintenance:v1"
    fingerprint = "0123456789abcdef"
    atom_ids = [
        "usertest_implement/usertest/20260704T161642Z/codex/0:maintenance_image_cleanup:1",
        "usertest_implement/usertest/20260707T135227Z/codex/0:maintenance_image_cleanup:1",
        "operator_recovery/docker_cleanup_20260713:maintenance_image_cleanup:1",
    ]
    outcome = {
        "case_id": case_id,
        "plan_revision_id": revision_id,
        "state": "mitigated",
        "outcome_scope": "case",
        "recorded_at": "2026-07-16T00:00:00Z",
    }
    identity = {
        "schema_version": 1,
        "source": "verified_plan_target_contract",
        "case_id": case_id,
        "problem_id": problem_id,
        "plan_revision_id": revision_id,
        "fingerprint": fingerprint,
        "target_contract_sha256": "8" * 64,
        "verified_plan_path": "I:/repo/.agents/plans/5 - complete/plan.md",
    }
    monkeypatch.setattr(
        "usertest_backlog.workflows.staged.verify_outcome_record_provenance",
        lambda record, **_: {
            "structural_status": "valid",
            "provenance_status": "verified",
            "verified": True,
            "errors": [],
            "outcome_record": record,
            "verified_case_identity": identity,
            "verified_case_identity_errors": [],
        },
    )
    atom_actions = {
        atom_id: {
            "atom_id": atom_id,
            "case_id": case_id,
            "plan_outcomes": {
                revision_id: {
                    "state": "mitigated",
                    "recorded_at": outcome["recorded_at"],
                    "fingerprint": fingerprint,
                    "required": True,
                    "outcome_record": outcome,
                }
            },
        }
        for atom_id in atom_ids
    }
    registry = _registry()
    existing_case = deepcopy(registry["cases"]["case:aggregate"])

    summary = _sync_case_registry_outcomes(
        case_registry=registry,
        atom_actions=atom_actions,
    )

    assert summary["missing_case_outcomes"] == 1
    assert summary["materialized_cases"] == 1
    assert summary["unmaterializable_case_outcomes"] == 0
    assert summary["conflicting_case_identities"] == 0
    assert registry["cases"]["case:aggregate"] == existing_case
    materialized = registry["cases"][case_id]
    assert materialized["canonical_problem_id"] == problem_id
    assert materialized["evidence_atom_ids"] == sorted(atom_ids)
    assert materialized["state"] == "mitigated"
    assert materialized["context_status"] == "identity_only"
    assert materialized["identity_materialization"]["selected_plan_revision_id"] == revision_id
    assert materialized["current_lifecycle"]["outcome_reference"]["validation_status"] == (
        "verified"
    )
    assert registry["problem_id_to_case_id"][problem_id] == case_id
    assert registry["ticket_fingerprint_to_case_id"][fingerprint] == case_id
    for atom_id in atom_ids:
        assert registry["atom_id_to_case_id"][atom_id] == case_id
        assert registry["atom_id_to_case_ids"][atom_id] == [case_id]

    after_first_sync = deepcopy(registry)
    second_summary = _sync_case_registry_outcomes(
        case_registry=registry,
        atom_actions=atom_actions,
    )
    assert second_summary["missing_case_outcomes"] == 0
    assert second_summary["materialized_cases"] == 0
    assert second_summary["cases_updated"] == 0
    assert registry == after_first_sync


def test_missing_case_requires_externally_verified_bound_identity(
    monkeypatch: Any,
) -> None:
    case_id = "case:missing"
    revision_id = "plan:missing:v1"
    outcome = {
        "case_id": case_id,
        "plan_revision_id": revision_id,
        "state": "planned",
        "outcome_scope": "case",
        "recorded_at": "2026-07-16T00:00:00Z",
    }
    monkeypatch.setattr(
        "usertest_backlog.workflows.staged.verify_outcome_record_provenance",
        lambda record, **_: {
            "structural_status": "valid",
            "provenance_status": "not_required_nonterminal",
            "verified": True,
            "errors": [],
            "outcome_record": record,
            "verified_case_identity": {
                "case_id": case_id,
                "problem_id": "problem:prose-only",
                "plan_revision_id": revision_id,
            },
        },
    )
    registry = _registry()
    summary = _sync_case_registry_outcomes(
        case_registry=registry,
        atom_actions={
            "source/run:signal:1": {
                "case_id": case_id,
                "plan_outcomes": {
                    revision_id: {
                        "state": "planned",
                        "recorded_at": outcome["recorded_at"],
                        "required": True,
                        "outcome_record": outcome,
                    }
                },
            }
        },
    )

    assert case_id not in registry["cases"]
    assert summary["missing_case_outcomes"] == 1
    assert summary["materialized_cases"] == 0
    assert summary["unmaterializable_case_outcomes"] == 1


def test_conflicting_verified_identities_do_not_materialize_case(
    monkeypatch: Any,
) -> None:
    case_id = "case:conflict"
    revision_id = "plan:conflict:v1"

    def verify(record: dict[str, Any], **_: Any) -> dict[str, Any]:
        return {
            "structural_status": "valid",
            "provenance_status": "verified",
            "verified": True,
            "errors": [],
            "outcome_record": record,
            "verified_case_identity": {
                "case_id": case_id,
                "problem_id": record["identity_problem_id"],
                "plan_revision_id": revision_id,
                "fingerprint": "0123456789abcdef",
            },
        }

    monkeypatch.setattr(
        "usertest_backlog.workflows.staged.verify_outcome_record_provenance",
        verify,
    )
    atom_actions = {}
    for index, problem_id in enumerate(("problem:one", "problem:two"), start=1):
        outcome = {
            "case_id": case_id,
            "plan_revision_id": revision_id,
            "state": "mitigated",
            "outcome_scope": "case",
            "recorded_at": "2026-07-16T00:00:00Z",
            "identity_problem_id": problem_id,
        }
        atom_actions[f"source/run:signal:{index}"] = {
            "case_id": case_id,
            "plan_outcomes": {
                revision_id: {
                    "state": "mitigated",
                    "recorded_at": outcome["recorded_at"],
                    "required": True,
                    "outcome_record": outcome,
                }
            },
        }
    registry = _registry()

    summary = _sync_case_registry_outcomes(
        case_registry=registry,
        atom_actions=atom_actions,
    )

    assert case_id not in registry["cases"]
    assert summary["conflicting_case_identities"] == 1
    assert summary["unmaterializable_case_outcomes"] == 1


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
                "case_relation_actions": [{"action": "merge", "target_case_id": source_case_id}],
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
    assert registry["cases"][source_case_id]["relation_receipt"] == references[source_case_id]
    assert registry["cases"][target_case_id]["incoming_relation_receipts"] == [
        references[source_case_id]
    ]
