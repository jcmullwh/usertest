from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import backlog_repo.outcome_verification as verification_module
from backlog_repo import (
    bind_outcome_verification_amendment,
    canonical_plan_sha256,
    canonical_ticket_body_sha256,
    outcome_suppresses_new_case_discovery,
    render_verification_contract_markdown,
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


def _stage6_plan_markdown(*, command: str = "python -m pytest tests/test_case.py") -> str:
    contract = render_verification_contract_markdown(
        [command],
        outcome_roles={
            "original_scenario": {
                "description": "Replay the observed failure after implementation.",
                "research_experiment_id": "experiment:one",
                "commands": [command],
                "predicates": [
                    {"type": "command_exit_code", "command_index": 0, "equals": 0}
                ],
            },
            "live": None,
            "mitigation_effect": None,
            "recurrence": {
                "description": "Inspect a later source-evidence window.",
                "verification_owner": "centralized_case_refresh",
                "commands": [],
                "predicates": [],
            },
        },
    )
    return (
        "# Generated plan\n\n"
        "- Fingerprint: `0123456789abcdef`\n"
        "- Case ID: `case:raw-plan`\n"
        "- Plan revision ID: `plan:raw-plan:v1`\n\n"
        "### Verification command contract\n\n"
        f"{contract}\n"
    )


def test_raw_repeated_cr_plan_lookup_and_stage6_recovery_reject_real_drift(
    tmp_path: Path,
) -> None:
    owner_root = tmp_path / "owner"
    plan_path = (
        owner_root
        / ".agents"
        / "plans"
        / "5 - complete"
        / "20260716_0123456789abcdef_plan.md"
    )
    plan_path.parent.mkdir(parents=True)
    markdown = _stage6_plan_markdown()
    repeated_cr_markdown = markdown.replace("\n", "\r\r\n")
    plan_path.write_bytes(repeated_cr_markdown.encode("utf-8"))
    parsed = verification_module.parse_verification_contract_markdown(
        repeated_cr_markdown
    )
    assert parsed is not None
    provenance = {
        "fingerprint": "0123456789abcdef",
        "case_id": "case:raw-plan",
        "plan_revision_id": "plan:raw-plan:v1",
        "local_plan_filename": plan_path.name,
        "local_plan_sha256": canonical_plan_sha256(repeated_cr_markdown),
        "ticket_body_sha256": canonical_ticket_body_sha256(repeated_cr_markdown),
        "verification_contract_sha256": parsed["contract_sha256"],
    }
    errors: list[str] = []

    found = verification_module._find_verified_plan(
        provenance,
        owner_roots=[owner_root],
        errors=errors,
    )

    assert found == (owner_root.resolve(), plan_path.resolve())
    assert errors == []
    roles = verification_module._role_contracts_from_verified_plan(
        plan_path,
        provenance=provenance,
        errors=errors,
    )
    assert roles["original_scenario"]["commands"] == [
        "python -m pytest tests/test_case.py"
    ]
    assert errors == []

    altered = _stage6_plan_markdown(command="python -m pytest tests/test_other.py")
    plan_path.write_bytes(altered.replace("\n", "\r\r\n").encode("utf-8"))
    drift_errors: list[str] = []
    assert (
        verification_module._find_verified_plan(
            provenance,
            owner_roots=[owner_root],
            errors=drift_errors,
        )
        is None
    )
    assert any("hash_or_identity_mismatch" in error for error in drift_errors)
    contract_errors: list[str] = []
    assert (
        verification_module._role_contracts_from_verified_plan(
            plan_path,
            provenance=provenance,
            errors=contract_errors,
        )
        == {}
    )
    assert "outcome_plan_verification_contract_hash_mismatch" in contract_errors


def test_review_provenance_allows_head_enrichment_but_binds_reviewed_head() -> None:
    stable = {
        "fingerprint": "0123456789abcdef",
        "case_id": "case:review",
        "plan_revision_id": "plan:review:v1",
        "ticket_body_sha256": "1" * 64,
        "local_plan_sha256": "2" * 64,
        "local_plan_filename": "plan.md",
        "verification_contract_sha256": "3" * 64,
        "target_contract_sha256": "4" * 64,
    }
    durable = {**stable, "verified_implementation_head": "a" * 40}
    review = {
        "ticket_provenance": {**stable, "review_only_note": "retained"},
        "reviewed_head_oid": "a" * 40,
    }
    errors: list[str] = []

    verification_module._verify_review_ticket_binding(
        review,
        label="outcome_review_ref",
        provenance=durable,
        errors=errors,
    )

    assert errors == []
    review["reviewed_head_oid"] = "b" * 40
    verification_module._verify_review_ticket_binding(
        review,
        label="outcome_review_summary",
        provenance=durable,
        errors=errors,
    )
    assert "outcome_review_summary_verified_implementation_head_mismatch" in errors
    review["reviewed_head_oid"] = "a" * 40
    review["ticket_provenance"]["local_plan_sha256"] = "9" * 64
    verification_module._verify_review_ticket_binding(
        review,
        label="outcome_review_ref",
        provenance=durable,
        errors=errors,
    )
    assert "outcome_review_ref_ticket_provenance_mismatch" in errors


def test_schema2_implementation_provenance_validates_receipt_and_artifacts(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "retained-run"
    run_dir.mkdir()
    head = "a" * 40
    repo_revision = "b" * 40
    execution_base = "c" * 40
    artifacts = {
        "verification.json": {"schema_version": 1, "passed": True},
        "target_ref.json": {
            "schema_version": 1,
            "commit_sha": execution_base,
        },
        "git_ref.json": {
            "schema_version": 1,
            "branch": "backlog/example",
            "commit_attempted": False,
            "commit_performed": False,
            "commit_observed": True,
            "base_commit": head,
            "head_commit": head,
        },
        "workspace_ref.json": {
            "schema_version": 1,
            "workspace_dir": str(tmp_path / "historical-workspace"),
            "workspace_strategy": "existing_clean_head",
            "will_cleanup_workspace": False,
        },
    }
    for filename, payload in artifacts.items():
        (run_dir / filename).write_text(
            json.dumps(payload, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    implementation = {
        "schema_version": 2,
        "provenance_mode": "existing_clean_head",
        "repo_revision": repo_revision,
        "execution_base_revision": execution_base,
        "verified_implementation_head": head,
        "verification_sha256": verification_module._sha256_file(
            run_dir / "verification.json"
        ),
        "target_ref_sha256": verification_module._sha256_file(
            run_dir / "target_ref.json"
        ),
        "git_ref_sha256": verification_module._sha256_file(run_dir / "git_ref.json"),
        "workspace_ref_sha256": verification_module._sha256_file(
            run_dir / "workspace_ref.json"
        ),
    }
    implementation["receipt_sha256"] = verification_module._sha256_json(
        implementation
    )
    stable = {
        "fingerprint": "0123456789abcdef",
        "case_id": "case:schema2",
        "plan_revision_id": "plan:schema2:v1",
        "ticket_body_sha256": "1" * 64,
        "local_plan_sha256": "2" * 64,
        "local_plan_filename": "plan.md",
        "verification_contract_sha256": "3" * 64,
        "target_contract_sha256": "4" * 64,
        "verified_implementation_head": head,
    }
    binding = {
        "schema_version": 1,
        "fingerprint": stable["fingerprint"],
        "case_id": stable["case_id"],
        "plan_revision_id": stable["plan_revision_id"],
        "ticket_body_sha256": stable["ticket_body_sha256"],
        "local_plan_sha256": stable["local_plan_sha256"],
        "plan_verification_contract_sha256": stable[
            "verification_contract_sha256"
        ],
        "plan_target_contract_sha256": stable["target_contract_sha256"],
        "configured_commands": ["python -m pytest tests/test_case.py"],
    }
    binding["binding_sha256"] = verification_module._sha256_json(binding)
    ticket_ref = {
        "schema_version": 2,
        "fingerprint": stable["fingerprint"],
        "case_id": stable["case_id"],
        "plan_revision_id": stable["plan_revision_id"],
        "ticket_provenance": {
            **stable,
            "target_contract": {
                "contract_sha256": stable["target_contract_sha256"],
                "repo_revision": repo_revision,
            },
        },
        "implementation_provenance": implementation,
        "verification_binding": binding,
    }
    errors: list[str] = []

    verification_module._verify_ticket_ref(
        ticket_ref,
        provenance=stable,
        record={
            "case_id": stable["case_id"],
            "plan_revision_id": stable["plan_revision_id"],
        },
        implementation_run_dir=run_dir,
        errors=errors,
    )

    assert errors == []
    ticket_ref["implementation_provenance"] = {
        **implementation,
        "receipt_sha256": "0" * 64,
    }
    tampered_errors: list[str] = []
    verification_module._verify_ticket_ref(
        ticket_ref,
        provenance=stable,
        record={
            "case_id": stable["case_id"],
            "plan_revision_id": stable["plan_revision_id"],
        },
        implementation_run_dir=run_dir,
        errors=tampered_errors,
    )
    assert "outcome_ticket_ref_implementation_provenance_hash_mismatch" in (
        tampered_errors
    )
    ticket_ref["implementation_provenance"] = {
        **implementation,
        "schema_version": 3,
    }
    ticket_ref["implementation_provenance"]["receipt_sha256"] = (
        verification_module._sha256_json(
            {
                key: value
                for key, value in ticket_ref["implementation_provenance"].items()
                if key != "receipt_sha256"
            }
        )
    )
    invalid_schema_errors: list[str] = []
    verification_module._verify_ticket_ref(
        ticket_ref,
        provenance={**stable, "target_contract_sha256": None},
        record={
            "case_id": stable["case_id"],
            "plan_revision_id": stable["plan_revision_id"],
        },
        implementation_run_dir=run_dir,
        errors=invalid_schema_errors,
    )
    assert "outcome_ticket_ref_implementation_provenance_schema_invalid" in (
        invalid_schema_errors
    )


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


def test_merge_provenance_accepts_only_descendant_amendment_on_target_branch(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "tests@example.com")
    _git(repo, "config", "user.name", "Tests")
    (repo / "implementation.txt").write_text("implementation\n", encoding="utf-8")
    _git(repo, "add", "implementation.txt")
    _git(repo, "commit", "-m", "implementation")
    implementation_commit = _git(repo, "rev-parse", "HEAD")
    _git(repo, "branch", "dev", implementation_commit)
    (repo / "correction.txt").write_text("correction\n", encoding="utf-8")
    _git(repo, "add", "correction.txt")
    _git(repo, "commit", "-m", "verification correction")
    correction_commit = _git(repo, "rev-parse", "HEAD")
    _git(repo, "update-ref", "refs/remotes/origin/dev", correction_commit)
    record = bind_outcome_verification_amendment(
        {
            "schema_version": 1,
            "case_id": "case:amendment",
            "plan_revision_id": "plan:amendment:v1",
            "state": "unverified",
            "recorded_at": "2026-07-15T00:00:00Z",
            "requires_live_verification": False,
            "target_branch": "dev",
            "merged_commit": implementation_commit,
            "pr_url": "https://example.invalid/pull/213",
            "test_evidence": [],
            "original_scenario_evidence": [],
            "live_evidence": [],
            "mitigation_evidence": [],
            "remaining_risks": [],
            "recurrence_check": {"status": "not_run"},
        },
        verification_commit=correction_commit,
        verification_pr_url="https://example.invalid/pull/215",
        recorded_at="2026-07-15T12:00:00Z",
    )
    errors: list[str] = []
    verification_module._verify_merge_provenance(
        record,
        owner_root=repo,
        errors=errors,
    )
    assert errors == []

    implementation_tree = _git(
        repo,
        "rev-parse",
        implementation_commit + "^{tree}",
    )
    unrelated_commit = _git(
        repo,
        "commit-tree",
        implementation_tree,
        "-m",
        "unrelated root",
    )
    invalid = dict(record)
    amendment = dict(record["verification_amendment"])
    amendment["verification_commit"] = unrelated_commit
    invalid["verification_amendment"] = amendment
    errors = []
    verification_module._verify_merge_provenance(
        invalid,
        owner_root=repo,
        errors=errors,
    )
    assert any("not_descendant" in error for error in errors)
    assert any("not_on_target_branch" in error for error in errors)


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
