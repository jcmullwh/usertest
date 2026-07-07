# ruff: noqa: E501,F401,F403,F405
from __future__ import annotations

from usertest_implement.shared import *


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

    success_states = {"SUCCESS", "SKIPPING", "NEUTRAL"}
    failure_states = {"FAILURE", "ERROR", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED"}
    pending_states = {"PENDING", "QUEUED", "IN_PROGRESS", "WAITING", "REQUESTED"}

    saw_pending = False
    for check in checks:
        state_raw = check.get("state")
        state = str(state_raw).strip().upper() if isinstance(state_raw, str) else ""
        if state in failure_states:
            return "completed", "failure"
        if state in pending_states or not state:
            saw_pending = True
        elif state not in success_states:
            saw_pending = True

    if saw_pending:
        return "pending", None
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
            "number,url,title,state,isDraft,headRefName,baseRefName,mergeable,statusCheckRollup",
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
        "diff_excerpt": diff_excerpt,
        "diff_truncated": diff_truncated,
    }


def _build_review_append_prompt(
    *,
    selected: SelectedTicket,
    handoff_summary: dict[str, Any] | None,
    pr_ref: dict[str, Any] | None,
    ci_gate: dict[str, Any] | None,
    pr_context: dict[str, Any],
) -> str:
    pr_json = json.dumps(pr_context.get("pr", {}), indent=2, ensure_ascii=False)
    checks_json = json.dumps(pr_context.get("checks", []), indent=2, ensure_ascii=False)
    handoff_json = json.dumps(handoff_summary or {}, indent=2, ensure_ascii=False)
    pr_ref_json = json.dumps(pr_ref or {}, indent=2, ensure_ascii=False)
    ci_gate_json = json.dumps(ci_gate or {}, indent=2, ensure_ascii=False)
    changed_files = pr_context.get("changed_files", [])
    changed_file_lines = "\n".join(f"- {path}" for path in changed_files) if changed_files else "- <none>"
    diff_excerpt = str(pr_context.get("diff_excerpt") or "").rstrip()

    return (
        "# Review task\n\n"
        "You are reviewing a PR-backed implementation of an already-selected backlog ticket.\n"
        "Do not redesign the ticket. Review only whether the PR stays aligned with the chosen approach,\n"
        "whether it adds unnecessary scope, whether there are implementation defects/regressions, and whether CI is green.\n\n"
        "Your report must use `task_run_v1` and must set `report.extensions.review_summary` to an object with:\n"
        "- `review_decision`: `approved` | `changes_requested` | `blocked`\n"
        "- `approach_alignment`: `aligned` | `diverged` | `unclear`\n"
        "- `scope_assessment`: `appropriate` | `excessive` | `unclear`\n"
        "- `rationale`: short string\n\n"
        "Use `issues[]` for findings. Do not modify repository source files. Do not merge the PR.\n\n"
        "# Ticket markdown\n\n"
        f"{selected.ticket_markdown.rstrip()}\n\n"
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
        "# Changed files\n\n"
        f"{changed_file_lines}\n\n"
        "# PR diff excerpt\n\n"
        "```diff\n"
        f"{diff_excerpt}\n"
        "```\n"
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
        ("scope_assessment", {"appropriate", "excessive", "unclear"}),
    ):
        raw = review_summary.get(key)
        value = raw.strip().lower() if isinstance(raw, str) and raw.strip() else None
        if value not in allowed:
            raise ValueError(f"extensions.review_summary.{key} must be one of {sorted(allowed)!r}")
        out[key] = value

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
    scope = str(review_summary.get("scope_assessment") or "").strip().lower()
    rationale = str(review_summary.get("rationale") or "").strip()
    merge_ready = bool(review_summary.get("merge_ready") is True)
    findings_raw = review_summary.get("findings")
    findings = findings_raw if isinstance(findings_raw, list) else []

    lines = [
        "## Automated implementation review",
        "",
        f"- Decision: `{decision or 'unknown'}`",
        f"- Approach alignment: `{alignment or 'unknown'}`",
        f"- Scope assessment: `{scope or 'unknown'}`",
        f"- Merge ready: `{'yes' if merge_ready else 'no'}`",
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
    ci_status = str(pr_context.get("ci_status") or "pending")
    ci_conclusion_raw = pr_context.get("ci_conclusion")
    ci_conclusion = (
        str(ci_conclusion_raw).strip().lower()
        if isinstance(ci_conclusion_raw, str) and str(ci_conclusion_raw).strip()
        else None
    )
    merge_ready = (
        agent_summary["review_decision"] == "approved"
        and agent_summary["approach_alignment"] == "aligned"
        and agent_summary["scope_assessment"] == "appropriate"
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
        "base_ref_name": pr_meta.get("baseRefName"),
        "is_draft": is_draft,
        "mergeable": mergeable,
        "mergeable_state": mergeable_raw,
        "ci_status": ci_status,
        "ci_conclusion": ci_conclusion,
        "review_decision": agent_summary["review_decision"],
        "approach_alignment": agent_summary["approach_alignment"],
        "scope_assessment": agent_summary["scope_assessment"],
        "rationale": agent_summary["rationale"],
        "findings": _review_findings_from_report(report),
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




__all__ = [name for name in globals() if not name.startswith("__")]
