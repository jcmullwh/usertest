from __future__ import annotations

import json
import subprocess
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

import pytest
from backlog_repo import (
    bind_outcome_verification_amendment,
    canonical_plan_sha256,
    canonical_ticket_body_sha256,
    extract_outcome_markdown,
    parse_verification_contract_markdown,
    render_verification_contract_markdown,
    upsert_outcome_markdown,
)

import usertest_implement.commands.outcome as outcome_command
from usertest_implement.commands.outcome import (
    _cmd_outcome_bind_verification_amendment,
    _git_is_ancestor,
    _load_outcome_updates,
    _verification_commit_on_target_branch,
)
from usertest_implement.outcome_evidence import build_verification_binding
from usertest_implement.parser import build_parser

CASE_ID = "case:outcome"
PLAN_REVISION_ID = "plan:outcome:v1"
FINGERPRINT = "995898bab30e968b"
PLAN_COMMANDS = ["pytest tests/test_outcome.py -q", "ruff check src tests"]


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return proc.stdout.strip()


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


def test_verification_amendment_parser_binds_explicit_correction_provenance(
    tmp_path: Path,
) -> None:
    owner_root = tmp_path / "owner"
    ledger_path = tmp_path / "ledger.yaml"
    args = build_parser().parse_args(
        [
            "--repo-root",
            str(tmp_path),
            "outcome",
            "bind-verification-amendment",
            "--owner-root",
            str(owner_root),
            "--fingerprint",
            FINGERPRINT,
            "--verification-commit",
            "a" * 40,
            "--verification-pr-url",
            "https://example.invalid/pull/215",
            "--ledger",
            str(ledger_path),
        ]
    )

    assert args.outcome_cmd == "bind-verification-amendment"
    assert args.owner_root == owner_root
    assert args.fingerprint == FINGERPRINT
    assert args.ticket_path is None
    assert args.verification_commit == "a" * 40
    assert args.verification_pr_url == "https://example.invalid/pull/215"
    assert args.ledger == ledger_path


def test_verification_amendment_git_checks_accept_descendant_on_remote_target_only(
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

    assert _git_is_ancestor(
        repo,
        ancestor=implementation_commit,
        descendant=correction_commit,
    )
    assert _verification_commit_on_target_branch(
        repo,
        verification_commit=correction_commit,
        target_branch="dev",
    )

    _git(repo, "checkout", "-b", "unrelated", implementation_commit)
    (repo / "unrelated.txt").write_text("unrelated\n", encoding="utf-8")
    _git(repo, "add", "unrelated.txt")
    _git(repo, "commit", "-m", "unrelated sibling")
    unrelated_commit = _git(repo, "rev-parse", "HEAD")
    assert not _git_is_ancestor(
        repo,
        ancestor=correction_commit,
        descendant=unrelated_commit,
    )
    assert not _verification_commit_on_target_branch(
        repo,
        verification_commit=unrelated_commit,
        target_branch="dev",
    )


def test_bind_amendment_accepts_completed_outcome_history_but_rejects_body_drift(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    owner_root = tmp_path / "owner"
    completed_root = owner_root / ".agents" / "plans" / "5 - complete"
    completed_root.mkdir(parents=True)
    ticket_path = completed_root / f"20260715_{FINGERPRINT}_ticket.md"
    exported_markdown = (
        "# Ticket\n\n"
        "Generated by `python -m usertest_backlog.cli reports export-tickets` on now.\n\n"
        f"- Fingerprint: `{FINGERPRINT}`\n\n"
        "Original causal behavior.\n"
    )
    stored_provenance = {
        "schema_version": 1,
        "fingerprint": FINGERPRINT,
        "case_id": f"legacy-case:{FINGERPRINT}",
        "plan_revision_id": f"legacy-plan:{FINGERPRINT}",
        "ticket_body_sha256": canonical_ticket_body_sha256(exported_markdown),
        "local_plan_sha256": canonical_plan_sha256(exported_markdown),
        "local_plan_filename": ticket_path.name,
        "verification_contract_sha256": None,
        "target_contract_sha256": None,
    }
    current = {
        "schema_version": 1,
        "case_id": f"legacy-case:{FINGERPRINT}",
        "plan_revision_id": f"legacy-plan:{FINGERPRINT}",
        "state": "unverified",
        "recorded_at": "2026-07-15T00:00:00Z",
        "requires_live_verification": False,
        "target_branch": "dev",
        "merged_commit": "1" * 40,
        "pr_url": "https://example.invalid/pull/213",
        "ticket_provenance": stored_provenance,
        "test_evidence": [],
        "original_scenario_evidence": [],
        "live_evidence": [],
        "mitigation_evidence": [],
        "remaining_risks": [],
        "recurrence_check": {"status": "not_run"},
    }
    completed_markdown = upsert_outcome_markdown(exported_markdown, current)
    ticket_path.write_bytes(completed_markdown.replace("\n", "\r\r\r\n").encode())
    bound: list[dict[str, object]] = []

    def _fake_bind(**kwargs):
        bound.append(kwargs)
        retained = extract_outcome_markdown(ticket_path.read_text(encoding="utf-8"))
        assert retained is not None
        return bind_outcome_verification_amendment(
            retained,
            verification_commit=str(kwargs["verification_commit"]),
            verification_pr_url=str(kwargs["verification_pr_url"]),
            recorded_at=str(kwargs["recorded_at"]),
        )

    monkeypatch.setattr(outcome_command, "_resolve_repo_root", lambda _path: tmp_path)
    monkeypatch.setattr(
        outcome_command,
        "_resolve_git_commit",
        lambda _repo, commit, **_kwargs: str(commit),
    )
    monkeypatch.setattr(outcome_command, "_git_is_ancestor", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        outcome_command,
        "_verification_commit_on_target_branch",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        outcome_command,
        "bind_outcome_verification_amendment_files",
        _fake_bind,
    )
    args = SimpleNamespace(
        repo_root=tmp_path,
        owner_root=owner_root,
        ticket_path=ticket_path,
        fingerprint=None,
        verification_commit="2" * 40,
        verification_pr_url="https://example.invalid/pull/215",
        ledger=tmp_path / "ledger.yaml",
    )

    assert _cmd_outcome_bind_verification_amendment(args) == 0
    assert len(bound) == 1

    drifted = ticket_path.read_bytes().replace(
        b"Original causal behavior.",
        b"Different causal behavior.",
    )
    ticket_path.write_bytes(drifted)
    with pytest.raises(SystemExit, match="ticket_body_sha256"):
        _cmd_outcome_bind_verification_amendment(args)
    assert len(bound) == 1


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
