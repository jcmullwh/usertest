from __future__ import annotations

import pytest

from backlog_repo.outcomes import (
    bind_outcome_verification_amendment,
    effective_outcome_verification_commit,
    extract_outcome_markdown,
    outcome_suppresses_new_case_discovery,
    reconcile_outcome_records,
    reconcile_terminal_outcome_stale_blockers,
    transition_outcome_record,
    upsert_outcome_markdown,
    validate_outcome_record,
)


def _record(**updates: object) -> dict[str, object]:
    record: dict[str, object] = {
        "schema_version": 1,
        "case_id": "case:lifecycle",
        "plan_revision_id": "plan:lifecycle:v1",
        "state": "planned",
        "recorded_at": "2026-07-09T00:00:00Z",
        "requires_live_verification": True,
        "target_branch": None,
        "merged_commit": None,
        "test_evidence": [],
        "original_scenario_evidence": [],
        "live_evidence": [],
        "mitigation_evidence": [],
        "remaining_risks": [],
        "recurrence_check": {"status": "not_run"},
    }
    record.update(updates)
    return record


def _passed(kind: str, reference: str) -> dict[str, object]:
    common = {
        "producer": "usertest_implement",
        "verification_producer": "runner_core",
        "evidence_kind": kind,
        "ticket_body_sha256": "c" * 64,
        "local_plan_sha256": "d" * 64,
        "local_plan_filename": "ticket.md",
        "verification_contract_sha256": "e" * 64,
        "target_contract_sha256": "1" * 64,
        "verified_implementation_head": "2" * 40,
        "fingerprint": "0123456789abcdef",
        "case_id": "case:lifecycle",
        "plan_revision_id": "plan:lifecycle:v1",
    }
    if kind == "test":
        receipt = {
            **common,
            "receipt_schema_version": 2,
            "run_dir": f"runs/{kind}",
            "verification_path": f"runs/{kind}/verification.json",
            "verification_sha256": "a" * 64,
            "ticket_ref_path": f"runs/{kind}/ticket_ref.json",
            "ticket_ref_sha256": "b" * 64,
            "verification_binding_sha256": "f" * 64,
            "commands": [f"verify-{kind}"],
        }
    else:
        receipt = {
            **common,
            "receipt_schema_version": 3,
            "role_artifact_path": f"runs/{kind}/outcome_role.json",
            "role_artifact_sha256": "a" * 64,
            "role_contract_sha256": "f" * 64,
            "merged_commit": "abc123",
            "verified_implementation_head": "2" * 40,
        }
    return {
        "kind": kind,
        "reference": reference,
        "result": "passed",
        "runner_receipt": receipt,
    }


def _completed_recurrence() -> dict[str, object]:
    return {
        "status": "completed",
        "result": "passed",
        "evidence": [_passed("recurrence", "runs/recurrence")],
    }


def test_tests_verified_does_not_claim_case_is_resolved() -> None:
    record = _record(
        state="tests_verified",
        target_branch="dev",
        merged_commit="abc123",
        test_evidence=[_passed("test", "tests/test_x.py")],
    )

    assert validate_outcome_record(record)["state"] == "tests_verified"
    assert outcome_suppresses_new_case_discovery(record) is False


def test_tests_verified_rejects_failed_only_evidence() -> None:
    record = _record(
        state="tests_verified",
        target_branch="dev",
        merged_commit="abc123",
        test_evidence=[{"kind": "pytest", "reference": "tests/test_x.py", "result": "failed"}],
    )

    with pytest.raises(ValueError, match="passing_test_evidence"):
        validate_outcome_record(record)


def test_runtime_resolution_requires_original_and_live_proof() -> None:
    record = _record(
        state="resolved",
        target_branch="dev",
        merged_commit="abc123",
        test_evidence=[_passed("test", "tests/test_x.py")],
        original_scenario_evidence=[_passed("original_scenario", "runs/replay/report.json")],
    )

    with pytest.raises(ValueError, match="live_evidence"):
        validate_outcome_record(record)

    record["live_evidence"] = [_passed("live", "runs/live/report.json")]
    record["recurrence_check"] = _completed_recurrence()
    assert outcome_suppresses_new_case_discovery(record) is True


def test_resolution_requires_honest_recurrence_disposition() -> None:
    record = _record(
        state="resolved",
        target_branch="dev",
        merged_commit="abc123",
        requires_live_verification=False,
        test_evidence=[_passed("test", "tests/test_x.py")],
        original_scenario_evidence=[_passed("original_scenario", "runs/replay")],
        recurrence_check={"status": "not_run"},
    )

    with pytest.raises(ValueError, match="recorded_recurrence_disposition"):
        validate_outcome_record(record)

    record["recurrence_check"] = {
        "status": "not_observed",
        "result": "no_new_source_window",
        "evidence": [],
    }
    record["remaining_risks"] = [
        "Longitudinal recurrence has not been observed because no new source window exists."
    ]
    assert validate_outcome_record(record)["recurrence_check"]["status"] == (
        "not_observed"
    )

    record["recurrence_check"] = _completed_recurrence()
    assert validate_outcome_record(record)["recurrence_check"]["status"] == "completed"


def test_resolution_cannot_relabel_stable_refresh_as_recurrence() -> None:
    record = _record(
        state="resolved",
        target_branch="dev",
        merged_commit="abc123",
        requires_live_verification=False,
        test_evidence=[_passed("test", "tests/test_x.py")],
        original_scenario_evidence=[_passed("original_scenario", "runs/replay")],
        recurrence_check={
            "status": "not_observed",
            "result": "passed",
            "evidence": [_passed("recurrence", "runs/stable-refresh")],
        },
        remaining_risks=["Recurrence has not been observed."],
    )

    with pytest.raises(ValueError, match="unobserved_recurrence_contract_invalid"):
        validate_outcome_record(record)


def test_superseded_outcome_requires_related_identity() -> None:
    with pytest.raises(ValueError, match="requires_related_identity"):
        validate_outcome_record(_record(state="superseded"))


def test_mitigated_requires_tests_and_dedicated_effect_role() -> None:
    record = _record(
        state="mitigated",
        target_branch="dev",
        merged_commit="abc123",
        test_evidence=[_passed("test", "tests/test_x.py")],
        original_scenario_evidence=[
            _passed("original_scenario", "runs/original/outcome_role.json")
        ],
    )
    with pytest.raises(ValueError, match="mitigation_evidence"):
        validate_outcome_record(record)

    record["mitigation_evidence"] = [
        _passed("mitigation_effect", "runs/mitigation/outcome_role.json")
    ]
    assert validate_outcome_record(record)["state"] == "mitigated"

    record["original_scenario_evidence"] = []
    with pytest.raises(ValueError, match="original_scenario_evidence"):
        validate_outcome_record(record)


def test_terminal_stale_blocker_reconciliation_changes_only_runner_owned_risk() -> None:
    terminal = validate_outcome_record(
        _record(
            state="mitigated",
            target_branch="dev",
            merged_commit="abc123",
            requires_live_verification=False,
            test_evidence=[_passed("test", "tests/test_x.py")],
            original_scenario_evidence=[
                _passed("original_scenario", "runs/original/outcome_role.json")
            ],
            mitigation_evidence=[
                _passed("mitigation_effect", "runs/mitigation/outcome_role.json")
            ],
            remaining_risks=[
                "Post-merge outcome verification is blocked: asset_missing",
                "The underlying failure mechanism is not claimed resolved.",
            ],
        )
    )

    reconciled = reconcile_terminal_outcome_stale_blockers(terminal)

    assert reconciled["remaining_risks"] == [
        "The underlying failure mechanism is not claimed resolved."
    ]
    assert {
        key: value for key, value in reconciled.items() if key != "remaining_risks"
    } == {key: value for key, value in terminal.items() if key != "remaining_risks"}
    assert reconcile_terminal_outcome_stale_blockers(reconciled) == reconciled
    with pytest.raises(ValueError, match="requires_terminal_state"):
        reconcile_terminal_outcome_stale_blockers(_record(state="unverified"))


def test_outcome_markdown_round_trip_replaces_previous_record() -> None:
    first = upsert_outcome_markdown("# Ticket\n", _record())
    second_record = _record(state="unverified", remaining_risks=["Original replay unavailable"])
    second = upsert_outcome_markdown(first, second_record)

    assert second.count("<!-- backlog-outcome:start -->") == 1
    assert extract_outcome_markdown(second) == validate_outcome_record(second_record)


def test_transition_requires_target_state_evidence_and_preserves_history() -> None:
    current = validate_outcome_record(
        _record(
            state="tests_verified",
            target_branch="dev",
            merged_commit="abc123",
            test_evidence=[_passed("test", "tests/test_x.py")],
        )
    )

    with pytest.raises(ValueError, match="original_scenario"):
        transition_outcome_record(
            current,
            state="resolved",
            recorded_at="2026-07-10T00:00:00Z",
            updates={},
        )

    resolved = transition_outcome_record(
        current,
        state="resolved",
        recorded_at="2026-07-10T00:00:00Z",
        updates={
            "original_scenario_evidence": [_passed("original_scenario", "runs/replay")],
            "live_evidence": [_passed("live", "runs/live")],
            "recurrence_check": _completed_recurrence(),
            "remaining_risks": [],
        },
    )
    assert resolved["state"] == "resolved"
    assert resolved["history"][-1]["state"] == "tests_verified"

    with pytest.raises(ValueError, match="not_allowed"):
        transition_outcome_record(
            resolved,
            state="mitigated",
            recorded_at="2026-07-11T00:00:00Z",
            updates={},
        )


def test_transition_cannot_rewrite_identity_or_live_verification_boundary() -> None:
    current = validate_outcome_record(
        _record(
            state="tests_verified",
            target_branch="dev",
            merged_commit="abc123",
            test_evidence=[_passed("test", "tests/test_x.py")],
        )
    )
    proof = {
        "original_scenario_evidence": [_passed("original_scenario", "runs/replay")],
        "recurrence_check": _completed_recurrence(),
    }

    for field, value in (
        ("case_id", "case:other"),
        ("plan_revision_id", "plan:other:v2"),
        ("outcome_scope", "plan_copy"),
        ("requires_live_verification", False),
    ):
        with pytest.raises(ValueError, match="immutable_field"):
            transition_outcome_record(
                current,
                state="resolved",
                recorded_at="2026-07-10T00:00:00Z",
                updates={**proof, field: value},
            )


def test_reconcile_preserves_advanced_outcome_during_tests_verified_retry() -> None:
    tests_verified = validate_outcome_record(
        _record(
            state="tests_verified",
            target_branch="dev",
            merged_commit="abc123",
            test_evidence=[_passed("test", "tests/test_x.py")],
        )
    )
    resolved = transition_outcome_record(
        tests_verified,
        state="resolved",
        recorded_at="2026-07-10T00:00:00Z",
        updates={
            "original_scenario_evidence": [_passed("original_scenario", "runs/replay")],
            "live_evidence": [_passed("live", "runs/live")],
            "recurrence_check": _completed_recurrence(),
        },
    )

    assert reconcile_outcome_records(resolved, tests_verified) == resolved

    changed_identity = dict(tests_verified)
    changed_identity["case_id"] = "case:other"
    with pytest.raises(ValueError, match="identity_mismatch"):
        reconcile_outcome_records(resolved, changed_identity)


def test_verification_amendment_is_write_once_and_requires_effective_role_receipts() -> None:
    implementation_commit = "1" * 40
    verification_commit = "2" * 40
    current = _record(
        state="unverified",
        target_branch="dev",
        merged_commit=implementation_commit,
        pr_url="https://example.invalid/pull/10",
        test_evidence=[_passed("test", "tests/test_x.py")],
    )
    amended = bind_outcome_verification_amendment(
        current,
        verification_commit=verification_commit,
        verification_pr_url="https://example.invalid/pull/11",
        recorded_at="2026-07-15T12:00:00Z",
    )

    assert amended["merged_commit"] == implementation_commit
    assert amended["pr_url"] == "https://example.invalid/pull/10"
    assert effective_outcome_verification_commit(amended) == verification_commit
    assert (
        bind_outcome_verification_amendment(
            amended,
            verification_commit=verification_commit,
            verification_pr_url="https://example.invalid/pull/11",
            recorded_at="2026-07-16T12:00:00Z",
        )
        == amended
    )
    with pytest.raises(ValueError, match="already_bound"):
        bind_outcome_verification_amendment(
            amended,
            verification_commit="3" * 40,
            verification_pr_url="https://example.invalid/pull/12",
            recorded_at="2026-07-16T12:00:00Z",
        )

    amendment = amended["verification_amendment"]
    assert isinstance(amendment, dict)

    def _amended_role(kind: str) -> dict[str, object]:
        item = _passed(kind, f"runs/{kind}")
        receipt = item["runner_receipt"]
        assert isinstance(receipt, dict)
        receipt.update(
            {
                "receipt_schema_version": 4,
                "merged_commit": implementation_commit,
                "execution_commit": verification_commit,
                "verification_amendment_id": amendment["amendment_id"],
            }
        )
        return item

    mitigated = validate_outcome_record(
        {
            **amended,
            "state": "mitigated",
            "original_scenario_evidence": [_amended_role("original_scenario")],
            "mitigation_evidence": [_amended_role("mitigation_effect")],
        }
    )
    assert mitigated["state"] == "mitigated"
    wrong = dict(mitigated)
    wrong_mitigation = _amended_role("mitigation_effect")
    wrong_receipt = wrong_mitigation["runner_receipt"]
    assert isinstance(wrong_receipt, dict)
    wrong_receipt["execution_commit"] = "3" * 40
    wrong["mitigation_evidence"] = [wrong_mitigation]
    with pytest.raises(ValueError, match="amendment_receipt_mismatch"):
        validate_outcome_record(wrong)


def test_schema_four_role_receipt_requires_a_verification_amendment() -> None:
    item = _passed("original_scenario", "runs/original_scenario")
    receipt = item["runner_receipt"]
    assert isinstance(receipt, dict)
    receipt.update(
        {
            "receipt_schema_version": 4,
            "merged_commit": "1" * 40,
            "execution_commit": "2" * 40,
            "verification_amendment_id": (
                "outcome_verification_amendment:" + "a" * 64
            ),
        }
    )
    with pytest.raises(ValueError, match="verification_amendment_missing_for_receipt"):
        validate_outcome_record(
            _record(
                state="unverified",
                merged_commit="1" * 40,
                original_scenario_evidence=[item],
            )
        )
