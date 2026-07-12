from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import UUID

from usertest_implement.shared import SelectedTicket, _utc_now_z, _write_json

RESUME_STATE_ARTIFACT_NAME = "ticket_resume_state.json"

LIFECYCLE_AGENT_FAILED = "agent_failed"
LIFECYCLE_VERIFICATION_FAILED = "verification_failed"
LIFECYCLE_VERIFICATION_FAILED_RESUME_READY = "verification_failed_resume_ready"
LIFECYCLE_PUSH_FAILED = "push_failed"
LIFECYCLE_CI_FAILED = "ci_failed"
LIFECYCLE_PR_CREATION_FAILED = "pr_creation_failed"
LIFECYCLE_AWAITING_REVIEW = "awaiting_review"
LIFECYCLE_REVIEW_CHANGES_REQUESTED = "review_changes_requested"
LIFECYCLE_REVIEW_BLOCKED = "review_blocked"
LIFECYCLE_MERGE_READY = "merge_ready"
LIFECYCLE_COMPLETE = "complete"
LIFECYCLE_IMPLEMENTED_LOCAL = "implemented_local"
LIFECYCLE_IN_PROGRESS = "in_progress"

_RUN_EVIDENCE_FILES: tuple[tuple[str, str], ...] = (
    ("workspace_ref", "workspace_ref.json"),
    ("target_ref", "target_ref.json"),
    ("raw_events", "raw_events.jsonl"),
    ("ticket_ref", "ticket_ref.json"),
    ("verification", "verification.json"),
    ("verification_reuse", "verification_reuse.json"),
    ("agent_attempts", "agent_attempts.json"),
    ("git_ref", "git_ref.json"),
    ("push_ref", "push_ref.json"),
    ("ci_gate", "ci_gate.json"),
    ("pr_ref", "pr_ref.json"),
    ("handoff_summary", "handoff_summary.json"),
    ("report", "report.json"),
    ("report_validation_errors", "report_validation_errors.json"),
    ("error", "error.json"),
)

_REVIEW_EVIDENCE_FILES: tuple[tuple[str, str], ...] = (
    ("review_summary", "review_summary.json"),
    ("review_ref", "review_ref.json"),
    ("pr_review_ref", "pr_review_ref.json"),
    ("merge_ref", "merge_ref.json"),
)

_FAILURE_CONCLUSIONS = {
    "failure",
    "failed",
    "error",
    "timed_out",
    "timeout",
    "cancelled",
    "canceled",
    "action_required",
}


def _read_json(path: Path) -> Any | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None


def _clean_str(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def _path_str(path: Path | None) -> str | None:
    return str(path) if path is not None else None


def _canonical_uuid(value: Any) -> str | None:
    text = _clean_str(value)
    if text is None:
        return None
    try:
        canonical = str(UUID(text))
    except ValueError:
        return None
    return canonical if text == canonical else None


def implementation_author_continuity(run_dir: Path) -> dict[str, Any]:
    """Return runner-attested author/session provenance for a repair run.

    Codex emits the durable thread id in ``raw_events.jsonl``.  That id is the
    only safe continuation target: using the most recent thread would risk
    sending review feedback to a different author.  Missing provenance remains
    an explicit fresh-restart condition for legacy or incomplete runs; it is
    never represented as exact-session continuity.
    """

    target_ref = _read_json(run_dir / "target_ref.json")
    agent = _clean_str(target_ref.get("agent")) if isinstance(target_ref, dict) else None
    session_id: str | None = None
    raw_events_path = run_dir / "raw_events.jsonl"
    try:
        with raw_events_path.open("r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(event, dict) or event.get("type") != "thread.started":
                    continue
                session_id = _canonical_uuid(event.get("thread_id"))
                if session_id is not None:
                    break
    except (FileNotFoundError, OSError, UnicodeError):
        session_id = None

    exact_session_available = agent == "codex" and session_id is not None
    if exact_session_available:
        status = "exact_session_available"
    elif agent == "codex":
        status = "author_session_unavailable"
    elif agent is None:
        status = "author_provenance_unavailable"
    else:
        status = "agent_continuation_unsupported"
    return {
        "agent": agent,
        "session_id": session_id,
        "status": status,
        "exact_session_available": exact_session_available,
        "agent_source": (
            str(run_dir / "target_ref.json")
            if (run_dir / "target_ref.json").exists()
            else None
        ),
        "session_source": str(raw_events_path) if raw_events_path.exists() else None,
    }


def _existing_evidence_paths(*, run_dir: Path, review_run_dir: Path | None) -> dict[str, str]:
    paths: dict[str, str] = {}
    for key, filename in _RUN_EVIDENCE_FILES:
        path = run_dir / filename
        if path.exists():
            paths[key] = str(path)
    if review_run_dir is not None:
        for key, filename in _REVIEW_EVIDENCE_FILES:
            path = review_run_dir / filename
            if path.exists():
                paths[key] = str(path)
    return paths


def _workspace_path(*, run_dir: Path, workspace_ref: dict[str, Any] | None) -> str | None:
    if isinstance(workspace_ref, dict):
        raw = _clean_str(workspace_ref.get("workspace_dir"))
        if raw is not None:
            return raw
        raw = _clean_str(workspace_ref.get("workspace_path"))
        if raw is not None:
            return raw
    return None


def _branch_from_sources(
    *,
    branch: str | None,
    git_ref: dict[str, Any] | None,
    push_ref: dict[str, Any] | None,
    pr_ref: dict[str, Any] | None,
    handoff_summary: dict[str, Any] | None,
    review_summary: dict[str, Any] | None,
) -> str | None:
    for value in (
        branch,
        git_ref.get("branch") if isinstance(git_ref, dict) else None,
        push_ref.get("branch") if isinstance(push_ref, dict) else None,
        pr_ref.get("branch") if isinstance(pr_ref, dict) else None,
        handoff_summary.get("branch") if isinstance(handoff_summary, dict) else None,
        review_summary.get("head_ref_name") if isinstance(review_summary, dict) else None,
    ):
        cleaned = _clean_str(value)
        if cleaned is not None:
            return cleaned
    return None


def _pr_url_from_sources(
    *,
    pr_ref: dict[str, Any] | None,
    handoff_summary: dict[str, Any] | None,
    review_summary: dict[str, Any] | None,
) -> str | None:
    for value in (
        handoff_summary.get("pr_url") if isinstance(handoff_summary, dict) else None,
        pr_ref.get("url") if isinstance(pr_ref, dict) else None,
        review_summary.get("pr_url") if isinstance(review_summary, dict) else None,
    ):
        cleaned = _clean_str(value)
        if cleaned is not None:
            return cleaned
    return None


def _first_failing_verification_command(verification: dict[str, Any] | None) -> str | None:
    if not isinstance(verification, dict):
        return None
    commands = verification.get("commands")
    if not isinstance(commands, list):
        return None
    for item in commands:
        if not isinstance(item, dict):
            continue
        exit_code = item.get("exit_code")
        if isinstance(exit_code, int) and exit_code != 0:
            return _clean_str(item.get("command"))
    return None


def _ci_failed(*, ci_gate: dict[str, Any] | None, handoff_summary: dict[str, Any] | None) -> bool:
    if isinstance(ci_gate, dict):
        if ci_gate.get("passed") is False:
            return True
        conclusion = _clean_str(ci_gate.get("conclusion"))
        if conclusion is not None and conclusion.lower() in _FAILURE_CONCLUSIONS:
            return True
    if isinstance(handoff_summary, dict):
        conclusion = _clean_str(handoff_summary.get("ci_conclusion"))
        if conclusion is not None and conclusion.lower() in _FAILURE_CONCLUSIONS:
            return True
    return False


def _classify_resume_state(
    *,
    run_dir: Path,
    review_run_dir: Path | None,
    exit_code: int | None,
    verification: dict[str, Any] | None,
    git_ref: dict[str, Any] | None,
    push_ref: dict[str, Any] | None,
    ci_gate: dict[str, Any] | None,
    pr_ref: dict[str, Any] | None,
    handoff_summary: dict[str, Any] | None,
    review_summary: dict[str, Any] | None,
    merge_ref: dict[str, Any] | None,
) -> tuple[str, str | None]:
    if isinstance(merge_ref, dict) and merge_ref.get("merged") is True:
        return LIFECYCLE_COMPLETE, None

    if isinstance(review_summary, dict):
        decision = (_clean_str(review_summary.get("review_decision")) or "").lower()
        if decision == "changes_requested":
            rationale = _clean_str(review_summary.get("rationale"))
            return LIFECYCLE_REVIEW_CHANGES_REQUESTED, rationale or "Review requested changes."
        if decision == "blocked":
            rationale = _clean_str(review_summary.get("rationale"))
            return LIFECYCLE_REVIEW_BLOCKED, rationale or "Review is blocked."
        if review_summary.get("merge_ready") is True:
            return LIFECYCLE_MERGE_READY, None

    if isinstance(handoff_summary, dict):
        decision = (_clean_str(handoff_summary.get("review_decision")) or "").lower()
        if decision == "changes_requested":
            return LIFECYCLE_REVIEW_CHANGES_REQUESTED, "Review requested changes."
        if decision == "blocked":
            reason = _clean_str(handoff_summary.get("review_error")) or "Review is blocked."
            return LIFECYCLE_REVIEW_BLOCKED, reason
        if handoff_summary.get("review_merge_ready") is True:
            return LIFECYCLE_MERGE_READY, None

    if isinstance(verification, dict) and verification.get("passed") is False:
        command = _first_failing_verification_command(verification)
        if command is not None:
            return LIFECYCLE_VERIFICATION_FAILED_RESUME_READY, f"Verification failed: {command}"
        return LIFECYCLE_VERIFICATION_FAILED_RESUME_READY, "Verification failed."

    if isinstance(push_ref, dict) and push_ref.get("error"):
        return LIFECYCLE_PUSH_FAILED, f"Push failed: {push_ref.get('error')}"

    if _ci_failed(ci_gate=ci_gate, handoff_summary=handoff_summary):
        error = _clean_str(ci_gate.get("error")) if isinstance(ci_gate, dict) else None
        run_url = _clean_str(ci_gate.get("run_url")) if isinstance(ci_gate, dict) else None
        detail = error or run_url or "CI gate failed."
        return LIFECYCLE_CI_FAILED, detail

    if isinstance(pr_ref, dict):
        requested = pr_ref.get("requested") is True
        created = pr_ref.get("created") is True
        error = _clean_str(pr_ref.get("error"))
        if requested and not created:
            reason = error or "PR creation was requested but no PR was created."
            return LIFECYCLE_PR_CREATION_FAILED, reason
        if error is not None and not created:
            return LIFECYCLE_PR_CREATION_FAILED, error

    if isinstance(handoff_summary, dict):
        if (
            handoff_summary.get("pr_requested") is True
            and handoff_summary.get("pr_created") is not True
        ):
            return LIFECYCLE_PR_CREATION_FAILED, "PR creation was requested but no PR was created."

    if exit_code is not None and int(exit_code) != 0:
        return LIFECYCLE_AGENT_FAILED, f"Implementation run exited with code {int(exit_code)}."

    if isinstance(pr_ref, dict) and pr_ref.get("created") is True:
        return LIFECYCLE_AWAITING_REVIEW, "PR is open and awaiting implementation review."

    if isinstance(handoff_summary, dict):
        if handoff_summary.get("final_status") == "success":
            if handoff_summary.get("pr_requested") is True:
                return LIFECYCLE_AWAITING_REVIEW, "PR is open and awaiting implementation review."
            return LIFECYCLE_IMPLEMENTED_LOCAL, None

    return LIFECYCLE_IN_PROGRESS, "Run has not produced a terminal resume state yet."


def build_ticket_resume_state(
    *,
    selected: SelectedTicket,
    run_dir: Path,
    owner_root: Path | None,
    branch: str | None = None,
    exit_code: int | None = None,
    review_run_dir: Path | None = None,
    ticket_path_override: Path | None = None,
) -> dict[str, Any]:
    workspace_ref = _read_json(run_dir / "workspace_ref.json")
    ticket_ref = _read_json(run_dir / "ticket_ref.json")
    verification = _read_json(run_dir / "verification.json")
    git_ref = _read_json(run_dir / "git_ref.json")
    push_ref = _read_json(run_dir / "push_ref.json")
    ci_gate = _read_json(run_dir / "ci_gate.json")
    pr_ref = _read_json(run_dir / "pr_ref.json")
    handoff_summary = _read_json(run_dir / "handoff_summary.json")
    review_summary = (
        _read_json(review_run_dir / "review_summary.json")
        if review_run_dir is not None
        else None
    )
    merge_ref = (
        _read_json(review_run_dir / "merge_ref.json")
        if review_run_dir is not None
        else None
    )

    workspace_ref_dict = workspace_ref if isinstance(workspace_ref, dict) else None
    verification_dict = verification if isinstance(verification, dict) else None
    git_ref_dict = git_ref if isinstance(git_ref, dict) else None
    push_ref_dict = push_ref if isinstance(push_ref, dict) else None
    ci_gate_dict = ci_gate if isinstance(ci_gate, dict) else None
    pr_ref_dict = pr_ref if isinstance(pr_ref, dict) else None
    handoff_summary_dict = handoff_summary if isinstance(handoff_summary, dict) else None
    review_summary_dict = review_summary if isinstance(review_summary, dict) else None
    merge_ref_dict = merge_ref if isinstance(merge_ref, dict) else None
    author_continuity = implementation_author_continuity(run_dir)

    lifecycle_state, blocking_reason = _classify_resume_state(
        run_dir=run_dir,
        review_run_dir=review_run_dir,
        exit_code=exit_code,
        verification=verification_dict,
        git_ref=git_ref_dict,
        push_ref=push_ref_dict,
        ci_gate=ci_gate_dict,
        pr_ref=pr_ref_dict,
        handoff_summary=handoff_summary_dict,
        review_summary=review_summary_dict,
        merge_ref=merge_ref_dict,
    )

    ticket_path = _path_str(ticket_path_override) or _path_str(selected.idea_path)
    if isinstance(ticket_ref, dict):
        owner_repo = ticket_ref.get("owner_repo")
        if isinstance(owner_repo, dict):
            ticket_path = ticket_path or _clean_str(owner_repo.get("idea_path"))

    return {
        "schema_version": 1,
        "kind": "ticket_resume_state",
        "generated_at_utc": _utc_now_z(),
        "ticket": {
            "fingerprint": selected.fingerprint,
            "path": ticket_path,
            "title": selected.title,
            "export_kind": selected.export_kind,
        },
        "owner_root": str(owner_root) if owner_root is not None else None,
        "run_dir": str(run_dir),
        "workspace_path": _workspace_path(run_dir=run_dir, workspace_ref=workspace_ref_dict),
        "branch": _branch_from_sources(
            branch=branch,
            git_ref=git_ref_dict,
            push_ref=push_ref_dict,
            pr_ref=pr_ref_dict,
            handoff_summary=handoff_summary_dict,
            review_summary=review_summary_dict,
        ),
        "pr_url": _pr_url_from_sources(
            pr_ref=pr_ref_dict,
            handoff_summary=handoff_summary_dict,
            review_summary=review_summary_dict,
        ),
        "review_run_dir": str(review_run_dir) if review_run_dir is not None else None,
        "implementation_author": author_continuity,
        "lifecycle_state": lifecycle_state,
        "blocking_reason": blocking_reason,
        "source_evidence_paths": _existing_evidence_paths(
            run_dir=run_dir,
            review_run_dir=review_run_dir,
        ),
    }


def write_ticket_resume_state(
    *,
    selected: SelectedTicket,
    run_dir: Path,
    owner_root: Path | None,
    branch: str | None = None,
    exit_code: int | None = None,
    review_run_dir: Path | None = None,
    ticket_path_override: Path | None = None,
) -> dict[str, Any]:
    state = build_ticket_resume_state(
        selected=selected,
        run_dir=run_dir,
        owner_root=owner_root,
        branch=branch,
        exit_code=exit_code,
        review_run_dir=review_run_dir,
        ticket_path_override=ticket_path_override,
    )
    _write_json(run_dir / RESUME_STATE_ARTIFACT_NAME, state)
    return state
