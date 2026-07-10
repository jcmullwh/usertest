# ruff: noqa: E501,F401,F403,F405
from __future__ import annotations

from hashlib import sha256

from backlog_repo import extract_outcome_markdown, reconcile_outcome_records

from usertest_implement.implementation_provenance import validate_verified_implementation_head
from usertest_implement.outcome_evidence import (
    expected_ticket_identity,
    validate_bound_runner_verification,
    validate_runner_ticket_ref,
)
from usertest_implement.outcome_progression import (
    progress_post_merge_outcome,
    verify_premerge_original_scenario,
)
from usertest_implement.resume_state import (
    RESUME_STATE_ARTIFACT_NAME,
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
    if Path(review_ticket_path).resolve() != selected.idea_path.resolve():
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
    if selected.idea_path is None or "4 - for_review" not in selected.idea_path.parts:
        raise SystemExit(
            f"Ticket {selected.fingerprint!r} is not in 4 - for_review and cannot be reviewed yet."
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
    if not current_merge_ready:
        raise SystemExit(
            "Refusing to run review before the PR gate is green: "
            + json.dumps(current_gate, ensure_ascii=False)
        )

    head_ref_name = pr_meta.get("headRefName")
    reviewed_head_oid = str(pr_meta.get("headRefOid") or "").strip()
    if not reviewed_head_oid:
        raise SystemExit("PR metadata is missing headRefOid; review cannot be bound to a commit.")
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
                    "review_model": review_model,
                    "review_persona_id": review_persona_id,
                    "review_mission_id": review_mission_id,
                },
                indent=2,
                ensure_ascii=False,
            )
        )
        return None, None

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
        model=review_model,
        policy=review_policy,
        persona_id=review_persona_id,
        mission_id=review_mission_id,
        seed=review_seed,
        agent_config_overrides=tuple(str(v) for v in review_agent_config_override or []),
        agent_append_system_prompt_file=staged_review_prompt_path,
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
    )

    try:
        result = run_once(cfg, request)
    finally:
        try:
            staged_review_prompt_path.unlink(missing_ok=True)
        except OSError:
            pass
    review_run_dir = result.run_dir
    if int(result.exit_code or 0) != 0:
        raise SystemExit(f"Review run failed (exit_code={result.exit_code}) in {review_run_dir}")
    if result.report_validation_errors:
        raise SystemExit(
            "Review run produced an invalid report: "
            + "; ".join(str(err) for err in result.report_validation_errors)
        )
    report = _read_json(review_run_dir / "report.json")
    if not isinstance(report, dict):
        raise SystemExit(f"Missing or invalid report.json in review run dir: {review_run_dir}")
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
    except ValueError as exc:
        raise SystemExit(f"Invalid review output in {review_run_dir}: {exc}") from exc

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
        },
    )
    update_ledger_file(
        ledger_path,
        fingerprint=selected.fingerprint,
        updates={
            "last_review_run_dir": str(review_run_dir),
            "last_review_pr_url": pr_url,
            "last_review_decision": review_summary["review_decision"],
            "last_review_merge_ready": bool(review_summary["merge_ready"]),
            "last_review_ci_conclusion": review_summary.get("ci_conclusion"),
            "last_reviewed_head_oid": review_summary.get("reviewed_head_oid"),
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
    pr_url = review_summary.get("pr_url")
    if not isinstance(pr_url, str) or not pr_url.strip():
        raise SystemExit(f"Review summary for {selected.fingerprint!r} is missing pr_url.")
    if review_summary.get("merge_ready") is not True:
        raise SystemExit(
            f"Review summary for {selected.fingerprint!r} is not merge-ready "
            f"(decision={review_summary.get('review_decision')!r}, "
            f"ci_conclusion={review_summary.get('ci_conclusion')!r})."
        )
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
        try:
            premerge_original_scenario = verify_premerge_original_scenario(
                repo_root=repo_root,
                owner_root=owner_root,
                fingerprint=selected.fingerprint,
                ticket_markdown=selected.ticket_markdown,
                current=preflight_outcome,
                selected_provenance=selected_provenance,
            )
        except (OSError, RuntimeError, ValueError) as exc:
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
