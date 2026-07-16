from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path

import pytest
from backlog_repo import (
    canonical_plan_sha256,
    canonical_ticket_body_sha256,
    parse_verification_contract_markdown,
    render_verification_contract_markdown,
)

from usertest_implement.commands.outcome import _load_outcome_updates
from usertest_implement.outcome_evidence import build_verification_binding

CASE_ID = "case:outcome"
PLAN_REVISION_ID = "plan:outcome:v1"
FINGERPRINT = "995898bab30e968b"
PLAN_COMMANDS = ["pytest tests/test_outcome.py -q", "ruff check src tests"]


def _current() -> dict[str, object]:
    return {
        "case_id": CASE_ID,
        "plan_revision_id": PLAN_REVISION_ID,
        "test_evidence": [],
        "original_scenario_evidence": [],
        "live_evidence": [],
    }


def _write_evidence(path: Path, evidence: dict[str, object]) -> None:
    path.write_text(json.dumps(evidence), encoding="utf-8")


def _ticket_provenance(owner_root: Path) -> dict[str, object]:
    plan_path = (
        owner_root
        / ".agents"
        / "plans"
        / "5 - complete"
        / f"20260710_{FINGERPRINT}_ticket.md"
    )
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    markdown = (
        "# Ticket\n"
        f"- Fingerprint: `{FINGERPRINT}`\n"
        f"- Case ID: `{CASE_ID}`\n"
        f"- Plan revision ID: `{PLAN_REVISION_ID}`\n\n"
        "### Verification command contract\n\n"
        f"{render_verification_contract_markdown(PLAN_COMMANDS)}\n"
    )
    plan_path.write_text(markdown, encoding="utf-8")
    contract = parse_verification_contract_markdown(markdown)
    assert contract is not None
    return {
        "schema_version": 1,
        "fingerprint": FINGERPRINT,
        "case_id": CASE_ID,
        "plan_revision_id": PLAN_REVISION_ID,
        "legacy_identity": False,
        "ticket_body_sha256": canonical_ticket_body_sha256(markdown),
        "local_plan_sha256": canonical_plan_sha256(markdown),
        "local_plan_path": str(plan_path),
        "local_plan_filename": plan_path.name,
        "verification_contract": contract,
        "verification_contract_sha256": contract["contract_sha256"],
        "generated_ticket": True,
    }


def _runner_receipt(
    runs_root: Path,
    *,
    owner_root: Path,
    provenance: dict[str, object],
    configured: list[str] | None = None,
    verification_configured: list[str] | None = None,
    executed: list[dict[str, object]] | None = None,
    include_commands_configured: bool = True,
) -> dict[str, object]:
    binding_commands = PLAN_COMMANDS if configured is None else configured
    receipt_commands = (
        binding_commands if verification_configured is None else verification_configured
    )
    command_results = executed
    if command_results is None:
        command_results = [
            {
                "command": command,
                "exit_code": 0,
                "timed_out": False,
                "cancelled": False,
                "dispatch_blocked": False,
                "rejected_sentinel": None,
            }
            for command in receipt_commands
        ]
    run_dir = runs_root / "test"
    run_dir.mkdir(parents=True, exist_ok=True)
    stored_provenance = {
        key: provenance[key]
        for key in (
            "schema_version",
            "fingerprint",
            "case_id",
            "plan_revision_id",
            "legacy_identity",
            "ticket_body_sha256",
            "local_plan_sha256",
            "local_plan_path",
            "local_plan_filename",
            "verification_contract_sha256",
            "generated_ticket",
        )
    }
    binding = build_verification_binding(
        ticket_provenance=provenance,
        configured_commands=binding_commands,
    )
    (run_dir / "ticket_ref.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "fingerprint": FINGERPRINT,
                "case_id": CASE_ID,
                "plan_revision_id": PLAN_REVISION_ID,
                "ticket_provenance": stored_provenance,
                "verification_binding": binding,
                "owner_repo": {
                    "root": str(owner_root),
                    "idea_path": provenance["local_plan_path"],
                },
            }
        ),
        encoding="utf-8",
    )
    verification_path = run_dir / "verification.json"
    verification = {
        "schema_version": 1,
        "passed": True,
        "status": "passed",
        "terminal_reason": "passed",
        "timed_out": False,
        "cancelled": False,
        "commands": command_results,
    }
    if include_commands_configured:
        verification["commands_configured"] = receipt_commands
    verification_path.write_text(
        json.dumps(verification),
        encoding="utf-8",
    )
    return {
        "run_dir": str(run_dir),
        "verification_sha256": sha256(verification_path.read_bytes()).hexdigest(),
        "evidence_kind": "test",
    }


def _load(
    path: Path,
    *,
    runs_root: Path,
    owner_root: Path,
    provenance: dict[str, object],
) -> dict[str, object]:
    return _load_outcome_updates(
        path,
        current=_current(),
        expected_fingerprint=FINGERPRINT,
        trusted_runs_root=runs_root,
        expected_ticket_provenance=provenance,
        owner_root=owner_root,
    )


def test_outcome_updates_accept_exact_plan_bound_test_receipt(tmp_path: Path) -> None:
    owner_root = tmp_path / "repo"
    provenance = _ticket_provenance(owner_root)
    runs_root = tmp_path / "runs"
    evidence_path = tmp_path / "evidence.json"
    _write_evidence(
        evidence_path,
        {
            "test_evidence": [
                {
                    "kind": "runner_verification",
                    "reference": "ticket-bound tests",
                    "result": "passed",
                    "runner_receipt": _runner_receipt(
                        runs_root,
                        owner_root=owner_root,
                        provenance=provenance,
                    ),
                }
            ]
        },
    )

    updates = _load(
        evidence_path,
        runs_root=runs_root,
        owner_root=owner_root,
        provenance=provenance,
    )

    receipt = updates["test_evidence"][0]["runner_receipt"]
    assert receipt["producer"] == "usertest_implement"
    assert receipt["verification_producer"] == "runner_core"
    assert receipt["commands"] == PLAN_COMMANDS
    assert receipt["ticket_body_sha256"] == provenance["ticket_body_sha256"]


def test_outcome_updates_accept_legacy_receipt_when_executed_commands_exactly_match_binding(
    tmp_path: Path,
) -> None:
    owner_root = tmp_path / "repo"
    provenance = _ticket_provenance(owner_root)
    runs_root = tmp_path / "runs"
    evidence_path = tmp_path / "evidence.json"
    _write_evidence(
        evidence_path,
        {
            "test_evidence": [
                {
                    "kind": "runner_verification",
                    "reference": "legacy exact ticket-bound tests",
                    "result": "passed",
                    "runner_receipt": _runner_receipt(
                        runs_root,
                        owner_root=owner_root,
                        provenance=provenance,
                        include_commands_configured=False,
                    ),
                }
            ]
        },
    )

    updates = _load(
        evidence_path,
        runs_root=runs_root,
        owner_root=owner_root,
        provenance=provenance,
    )

    assert updates["test_evidence"][0]["runner_receipt"]["commands"] == PLAN_COMMANDS


@pytest.mark.parametrize(
    "unsupported",
    [
        {"original_scenario_evidence": []},
        {"live_evidence": []},
        {
            "recurrence_check": {
                "status": "completed",
                "result": "passed",
                "evidence": [],
            }
        },
    ],
)
def test_outcome_updates_reject_unsupported_runner_roles(
    tmp_path: Path,
    unsupported: dict[str, object],
) -> None:
    owner_root = tmp_path / "repo"
    provenance = _ticket_provenance(owner_root)
    evidence_path = tmp_path / "evidence.json"
    _write_evidence(evidence_path, unsupported)

    with pytest.raises(ValueError, match="verifiable evidence|runner-owned recurrence"):
        _load(
            evidence_path,
            runs_root=tmp_path / "runs",
            owner_root=owner_root,
            provenance=provenance,
        )


def test_outcome_updates_record_unobserved_recurrence_without_fake_evidence(
    tmp_path: Path,
) -> None:
    owner_root = tmp_path / "repo"
    provenance = _ticket_provenance(owner_root)
    evidence_path = tmp_path / "evidence.json"
    _write_evidence(
        evidence_path,
        {
            "recurrence_check": {
                "status": "not_observed",
                "result": "no_new_source_window",
                "evidence": [],
            },
            "remaining_risks": [
                "Longitudinal recurrence has not been observed because no new source window exists."
            ],
        },
    )

    updates = _load(
        evidence_path,
        runs_root=tmp_path / "runs",
        owner_root=owner_root,
        provenance=provenance,
    )

    assert updates["recurrence_check"] == {
        "status": "not_observed",
        "result": "no_new_source_window",
        "evidence": [],
    }


def test_outcome_updates_reject_arbitrary_hashed_artifact(tmp_path: Path) -> None:
    owner_root = tmp_path / "repo"
    provenance = _ticket_provenance(owner_root)
    artifact = tmp_path / "not-a-test.txt"
    artifact.write_text("I did not run the plan commands.\n", encoding="utf-8")
    evidence_path = tmp_path / "evidence.json"
    _write_evidence(
        evidence_path,
        {
            "test_evidence": [
                {
                    "kind": "tests",
                    "reference": "claimed tests",
                    "result": "passed",
                    "artifact_path": artifact.name,
                    "artifact_sha256": sha256(artifact.read_bytes()).hexdigest(),
                }
            ]
        },
    )

    with pytest.raises(ValueError, match="requires a runner_receipt"):
        _load(
            evidence_path,
            runs_root=tmp_path / "runs",
            owner_root=owner_root,
            provenance=provenance,
        )


def test_outcome_updates_reject_empty_executed_command_coverage(tmp_path: Path) -> None:
    owner_root = tmp_path / "repo"
    provenance = _ticket_provenance(owner_root)
    runs_root = tmp_path / "runs"
    receipt = _runner_receipt(
        runs_root,
        owner_root=owner_root,
        provenance=provenance,
        executed=[],
    )
    evidence_path = tmp_path / "evidence.json"
    _write_evidence(
        evidence_path,
        {
            "test_evidence": [
                {
                    "kind": "tests",
                    "reference": "empty coverage",
                    "result": "passed",
                    "runner_receipt": receipt,
                }
            ]
        },
    )

    with pytest.raises(ValueError, match="coverage mismatch"):
        _load(
            evidence_path,
            runs_root=runs_root,
            owner_root=owner_root,
            provenance=provenance,
        )


@pytest.mark.parametrize(
    ("binding_commands", "receipt_commands", "message"),
    [
        ([PLAN_COMMANDS[0]], [PLAN_COMMANDS[0]], "explicit stage-6"),
        (["echo ok"], ["echo ok"], "explicit stage-6"),
        (PLAN_COMMANDS, [PLAN_COMMANDS[1], PLAN_COMMANDS[0]], "selected plan contract"),
    ],
)
def test_outcome_updates_reject_partial_forged_or_reordered_commands(
    tmp_path: Path,
    binding_commands: list[str],
    receipt_commands: list[str],
    message: str,
) -> None:
    owner_root = tmp_path / "repo"
    provenance = _ticket_provenance(owner_root)
    runs_root = tmp_path / "runs"
    receipt = _runner_receipt(
        runs_root,
        owner_root=owner_root,
        provenance=provenance,
        configured=binding_commands,
        verification_configured=receipt_commands,
    )
    evidence_path = tmp_path / "evidence.json"
    _write_evidence(
        evidence_path,
        {
            "test_evidence": [
                {
                    "kind": "tests",
                    "reference": "forged command coverage",
                    "result": "passed",
                    "runner_receipt": receipt,
                }
            ]
        },
    )

    with pytest.raises(ValueError, match=message):
        _load(
            evidence_path,
            runs_root=runs_root,
            owner_root=owner_root,
            provenance=provenance,
        )


def test_outcome_updates_reject_stale_cross_plan_body_hash(tmp_path: Path) -> None:
    owner_root = tmp_path / "repo"
    provenance = _ticket_provenance(owner_root)
    runs_root = tmp_path / "runs"
    receipt = _runner_receipt(
        runs_root,
        owner_root=owner_root,
        provenance=provenance,
    )
    ticket_ref_path = Path(str(receipt["run_dir"])) / "ticket_ref.json"
    ticket_ref = json.loads(ticket_ref_path.read_text(encoding="utf-8"))
    ticket_ref["ticket_provenance"]["ticket_body_sha256"] = "f" * 64
    ticket_ref_path.write_text(json.dumps(ticket_ref), encoding="utf-8")
    evidence_path = tmp_path / "evidence.json"
    _write_evidence(
        evidence_path,
        {
            "test_evidence": [
                {
                    "kind": "tests",
                    "reference": "stale plan",
                    "result": "passed",
                    "runner_receipt": receipt,
                }
            ]
        },
    )

    with pytest.raises(ValueError, match="stale or cross-plan"):
        _load(
            evidence_path,
            runs_root=runs_root,
            owner_root=owner_root,
            provenance=provenance,
        )


@pytest.mark.parametrize(
    "unsafe_command",
    [
        'pdm run python -c "import sys; sys.exit(0)"',
        "pytest $(echo tests/test_outcome.py) -q",
    ],
)
def test_plan_bound_verification_rejects_inline_or_substituted_always_pass_commands(
    tmp_path: Path,
    unsafe_command: str,
) -> None:
    provenance = _ticket_provenance(tmp_path / "repo")
    contract_markdown = render_verification_contract_markdown([unsafe_command])
    unsafe_contract = parse_verification_contract_markdown(contract_markdown)
    assert unsafe_contract is not None
    provenance["verification_contract"] = unsafe_contract
    provenance["verification_contract_sha256"] = unsafe_contract["contract_sha256"]

    with pytest.raises(ValueError, match="Unsafe configured verification command"):
        build_verification_binding(
            ticket_provenance=provenance,
            configured_commands=[unsafe_command],
        )
