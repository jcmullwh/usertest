from __future__ import annotations

import argparse
import json
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

import pytest
from backlog_repo import render_verification_contract_markdown
from runner_core.runner import RunResult

from usertest_implement.ci import _wait_for_ci_success
from usertest_implement.commands.review import (
    _build_merge_outcome_record,
    _build_review_correction_prompt,
    _cmd_review_merge,
    _cmd_review_run,
    _parse_retained_review_correction_prompt,
    _recover_noncausal_premerge_rejection,
    _report_without_runner_added_extensions,
    _require_causal_review_acceptance,
    _require_explicit_passing_ci,
    _require_unchanged_reviewed_head,
    _review_correction_context,
)
from usertest_implement.commands.run import _run_selected_ticket
from usertest_implement.outcome_evidence import build_verification_binding
from usertest_implement.outcome_progression import (
    OutcomeContractNotExecutable,
    OutcomeRoleDidNotPass,
)
from usertest_implement.review_context import (
    _build_final_review_summary,
    _build_pr_review_body,
    _build_review_append_prompt,
    _classify_pr_checks,
    _collect_merged_pr_provenance,
    _collect_pr_review_context,
    _extract_agent_review_summary,
    _run_gh_json,
    _run_gh_text,
)
from usertest_implement.selection import (
    _case_plan_fingerprint,
    _select_ticket_from_path,
    _selected_ticket_provenance,
)
from usertest_implement.shared import (
    SelectedTicket,
    _read_json,
)


def _approved_causal_fields() -> dict[str, object]:
    return {
        "mechanism_assessment": "mechanism_addressed",
        "original_scenario_oracle": "exercised",
        "causal_path_assessment": "closed",
        "remaining_causal_paths": [],
    }


def test_merge_outcome_does_not_convert_skipped_check_to_pass(tmp_path: Path) -> None:
    selected = SelectedTicket(
        fingerprint="0123456789abcdef",
        title="Test",
        export_kind="implementation",
        stage="ready_for_ticket",
        owner_root=tmp_path,
        idea_path=None,
        ticket_markdown=(
            "# Test\n\n"
            "- Case ID: `case:test`\n"
            "- Plan revision ID: `planrev:case:test:abc:1`\n"
            "- Requires live verification: `false`\n"
        ),
        tickets_export_path=None,
        export_index=None,
    )

    with pytest.raises(ValueError, match="explicitly passing"):
        _build_merge_outcome_record(
            selected=selected,
            pr_url="https://example.invalid/pr/1",
            pr_context={
                "ci_conclusion": "success",
                "checks": [
                    {
                        "name": "optional",
                        "state": "COMPLETED",
                        "bucket": "skipping",
                        "link": "https://example.invalid/check/optional",
                    }
                ],
            },
            merge_provenance={
                "target_branch": "dev",
                "merged_commit": "abc123",
            },
            review_run_dir=tmp_path / "review",
        )


def test_skipped_or_neutral_only_checks_are_rejected_before_merge() -> None:
    context = {
        "checks": [
            {"name": "optional", "state": "SKIPPING", "bucket": "skipping"},
            {"name": "advisory", "state": "NEUTRAL", "bucket": "skipping"},
        ]
    }

    assert _classify_pr_checks(context["checks"]) == ("completed", "neutral")
    with pytest.raises(SystemExit, match="no explicitly passing CI check"):
        _require_explicit_passing_ci(context)


def test_generic_passing_ci_records_implemented_not_tests_verified(
    tmp_path: Path,
) -> None:
    selected = SelectedTicket(
        fingerprint="0123456789abcdef",
        title="Test",
        export_kind="implementation",
        stage="ready_for_ticket",
        owner_root=tmp_path,
        idea_path=None,
        ticket_markdown=(
            "# Test\n\n"
            "- Case ID: `case:test`\n"
            "- Plan revision ID: `planrev:case:test:abc:1`\n"
            "- Requires live verification: `false`\n"
        ),
        tickets_export_path=None,
        export_index=None,
    )

    outcome = _build_merge_outcome_record(
        selected=selected,
        pr_url="https://example.invalid/pr/1",
        pr_context={
            "checks": [
                {
                    "name": "metadata lint",
                    "state": "SUCCESS",
                    "bucket": "pass",
                    "link": "https://example.invalid/check/lint",
                }
            ]
        },
        merge_provenance={"target_branch": "dev", "merged_commit": "abc123"},
        review_run_dir=tmp_path / "review",
    )

    assert outcome["state"] == "implemented"
    assert outcome["test_evidence"] == []
    assert outcome["ci_evidence"][0]["name"] == "metadata lint"


def test_collect_merged_pr_provenance_uses_merge_commit_oid(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        "usertest_implement.review_context._run_gh_json",
        lambda **_: {
            "url": "https://example.invalid/pr/1",
            "state": "MERGED",
            "baseRefName": "dev",
            "mergeCommit": {"oid": "merge123"},
        },
    )

    provenance = _collect_merged_pr_provenance(
        workspace_dir=tmp_path,
        pr_url="https://example.invalid/pr/1",
    )
    assert provenance["merged_commit"] == "merge123"


def _write_json(path: Path, obj: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _make_ticket(
    owner_root: Path,
    *,
    bucket: str,
    fingerprint: str,
    verification_commands: list[str] | None = None,
) -> Path:
    bucket_dir = owner_root / ".agents" / "plans" / bucket
    bucket_dir.mkdir(parents=True, exist_ok=True)
    ticket_path = bucket_dir / f"20260309_{fingerprint}_ticket.md"
    verification_contract = (
        "\n### Verification command contract\n\n"
        + render_verification_contract_markdown(verification_commands)
        + "\n"
        if verification_commands
        else ""
    )
    ticket_path.write_text(
        "# Ticket\n\n"
        f"- Fingerprint: `{fingerprint}`\n"
        "- Export kind: `implementation`\n"
        "- Stage: `ready_for_ticket`\n"
        f"{verification_contract}",
        encoding="utf-8",
    )
    return ticket_path


def _write_bound_ticket_ref(
    run_dir: Path,
    *,
    fingerprint: str,
    owner_root: Path,
    ticket_path: Path,
    verification_evidence_kind: str | None = None,
    case_id: str | None = None,
    plan_revision_id: str | None = None,
    verification_commands: list[str] | None = None,
    force_v2: bool = False,
) -> None:
    if force_v2 or verification_commands is not None:
        selected = _select_ticket_from_path(ticket_path)
        provenance = _selected_ticket_provenance(
            selected,
            require_local_plan=True,
        )
        binding = build_verification_binding(
            ticket_provenance=provenance,
            configured_commands=verification_commands or [],
        )
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
        _write_json(
            run_dir / "ticket_ref.json",
            {
                "schema_version": 2,
                "fingerprint": fingerprint,
                "case_id": provenance["case_id"],
                "plan_revision_id": provenance["plan_revision_id"],
                "ticket_provenance": stored_provenance,
                "verification_binding": binding,
                "owner_repo": {
                    "root": str(owner_root),
                    "idea_path": str(ticket_path),
                },
            },
        )
        return
    _write_json(
        run_dir / "ticket_ref.json",
        {
            "schema_version": 1,
            "fingerprint": fingerprint,
            "case_id": case_id,
            "plan_revision_id": plan_revision_id,
            "verification_evidence_kind": verification_evidence_kind,
            "owner_repo": {"root": str(owner_root), "idea_path": str(ticket_path)},
        },
    )


def _write_adopted_review_state(
    *,
    run_dir: Path,
    ledger_path: Path,
    owner_root: Path,
    ticket_path: Path,
    fingerprint: str,
    lifecycle_state: str = "awaiting_review",
) -> None:
    ticket_sha256 = sha256(ticket_path.read_bytes()).hexdigest()
    _write_json(
        run_dir / "ticket_resume_state.json",
        {
            "schema_version": 1,
            "kind": "ticket_resume_state",
            "ticket": {"fingerprint": fingerprint, "path": str(ticket_path)},
            "owner_root": str(owner_root),
            "run_dir": str(run_dir),
            "lifecycle_state": lifecycle_state,
        },
    )
    _write_json(
        run_dir / "adoption_ref.json",
        {
            "schema_version": 1,
            "kind": "existing_pr_adoption",
            "run_dir": str(run_dir),
            "fingerprint": fingerprint,
            "ticket_path": str(ticket_path),
            "ticket_sha256": ticket_sha256,
            "flags": {"pr_adopted": True, "ticket_mutated": False},
        },
    )
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    ledger_path.write_text(
        "schema_version: 1\nupdated_at: null\nactions:\n"
        f"  {fingerprint}:\n"
        f"    fingerprint: {fingerprint}\n"
        f"    idea_path: {json.dumps(str(ticket_path))}\n"
        f"    last_run_dir: {json.dumps(str(run_dir))}\n"
        "    last_handoff_mode: adopt_existing_pr\n"
        "    last_resume_lifecycle_state: awaiting_review\n"
        "    pr_adopted: true\n",
        encoding="utf-8",
    )


def _review_ticket_provenance(ticket_path: Path) -> dict[str, object]:
    provenance = _selected_ticket_provenance(
        _select_ticket_from_path(ticket_path),
        require_local_plan=True,
    )
    return {
        key: provenance[key]
        for key in (
            "schema_version",
            "fingerprint",
            "case_id",
            "plan_revision_id",
            "ticket_body_sha256",
            "local_plan_sha256",
            "local_plan_filename",
              "verification_contract_sha256",
              "target_contract_sha256",
          )
    }


def test_merge_outcome_rejects_empty_runner_command_coverage(tmp_path: Path) -> None:
    case_id = "case:test"
    plan_revision_id = "plan:test:v1"
    fingerprint = _case_plan_fingerprint(
        case_id=case_id,
        plan_revision_id=plan_revision_id,
    )
    ticket_path = (
        tmp_path
        / ".agents"
        / "plans"
        / "4 - for_review"
        / f"20260710_{fingerprint}_ticket.md"
    )
    ticket_path.parent.mkdir(parents=True)
    verification_commands = ["pytest tests/test_real.py"]
    ticket_markdown = (
        "# Ticket\n"
        f"- Fingerprint: `{fingerprint}`\n"
        f"- Case ID: `{case_id}`\n"
        f"- Plan revision ID: `{plan_revision_id}`\n"
        "- Requires live verification: `false`\n\n"
        "### Verification command contract\n\n"
        f"{render_verification_contract_markdown(verification_commands)}\n"
    )
    ticket_path.write_text(ticket_markdown, encoding="utf-8")
    run_dir = tmp_path / "runs" / "impl"
    _write_bound_ticket_ref(
        run_dir,
        fingerprint=fingerprint,
        owner_root=tmp_path,
        ticket_path=ticket_path,
        case_id=case_id,
        plan_revision_id=plan_revision_id,
        verification_commands=verification_commands,
    )
    _write_json(
        run_dir / "verification.json",
        {
            "schema_version": 1,
            "passed": True,
            "status": "passed",
            "terminal_reason": "passed",
            "timed_out": False,
            "cancelled": False,
            "commands_configured": verification_commands,
            "commands": [],
        },
    )
    selected = SelectedTicket(
        fingerprint=fingerprint,
        title="Ticket",
        export_kind="implementation",
        stage="ready_for_ticket",
        owner_root=tmp_path,
        idea_path=ticket_path,
        ticket_markdown=ticket_markdown,
        tickets_export_path=None,
        export_index=None,
    )

    with pytest.raises(ValueError, match="coverage mismatch"):
        _build_merge_outcome_record(
            selected=selected,
            pr_url="https://example.invalid/pr/1",
            pr_context={
                "checks": [
                    {
                        "name": "CI",
                        "state": "SUCCESS",
                        "link": "https://example.invalid/check",
                    }
                ]
            },
            merge_provenance={"target_branch": "dev", "merged_commit": "a" * 40},
            review_run_dir=tmp_path / "review",
            implementation_run_dir=run_dir,
        )


def test_merge_outcome_rejects_implementation_run_for_other_ticket(tmp_path: Path) -> None:
    selected_path = tmp_path / ".agents" / "plans" / "4 - for_review" / "ticket.md"
    selected_path.parent.mkdir(parents=True)
    selected_markdown = (
        "# Ticket\n"
        "- Fingerprint: `0123456789abcdef`\n"
        "- Requires live verification: `false`\n"
    )
    selected_path.write_text(selected_markdown, encoding="utf-8")
    selected = SelectedTicket(
        fingerprint="0123456789abcdef",
        title="Ticket",
        export_kind="implementation",
        stage="ready_for_ticket",
        owner_root=tmp_path,
        idea_path=selected_path,
        ticket_markdown=selected_markdown,
        tickets_export_path=None,
        export_index=None,
    )
    run_dir = tmp_path / "runs" / "other"
    _write_bound_ticket_ref(
        run_dir,
        fingerprint="fedcba9876543210",
        owner_root=tmp_path,
        ticket_path=tmp_path / "other.md",
    )

    with pytest.raises(ValueError, match="fingerprint mismatch"):
        _build_merge_outcome_record(
            selected=selected,
            pr_url="https://example.invalid/pr/1",
            pr_context={"checks": [{"state": "SUCCESS"}]},
            merge_provenance={"target_branch": "dev", "merged_commit": "a" * 40},
            review_run_dir=tmp_path / "review",
            implementation_run_dir=run_dir,
        )


def _review_run_args(
    *,
    repo_root: Path,
    owner_root: Path,
    ticket_path: Path,
    ledger: Path,
) -> argparse.Namespace:
    return argparse.Namespace(
        repo_root=repo_root,
        owner_root=owner_root,
        ticket_path=ticket_path,
        fingerprint=None,
        ledger=ledger,
        agent="codex",
        model=None,
        policy="write",
        persona_id="compliance_sentinel",
        mission_id="review_backlog_implementation_pr_v1",
        seed=0,
        agent_config_override=[],
        keep_workspace=False,
        exec_backend="local",
        exec_use_host_agent_login=True,
        exec_use_target_sandbox_cli_install=False,
        exec_docker_profile=None,
        exec_keep_container=True,
        exec_cache="warm",
        exec_cache_dir=None,
        maintenance_venv_cache=True,
        dry_run=False,
    )


def _review_simple_args(
    *,
    repo_root: Path,
    owner_root: Path,
    ticket_path: Path,
    ledger: Path,
) -> argparse.Namespace:
    return argparse.Namespace(
        repo_root=repo_root,
        owner_root=owner_root,
        ticket_path=ticket_path,
        fingerprint=None,
        ledger=ledger,
    )


def _prepare_premerge_failure_case(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    raised: Exception,
) -> dict[str, object]:
    repo_root = tmp_path / "repo_root"
    fingerprint = "causalpremerge01"
    ticket_path = _make_ticket(
        repo_root,
        bucket="4 - for_review",
        fingerprint=fingerprint,
    )
    implementation_run_dir = repo_root / "runs" / "implementation" / "1"
    implementation_run_dir.mkdir(parents=True)
    review_run_dir = repo_root / "runs" / "review" / "1"
    pr_url = "https://example.invalid/pr/213"
    reviewed_head = "a" * 40
    scope = {
        "schema_version": 2,
        "status": "verified",
        "receipt_sha256": "scope-receipt",
    }
    review_summary = {
        "schema_version": 1,
        "ticket_fingerprint": fingerprint,
        "ticket_path": str(ticket_path),
        "run_dir": str(review_run_dir),
        "pr_url": pr_url,
        "reviewed_head_oid": reviewed_head,
        "review_decision": "approved",
        "causal_acceptance": True,
        "merge_ready": True,
        "ci_conclusion": "success",
        "mechanism_assessment": "mechanism_addressed",
        "original_scenario_oracle": "exercised",
        "causal_path_assessment": "closed",
        "implementation_scope": scope,
        "findings": [],
    }
    _write_json(review_run_dir / "review_summary.json", review_summary)
    ledger_path = repo_root / ".agents" / "state" / "backlog_implement_actions.yaml"
    ledger_path.parent.mkdir(parents=True)
    ledger_path.write_text(
        "schema_version: 1\nupdated_at: null\nactions:\n"
        f"  {fingerprint}:\n"
        f"    fingerprint: {fingerprint}\n"
        f"    last_run_dir: {json.dumps(str(implementation_run_dir))}\n"
        f"    last_review_run_dir: {json.dumps(str(review_run_dir))}\n",
        encoding="utf-8",
    )
    pr_context = {
        "pr": {
            "number": 213,
            "url": pr_url,
            "state": "OPEN",
            "isDraft": False,
            "mergeable": "MERGEABLE",
            "baseRefName": "dev",
            "headRefOid": reviewed_head,
        },
        "ci_status": "completed",
        "ci_conclusion": "success",
        "checks": [{"name": "tests", "state": "SUCCESS", "bucket": "pass"}],
    }
    provenance = {
        "generated_ticket": True,
        "case_id": "case:premerge-failure",
        "plan_revision_id": "planrev:premerge-failure:1",
        "target_contract": {"targets": []},
    }

    monkeypatch.setattr(
        "usertest_implement.commands.review._require_review_artifact_bindings",
        lambda **_: implementation_run_dir,
    )
    monkeypatch.setattr(
        "usertest_implement.commands.review._preflight_existing_outcome_stores",
        lambda **_: None,
    )
    monkeypatch.setattr(
        "usertest_implement.commands.review._selected_ticket_provenance",
        lambda *_args, **_kwargs: provenance,
    )
    monkeypatch.setattr(
        "usertest_implement.commands.review._collect_pr_review_context",
        lambda **_: dict(pr_context),
    )
    monkeypatch.setattr(
        "usertest_implement.commands.review.validate_verified_implementation_head",
        lambda **_: {"verified_implementation_head": reviewed_head},
    )
    monkeypatch.setattr(
        "usertest_implement.commands.review._attach_deterministic_plan_scope",
        lambda *, pr_context, **_: {**pr_context, "implementation_scope": scope},
    )
    preflight_outcome = {
        "state": "tests_verified",
        "case_id": provenance["case_id"],
        "plan_revision_id": provenance["plan_revision_id"],
        "target_branch": "dev",
        "merged_commit": reviewed_head,
        "ticket_provenance": {"verified_implementation_head": reviewed_head},
    }
    monkeypatch.setattr(
        "usertest_implement.commands.review._build_merge_outcome_record",
        lambda **_: dict(preflight_outcome),
    )
    monkeypatch.setattr(
        "usertest_implement.commands.review._reconcile_merge_outcome",
        lambda **kwargs: kwargs["proposed"],
    )

    def _raise_premerge(**_kwargs):
        raise raised

    monkeypatch.setattr(
        "usertest_implement.commands.review.verify_premerge_original_scenario",
        _raise_premerge,
    )
    monkeypatch.setattr(
        "usertest_implement.commands.review.write_ticket_resume_state",
        lambda **_: {"lifecycle_state": "review_changes_requested"},
    )

    def _unexpected_subprocess(*_args, **_kwargs):
        raise AssertionError("merge subprocess must not run after premerge failure")

    monkeypatch.setattr(
        "usertest_implement.commands.review.subprocess.run",
        _unexpected_subprocess,
    )
    return {
        "repo_root": repo_root,
        "ticket_path": ticket_path,
        "review_run_dir": review_run_dir,
        "ledger_path": ledger_path,
        "review_summary": review_summary,
    }


def _prepare_legacy_premerge_recovery_case(tmp_path: Path) -> dict[str, object]:
    owner_root = tmp_path / "owner"
    fingerprint = "legacyenospace01"
    ticket_path = _make_ticket(
        owner_root,
        bucket="4 - for_review",
        fingerprint=fingerprint,
    )
    selected = _select_ticket_from_path(ticket_path)
    implementation_run_dir = tmp_path / "runs" / "implementation" / "1"
    implementation_run_dir.mkdir(parents=True)
    review_run_dir = tmp_path / "runs" / "review" / "1"
    pr_url = "https://example.invalid/pr/213"
    reviewed_head = "b" * 40
    scope = {
        "schema_version": 2,
        "status": "verified",
        "receipt_sha256": "scope-receipt",
    }
    overwritten_summary = {
        "schema_version": 1,
        "ticket_fingerprint": fingerprint,
        "ticket_path": str(ticket_path),
        "run_dir": str(review_run_dir),
        "pr_url": pr_url,
        "reviewed_head_oid": reviewed_head,
        "review_decision": "changes_requested",
        "causal_acceptance": True,
        "merge_ready": False,
        "ci_conclusion": "success",
        "mechanism_assessment": "mechanism_not_addressed",
        "original_scenario_oracle": "not_exercised",
        "causal_path_assessment": "open",
        "remaining_causal_paths": ["checkout failed: No space left on device"],
        "implementation_scope": scope,
        "rationale": "checkout failed: No space left on device",
        "findings": [
            {
                "severity": "critical",
                "title": "Original evidence-backed scenario still fails",
            }
        ],
        "correction_count": 4,
        "review_author_session_id": "review-session",
    }
    _write_json(review_run_dir / "review_summary.json", overwritten_summary)
    _write_json(
        review_run_dir / "premerge_original_scenario_failure.json",
        {
            "schema_version": 1,
            "status": "changes_requested",
            "detail": "git worktree add failed: No space left on device",
            "role_artifact_path": None,
        },
    )
    _write_json(
        review_run_dir / "report.json",
        {
            "schema_version": 1,
            "kind": "task_run_v1",
            "status": "success",
            "summary": "Approved causal implementation.",
            "issues": [],
            "extensions": {
                "reviewed_head_oid": reviewed_head,
                "review_summary": {
                    "review_decision": "approved",
                    "approach_alignment": "aligned",
                    "mechanism_assessment": "mechanism_addressed",
                    "original_scenario_oracle": "exercised",
                    "causal_path_assessment": "closed",
                    "remaining_causal_paths": [],
                    "scope_assessment": "appropriate",
                    "rationale": "The retained review found the causal path closed.",
                },
            },
        },
    )
    _write_json(
        review_run_dir / "review_ref.json",
        {
            "schema_version": 2,
            "pr_url": pr_url,
            "reviewed_head_oid": reviewed_head,
        },
    )
    ledger_path = tmp_path / ".agents" / "state" / "backlog_implement_actions.yaml"
    ledger_path.parent.mkdir(parents=True)
    ledger_path.write_text(
        "schema_version: 1\nupdated_at: null\nactions:\n"
        f"  {fingerprint}:\n"
        f"    fingerprint: {fingerprint}\n"
        f"    last_review_run_dir: {json.dumps(str(review_run_dir))}\n"
        "    last_review_decision: changes_requested\n",
        encoding="utf-8",
    )
    pr_context = {
        "pr": {
            "number": 213,
            "url": pr_url,
            "title": "PR",
            "state": "OPEN",
            "isDraft": False,
            "mergeable": "MERGEABLE",
            "headRefName": "backlog/test",
            "headRefOid": reviewed_head,
            "baseRefName": "dev",
        },
        "ci_status": "completed",
        "ci_conclusion": "success",
        "checks": [{"name": "tests", "state": "SUCCESS", "bucket": "pass"}],
        "implementation_scope": scope,
    }
    return {
        "selected": selected,
        "implementation_run_dir": implementation_run_dir,
        "review_run_dir": review_run_dir,
        "ledger_path": ledger_path,
        "pr_url": pr_url,
        "reviewed_head": reviewed_head,
        "pr_context": pr_context,
        "overwritten_summary": overwritten_summary,
    }


def test_review_merge_classifies_failed_outcome_role_as_causal_rejection(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    role_artifact = tmp_path / "roles" / "original_scenario" / "outcome_role.json"
    case = _prepare_premerge_failure_case(
        monkeypatch,
        tmp_path,
        raised=OutcomeRoleDidNotPass(
            role="original_scenario",
            artifact_path=role_artifact,
            timed_out=False,
        ),
    )

    exit_code = _cmd_review_merge(
        _review_simple_args(
            repo_root=case["repo_root"],
            owner_root=case["repo_root"],
            ticket_path=case["ticket_path"],
            ledger=case["ledger_path"],
        )
    )

    assert exit_code == 4
    review_run_dir = case["review_run_dir"]
    failure = _read_json(review_run_dir / "premerge_original_scenario_failure.json")
    assert isinstance(failure, dict)
    assert failure["status"] == "changes_requested"
    assert failure["role_artifact_path"] == str(role_artifact)
    assert not (review_run_dir / "premerge_original_scenario_blocked.json").exists()
    summary = _read_json(review_run_dir / "review_summary.json")
    assert isinstance(summary, dict)
    assert summary["review_decision"] == "changes_requested"
    assert summary["causal_acceptance"] is False
    assert summary["mechanism_assessment"] == "mechanism_not_addressed"
    assert summary["original_scenario_oracle"] == "not_exercised"
    assert summary["causal_path_assessment"] == "open"
    assert summary["findings"][-1]["title"] == "Original evidence-backed scenario still fails"
    ledger_text = case["ledger_path"].read_text(encoding="utf-8")
    assert "last_premerge_original_scenario_status: failed" in ledger_text
    assert "last_review_decision: changes_requested" in ledger_text
    assert "last_review_causal_acceptance: false" in ledger_text


def test_review_merge_routes_nonexecutable_outcome_contract_to_same_author_correction(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    receipt_path = tmp_path / "roles" / "outcome_contract_executability.json"
    failure = {
        "role": "live",
        "command_index": 0,
        "command": "python -m pytest tests/missing.py::test_live",
        "reason": "pytest_target_path_missing",
    }
    case = _prepare_premerge_failure_case(
        monkeypatch,
        tmp_path,
        raised=OutcomeContractNotExecutable(
            receipt_path=receipt_path,
            failures=[failure],
        ),
    )

    exit_code = _cmd_review_merge(
        _review_simple_args(
            repo_root=case["repo_root"],
            owner_root=case["repo_root"],
            ticket_path=case["ticket_path"],
            ledger=case["ledger_path"],
        )
    )

    assert exit_code == 4
    review_run_dir = case["review_run_dir"]
    correction = _read_json(
        review_run_dir / "premerge_outcome_contract_not_executable.json"
    )
    assert isinstance(correction, dict)
    assert correction["status"] == "changes_requested"
    assert correction["classification"] == "outcome_contract_not_executable"
    assert correction["causal_result"] == "not_run"
    assert correction["executability_receipt_path"] == str(receipt_path)
    assert correction["failures"] == [failure]
    assert not (review_run_dir / "premerge_original_scenario_blocked.json").exists()
    summary = _read_json(review_run_dir / "review_summary.json")
    assert isinstance(summary, dict)
    assert summary["review_decision"] == "changes_requested"
    assert summary["merge_ready"] is False
    assert summary["mechanism_assessment"] == "mechanism_addressed"
    assert summary["findings"][-1]["title"] == (
        "Mandatory outcome role command is not executable"
    )
    ledger_text = case["ledger_path"].read_text(encoding="utf-8")
    assert "last_premerge_outcome_contract_status: correction_required" in ledger_text
    assert "last_resume_lifecycle_state: review_changes_requested" in ledger_text


@pytest.mark.parametrize(
    "raised",
    [
        OSError("No space left on device"),
        RuntimeError("git worktree setup failed"),
        ValueError("outcome contract unavailable"),
    ],
    ids=["oserror", "runtimeerror", "valueerror"],
)
def test_review_merge_classifies_premerge_setup_errors_as_infrastructure_blocks(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    raised: Exception,
) -> None:
    case = _prepare_premerge_failure_case(
        monkeypatch,
        tmp_path,
        raised=raised,
    )
    review_run_dir = case["review_run_dir"]
    summary_path = review_run_dir / "review_summary.json"
    summary_before = summary_path.read_bytes()

    exit_code = _cmd_review_merge(
        _review_simple_args(
            repo_root=case["repo_root"],
            owner_root=case["repo_root"],
            ticket_path=case["ticket_path"],
            ledger=case["ledger_path"],
        )
    )

    assert exit_code == 3
    assert summary_path.read_bytes() == summary_before
    assert not (review_run_dir / "premerge_original_scenario_failure.json").exists()
    blocked = _read_json(review_run_dir / "premerge_original_scenario_blocked.json")
    assert isinstance(blocked, dict)
    assert blocked["status"] == "blocked"
    assert blocked["classification"] == "premerge_infrastructure"
    assert blocked["causal_result"] == "not_run"
    assert blocked["review_preserved"] is True
    assert str(raised) in blocked["detail"]
    ledger_text = case["ledger_path"].read_text(encoding="utf-8")
    assert "last_premerge_original_scenario_status: blocked_infrastructure" in ledger_text
    assert "last_review_decision: approved" in ledger_text


def test_legacy_enospc_recovery_restores_approved_review_and_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    case = _prepare_legacy_premerge_recovery_case(tmp_path)
    monkeypatch.setattr(
        "usertest_implement.commands.review.write_ticket_resume_state",
        lambda **_: {"lifecycle_state": "awaiting_merge"},
    )

    restored = _recover_noncausal_premerge_rejection(
        selected=case["selected"],
        ledger_path=case["ledger_path"],
        review_run_dir=case["review_run_dir"],
        review_summary=case["overwritten_summary"],
        pr_url=case["pr_url"],
        pr_context=case["pr_context"],
        implementation_run_dir=case["implementation_run_dir"],
    )

    assert restored["review_decision"] == "approved"
    assert restored["causal_acceptance"] is True
    assert restored["mechanism_assessment"] == "mechanism_addressed"
    assert restored["original_scenario_oracle"] == "exercised"
    assert restored["causal_path_assessment"] == "closed"
    assert restored["remaining_causal_paths"] == []
    assert "No space left on device" not in restored["rationale"]
    review_run_dir = case["review_run_dir"]
    reclassification_path = (
        review_run_dir / "premerge_original_scenario_infrastructure_reclassification.json"
    )
    reclassification = _read_json(reclassification_path)
    assert isinstance(reclassification, dict)
    assert reclassification["classification"] == "premerge_infrastructure"
    assert reclassification["causal_result"] == "not_run"
    assert reclassification["review_preserved"] is True
    assert reclassification["source_failure_path"] == str(
        review_run_dir / "premerge_original_scenario_failure.json"
    )
    assert (review_run_dir / "premerge_original_scenario_failure.json").exists()
    ledger_text = case["ledger_path"].read_text(encoding="utf-8")
    assert "last_review_decision: approved" in ledger_text
    assert "last_review_causal_acceptance: true" in ledger_text
    assert "last_premerge_original_scenario_status: blocked_infrastructure" in ledger_text
    assert "last_resume_lifecycle_state: awaiting_merge" in ledger_text

    stable_paths = [
        review_run_dir / "review_summary.json",
        reclassification_path,
        case["ledger_path"],
    ]
    stable_bytes = {path: path.read_bytes() for path in stable_paths}
    second = _recover_noncausal_premerge_rejection(
        selected=case["selected"],
        ledger_path=case["ledger_path"],
        review_run_dir=review_run_dir,
        review_summary=restored,
        pr_url=case["pr_url"],
        pr_context=case["pr_context"],
        implementation_run_dir=case["implementation_run_dir"],
    )
    assert second == restored
    assert {path: path.read_bytes() for path in stable_paths} == stable_bytes


def test_legacy_premerge_recovery_does_not_reclassify_a_role_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    case = _prepare_legacy_premerge_recovery_case(tmp_path)
    failure_path = case["review_run_dir"] / "premerge_original_scenario_failure.json"
    failure = _read_json(failure_path)
    assert isinstance(failure, dict)
    failure["role_artifact_path"] = str(tmp_path / "outcome_role.json")
    _write_json(failure_path, failure)
    summary_path = case["review_run_dir"] / "review_summary.json"
    summary_before = summary_path.read_bytes()
    ledger_before = case["ledger_path"].read_bytes()

    observed = _recover_noncausal_premerge_rejection(
        selected=case["selected"],
        ledger_path=case["ledger_path"],
        review_run_dir=case["review_run_dir"],
        review_summary=case["overwritten_summary"],
        pr_url=case["pr_url"],
        pr_context=case["pr_context"],
        implementation_run_dir=case["implementation_run_dir"],
    )

    assert observed == case["overwritten_summary"]
    assert summary_path.read_bytes() == summary_before
    assert case["ledger_path"].read_bytes() == ledger_before
    assert not (
        case["review_run_dir"]
        / "premerge_original_scenario_infrastructure_reclassification.json"
    ).exists()


@pytest.mark.parametrize(
    "mismatch",
    ["current_pr_url", "current_head", "report_head", "review_ref_head"],
)
def test_legacy_premerge_recovery_refuses_provenance_mismatches_without_mutation(
    tmp_path: Path,
    mismatch: str,
) -> None:
    case = _prepare_legacy_premerge_recovery_case(tmp_path)
    if mismatch == "current_pr_url":
        case["pr_context"]["pr"]["url"] = "https://example.invalid/pr/other"
    elif mismatch == "current_head":
        case["pr_context"]["pr"]["headRefOid"] = "c" * 40
    elif mismatch == "report_head":
        report_path = case["review_run_dir"] / "report.json"
        report = _read_json(report_path)
        assert isinstance(report, dict)
        report["extensions"]["reviewed_head_oid"] = "c" * 40
        _write_json(report_path, report)
    else:
        review_ref_path = case["review_run_dir"] / "review_ref.json"
        review_ref = _read_json(review_ref_path)
        assert isinstance(review_ref, dict)
        review_ref["reviewed_head_oid"] = "c" * 40
        _write_json(review_ref_path, review_ref)

    review_run_dir = case["review_run_dir"]
    stable_paths = [
        review_run_dir / "review_summary.json",
        review_run_dir / "premerge_original_scenario_failure.json",
        case["ledger_path"],
    ]
    stable_bytes = {path: path.read_bytes() for path in stable_paths}

    with pytest.raises(SystemExit, match="retained review provenance does not match"):
        _recover_noncausal_premerge_rejection(
            selected=case["selected"],
            ledger_path=case["ledger_path"],
            review_run_dir=review_run_dir,
            review_summary=case["overwritten_summary"],
            pr_url=case["pr_url"],
            pr_context=case["pr_context"],
            implementation_run_dir=case["implementation_run_dir"],
        )

    assert {path: path.read_bytes() for path in stable_paths} == stable_bytes
    assert not (
        review_run_dir / "premerge_original_scenario_infrastructure_reclassification.json"
    ).exists()


def test_build_final_review_summary_requires_green_ci_and_alignment(tmp_path: Path) -> None:
    owner_root = tmp_path / "repo"
    ticket_path = _make_ticket(owner_root, bucket="4 - for_review", fingerprint="abc123abc123abcd")
    report = {
        "issues": [
            {
                "severity": "warn",
                "title": "Scope note",
                "details": "Touched one extra file.",
            }
        ]
    }
    summary = _build_final_review_summary(
        selected=type("Selected", (), {
            "fingerprint": "abc123abc123abcd",
            "idea_path": ticket_path,
        })(),
        review_run_dir=tmp_path / "review_run",
        pr_url="https://example.invalid/pr/1",
        pr_context={
            "pr": {
                "number": 1,
                "title": "PR",
                "state": "OPEN",
                "isDraft": False,
                "mergeable": "MERGEABLE",
                "baseRefName": "dev",
                "headRefOid": "abc123def456",
                "headRefName": "branch",
            },
            "ci_status": "completed",
            "ci_conclusion": "success",
            "implementation_scope": {
                "status": "verified",
                "receipt_sha256": "a" * 64,
            },
            "checks": [
                {
                    "name": "tests",
                    "state": "SUCCESS",
                    "bucket": "pass",
                    "link": "https://example.invalid/check/1",
                }
            ],
        },
        agent_summary={
            "review_decision": "approved",
            "approach_alignment": "aligned",
            **_approved_causal_fields(),
            "scope_assessment": "appropriate",
            "rationale": "Looks good.",
        },
        report=report,
    )
    assert summary["causal_acceptance"] is True
    assert summary["merge_ready"] is True
    assert summary["reviewed_head_oid"] == "abc123def456"
    assert len(summary["findings"]) == 1

    critical = _build_final_review_summary(
        selected=type(
            "Selected",
            (),
            {"fingerprint": "abc123abc123abcd", "idea_path": ticket_path},
        )(),
        review_run_dir=tmp_path / "review_critical",
        pr_url="https://example.invalid/pr/1",
        pr_context={
            **{
                key: value
                for key, value in {
                    "pr": {
                        "number": 1,
                        "title": "PR",
                        "state": "OPEN",
                        "isDraft": False,
                        "mergeable": "MERGEABLE",
                        "baseRefName": "dev",
                        "headRefOid": "abc123def456",
                        "headRefName": "branch",
                    },
                    "ci_status": "completed",
                    "ci_conclusion": "success",
                    "implementation_scope": {
                        "status": "verified",
                        "receipt_sha256": "a" * 64,
                    },
                }.items()
            }
        },
        agent_summary={
            "review_decision": "approved",
            "approach_alignment": "aligned",
            **_approved_causal_fields(),
            "scope_assessment": "appropriate",
            "rationale": "Approved despite the finding.",
        },
        report={
            "issues": [
                {
                    "severity": "critical",
                    "title": "Data loss",
                    "details": "The implementation deletes retained evidence.",
                }
            ]
        },
    )
    assert critical["causal_acceptance"] is False
    assert critical["merge_ready"] is False
    assert critical["blocking_finding_count"] == 1

    scope_blocked = _build_final_review_summary(
        selected=type(
            "Selected",
            (),
            {"fingerprint": "abc123abc123abcd", "idea_path": ticket_path},
        )(),
        review_run_dir=tmp_path / "review_scope_failed",
        pr_url="https://example.invalid/pr/1",
        pr_context={
            "pr": {
                "number": 1,
                "title": "PR",
                "state": "OPEN",
                "isDraft": False,
                "mergeable": "MERGEABLE",
                "baseRefName": "dev",
                "headRefOid": "abc123def456",
                "headRefName": "branch",
            },
            "ci_status": "completed",
            "ci_conclusion": "success",
            "implementation_scope": {
                "status": "failed",
                "errors": ["implementation_scope_unplanned_path:extra.py"],
            },
        },
        agent_summary={
            "review_decision": "approved",
            "approach_alignment": "aligned",
            **_approved_causal_fields(),
            "scope_assessment": "appropriate",
            "rationale": "Model says it is aligned.",
        },
        report=report,
    )
    assert scope_blocked["causal_acceptance"] is False
    assert scope_blocked["merge_ready"] is False
    assert scope_blocked["deterministic_scope_verified"] is False

    blocked = _build_final_review_summary(
        selected=type("Selected", (), {
            "fingerprint": "abc123abc123abcd",
            "idea_path": ticket_path,
        })(),
        review_run_dir=tmp_path / "review_run2",
        pr_url="https://example.invalid/pr/1",
        pr_context={
            "pr": {
                "number": 1,
                "title": "PR",
                "state": "OPEN",
                "isDraft": False,
                "mergeable": "MERGEABLE",
                "headRefOid": "abc123def456",
                "headRefName": "branch",
                "baseRefName": "dev",
            },
            "ci_status": "pending",
            "ci_conclusion": None,
            "implementation_scope": {"status": "verified"},
        },
        agent_summary={
            "review_decision": "approved",
            "approach_alignment": "aligned",
            **_approved_causal_fields(),
            "scope_assessment": "appropriate",
            "rationale": "Looks good.",
        },
        report=report,
    )
    assert blocked["causal_acceptance"] is True
    assert blocked["merge_ready"] is False


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("mechanism_assessment", "symptom_only"),
        ("original_scenario_oracle", "not_exercised"),
        ("causal_path_assessment", "unclear"),
    ],
)
def test_build_final_review_summary_rejects_shallow_causal_acceptance(
    tmp_path: Path,
    field: str,
    value: str,
) -> None:
    agent_summary: dict[str, object] = {
        "review_decision": "approved",
        "approach_alignment": "aligned",
        **_approved_causal_fields(),
        "scope_assessment": "appropriate",
        "rationale": "The diff is narrow and CI is green.",
    }
    agent_summary[field] = value

    summary = _build_final_review_summary(
        selected=SimpleNamespace(
            fingerprint="abc123abc123abcd",
            idea_path=tmp_path / "ticket.md",
        ),
        review_run_dir=tmp_path / "review_run",
        pr_url="https://example.invalid/pr/1",
        pr_context={
            "pr": {
                "number": 1,
                "title": "PR",
                "state": "OPEN",
                "isDraft": False,
                "mergeable": "MERGEABLE",
                "baseRefName": "dev",
                "headRefOid": "abc123def456",
                "headRefName": "branch",
            },
            "ci_status": "completed",
            "ci_conclusion": "success",
            "implementation_scope": {"status": "verified"},
        },
        agent_summary=agent_summary,
        report={"issues": []},
    )

    assert summary["causal_acceptance"] is False
    assert summary["merge_ready"] is False
    assert summary[field] == value


def test_scope_label_is_advisory_without_a_concrete_blocking_finding(
    tmp_path: Path,
) -> None:
    summary = _build_final_review_summary(
        selected=SimpleNamespace(
            fingerprint="abc123abc123abcd",
            idea_path=tmp_path / "ticket.md",
        ),
        review_run_dir=tmp_path / "review_run",
        pr_url="https://example.invalid/pr/1",
        pr_context={
            "pr": {
                "number": 1,
                "title": "PR",
                "state": "OPEN",
                "isDraft": False,
                "mergeable": "MERGEABLE",
                "baseRefName": "dev",
                "headRefOid": "abc123def456",
                "headRefName": "branch",
            },
            "ci_status": "completed",
            "ci_conclusion": "success",
            "implementation_scope": {
                "status": "verified",
                "advisories": ["implementation_scope_extra_path:extra_support.py"],
            },
        },
        agent_summary={
            "review_decision": "approved",
            "approach_alignment": "aligned",
            **_approved_causal_fields(),
            "scope_assessment": "excessive",
            "rationale": "The wider propagation is unnecessary but not harmful.",
        },
        report={"issues": []},
    )

    assert summary["scope_assessment"] == "excessive"
    assert summary["causal_acceptance"] is True
    assert summary["merge_ready"] is True


def test_review_prompt_centers_causal_acceptance_before_scope() -> None:
    prompt = _build_review_append_prompt(
        selected=SimpleNamespace(
            ticket_markdown=(
                "# Ticket\n\n"
                "Researched mechanism: stale lifecycle classification.\n"
                "Original-scenario oracle: replay incomplete run directory.\n"
            )
        ),
        handoff_summary=None,
        pr_ref=None,
        ci_gate=None,
        pr_context={
            "pr": {},
            "checks": [],
            "changed_files": ["extra_support.py"],
            "diff_excerpt": "diff --git a/core.py b/core.py",
            "implementation_scope": {
                "status": "verified_with_advisories",
                "advisories": ["implementation_scope_extra_path:extra_support.py"],
            },
        },
    )

    causal_position = prompt.index("Start with the researched failure mechanism")
    scope_position = prompt.index("# Scope advisory and immutable head/target gate")
    assert causal_position < scope_position
    assert "merely suppresses a visible symptom" in prompt
    assert "not the mutable merge gate" in prompt
    assert "bound original-scenario oracle" in prompt
    assert "which causal paths, if any, can still reproduce the problem" in prompt
    assert "Scope is secondary to causal correctness" in prompt
    assert "hard-blocks only a missing planned production target" in prompt


def test_review_correction_reuses_exact_prior_reviewer_frontier(tmp_path: Path) -> None:
    fingerprint = "c0rrectc0rrect00"
    ticket_path = _make_ticket(
        tmp_path,
        bucket="2 - ready",
        fingerprint=fingerprint,
    )
    selected = _select_ticket_from_path(ticket_path)
    implementation_run_dir = tmp_path / "runs" / "implementation"
    implementation_run_dir.mkdir(parents=True)
    previous_review_run_dir = tmp_path / "runs" / "review" / "first"
    previous_review_run_dir.mkdir(parents=True)
    workspace_dir = tmp_path / "review_workspace"
    workspace_dir.mkdir()
    pr_url = "https://example.invalid/pr/213"
    head_oid = "e" * 40
    session_id = "019f6733-3afd-7270-839c-5e9d0ca4ff70"
    _write_json(
        previous_review_run_dir / "review_summary.json",
        {
            "ticket_fingerprint": fingerprint,
            "pr_url": pr_url,
            "reviewed_head_oid": head_oid,
            "review_decision": "changes_requested",
        },
    )
    _write_json(
        previous_review_run_dir / "review_ref.json",
        {"implementation_run_dir": str(implementation_run_dir)},
    )
    _write_json(
        previous_review_run_dir / "target_ref.json",
        {"agent": "codex", "model": "gpt-5.6-terra"},
    )
    _write_json(
        previous_review_run_dir / "workspace_ref.json",
        {"workspace_dir": str(workspace_dir)},
    )
    (previous_review_run_dir / "raw_events.jsonl").write_text(
        json.dumps({"type": "thread.started", "thread_id": session_id}) + "\n",
        encoding="utf-8",
    )
    ledger_path = tmp_path / ".agents" / "state" / "actions.yaml"
    ledger_path.parent.mkdir(parents=True)
    ledger_path.write_text(
        "schema_version: 1\nactions:\n"
        f"  {fingerprint}:\n"
        f"    last_review_run_dir: {json.dumps(str(previous_review_run_dir))}\n",
        encoding="utf-8",
    )

    context = _review_correction_context(
        selected=selected,
        implementation_run_dir=implementation_run_dir,
        ledger_path=ledger_path,
        pr_url=pr_url,
        reviewed_head_oid=head_oid,
    )

    assert context["codex_resume_session_id"] == session_id
    assert context["resume_workspace_dir"] == workspace_dir.resolve()
    assert context["prior_model"] == "gpt-5.6-terra"
    prompt = _build_review_correction_prompt(
        corrections=["Verify that the planned test was dependency-correctly relocated."],
        correction_context=context,
        pr_context={"pr": {"url": pr_url}, "checks": []},
    )
    assert "Preserve every valid observation" in prompt
    assert "complete replacement `task_run_v1`" in prompt
    assert "dependency-correctly relocated" in prompt


def test_review_correction_refuses_a_different_pr_head(tmp_path: Path) -> None:
    fingerprint = "c0rrectc0rrect01"
    ticket_path = _make_ticket(
        tmp_path,
        bucket="2 - ready",
        fingerprint=fingerprint,
    )
    selected = _select_ticket_from_path(ticket_path)
    implementation_run_dir = tmp_path / "runs" / "implementation"
    implementation_run_dir.mkdir(parents=True)
    previous_review_run_dir = tmp_path / "runs" / "review" / "first"
    _write_json(
        previous_review_run_dir / "review_summary.json",
        {
            "ticket_fingerprint": fingerprint,
            "pr_url": "https://example.invalid/pr/213",
            "reviewed_head_oid": "a" * 40,
        },
    )
    _write_json(
        previous_review_run_dir / "review_ref.json",
        {"implementation_run_dir": str(implementation_run_dir)},
    )
    ledger_path = tmp_path / ".agents" / "state" / "actions.yaml"
    ledger_path.parent.mkdir(parents=True)
    ledger_path.write_text(
        "schema_version: 1\nactions:\n"
        f"  {fingerprint}:\n"
        f"    last_review_run_dir: {json.dumps(str(previous_review_run_dir))}\n",
        encoding="utf-8",
    )

    with pytest.raises(SystemExit, match="different PR head"):
        _review_correction_context(
            selected=selected,
            implementation_run_dir=implementation_run_dir,
            ledger_path=ledger_path,
            pr_url="https://example.invalid/pr/213",
            reviewed_head_oid="b" * 40,
        )


def test_retained_review_correction_prompt_preserves_order_and_prior_binding(
    tmp_path: Path,
) -> None:
    previous = {
        "ticket_fingerprint": "03f0d43eb78e28fa",
        "pr_url": "https://example.invalid/pr/213",
        "reviewed_head_oid": "e" * 40,
    }
    context = {
        "previous_summary": previous,
        "previous_review_run_dir": tmp_path / "previous",
    }
    prompt = _build_review_correction_prompt(
        corrections=["Keep the valid frontier.", "Correct only the proven defect."],
        correction_context=context,
        pr_context={
            "pr": {
                "url": "https://example.invalid/pr/213",
                "headRefOid": "e" * 40,
            },
            "checks": [],
        },
    )
    prompt_path = tmp_path / "prompt.txt"
    prompt_path.write_text(prompt, encoding="utf-8")

    parsed = _parse_retained_review_correction_prompt(prompt_path)

    assert parsed["corrections"] == [
        "Keep the valid frontier.",
        "Correct only the proven defect.",
    ]
    assert parsed["previous_review_summary"] == previous
    assert parsed["pr"]["headRefOid"] == "e" * 40


def test_retained_review_report_allows_only_documented_runner_extensions() -> None:
    agent_report = {
        "schema_version": 1,
        "kind": "task_run_v1",
        "extensions": {"review_summary": {"review_decision": "changes_requested"}},
    }
    materialized = json.loads(json.dumps(agent_report))
    materialized["extensions"].update(
        {
            "verification": {"status": "disabled"},
            "python_toolchain_capability": {"toolchain_status": "not_required"},
            "shell_capability": {"sandbox_mode": "read-only"},
        }
    )

    assert _report_without_runner_added_extensions(materialized) == agent_report
    materialized["summary"] = "runner must not rewrite model-authored content"
    assert _report_without_runner_added_extensions(materialized) != agent_report


def test_extract_agent_review_summary_requires_explicit_causal_path_accounting() -> None:
    review_summary = {
        "review_decision": "approved",
        "approach_alignment": "aligned",
        **_approved_causal_fields(),
        "scope_assessment": "appropriate",
        "rationale": "The mechanism and original scenario are covered.",
    }
    parsed = _extract_agent_review_summary(
        {"extensions": {"review_summary": review_summary}}
    )
    assert parsed["mechanism_assessment"] == "mechanism_addressed"
    assert parsed["remaining_causal_paths"] == []

    invalid = dict(review_summary)
    invalid["causal_path_assessment"] = "residual"
    with pytest.raises(ValueError, match="must name at least one path"):
        _extract_agent_review_summary(
            {"extensions": {"review_summary": invalid}}
        )


def test_build_pr_review_body_includes_findings_and_merge_state() -> None:
    body = _build_pr_review_body(
        review_summary={
            "review_decision": "changes_requested",
            "approach_alignment": "diverged",
            "mechanism_assessment": "symptom_only",
            "original_scenario_oracle": "not_exercised",
            "causal_path_assessment": "residual",
            "remaining_causal_paths": ["The alternate CLI bypass remains open."],
            "scope_assessment": "excessive",
            "rationale": "The implementation drifted from the selected approach.",
            "merge_ready": False,
            "findings": [
                {
                    "severity": "high",
                    "title": "Behavior regression",
                    "details": "The PR changes the CLI contract.",
                    "evidence": {"path": "apps/usertest_implement/src/usertest_implement/cli.py"},
                    "suggested_fix": "Restore the original CLI arguments.",
                }
            ],
        }
    )

    assert "## Automated implementation review" in body
    assert "- Decision: `changes_requested`" in body
    assert "- Researched mechanism: `symptom_only`" in body
    assert "- Original-scenario oracle: `not_exercised`" in body
    assert "- Causal paths: `residual`" in body
    assert "- Merge ready: `no`" in body
    assert "1. [high] Behavior regression" in body
    assert "Evidence:" in body
    assert "Suggested fix: Restore the original CLI arguments." in body


def test_merge_rejects_pr_head_changed_after_review() -> None:
    with pytest.raises(SystemExit, match="head changed after automated review"):
        _require_unchanged_reviewed_head(
            fingerprint="deadbeefdeadbeef",
            review_summary={"reviewed_head_oid": "reviewed123"},
            pr_meta={"headRefOid": "new456"},
        )


def test_review_run_writes_review_summary_and_updates_ledger(monkeypatch, tmp_path: Path) -> None:
    repo_root = tmp_path / "repo_root"
    owner_root = repo_root
    owner_root.mkdir(parents=True)
    ticket_path = _make_ticket(owner_root, bucket="4 - for_review", fingerprint="feedfacefeedface")
    ledger_path = repo_root / ".agents" / "state" / "backlog_implement_actions.yaml"
    impl_run_dir = repo_root / "runs" / "impl" / "0"
    review_run_dir = repo_root / "runs" / "review" / "0"
    _write_json(
        impl_run_dir / "handoff_summary.json",
        {
            "schema_version": 1,
            "pr_created": True,
            "pr_url": "https://example.invalid/pr/2",
            "ci_conclusion": "success",
        },
    )
    _write_json(
        impl_run_dir / "pr_ref.json",
        {"created": True, "url": "https://example.invalid/pr/2"},
    )
    _write_bound_ticket_ref(
        impl_run_dir,
        fingerprint="feedfacefeedface",
        owner_root=owner_root,
        ticket_path=ticket_path,
    )
    _write_json(impl_run_dir / "ci_gate.json", {"passed": True})
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    ledger_path.write_text(
        "schema_version: 1\nupdated_at: null\nactions:\n"
        "  feedfacefeedface:\n"
        "    fingerprint: feedfacefeedface\n"
        f"    last_run_dir: {json.dumps(str(impl_run_dir))}\n",
        encoding="utf-8",
    )

    def _fake_collect_pr_review_context(*, workspace_dir: Path, pr_url: str) -> dict[str, object]:
        assert workspace_dir == owner_root
        assert pr_url == "https://example.invalid/pr/2"
        return {
            "pr": {
                "number": 2,
                "url": pr_url,
                "title": "PR",
                "state": "OPEN",
                "isDraft": False,
                "mergeable": "MERGEABLE",
                "headRefOid": "abc123def456",
                "headRefName": "backlog/review",
                "baseRefName": "dev",
            },
            "checks": [{"name": "CI", "state": "SUCCESS"}],
            "ci_status": "completed",
            "ci_conclusion": "success",
            "changed_files": ["apps/usertest_implement/src/usertest_implement/cli.py"],
            "diff_excerpt": "diff --git a/file b/file",
            "diff_truncated": False,
        }

    def _fake_run_once(_cfg, _request):
        assert _request.repo == "https://example.invalid/repo.git"
        assert _request.ref == "abc123def456"
        assert _request.agent_append_system_prompt is None
        assert _request.agent_append_system_prompt_file is not None
        assert _request.agent_append_system_prompt_file.exists()
        _write_json(
            review_run_dir / "report.json",
            {
                "schema_version": 1,
                "kind": "task_run_v1",
                "status": "success",
                "goal": "Review",
                "summary": "Reviewed.",
                "steps": [
                    {
                        "name": "Review",
                        "attempts": [{"action": "Reviewed"}],
                        "outcome": "done",
                    }
                ],
                "outputs": [],
                "next_actions": ["Merge after approval."],
                "issues": [],
                "extensions": {
                    "review_summary": {
                        "review_decision": "approved",
                        "approach_alignment": "aligned",
                        **_approved_causal_fields(),
                        "scope_assessment": "appropriate",
                        "rationale": "Aligned and scoped correctly.",
                    }
                },
            },
        )
        return RunResult(run_dir=review_run_dir, exit_code=0, report_validation_errors=[])

    monkeypatch.setattr(
        "usertest_implement.commands.review._load_runner_config",
        lambda _repo_root: object(),
    )
    monkeypatch.setattr(
        "usertest_implement.commands.review._collect_pr_review_context",
        _fake_collect_pr_review_context,
    )
    monkeypatch.setattr(
        "usertest_implement.commands.review._git_remote_url",
        lambda *, repo_dir, remote_name: "https://example.invalid/repo.git",
    )
    monkeypatch.setattr(
        "usertest_implement.commands.review._infer_git_root",
        lambda path: owner_root,
    )
    monkeypatch.setattr(
        "usertest_implement.commands.review._maintenance_profile_is_eligible",
        lambda *, repo_root, repo_input: False,
    )
    monkeypatch.setattr("usertest_implement.commands.review.run_once", _fake_run_once)

    def _fake_subprocess_run(
        argv,
        cwd=None,
        capture_output=None,
        text=None,
        encoding=None,
        errors=None,
        check=None,
    ):
        assert cwd == str(owner_root)
        assert capture_output is True
        assert text is True
        assert encoding == "utf-8"
        assert errors == "replace"
        assert check is False
        assert argv[:4] == ["gh", "pr", "review", "https://example.invalid/pr/2"]
        assert "--comment" in argv
        body_path = Path(argv[argv.index("--body-file") + 1])
        body_text = body_path.read_text(encoding="utf-8")
        assert "Automated implementation review" in body_text
        assert "- Decision: `approved`" in body_text
        return SimpleNamespace(returncode=0, stdout="review submitted", stderr="")

    monkeypatch.setattr("usertest_implement.commands.review.subprocess.run", _fake_subprocess_run)
    monkeypatch.setattr(
        "usertest_implement.commands.review._collect_merged_pr_provenance",
        lambda **_: {
            "pr_url": "https://example.invalid/pr/4",
            "target_branch": "dev",
            "merged_commit": "abc123def456",
        },
    )

    exit_code = _cmd_review_run(
        _review_run_args(
            repo_root=repo_root,
            owner_root=owner_root,
            ticket_path=ticket_path,
            ledger=ledger_path,
        )
    )
    assert exit_code == 0
    review_summary = _read_json(review_run_dir / "review_summary.json")
    assert isinstance(review_summary, dict)
    assert review_summary["merge_ready"] is True
    assert review_summary["review_decision"] == "approved"
    pr_review_ref = _read_json(review_run_dir / "pr_review_ref.json")
    assert isinstance(pr_review_ref, dict)
    assert pr_review_ref["submitted"] is True
    assert pr_review_ref["event"] == "COMMENT"

    ledger_text = ledger_path.read_text(encoding="utf-8")
    assert "last_review_run_dir" in ledger_text
    assert "last_review_causal_acceptance: true" in ledger_text
    assert "last_review_merge_ready: true" in ledger_text.lower()


def test_review_merge_refuses_when_summary_not_causally_accepted() -> None:
    with pytest.raises(SystemExit, match="causally accepted"):
        _require_causal_review_acceptance(
            {
                "review_decision": "changes_requested",
                "causal_acceptance": False,
                "merge_ready": False,
                "ci_conclusion": "failure",
            }
        )


def test_review_merge_moves_ticket_to_complete(monkeypatch, tmp_path: Path) -> None:
    repo_root = tmp_path / "repo_root"
    owner_root = repo_root
    owner_root.mkdir(parents=True)
    verification_commands = ["pdm run pytest -q"]
    ticket_path = _make_ticket(
        owner_root,
        bucket="4 - for_review",
        fingerprint="cafebabecafebabe",
        verification_commands=verification_commands,
    )
    complete_path = owner_root / ".agents" / "plans" / "5 - complete" / ticket_path.name
    (owner_root / ".agents" / "plans" / "5 - complete").mkdir(parents=True, exist_ok=True)
    ledger_path = repo_root / ".agents" / "state" / "backlog_implement_actions.yaml"
    impl_run_dir = repo_root / "runs" / "impl" / "2"
    review_run_dir = repo_root / "runs" / "review" / "2"
    _write_bound_ticket_ref(
        impl_run_dir,
        fingerprint="cafebabecafebabe",
        owner_root=owner_root,
        ticket_path=ticket_path,
        verification_commands=verification_commands,
    )
    _write_json(
        impl_run_dir / "verification.json",
        {
            "schema_version": 1,
            "passed": True,
            "status": "passed",
            "terminal_reason": "passed",
            "timed_out": False,
            "cancelled": False,
            "commands_configured": verification_commands,
            "commands": [
                {
                    "command": "pdm run pytest -q",
                    "exit_code": 0,
                    "timed_out": False,
                    "cancelled": False,
                    "dispatch_blocked": False,
                    "rejected_sentinel": None,
                }
            ],
        },
    )
    _write_json(
        impl_run_dir / "pr_ref.json",
        {"schema_version": 1, "created": True, "url": "https://example.invalid/pr/4"},
    )
    review_ticket_provenance = _review_ticket_provenance(ticket_path)
    implementation_ticket_ref_sha256 = sha256(
        (impl_run_dir / "ticket_ref.json").read_bytes()
    ).hexdigest()
    _write_json(
        review_run_dir / "review_summary.json",
        {
            "schema_version": 1,
            "ticket_fingerprint": "cafebabecafebabe",
            "run_dir": str(review_run_dir),
            "pr_url": "https://example.invalid/pr/4",
            "review_decision": "approved",
            "causal_acceptance": True,
            "merge_ready": False,
            "ci_conclusion": "failure",
            "reviewed_head_oid": "abc123def456",
            "ticket_provenance": review_ticket_provenance,
            "implementation_ticket_ref_sha256": implementation_ticket_ref_sha256,
        },
    )
    _write_json(
        review_run_dir / "review_ref.json",
        {
            "schema_version": 2,
            "ticket_fingerprint": "cafebabecafebabe",
            "ticket_path": str(ticket_path),
            "implementation_run_dir": str(impl_run_dir),
            "ticket_provenance": review_ticket_provenance,
            "implementation_ticket_ref_sha256": implementation_ticket_ref_sha256,
            "pr_url": "https://example.invalid/pr/4",
        },
    )
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    ledger_path.write_text(
        "schema_version: 1\nupdated_at: null\nactions:\n"
        "  cafebabecafebabe:\n"
        "    fingerprint: cafebabecafebabe\n"
        f"    last_run_dir: {json.dumps(str(impl_run_dir))}\n"
        f"    last_review_run_dir: {json.dumps(str(review_run_dir))}\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(
        "usertest_implement.commands.review._collect_pr_review_context",
        lambda **_: {
            "pr": {
                "number": 4,
                "url": "https://example.invalid/pr/4",
                "state": "OPEN",
                "isDraft": False,
                "mergeable": "MERGEABLE",
                "baseRefName": "dev",
                "headRefOid": "abc123def456",
            },
            "ci_status": "completed",
            "ci_conclusion": "success",
            "checks": [
                {
                    "name": "tests",
                    "state": "SUCCESS",
                    "bucket": "pass",
                    "link": "https://example.invalid/check/merge",
                }
            ],
        },
    )

    def _fake_subprocess_run(argv, cwd=None, capture_output=None, text=None, check=None):
        assert argv[:3] == ["gh", "pr", "merge"]
        assert argv[-2:] == ["--match-head-commit", "abc123def456"]

        class _Proc:
            returncode = 0
            stdout = "merged"
            stderr = ""

        return _Proc()

    monkeypatch.setattr("usertest_implement.commands.review.subprocess.run", _fake_subprocess_run)
    monkeypatch.setattr(
        "usertest_implement.commands.review._collect_merged_pr_provenance",
        lambda **_: {
            "pr_url": "https://example.invalid/pr/4",
            "target_branch": "dev",
            "merged_commit": "abc123def456",
        },
    )

    exit_code = _cmd_review_merge(
        _review_simple_args(
            repo_root=repo_root,
            owner_root=owner_root,
            ticket_path=ticket_path,
            ledger=ledger_path,
        )
    )
    assert exit_code == 0
    assert complete_path.exists()
    merge_ref = _read_json(review_run_dir / "merge_ref.json")
    assert isinstance(merge_ref, dict)
    assert merge_ref["merged"] is True
    assert merge_ref["target_branch"] == "dev"
    assert merge_ref["merged_commit"] == "abc123def456"
    assert merge_ref["outcome_state"] == "tests_verified"
    completed_markdown = complete_path.read_text(encoding="utf-8")
    assert '"state": "tests_verified"' in completed_markdown
    assert '"original_scenario_evidence": []' in completed_markdown
    resume_state = _read_json(impl_run_dir / "ticket_resume_state.json")
    assert isinstance(resume_state, dict)
    assert resume_state["lifecycle_state"] == "complete"
    assert resume_state["ticket"]["path"] == str(complete_path)
    ledger_text = ledger_path.read_text(encoding="utf-8")
    assert "last_resume_state_path" in ledger_text
    assert "last_resume_lifecycle_state: complete" in ledger_text.lower()
    assert "last_outcome_state: tests_verified" in ledger_text.lower()


def test_review_merge_preflights_outcome_before_merge_subprocess(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo_root"
    case_id = "case:preflight"
    plan_revision_id = "plan:preflight:v1"
    fingerprint = _case_plan_fingerprint(
        case_id=case_id,
        plan_revision_id=plan_revision_id,
    )
    ticket_path = (
        repo_root
        / ".agents"
        / "plans"
        / "4 - for_review"
        / f"20260710_{fingerprint}_ticket.md"
    )
    ticket_path.parent.mkdir(parents=True)
    ticket_path.write_text(
        "# Ticket\n"
        f"- Fingerprint: `{fingerprint}`\n"
        f"- Case ID: `{case_id}`\n"
        f"- Plan revision ID: `{plan_revision_id}`\n",
        encoding="utf-8",
    )
    impl_run_dir = repo_root / "runs" / "impl" / "preflight"
    review_run_dir = repo_root / "runs" / "review" / "preflight"
    _write_bound_ticket_ref(
        impl_run_dir,
        fingerprint=fingerprint,
        owner_root=repo_root,
        ticket_path=ticket_path,
        case_id=case_id,
        plan_revision_id=plan_revision_id,
        force_v2=True,
    )
    pr_url = "https://example.invalid/pr/preflight"
    _write_json(
        impl_run_dir / "pr_ref.json",
        {"schema_version": 1, "created": True, "url": pr_url},
    )
    review_ticket_provenance = _review_ticket_provenance(ticket_path)
    implementation_ticket_ref_sha256 = sha256(
        (impl_run_dir / "ticket_ref.json").read_bytes()
    ).hexdigest()
    _write_json(
        review_run_dir / "review_summary.json",
        {
            "schema_version": 1,
            "ticket_fingerprint": fingerprint,
            "run_dir": str(review_run_dir),
            "pr_url": pr_url,
            "review_decision": "approved",
            "merge_ready": True,
            "ci_conclusion": "success",
            "reviewed_head_oid": "abc123def456",
            "ticket_provenance": review_ticket_provenance,
            "implementation_ticket_ref_sha256": implementation_ticket_ref_sha256,
        },
    )
    _write_json(
        review_run_dir / "review_ref.json",
        {
            "schema_version": 2,
            "ticket_fingerprint": fingerprint,
            "ticket_path": str(ticket_path),
            "implementation_run_dir": str(impl_run_dir),
            "ticket_provenance": review_ticket_provenance,
            "implementation_ticket_ref_sha256": implementation_ticket_ref_sha256,
            "pr_url": pr_url,
        },
    )
    ledger_path = repo_root / ".agents" / "state" / "backlog_implement_actions.yaml"
    ledger_path.parent.mkdir(parents=True)
    ledger_path.write_text(
        "schema_version: 1\nupdated_at: null\nactions:\n"
        f"  {fingerprint}:\n"
        f"    fingerprint: {fingerprint}\n"
        f"    last_run_dir: {json.dumps(str(impl_run_dir))}\n"
        f"    last_review_run_dir: {json.dumps(str(review_run_dir))}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "usertest_implement.commands.review._collect_pr_review_context",
        lambda **_: {
            "pr": {
                "url": pr_url,
                "state": "OPEN",
                "isDraft": False,
                "mergeable": "MERGEABLE",
                "baseRefName": "dev",
                "headRefOid": "abc123def456",
            },
            "ci_status": "completed",
            "ci_conclusion": "success",
            "checks": [{"name": "tests", "state": "SUCCESS"}],
        },
    )
    merge_calls: list[list[str]] = []

    def _unexpected_merge(argv, **_kwargs):
        merge_calls.append(list(argv))
        raise AssertionError("merge subprocess must not run before outcome preflight")

    monkeypatch.setattr(
        "usertest_implement.commands.review.subprocess.run",
        _unexpected_merge,
    )

    with pytest.raises(ValueError, match="missing Requires live verification"):
        _cmd_review_merge(
            _review_simple_args(
                repo_root=repo_root,
                owner_root=repo_root,
                ticket_path=ticket_path,
                ledger=ledger_path,
            )
        )
    assert merge_calls == []


def test_run_defers_review_until_for_review_and_green_ci(
    monkeypatch,
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo_root"
    repo_root.mkdir(parents=True)
    target_repo = tmp_path / "target_repo"
    target_repo.mkdir(parents=True)
    ticket_path = _make_ticket(target_repo, bucket="2 - ready", fingerprint="facefacefaceface")
    impl_run_dir = repo_root / "runs" / "impl" / "0"
    impl_run_dir.mkdir(parents=True, exist_ok=True)
    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir(parents=True, exist_ok=True)

    def _fake_run_once(_cfg, _request):
        _write_json(
            impl_run_dir / "workspace_ref.json",
            {"workspace_dir": str(workspace_dir)},
        )
        return SimpleNamespace(run_dir=impl_run_dir, exit_code=0, report_validation_errors=[])

    monkeypatch.setattr("usertest_implement.commands.run.run_once", _fake_run_once)
    monkeypatch.setattr(
        "usertest_implement.commands.run._maintenance_profile_is_eligible",
        lambda **_: False,
    )
    monkeypatch.setattr(
        "usertest_implement.commands.run._git_head_sha",
        lambda _workspace_dir: "abc123",
    )
    monkeypatch.setattr(
        "usertest_implement.commands.run.finalize_commit",
        lambda **_: {"commit_performed": True, "branch": "backlog/test", "head_commit": "abc123"},
    )
    monkeypatch.setattr(
        "usertest_implement.commands.run.finalize_push",
        lambda **_: {"pushed": True, "remote_name": "origin", "remote_url": "https://example.invalid/repo.git"},
    )
    review_run_dir = repo_root / "runs" / "review" / "0"

    def _fake_run_review_for_selected_ticket(**kwargs):
        assert kwargs["repo_root"] == repo_root
        assert kwargs["owner_root"] == target_repo
        assert kwargs["implementation_run_dir"] == impl_run_dir
        assert kwargs["review_agent"] == "claude"
        assert kwargs["review_model"] == "review-model"
        return (
            review_run_dir,
            {
                "review_decision": "approved",
                "merge_ready": True,
                "ci_conclusion": "success",
            },
        )

    monkeypatch.setattr(
        "usertest_implement.commands.run._run_review_for_selected_ticket",
        _fake_run_review_for_selected_ticket,
    )

    def _fake_run_gh_text(*, cwd: Path, argv: list[str]) -> str:
        assert cwd == workspace_dir
        if argv[:3] == ["gh", "pr", "create"]:
            return "https://example.invalid/pr/55\n"
        raise AssertionError(f"unexpected gh call: {argv}")

    monkeypatch.setattr("usertest_implement.commands.run._run_gh_text", _fake_run_gh_text)

    args = argparse.Namespace(
        repo_root=repo_root,
        settings=None,
        settings_profile=None,
        repo=str(target_repo),
        ref=None,
        agent="codex",
        model=None,
        policy="write",
        persona_id="thoughtful_maintainer",
        mission_id="implement_maintenance_backlog_ticket_v1",
        implementation_review_agent="claude",
        implementation_review_model="review-model",
        seed=0,
        agent_config_override=[],
        keep_workspace=False,
        exec_backend="local",
        exec_use_host_agent_login=True,
        exec_use_target_sandbox_cli_install=False,
        exec_docker_profile=None,
        exec_keep_container=True,
        exec_cache="warm",
        exec_cache_dir=None,
        maintenance_venv_cache=True,
        dry_run=False,
        verification_commands=[],
        verification_timeout_seconds=None,
        skip_verify=False,
        verify_reuse="auto",
        ci_timeout_seconds=60.0,
        skip_ci_wait=True,
        draft_pr_on_ci_failure=True,
        commit=True,
        branch=None,
        commit_message=None,
        git_user_name=None,
        git_user_email=None,
        push=True,
        remote_name="origin",
        remote_url=None,
        force_push=False,
        base_branch="dev",
        pr=True,
        move_on_start=False,
        move_on_commit=True,
        ledger=Path(".agents/state/backlog_implement_actions.yaml"),
        ticket_path=ticket_path,
        tickets_export=None,
        fingerprint=None,
        _settings_info=None,
    )

    cfg = object()
    selected = SelectedTicket(
        fingerprint="facefacefaceface",
        title="Ticket",
        export_kind="implementation",
        stage="ready_for_ticket",
        owner_root=target_repo,
        idea_path=ticket_path,
        ticket_markdown=ticket_path.read_text(encoding="utf-8"),
        tickets_export_path=None,
        export_index=None,
    )

    exit_code = _run_selected_ticket(
        args=args,
        repo_root=repo_root,
        cfg=cfg,
        selected=selected,
    )
    assert exit_code == 0
    handoff_summary = _read_json(impl_run_dir / "handoff_summary.json")
    assert isinstance(handoff_summary, dict)
    assert handoff_summary["pr_created"] is True
    assert handoff_summary["review_required"] is True
    assert handoff_summary["review_run_dir"] == str(review_run_dir)
    assert handoff_summary["review_merge_ready"] is True
    assert handoff_summary["final_status"] == "success"


def test_run_records_missing_gh_when_pr_create_exec_fails(
    monkeypatch,
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo_root"
    repo_root.mkdir(parents=True)
    target_repo = tmp_path / "target_repo"
    target_repo.mkdir(parents=True)
    ticket_path = _make_ticket(target_repo, bucket="2 - ready", fingerprint="ghghghghghghghgh")
    impl_run_dir = repo_root / "runs" / "impl" / "0"
    impl_run_dir.mkdir(parents=True, exist_ok=True)
    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir(parents=True, exist_ok=True)

    def _fake_run_once(_cfg, _request):
        _write_json(
            impl_run_dir / "workspace_ref.json",
            {"workspace_dir": str(workspace_dir)},
        )
        return SimpleNamespace(run_dir=impl_run_dir, exit_code=0, report_validation_errors=[])

    monkeypatch.setattr("usertest_implement.commands.run.run_once", _fake_run_once)
    monkeypatch.setattr(
        "usertest_implement.commands.run._maintenance_profile_is_eligible",
        lambda **_: False,
    )
    monkeypatch.setattr(
        "usertest_implement.commands.run._git_head_sha",
        lambda _workspace_dir: "abc123",
    )
    monkeypatch.setattr(
        "usertest_implement.commands.run.finalize_commit",
        lambda **_: {"commit_performed": True, "branch": "backlog/test", "head_commit": "abc123"},
    )
    monkeypatch.setattr(
        "usertest_implement.commands.run.finalize_push",
        lambda **_: {"pushed": True, "remote_name": "origin", "remote_url": "https://example.invalid/repo.git"},
    )

    def _fake_run_gh_text(*, cwd: Path, argv: list[str]) -> str:
        assert cwd == workspace_dir
        if argv[:3] == ["gh", "pr", "create"]:
            raise RuntimeError("gh not found on PATH")
        raise AssertionError(f"unexpected gh call: {argv}")

    monkeypatch.setattr("usertest_implement.commands.run._run_gh_text", _fake_run_gh_text)

    args = argparse.Namespace(
        repo_root=repo_root,
        settings=None,
        settings_profile=None,
        repo=str(target_repo),
        ref=None,
        agent="codex",
        model=None,
        policy="write",
        persona_id="thoughtful_maintainer",
        mission_id="implement_maintenance_backlog_ticket_v1",
        implementation_review_agent="claude",
        implementation_review_model="review-model",
        seed=0,
        agent_config_override=[],
        keep_workspace=False,
        exec_backend="local",
        exec_use_host_agent_login=True,
        exec_use_target_sandbox_cli_install=False,
        exec_docker_profile=None,
        exec_keep_container=True,
        exec_cache="warm",
        exec_cache_dir=None,
        maintenance_venv_cache=True,
        dry_run=False,
        verification_commands=[],
        verification_timeout_seconds=None,
        skip_verify=False,
        verify_reuse="auto",
        ci_timeout_seconds=60.0,
        skip_ci_wait=True,
        draft_pr_on_ci_failure=True,
        commit=True,
        branch=None,
        commit_message=None,
        git_user_name=None,
        git_user_email=None,
        push=True,
        remote_name="origin",
        remote_url=None,
        force_push=False,
        base_branch="dev",
        pr=True,
        move_on_start=False,
        move_on_commit=True,
        ledger=Path(".agents/state/backlog_implement_actions.yaml"),
        ticket_path=ticket_path,
        tickets_export=None,
        fingerprint=None,
        _settings_info=None,
    )

    cfg = object()
    selected = SelectedTicket(
        fingerprint="ghghghghghghghgh",
        title="Ticket",
        export_kind="implementation",
        stage="ready_for_ticket",
        owner_root=target_repo,
        idea_path=ticket_path,
        ticket_markdown=ticket_path.read_text(encoding="utf-8"),
        tickets_export_path=None,
        export_index=None,
    )

    exit_code = _run_selected_ticket(
        args=args,
        repo_root=repo_root,
        cfg=cfg,
        selected=selected,
    )

    assert exit_code == 5
    pr_ref = _read_json(impl_run_dir / "pr_ref.json")
    assert isinstance(pr_ref, dict)
    assert pr_ref["created"] is False
    assert pr_ref["error"] == "gh not found on PATH"


def test_review_run_refuses_when_ticket_not_in_for_review(monkeypatch, tmp_path: Path) -> None:
    repo_root = tmp_path / "repo_root"
    repo_root.mkdir(parents=True)
    owner_root = repo_root
    owner_root.mkdir(parents=True, exist_ok=True)
    ticket_path = _make_ticket(owner_root, bucket="2 - ready", fingerprint="beadbeadbeadbead")
    ledger_path = repo_root / ".agents" / "state" / "backlog_implement_actions.yaml"
    impl_run_dir = repo_root / "runs" / "impl" / "1"
    _write_json(
        impl_run_dir / "handoff_summary.json",
        {
            "schema_version": 1,
            "pr_created": True,
            "pr_url": "https://example.invalid/pr/56",
            "ci_conclusion": "success",
        },
    )
    _write_json(
        impl_run_dir / "pr_ref.json",
        {"created": True, "url": "https://example.invalid/pr/56"},
    )
    _write_json(impl_run_dir / "ci_gate.json", {"passed": True})
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    ledger_path.write_text(
        "schema_version: 1\nupdated_at: null\nactions:\n"
        "  beadbeadbeadbead:\n"
        "    fingerprint: beadbeadbeadbead\n"
        f"    last_run_dir: {json.dumps(str(impl_run_dir))}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "usertest_implement.commands.review._load_runner_config",
        lambda _repo_root: object(),
    )

    try:
        _cmd_review_run(
            _review_run_args(
                repo_root=repo_root,
                owner_root=owner_root,
                ticket_path=ticket_path,
                ledger=ledger_path,
            )
        )
    except SystemExit as exc:
        assert "not in 4 - for_review" in str(exc)
    else:
        raise AssertionError("Expected review run to refuse non-for_review tickets")


def test_review_run_accepts_bound_adopted_awaiting_review_ticket(
    monkeypatch,
    tmp_path: Path,
    capsys,
) -> None:
    repo_root = tmp_path / "repo_root"
    owner_root = repo_root
    owner_root.mkdir(parents=True, exist_ok=True)
    fingerprint = "ad0ptedad0pted00"
    ticket_path = _make_ticket(
        owner_root,
        bucket="2 - ready",
        fingerprint=fingerprint,
    )
    original_ticket_bytes = ticket_path.read_bytes()
    ledger_path = repo_root / ".agents" / "state" / "backlog_implement_actions.yaml"
    impl_run_dir = repo_root / "runs" / "adoptions" / "1"
    pr_url = "https://example.invalid/pr/58"
    _write_json(
        impl_run_dir / "handoff_summary.json",
        {
            "schema_version": 1,
            "handoff_mode": "adopt_existing_pr",
            "final_status": "success",
            "pr_adopted": True,
            "pr_url": pr_url,
            "review_required": True,
        },
    )
    _write_json(
        impl_run_dir / "pr_ref.json",
        {"existing_pr": True, "adopted": True, "url": pr_url},
    )
    _write_bound_ticket_ref(
        impl_run_dir,
        fingerprint=fingerprint,
        owner_root=owner_root,
        ticket_path=ticket_path,
    )
    _write_adopted_review_state(
        run_dir=impl_run_dir,
        ledger_path=ledger_path,
        owner_root=owner_root,
        ticket_path=ticket_path,
        fingerprint=fingerprint,
    )
    monkeypatch.setattr(
        "usertest_implement.commands.review._load_runner_config",
        lambda _repo_root: object(),
    )
    monkeypatch.setattr(
        "usertest_implement.commands.review._collect_pr_review_context",
        lambda **_: {
            "pr": {
                "number": 58,
                "url": pr_url,
                "title": "PR",
                "state": "OPEN",
                "isDraft": True,
                "mergeable": "MERGEABLE",
                "headRefName": "backlog/review",
                "headRefOid": "a" * 40,
                "baseRefName": "dev",
            },
            "checks": [{"name": "CI", "state": "PENDING"}],
            "ci_status": "pending",
            "ci_conclusion": None,
            "changed_files": [],
            "diff_excerpt": "",
            "diff_truncated": False,
        },
    )
    args = _review_run_args(
        repo_root=repo_root,
        owner_root=owner_root,
        ticket_path=ticket_path,
        ledger=ledger_path,
    )
    args.dry_run = True

    assert _cmd_review_run(args) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["merge_gate_ready_now"] is False
    assert payload["merge_gate"]["is_draft"] is True
    assert ticket_path.read_bytes() == original_ticket_bytes
    for_review_dir = owner_root / ".agents" / "plans" / "4 - for_review"
    assert not for_review_dir.exists() or not list(for_review_dir.glob("*.md"))


def test_review_run_rejects_inconsistent_adopted_lifecycle(
    monkeypatch,
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo_root"
    repo_root.mkdir(parents=True)
    ticket_path = _make_ticket(
        repo_root,
        bucket="2 - ready",
        fingerprint="badad0ptbadad0pt",
    )
    ledger_path = repo_root / ".agents" / "state" / "backlog_implement_actions.yaml"
    impl_run_dir = repo_root / "runs" / "adoptions" / "bad"
    _write_adopted_review_state(
        run_dir=impl_run_dir,
        ledger_path=ledger_path,
        owner_root=repo_root,
        ticket_path=ticket_path,
        fingerprint="badad0ptbadad0pt",
        lifecycle_state="implemented",
    )
    monkeypatch.setattr(
        "usertest_implement.commands.review._load_runner_config",
        lambda _repo_root: object(),
    )

    with pytest.raises(SystemExit, match="resume-state lifecycle is not one of awaiting_review"):
        _cmd_review_run(
            _review_run_args(
                repo_root=repo_root,
                owner_root=repo_root,
                ticket_path=ticket_path,
                ledger=ledger_path,
            )
        )


def test_review_run_allows_review_before_pr_gate_is_green(
    monkeypatch,
    tmp_path: Path,
    capsys,
) -> None:
    repo_root = tmp_path / "repo_root"
    owner_root = repo_root
    owner_root.mkdir(parents=True, exist_ok=True)
    ticket_path = _make_ticket(owner_root, bucket="4 - for_review", fingerprint="beadbeadbeadbead")
    ledger_path = repo_root / ".agents" / "state" / "backlog_implement_actions.yaml"
    impl_run_dir = repo_root / "runs" / "impl" / "2"
    _write_json(
        impl_run_dir / "handoff_summary.json",
        {
            "schema_version": 1,
            "pr_created": True,
            "pr_url": "https://example.invalid/pr/57",
            "ci_conclusion": None,
        },
    )
    _write_json(
        impl_run_dir / "pr_ref.json",
        {"created": True, "url": "https://example.invalid/pr/57"},
    )
    _write_bound_ticket_ref(
        impl_run_dir,
        fingerprint="beadbeadbeadbead",
        owner_root=owner_root,
        ticket_path=ticket_path,
    )
    _write_json(impl_run_dir / "ci_gate.json", {"passed": True})
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    ledger_path.write_text(
        "schema_version: 1\nupdated_at: null\nactions:\n"
        "  beadbeadbeadbead:\n"
        "    fingerprint: beadbeadbeadbead\n"
        f"    last_run_dir: {json.dumps(str(impl_run_dir))}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "usertest_implement.commands.review._load_runner_config",
        lambda _repo_root: object(),
    )

    monkeypatch.setattr(
        "usertest_implement.commands.review._collect_pr_review_context",
        lambda **_: {
            "pr": {
                "number": 57,
                "url": "https://example.invalid/pr/57",
                "title": "PR",
                "state": "OPEN",
                "isDraft": False,
                "mergeable": "UNKNOWN",
                "headRefName": "backlog/review",
                "headRefOid": "b" * 40,
                "baseRefName": "dev",
            },
            "checks": [{"name": "CI", "state": "PENDING"}],
            "ci_status": "pending",
            "ci_conclusion": None,
            "changed_files": [],
            "diff_excerpt": "",
            "diff_truncated": False,
        },
    )

    args = _review_run_args(
        repo_root=repo_root,
        owner_root=owner_root,
        ticket_path=ticket_path,
        ledger=ledger_path,
    )
    args.dry_run = True
    assert _cmd_review_run(args) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["merge_gate_ready_now"] is False
    assert payload["merge_gate"]["ci_status"] == "pending"


def test_wait_for_ci_success_polls_view_until_completed_success(
    monkeypatch,
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True)
    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir(parents=True)
    monotonic_values = iter([0.0, 0.0, 1.0, 2.0, 3.0, 4.0])
    view_calls = {"count": 0}

    def _fake_run(
        argv,
        cwd=None,
        capture_output=None,
        text=None,
        encoding=None,
        errors=None,
        check=None,
    ):
        assert cwd == str(workspace_dir)
        assert text is True
        assert encoding == "utf-8"
        assert errors == "replace"
        if argv[:3] == ["gh", "run", "list"]:
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps(
                    [
                        {
                            "databaseId": 123,
                            "headSha": "abc123",
                            "event": "push",
                            "status": "queued",
                            "conclusion": "",
                            "createdAt": "2026-03-14T20:00:00Z",
                            "url": "https://example.invalid/runs/123",
                        }
                    ]
                ),
                stderr="",
            )
        if argv[:3] == ["gh", "run", "view"]:
            view_calls["count"] += 1
            payload = (
                {
                    "status": "in_progress",
                    "conclusion": "",
                    "url": "https://example.invalid/runs/123",
                }
                if view_calls["count"] == 1
                else {
                    "status": "completed",
                    "conclusion": "success",
                    "url": "https://example.invalid/runs/123",
                }
            )
            return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")
        raise AssertionError(f"unexpected subprocess call: {argv}")

    monkeypatch.setattr("usertest_implement.review_context.subprocess.run", _fake_run)
    monkeypatch.setattr("usertest_implement.ci.time.sleep", lambda _seconds: None)
    monkeypatch.setattr(
        "usertest_implement.ci.time.monotonic",
        lambda: next(monotonic_values),
    )

    summary = _wait_for_ci_success(
        run_dir=run_dir,
        workspace_dir=workspace_dir,
        branch="backlog/test",
        head_sha="abc123",
        workflow="CI",
        timeout_seconds=60.0,
    )

    assert summary["run_id"] == 123
    assert summary["status"] == "completed"
    assert summary["conclusion"] == "success"
    assert summary["passed"] is True
    ci_gate = _read_json(run_dir / "ci_gate.json")
    assert isinstance(ci_gate, dict)
    assert ci_gate["finished_at_utc"] is not None


def test_run_gh_text_returns_empty_string_when_stdout_missing(monkeypatch, tmp_path: Path) -> None:
    def _fake_subprocess_run(
        argv,
        cwd=None,
        capture_output=None,
        text=None,
        encoding=None,
        errors=None,
        check=None,
    ):
        assert text is True
        assert encoding == "utf-8"
        assert errors == "replace"
        return SimpleNamespace(returncode=0, stdout=None, stderr=None)

    monkeypatch.setattr("usertest_implement.review_context.subprocess.run", _fake_subprocess_run)

    assert _run_gh_text(cwd=tmp_path, argv=["gh", "pr", "diff", "123"]) == ""


def test_run_gh_text_reports_missing_gh(monkeypatch, tmp_path: Path) -> None:
    def _fake_subprocess_run(*_args, **_kwargs):
        raise FileNotFoundError("gh")

    monkeypatch.setattr("usertest_implement.review_context.subprocess.run", _fake_subprocess_run)

    with pytest.raises(RuntimeError, match="gh not found on PATH"):
        _run_gh_text(cwd=tmp_path, argv=["gh", "pr", "diff", "123"])


def test_run_gh_json_accepts_missing_stdout_as_null(monkeypatch, tmp_path: Path) -> None:
    def _fake_subprocess_run(
        argv,
        cwd=None,
        capture_output=None,
        text=None,
        encoding=None,
        errors=None,
        check=None,
    ):
        assert text is True
        assert encoding == "utf-8"
        assert errors == "replace"
        return SimpleNamespace(returncode=0, stdout=None, stderr=None)

    monkeypatch.setattr("usertest_implement.review_context.subprocess.run", _fake_subprocess_run)

    assert _run_gh_json(cwd=tmp_path, argv=["gh", "pr", "view", "123", "--json", "number"]) is None


def test_collect_pr_review_context_handles_empty_diff(monkeypatch, tmp_path: Path) -> None:
    calls = {"text": 0}

    def _fake_gh_json(*, cwd: Path, argv: list[str]):
        if "checks" in argv:
            return [{"name": "CI", "state": "SUCCESS"}]
        return {
            "number": 123,
            "url": "https://example.invalid/pr/123",
            "title": "PR",
            "state": "OPEN",
            "isDraft": False,
            "headRefName": "branch",
            "baseRefName": "dev",
            "mergeable": "MERGEABLE",
        }

    def _fake_gh_text(*, cwd: Path, argv: list[str]):
        calls["text"] += 1
        if "--name-only" in argv:
            return "apps/usertest_implement/src/usertest_implement/cli.py\n"
        return ""

    monkeypatch.setattr("usertest_implement.review_context._run_gh_json", _fake_gh_json)
    monkeypatch.setattr("usertest_implement.review_context._run_gh_text", _fake_gh_text)

    context = _collect_pr_review_context(
        workspace_dir=tmp_path,
        pr_url="https://example.invalid/pr/123",
    )
    assert context["ci_conclusion"] == "success"
    assert context["diff_excerpt"] == ""
    assert context["diff_truncated"] is False
    assert calls["text"] == 2
