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
    load_ledger,
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
