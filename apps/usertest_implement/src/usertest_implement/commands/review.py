# ruff: noqa: E501,F401,F403,F405
from __future__ import annotations

from dataclasses import replace
from hashlib import sha256

from backlog_repo import extract_outcome_markdown, reconcile_outcome_records
from reporter import validate_report
from runner_core.artifacts import _extract_json_object_with_receipt
from runner_core.codex_execpolicy import (
    revalidate_controlled_codex_execpolicy_receipt_for_expected_sandbox,
)

from usertest_implement.implementation_provenance import validate_verified_implementation_head
from usertest_implement.outcome_evidence import (
    expected_ticket_identity,
    validate_bound_runner_verification,
    validate_runner_ticket_ref,
)
from usertest_implement.outcome_progression import (
    OutcomeContractNotExecutable,
    OutcomeRoleDidNotPass,
    progress_post_merge_outcome,
    verify_premerge_original_scenario,
)
from usertest_implement.resume_state import (
    LIFECYCLE_AWAITING_REVIEW,
    LIFECYCLE_REVIEW_BLOCKED,
    LIFECYCLE_REVIEW_CHANGES_REQUESTED,
    RESUME_STATE_ARTIFACT_NAME,
    implementation_author_continuity,
    write_ticket_resume_state,
)
from usertest_implement.review_context import (
    _attach_deterministic_plan_scope,
    _build_final_review_summary,
    _build_pr_review_body,
    _build_review_append_prompt,
    _coerce_pr_url,
    _collect_merged_pr_provenance,
    _collect_pr_review_context,
    _current_merge_gate_from_pr_context,
    _extract_agent_review_summary,
    _load_ledger_entry,
    _review_findings_from_report,
    _run_gh_text,
    _submit_pr_review,
)
from usertest_implement.selection import (
    _select_review_ticket,
    _selected_ticket_provenance,
)
from usertest_implement.shared import *
from usertest_implement.tickets import _PLAN_BUCKETS, repair_ticket_newline_expansion

_LEGACY_READ_ONLY_EXEC_POLICY_CASCADE = frozenset(
    {
        "codex_execpolicy_auth_verification_status_invalid",
        "codex_execpolicy_chatgpt_subscription_auth_verified_not_verified",
        "codex_execpolicy_chatgpt_subscription_activation_probe_verified_not_verified",
        "codex_execpolicy_controlled_execution_mode_verified_not_verified",
        "codex_execpolicy_activation_probe_invalid",
        "codex_execpolicy_activation_windows_sandbox_mode_invalid",
    }
)
_RUNNER_ADDED_REPORT_EXTENSION_KEYS = frozenset(
    {
        "verification",
        "python_toolchain_capability",
        "shell_capability",
    }
)


def _has_explicit_passing_ci(pr_context: dict[str, Any]) -> bool:
    checks_raw = pr_context.get("checks")
    checks = checks_raw if isinstance(checks_raw, list) else []
    return any(
        isinstance(check, dict)
        and str(check.get("state") or "").strip().lower() == "success"
        for check in checks
    )


def _require_explicit_passing_ci(pr_context: dict[str, Any]) -> None:
    if not _has_explicit_passing_ci(pr_context):
        raise SystemExit(
            "Refusing to merge because the current PR has no explicitly passing CI check."
        )


def _build_merge_outcome_record(
    *,
    selected: SelectedTicket,
    pr_url: str,
    pr_context: dict[str, Any],
    merge_provenance: dict[str, str],
    review_run_dir: Path,
    implementation_run_dir: Path | None = None,
) -> dict[str, Any]:
    """Build a tests-verified outcome without claiming runtime resolution."""

    metadata = parse_ticket_markdown_metadata(selected.ticket_markdown)
    fingerprint = selected.fingerprint
    case_id, plan_revision_id = expected_ticket_identity(
        fingerprint=fingerprint,
        case_id=metadata.get("case_id"),
        plan_revision_id=metadata.get("plan_revision_id"),
    )
    requires_live_raw = metadata.get("requires_live_verification")
    if isinstance(requires_live_raw, str):
        normalized_requires_live = requires_live_raw.strip().lower()
        if normalized_requires_live not in {"true", "false"}:
            raise ValueError(
                "Requires live verification metadata must be `true` or `false`"
            )
        requires_live = normalized_requires_live == "true"
    elif case_id.startswith("legacy-case:"):
        # Legacy tickets did not declare the boundary. Fail closed: live proof
        # remains required until the case is explicitly classified.
        requires_live = True
    else:
        raise ValueError("Case-aware ticket is missing Requires live verification metadata")

    target_branch = merge_provenance.get("target_branch")
    merged_commit = merge_provenance.get("merged_commit")
    if not isinstance(target_branch, str) or not target_branch.strip():
        raise ValueError("Merged PR provenance is missing target_branch")
    if not isinstance(merged_commit, str) or not merged_commit.strip():
        raise ValueError("Merged PR provenance is missing merged_commit")

    checks_raw = pr_context.get("checks")
    checks = [item for item in checks_raw if isinstance(item, dict)] if isinstance(checks_raw, list) else []
    ci_evidence: list[dict[str, str]] = []
    for check in checks:
        bucket = str(check.get("bucket") or "").strip().lower()
        state = str(check.get("state") or "").strip().lower()
        if state == "success":
            result = "passed"
        elif bucket == "skipping" or state in {"skipped", "neutral"}:
            result = "skipped"
        else:
            continue
        reference = str(check.get("link") or pr_url).strip()
        name = str(check.get("name") or "CI check").strip()
        ci_evidence.append(
            {"kind": "ci", "reference": reference, "result": result, "name": name}
        )
    if not any(item["result"] == "passed" for item in ci_evidence):
        raise ValueError("PR context does not contain an explicitly passing CI check")

    selected_provenance: dict[str, Any] | None = None
    implementation_provenance: dict[str, Any] | None = None
    test_evidence: list[dict[str, Any]] = []
    if implementation_run_dir is not None:
        selected_provenance = _selected_ticket_provenance(
            selected,
            require_local_plan=True,
        )
        case_id = str(selected_provenance["case_id"])
        plan_revision_id = str(selected_provenance["plan_revision_id"])
        validate_runner_ticket_ref(
            run_dir=implementation_run_dir,
            fingerprint=fingerprint,
            case_id=case_id,
            plan_revision_id=plan_revision_id,
            owner_root=selected.owner_root,
            expected_ticket_provenance=selected_provenance,
        )
        if selected_provenance.get("target_contract") is not None:
            implementation_provenance = validate_verified_implementation_head(
                run_dir=implementation_run_dir
            )
        verification_path = implementation_run_dir / "verification.json"
        if (
            verification_path.exists()
            and selected_provenance.get("verification_contract") is not None
        ):
            receipt = validate_bound_runner_verification(
                run_dir=implementation_run_dir,
                fingerprint=fingerprint,
                case_id=case_id,
                plan_revision_id=plan_revision_id,
                evidence_kind="test",
                owner_root=selected.owner_root,
                expected_ticket_provenance=selected_provenance,
            )
            test_evidence.append(
                {
                    "kind": "runner_verification",
                    "reference": str(verification_path),
                    "result": "passed",
                    "runner_receipt": receipt,
                    "commands": receipt["commands"],
                }
            )

    outcome_state = "tests_verified" if test_evidence else "implemented"

    remaining_risks = ["Original failure scenario has not been replayed after merge"]
    if not test_evidence:
        remaining_risks.append(
            "No retained runner verification artifact proves that tests passed"
        )
    if requires_live:
        remaining_risks.append("Live runtime verification is still required")
    if case_id.startswith("legacy-case:"):
        remaining_risks.append("Legacy ticket did not carry a canonical case identity")

    return validate_outcome_record(
        {
            "schema_version": 1,
            "case_id": case_id,
            "plan_revision_id": plan_revision_id,
            "state": outcome_state,
            "recorded_at": _utc_now_z(),
            "requires_live_verification": requires_live,
            "target_branch": target_branch.strip(),
            "merged_commit": merged_commit.strip(),
            "pr_url": pr_url,
            "test_evidence": test_evidence,
            "ci_evidence": ci_evidence,
            "original_scenario_evidence": [],
            "live_evidence": [],
            "mitigation_evidence": [],
            "remaining_risks": remaining_risks,
            "recurrence_check": {"status": "not_run"},
            "review_run_dir": str(review_run_dir),
            "legacy_identity": case_id.startswith("legacy-case:"),
            "ticket_provenance": (
                {
                    **{
                        key: selected_provenance[key]
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
                    },
                    **(
                        {
                            "verified_implementation_head": implementation_provenance[
                                "verified_implementation_head"
                            ]
                        }
                        if implementation_provenance is not None
                        else {}
                    ),
                }
                if selected_provenance is not None
                else None
            ),
        }
    )


def _reconcile_merge_outcome(
    *,
    selected: SelectedTicket,
    ledger_entry: dict[str, Any],
    proposed: dict[str, Any],
) -> dict[str, Any]:
    """Preserve an already-advanced durable outcome during merge retries."""

    ticket_outcome = extract_outcome_markdown(selected.ticket_markdown)
    ledger_outcome_raw = ledger_entry.get("outcome")
    ledger_outcome = (
        validate_outcome_record(ledger_outcome_raw)
        if isinstance(ledger_outcome_raw, dict)
        else None
    )
    if (
        ticket_outcome is not None
        and ledger_outcome is not None
        and ticket_outcome != ledger_outcome
    ):
        raise ValueError(
            "Ticket and ledger outcomes disagree; refusing merge finalization for "
            f"{selected.fingerprint!r}"
        )
    existing = ticket_outcome or ledger_outcome
    if existing is None:
        return proposed
    return reconcile_outcome_records(existing, proposed)


def _selected_outcome_identity(selected: SelectedTicket) -> tuple[str, str]:
    provenance = _selected_ticket_provenance(selected, require_local_plan=True)
    return str(provenance["case_id"]), str(provenance["plan_revision_id"])


def _preflight_existing_outcome_stores(
    *,
    selected: SelectedTicket,
    ledger_entry: dict[str, Any],
) -> None:
    case_id, plan_revision_id = _selected_outcome_identity(selected)
    ticket_outcome = extract_outcome_markdown(selected.ticket_markdown)
    ledger_raw = ledger_entry.get("outcome")
    ledger_outcome = validate_outcome_record(ledger_raw) if isinstance(ledger_raw, dict) else None
    if ticket_outcome is not None and ledger_outcome is not None and ticket_outcome != ledger_outcome:
        raise ValueError(
            "Ticket and ledger outcomes disagree; refusing merge before external mutation"
        )
    for label, outcome in (("ticket", ticket_outcome), ("ledger", ledger_outcome)):
        if outcome is None:
            continue
        if outcome.get("outcome_scope") != "case":
            raise ValueError(f"{label} outcome is not a canonical case outcome")
        if outcome.get("case_id") != case_id:
            raise ValueError(
                f"{label} outcome case identity mismatch: "
                f"expected={case_id!r} observed={outcome.get('case_id')!r}"
            )
        if outcome.get("plan_revision_id") != plan_revision_id:
            raise ValueError(
                f"{label} outcome plan identity mismatch: "
                f"expected={plan_revision_id!r} "
                f"observed={outcome.get('plan_revision_id')!r}"
            )


def _require_review_artifact_bindings(
    *,
    selected: SelectedTicket,
    ledger_entry: dict[str, Any],
    review_run_dir: Path,
    review_summary: dict[str, Any],
    pr_url: str,
) -> Path:
    selected_provenance = _selected_ticket_provenance(
        selected,
        require_local_plan=True,
    )
    case_id = str(selected_provenance["case_id"])
    plan_revision_id = str(selected_provenance["plan_revision_id"])
    if review_summary.get("ticket_fingerprint") != selected.fingerprint:
        raise ValueError("Review summary fingerprint does not match the selected ticket")
    expected_review_provenance = {
        key: selected_provenance[key]
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
    if review_summary.get("ticket_provenance") != expected_review_provenance:
        raise ValueError("Review summary ticket provenance is stale or cross-plan")
    summary_run_dir = review_summary.get("run_dir")
    if not isinstance(summary_run_dir, str) or Path(summary_run_dir).resolve() != review_run_dir.resolve():
        raise ValueError("Review summary run_dir does not match the selected review run")

    review_ref = _read_json(review_run_dir / "review_ref.json")
    if not isinstance(review_ref, dict) or review_ref.get("schema_version") != 2:
        raise ValueError("Review run is missing a valid review_ref.json")
    if review_ref.get("ticket_fingerprint") != selected.fingerprint:
        raise ValueError("review_ref.json fingerprint does not match the selected ticket")
    if review_ref.get("pr_url") != pr_url:
        raise ValueError("review_ref.json PR URL does not match review_summary.json")
    if review_ref.get("ticket_provenance") != expected_review_provenance:
        raise ValueError("review_ref.json ticket provenance is stale or cross-plan")
    review_ticket_path = review_ref.get("ticket_path")
    if selected.idea_path is None or not isinstance(review_ticket_path, str):
        raise ValueError("Review reference is missing the selected ticket path")
    recorded_ticket_path = Path(review_ticket_path).resolve()
    selected_ticket_path = selected.idea_path.resolve()
    if recorded_ticket_path != selected_ticket_path and not _is_same_queue_ticket_move(
        owner_root=selected.owner_root,
        recorded_path=recorded_ticket_path,
        selected_path=selected_ticket_path,
    ):
        raise ValueError("review_ref.json ticket path does not match the selected ticket")

    run_dir_raw = review_ref.get("implementation_run_dir")
    ledger_run_dir_raw = ledger_entry.get("last_run_dir")
    if not isinstance(run_dir_raw, str) or not run_dir_raw.strip():
        raise ValueError("review_ref.json is missing implementation_run_dir")
    if not isinstance(ledger_run_dir_raw, str) or not ledger_run_dir_raw.strip():
        raise ValueError("Ledger is missing last_run_dir for reviewed implementation")
    implementation_run_dir = Path(run_dir_raw).resolve()
    if implementation_run_dir != Path(ledger_run_dir_raw).resolve():
        raise ValueError("Review implementation run does not match ledger last_run_dir")

    implementation_ticket_ref_path = implementation_run_dir / "ticket_ref.json"
    expected_ticket_ref_hash = sha256(
        implementation_ticket_ref_path.read_bytes()
    ).hexdigest()
    if review_ref.get("implementation_ticket_ref_sha256") != expected_ticket_ref_hash:
        raise ValueError("review_ref.json implementation ticket_ref hash mismatch")
    if review_summary.get("implementation_ticket_ref_sha256") != expected_ticket_ref_hash:
        raise ValueError("Review summary implementation ticket_ref hash mismatch")

    validate_runner_ticket_ref(
        run_dir=implementation_run_dir,
        fingerprint=selected.fingerprint,
        case_id=case_id,
        plan_revision_id=plan_revision_id,
        owner_root=selected.owner_root,
        expected_ticket_provenance=selected_provenance,
    )
    handoff_summary = _read_json(implementation_run_dir / "handoff_summary.json")
    pr_ref = _read_json(implementation_run_dir / "pr_ref.json")
    implementation_pr_url = _coerce_pr_url(
        handoff_summary=handoff_summary if isinstance(handoff_summary, dict) else None,
        pr_ref=pr_ref if isinstance(pr_ref, dict) else None,
    )
    if implementation_pr_url != pr_url:
        raise ValueError("Implementation run PR URL does not match the reviewed PR")
    if isinstance(handoff_summary, dict):
        handoff_pr_url = handoff_summary.get("pr_url")
        if isinstance(handoff_pr_url, str) and handoff_pr_url.strip() != pr_url:
            raise ValueError("Implementation handoff PR URL does not match the reviewed PR")
    if isinstance(pr_ref, dict):
        run_pr_url = pr_ref.get("url")
        if isinstance(run_pr_url, str) and run_pr_url.strip() != pr_url:
            raise ValueError("Implementation pr_ref URL does not match the reviewed PR")
    return implementation_run_dir


def _is_same_queue_ticket_move(
    *,
    owner_root: Path | None,
    recorded_path: Path,
    selected_path: Path,
) -> bool:
    """Accept only a bucket-only move of the same immutable ticket identity."""

    if owner_root is None:
        return False
    plans_root = (owner_root / ".agents" / "plans").resolve()
    try:
        recorded_relative = recorded_path.resolve().relative_to(plans_root)
        selected_relative = selected_path.resolve().relative_to(plans_root)
    except (OSError, ValueError):
        return False
    if len(recorded_relative.parts) != 2 or len(selected_relative.parts) != 2:
        return False
    recorded_bucket, recorded_name = recorded_relative.parts
    selected_bucket, selected_name = selected_relative.parts
    return (
        recorded_bucket in _PLAN_BUCKETS
        and selected_bucket in _PLAN_BUCKETS
        and recorded_name.casefold() == selected_name.casefold()
    )


def _require_unchanged_reviewed_head(
    *,
    fingerprint: str,
    review_summary: dict[str, Any],
    pr_meta: dict[str, Any],
) -> str:
    reviewed_head_oid = str(review_summary.get("reviewed_head_oid") or "").strip()
    if not reviewed_head_oid:
        raise SystemExit(
            f"Review summary for {fingerprint!r} is missing reviewed_head_oid; "
            "run review again before merging."
        )
    current_head_oid = str(pr_meta.get("headRefOid") or "").strip()
    if not current_head_oid:
        raise SystemExit("Current PR metadata is missing headRefOid; refusing merge.")
    if current_head_oid != reviewed_head_oid:
        raise SystemExit(
            "PR head changed after automated review; run review again before merging: "
            f"reviewed={reviewed_head_oid} current={current_head_oid}"
        )
    return reviewed_head_oid


def _review_adoption_eligibility_errors(
    *,
    selected: SelectedTicket,
    implementation_run_dir: Path,
    ledger_path: Path,
    allowed_lifecycles: frozenset[str],
) -> list[str]:
    """Explain why a pre-review ticket is not backed by an adopted handoff."""

    errors: list[str] = []
    if selected.idea_path is None or not any(
        bucket in selected.idea_path.parts
        for bucket in ("2 - ready", "3 - in_progress")
    ):
        errors.append("ticket is not in 2 - ready or 3 - in_progress")
        return errors

    run_dir = implementation_run_dir.resolve()
    ticket_path = selected.idea_path.resolve()
    ledger_entry = _load_ledger_entry(
        ledger_path=ledger_path,
        fingerprint=selected.fingerprint,
    )
    if ledger_entry.get("last_resume_lifecycle_state") not in allowed_lifecycles:
        errors.append(
            "ledger lifecycle is not one of " + ", ".join(sorted(allowed_lifecycles))
        )
    if ledger_entry.get("last_handoff_mode") != "adopt_existing_pr":
        errors.append("ledger handoff mode is not adopt_existing_pr")
    if ledger_entry.get("pr_adopted") is not True:
        errors.append("ledger does not record pr_adopted=true")
    ledger_run_dir = ledger_entry.get("last_run_dir")
    if not isinstance(ledger_run_dir, str) or Path(ledger_run_dir).resolve() != run_dir:
        errors.append("ledger last_run_dir does not match the adoption run")
    ledger_ticket_path = ledger_entry.get("idea_path")
    if (
        not isinstance(ledger_ticket_path, str)
        or Path(ledger_ticket_path).resolve() != ticket_path
    ):
        errors.append("ledger ticket path does not match the selected ticket")

    resume_state = _read_json(run_dir / RESUME_STATE_ARTIFACT_NAME)
    if not isinstance(resume_state, dict):
        errors.append("adoption run is missing ticket_resume_state.json")
    else:
        resume_ticket = resume_state.get("ticket")
        if not isinstance(resume_ticket, dict):
            errors.append("resume state is missing ticket identity")
        else:
            if resume_ticket.get("fingerprint") != selected.fingerprint:
                errors.append("resume-state fingerprint does not match the selected ticket")
            resume_ticket_path = resume_ticket.get("path")
            if (
                not isinstance(resume_ticket_path, str)
                or Path(resume_ticket_path).resolve() != ticket_path
            ):
                errors.append("resume-state ticket path does not match the selected ticket")
        if resume_state.get("lifecycle_state") not in allowed_lifecycles:
            errors.append(
                "resume-state lifecycle is not one of "
                + ", ".join(sorted(allowed_lifecycles))
            )
        resume_run_dir = resume_state.get("run_dir")
        if (
            not isinstance(resume_run_dir, str)
            or Path(resume_run_dir).resolve() != run_dir
        ):
            errors.append("resume-state run_dir does not match the adoption run")

    adoption_ref = _read_json(run_dir / "adoption_ref.json")
    if not isinstance(adoption_ref, dict):
        errors.append("adoption run is missing adoption_ref.json")
    else:
        if adoption_ref.get("kind") != "existing_pr_adoption":
            errors.append("adoption reference kind is not existing_pr_adoption")
        if adoption_ref.get("fingerprint") != selected.fingerprint:
            errors.append("adoption fingerprint does not match the selected ticket")
        adoption_run_dir = adoption_ref.get("run_dir")
        if (
            not isinstance(adoption_run_dir, str)
            or Path(adoption_run_dir).resolve() != run_dir
        ):
            errors.append("adoption run_dir does not match the recorded run")
        adoption_ticket_path = adoption_ref.get("ticket_path")
        if (
            not isinstance(adoption_ticket_path, str)
            or Path(adoption_ticket_path).resolve() != ticket_path
        ):
            errors.append("adoption ticket path does not match the selected ticket")
        flags = adoption_ref.get("flags")
        if not isinstance(flags, dict) or flags.get("pr_adopted") is not True:
            errors.append("adoption reference does not record pr_adopted=true")
        if not isinstance(flags, dict) or flags.get("ticket_mutated") is not False:
            errors.append("adoption reference does not prove ticket_mutated=false")
        ticket_sha256 = adoption_ref.get("ticket_sha256")
        if (
            not isinstance(ticket_sha256, str)
            or ticket_sha256 != sha256(selected.idea_path.read_bytes()).hexdigest()
        ):
            errors.append("selected ticket bytes no longer match the adopted ticket")

    return errors


def _require_review_eligibility(
    *,
    selected: SelectedTicket,
    implementation_run_dir: Path,
    ledger_path: Path,
    correction_requested: bool,
) -> None:
    if selected.idea_path is not None and "4 - for_review" in selected.idea_path.parts:
        return
    allowed_lifecycles = (
        frozenset(
            {
                LIFECYCLE_REVIEW_CHANGES_REQUESTED,
                LIFECYCLE_REVIEW_BLOCKED,
            }
        )
        if correction_requested
        else frozenset({LIFECYCLE_AWAITING_REVIEW})
    )
    adoption_errors = _review_adoption_eligibility_errors(
        selected=selected,
        implementation_run_dir=implementation_run_dir,
        ledger_path=ledger_path,
        allowed_lifecycles=allowed_lifecycles,
    )
    if not adoption_errors:
        return
    raise SystemExit(
        f"Ticket {selected.fingerprint!r} is not in 4 - for_review and cannot be "
        "reviewed without a valid adopted awaiting_review handoff: "
        + "; ".join(adoption_errors)
    )


def _review_correction_context(
    *,
    selected: SelectedTicket,
    implementation_run_dir: Path,
    ledger_path: Path,
    pr_url: str,
    reviewed_head_oid: str,
) -> dict[str, Any]:
    ledger_entry = _load_ledger_entry(
        ledger_path=ledger_path,
        fingerprint=selected.fingerprint,
    )
    previous_run_raw = ledger_entry.get("last_review_run_dir")
    if not isinstance(previous_run_raw, str) or not previous_run_raw.strip():
        raise SystemExit(
            "A review correction requires last_review_run_dir in the ticket ledger."
        )
    previous_run_dir = Path(previous_run_raw).resolve()
    previous_summary = _read_json(previous_run_dir / "review_summary.json")
    previous_ref = _read_json(previous_run_dir / "review_ref.json")
    if not isinstance(previous_summary, dict) or not isinstance(previous_ref, dict):
        raise SystemExit(
            "A review correction requires the prior review_summary.json and review_ref.json."
        )
    if previous_summary.get("ticket_fingerprint") != selected.fingerprint:
        raise SystemExit("Prior review fingerprint does not match the selected ticket.")
    if previous_summary.get("pr_url") != pr_url:
        raise SystemExit("Prior review PR does not match the current implementation PR.")
    previous_head_oid = str(previous_summary.get("reviewed_head_oid") or "").strip()
    if not previous_head_oid:
        raise SystemExit("Prior review is missing its reviewed PR head.")
    prior_implementation_run = previous_ref.get("implementation_run_dir")
    if not isinstance(prior_implementation_run, str) or not prior_implementation_run.strip():
        raise SystemExit("Prior review is missing its implementation run binding.")

    continuity = implementation_author_continuity(previous_run_dir)
    if continuity.get("agent") != "codex" or continuity.get("exact_session_available") is not True:
        raise SystemExit(
            "The prior reviewer has no exact resumable Codex session; refusing to call a "
            "fresh reviewer a same-author correction."
        )
    session_id = continuity.get("session_id")
    if not isinstance(session_id, str) or not session_id.strip():
        raise SystemExit("Prior review is missing its exact Codex session id.")

    def _workspace_head(candidate: Path) -> str | None:
        try:
            proc = subprocess.run(
                ["git", "-C", str(candidate), "rev-parse", "HEAD"],
                capture_output=True,
                check=False,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except OSError:
            return None
        if proc.returncode != 0:
            return None
        value = (proc.stdout or "").strip()
        return value if value else None

    resume_workspace_dir: Path | None = None
    resume_workspace_source = "fresh_checkout"
    workspace_ref = _read_json(previous_run_dir / "workspace_ref.json")
    if isinstance(workspace_ref, dict):
        workspace_raw = workspace_ref.get("workspace_dir")
        if isinstance(workspace_raw, str) and workspace_raw.strip():
            candidate = Path(workspace_raw).resolve()
            if _workspace_head(candidate) == reviewed_head_oid:
                resume_workspace_dir = candidate
                resume_workspace_source = "prior_review"

    if resume_workspace_dir is None:
        current_workspace_ref = _read_json(implementation_run_dir / "workspace_ref.json")
        if isinstance(current_workspace_ref, dict):
            current_workspace_raw = current_workspace_ref.get("workspace_dir")
            if isinstance(current_workspace_raw, str) and current_workspace_raw.strip():
                candidate = Path(current_workspace_raw).resolve()
                if _workspace_head(candidate) == reviewed_head_oid:
                    resume_workspace_dir = candidate
                    resume_workspace_source = "current_verified_implementation"

    target_ref = _read_json(previous_run_dir / "target_ref.json")
    prior_model = target_ref.get("model") if isinstance(target_ref, dict) else None
    return {
        "previous_review_run_dir": previous_run_dir,
        "previous_summary": previous_summary,
        "author_continuity": continuity,
        "codex_resume_session_id": session_id,
        "codex_resume_usage_source_run_dir": previous_run_dir,
        "resume_workspace_dir": resume_workspace_dir,
        "resume_workspace_source": resume_workspace_source,
        "prior_model": prior_model if isinstance(prior_model, str) else None,
        "previous_reviewed_head_oid": previous_head_oid,
        "current_reviewed_head_oid": reviewed_head_oid,
        "previous_implementation_run_dir": Path(prior_implementation_run).resolve(),
        "current_implementation_run_dir": implementation_run_dir.resolve(),
    }


def _build_review_correction_prompt(
    *,
    corrections: list[str],
    correction_context: dict[str, Any],
    pr_context: dict[str, Any],
) -> str:
    prior_summary = correction_context["previous_summary"]
    correction_lines = "\n".join(
        f"{index}. {correction}" for index, correction in enumerate(corrections, start=1)
    )
    current_pr = json.dumps(pr_context.get("pr", {}), indent=2, ensure_ascii=False)
    current_checks = json.dumps(
        pr_context.get("checks", []), indent=2, ensure_ascii=False
    )
    return (
        "This is a focused correction turn for your immediately preceding causal PR review. "
        "Preserve every valid observation from that review; do not restart or discard the "
        "frontier merely because one or more findings were wrong or incomplete. Re-inspect "
        "the exact current verified head and verify each correction below against repository and "
        "artifact evidence. Then return one complete replacement `task_run_v1` report with "
        "a fully revised `extensions.review_summary`; do not return a patch or commentary.\n\n"
        "Do not modify repository files, merge the PR, invoke Docker, or require prohibited "
        "live Docker execution. Keep code/test confidence distinct from live-runtime proof. "
        "A stale planned path is not authoritative when retained evidence proves a "
        "dependency-correct relocation. Findings must identify a real correctness or causal "
        "gap, not merely disagreement with wording or layout.\n\n"
        "The implementation run or PR head may have advanced because the implementation author "
        "acted on the prior review. Treat that as the expected correction loop, inspect the full "
        "current head, and retain or revise each prior finding according to current evidence.\n\n"
        "## Supervisor correction findings to verify\n\n"
        f"{correction_lines}\n\n"
        "## Previous review summary\n\n"
        f"```json\n{json.dumps(prior_summary, indent=2, ensure_ascii=False)}\n```\n\n"
        "## Current PR metadata\n\n"
        f"```json\n{current_pr}\n```\n\n"
        "## Current checks\n\n"
        f"```json\n{current_checks}\n```\n"
    )


_MAX_REVIEW_SEMANTIC_CORRECTIONS = 3


def _build_review_semantic_correction_request(
    *,
    request: RunRequest,
    failed_run_dir: Path,
    reviewed_head_oid: str,
    validation_error: str,
) -> RunRequest:
    continuity = implementation_author_continuity(failed_run_dir)
    session_id = continuity.get("session_id")
    if (
        continuity.get("agent") != "codex"
        or continuity.get("exact_session_available") is not True
        or not isinstance(session_id, str)
        or not session_id.strip()
    ):
        raise SystemExit(
            "Invalid review output has no exact resumable Codex session; "
            "automatic semantic correction is unavailable."
        )
    prompt = (
        "Your immediately preceding review report was retained, but deterministic semantic "
        "validation rejected it for this exact reason:\n\n"
        f"{validation_error}\n\n"
        "Correct the inconsistency while preserving all valid evidence, findings, and bounded "
        "outcome distinctions from your preceding turn. Reinspect the current verified head if "
        "needed. Return one complete replacement `task_run_v1` JSON report with a fully revised "
        "`extensions.review_summary`; do not return a patch or commentary. Do not modify files, "
        "merge the PR, weaken a real blocking finding, or invent new evidence. A `closed` causal "
        "assessment requires an empty `remaining_causal_paths`; use `residual` when explicit paths "
        "remain, including bounded paths outside the selected mechanism.\n\n"
        f"Current verified PR head: {reviewed_head_oid}"
    )
    return replace(
        request,
        agent_append_system_prompt_file=None,
        agent_user_prompt=prompt,
        codex_resume_session_id=session_id.strip(),
        codex_resume_usage_source_run_dir=failed_run_dir,
    )


def _required_json_object(path: Path, *, label: str) -> dict[str, Any]:
    value = _read_json(path)
    if not isinstance(value, dict):
        raise SystemExit(f"{label} is missing or invalid: {path}")
    return value


def _required_json_list(path: Path, *, label: str) -> list[Any]:
    value = _read_json(path)
    if not isinstance(value, list):
        raise SystemExit(f"{label} is missing or invalid: {path}")
    return value


def _sha256_file(path: Path) -> str:
    try:
        return sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise SystemExit(f"Unable to hash retained review artifact {path}: {exc}") from exc


def _json_fence_after_heading(text: str, *, heading: str) -> dict[str, Any]:
    marker = f"## {heading}\n\n```json\n"
    start = text.find(marker)
    if start < 0:
        raise SystemExit(f"Retained correction prompt is missing {heading!r} JSON.")
    start += len(marker)
    end = text.find("\n```", start)
    if end < 0:
        raise SystemExit(f"Retained correction prompt has an unterminated {heading!r} JSON fence.")
    try:
        value = json.loads(text[start:end])
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Retained correction prompt has invalid {heading!r} JSON.") from exc
    if not isinstance(value, dict):
        raise SystemExit(f"Retained correction prompt {heading!r} must be a JSON object.")
    return value


def _parse_retained_review_correction_prompt(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise SystemExit(f"Unable to read retained review correction prompt: {path}") from exc
    start_marker = "## Supervisor correction findings to verify\n\n"
    end_marker = "\n\n## Previous review summary\n\n"
    start = text.find(start_marker)
    end = text.find(end_marker, start + len(start_marker)) if start >= 0 else -1
    if start < 0 or end < 0:
        raise SystemExit("Retained prompt is not a generated focused review correction prompt.")
    numbered = text[start + len(start_marker) : end].splitlines()
    corrections: list[str] = []
    for line in numbered:
        if not line.strip():
            continue
        match = re.fullmatch(r"(\d+)\.\s+(.+)", line)
        expected_index = len(corrections) + 1
        if match is None or int(match.group(1)) != expected_index:
            raise SystemExit("Retained correction findings are malformed or out of order.")
        corrections.append(match.group(2).strip())
    if not corrections:
        raise SystemExit("Retained correction prompt contains no supervisor findings.")
    return {
        "corrections": corrections,
        "previous_review_summary": _json_fence_after_heading(
            text,
            heading="Previous review summary",
        ),
        "pr": _json_fence_after_heading(text, heading="Current PR metadata"),
    }


def _report_without_runner_added_extensions(report: dict[str, Any]) -> dict[str, Any]:
    normalized = json.loads(json.dumps(report))
    extensions_raw = normalized.get("extensions")
    if isinstance(extensions_raw, dict):
        for key in _RUNNER_ADDED_REPORT_EXTENSION_KEYS:
            extensions_raw.pop(key, None)
        if not extensions_raw:
            normalized.pop("extensions", None)
    return normalized


def _raw_review_turn_completed(path: Path, *, session_id: str) -> bool:
    thread_ids: list[str] = []
    completed = False
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(event, dict):
                    continue
                if event.get("type") == "thread.started":
                    thread_id = event.get("thread_id")
                    if isinstance(thread_id, str):
                        thread_ids.append(thread_id)
                elif event.get("type") == "turn.completed":
                    completed = True
    except (OSError, UnicodeError):
        return False
    return bool(completed and thread_ids and set(thread_ids) == {session_id})


def _existing_completed_review_adoption(
    *,
    ledger_path: Path,
    fingerprint: str,
    source_review_run_dir: Path,
    reviewed_head_oid: str,
) -> tuple[Path, dict[str, Any]] | None:
    entry = _load_ledger_entry(ledger_path=ledger_path, fingerprint=fingerprint)
    run_raw = entry.get("last_review_run_dir")
    if not isinstance(run_raw, str) or not run_raw.strip():
        return None
    run_dir = Path(run_raw).resolve()
    adoption = _read_json(run_dir / "review_report_adoption.json")
    summary = _read_json(run_dir / "review_summary.json")
    publication = _read_json(run_dir / "pr_review_ref.json")
    if not all(isinstance(item, dict) for item in (adoption, summary, publication)):
        return None
    if Path(str(adoption.get("source_review_run_dir") or "")).resolve() != source_review_run_dir:
        return None
    if summary.get("reviewed_head_oid") != reviewed_head_oid:
        return None
    if publication.get("submitted") is not True:
        return None
    return run_dir, summary


def _validate_retained_review_for_adoption(
    *,
    repo_root: Path,
    runs_dir: Path,
    source_review_run_dir: Path,
    correction_context: dict[str, Any],
    pr_url: str,
    reviewed_head_oid: str,
    implementation_run_dir: Path,
) -> tuple[dict[str, Any], dict[str, Any]]:
    source = source_review_run_dir.resolve()
    trusted_runs = runs_dir.resolve()
    if not source.is_dir() or not source.is_relative_to(trusted_runs):
        raise SystemExit(
            "Retained review run must exist beneath the configured usertest runs directory."
        )
    for filename in ("review_summary.json", "review_ref.json"):
        if (source / filename).exists():
            raise SystemExit(
                f"Retained review run already contains finalized {filename}; refusing adoption."
            )
    prior_publication = _read_json(source / "pr_review_ref.json")
    if isinstance(prior_publication, dict) and prior_publication.get("submitted") is True:
        raise SystemExit("Retained review report was already published.")

    required_files = (
        "report.json",
        "error.json",
        "report_validation_errors.json",
        "agent_attempts.json",
        "target_ref.json",
        "prompt.txt",
        "agent_last_message.txt",
        "report.schema.json",
        "preflight.json",
        "codex_execpolicy_overlay.json",
        "raw_events.jsonl",
        "workspace_ref.json",
    )
    missing = [filename for filename in required_files if not (source / filename).is_file()]
    if missing:
        raise SystemExit("Retained review run is incomplete: " + ", ".join(missing))

    report = _required_json_object(source / "report.json", label="Retained report")
    error = _required_json_object(source / "error.json", label="Retained runner error")
    validation_errors_raw = _required_json_list(
        source / "report_validation_errors.json",
        label="Retained report validation errors",
    )
    validation_errors = [str(value) for value in validation_errors_raw]
    if (
        len(validation_errors) != len(_LEGACY_READ_ONLY_EXEC_POLICY_CASCADE)
        or set(validation_errors) != _LEGACY_READ_ONLY_EXEC_POLICY_CASCADE
    ):
        raise SystemExit(
            "Retained review has errors beyond the known read-only exec-policy cascade."
        )
    if error.get("type") != "AgentExecFailed" or int(error.get("exit_code") or 0) != 1:
        raise SystemExit("Retained runner failure is not the expected post-agent failure.")

    target_ref = _required_json_object(source / "target_ref.json", label="Retained target ref")
    session_id = str(correction_context["codex_resume_session_id"])
    if target_ref.get("agent") != "codex":
        raise SystemExit("Retained correction was not authored by Codex.")
    if target_ref.get("policy") != "inspect":
        raise SystemExit("Retained correction did not use the inspect/read-only policy.")
    if target_ref.get("mission_id") != "review_backlog_implementation_pr_v1":
        raise SystemExit("Retained correction used a different review mission.")
    if target_ref.get("requested_codex_resume_session_id") != session_id:
        raise SystemExit("Retained correction requested a different reviewer session.")
    if target_ref.get("ref") != reviewed_head_oid or target_ref.get("commit_sha") != reviewed_head_oid:
        raise SystemExit("Retained correction is bound to a different PR head.")
    auth = target_ref.get("codex_resume_auth")
    if not isinstance(auth, dict) or not all(
        (
            auth.get("auth_mode") == "host_chatgpt_subscription_login",
            auth.get("host_agent_login_required") is True,
            auth.get("api_billing_environment_disabled") is True,
        )
    ):
        raise SystemExit("Retained correction lacks host ChatGPT subscription provenance.")
    prior_model = correction_context.get("prior_model")
    if isinstance(prior_model, str) and target_ref.get("model") != prior_model:
        raise SystemExit("Retained correction model differs from its prior reviewer.")

    attempts = _required_json_object(
        source / "agent_attempts.json",
        label="Retained agent attempts",
    ).get("attempts")
    if not isinstance(attempts, list) or not attempts or not isinstance(attempts[-1], dict):
        raise SystemExit("Retained correction has no final agent attempt.")
    final_attempt = attempts[-1]
    if (
        int(final_attempt.get("exit_code") or 0) != 0
        or final_attempt.get("failure_subtype") not in {None, ""}
        or final_attempt.get("report_validation_errors") != []
        or final_attempt.get("json_syntax_repair") is not None
        or final_attempt.get("continued_session") is not True
        or final_attempt.get("agent_session_id") != session_id
    ):
        raise SystemExit("Retained correction agent attempt was not a clean same-session success.")
    argv = final_attempt.get("argv")
    if not isinstance(argv, list) or "--sandbox" not in argv:
        raise SystemExit("Retained correction attempt is missing its sandbox argument.")
    sandbox_index = argv.index("--sandbox")
    if sandbox_index + 1 >= len(argv) or argv[sandbox_index + 1] != "read-only":
        raise SystemExit("Retained correction attempt did not use read-only sandboxing.")
    if not _raw_review_turn_completed(source / "raw_events.jsonl", session_id=session_id):
        raise SystemExit("Retained correction raw events do not prove a completed same-session turn.")

    preflight = _required_json_object(source / "preflight.json", label="Retained preflight")
    shell = preflight.get("shell_capability")
    if not isinstance(shell, dict) or not all(
        (
            shell.get("state") == "available",
            shell.get("sandbox_mode") == "read-only",
            shell.get("probe_status") == "passed",
            shell.get("policy_status") == "allowed",
        )
    ):
        raise SystemExit("Retained correction preflight does not prove read-only shell access.")

    revalidation = revalidate_controlled_codex_execpolicy_receipt_for_expected_sandbox(
        source / "codex_execpolicy_overlay.json",
        expected_sandbox_mode="read-only",
    )
    if (
        revalidation.get("verified") is not True
        or revalidation.get("errors") != []
        or set(revalidation.get("source_errors") or [])
        != _LEGACY_READ_ONLY_EXEC_POLICY_CASCADE
        or revalidation.get("observed_sandbox_mode") != "read-only"
        or revalidation.get("raw_events_hash_valid") is not True
        or revalidation.get("underlying_probe_verified") is not True
        or revalidation.get("login_status_verified") is not True
        or revalidation.get("post_login_status_verified") is not True
    ):
        raise SystemExit("Retained correction subscription receipt cannot be revalidated.")

    last_message_text = (source / "agent_last_message.txt").read_text(encoding="utf-8")
    try:
        agent_report, syntax_receipt = _extract_json_object_with_receipt(last_message_text)
    except Exception as exc:  # noqa: BLE001
        raise SystemExit("Retained correction last message is not valid JSON.") from exc
    if syntax_receipt is not None:
        raise SystemExit("Retained correction unexpectedly required JSON syntax repair.")
    if _report_without_runner_added_extensions(report) != agent_report:
        raise SystemExit(
            "Retained report differs from the model output beyond documented runner extensions."
        )
    retained_schema = _required_json_object(
        source / "report.schema.json",
        label="Retained report schema",
    )
    current_schema = _required_json_object(
        repo_root / "configs" / "report_schemas" / "task_run_v1.schema.json",
        label="Current review report schema",
    )
    retained_schema_errors = validate_report(
        report,
        retained_schema,
        require_shell_capability=True,
    )
    current_schema_errors = validate_report(
        report,
        current_schema,
        require_shell_capability=True,
    )
    if retained_schema_errors or current_schema_errors:
        raise SystemExit(
            "Retained correction report is not schema-valid: "
            + "; ".join([*retained_schema_errors, *current_schema_errors])
        )
    try:
        _extract_agent_review_summary(report)
    except ValueError as exc:
        raise SystemExit(f"Retained correction review contract is invalid: {exc}") from exc

    parsed_prompt = _parse_retained_review_correction_prompt(source / "prompt.txt")
    if parsed_prompt["previous_review_summary"] != correction_context["previous_summary"]:
        raise SystemExit("Retained correction prompt embeds a different prior review summary.")
    prompt_pr = parsed_prompt["pr"]
    if prompt_pr.get("url") != pr_url or prompt_pr.get("headRefOid") != reviewed_head_oid:
        raise SystemExit("Retained correction prompt embeds a different PR or head.")
    workspace_ref = _required_json_object(
        source / "workspace_ref.json",
        label="Retained workspace ref",
    )
    prior_workspace = correction_context.get("resume_workspace_dir")
    retained_workspace_raw = workspace_ref.get("workspace_dir")
    if (
        prior_workspace is None
        or not isinstance(retained_workspace_raw, str)
        or Path(retained_workspace_raw).resolve() != Path(prior_workspace).resolve()
    ):
        raise SystemExit("Retained correction did not resume the prior reviewer workspace.")

    implementation_head = validate_verified_implementation_head(
        run_dir=implementation_run_dir
    )
    if implementation_head.get("verified_implementation_head") != reviewed_head_oid:
        raise SystemExit("Retained correction head differs from the verified implementation head.")

    retained_hashes = {
        filename: _sha256_file(source / filename) for filename in required_files
    }
    validator_paths = (
        Path(__file__).resolve(),
        Path(revalidate_controlled_codex_execpolicy_receipt_for_expected_sandbox.__code__.co_filename).resolve(),
    )
    evidence = {
        "schema_version": 1,
        "kind": "retained_same_author_review_adoption",
        "validated_at_utc": _utc_now_z(),
        "source_review_run_dir": str(source),
        "source_artifact_sha256": retained_hashes,
        "source_report_sha256": retained_hashes["report.json"],
        "source_error_codes": validation_errors,
        "supplemental_execpolicy_revalidation": revalidation,
        "review_author_session_id": session_id,
        "reviewed_head_oid": reviewed_head_oid,
        "pr_url": pr_url,
        "implementation_run_dir": str(implementation_run_dir.resolve()),
        "corrections": parsed_prompt["corrections"],
        "model_invoked": False,
        "docker_invoked": False,
        "source_failure_artifacts_modified": False,
        "validator_source_sha256": {
            str(path): _sha256_file(path) for path in validator_paths
        },
    }
    evidence["adoption_evidence_sha256"] = sha256(
        json.dumps(evidence, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    return report, evidence


def _materialize_review_adoption(
    *,
    runs_dir: Path,
    fingerprint: str,
    source_review_run_dir: Path,
    report: dict[str, Any],
    adoption_evidence: dict[str, Any],
) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    run_dir = runs_dir.resolve() / "_review_adoptions" / fingerprint / stamp
    run_dir.mkdir(parents=True, exist_ok=False)
    source = source_review_run_dir.resolve()
    for filename in (
        "target_ref.json",
        "workspace_ref.json",
        "raw_events.jsonl",
        "agent_attempts.json",
        "report.schema.json",
        "agent_last_message.txt",
        "prompt.txt",
    ):
        shutil.copy2(source / filename, run_dir / filename)
    _write_json(run_dir / "report.json", report)
    _write_json(run_dir / "review_report_adoption.json", adoption_evidence)
    return run_dir


def _run_review_for_selected_ticket(
    *,
    repo_root: Path,
    cfg: RunnerConfig,
    owner_root: Path,
    selected: SelectedTicket,
    implementation_run_dir: Path,
    ledger_path: Path,
    review_agent: str,
    review_model: str | None,
    review_policy: str,
    review_persona_id: str,
    review_mission_id: str,
    review_seed: int,
    review_agent_config_override: list[str],
    review_corrections: list[str],
    adopt_review_run: Path | None,
    keep_workspace: bool,
    exec_backend: str,
    exec_use_host_agent_login: bool,
    exec_use_target_sandbox_cli_install: bool,
    exec_docker_profile: str | None,
    exec_keep_container: bool,
    exec_cache: str,
    exec_cache_dir: Path | None,
    maintenance_venv_cache: bool,
    dry_run: bool,
) -> tuple[Path | None, dict[str, Any] | None]:
    if not implementation_run_dir.exists():
        raise SystemExit(f"Recorded implementation run dir does not exist: {implementation_run_dir}")
    _require_review_eligibility(
        selected=selected,
        implementation_run_dir=implementation_run_dir,
        ledger_path=ledger_path,
        correction_requested=bool(review_corrections) or adopt_review_run is not None,
    )
    selected_provenance = _selected_ticket_provenance(
        selected,
        require_local_plan=True,
    )
    case_id = str(selected_provenance["case_id"])
    plan_revision_id = str(selected_provenance["plan_revision_id"])
    validate_runner_ticket_ref(
        run_dir=implementation_run_dir,
        fingerprint=selected.fingerprint,
        case_id=case_id,
        plan_revision_id=plan_revision_id,
        owner_root=selected.owner_root,
        expected_ticket_provenance=selected_provenance,
    )

    handoff_summary = _read_json(implementation_run_dir / "handoff_summary.json")
    pr_ref = _read_json(implementation_run_dir / "pr_ref.json")
    ci_gate = _read_json(implementation_run_dir / "ci_gate.json")
    pr_url = _coerce_pr_url(handoff_summary=handoff_summary, pr_ref=pr_ref)
    if pr_url is None:
        raise SystemExit(
            f"Ticket {selected.fingerprint!r} does not have a PR to review "
            f"(run_dir={implementation_run_dir})."
        )
    if isinstance(handoff_summary, dict):
        handoff_pr_url = handoff_summary.get("pr_url")
        if isinstance(handoff_pr_url, str) and handoff_pr_url.strip() != pr_url:
            raise SystemExit("Implementation handoff and PR reference URLs disagree.")
    if isinstance(pr_ref, dict):
        run_pr_url = pr_ref.get("url")
        if isinstance(run_pr_url, str) and run_pr_url.strip() != pr_url:
            raise SystemExit("Implementation PR reference URLs disagree.")

    pr_context = _collect_pr_review_context(workspace_dir=owner_root, pr_url=pr_url)
    target_contract = selected_provenance.get("target_contract")
    if isinstance(target_contract, dict):
        try:
            implementation_provenance = validate_verified_implementation_head(
                run_dir=implementation_run_dir
            )
        except ValueError as exc:
            raise SystemExit(
                "Implementation verification is not bound to its committed PR head: "
                f"{exc}"
            ) from exc
        pr_context = _attach_deterministic_plan_scope(
            pr_context=pr_context,
            target_contract=target_contract,
            verified_implementation_head=str(
                implementation_provenance["verified_implementation_head"]
            ),
        )
    elif selected_provenance.get("generated_ticket") is True:
        raise SystemExit("Generated plan is missing its runner-owned target contract.")
    else:
        pr_context = {
            **pr_context,
            "implementation_scope": {
                "schema_version": 1,
                "status": "not_applicable_external",
                "reason": "Externally originated plan has no automated stage-6 contract.",
            },
        }
    implementation_scope = pr_context["implementation_scope"]
    if implementation_scope.get("status") not in {
        "verified",
        "not_applicable_external",
    }:
        raise SystemExit(
            "Refusing model review because a required production target is untouched "
            "or the PR head is not the verified implementation head: "
            + json.dumps(implementation_scope.get("errors", []), ensure_ascii=False)
        )
    pr_meta = pr_context.get("pr")
    if not isinstance(pr_meta, dict):
        raise SystemExit("Unable to read PR metadata for review.")
    if pr_meta.get("url") != pr_url:
        raise SystemExit("Current PR metadata URL does not match the implementation PR.")
    current_merge_ready, current_gate = _current_merge_gate_from_pr_context(pr_context)

    head_ref_name = pr_meta.get("headRefName")
    reviewed_head_oid = str(pr_meta.get("headRefOid") or "").strip()
    if not reviewed_head_oid:
        raise SystemExit("PR metadata is missing headRefOid; review cannot be bound to a commit.")
    source_review_run_dir = (
        adopt_review_run.expanduser().resolve() if adopt_review_run is not None else None
    )
    if source_review_run_dir is not None:
        existing_adoption = _existing_completed_review_adoption(
            ledger_path=ledger_path,
            fingerprint=selected.fingerprint,
            source_review_run_dir=source_review_run_dir,
            reviewed_head_oid=reviewed_head_oid,
        )
        if existing_adoption is not None:
            existing_run_dir, existing_summary = existing_adoption
            if dry_run:
                print(
                    json.dumps(
                        {
                            "ticket_fingerprint": selected.fingerprint,
                            "review_adoption_status": "already_completed",
                            "review_run_dir": str(existing_run_dir),
                            "source_review_run_dir": str(source_review_run_dir),
                            "reviewed_head_oid": reviewed_head_oid,
                            "model_invoked": False,
                            "docker_invoked": False,
                        },
                        indent=2,
                        ensure_ascii=False,
                    )
                )
            return existing_run_dir, existing_summary
    correction_context = (
        _review_correction_context(
            selected=selected,
            implementation_run_dir=implementation_run_dir,
            ledger_path=ledger_path,
            pr_url=pr_url,
            reviewed_head_oid=reviewed_head_oid,
        )
        if review_corrections or source_review_run_dir is not None
        else None
    )
    effective_review_model = review_model
    adoption_report: dict[str, Any] | None = None
    adoption_evidence: dict[str, Any] | None = None
    if correction_context is not None:
        if review_agent != "codex":
            raise SystemExit("Exact-session review correction requires --agent codex.")
        prior_model = correction_context.get("prior_model")
        if effective_review_model is None and isinstance(prior_model, str):
            effective_review_model = prior_model
        elif (
            isinstance(prior_model, str)
            and isinstance(effective_review_model, str)
            and effective_review_model != prior_model
        ):
            raise SystemExit(
                "Review correction model differs from the prior reviewer model: "
                f"prior={prior_model!r} requested={effective_review_model!r}"
            )
        if source_review_run_dir is not None:
            adoption_report, adoption_evidence = _validate_retained_review_for_adoption(
                repo_root=repo_root,
                runs_dir=Path(cfg.runs_dir),
                source_review_run_dir=source_review_run_dir,
                correction_context=correction_context,
                pr_url=pr_url,
                reviewed_head_oid=reviewed_head_oid,
                implementation_run_dir=implementation_run_dir,
            )
            review_corrections = [
                str(value) for value in adoption_evidence["corrections"]
            ]
            source_target = _required_json_object(
                source_review_run_dir / "target_ref.json",
                label="Retained target ref",
            )
            source_model = source_target.get("model")
            if not isinstance(source_model, str) or not source_model.strip():
                raise SystemExit("Retained correction target is missing its model.")
            if effective_review_model is None:
                effective_review_model = source_model
            elif effective_review_model != source_model:
                raise SystemExit("Requested adoption model differs from the retained correction.")
            review_prompt = ""
        else:
            review_prompt = _build_review_correction_prompt(
                corrections=review_corrections,
                correction_context=correction_context,
                pr_context=pr_context,
            )
    else:
        review_prompt = _build_review_append_prompt(
            selected=selected,
            handoff_summary=handoff_summary if isinstance(handoff_summary, dict) else None,
            pr_ref=pr_ref if isinstance(pr_ref, dict) else None,
            ci_gate=ci_gate if isinstance(ci_gate, dict) else None,
            pr_context=pr_context,
        )

    if dry_run:
        print(
            json.dumps(
                {
                    "ticket_fingerprint": selected.fingerprint,
                    "ticket_path": str(selected.idea_path) if selected.idea_path is not None else None,
                    "implementation_run_dir": str(implementation_run_dir),
                    "pr_url": pr_url,
                    "head_ref_name": head_ref_name,
                    "reviewed_head_oid": reviewed_head_oid,
                    "review_agent": review_agent,
                    "review_model": effective_review_model,
                    "review_persona_id": review_persona_id,
                    "review_mission_id": review_mission_id,
                    "merge_gate_ready_now": current_merge_ready,
                    "merge_gate": current_gate,
                    "correction_count": len(review_corrections),
                    "adopt_retained_review_run_dir": (
                        str(source_review_run_dir)
                        if source_review_run_dir is not None
                        else None
                    ),
                    "adoption_revalidation_verified": (
                        adoption_evidence["supplemental_execpolicy_revalidation"].get(
                            "verified"
                        )
                        if adoption_evidence is not None
                        else None
                    ),
                    "model_invoked": source_review_run_dir is None,
                    "docker_invoked": False if source_review_run_dir is not None else None,
                    "correction_of_review_run_dir": (
                        str(correction_context["previous_review_run_dir"])
                        if correction_context is not None
                        else None
                    ),
                    "codex_resume_session_id": (
                        correction_context["codex_resume_session_id"]
                        if correction_context is not None
                        else None
                    ),
                },
                indent=2,
                ensure_ascii=False,
            )
        )
        return None, None

    if source_review_run_dir is not None:
        assert adoption_report is not None
        assert adoption_evidence is not None
        review_run_dir = _materialize_review_adoption(
            runs_dir=Path(cfg.runs_dir),
            fingerprint=selected.fingerprint,
            source_review_run_dir=source_review_run_dir,
            report=adoption_report,
            adoption_evidence=adoption_evidence,
        )
        staged_review_prompt_path = source_review_run_dir / "prompt.txt"
        report = adoption_report
    else:
        effective_repo_input = str(owner_root)
        if _looks_like_local_path(effective_repo_input):
            git_root = _infer_git_root(owner_root)
            if git_root is not None:
                remote_url = _git_remote_url(repo_dir=git_root, remote_name="origin")
                if isinstance(remote_url, str) and remote_url.strip():
                    effective_repo_input = remote_url.strip()

        effective_exec_backend = str(exec_backend).strip().lower()
        maintenance_profile_eligible = _maintenance_profile_is_eligible(
            repo_root=repo_root,
            repo_input=effective_repo_input,
        )
        effective_exec_docker_profile = _resolve_exec_docker_profile(
            exec_backend=effective_exec_backend,
            requested_profile=exec_docker_profile,
            maintenance_eligible=maintenance_profile_eligible,
        )
        if effective_exec_backend == "docker":
            _require_docker_available()

        staged_review_prompt_dir = repo_root / "runs" / "_tmp_review_prompt_staging"
        staged_review_prompt_dir.mkdir(parents=True, exist_ok=True)
        staged_review_prompt_path = (
            staged_review_prompt_dir
            / f"{selected.fingerprint}_{int(time.time() * 1000)}_review_prompt.md"
        )
        staged_review_prompt_path.write_text(review_prompt, encoding="utf-8")

        request = RunRequest(
            repo=effective_repo_input,
            ref=reviewed_head_oid,
            agent=review_agent,
            model=effective_review_model,
            policy=review_policy,
            persona_id=review_persona_id,
            mission_id=review_mission_id,
            seed=review_seed,
            agent_config_overrides=tuple(str(v) for v in review_agent_config_override or []),
            agent_append_system_prompt_file=(
                None if correction_context is not None else staged_review_prompt_path
            ),
            agent_user_prompt=(review_prompt if correction_context is not None else None),
            codex_resume_session_id=(
                correction_context["codex_resume_session_id"]
                if correction_context is not None
                else None
            ),
            codex_resume_usage_source_run_dir=(
                correction_context["codex_resume_usage_source_run_dir"]
                if correction_context is not None
                else None
            ),
            keep_workspace=bool(keep_workspace),
            verification_commands=(),
            verification_reuse_mode="off",
            exec_backend=effective_exec_backend,
            exec_docker_profile=effective_exec_docker_profile,
            exec_use_host_agent_login=bool(exec_use_host_agent_login),
            exec_use_target_sandbox_cli_install=bool(exec_use_target_sandbox_cli_install),
            exec_cache=str(exec_cache),
            exec_cache_dir=exec_cache_dir,
            exec_maintenance_venv_cache=bool(maintenance_venv_cache),
            exec_keep_container=bool(exec_keep_container),
            resume_workspace_dir=(
                correction_context["resume_workspace_dir"]
                if correction_context is not None
                else None
            ),
        )

        result = run_once(cfg, request)
        review_run_dir = result.run_dir
        if int(result.exit_code or 0) != 0:
            raise SystemExit(
                f"Review run failed (exit_code={result.exit_code}) in {review_run_dir}"
            )
        if result.report_validation_errors:
            raise SystemExit(
                "Review run produced an invalid report: "
                + "; ".join(str(err) for err in result.report_validation_errors)
            )
        report = _read_json(review_run_dir / "report.json")
        if not isinstance(report, dict):
            raise SystemExit(
                f"Missing or invalid report.json in review run dir: {review_run_dir}"
            )
    semantic_corrections: list[dict[str, str]] = []
    while True:
        try:
            agent_summary = _extract_agent_review_summary(report)
            review_summary = _build_final_review_summary(
                selected=selected,
                review_run_dir=review_run_dir,
                pr_url=pr_url,
                pr_context=pr_context,
                agent_summary=agent_summary,
                report=report,
            )
            break
        except ValueError as exc:
            if (
                source_review_run_dir is not None
                or review_agent != "codex"
                or len(semantic_corrections) >= _MAX_REVIEW_SEMANTIC_CORRECTIONS
            ):
                raise SystemExit(f"Invalid review output in {review_run_dir}: {exc}") from exc
            failed_run_dir = review_run_dir
            validation_error = str(exc)
            semantic_corrections.append(
                {
                    "failed_run_dir": str(failed_run_dir),
                    "validation_error": validation_error,
                }
            )
            request = _build_review_semantic_correction_request(
                request=request,
                failed_run_dir=failed_run_dir,
                reviewed_head_oid=reviewed_head_oid,
                validation_error=validation_error,
            )
            result = run_once(cfg, request)
            review_run_dir = result.run_dir
            if int(result.exit_code or 0) != 0:
                raise SystemExit(
                    "Review semantic correction failed "
                    f"(exit_code={result.exit_code}) in {review_run_dir}"
                ) from None
            if result.report_validation_errors:
                raise SystemExit(
                    "Review semantic correction produced an invalid report: "
                    + "; ".join(str(err) for err in result.report_validation_errors)
                ) from None
            report = _read_json(review_run_dir / "report.json")
            if not isinstance(report, dict):
                raise SystemExit(
                    "Missing or invalid semantic-correction report.json in review run dir: "
                    f"{review_run_dir}"
                ) from None

    if correction_context is not None:
        review_summary = {
            **review_summary,
            "correction_of_review_run_dir": str(
                correction_context["previous_review_run_dir"]
            ),
            "correction_count": len(review_corrections),
            "review_author_session_id": correction_context[
                "codex_resume_session_id"
            ],
        }
    if semantic_corrections:
        review_summary = {
            **review_summary,
            "semantic_correction_count": len(semantic_corrections),
            "semantic_corrections": semantic_corrections,
        }
    if source_review_run_dir is not None:
        review_summary = {
            **review_summary,
            "review_source": "retained_same_author_report_adoption",
            "adopted_from_review_run_dir": str(source_review_run_dir),
            "model_invoked_for_adoption": False,
            "adoption_evidence_sha256": adoption_evidence[
                "adoption_evidence_sha256"
            ],
        }
    ticket_provenance_for_review = {
        key: selected_provenance[key]
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
    implementation_ticket_ref_sha256 = sha256(
        (implementation_run_dir / "ticket_ref.json").read_bytes()
    ).hexdigest()
    review_summary = {
        **review_summary,
        "ticket_provenance": ticket_provenance_for_review,
        "implementation_ticket_ref_sha256": implementation_ticket_ref_sha256,
    }

    _write_json(review_run_dir / "review_summary.json", review_summary)
    pr_review_ref = _submit_pr_review(
        workspace_dir=owner_root,
        pr_url=pr_url,
        review_run_dir=review_run_dir,
        review_summary=review_summary,
    )
    _write_json(review_run_dir / "pr_review_ref.json", pr_review_ref)
    if pr_review_ref.get("submitted") is not True:
        raise SystemExit(
            "Failed to publish PR review: "
            + (
                str(pr_review_ref.get("stderr") or "").strip()
                or str(pr_review_ref.get("stdout") or "").strip()
                or "gh pr review failed"
            )
        )
    _write_json(
        review_run_dir / "review_ref.json",
        {
            "schema_version": 2,
            "ticket_fingerprint": selected.fingerprint,
            "ticket_path": str(selected.idea_path) if selected.idea_path is not None else None,
            "implementation_run_dir": str(implementation_run_dir),
            "implementation_ticket_ref_sha256": implementation_ticket_ref_sha256,
            "ticket_provenance": ticket_provenance_for_review,
            "pr_url": pr_url,
            "reviewed_head_oid": reviewed_head_oid,
            "staged_review_prompt_path": str(staged_review_prompt_path),
            "adopted_from_review_run_dir": (
                str(source_review_run_dir)
                if source_review_run_dir is not None
                else None
            ),
            "review_report_adoption_path": (
                str(review_run_dir / "review_report_adoption.json")
                if source_review_run_dir is not None
                else None
            ),
            "correction_of_review_run_dir": (
                str(correction_context["previous_review_run_dir"])
                if correction_context is not None
                else None
            ),
            "review_corrections": review_corrections,
            "review_author_session_id": (
                correction_context["codex_resume_session_id"]
                if correction_context is not None
                else None
            ),
            "semantic_correction_count": len(semantic_corrections),
            "semantic_corrections": semantic_corrections,
        },
    )
    update_ledger_file(
        ledger_path,
        fingerprint=selected.fingerprint,
        updates={
            "last_review_run_dir": str(review_run_dir),
            "last_review_pr_url": pr_url,
            "last_review_decision": review_summary["review_decision"],
            "last_review_causal_acceptance": bool(
                review_summary.get("causal_acceptance") is True
            ),
            "last_review_merge_ready": bool(review_summary["merge_ready"]),
            "last_review_ci_conclusion": review_summary.get("ci_conclusion"),
            "last_reviewed_head_oid": review_summary.get("reviewed_head_oid"),
            "last_review_correction_of": review_summary.get(
                "correction_of_review_run_dir"
            ),
            "last_review_correction_count": int(
                review_summary.get("correction_count") or 0
            ),
            "last_review_author_session_id": review_summary.get(
                "review_author_session_id"
            ),
        },
    )
    resume_state = write_ticket_resume_state(
        selected=selected,
        run_dir=implementation_run_dir,
        owner_root=owner_root,
        branch=None,
        exit_code=0,
        review_run_dir=review_run_dir,
    )
    update_ledger_file(
        ledger_path,
        fingerprint=selected.fingerprint,
        updates={
            "last_resume_state_path": str(
                implementation_run_dir / RESUME_STATE_ARTIFACT_NAME
            ),
            "last_resume_lifecycle_state": resume_state.get("lifecycle_state"),
        },
    )
    return review_run_dir, review_summary



def _read_review_summary(*, review_run_dir: Path) -> dict[str, Any]:
    summary = _read_json(review_run_dir / "review_summary.json")
    if not isinstance(summary, dict):
        raise SystemExit(f"Missing review_summary.json in {review_run_dir}")
    return summary


def _require_causal_review_acceptance(review_summary: dict[str, Any]) -> None:
    if review_summary.get("causal_acceptance") is True:
        return
    if (
        "causal_acceptance" not in review_summary
        and review_summary.get("merge_ready") is True
    ):
        # Backward compatibility for reviews recorded before causal acceptance
        # was stored separately from the mutable PR merge gate.
        return
    raise SystemExit(
        "Review summary is not causally accepted "
        f"(decision={review_summary.get('review_decision')!r}, "
        f"mechanism={review_summary.get('mechanism_assessment')!r}, "
        f"oracle={review_summary.get('original_scenario_oracle')!r}, "
        f"causal_path={review_summary.get('causal_path_assessment')!r})."
    )


def _cmd_review_run(args: argparse.Namespace) -> int:
    repo_root = _resolve_repo_root(args.repo_root)
    cfg = _load_runner_config(repo_root)
    owner_root = args.owner_root.resolve()
    selected = _select_review_ticket(
        owner_root=owner_root,
        ticket_path=args.ticket_path,
        fingerprint=args.fingerprint,
    )

    ledger_path = _resolve_ledger_path(repo_root=repo_root, raw=args.ledger)
    ledger_entry = _load_ledger_entry(ledger_path=ledger_path, fingerprint=selected.fingerprint)
    run_dir_raw = ledger_entry.get("last_run_dir")
    if not isinstance(run_dir_raw, str) or not run_dir_raw.strip():
        raise SystemExit(
            f"No last_run_dir recorded in ledger for ticket {selected.fingerprint!r}. "
            f"Expected ledger entry in {ledger_path}."
        )
    implementation_run_dir = Path(run_dir_raw)
    review_run_dir, _review_summary = _run_review_for_selected_ticket(
        repo_root=repo_root,
        cfg=cfg,
        owner_root=owner_root,
        selected=selected,
        implementation_run_dir=implementation_run_dir,
        ledger_path=ledger_path,
        review_agent=str(args.agent),
        review_model=args.model,
        review_policy=str(args.policy),
        review_persona_id=str(args.persona_id),
        review_mission_id=str(args.mission_id),
        review_seed=int(args.seed),
        review_agent_config_override=list(getattr(args, "agent_config_override", []) or []),
        review_corrections=[
            str(value).strip()
            for value in list(getattr(args, "review_corrections", []) or [])
            if str(value).strip()
        ],
        adopt_review_run=None,
        keep_workspace=bool(args.keep_workspace),
        exec_backend=str(args.exec_backend),
        exec_use_host_agent_login=bool(args.exec_use_host_agent_login),
        exec_use_target_sandbox_cli_install=bool(args.exec_use_target_sandbox_cli_install),
        exec_docker_profile=getattr(args, "exec_docker_profile", None),
        exec_keep_container=bool(args.exec_keep_container),
        exec_cache=str(args.exec_cache),
        exec_cache_dir=args.exec_cache_dir,
        maintenance_venv_cache=bool(args.maintenance_venv_cache),
        dry_run=bool(args.dry_run),
    )
    if review_run_dir is None:
        return 0
    print(str(review_run_dir))
    return 0


def _cmd_review_adopt_run(args: argparse.Namespace) -> int:
    """Adopt a valid same-author review whose runner failed after agent success."""

    repo_root = _resolve_repo_root(args.repo_root)
    cfg = _load_runner_config(repo_root)
    owner_root = args.owner_root.resolve()
    selected = _select_review_ticket(
        owner_root=owner_root,
        ticket_path=args.ticket_path,
        fingerprint=args.fingerprint,
    )
    ledger_path = _resolve_ledger_path(repo_root=repo_root, raw=args.ledger)
    ledger_entry = _load_ledger_entry(
        ledger_path=ledger_path,
        fingerprint=selected.fingerprint,
    )
    implementation_run_raw = ledger_entry.get("last_run_dir")
    if not isinstance(implementation_run_raw, str) or not implementation_run_raw.strip():
        raise SystemExit(
            f"No last_run_dir recorded in ledger for ticket {selected.fingerprint!r}."
        )
    review_run_dir, _summary = _run_review_for_selected_ticket(
        repo_root=repo_root,
        cfg=cfg,
        owner_root=owner_root,
        selected=selected,
        implementation_run_dir=Path(implementation_run_raw).resolve(),
        ledger_path=ledger_path,
        review_agent="codex",
        review_model=None,
        review_policy="inspect",
        review_persona_id=_DEFAULT_REVIEW_PERSONA_ID,
        review_mission_id=_DEFAULT_REVIEW_MISSION_ID,
        review_seed=0,
        review_agent_config_override=[],
        review_corrections=[],
        adopt_review_run=args.review_run_dir,
        keep_workspace=True,
        exec_backend="local",
        exec_use_host_agent_login=True,
        exec_use_target_sandbox_cli_install=False,
        exec_docker_profile=None,
        exec_keep_container=False,
        exec_cache="warm",
        exec_cache_dir=None,
        maintenance_venv_cache=False,
        dry_run=bool(args.dry_run),
    )
    if review_run_dir is not None:
        print(str(review_run_dir))
    return 0


def _cmd_review_status(args: argparse.Namespace) -> int:
    repo_root = _resolve_repo_root(args.repo_root)
    owner_root = args.owner_root.resolve()
    selected = _select_review_ticket(
        owner_root=owner_root,
        ticket_path=args.ticket_path,
        fingerprint=args.fingerprint,
    )
    ledger_path = _resolve_ledger_path(repo_root=repo_root, raw=args.ledger)
    ledger_entry = _load_ledger_entry(ledger_path=ledger_path, fingerprint=selected.fingerprint)
    review_run_dir_raw = ledger_entry.get("last_review_run_dir")
    if not isinstance(review_run_dir_raw, str) or not review_run_dir_raw.strip():
        raise SystemExit(f"No review run recorded in ledger for ticket {selected.fingerprint!r}.")
    review_summary = _read_review_summary(review_run_dir=Path(review_run_dir_raw))
    print(json.dumps(review_summary, indent=2, ensure_ascii=False))
    return 0


def _recover_noncausal_premerge_rejection(
    *,
    selected: SelectedTicket,
    ledger_path: Path,
    review_run_dir: Path,
    review_summary: dict[str, Any],
    pr_url: str,
    pr_context: dict[str, Any],
    implementation_run_dir: Path,
) -> dict[str, Any]:
    """Repair summaries written by the former catch-all premerge failure path.

    Older review-merge code converted worktree/setup/contract exceptions into a
    critical causal finding. Those exceptions have no outcome-role artifact and do
    not establish that the implementation failed its oracle. Rebuild the immutable
    model conclusion from its retained report, retain the operational block as a
    separate artifact, and restore the merge lifecycle without another model turn.
    """

    failure_path = review_run_dir / "premerge_original_scenario_failure.json"
    failure = _read_json(failure_path)
    if not isinstance(failure, dict):
        return review_summary
    role_artifact_path = failure.get("role_artifact_path")
    role_failure = isinstance(role_artifact_path, str) and bool(role_artifact_path.strip())
    prior_attempt_count = failure.get("attempt_count")
    if not isinstance(prior_attempt_count, int) or prior_attempt_count < 1:
        prior_attempt_count = 1
    if role_failure and prior_attempt_count >= 2:
        return review_summary
    if review_summary.get("review_decision") != "changes_requested":
        return review_summary
    report = _read_json(review_run_dir / "report.json")
    if not isinstance(report, dict):
        return review_summary
    pr_meta_raw = pr_context.get("pr")
    pr_meta = pr_meta_raw if isinstance(pr_meta_raw, dict) else {}
    summary_pr_url = str(review_summary.get("pr_url") or "").strip()
    summary_head = str(review_summary.get("reviewed_head_oid") or "").strip().lower()
    current_pr_url = str(pr_meta.get("url") or "").strip()
    current_head = str(pr_meta.get("headRefOid") or "").strip().lower()
    requested_pr_url = str(pr_url or "").strip()
    provenance_errors: list[str] = []
    if not summary_pr_url:
        provenance_errors.append("overwritten review summary is missing pr_url")
    if not summary_head:
        provenance_errors.append("overwritten review summary is missing reviewed_head_oid")
    if requested_pr_url != summary_pr_url:
        provenance_errors.append("requested PR URL does not match overwritten review summary")
    if current_pr_url != summary_pr_url:
        provenance_errors.append("current PR URL does not match overwritten review summary")
    if current_head != summary_head:
        provenance_errors.append("current PR head does not match overwritten review summary")

    extensions_raw = report.get("extensions")
    extensions = extensions_raw if isinstance(extensions_raw, dict) else {}
    if "reviewed_head_oid" in extensions:
        report_head = str(extensions.get("reviewed_head_oid") or "").strip().lower()
        if report_head != summary_head:
            provenance_errors.append(
                "retained report reviewed head does not match overwritten review summary"
            )

    review_ref = _read_json(review_run_dir / "review_ref.json")
    if isinstance(review_ref, dict):
        if "reviewed_head_oid" in review_ref:
            review_ref_head = str(review_ref.get("reviewed_head_oid") or "").strip().lower()
            if review_ref_head != summary_head:
                provenance_errors.append(
                    "review reference head does not match overwritten review summary"
                )
        if "pr_url" in review_ref:
            review_ref_url = str(review_ref.get("pr_url") or "").strip()
            if review_ref_url != summary_pr_url:
                provenance_errors.append(
                    "review reference PR URL does not match overwritten review summary"
                )

    if provenance_errors:
        raise SystemExit(
            "Refusing to recover a legacy pre-merge rejection because its retained "
            "review provenance does not match the current PR: "
            + "; ".join(provenance_errors)
        )
    try:
        agent_summary = _extract_agent_review_summary(report)
        restored = _build_final_review_summary(
            selected=selected,
            review_run_dir=review_run_dir,
            pr_url=pr_url,
            pr_context=pr_context,
            agent_summary=agent_summary,
            report=report,
        )
    except ValueError:
        return review_summary
    if restored.get("causal_acceptance") is not True:
        return review_summary
    extension_keys = (
        "correction_of_review_run_dir",
        "correction_count",
        "review_author_session_id",
        "ticket_provenance",
        "implementation_ticket_ref_sha256",
        "adopted_from_review_run_dir",
        "model_invoked_for_adoption",
        "adoption_evidence_sha256",
    )
    restored = {
        **restored,
        **{
            key: review_summary[key]
            for key in extension_keys
            if key in review_summary
        },
    }
    detail = str(failure.get("detail") or "").strip()
    if role_failure:
        recovery_status = "retry_pending"
        recovery_path = review_run_dir / "premerge_original_scenario_retry_pending.json"
        recovery = {
            "schema_version": 1,
            "status": recovery_status,
            "classification": "bounded_same_head_retry",
            "causal_result": "pending_retry",
            "ticket_fingerprint": selected.fingerprint,
            "reviewed_head_oid": str(review_summary.get("reviewed_head_oid") or ""),
            "detail": detail,
            "source_failure_path": str(failure_path),
            "source_role_artifact_path": role_artifact_path,
            "prior_attempt_count": prior_attempt_count,
            "maximum_attempt_count": 2,
            "review_preserved": True,
            "recorded_at_utc": _utc_now_z(),
        }
    else:
        recovery_status = "blocked_infrastructure"
        recovery_path = (
            review_run_dir
            / "premerge_original_scenario_infrastructure_reclassification.json"
        )
        recovery = {
            "schema_version": 1,
            "status": "blocked",
            "classification": "premerge_infrastructure",
            "causal_result": "not_run",
            "ticket_fingerprint": selected.fingerprint,
            "detail": detail,
            "source_failure_path": str(failure_path),
            "review_preserved": True,
            "reclassified_at_utc": _utc_now_z(),
        }
    _write_json(recovery_path, recovery)
    _write_json(review_run_dir / "review_summary.json", restored)
    resume_state = write_ticket_resume_state(
        selected=selected,
        run_dir=implementation_run_dir,
        owner_root=selected.owner_root,
        branch=None,
        exit_code=0,
        review_run_dir=review_run_dir,
    )
    update_ledger_file(
        ledger_path,
        fingerprint=selected.fingerprint,
        updates={
            "last_review_decision": restored["review_decision"],
            "last_review_causal_acceptance": bool(
                restored.get("causal_acceptance") is True
            ),
            "last_review_merge_ready": bool(restored["merge_ready"]),
            "last_premerge_original_scenario_status": recovery_status,
            (
                "last_premerge_original_scenario_retry"
                if role_failure
                else "last_premerge_original_scenario_block"
            ): str(recovery_path),
            "last_resume_state_path": str(
                implementation_run_dir / RESUME_STATE_ARTIFACT_NAME
            ),
            "last_resume_lifecycle_state": resume_state.get("lifecycle_state"),
        },
    )
    return restored


def _cmd_review_merge(args: argparse.Namespace) -> int:
    repo_root = _resolve_repo_root(args.repo_root)
    owner_root = args.owner_root.resolve()
    selected = _select_review_ticket(
        owner_root=owner_root,
        ticket_path=args.ticket_path,
        fingerprint=args.fingerprint,
    )
    ledger_path = _resolve_ledger_path(repo_root=repo_root, raw=args.ledger)
    ledger_entry = _load_ledger_entry(ledger_path=ledger_path, fingerprint=selected.fingerprint)
    review_run_dir_raw = ledger_entry.get("last_review_run_dir")
    if not isinstance(review_run_dir_raw, str) or not review_run_dir_raw.strip():
        raise SystemExit(f"No review run recorded in ledger for ticket {selected.fingerprint!r}.")
    review_run_dir = Path(review_run_dir_raw)
    review_summary = _read_review_summary(review_run_dir=review_run_dir)
    reviewed_ticket_provenance = review_summary.get("ticket_provenance")
    if selected.idea_path is not None and isinstance(reviewed_ticket_provenance, dict):
        expected_body_hash = reviewed_ticket_provenance.get("ticket_body_sha256")
        expected_plan_hash = reviewed_ticket_provenance.get("local_plan_sha256")
        if isinstance(expected_body_hash, str) and isinstance(expected_plan_hash, str):
            repair_receipt = repair_ticket_newline_expansion(
                path=selected.idea_path,
                expected_ticket_body_sha256=expected_body_hash,
                expected_local_plan_sha256=expected_plan_hash,
            )
            if repair_receipt is not None:
                _write_json(
                    review_run_dir / "ticket_newline_repair.json",
                    {**repair_receipt, "recorded_at_utc": _utc_now_z()},
                )
                selected = _select_review_ticket(
                    owner_root=owner_root,
                    ticket_path=None,
                    fingerprint=selected.fingerprint,
                )
    pr_url = review_summary.get("pr_url")
    if not isinstance(pr_url, str) or not pr_url.strip():
        raise SystemExit(f"Review summary for {selected.fingerprint!r} is missing pr_url.")
    implementation_run_dir = _require_review_artifact_bindings(
        selected=selected,
        ledger_entry=ledger_entry,
        review_run_dir=review_run_dir,
        review_summary=review_summary,
        pr_url=pr_url,
    )
    _preflight_existing_outcome_stores(selected=selected, ledger_entry=ledger_entry)
    pr_context = _collect_pr_review_context(workspace_dir=owner_root, pr_url=pr_url)
    selected_provenance = _selected_ticket_provenance(
        selected,
        require_local_plan=True,
    )
    target_contract = selected_provenance.get("target_contract")
    if isinstance(target_contract, dict):
        try:
            implementation_provenance = validate_verified_implementation_head(
                run_dir=implementation_run_dir
            )
        except ValueError as exc:
            raise SystemExit(
                "Implementation verification is not bound to its committed PR head: "
                f"{exc}"
            ) from exc
        pr_context = _attach_deterministic_plan_scope(
            pr_context=pr_context,
            target_contract=target_contract,
            verified_implementation_head=str(
                implementation_provenance["verified_implementation_head"]
            ),
        )
    elif selected_provenance.get("generated_ticket") is True:
        raise SystemExit("Generated plan is missing its runner-owned target contract.")
    else:
        pr_context = {
            **pr_context,
            "implementation_scope": {
                "schema_version": 1,
                "status": "not_applicable_external",
                "reason": "Externally originated plan has no automated stage-6 contract.",
            },
        }
    current_scope = pr_context["implementation_scope"]
    reviewed_scope = review_summary.get("implementation_scope")
    external_scope = current_scope.get("status") == "not_applicable_external"
    if not external_scope and (
        current_scope.get("status") != "verified"
        or not isinstance(reviewed_scope, dict)
        or reviewed_scope.get("status") != "verified"
        or current_scope.get("receipt_sha256") != reviewed_scope.get("receipt_sha256")
    ):
        raise SystemExit(
            "Current PR scope no longer matches the reviewed stage-6 target contract."
        )
    review_summary = _recover_noncausal_premerge_rejection(
        selected=selected,
        ledger_path=ledger_path,
        review_run_dir=review_run_dir,
        review_summary=review_summary,
        pr_url=pr_url,
        pr_context=pr_context,
        implementation_run_dir=implementation_run_dir,
    )
    _require_causal_review_acceptance(review_summary)
    pr_meta_raw = pr_context.get("pr")
    pr_meta = pr_meta_raw if isinstance(pr_meta_raw, dict) else {}
    if pr_meta.get("url") != pr_url:
        raise SystemExit("Current PR metadata URL does not match the reviewed PR URL.")
    reviewed_head_oid = _require_unchanged_reviewed_head(
        fingerprint=selected.fingerprint,
        review_summary=review_summary,
        pr_meta=pr_meta,
    )
    _require_explicit_passing_ci(pr_context)
    already_merged = str(pr_meta.get("state") or "").strip().upper() == "MERGED"
    merge_provenance: dict[str, str] | None = None
    if already_merged:
        merge_provenance = _collect_merged_pr_provenance(
            workspace_dir=owner_root,
            pr_url=pr_url,
        )
        preflight_provenance = merge_provenance
    else:
        provisional_branch = str(pr_meta.get("baseRefName") or "").strip()
        provisional_commit = str(pr_meta.get("headRefOid") or "").strip()
        if not provisional_branch or not provisional_commit:
            raise SystemExit("Current PR metadata is missing preflight merge provenance.")
        preflight_provenance = {
            "target_branch": provisional_branch,
            "merged_commit": provisional_commit,
        }
    preflight_outcome = _build_merge_outcome_record(
        selected=selected,
        pr_url=pr_url,
        pr_context=pr_context,
        merge_provenance=preflight_provenance,
        review_run_dir=review_run_dir,
        implementation_run_dir=implementation_run_dir,
    )
    _reconcile_merge_outcome(
        selected=selected,
        ledger_entry=ledger_entry,
        proposed=preflight_outcome,
    )
    if selected_provenance.get("generated_ticket") is True and not already_merged:
        retry_pending_path = review_run_dir / "premerge_original_scenario_retry_pending.json"
        retry_pending = _read_json(retry_pending_path)
        prior_role_attempt_count = 0
        prior_role_artifact_paths: list[str] = []
        if (
            isinstance(retry_pending, dict)
            and retry_pending.get("status") == "retry_pending"
            and str(retry_pending.get("reviewed_head_oid") or "").strip().lower()
            == reviewed_head_oid.lower()
        ):
            raw_prior_count = retry_pending.get("prior_attempt_count")
            if isinstance(raw_prior_count, int) and raw_prior_count > 0:
                prior_role_attempt_count = raw_prior_count
            prior_path = retry_pending.get("source_role_artifact_path")
            if isinstance(prior_path, str) and prior_path.strip():
                prior_role_artifact_paths.append(prior_path)
        remaining_role_attempts = max(1, 2 - prior_role_attempt_count)
        role_failures: list[OutcomeRoleDidNotPass] = []
        try:
            for attempt_number in range(1, remaining_role_attempts + 1):
                try:
                    premerge_original_scenario = verify_premerge_original_scenario(
                        repo_root=repo_root,
                        owner_root=owner_root,
                        fingerprint=selected.fingerprint,
                        ticket_markdown=selected.ticket_markdown,
                        current=preflight_outcome,
                        selected_provenance=selected_provenance,
                    )
                except OutcomeRoleDidNotPass as attempt_exc:
                    role_failures.append(attempt_exc)
                    if attempt_number < remaining_role_attempts:
                        continue
                    raise
                break
        except OutcomeContractNotExecutable as exc:
            detail = str(exc)
            correction_path = (
                review_run_dir / "premerge_outcome_contract_not_executable.json"
            )
            correction = {
                "schema_version": 1,
                "status": "changes_requested",
                "classification": "outcome_contract_not_executable",
                "causal_result": "not_run",
                "ticket_fingerprint": selected.fingerprint,
                "case_id": selected_provenance["case_id"],
                "plan_revision_id": selected_provenance["plan_revision_id"],
                "verified_implementation_head": reviewed_head_oid,
                "detail": detail,
                "executability_receipt_path": str(exc.receipt_path),
                "failures": [dict(item) for item in exc.failures],
                "recorded_at_utc": _utc_now_z(),
            }
            _write_json(correction_path, correction)
            existing_findings_raw = review_summary.get("findings")
            existing_findings = (
                [item for item in existing_findings_raw if isinstance(item, dict)]
                if isinstance(existing_findings_raw, list)
                else []
            )
            executability_finding = {
                "severity": "critical",
                "title": "Mandatory outcome role command is not executable",
                "details": detail,
                "evidence": str(exc.receipt_path),
                "suggested_fix": (
                    "Correct the implementation on the same PR so every recognized, "
                    "mandatory plan-bound pytest path and node exists on the verified "
                    "head. Re-run the exact role command; do not weaken the outcome "
                    "contract or satisfy it with a skip."
                ),
            }
            updated_findings = [*existing_findings, executability_finding]
            previous_paths_raw = review_summary.get("remaining_causal_paths")
            previous_paths = (
                [str(item) for item in previous_paths_raw if str(item).strip()]
                if isinstance(previous_paths_raw, list)
                else []
            )
            updated_review_summary = {
                **review_summary,
                "review_decision": "changes_requested",
                "causal_acceptance": False,
                "merge_ready": False,
                "causal_path_assessment": "residual",
                "remaining_causal_paths": [*previous_paths, detail],
                "rationale": detail,
                "findings": updated_findings,
                "blocking_finding_count": sum(
                    1
                    for item in updated_findings
                    if str(item.get("severity") or "").strip().casefold()
                    in {"error", "high", "critical", "blocker", "fatal"}
                ),
            }
            _write_json(
                review_run_dir / "review_summary.json",
                updated_review_summary,
            )
            resume_state = write_ticket_resume_state(
                selected=selected,
                run_dir=implementation_run_dir,
                owner_root=selected.owner_root,
                branch=str(pr_meta.get("headRefName") or "").strip() or None,
                exit_code=4,
                review_run_dir=review_run_dir,
            )
            update_ledger_file(
                ledger_path,
                fingerprint=selected.fingerprint,
                updates={
                    "last_review_decision": "changes_requested",
                    "last_review_causal_acceptance": False,
                    "last_review_merge_ready": False,
                    "last_premerge_outcome_contract_status": "correction_required",
                    "last_premerge_outcome_contract_failure": str(correction_path),
                    "last_resume_state_path": str(
                        implementation_run_dir / RESUME_STATE_ARTIFACT_NAME
                    ),
                    "last_resume_lifecycle_state": resume_state.get("lifecycle_state"),
                },
            )
            print(detail, file=sys.stderr)
            return 4
        except OutcomeRoleDidNotPass as exc:
            detail = (
                "Runner-owned original-scenario proof failed on the exact verified PR "
                f"head before merge: {exc}"
            )
            artifact_path = getattr(exc, "artifact_path", None)
            failure = {
                "schema_version": 1,
                "status": "changes_requested",
                "ticket_fingerprint": selected.fingerprint,
                "case_id": selected_provenance["case_id"],
                "plan_revision_id": selected_provenance["plan_revision_id"],
                "verified_implementation_head": reviewed_head_oid,
                "detail": detail,
                "role_artifact_path": (
                    str(artifact_path) if isinstance(artifact_path, Path) else None
                ),
                "role_artifact_paths": [
                    *prior_role_artifact_paths,
                    *[
                        str(item.artifact_path)
                        for item in role_failures
                        if isinstance(item.artifact_path, Path)
                    ],
                ],
                "attempt_count": prior_role_attempt_count + len(role_failures),
                "maximum_attempt_count": 2,
                "recorded_at_utc": _utc_now_z(),
            }
            _write_json(
                review_run_dir / "premerge_original_scenario_failure.json",
                failure,
            )
            existing_findings_raw = review_summary.get("findings")
            existing_findings = (
                [item for item in existing_findings_raw if isinstance(item, dict)]
                if isinstance(existing_findings_raw, list)
                else []
            )
            causal_finding = {
                "severity": "critical",
                "title": "Original evidence-backed scenario still fails",
                "details": detail,
                "evidence": failure["role_artifact_path"],
                "suggested_fix": (
                    "Re-open the researched mechanism and change the implementation so "
                    "the bound positive-behavior oracle passes; do not suppress the symptom."
                ),
            }
            updated_review_summary = {
                **review_summary,
                "review_decision": "changes_requested",
                "causal_acceptance": False,
                "merge_ready": False,
                "mechanism_assessment": "mechanism_not_addressed",
                "original_scenario_oracle": "not_exercised",
                "causal_path_assessment": "open",
                "remaining_causal_paths": [detail],
                "rationale": detail,
                "findings": [*existing_findings, causal_finding],
                "blocking_finding_count": sum(
                    1
                    for item in [*existing_findings, causal_finding]
                    if str(item.get("severity") or "").strip().casefold()
                    in {"error", "high", "critical", "blocker", "fatal"}
                ),
            }
            _write_json(
                review_run_dir / "review_summary.json",
                updated_review_summary,
            )
            resume_state = write_ticket_resume_state(
                selected=selected,
                run_dir=implementation_run_dir,
                owner_root=selected.owner_root,
                branch=str(pr_meta.get("headRefName") or "").strip() or None,
                exit_code=4,
                review_run_dir=review_run_dir,
            )
            update_ledger_file(
                ledger_path,
                fingerprint=selected.fingerprint,
                updates={
                    "last_review_decision": "changes_requested",
                    "last_review_causal_acceptance": False,
                    "last_review_merge_ready": False,
                    "last_premerge_original_scenario_status": "failed",
                    "last_premerge_original_scenario_failure": str(
                        review_run_dir / "premerge_original_scenario_failure.json"
                    ),
                    "last_resume_state_path": str(
                        implementation_run_dir / RESUME_STATE_ARTIFACT_NAME
                    ),
                    "last_resume_lifecycle_state": resume_state.get("lifecycle_state"),
                },
            )
            print(detail, file=sys.stderr)
            return 4
        except (OSError, RuntimeError, ValueError) as exc:
            detail = (
                "Runner-owned original-scenario proof was blocked before a causal "
                f"result was produced: {exc}"
            )
            artifact_path = getattr(exc, "artifact_path", None)
            blocked = {
                "schema_version": 1,
                "status": "blocked",
                "classification": "premerge_infrastructure",
                "causal_result": "not_run",
                "ticket_fingerprint": selected.fingerprint,
                "case_id": selected_provenance["case_id"],
                "plan_revision_id": selected_provenance["plan_revision_id"],
                "verified_implementation_head": reviewed_head_oid,
                "detail": detail,
                "role_artifact_path": (
                    str(artifact_path) if isinstance(artifact_path, Path) else None
                ),
                "review_preserved": True,
                "recorded_at_utc": _utc_now_z(),
            }
            blocked_path = review_run_dir / "premerge_original_scenario_blocked.json"
            _write_json(blocked_path, blocked)
            update_ledger_file(
                ledger_path,
                fingerprint=selected.fingerprint,
                updates={
                    "last_premerge_original_scenario_status": "blocked_infrastructure",
                    "last_premerge_original_scenario_block": str(blocked_path),
                    "last_review_decision": review_summary["review_decision"],
                    "last_review_causal_acceptance": bool(
                        review_summary.get("causal_acceptance") is True
                    ),
                    "last_review_merge_ready": bool(review_summary["merge_ready"]),
                },
            )
            print(detail, file=sys.stderr)
            return 3
        if prior_role_attempt_count or role_failures:
            _write_json(
                review_run_dir / "premerge_original_scenario_retry.json",
                {
                    "schema_version": 1,
                    "status": "passed_after_retry",
                    "classification": "self_healed_nondeterministic_or_infrastructure_failure",
                    "causal_result": "passed",
                    "ticket_fingerprint": selected.fingerprint,
                    "reviewed_head_oid": reviewed_head_oid,
                    "attempt_count": prior_role_attempt_count + len(role_failures) + 1,
                    "prior_role_artifact_paths": [
                        *prior_role_artifact_paths,
                        *[
                            str(item.artifact_path)
                            for item in role_failures
                            if isinstance(item.artifact_path, Path)
                        ],
                    ],
                    "review_preserved": True,
                    "recorded_at_utc": _utc_now_z(),
                },
            )
        _write_json(
            review_run_dir / "premerge_original_scenario.json",
            premerge_original_scenario,
        )

    if already_merged:
        merge_ref = {
            "schema_version": 1,
            "pr_url": pr_url,
            "merged": True,
            "already_merged": True,
            "stdout": "PR was already merged; resuming outcome finalization",
            "stderr": "",
            "returncode": 0,
            "merged_at_utc": ledger_entry.get("last_merged_at") or _utc_now_z(),
        }
    else:
        current_merge_ready, current_gate = _current_merge_gate_from_pr_context(pr_context)
        if not current_merge_ready:
            raise SystemExit(
                "Refusing to merge because the current PR gate is not green: "
                f"{json.dumps(current_gate, ensure_ascii=False)}"
            )

        proc = subprocess.run(
            [
                "gh",
                "pr",
                "merge",
                pr_url,
                "--merge",
                "--delete-branch",
                "--match-head-commit",
                reviewed_head_oid,
            ],
            cwd=str(owner_root),
            capture_output=True,
            text=True,
            check=False,
        )
        merge_ref = {
            "schema_version": 1,
            "pr_url": pr_url,
            "merged": proc.returncode == 0,
            "already_merged": False,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
            "returncode": int(proc.returncode),
            "merged_at_utc": _utc_now_z() if proc.returncode == 0 else None,
        }
        _write_json(review_run_dir / "merge_ref.json", merge_ref)
        if proc.returncode != 0:
            raise SystemExit(proc.stderr.strip() or proc.stdout.strip() or "gh pr merge failed")

    if merge_provenance is None:
        merge_provenance = _collect_merged_pr_provenance(
            workspace_dir=owner_root,
            pr_url=pr_url,
        )
    proposed_outcome = _build_merge_outcome_record(
        selected=selected,
        pr_url=pr_url,
        pr_context=pr_context,
        merge_provenance=merge_provenance,
        review_run_dir=review_run_dir,
        implementation_run_dir=implementation_run_dir,
    )
    outcome_record = _reconcile_merge_outcome(
        selected=selected,
        ledger_entry=ledger_entry,
        proposed=proposed_outcome,
    )
    merge_ref["target_branch"] = outcome_record["target_branch"]
    merge_ref["merged_commit"] = outcome_record["merged_commit"]
    merge_ref["outcome_state"] = outcome_record["state"]
    _write_json(review_run_dir / "merge_ref.json", merge_ref)

    completed_ticket_path = None
    if selected.owner_root is not None:
        completed_ticket_path = move_ticket_file(
            owner_root=selected.owner_root,
            fingerprint=selected.fingerprint,
            to_bucket="5 - complete",
            dry_run=False,
            outcome_record=outcome_record,
        )
        _sync_ticket_atom_actions(repo_root=repo_root, owner_root=selected.owner_root)
    update_ledger_file(
        ledger_path,
        fingerprint=selected.fingerprint,
        updates={
            "last_merge_pr_url": pr_url,
            "last_merged_at": merge_ref["merged_at_utc"],
            "last_outcome_state": outcome_record["state"],
            "outcome": outcome_record,
        },
    )
    outcome_progression = None
    if (
        completed_ticket_path is not None
        and selected_provenance.get("generated_ticket") is True
    ):
        outcome_progression = progress_post_merge_outcome(
            repo_root=repo_root,
            owner_root=owner_root,
            ticket_path=completed_ticket_path,
            ledger_path=ledger_path,
        )
        merge_ref["outcome_state"] = outcome_progression.final_state
        merge_ref["outcome_progression"] = outcome_progression.to_dict()
        _write_json(review_run_dir / "merge_ref.json", merge_ref)
        _write_json(
            review_run_dir / "outcome_progression.json",
            outcome_progression.to_dict(),
        )
        _sync_ticket_atom_actions(repo_root=repo_root, owner_root=owner_root)
    if implementation_run_dir is not None:
        resume_state = write_ticket_resume_state(
            selected=selected,
            run_dir=implementation_run_dir,
            owner_root=selected.owner_root,
            branch=None,
            exit_code=0,
            review_run_dir=review_run_dir,
            ticket_path_override=completed_ticket_path,
        )
        update_ledger_file(
            ledger_path,
            fingerprint=selected.fingerprint,
            updates={
                "last_resume_state_path": str(
                    implementation_run_dir / RESUME_STATE_ARTIFACT_NAME
                ),
                "last_resume_lifecycle_state": resume_state.get("lifecycle_state"),
            },
        )
    print(pr_url)
    if outcome_progression is not None and not outcome_progression.complete:
        print(
            json.dumps(outcome_progression.to_dict(), indent=2, ensure_ascii=False),
            file=sys.stderr,
        )
        return 3
    return 0




__all__ = [name for name in globals() if not name.startswith("__")]
