from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from backlog_repo import (
    extract_outcome_markdown,
    transition_outcome_record,
    upsert_outcome_markdown,
)

from usertest_implement.ledger import (
    bind_outcome_verification_amendment_files,
    load_ledger,
    reconcile_terminal_outcome_stale_blockers_files,
    transition_outcome_files,
    update_ledger_file,
)


def _passed(kind: str, reference: str) -> dict[str, object]:
    if kind == "test":
        receipt = {
            "receipt_schema_version": 2,
            "producer": "usertest_implement",
            "verification_producer": "runner_core",
            "evidence_kind": kind,
            "run_dir": f"runs/{kind}",
            "verification_path": f"runs/{kind}/verification.json",
            "verification_sha256": "a" * 64,
            "ticket_ref_path": f"runs/{kind}/ticket_ref.json",
            "ticket_ref_sha256": "b" * 64,
            "ticket_body_sha256": "c" * 64,
            "local_plan_sha256": "d" * 64,
            "local_plan_filename": "ticket.md",
            "verification_contract_sha256": "e" * 64,
            "verification_binding_sha256": "f" * 64,
            "fingerprint": "feedfacefeedface",
            "case_id": "case:ledger",
            "plan_revision_id": "plan:ledger:v1",
            "commands": [f"verify-{kind}"],
        }
    else:
        receipt = {
            "receipt_schema_version": 3,
            "producer": "usertest_implement",
            "verification_producer": "runner_core",
            "evidence_kind": kind,
            "role_artifact_path": f"runs/{kind}/outcome_role.json",
            "role_artifact_sha256": "a" * 64,
            "role_contract_sha256": "b" * 64,
            "ticket_body_sha256": "c" * 64,
            "local_plan_sha256": "d" * 64,
            "local_plan_filename": "ticket.md",
            "verification_contract_sha256": "e" * 64,
            "target_contract_sha256": "f" * 64,
            "verified_implementation_head": "1" * 40,
            "merged_commit": "2" * 40,
            "fingerprint": "feedfacefeedface",
            "case_id": "case:ledger",
            "plan_revision_id": "plan:ledger:v1",
        }
    return {
        "kind": kind,
        "reference": reference,
        "result": "passed",
        "runner_receipt": receipt,
    }


def _recurrence() -> dict[str, object]:
    return {
        "status": "completed",
        "result": "passed",
        "evidence": [_passed("recurrence", "runs/recurrence")],
    }


def _tests_verified_outcome() -> dict[str, object]:
    return {
        "schema_version": 1,
        "case_id": "case:ledger",
        "plan_revision_id": "plan:ledger:v1",
        "state": "tests_verified",
        "recorded_at": "2026-07-09T00:00:00Z",
        "requires_live_verification": True,
        "target_branch": "dev",
        "merged_commit": "abc123",
        "test_evidence": [_passed("test", "tests/test_x.py")],
        "original_scenario_evidence": [],
        "live_evidence": [],
        "remaining_risks": ["Original replay pending"],
        "recurrence_check": {"status": "not_run"},
    }


def test_update_ledger_file_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "ledger.yaml"
    updates = {"last_run_dir": "runs/x", "last_exit_code": 0}

    update_ledger_file(path, fingerprint="deadbeefdeadbeef", updates=updates)
    first = path.read_text(encoding="utf-8")

    update_ledger_file(path, fingerprint="deadbeefdeadbeef", updates=updates)
    second = path.read_text(encoding="utf-8")

    assert first == second


def test_update_ledger_file_preserves_concurrent_updates(tmp_path: Path) -> None:
    path = tmp_path / "ledger.yaml"

    def _worker(i: int) -> None:
        fingerprint = f"{i:016x}"
        update_ledger_file(
            path,
            fingerprint=fingerprint,
            updates={"last_run_dir": f"runs/{i}", "last_exit_code": 0},
        )

    with ThreadPoolExecutor(max_workers=6) as executor:
        list(executor.map(_worker, range(12)))

    doc = load_ledger(path)
    actions = doc.get("actions")
    assert isinstance(actions, dict)
    assert len(actions) == 12
    for i in range(12):
        fingerprint = f"{i:016x}"
        entry = actions.get(fingerprint)
        assert isinstance(entry, dict)
        assert entry.get("last_run_dir") == f"runs/{i}"


def test_load_ledger_rejects_corrupt_state_instead_of_resetting_it(tmp_path: Path) -> None:
    path = tmp_path / "ledger.yaml"
    path.write_text("schema_version: 1\nactions: []\n", encoding="utf-8")

    with pytest.raises(ValueError, match="actions must be a mapping"):
        load_ledger(path)


def test_load_ledger_rejects_entry_fingerprint_mismatch(tmp_path: Path) -> None:
    path = tmp_path / "ledger.yaml"
    path.write_text(
        "schema_version: 1\nupdated_at: null\nactions:\n"
        "  aaaaaaaaaaaaaaaa:\n"
        "    fingerprint: bbbbbbbbbbbbbbbb\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="entry fingerprint mismatch"):
        load_ledger(path)


def test_ledger_retry_preserves_resolved_outcome() -> None:
    tests_verified = _tests_verified_outcome()
    resolved = transition_outcome_record(
        tests_verified,
        state="resolved",
        recorded_at="2026-07-10T00:00:00Z",
        updates={
            "original_scenario_evidence": [_passed("original_scenario", "runs/replay")],
            "live_evidence": [_passed("live", "runs/live")],
            "remaining_risks": [],
            "recurrence_check": _recurrence(),
        },
    )
    doc = {
        "schema_version": 1,
        "actions": {"feedfacefeedface": {"fingerprint": "feedfacefeedface", "outcome": resolved}},
    }

    from usertest_implement.ledger import update_ledger_doc

    updated = update_ledger_doc(
        doc,
        fingerprint="feedfacefeedface",
        updates={"outcome": tests_verified},
    )
    assert updated["actions"]["feedfacefeedface"]["outcome"] == resolved


def test_transition_outcome_files_updates_ticket_and_ledger_together(tmp_path: Path) -> None:
    fingerprint = "feedfacefeedface"
    ticket_path = tmp_path / "ticket.md"
    ledger_path = tmp_path / "ledger.yaml"
    current = _tests_verified_outcome()
    ticket_path.write_text(
        upsert_outcome_markdown(
            "# Ticket\n"
            f"- Fingerprint: `{fingerprint}`\n"
            "- Case ID: `case:ledger`\n"
            "- Plan revision ID: `plan:ledger:v1`\n",
            current,
        ),
        encoding="utf-8",
    )
    update_ledger_file(
        ledger_path,
        fingerprint=fingerprint,
        updates={"outcome": current, "last_outcome_state": "tests_verified"},
    )

    transitioned = transition_outcome_files(
        ledger_path=ledger_path,
        ticket_path=ticket_path,
        fingerprint=fingerprint,
        state="original_scenario_verified",
        recorded_at="2026-07-10T00:00:00Z",
        updates={
            "original_scenario_evidence": [_passed("original_scenario", "runs/replay")],
            "remaining_risks": ["Live verification pending"],
        },
    )

    assert transitioned["state"] == "original_scenario_verified"
    assert extract_outcome_markdown(ticket_path.read_text(encoding="utf-8")) == transitioned
    ledger = load_ledger(ledger_path)
    assert ledger["actions"][fingerprint]["outcome"] == transitioned


def test_transition_outcome_files_rejects_ticket_outcome_case_mismatch(
    tmp_path: Path,
) -> None:
    fingerprint = "feedfacefeedface"
    ticket_path = tmp_path / f"20260710_{fingerprint}_ticket.md"
    ledger_path = tmp_path / "ledger.yaml"
    current = _tests_verified_outcome()
    ticket_path.write_text(
        upsert_outcome_markdown(
            "# Ticket\n"
            f"- Fingerprint: `{fingerprint}`\n"
            "- Case ID: `case:other`\n"
            "- Plan revision ID: `plan:ledger:v1`\n",
            current,
        ),
        encoding="utf-8",
    )
    update_ledger_file(
        ledger_path,
        fingerprint=fingerprint,
        updates={"outcome": current, "last_outcome_state": "tests_verified"},
    )

    with pytest.raises(ValueError, match="case identity mismatch"):
        transition_outcome_files(
            ledger_path=ledger_path,
            ticket_path=ticket_path,
            fingerprint=fingerprint,
            state="original_scenario_verified",
            recorded_at="2026-07-10T00:00:00Z",
            updates={
                "original_scenario_evidence": [
                    _passed("original_scenario", "runs/replay")
                ]
            },
        )


def test_bind_verification_amendment_updates_both_stores_once(tmp_path: Path) -> None:
    fingerprint = "feedfacefeedface"
    ticket_path = tmp_path / f"20260710_{fingerprint}_ticket.md"
    ledger_path = tmp_path / "ledger.yaml"
    current = {
        **_tests_verified_outcome(),
        "merged_commit": "1" * 40,
        "pr_url": "https://example.invalid/pull/10",
    }
    ticket_path.write_text(
        upsert_outcome_markdown(
            "# Ticket\n"
            f"- Fingerprint: `{fingerprint}`\n"
            "- Case ID: `case:ledger`\n"
            "- Plan revision ID: `plan:ledger:v1`\n",
            current,
        ),
        encoding="utf-8",
    )
    update_ledger_file(
        ledger_path,
        fingerprint=fingerprint,
        updates={"outcome": current, "last_outcome_state": "tests_verified"},
    )

    amended = bind_outcome_verification_amendment_files(
        ledger_path=ledger_path,
        ticket_path=ticket_path,
        fingerprint=fingerprint,
        verification_commit="2" * 40,
        verification_pr_url="https://example.invalid/pull/11",
        recorded_at="2026-07-15T12:00:00Z",
    )
    ticket_after = ticket_path.read_bytes()
    ledger_after = ledger_path.read_bytes()

    assert amended["merged_commit"] == "1" * 40
    assert amended["pr_url"] == "https://example.invalid/pull/10"
    assert load_ledger(ledger_path)["actions"][fingerprint]["outcome"] == amended
    assert extract_outcome_markdown(ticket_path.read_text(encoding="utf-8")) == amended

    rebound = bind_outcome_verification_amendment_files(
        ledger_path=ledger_path,
        ticket_path=ticket_path,
        fingerprint=fingerprint,
        verification_commit="2" * 40,
        verification_pr_url="https://example.invalid/pull/11",
        recorded_at="2026-07-16T12:00:00Z",
    )
    assert rebound == amended
    assert ticket_path.read_bytes() == ticket_after
    assert ledger_path.read_bytes() == ledger_after

    with pytest.raises(ValueError, match="already_bound"):
        bind_outcome_verification_amendment_files(
            ledger_path=ledger_path,
            ticket_path=ticket_path,
            fingerprint=fingerprint,
            verification_commit="3" * 40,
            verification_pr_url="https://example.invalid/pull/12",
            recorded_at="2026-07-16T12:00:00Z",
        )
    assert ticket_path.read_bytes() == ticket_after
    assert ledger_path.read_bytes() == ledger_after


def test_terminal_stale_blocker_reconciliation_is_atomic_and_idempotent(
    tmp_path: Path,
) -> None:
    fingerprint = "feedfacefeedface"
    ticket_path = tmp_path / f"20260710_{fingerprint}_ticket.md"
    ledger_path = tmp_path / "ledger.yaml"
    terminal = transition_outcome_record(
        _tests_verified_outcome(),
        state="mitigated",
        recorded_at="2026-07-16T00:00:00Z",
        updates={
            "original_scenario_evidence": [
                _passed("original_scenario", "runs/original")
            ],
            "mitigation_evidence": [_passed("mitigation_effect", "runs/mitigation")],
            "remaining_risks": [
                "Post-merge outcome verification is blocked: transport_missing",
                "The underlying failure mechanism is not claimed resolved.",
            ],
        },
    )
    ticket_path.write_text(
        upsert_outcome_markdown(
            "# Ticket\n"
            f"- Fingerprint: `{fingerprint}`\n"
            "- Case ID: `case:ledger`\n"
            "- Plan revision ID: `plan:ledger:v1`\n",
            terminal,
        ),
        encoding="utf-8",
    )
    update_ledger_file(
        ledger_path,
        fingerprint=fingerprint,
        updates={"outcome": terminal, "last_outcome_state": "mitigated"},
    )

    reconciled = reconcile_terminal_outcome_stale_blockers_files(
        ledger_path=ledger_path,
        ticket_path=ticket_path,
        fingerprint=fingerprint,
    )
    ticket_after = ticket_path.read_bytes()
    ledger_after = ledger_path.read_bytes()

    assert reconciled["state"] == "mitigated"
    assert reconciled["remaining_risks"] == [
        "The underlying failure mechanism is not claimed resolved."
    ]
    assert {
        key: value for key, value in reconciled.items() if key != "remaining_risks"
    } == {key: value for key, value in terminal.items() if key != "remaining_risks"}
    assert extract_outcome_markdown(
        ticket_path.read_text(encoding="utf-8")
    ) == reconciled
    assert load_ledger(ledger_path)["actions"][fingerprint]["outcome"] == reconciled

    assert (
        reconcile_terminal_outcome_stale_blockers_files(
            ledger_path=ledger_path,
            ticket_path=ticket_path,
            fingerprint=fingerprint,
        )
        == reconciled
    )
    assert ticket_path.read_bytes() == ticket_after
    assert ledger_path.read_bytes() == ledger_after
