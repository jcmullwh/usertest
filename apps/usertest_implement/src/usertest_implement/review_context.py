# ruff: noqa: E501,F401,F403,F405
from __future__ import annotations

from backlog_repo.plan_scope import assess_pr_plan_scope

from usertest_implement.shared import *
from usertest_implement.ticket_prompt import project_ticket_prompt_context


def _run_gh(*, cwd: Path, argv: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            argv,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except OSError as exc:
        raise RuntimeError("gh not found on PATH") from exc


def _run_gh_json(*, cwd: Path, argv: list[str]) -> Any:
    proc = _run_gh(cwd=cwd, argv=argv)
    if proc.returncode != 0:
        stderr = (proc.stderr or "").strip()
        stdout = (proc.stdout or "").strip()
        raise RuntimeError(stderr or stdout or "gh failed")
    try:
        return json.loads(proc.stdout or "null")
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"gh returned invalid JSON: {exc}") from exc


def _run_gh_text(*, cwd: Path, argv: list[str]) -> str:
    proc = _run_gh(cwd=cwd, argv=argv)
    if proc.returncode != 0:
        stderr = (proc.stderr or "").strip()
        stdout = (proc.stdout or "").strip()
        raise RuntimeError(stderr or stdout or "gh failed")
    return proc.stdout or ""


def _load_ledger_entry(*, ledger_path: Path, fingerprint: str) -> dict[str, Any]:
    doc = load_ledger(ledger_path)
    actions = doc.get("actions")
    if not isinstance(actions, dict):
        return {}
    entry = actions.get(fingerprint)
    return entry if isinstance(entry, dict) else {}


def _coerce_pr_url(*, handoff_summary: dict[str, Any] | None, pr_ref: dict[str, Any] | None) -> str | None:
    if isinstance(handoff_summary, dict):
        pr_url = handoff_summary.get("pr_url")
        if isinstance(pr_url, str) and pr_url.strip():
            return pr_url.strip()
    if isinstance(pr_ref, dict):
        pr_url = pr_ref.get("url")
        if isinstance(pr_url, str) and pr_url.strip():
            return pr_url.strip()
    return None


def _classify_pr_checks(checks: list[dict[str, Any]]) -> tuple[str, str | None]:
    if not checks:
        return "pending", None

    accepted_terminal_states = {"SUCCESS", "SKIPPING", "NEUTRAL"}
    failure_states = {"FAILURE", "ERROR", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED"}
    pending_states = {"PENDING", "QUEUED", "IN_PROGRESS", "WAITING", "REQUESTED"}

    saw_pending = False
    saw_success = False
    for check in checks:
        state_raw = check.get("state")
        state = str(state_raw).strip().upper() if isinstance(state_raw, str) else ""
        if state in failure_states:
            return "completed", "failure"
        if state == "SUCCESS":
            saw_success = True
        if state in pending_states or not state:
            saw_pending = True
        elif state not in accepted_terminal_states:
            saw_pending = True

    if saw_pending:
        return "pending", None
    if not saw_success:
        return "completed", "neutral"
    return "completed", "success"


def _collect_pr_review_context(*, workspace_dir: Path, pr_url: str) -> dict[str, Any]:
    view_raw = _run_gh_json(
        cwd=workspace_dir,
        argv=[
            "gh",
            "pr",
            "view",
            pr_url,
            "--json",
            "number,url,title,state,isDraft,headRefName,headRefOid,baseRefName,baseRefOid,mergeable,reviewDecision,statusCheckRollup",
        ],
    )
    if not isinstance(view_raw, dict):
        raise RuntimeError("gh pr view returned non-object JSON")

    checks_raw = _run_gh_json(
        cwd=workspace_dir,
        argv=[
            "gh",
            "pr",
            "checks",
            pr_url,
            "--json",
            "name,state,startedAt,completedAt,link,bucket,event",
        ],
    )
    checks = [item for item in checks_raw if isinstance(item, dict)] if isinstance(checks_raw, list) else []
    ci_status, ci_conclusion = _classify_pr_checks(checks)

    changed_files_text = _run_gh_text(
        cwd=workspace_dir,
        argv=["gh", "pr", "diff", pr_url, "--name-only"],
    )
    changed_files = [line.strip() for line in changed_files_text.splitlines() if line.strip()]

    diff_full = _run_gh_text(
        cwd=workspace_dir,
        argv=["gh", "pr", "diff", pr_url],
    )
    diff_excerpt = diff_full
    diff_truncated = False
    if len(diff_excerpt) > _MAX_REVIEW_DIFF_CHARS:
        diff_excerpt = diff_excerpt[:_MAX_REVIEW_DIFF_CHARS].rstrip() + "\n\n[diff truncated]\n"
        diff_truncated = True

    return {
        "pr": view_raw,
        "checks": checks,
        "ci_status": ci_status,
        "ci_conclusion": ci_conclusion,
        "changed_files": changed_files,
        "diff_full": diff_full,
        "diff_excerpt": diff_excerpt,
        "diff_truncated": diff_truncated,
    }


def _collect_merged_pr_provenance(*, workspace_dir: Path, pr_url: str) -> dict[str, str]:
    """Read the authoritative merge commit and target branch after merge."""

    raw = _run_gh_json(
        cwd=workspace_dir,
        argv=[
            "gh",
            "pr",
            "view",
            pr_url,
            "--json",
            "url,state,baseRefName,mergeCommit",
        ],
    )
    if not isinstance(raw, dict):
        raise RuntimeError("gh pr view returned non-object merge provenance")
    state = str(raw.get("state") or "").strip().upper()
    target_branch = raw.get("baseRefName")
    merge_commit_raw = raw.get("mergeCommit")
    merge_commit = (
        merge_commit_raw.get("oid") if isinstance(merge_commit_raw, dict) else None
    )
    if state != "MERGED":
        raise RuntimeError(f"PR did not report MERGED after merge command: {state!r}")
    if not isinstance(target_branch, str) or not target_branch.strip():
        raise RuntimeError("Merged PR provenance is missing baseRefName")
    if not isinstance(merge_commit, str) or not merge_commit.strip():
        raise RuntimeError("Merged PR provenance is missing mergeCommit.oid")
    return {
        "pr_url": str(raw.get("url") or pr_url).strip(),
        "target_branch": target_branch.strip(),
        "merged_commit": merge_commit.strip(),
    }


def _build_review_append_prompt(
    *,
    selected: SelectedTicket,
    handoff_summary: dict[str, Any] | None,
    pr_ref: dict[str, Any] | None,
    ci_gate: dict[str, Any] | None,
    pr_context: dict[str, Any],
) -> str:
    ticket_prompt_context = project_ticket_prompt_context(selected)
    pr_json = json.dumps(pr_context.get("pr", {}), indent=2, ensure_ascii=False)
    checks_json = json.dumps(pr_context.get("checks", []), indent=2, ensure_ascii=False)
    handoff_json = json.dumps(handoff_summary or {}, indent=2, ensure_ascii=False)
    pr_ref_json = json.dumps(pr_ref or {}, indent=2, ensure_ascii=False)
    ci_gate_json = json.dumps(ci_gate or {}, indent=2, ensure_ascii=False)
    changed_files = pr_context.get("changed_files", [])
    changed_file_lines = "\n".join(f"- {path}" for path in changed_files) if changed_files else "- <none>"
    diff_excerpt = str(pr_context.get("diff_excerpt") or "").rstrip()
    implementation_scope = pr_context.get("implementation_scope")
    scope_json = json.dumps(
        implementation_scope or {}, indent=2, ensure_ascii=False
    )

    return (
        "# Review task\n\n"
        "You are performing the causal acceptance review for a PR-backed implementation "
        "of an already-selected backlog ticket.\n"
        "Start with the researched failure mechanism, not the changed-file list. Determine "
        "whether the diff changes that mechanism or merely suppresses a visible symptom; "
        "whether verification actually exercises the ticket's bound original-scenario "
        "oracle; and which causal paths, if any, can still reproduce the problem. Then "
        "check defects, regressions, and CI. Do not redesign the ticket unless the evidence "
        "shows that the selected mechanism is wrong or the implementation diverges from it.\n\n"
        "Your report must use `task_run_v1` and must set `report.extensions.review_summary` to an object with:\n"
        "- `review_decision`: `approved` | `changes_requested` | `blocked`\n"
        "- `approach_alignment`: `aligned` | `diverged` | `unclear`\n"
        "- `mechanism_assessment`: `mechanism_addressed` | `symptom_only` | `unclear`\n"
        "- `original_scenario_oracle`: `exercised` | `not_exercised` | `unclear`\n"
        "- `causal_path_assessment`: `closed` | `residual` | `unclear`\n"
        "- `remaining_causal_paths`: an array naming every known residual path (empty only when none remain)\n"
        "- `scope_assessment`: `appropriate` | `excessive` | `unclear`\n"
        "- `rationale`: short string\n\n"
        "An approval requires `mechanism_addressed`, `exercised`, and `closed`. A test that "
        "does not replay the bound oracle is not original-scenario verification. Put every "
        "symptom-only change, unexercised oracle, or residual causal path in `issues[]`; use "
        "high or critical severity when it invalidates the claimed resolution. The "
        "`review_decision` is your causal/code acceptance judgment, not the mutable merge "
        "gate. A draft PR, pending CI, an infrastructure failure, or an unrelated/base-branch "
        "failure makes the PR operationally not merge-ready, but must not by itself turn an "
        "otherwise sound implementation into `changes_requested` or `blocked`. A CI failure "
        "caused by this diff is an implementation defect and should affect the decision. The "
        "runner computes merge readiness separately. Do not modify repository source files. "
        "Do not merge the PR.\n\n"
        "# Ticket markdown\n\n"
        f"{ticket_prompt_context.rstrip()}\n\n"
        "# Handoff summary\n\n"
        f"```json\n{handoff_json}\n```\n\n"
        "# PR reference\n\n"
        f"```json\n{pr_ref_json}\n```\n\n"
        "# CI gate artifact from implementation\n\n"
        f"```json\n{ci_gate_json}\n```\n\n"
        "# Current PR metadata\n\n"
        f"```json\n{pr_json}\n```\n\n"
        "# Current PR checks\n\n"
        f"```json\n{checks_json}\n```\n\n"
        "# PR diff excerpt\n\n"
        "```diff\n"
        f"{diff_excerpt}\n"
        "```\n\n"
        "# Scope advisory and immutable head/target gate\n\n"
        "Scope is secondary to causal correctness. The runner-owned receipt hard-blocks "
        "only a missing planned production target or a reviewed head that differs from the "
        "verified implementation head; you may not waive those failures. Extra paths, "
        "untouched non-production targets, and wider implementation breadth are advisory. "
        "Judge them briefly as necessary propagation/support work or inappropriate scope, "
        "without treating a narrow diff as evidence that the mechanism was addressed.\n\n"
        "## Changed files\n\n"
        f"{changed_file_lines}\n\n"
        "## Runner receipt\n\n"
        f"```json\n{scope_json}\n```\n"
    )


def _extract_agent_review_summary(report: dict[str, Any]) -> dict[str, Any]:
    extensions = report.get("extensions")
    if not isinstance(extensions, dict):
        raise ValueError("report.json missing extensions object")
    review_summary = extensions.get("review_summary")
    if not isinstance(review_summary, dict):
        raise ValueError("report.json missing extensions.review_summary object")

    out: dict[str, Any] = {}
    for key, allowed in (
        ("review_decision", {"approved", "changes_requested", "blocked"}),
        ("approach_alignment", {"aligned", "diverged", "unclear"}),
        (
            "mechanism_assessment",
            {"mechanism_addressed", "symptom_only", "unclear"},
        ),
        (
            "original_scenario_oracle",
            {"exercised", "not_exercised", "unclear"},
        ),
        ("causal_path_assessment", {"closed", "residual", "unclear"}),
        ("scope_assessment", {"appropriate", "excessive", "unclear"}),
    ):
        raw = review_summary.get(key)
        value = raw.strip().lower() if isinstance(raw, str) and raw.strip() else None
        if value not in allowed:
            raise ValueError(f"extensions.review_summary.{key} must be one of {sorted(allowed)!r}")
        out[key] = value

    remaining_raw = review_summary.get("remaining_causal_paths")
    if not isinstance(remaining_raw, list):
        raise ValueError(
            "extensions.review_summary.remaining_causal_paths must be an array"
        )
    remaining_causal_paths: list[str] = []
    for index, raw in enumerate(remaining_raw):
        if not isinstance(raw, str) or not raw.strip():
            raise ValueError(
                "extensions.review_summary.remaining_causal_paths"
                f"[{index}] must be a non-empty string"
            )
        remaining_causal_paths.append(raw.strip())
    if out["causal_path_assessment"] == "closed" and remaining_causal_paths:
        raise ValueError(
            "extensions.review_summary.remaining_causal_paths must be empty when "
            "causal_path_assessment is closed"
        )
    if out["causal_path_assessment"] == "residual" and not remaining_causal_paths:
        raise ValueError(
            "extensions.review_summary.remaining_causal_paths must name at least one "
            "path when causal_path_assessment is residual"
        )
    out["remaining_causal_paths"] = remaining_causal_paths

    rationale_raw = review_summary.get("rationale")
    if not isinstance(rationale_raw, str) or not rationale_raw.strip():
        raise ValueError("extensions.review_summary.rationale must be a non-empty string")
    out["rationale"] = rationale_raw.strip()
    return out


def _review_findings_from_report(report: dict[str, Any]) -> list[dict[str, Any]]:
    issues_raw = report.get("issues")
    issues = issues_raw if isinstance(issues_raw, list) else []
    findings: list[dict[str, Any]] = []
    for issue in issues:
        if not isinstance(issue, dict):
            continue
        severity_raw = issue.get("severity")
        severity = severity_raw if isinstance(severity_raw, str) and severity_raw.strip() else "info"
        title_raw = issue.get("title")
        details_raw = issue.get("details")
        if not isinstance(title_raw, str) or not title_raw.strip():
            continue
        if not isinstance(details_raw, str) or not details_raw.strip():
            continue
        findings.append(
            {
                "severity": severity.strip().lower(),
                "title": title_raw.strip(),
                "details": details_raw.strip(),
                "evidence": issue.get("evidence"),
                "suggested_fix": issue.get("suggested_fix"),
            }
        )
    return findings


def _stringify_review_detail(value: Any) -> str | None:
    if isinstance(value, str):
        text = value.strip()
        return text or None
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _build_pr_review_body(*, review_summary: dict[str, Any]) -> str:
    decision = str(review_summary.get("review_decision") or "").strip().lower()
    alignment = str(review_summary.get("approach_alignment") or "").strip().lower()
    mechanism = str(review_summary.get("mechanism_assessment") or "").strip().lower()
    oracle = str(review_summary.get("original_scenario_oracle") or "").strip().lower()
    causal_paths = str(review_summary.get("causal_path_assessment") or "").strip().lower()
    scope = str(review_summary.get("scope_assessment") or "").strip().lower()
    rationale = str(review_summary.get("rationale") or "").strip()
    merge_ready = bool(review_summary.get("merge_ready") is True)
    reviewed_head_oid = str(review_summary.get("reviewed_head_oid") or "").strip()
    findings_raw = review_summary.get("findings")
    findings = findings_raw if isinstance(findings_raw, list) else []

    lines = [
        "## Automated implementation review",
        "",
        f"- Decision: `{decision or 'unknown'}`",
        f"- Approach alignment: `{alignment or 'unknown'}`",
        f"- Researched mechanism: `{mechanism or 'unknown'}`",
        f"- Original-scenario oracle: `{oracle or 'unknown'}`",
        f"- Causal paths: `{causal_paths or 'unknown'}`",
        f"- Scope assessment: `{scope or 'unknown'}`",
        f"- Merge ready: `{'yes' if merge_ready else 'no'}`",
        f"- Reviewed commit: `{reviewed_head_oid or 'unknown'}`",
        "",
        "### Rationale",
        "",
        rationale or "No rationale provided.",
        "",
        "### Findings",
        "",
    ]
    if not findings:
        lines.append("No additional findings.")
    else:
        for index, finding_raw in enumerate(findings, start=1):
            if not isinstance(finding_raw, dict):
                continue
            severity = str(finding_raw.get("severity") or "info").strip().lower() or "info"
            title = str(finding_raw.get("title") or "Untitled finding").strip() or "Untitled finding"
            details = str(finding_raw.get("details") or "").strip() or "No details provided."
            lines.append(f"{index}. [{severity}] {title}")
            lines.append("")
            lines.append(details)
            evidence = _stringify_review_detail(finding_raw.get("evidence"))
            if evidence:
                lines.append("")
                lines.append(f"Evidence: {evidence}")
            suggested_fix = _stringify_review_detail(finding_raw.get("suggested_fix"))
            if suggested_fix:
                lines.append("")
                lines.append(f"Suggested fix: {suggested_fix}")
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _submit_pr_review(
    *,
    workspace_dir: Path,
    pr_url: str,
    review_run_dir: Path,
    review_summary: dict[str, Any],
) -> dict[str, Any]:
    body = _build_pr_review_body(review_summary=review_summary)
    body_path = review_run_dir / "pr_review.md"
    body_path.write_text(body, encoding="utf-8")
    proc = subprocess.run(
        [
            "gh",
            "pr",
            "review",
            pr_url,
            "--comment",
            "--body-file",
            str(body_path),
        ],
        cwd=str(workspace_dir),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    return {
        "schema_version": 1,
        "pr_url": pr_url,
        "event": "COMMENT",
        "submitted": proc.returncode == 0,
        "body_path": str(body_path),
        "stdout": proc.stdout,
        "stderr": proc.stderr,
        "returncode": int(proc.returncode),
        "submitted_at_utc": _utc_now_z() if proc.returncode == 0 else None,
    }


def _build_final_review_summary(
    *,
    selected: SelectedTicket,
    review_run_dir: Path,
    pr_url: str,
    pr_context: dict[str, Any],
    agent_summary: dict[str, Any],
    report: dict[str, Any],
) -> dict[str, Any]:
    pr_meta = pr_context.get("pr")
    if not isinstance(pr_meta, dict):
        raise ValueError("PR context missing metadata")
    pr_number_raw = pr_meta.get("number")
    pr_number = (
        int(pr_number_raw)
        if isinstance(pr_number_raw, int)
        else int(str(pr_number_raw).strip())
        if isinstance(pr_number_raw, str) and str(pr_number_raw).strip().isdigit()
        else None
    )
    mergeable_raw = pr_meta.get("mergeable")
    mergeable = str(mergeable_raw).strip().upper() == "MERGEABLE"
    is_draft = bool(pr_meta.get("isDraft") is True)
    pr_state = str(pr_meta.get("state") or "").strip().upper()
    reviewed_head_oid = str(pr_meta.get("headRefOid") or "").strip()
    if not reviewed_head_oid:
        raise ValueError("PR context missing headRefOid; review cannot be bound to a commit")
    ci_status = str(pr_context.get("ci_status") or "pending")
    ci_conclusion_raw = pr_context.get("ci_conclusion")
    ci_conclusion = (
        str(ci_conclusion_raw).strip().lower()
        if isinstance(ci_conclusion_raw, str) and str(ci_conclusion_raw).strip()
        else None
    )
    implementation_scope_raw = pr_context.get("implementation_scope")
    implementation_scope = (
        implementation_scope_raw
        if isinstance(implementation_scope_raw, dict)
        else {}
    )
    deterministic_scope_verified = implementation_scope.get("status") in {
        "verified",
        "not_applicable_external",
    }
    findings = _review_findings_from_report(report)
    blocking_findings = [
        finding
        for finding in findings
        if str(finding.get("severity") or "").strip().casefold()
        in {"error", "high", "critical", "blocker", "fatal"}
    ]
    causal_acceptance = (
        agent_summary["review_decision"] == "approved"
        and agent_summary["approach_alignment"] == "aligned"
        and agent_summary["mechanism_assessment"] == "mechanism_addressed"
        and agent_summary["original_scenario_oracle"] == "exercised"
        and agent_summary["causal_path_assessment"] == "closed"
        and not agent_summary["remaining_causal_paths"]
        and deterministic_scope_verified
        and not blocking_findings
    )
    merge_ready = (
        causal_acceptance
        and ci_conclusion == "success"
        and mergeable
        and not is_draft
        and pr_state == "OPEN"
    )
    return {
        "schema_version": 1,
        "ticket_fingerprint": selected.fingerprint,
        "ticket_path": str(selected.idea_path) if selected.idea_path is not None else None,
        "run_dir": str(review_run_dir),
        "pr_url": pr_url,
        "pr_number": pr_number,
        "pr_state": pr_state.lower() if pr_state else None,
        "pr_title": pr_meta.get("title"),
        "head_ref_name": pr_meta.get("headRefName"),
        "reviewed_head_oid": reviewed_head_oid,
        "base_ref_name": pr_meta.get("baseRefName"),
        "is_draft": is_draft,
        "mergeable": mergeable,
        "mergeable_state": mergeable_raw,
        "ci_status": ci_status,
        "ci_conclusion": ci_conclusion,
        "review_decision": agent_summary["review_decision"],
        "approach_alignment": agent_summary["approach_alignment"],
        "mechanism_assessment": agent_summary["mechanism_assessment"],
        "original_scenario_oracle": agent_summary["original_scenario_oracle"],
        "causal_path_assessment": agent_summary["causal_path_assessment"],
        "remaining_causal_paths": agent_summary["remaining_causal_paths"],
        "scope_assessment": agent_summary["scope_assessment"],
        "implementation_scope": implementation_scope,
        "deterministic_scope_verified": deterministic_scope_verified,
        "rationale": agent_summary["rationale"],
        "findings": findings,
        "blocking_finding_count": len(blocking_findings),
        "causal_acceptance": causal_acceptance,
        "merge_ready": merge_ready,
        "review_source": "automated",
        "reviewed_at_utc": _utc_now_z(),
    }


def _current_merge_gate_from_pr_context(pr_context: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
    pr_meta = pr_context.get("pr")
    if not isinstance(pr_meta, dict):
        raise ValueError("PR context missing metadata")
    mergeable_state = pr_meta.get("mergeable")
    mergeable = str(mergeable_state).strip().upper() == "MERGEABLE"
    is_draft = bool(pr_meta.get("isDraft") is True)
    pr_state = str(pr_meta.get("state") or "").strip().upper()
    ci_status = str(pr_context.get("ci_status") or "pending")
    ci_conclusion_raw = pr_context.get("ci_conclusion")
    ci_conclusion = (
        str(ci_conclusion_raw).strip().lower()
        if isinstance(ci_conclusion_raw, str) and str(ci_conclusion_raw).strip()
        else None
    )
    gate = {
        "pr_state": pr_state.lower() if pr_state else None,
        "is_draft": is_draft,
        "mergeable": mergeable,
        "mergeable_state": mergeable_state,
        "ci_status": ci_status,
        "ci_conclusion": ci_conclusion,
    }
    okay = pr_state == "OPEN" and not is_draft and mergeable and ci_conclusion == "success"
    return okay, gate


def _attach_deterministic_plan_scope(
    *,
    pr_context: dict[str, Any],
    target_contract: dict[str, Any],
    verified_implementation_head: str,
) -> dict[str, Any]:
    """Attach and enforce the runner-owned PR/plan scope receipt before model review."""

    changed_files_raw = pr_context.get("changed_files")
    changed_files = (
        [str(path) for path in changed_files_raw]
        if isinstance(changed_files_raw, list)
        else []
    )
    pr_meta_raw = pr_context.get("pr")
    pr_meta = pr_meta_raw if isinstance(pr_meta_raw, dict) else {}
    reviewed_head_oid = str(pr_meta.get("headRefOid") or "").strip()
    receipt = assess_pr_plan_scope(
        contract=target_contract,
        changed_files=changed_files,
        diff_text=str(pr_context.get("diff_full") or ""),
        reviewed_head_oid=reviewed_head_oid,
        verified_implementation_head=verified_implementation_head,
    )
    return {**pr_context, "implementation_scope": receipt}




__all__ = [name for name in globals() if not name.startswith("__")]
