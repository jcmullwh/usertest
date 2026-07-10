from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import backlog_repo.outcome_verification as verification_module
from backlog_repo import (
    outcome_suppresses_new_case_discovery,
    verify_outcome_record_provenance,
    write_case_relation_receipt,
)


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return proc.stdout.strip()


def _forged_receipt(
    *,
    tmp_path: Path,
    evidence_kind: str,
) -> dict[str, object]:
    run_dir = tmp_path / "trusted-runs" / evidence_kind
    common = {
        "producer": "usertest_implement",
        "verification_producer": "runner_core",
        "evidence_kind": evidence_kind,
        "fingerprint": "0123456789abcdef",
        "case_id": "case:forged",
        "plan_revision_id": "plan:forged:v1",
        "ticket_body_sha256": "0" * 64,
        "local_plan_sha256": "0" * 64,
        "local_plan_filename": "forged.md",
        "verification_contract_sha256": "0" * 64,
        "target_contract_sha256": "1" * 64,
        "verified_implementation_head": "0" * 40,
    }
    if evidence_kind == "test":
        return {
            **common,
            "receipt_schema_version": 2,
            "run_dir": str(run_dir),
            "verification_path": str(run_dir / "verification.json"),
            "verification_sha256": "0" * 64,
            "ticket_ref_path": str(run_dir / "ticket_ref.json"),
            "ticket_ref_sha256": "0" * 64,
            "verification_binding_sha256": "0" * 64,
            "commands": ["pytest tests/test_forged.py -q"],
        }
    return {
        **common,
        "receipt_schema_version": 3,
        "role_artifact_path": str(run_dir / "outcome_role.json"),
        "role_artifact_sha256": "0" * 64,
        "role_contract_sha256": "2" * 64,
        "merged_commit": "0" * 40,
    }


def _forged_resolved_outcome(tmp_path: Path) -> dict[str, object]:
    def evidence(kind: str) -> dict[str, object]:
        return {
            "kind": kind,
            "reference": f"runs/{kind}/verification.json",
            "result": "passed",
            "runner_receipt": _forged_receipt(
                tmp_path=tmp_path,
                evidence_kind=kind,
            ),
        }

    return {
        "schema_version": 1,
        "case_id": "case:forged",
        "plan_revision_id": "plan:forged:v1",
        "state": "resolved",
        "recorded_at": "2026-07-10T00:00:00Z",
        "requires_live_verification": False,
        "target_branch": "dev",
        "merged_commit": "0" * 40,
        "pr_url": "https://example.invalid/pr/1",
        "review_run_dir": str(tmp_path / "trusted-runs" / "review"),
        "ticket_provenance": {
            "schema_version": 1,
            "fingerprint": "0123456789abcdef",
            "case_id": "case:forged",
            "plan_revision_id": "plan:forged:v1",
            "ticket_body_sha256": "0" * 64,
            "local_plan_sha256": "0" * 64,
            "local_plan_filename": "forged.md",
            "verification_contract_sha256": "0" * 64,
            "target_contract_sha256": "1" * 64,
            "verified_implementation_head": "0" * 40,
        },
        "test_evidence": [evidence("test")],
        "original_scenario_evidence": [evidence("original_scenario")],
        "live_evidence": [],
        "mitigation_evidence": [],
        "remaining_risks": [],
        "recurrence_check": {
            "status": "completed",
            "result": "passed",
            "evidence": [evidence("recurrence")],
        },
    }


def test_structurally_valid_forged_resolved_outcome_is_not_provenance_verified(
    tmp_path: Path,
) -> None:
    record = _forged_resolved_outcome(tmp_path)

    assert outcome_suppresses_new_case_discovery(record) is True

    verification = verify_outcome_record_provenance(
        record,
        trusted_runs_roots=[tmp_path / "trusted-runs"],
        owner_roots=[tmp_path / "owner"],
    )

    assert verification["structural_status"] == "valid"
    assert verification["provenance_status"] == "failed"
    assert verification["verified"] is False
    assert "outcome_local_plan_missing:forged.md" in verification["errors"]
    assert any("artifact_missing" in error for error in verification["errors"])


def test_planned_outcome_does_not_require_external_provenance() -> None:
    record = {
        "schema_version": 1,
        "case_id": "case:planned",
        "plan_revision_id": "plan:planned:v1",
        "state": "planned",
        "recorded_at": "2026-07-10T00:00:00Z",
        "requires_live_verification": False,
        "test_evidence": [],
        "original_scenario_evidence": [],
        "live_evidence": [],
        "mitigation_evidence": [],
        "remaining_risks": [],
        "recurrence_check": {"status": "not_run"},
    }

    verification = verify_outcome_record_provenance(
        record,
        trusted_runs_roots=[],
        owner_roots=[],
    )

    assert verification["verified"] is True
    assert verification["provenance_status"] == "not_required_nonterminal"


@pytest.mark.parametrize(("rejected", "expected_error"), [(False, False), (True, True)])
def test_test_receipt_treats_rejected_sentinel_as_boolean_flag(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    rejected: bool,
    expected_error: bool,
) -> None:
    run_dir = tmp_path / "trusted" / "implementation"
    verification_path = run_dir / "verification.json"
    ticket_ref_path = run_dir / "ticket_ref.json"
    run_dir.mkdir(parents=True)
    verification_path.write_text("{}\n", encoding="utf-8")
    ticket_ref_path.write_text("{}\n", encoding="utf-8")
    command = "pytest tests/test_behavior.py -q"
    verification = {
        "schema_version": 1,
        "passed": True,
        "status": "passed",
        "terminal_reason": "passed",
        "timed_out": False,
        "cancelled": False,
        "commands_configured": [command],
        "commands": [
            {
                "command": command,
                "exit_code": 0,
                "timed_out": False,
                "cancelled": False,
                "dispatch_blocked": False,
                "rejected_sentinel": rejected,
            }
        ],
    }
    binding = {
        "configured_commands": [command],
        "binding_sha256": "f" * 64,
    }
    monkeypatch.setattr(
        verification_module,
        "_read_json",
        lambda path, **_: verification if path == verification_path else {},
    )
    monkeypatch.setattr(
        verification_module,
        "_verify_ticket_ref",
        lambda *_args, **_kwargs: binding,
    )
    monkeypatch.setattr(verification_module, "_sha256_file", lambda _path: "a" * 64)
    provenance = {
        "fingerprint": "0123456789abcdef",
        "ticket_body_sha256": "b" * 64,
        "local_plan_sha256": "c" * 64,
        "local_plan_filename": "plan.md",
        "verification_contract_sha256": "d" * 64,
        "target_contract_sha256": "e" * 64,
    }
    receipt = {
        **provenance,
        "evidence_kind": "test",
        "case_id": "case:test",
        "plan_revision_id": "plan:test:v1",
        "run_dir": str(run_dir),
        "verification_path": str(verification_path),
        "verification_sha256": "a" * 64,
        "ticket_ref_path": str(ticket_ref_path),
        "ticket_ref_sha256": "a" * 64,
        "verification_binding_sha256": "f" * 64,
        "commands": [command],
    }
    errors: list[str] = []

    verification_module._verify_receipt(
        receipt,
        evidence_kind="test",
        record={"case_id": "case:test", "plan_revision_id": "plan:test:v1"},
        provenance=provenance,
        trusted_runs_roots=[tmp_path / "trusted"],
        expected_implementation_run_dir=run_dir,
        errors=errors,
    )

    assert (
        "outcome_test_receipt_command_0_not_passed" in errors
    ) is expected_error


def test_merge_provenance_requires_verified_implementation_in_merged_commit(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "dev")
    _git(repo, "config", "user.email", "tests@example.com")
    _git(repo, "config", "user.name", "Tests")
    (repo / "base.txt").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "base.txt")
    _git(repo, "commit", "-m", "base")
    base = _git(repo, "rev-parse", "HEAD")
    (repo / "target.txt").write_text("target\n", encoding="utf-8")
    _git(repo, "add", "target.txt")
    _git(repo, "commit", "-m", "target")
    unrelated_target = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-b", "feature", base)
    (repo / "fix.txt").write_text("causal fix\n", encoding="utf-8")
    _git(repo, "add", "fix.txt")
    _git(repo, "commit", "-m", "verified implementation")
    verified_head = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "dev")
    record = {
        "merged_commit": unrelated_target,
        "target_branch": "dev",
        "ticket_provenance": {"verified_implementation_head": verified_head},
    }
    errors: list[str] = []

    verification_module._verify_merge_provenance(
        record,
        owner_root=repo,
        errors=errors,
    )

    assert any(
        error.startswith("outcome_verified_implementation_head_not_in_merged_commit:")
        for error in errors
    )

    _git(repo, "merge", "--no-ff", "feature", "-m", "merge verified implementation")
    record["merged_commit"] = _git(repo, "rev-parse", "HEAD")
    errors = []
    verification_module._verify_merge_provenance(
        record,
        owner_root=repo,
        errors=errors,
    )
    assert not any("verified_implementation_head" in error for error in errors)


def _relationship_outcome(
    *,
    source_case_id: str,
    target_case_id: str,
    state: str = "duplicate",
    relation_receipt: dict[str, object] | None = None,
) -> dict[str, object]:
    outcome: dict[str, object] = {
        "schema_version": 1,
        "case_id": source_case_id,
        "plan_revision_id": f"plan:{source_case_id}:v1",
        "state": state,
        "outcome_scope": "case",
        "recorded_at": "2026-07-10T00:00:00Z",
        "requires_live_verification": False,
        "target_branch": None,
        "merged_commit": None,
        "test_evidence": [],
        "original_scenario_evidence": [],
        "live_evidence": [],
        "mitigation_evidence": [],
        "remaining_risks": [],
        "recurrence_check": {"status": "not_run"},
        "related_case_id": target_case_id,
    }
    if relation_receipt is not None:
        outcome["relation_receipt"] = relation_receipt
    return outcome


def _relation_receipt(
    tmp_path: Path,
    *,
    source_case_id: str,
    target_case_id: str,
    relation_kind: str = "canonical_absorption",
) -> dict[str, object]:
    runs_root = tmp_path / "trusted-runs"
    response_path = runs_root / "problem_mining_relation_review_001.response.txt"
    response_path.parent.mkdir(parents=True)
    response_path.write_text("[]\n", encoding="utf-8")
    _payload, references = write_case_relation_receipt(
        runs_root / "problem_mining_relation_review_001.relations.json",
        stage="problem_mining",
        relation_review_response_path=response_path,
        relations=[
            {
                "source_case_id": source_case_id,
                "target_case_id": target_case_id,
                "direction": "source_to_canonical",
                "relation_kind": relation_kind,
                "decision_actions": (
                    ["merge"] if relation_kind == "canonical_absorption" else []
                ),
            }
        ],
    )
    return references[source_case_id]


def test_duplicate_relation_requires_exact_runner_receipt_and_registry_direction(
    tmp_path: Path,
) -> None:
    source_case_id = "case:source"
    target_case_id = "case:canonical"
    receipt = _relation_receipt(
        tmp_path,
        source_case_id=source_case_id,
        target_case_id=target_case_id,
    )
    registry = {
        "schema_version": 1,
        "cases": {
            source_case_id: {
                "case_id": source_case_id,
                "state": "alias",
                "alias_of": target_case_id,
                "relation_receipt": receipt,
            },
            target_case_id: {"case_id": target_case_id, "state": "active"},
        },
    }

    verification = verify_outcome_record_provenance(
        _relationship_outcome(
            source_case_id=source_case_id,
            target_case_id=target_case_id,
            relation_receipt=receipt,
        ),
        trusted_runs_roots=[tmp_path / "trusted-runs"],
        owner_roots=[],
        case_registry=registry,
    )

    assert verification["verified"] is True
    assert verification["provenance_status"] == "verified"


def test_superseded_relation_requires_directed_registry_supersession(
    tmp_path: Path,
) -> None:
    source_case_id = "case:old"
    target_case_id = "case:replacement"
    receipt = _relation_receipt(
        tmp_path,
        source_case_id=source_case_id,
        target_case_id=target_case_id,
        relation_kind="supersession",
    )
    registry = {
        "schema_version": 1,
        "cases": {
            source_case_id: {
                "case_id": source_case_id,
                "state": "superseded",
                "superseded_by": target_case_id,
                "relation_receipt": receipt,
            },
            target_case_id: {"case_id": target_case_id, "state": "active"},
        },
    }

    verification = verify_outcome_record_provenance(
        _relationship_outcome(
            source_case_id=source_case_id,
            target_case_id=target_case_id,
            state="superseded",
            relation_receipt=receipt,
        ),
        trusted_runs_roots=[tmp_path / "trusted-runs"],
        owner_roots=[],
        case_registry=registry,
    )

    assert verification["verified"] is True


def test_duplicate_relation_to_arbitrary_active_case_fails_closed(
    tmp_path: Path,
) -> None:
    source_case_id = "case:source"
    target_case_id = "case:arbitrary-active"
    receipt = _relation_receipt(
        tmp_path,
        source_case_id=source_case_id,
        target_case_id=target_case_id,
    )
    registry = {
        "schema_version": 1,
        "cases": {
            source_case_id: {"case_id": source_case_id, "state": "active"},
            target_case_id: {"case_id": target_case_id, "state": "active"},
        },
    }

    verification = verify_outcome_record_provenance(
        _relationship_outcome(
            source_case_id=source_case_id,
            target_case_id=target_case_id,
            relation_receipt=receipt,
        ),
        trusted_runs_roots=[tmp_path / "trusted-runs"],
        owner_roots=[],
        case_registry=registry,
    )

    assert verification["verified"] is False
    assert any(
        error.startswith("outcome_relationship_registry_direction_mismatch")
        for error in verification["errors"]
    )
    assert f"outcome_relationship_registry_receipt_mismatch:{source_case_id}" in verification[
        "errors"
    ]


def test_relationship_outcome_cannot_close_case_by_pointing_to_itself(
    tmp_path: Path,
) -> None:
    case_id = "case:self"
    verification = verify_outcome_record_provenance(
        _relationship_outcome(
            source_case_id=case_id,
            target_case_id=case_id,
        ),
        trusted_runs_roots=[tmp_path / "trusted-runs"],
        owner_roots=[],
        case_registry={
            "schema_version": 1,
            "cases": {case_id: {"case_id": case_id, "state": "active"}},
        },
    )

    assert verification["verified"] is False
    assert f"outcome_relationship_self_relation:{case_id}" in verification["errors"]
    assert "outcome_relationship_receipt_missing" in verification["errors"]


def test_runner_relation_receipt_rejects_cycles(tmp_path: Path) -> None:
    response_path = tmp_path / "trusted-runs" / "relation.response.txt"
    response_path.parent.mkdir(parents=True)
    response_path.write_text("[]\n", encoding="utf-8")

    with pytest.raises(ValueError, match="case_relation_receipt_cycle"):
        write_case_relation_receipt(
            tmp_path / "trusted-runs" / "relation.relations.json",
            stage="problem_mining",
            relation_review_response_path=response_path,
            relations=[
                {
                    "source_case_id": "case:a",
                    "target_case_id": "case:b",
                    "direction": "source_to_canonical",
                    "relation_kind": "canonical_absorption",
                    "decision_actions": ["merge"],
                },
                {
                    "source_case_id": "case:b",
                    "target_case_id": "case:a",
                    "direction": "source_to_canonical",
                    "relation_kind": "canonical_absorption",
                    "decision_actions": ["merge"],
                },
            ],
        )


def test_relation_receipt_retains_immutable_evidence_across_rolling_cycles(
    tmp_path: Path,
) -> None:
    runs_root = tmp_path / "trusted-runs"
    rolling_response = runs_root / "relation.response.txt"
    rolling_response.parent.mkdir(parents=True)
    receipt_base = runs_root / "relation.relations.json"
    relation = {
        "source_case_id": "case:old",
        "target_case_id": "case:canonical",
        "direction": "source_to_canonical",
        "relation_kind": "canonical_absorption",
        "decision_actions": ["merge"],
    }

    rolling_response.write_text('[{"cycle":1}]\n', encoding="utf-8")
    _first_payload, first_references = write_case_relation_receipt(
        receipt_base,
        stage="problem_mining",
        relation_review_response_path=rolling_response,
        relations=[relation],
    )
    first_ref = first_references["case:old"]
    first_receipt_path = Path(str(first_ref["receipt_path"]))

    rolling_response.write_text('[{"cycle":2}]\n', encoding="utf-8")
    _second_payload, second_references = write_case_relation_receipt(
        receipt_base,
        stage="problem_mining",
        relation_review_response_path=rolling_response,
        relations=[relation],
    )
    second_ref = second_references["case:old"]

    assert first_receipt_path.is_file()
    assert Path(str(second_ref["receipt_path"])).is_file()
    assert first_ref["receipt_path"] != second_ref["receipt_path"]
    verification = verify_outcome_record_provenance(
        _relationship_outcome(
            source_case_id="case:old",
            target_case_id="case:canonical",
            relation_receipt=first_ref,
        ),
        trusted_runs_roots=[runs_root],
        owner_roots=[],
        case_registry={
            "schema_version": 1,
            "cases": {
                "case:old": {
                    "case_id": "case:old",
                    "state": "alias",
                    "alias_of": "case:canonical",
                    "relation_receipt": first_ref,
                },
                "case:canonical": {
                    "case_id": "case:canonical",
                    "state": "active",
                },
            },
        },
    )
    assert verification["verified"] is True
