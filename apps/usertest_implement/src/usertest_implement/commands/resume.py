# ruff: noqa: E501,F401,F403,F405
from __future__ import annotations

from runner_core.verification_prompts import _build_verification_followup_prompt

from usertest_implement.ci import _ci_timeout_seconds_arg, _git_head_sha, _wait_for_ci_success
from usertest_implement.implementation_provenance import record_verified_implementation_head
from usertest_implement.resume_state import (
    LIFECYCLE_CI_FAILED,
    LIFECYCLE_REVIEW_CHANGES_REQUESTED,
    LIFECYCLE_VERIFICATION_FAILED,
    LIFECYCLE_VERIFICATION_FAILED_RESUME_READY,
    RESUME_STATE_ARTIFACT_NAME,
    implementation_author_continuity,
    write_ticket_resume_state,
)
from usertest_implement.review_context import (
    _coerce_pr_url,
    _collect_pr_review_context,
    _current_merge_gate_from_pr_context,
)
from usertest_implement.shared import *

_REQUIRED_RESUME_ARTIFACTS: tuple[str, ...] = (
    "verification.json",
    "verification_reuse.json",
    "agent_attempts.json",
    "workspace_ref.json",
    "ticket_ref.json",
)
_VALID_VERIFICATION_RESUME_STATES = {
    LIFECYCLE_VERIFICATION_FAILED_RESUME_READY,
    # Backward-compatible with runs written before the durable resume-ready state existed.
    LIFECYCLE_VERIFICATION_FAILED,
}
_VALID_PR_RESUME_STATES = {
    LIFECYCLE_CI_FAILED,
    LIFECYCLE_REVIEW_CHANGES_REQUESTED,
}
_PROMPT_ARTIFACT_MAX_CHARS = 5000
_PROMPT_REPORT_MAX_CHARS = 6000


def _clean_str(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def _read_text_if_present(path: Path, *, max_chars: int) -> str | None:
    try:
        text = path.read_text(encoding="utf-8").strip()
    except (FileNotFoundError, OSError, UnicodeError):
        return None
    if not text:
        return None
    if len(text) <= max_chars:
        return text
    return text[-max_chars:] + "\n...[truncated to tail]"


def _implementation_author_for_resume(
    *, run_dir: Path, resume_state: dict[str, Any]
) -> dict[str, Any]:
    recorded = resume_state.get("implementation_author")
    if isinstance(recorded, dict):
        agent = _clean_str(recorded.get("agent"))
        session_id = _clean_str(recorded.get("session_id"))
        if agent is not None or session_id is not None:
            exact = agent == "codex" and session_id is not None
            return {
                **recorded,
                "agent": agent,
                "session_id": session_id,
                "exact_session_available": exact,
                "status": (
                    "exact_session_available"
                    if exact
                    else _clean_str(recorded.get("status")) or "author_session_unavailable"
                ),
            }
    return implementation_author_continuity(run_dir)


def _resume_agent_continuity(
    *, run_dir: Path, resume_state: dict[str, Any], requested_agent: str
) -> tuple[str, str | None, dict[str, Any]]:
    author = _implementation_author_for_resume(run_dir=run_dir, resume_state=resume_state)
    author_agent = _clean_str(author.get("agent"))
    author_session_id = _clean_str(author.get("session_id"))
    if author_agent == "codex" and author_session_id is not None:
        return (
            "codex",
            author_session_id,
            {
                **author,
                "status": "exact_author_session",
                "requested_agent": requested_agent,
                "effective_agent": "codex",
                "fresh_restart": False,
            },
        )
    # Legacy/incomplete runs may not have a resumable author session. Preserve
    # throughput, but make the fresh restart explicit instead of claiming that
    # the author received the feedback.
    return (
        requested_agent,
        None,
        {
            **author,
            "status": _clean_str(author.get("status")) or "author_session_unavailable",
            "requested_agent": requested_agent,
            "effective_agent": requested_agent,
            "fresh_restart": True,
        },
    )


def _artifact_status(run_dir: Path, filename: str) -> str:
    path = run_dir / filename
    if not path.exists():
        return f"missing: {path}"
    data = _read_json(path)
    if isinstance(data, dict):
        compact = json.dumps(data, ensure_ascii=False, sort_keys=True)
        if len(compact) > _PROMPT_ARTIFACT_MAX_CHARS:
            compact = compact[:_PROMPT_ARTIFACT_MAX_CHARS] + "...[truncated]"
        return compact
    text = _read_text_if_present(path, max_chars=_PROMPT_ARTIFACT_MAX_CHARS)
    return text or f"present but unreadable/non-json: {path}"


def _prior_last_message_from_attempts(run_dir: Path, attempts: dict[str, Any] | None) -> str:
    candidates: list[Path] = []
    if isinstance(attempts, dict):
        raw_attempts = attempts.get("attempts")
        if isinstance(raw_attempts, list):
            for item in reversed(raw_attempts):
                if not isinstance(item, dict):
                    continue
                raw_path = _clean_str(item.get("last_message_path"))
                if raw_path is None:
                    continue
                path = Path(raw_path)
                if not path.is_absolute():
                    path = run_dir / raw_path
                candidates.append(path)
                break
    candidates.append(run_dir / "agent_last_message.txt")
    for path in candidates:
        text = _read_text_if_present(path, max_chars=_PROMPT_REPORT_MAX_CHARS)
        if text:
            return text
    return ""


def _prior_report_block(run_dir: Path) -> str:
    report_text = _read_text_if_present(run_dir / "report.json", max_chars=_PROMPT_REPORT_MAX_CHARS)
    if report_text:
        return "Prior report output (report.json):\n```json\n" + report_text + "\n```"
    last = _read_text_if_present(run_dir / "agent_last_message.txt", max_chars=_PROMPT_REPORT_MAX_CHARS)
    if last:
        return "Prior assistant output (agent_last_message.txt):\n```\n" + last + "\n```"
    return "Prior report output: (not present)"


def _schema_for_resume(run_dir: Path) -> dict[str, Any]:
    schema = _read_json(run_dir / "report.schema.json")
    return schema if isinstance(schema, dict) else {}


def _verification_summary_for_prompt(
    *,
    verification: dict[str, Any],
    verification_reuse: dict[str, Any] | None,
) -> dict[str, Any]:
    summary = dict(verification)
    if "status" not in summary and "terminal_reason" not in summary:
        summary["status"] = "failed"
    if "failure_reason" not in summary:
        summary["failure_reason"] = "verification_failed"
    if isinstance(verification_reuse, dict):
        artifacts = _clean_str(verification_reuse.get("selected_artifacts_dir"))
        if artifacts is not None:
            summary.setdefault("artifacts_dir_for_agent", artifacts)
    return summary


def _build_verification_resume_prompt(
    *,
    original_run_dir: Path,
    resume_state: dict[str, Any],
    verification: dict[str, Any],
    verification_reuse: dict[str, Any] | None,
    agent_attempts: dict[str, Any] | None,
    workspace_ref: dict[str, Any],
    ticket_ref: dict[str, Any],
) -> str:
    ticket = resume_state.get("ticket") if isinstance(resume_state.get("ticket"), dict) else {}
    fingerprint = _clean_str(ticket.get("fingerprint")) or _clean_str(ticket_ref.get("fingerprint")) or "unknown"
    title = _clean_str(ticket.get("title")) or _clean_str(ticket_ref.get("title")) or "Untitled ticket"
    branch = _clean_str(resume_state.get("branch")) or "(not recorded)"
    workspace_path = _clean_str(resume_state.get("workspace_path")) or _clean_str(workspace_ref.get("workspace_dir")) or "(not recorded)"

    attempt_count = 0
    if isinstance(agent_attempts, dict) and isinstance(agent_attempts.get("attempts"), list):
        attempt_count = len(agent_attempts["attempts"])

    artifact_lines = [
        f"- {name}: {_artifact_status(original_run_dir, name)}" for name in _REQUIRED_RESUME_ARTIFACTS
    ]
    artifact_block = "\n".join(artifact_lines)

    base_prompt = (
        "Resume a previous ticket implementation after the verification gate failed.\n"
        "Do not restart the original full ticket prompt from scratch. Use the current workspace "
        "state and the structured verification evidence below to make the smallest coherent fix "
        "that causes the failed verification checks to pass. Preserve unrelated work.\n\n"
        f"Original run dir: {original_run_dir}\n"
        f"Original resume state: {original_run_dir / RESUME_STATE_ARTIFACT_NAME}\n"
        f"Ticket: {fingerprint} — {title}\n"
        f"Recorded branch: {branch}\n"
        f"Recorded workspace: {workspace_path}\n"
        f"Prior agent attempts: {attempt_count}\n\n"
        "Structured resume artifacts:\n"
        f"{artifact_block}\n\n"
        f"{_prior_report_block(original_run_dir)}\n\n"
        "When you finish, return the required JSON report only. Include in the report summary that "
        "this was a verification-failure resume and mention the original run dir."
    )
    return _build_verification_followup_prompt(
        base_prompt=base_prompt,
        verification_summary=_verification_summary_for_prompt(
            verification=verification,
            verification_reuse=verification_reuse,
        ),
        schema_dict=_schema_for_resume(original_run_dir),
        prior_last_message_text=_prior_last_message_from_attempts(original_run_dir, agent_attempts),
        attempt_number=1,
    )



def _review_run_dir_from_state(resume_state: dict[str, Any]) -> Path | None:
    evidence = resume_state.get("source_evidence_paths")
    if isinstance(evidence, dict):
        for key in ("review_summary", "review_ref", "pr_review_ref"):
            raw = _clean_str(evidence.get(key))
            if raw is not None:
                return Path(raw).parent
    return None


def _load_review_summary_for_resume(*, resume_state: dict[str, Any]) -> dict[str, Any] | None:
    evidence = resume_state.get("source_evidence_paths")
    if isinstance(evidence, dict):
        raw = _clean_str(evidence.get("review_summary"))
        if raw is not None:
            data = _read_json(Path(raw))
            if isinstance(data, dict):
                return data
    review_run_dir = _review_run_dir_from_state(resume_state)
    if review_run_dir is not None:
        data = _read_json(review_run_dir / "review_summary.json")
        if isinstance(data, dict):
            return data
    return None


def _pr_url_from_resume_artifacts(
    *,
    run_dir: Path,
    resume_state: dict[str, Any],
    ticket_ref: dict[str, Any],
) -> str | None:
    raw = _clean_str(resume_state.get("pr_url"))
    if raw is not None:
        return raw
    handoff_summary = _read_json(run_dir / "handoff_summary.json")
    pr_ref = _read_json(run_dir / "pr_ref.json")
    review_summary = _load_review_summary_for_resume(resume_state=resume_state)
    pr_url = _coerce_pr_url(
        handoff_summary=handoff_summary if isinstance(handoff_summary, dict) else None,
        pr_ref=pr_ref if isinstance(pr_ref, dict) else None,
    )
    if pr_url is not None:
        return pr_url
    if isinstance(review_summary, dict):
        raw = _clean_str(review_summary.get("pr_url"))
        if raw is not None:
            return raw
    raw = _clean_str(ticket_ref.get("pr_url"))
    return raw


def _owner_root_from_resume(*, resume_state: dict[str, Any], ticket_ref: dict[str, Any]) -> Path | None:
    raw = _clean_str(resume_state.get("owner_root"))
    if raw is not None:
        path = Path(raw)
        if path.exists():
            return path
    owner_repo = ticket_ref.get("owner_repo") if isinstance(ticket_ref.get("owner_repo"), dict) else {}
    raw = _clean_str(owner_repo.get("root"))
    if raw is not None:
        path = Path(raw)
        if path.exists():
            return path
    return None


def _pr_context_workspace(
    *,
    resume_state: dict[str, Any],
    workspace_ref: dict[str, Any],
    ticket_ref: dict[str, Any],
) -> Path:
    owner_root = _owner_root_from_resume(resume_state=resume_state, ticket_ref=ticket_ref)
    if owner_root is not None:
        return owner_root
    for raw in (workspace_ref.get("workspace_dir"), resume_state.get("workspace_path")):
        cleaned = _clean_str(raw)
        if cleaned is None:
            continue
        path = Path(cleaned)
        if path.exists():
            return path
    raise SystemExit(
        "Cannot resume PR-backed run: no existing owner/workspace path is available to refresh PR state with gh. "
        "Pass --repo for agent checkout after restoring a local owner/workspace, or rerun from a machine with the repository present."
    )


def _review_findings_for_resume(review_summary: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(review_summary, dict):
        return []
    decision = (_clean_str(review_summary.get("review_decision")) or "").lower()
    if decision != "changes_requested":
        return []
    findings = review_summary.get("findings")
    if not isinstance(findings, list):
        return []
    return [item for item in findings if isinstance(item, dict)]


def _failing_check_pointers(pr_context: dict[str, Any]) -> list[dict[str, Any]]:
    checks = pr_context.get("checks")
    if not isinstance(checks, list):
        return []
    failure_states = {"FAILURE", "ERROR", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED"}
    out: list[dict[str, Any]] = []
    for check in checks:
        if not isinstance(check, dict):
            continue
        state = str(check.get("state") or "").strip().upper()
        if state not in failure_states:
            continue
        out.append(
            {
                "name": check.get("name"),
                "state": check.get("state"),
                "link": check.get("link"),
                "bucket": check.get("bucket"),
                "startedAt": check.get("startedAt"),
                "completedAt": check.get("completedAt"),
            }
        )
    return out


def _artifact_path_map_for_pr_resume(
    *,
    run_dir: Path,
    review_run_dir: Path | None,
    state_path: Path,
) -> dict[str, str]:
    artifacts: dict[str, str] = {"resume_state": str(state_path)}
    for name in (
        "ticket_ref.json",
        "workspace_ref.json",
        "handoff_summary.json",
        "pr_ref.json",
        "ci_gate.json",
        "git_ref.json",
        "push_ref.json",
        "report.json",
    ):
        path = run_dir / name
        if path.exists():
            artifacts[name.removesuffix(".json")] = str(path)
    if review_run_dir is not None:
        for name in ("review_summary.json", "review_ref.json", "pr_review_ref.json"):
            path = review_run_dir / name
            if path.exists():
                artifacts[name.removesuffix(".json")] = str(path)
    return artifacts


def _build_pr_resume_prompt(
    *,
    original_run_dir: Path,
    state_path: Path,
    resume_state: dict[str, Any],
    ticket_ref: dict[str, Any],
    selected: SelectedTicket,
    pr_url: str,
    pr_context: dict[str, Any],
    review_summary: dict[str, Any] | None,
    branch: str,
) -> str:
    pr_json = json.dumps(pr_context.get("pr", {}), indent=2, ensure_ascii=False)
    checks_json = json.dumps(pr_context.get("checks", []), indent=2, ensure_ascii=False)
    failing_checks_json = json.dumps(_failing_check_pointers(pr_context), indent=2, ensure_ascii=False)
    review_findings = _review_findings_for_resume(review_summary)
    review_findings_json = json.dumps(review_findings, indent=2, ensure_ascii=False)
    review_summary_json = json.dumps(review_summary or {}, indent=2, ensure_ascii=False)
    artifact_paths = _artifact_path_map_for_pr_resume(
        run_dir=original_run_dir,
        review_run_dir=_review_run_dir_from_state(resume_state),
        state_path=state_path,
    )
    artifact_paths_json = json.dumps(artifact_paths, indent=2, ensure_ascii=False)
    handoff_summary = _read_json(original_run_dir / "handoff_summary.json")
    handoff_json = json.dumps(handoff_summary if isinstance(handoff_summary, dict) else {}, indent=2, ensure_ascii=False)
    ci_gate = _read_json(original_run_dir / "ci_gate.json")
    ci_gate_json = json.dumps(ci_gate if isinstance(ci_gate, dict) else {}, indent=2, ensure_ascii=False)
    changed_files = pr_context.get("changed_files")
    changed_file_lines = (
        "\n".join(f"- {path}" for path in changed_files)
        if isinstance(changed_files, list) and changed_files
        else "- <none>"
    )
    diff_excerpt = str(pr_context.get("diff_excerpt") or "").rstrip()
    blocking_reason = _clean_str(resume_state.get("blocking_reason")) or "PR is blocked."

    return (
        "# Resume PR-backed ticket implementation\n\n"
        "Resume an existing open pull request after review requested changes or CI failed. "
        "Do not create a duplicate PR. Commit any necessary fixes to the existing PR branch and preserve unrelated work.\n\n"
        f"Original run dir: {original_run_dir}\n"
        f"Original resume state: {state_path}\n"
        f"Lifecycle state: {_clean_str(resume_state.get('lifecycle_state')) or 'unknown'}\n"
        f"Blocking reason: {blocking_reason}\n"
        f"Existing PR: {pr_url}\n"
        f"Existing PR branch: {branch}\n\n"
        "# Ticket context\n\n"
        f"Fingerprint: {selected.fingerprint}\n"
        f"Title: {selected.title or ticket_ref.get('title') or 'Untitled ticket'}\n\n"
        f"{selected.ticket_markdown.rstrip()}\n\n"
        "# Prior implementation summary\n\n"
        f"```json\n{handoff_json}\n```\n\n"
        f"{_prior_report_block(original_run_dir)}\n\n"
        "# Current PR metadata (refreshed immediately before this prompt)\n\n"
        f"```json\n{pr_json}\n```\n\n"
        "# Current PR checks (refreshed immediately before this prompt)\n\n"
        f"```json\n{checks_json}\n```\n\n"
        "# Failing check/log pointers\n\n"
        f"```json\n{failing_checks_json}\n```\n\n"
        "# Prior CI gate artifact\n\n"
        f"```json\n{ci_gate_json}\n```\n\n"
        "# Unresolved review findings\n\n"
        "These are findings from the latest recorded automated review that still requested changes. "
        "Treat them as unresolved unless the refreshed PR state clearly proves they are obsolete.\n\n"
        f"```json\n{review_findings_json}\n```\n\n"
        "# Latest recorded review summary\n\n"
        f"```json\n{review_summary_json}\n```\n\n"
        "# Run artifact paths\n\n"
        f"```json\n{artifact_paths_json}\n```\n\n"
        "# Changed files\n\n"
        f"{changed_file_lines}\n\n"
        "# Current PR diff excerpt\n\n"
        f"```diff\n{diff_excerpt}\n```\n\n"
        "# Required outcome\n\n"
        "Make the smallest coherent fix. When complete, return the required JSON report only and mention "
        "that this was a PR resume for the existing PR branch."
    )


def _current_pr_resume_noop_reason(
    *,
    pr_context: dict[str, Any],
    review_summary: dict[str, Any] | None,
) -> str | None:
    current_merge_ready, current_gate = _current_merge_gate_from_pr_context(pr_context)
    if not current_merge_ready:
        return None
    findings = _review_findings_for_resume(review_summary)
    if findings:
        pr_meta = pr_context.get("pr") if isinstance(pr_context.get("pr"), dict) else {}
        review_decision = str(pr_meta.get("reviewDecision") or "").strip().upper()
        if review_decision != "APPROVED":
            return None
    return "Current PR merge gate is already green: " + json.dumps(current_gate, ensure_ascii=False)


def _resolve_pr_resume_target(
    *,
    args: argparse.Namespace,
    run_dir: Path,
    resume_state: dict[str, Any],
    workspace_ref: dict[str, Any],
    ticket_ref: dict[str, Any],
    pr_context: dict[str, Any],
) -> tuple[str, str, Path | None, str]:
    pr_meta = pr_context.get("pr") if isinstance(pr_context.get("pr"), dict) else {}
    branch = _clean_str(getattr(args, "ref", None)) or _clean_str(pr_meta.get("headRefName")) or _clean_str(resume_state.get("branch"))
    if branch is None:
        raise SystemExit("Cannot resume PR-backed run: current PR metadata and resume state are missing the PR branch.")
    repo_input, _old_ref, resume_workspace_dir, workspace_strategy = _resolve_resume_target(
        args=args,
        run_dir=run_dir,
        resume_state={**resume_state, "branch": branch},
        workspace_ref=workspace_ref,
        ticket_ref=ticket_ref,
    )
    return repo_input, branch, resume_workspace_dir, workspace_strategy

def _selected_from_resume_state(
    *,
    resume_state: dict[str, Any],
    ticket_ref: dict[str, Any],
) -> SelectedTicket:
    ticket = resume_state.get("ticket") if isinstance(resume_state.get("ticket"), dict) else {}
    owner_repo = ticket_ref.get("owner_repo") if isinstance(ticket_ref.get("owner_repo"), dict) else {}
    path_raw = _clean_str(ticket.get("path")) or _clean_str(owner_repo.get("idea_path"))
    ticket_path = Path(path_raw) if path_raw is not None else None
    markdown = ""
    if ticket_path is not None:
        try:
            markdown = ticket_path.read_text(encoding="utf-8")
        except (FileNotFoundError, OSError, UnicodeError):
            markdown = ""
    return SelectedTicket(
        fingerprint=_clean_str(ticket.get("fingerprint")) or _clean_str(ticket_ref.get("fingerprint")) or "unknown",
        title=_clean_str(ticket.get("title")) or _clean_str(ticket_ref.get("title")),
        export_kind=_clean_str(ticket.get("export_kind")) or _clean_str(ticket_ref.get("export_kind")),
        stage=None,
        owner_root=Path(str(owner_repo["root"])) if _clean_str(owner_repo.get("root")) else None,
        idea_path=ticket_path,
        ticket_markdown=markdown,
        tickets_export_path=Path(str(ticket_ref["tickets_export_path"])) if _clean_str(ticket_ref.get("tickets_export_path")) else None,
        export_index=ticket_ref.get("export_index") if isinstance(ticket_ref.get("export_index"), int) else None,
    )


def _commands_from_original_run(run_dir: Path, verification: dict[str, Any]) -> list[str]:
    config = _read_json(run_dir / "verification_config.json")
    if isinstance(config, dict) and isinstance(config.get("commands"), list):
        out = [str(item).strip() for item in config["commands"] if isinstance(item, str) and item.strip()]
        if out:
            return out
    commands = verification.get("commands")
    out: list[str] = []
    if isinstance(commands, list):
        for item in commands:
            if not isinstance(item, dict):
                continue
            cmd = _clean_str(item.get("command"))
            if cmd is not None:
                out.append(cmd)
    return out


def _timeout_from_original_run(run_dir: Path) -> float | None:
    config = _read_json(run_dir / "verification_config.json")
    if isinstance(config, dict):
        raw = config.get("timeout_seconds")
        if isinstance(raw, (int, float)) and raw > 0:
            return float(raw)
    return None


def _git_remote_url_safe(repo_dir: Path, remote_name: str) -> str | None:
    try:
        return _git_remote_url(repo_dir=repo_dir, remote_name=remote_name)
    except Exception:
        return None


def _fallback_repo_input(
    *,
    run_dir: Path,
    ticket_ref: dict[str, Any],
    push_ref: dict[str, Any] | None,
    override_repo: str | None,
    remote_name: str,
) -> str | None:
    if override_repo is not None and override_repo.strip():
        return override_repo.strip()
    for value in (
        push_ref.get("remote_url") if isinstance(push_ref, dict) else None,
        (_read_json(run_dir / "target_ref.json") or {}).get("repo_input") if isinstance(_read_json(run_dir / "target_ref.json"), dict) else None,
    ):
        cleaned = _clean_str(value)
        if cleaned is not None and not (_looks_like_local_path(cleaned) and not Path(cleaned).exists()):
            return cleaned
    owner_repo = ticket_ref.get("owner_repo") if isinstance(ticket_ref.get("owner_repo"), dict) else {}
    owner_root = _clean_str(owner_repo.get("root"))
    if owner_root is not None:
        owner_path = Path(owner_root)
        if owner_path.exists():
            remote_url = _git_remote_url_safe(repo_dir=owner_path, remote_name=remote_name)
            if remote_url is not None:
                return remote_url
            return str(owner_path)
    return None


def _resolve_resume_target(
    *,
    args: argparse.Namespace,
    run_dir: Path,
    resume_state: dict[str, Any],
    workspace_ref: dict[str, Any],
    ticket_ref: dict[str, Any],
) -> tuple[str, str | None, Path | None, str]:
    branch = _clean_str(getattr(args, "ref", None)) or _clean_str(resume_state.get("branch"))
    workspace_raw = _clean_str(workspace_ref.get("workspace_dir")) or _clean_str(resume_state.get("workspace_path"))
    workspace_dir = Path(workspace_raw).expanduser() if workspace_raw is not None else None
    if workspace_dir is not None and workspace_dir.exists() and workspace_dir.is_dir():
        repo_input = _clean_str(getattr(args, "repo", None)) or str(workspace_dir)
        return repo_input, branch, workspace_dir, "same_workspace"

    push_ref = _read_json(run_dir / "push_ref.json")
    push_ref_dict = push_ref if isinstance(push_ref, dict) else None
    repo_input = _fallback_repo_input(
        run_dir=run_dir,
        ticket_ref=ticket_ref,
        push_ref=push_ref_dict,
        override_repo=_clean_str(getattr(args, "repo", None)),
        remote_name=str(getattr(args, "remote_name", "origin") or "origin"),
    )
    if repo_input is None:
        raise SystemExit(
            "Cannot resume: original workspace is missing and no fallback repo/remote could be inferred. "
            "Pass --repo with a repository URL or path."
        )
    if branch is None:
        raise SystemExit(
            "Cannot resume: original workspace is missing and no recorded branch/ref is available. "
            "Pass --ref with the branch to check out."
        )
    return repo_input, branch, None, "recorded_branch_fallback"


def _mark_original_resume_state(
    *,
    state_path: Path,
    resumed_run_dir: Path,
    new_state_path: Path,
) -> None:
    state = _read_json(state_path)
    if not isinstance(state, dict):
        return
    attempts = state.get("resume_attempts")
    if not isinstance(attempts, list):
        attempts = []
    attempts.append(
        {
            "resumed_at_utc": _utc_now_z(),
            "run_dir": str(resumed_run_dir),
            "resume_state_path": str(new_state_path),
        }
    )
    state["resume_attempts"] = attempts
    state["last_resumed_run_dir"] = str(resumed_run_dir)
    state["last_resumed_state_path"] = str(new_state_path)
    _write_json(state_path, state)



def _cmd_resume_pr(
    *,
    args: argparse.Namespace,
    repo_root: Path,
    state_path: Path,
    run_dir: Path,
    resume_state: dict[str, Any],
    lifecycle: str,
) -> int:
    workspace_ref = _read_json(run_dir / "workspace_ref.json")
    ticket_ref = _read_json(run_dir / "ticket_ref.json")
    if not isinstance(workspace_ref, dict):
        raise SystemExit("Cannot resume PR-backed run: workspace_ref.json must contain an object.")
    if not isinstance(ticket_ref, dict):
        raise SystemExit("Cannot resume PR-backed run: ticket_ref.json must contain an object.")

    selected = _selected_from_resume_state(resume_state=resume_state, ticket_ref=ticket_ref)
    pr_url = _pr_url_from_resume_artifacts(
        run_dir=run_dir,
        resume_state=resume_state,
        ticket_ref=ticket_ref,
    )
    if pr_url is None:
        raise SystemExit("Cannot resume PR-backed run: no PR URL is recorded in resume/run artifacts.")

    # This is the ticket's current-state refresh boundary: read live PR metadata/checks/diff just
    # before deciding whether to prompt the resumed implementation agent.
    pr_workspace = _pr_context_workspace(
        resume_state=resume_state,
        workspace_ref=workspace_ref,
        ticket_ref=ticket_ref,
    )
    pr_context = _collect_pr_review_context(workspace_dir=pr_workspace, pr_url=pr_url)
    review_summary = _load_review_summary_for_resume(resume_state=resume_state)
    pr_meta = pr_context.get("pr") if isinstance(pr_context.get("pr"), dict) else {}
    branch = _clean_str(getattr(args, "ref", None)) or _clean_str(pr_meta.get("headRefName")) or _clean_str(resume_state.get("branch"))
    if branch is None:
        raise SystemExit("Cannot resume PR-backed run: current PR metadata and resume state are missing the PR branch.")

    noop_reason = _current_pr_resume_noop_reason(
        pr_context=pr_context,
        review_summary=review_summary,
    )
    if noop_reason is not None:
        payload = {
            "schema_version": 1,
            "status": "noop_current_gates_green",
            "reason": noop_reason,
            "original_run_dir": str(run_dir),
            "resume_state_path": str(state_path),
            "pr_url": pr_url,
            "branch": branch,
            "current_pr_context": pr_context,
        }
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0

    repo_input, ref, resume_workspace_dir, workspace_strategy = _resolve_pr_resume_target(
        args=args,
        run_dir=run_dir,
        resume_state=resume_state,
        workspace_ref=workspace_ref,
        ticket_ref=ticket_ref,
        pr_context=pr_context,
    )
    prompt = _build_pr_resume_prompt(
        original_run_dir=run_dir,
        state_path=state_path,
        resume_state=resume_state,
        ticket_ref=ticket_ref,
        selected=selected,
        pr_url=pr_url,
        pr_context=pr_context,
        review_summary=review_summary,
        branch=ref,
    )

    verification = _read_json(run_dir / "verification.json")
    verification_commands = [
        str(cmd).strip()
        for cmd in (getattr(args, "verification_commands", None) or [])
        if isinstance(cmd, str) and str(cmd).strip()
    ]
    if not verification_commands:
        verification_commands = _commands_from_original_run(
            run_dir,
            verification if isinstance(verification, dict) else {},
        )
    verification_timeout_seconds = getattr(args, "verification_timeout_seconds", None)
    if verification_timeout_seconds is None or verification_timeout_seconds <= 0:
        verification_timeout_seconds = _timeout_from_original_run(run_dir)

    exec_backend = str(args.exec_backend).strip().lower()
    exec_docker_profile = _resolve_exec_docker_profile(
        exec_backend=exec_backend,
        requested_profile=getattr(args, "exec_docker_profile", None),
        maintenance_eligible=_maintenance_profile_is_eligible(repo_root=repo_root, repo_input=repo_input),
    )
    exec_cache_dir = getattr(args, "exec_cache_dir", None)
    if exec_cache_dir is not None:
        exec_cache_dir = exec_cache_dir.resolve()
    exec_cache = str(getattr(args, "exec_cache", "cold") or "cold")
    if exec_cache == "warm" and exec_cache_dir is None:
        exec_cache_dir = repo_root / "runs" / "_cache" / "usertest_implement"
    maintenance_venv_cache = bool(exec_backend == "docker" and exec_cache == "warm" and bool(getattr(args, "maintenance_venv_cache", True)))
    exec_maintenance_image_metadata_path = getattr(args, "exec_maintenance_image_metadata_path", None)
    if exec_maintenance_image_metadata_path is not None:
        exec_maintenance_image_metadata_path = exec_maintenance_image_metadata_path.resolve()

    effective_agent, codex_resume_session_id, author_continuity = _resume_agent_continuity(
        run_dir=run_dir,
        resume_state=resume_state,
        requested_agent=str(args.agent),
    )

    request = RunRequest(
        repo=repo_input,
        ref=ref,
        agent=effective_agent,
        policy=str(args.policy),
        persona_id=args.persona_id,
        mission_id=args.mission_id,
        seed=int(args.seed),
        model=args.model,
        agent_config_overrides=tuple(args.agent_config_override or []),
        agent_append_system_prompt=prompt,
        codex_resume_session_id=codex_resume_session_id,
        keep_workspace=True,
        verification_commands=tuple(verification_commands),
        verification_timeout_seconds=verification_timeout_seconds,
        verification_reuse_mode=str(getattr(args, "verify_reuse", "auto") or "auto"),
        exec_backend=exec_backend,
        exec_docker_profile=exec_docker_profile,
        exec_keep_container=bool(args.exec_keep_container),
        exec_cache=exec_cache,
        exec_cache_dir=exec_cache_dir,
        exec_maintenance_venv_cache=maintenance_venv_cache,
        exec_maintenance_image_metadata_path=exec_maintenance_image_metadata_path,
        exec_use_host_agent_login=bool(args.exec_use_host_agent_login),
        exec_use_target_sandbox_cli_install=bool(args.exec_use_target_sandbox_cli_install),
        resume_workspace_dir=resume_workspace_dir,
    )

    if args.dry_run:
        print(
            json.dumps(
                {
                    "schema_version": 1,
                    "resume_kind": "pr",
                    "original_run_dir": str(run_dir),
                    "resume_state_path": str(state_path),
                    "workspace_strategy": workspace_strategy,
                    "pr_url": pr_url,
                    "branch": ref,
                    "current_pr_context": pr_context,
                    "implementation_author_continuity": author_continuity,
                    "unresolved_review_findings": _review_findings_for_resume(review_summary),
                    "failing_check_pointers": _failing_check_pointers(pr_context),
                    "run_request": {
                        "repo": request.repo,
                        "ref": request.ref,
                        "agent": request.agent,
                        "policy": request.policy,
                        "model": request.model,
                        "codex_resume_session_id": request.codex_resume_session_id,
                        "keep_workspace": request.keep_workspace,
                        "resume_workspace_dir": str(request.resume_workspace_dir) if request.resume_workspace_dir is not None else None,
                        "verification_commands": list(request.verification_commands),
                        "verification_timeout_seconds": request.verification_timeout_seconds,
                        "verification_reuse_mode": request.verification_reuse_mode,
                        "commit": True,
                        "push": True,
                        "pr": False,
                    },
                    "prompt": prompt,
                },
                indent=2,
                ensure_ascii=False,
            )
        )
        return 0

    if exec_backend == "docker":
        _require_docker_available()

    cfg = _load_runner_config(repo_root)
    result = run_once(cfg, request)
    resumed_run_dir = result.run_dir
    _write_json(resumed_run_dir / "ticket_ref.json", ticket_ref)
    _write_json(resumed_run_dir / "current_pr_context.json", pr_context)
    _write_json(
        resumed_run_dir / "resume_ref.json",
        {
            "schema_version": 1,
            "resume_kind": "pr",
            "resumed_from_run_dir": str(run_dir),
            "resumed_from_resume_state_path": str(state_path),
            "resumed_from_lifecycle_state": lifecycle,
            "workspace_strategy": workspace_strategy,
            "pr_url": pr_url,
            "branch": ref,
            "implementation_author_continuity": author_continuity,
            "source_evidence_paths": _artifact_path_map_for_pr_resume(
                run_dir=run_dir,
                review_run_dir=_review_run_dir_from_state(resume_state),
                state_path=state_path,
            ),
        },
    )
    _write_json(
        resumed_run_dir / "pr_ref.json",
        {
            "schema_version": 1,
            "requested": False,
            "created": True,
            "existing_pr": True,
            "url": pr_url,
            "branch": ref,
            "title": pr_meta.get("title"),
            "agent": effective_agent,
            "model": args.model,
            "error": None,
        },
    )

    exit_code = int(result.exit_code or 0)
    if result.report_validation_errors:
        exit_code = max(exit_code, 2)
    verification_after = _read_json(resumed_run_dir / "verification.json")
    if isinstance(verification_after, dict) and verification_after.get("passed") is False:
        exit_code = max(exit_code, 2)

    git_ref: dict[str, Any] | None = None
    push_ref: dict[str, Any] | None = None
    ci_ref: dict[str, Any] | None = None
    commit_performed = False
    if exit_code == 0:
        commit_message = (
            getattr(args, "commit_message", None)
            or f"{selected.fingerprint}: Resume PR feedback"
        )
        git_ref = finalize_commit(
            run_dir=resumed_run_dir,
            branch=ref,
            commit_message=commit_message,
            git_user_name=getattr(args, "git_user_name", None),
            git_user_email=getattr(args, "git_user_email", None),
        )
        commit_performed = bool(git_ref.get("commit_performed") is True)
        if git_ref.get("error"):
            exit_code = max(exit_code, 3)
        current_ticket_ref = _read_json(resumed_run_dir / "ticket_ref.json")
        current_ticket_provenance = (
            current_ticket_ref.get("ticket_provenance")
            if isinstance(current_ticket_ref, dict)
            and isinstance(current_ticket_ref.get("ticket_provenance"), dict)
            else {}
        )
        if commit_performed and isinstance(
            current_ticket_provenance.get("target_contract"), dict
        ):
            try:
                record_verified_implementation_head(
                    run_dir=resumed_run_dir,
                    require_exact_base=False,
                )
            except ValueError as exc:
                raise SystemExit(
                    "Unable to bind resumed verification to the committed PR head: "
                    f"{exc}"
                ) from exc

    if exit_code == 0:
        push_candidates: list[Path] = []
        workspace_after_for_push = _read_json(resumed_run_dir / "workspace_ref.json")
        if isinstance(workspace_after_for_push, dict):
            raw_workspace_for_push = _clean_str(workspace_after_for_push.get("workspace_dir"))
            if raw_workspace_for_push is not None and (Path(raw_workspace_for_push) / ".git").exists():
                push_candidates.append(Path(raw_workspace_for_push))
        if selected.owner_root is not None and (selected.owner_root / ".git").exists():
            push_candidates.append(selected.owner_root)
        if _looks_like_local_path(repo_input) and (Path(repo_input) / ".git").exists():
            push_candidates.append(Path(repo_input))
        push_ref = finalize_push(
            run_dir=resumed_run_dir,
            remote_name=str(getattr(args, "remote_name", "origin") or "origin"),
            remote_url=getattr(args, "remote_url", None),
            candidate_repo_dirs=push_candidates,
            branch=ref,
            force_with_lease=bool(getattr(args, "force_push", False)),
        )
        if push_ref.get("error") or push_ref.get("pushed") is not True:
            exit_code = max(exit_code, 4)

    if exit_code == 0:
        workspace_after = _read_json(resumed_run_dir / "workspace_ref.json")
        workspace_dir = None
        if isinstance(workspace_after, dict):
            raw_workspace = _clean_str(workspace_after.get("workspace_dir"))
            if raw_workspace is not None:
                workspace_dir = Path(raw_workspace)
        if bool(getattr(args, "skip_ci_wait", False)):
            ci_ref = {
                "schema_version": 1,
                "workflow": "CI",
                "branch": ref,
                "head_sha": _git_head_sha(workspace_dir) if workspace_dir is not None else None,
                "run_id": None,
                "run_url": None,
                "status": None,
                "conclusion": None,
                "passed": None,
                "error": None,
                "skipped": True,
                "skip_reason": "flag --skip-ci-wait",
                "started_at_utc": _utc_now_z(),
                "finished_at_utc": _utc_now_z(),
                "timeout_seconds": _ci_timeout_seconds_arg(getattr(args, "ci_timeout_seconds", None)),
            }
            _write_json(resumed_run_dir / "ci_gate.json", ci_ref)
        elif workspace_dir is None:
            ci_ref = {
                "schema_version": 1,
                "workflow": "CI",
                "branch": ref,
                "head_sha": None,
                "passed": False,
                "error": "Missing workspace_ref.json; cannot locate workspace for CI follow-up.",
                "skipped": True,
                "skip_reason": "workspace_missing",
            }
            _write_json(resumed_run_dir / "ci_gate.json", ci_ref)
            exit_code = max(exit_code, 5)
        else:
            head_sha = _git_head_sha(workspace_dir)
            if head_sha is None:
                ci_ref = {
                    "schema_version": 1,
                    "workflow": "CI",
                    "branch": ref,
                    "head_sha": None,
                    "passed": False,
                    "error": "Unable to determine HEAD SHA for CI follow-up.",
                    "skipped": True,
                    "skip_reason": "head_sha_unavailable",
                }
                _write_json(resumed_run_dir / "ci_gate.json", ci_ref)
                exit_code = max(exit_code, 5)
            else:
                ci_ref = _wait_for_ci_success(
                    run_dir=resumed_run_dir,
                    workspace_dir=workspace_dir,
                    branch=ref,
                    head_sha=head_sha,
                    workflow="CI",
                    timeout_seconds=_ci_timeout_seconds_arg(getattr(args, "ci_timeout_seconds", None)),
                )
                if ci_ref.get("passed") is not True:
                    exit_code = max(exit_code, 5)

    handoff_summary = {
        "schema_version": 1,
        "branch": ref,
        "commit_requested": True,
        "commit_performed": commit_performed,
        "push_requested": True,
        "pushed": bool(isinstance(push_ref, dict) and push_ref.get("pushed") is True),
        "pr_requested": False,
        "pr_created": True,
        "pr_url": pr_url,
        "ci_required": True,
        "ci_status": ci_ref.get("status") if isinstance(ci_ref, dict) else None,
        "ci_conclusion": ci_ref.get("conclusion") if isinstance(ci_ref, dict) else None,
        "review_required": True,
        "review_run_dir": None,
        "review_decision": None,
        "review_merge_ready": None,
        "review_error": None,
        "final_status": "success" if exit_code == 0 else "failed",
        "resumed_from_run_dir": str(run_dir),
        "resume_kind": "pr",
    }
    _write_json(resumed_run_dir / "handoff_summary.json", handoff_summary)

    new_resume_lifecycle: object | None = None
    try:
        new_state = write_ticket_resume_state(
            selected=selected,
            run_dir=resumed_run_dir,
            owner_root=selected.owner_root,
            branch=ref,
            exit_code=exit_code,
        )
        new_state["resumed_from_run_dir"] = str(run_dir)
        new_state["resumed_from_resume_state_path"] = str(state_path)
        new_state["workspace_strategy"] = workspace_strategy
        new_state["resume_kind"] = "pr"
        new_resume_lifecycle = new_state.get("lifecycle_state")
        _write_json(resumed_run_dir / RESUME_STATE_ARTIFACT_NAME, new_state)
        _mark_original_resume_state(
            state_path=state_path,
            resumed_run_dir=resumed_run_dir,
            new_state_path=resumed_run_dir / RESUME_STATE_ARTIFACT_NAME,
        )
    except Exception as e:
        print(f"WARNING: failed to update resume state: {e}", file=sys.stderr)

    if getattr(args, "ledger", None) is not None:
        try:
            ledger_path = _resolve_ledger_path(repo_root=repo_root, raw=args.ledger)
            updates: dict[str, Any] = {
                "title": selected.title,
                "owner_root": str(selected.owner_root) if selected.owner_root is not None else None,
                "idea_path": str(selected.idea_path) if selected.idea_path is not None else None,
                "last_run_dir": str(resumed_run_dir),
                "last_exit_code": int(exit_code),
                "last_branch": ref,
                "last_pr_url": pr_url,
                "last_resume_state_path": str(resumed_run_dir / RESUME_STATE_ARTIFACT_NAME),
                "last_resume_lifecycle_state": new_resume_lifecycle,
            }
            if isinstance(git_ref, dict):
                updates["last_head_commit"] = git_ref.get("head_commit")
            if isinstance(push_ref, dict) and push_ref.get("pushed") is True:
                updates["last_push_remote"] = push_ref.get("remote_name")
                updates["last_push_remote_url"] = push_ref.get("remote_url")
            update_ledger_file(ledger_path, fingerprint=selected.fingerprint, updates=updates)
        except Exception as e:
            print(f"WARNING: failed to update ledger for PR resume: {e}", file=sys.stderr)

    print(str(resumed_run_dir))
    return exit_code

def _cmd_resume(args: argparse.Namespace) -> int:
    repo_root = _resolve_repo_root(args.repo_root)

    state_path = args.resume_state.resolve() if args.resume_state is not None else (args.run_dir.resolve() / RESUME_STATE_ARTIFACT_NAME)
    run_dir = args.run_dir.resolve() if args.run_dir is not None else state_path.parent.resolve()
    resume_state = _read_json(state_path)
    if not isinstance(resume_state, dict):
        raise SystemExit(f"Cannot resume: missing or invalid resume state: {state_path}")
    lifecycle = _clean_str(resume_state.get("lifecycle_state"))
    if lifecycle in _VALID_PR_RESUME_STATES:
        return _cmd_resume_pr(
            args=args,
            repo_root=repo_root,
            state_path=state_path,
            run_dir=run_dir,
            resume_state=resume_state,
            lifecycle=lifecycle,
        )
    if lifecycle not in _VALID_VERIFICATION_RESUME_STATES:
        allowed = sorted(_VALID_VERIFICATION_RESUME_STATES | _VALID_PR_RESUME_STATES)
        raise SystemExit(
            "Cannot resume: resume state must be one of "
            f"{allowed!r}; got {lifecycle!r} in {state_path}."
        )

    missing = [name for name in _REQUIRED_RESUME_ARTIFACTS if not (run_dir / name).exists()]
    if missing:
        raise SystemExit(
            "Cannot resume: required verification resume artifact(s) missing from "
            f"{run_dir}: {', '.join(missing)}"
        )

    verification = _read_json(run_dir / "verification.json")
    verification_reuse = _read_json(run_dir / "verification_reuse.json")
    agent_attempts = _read_json(run_dir / "agent_attempts.json")
    workspace_ref = _read_json(run_dir / "workspace_ref.json")
    ticket_ref = _read_json(run_dir / "ticket_ref.json")
    if not isinstance(verification, dict) or verification.get("passed") is not False:
        raise SystemExit("Cannot resume: verification.json does not record a failed verification run.")
    if not isinstance(workspace_ref, dict):
        raise SystemExit("Cannot resume: workspace_ref.json must contain an object.")
    if not isinstance(ticket_ref, dict):
        raise SystemExit("Cannot resume: ticket_ref.json must contain an object.")

    verification_reuse_dict = verification_reuse if isinstance(verification_reuse, dict) else None
    agent_attempts_dict = agent_attempts if isinstance(agent_attempts, dict) else None
    selected = _selected_from_resume_state(resume_state=resume_state, ticket_ref=ticket_ref)
    repo_input, ref, resume_workspace_dir, workspace_strategy = _resolve_resume_target(
        args=args,
        run_dir=run_dir,
        resume_state=resume_state,
        workspace_ref=workspace_ref,
        ticket_ref=ticket_ref,
    )
    prompt = _build_verification_resume_prompt(
        original_run_dir=run_dir,
        resume_state=resume_state,
        verification=verification,
        verification_reuse=verification_reuse_dict,
        agent_attempts=agent_attempts_dict,
        workspace_ref=workspace_ref,
        ticket_ref=ticket_ref,
    )

    verification_commands = [
        str(cmd).strip() for cmd in (getattr(args, "verification_commands", None) or []) if isinstance(cmd, str) and str(cmd).strip()
    ]
    if not verification_commands:
        verification_commands = _commands_from_original_run(run_dir, verification)
    verification_timeout_seconds = getattr(args, "verification_timeout_seconds", None)
    if verification_timeout_seconds is None or verification_timeout_seconds <= 0:
        verification_timeout_seconds = _timeout_from_original_run(run_dir)

    exec_backend = str(args.exec_backend).strip().lower()
    exec_docker_profile = _resolve_exec_docker_profile(
        exec_backend=exec_backend,
        requested_profile=getattr(args, "exec_docker_profile", None),
        maintenance_eligible=_maintenance_profile_is_eligible(repo_root=repo_root, repo_input=repo_input),
    )
    exec_cache_dir = getattr(args, "exec_cache_dir", None)
    if exec_cache_dir is not None:
        exec_cache_dir = exec_cache_dir.resolve()
    exec_cache = str(getattr(args, "exec_cache", "cold") or "cold")
    if exec_cache == "warm" and exec_cache_dir is None:
        exec_cache_dir = repo_root / "runs" / "_cache" / "usertest_implement"
    maintenance_venv_cache = bool(exec_backend == "docker" and exec_cache == "warm" and bool(getattr(args, "maintenance_venv_cache", True)))
    exec_maintenance_image_metadata_path = getattr(args, "exec_maintenance_image_metadata_path", None)
    if exec_maintenance_image_metadata_path is not None:
        exec_maintenance_image_metadata_path = exec_maintenance_image_metadata_path.resolve()

    effective_agent, codex_resume_session_id, author_continuity = _resume_agent_continuity(
        run_dir=run_dir,
        resume_state=resume_state,
        requested_agent=str(args.agent),
    )

    request = RunRequest(
        repo=repo_input,
        ref=ref,
        agent=effective_agent,
        policy=str(args.policy),
        persona_id=args.persona_id,
        mission_id=args.mission_id,
        seed=int(args.seed),
        model=args.model,
        agent_config_overrides=tuple(args.agent_config_override or []),
        agent_append_system_prompt=prompt,
        codex_resume_session_id=codex_resume_session_id,
        keep_workspace=True,
        verification_commands=tuple(verification_commands),
        verification_timeout_seconds=verification_timeout_seconds,
        verification_reuse_mode=str(getattr(args, "verify_reuse", "auto") or "auto"),
        exec_backend=exec_backend,
        exec_docker_profile=exec_docker_profile,
        exec_keep_container=bool(args.exec_keep_container),
        exec_cache=exec_cache,
        exec_cache_dir=exec_cache_dir,
        exec_maintenance_venv_cache=maintenance_venv_cache,
        exec_maintenance_image_metadata_path=exec_maintenance_image_metadata_path,
        exec_use_host_agent_login=bool(args.exec_use_host_agent_login),
        exec_use_target_sandbox_cli_install=bool(args.exec_use_target_sandbox_cli_install),
        resume_workspace_dir=resume_workspace_dir,
    )

    if args.dry_run:
        print(
            json.dumps(
                {
                    "schema_version": 1,
                    "original_run_dir": str(run_dir),
                    "resume_state_path": str(state_path),
                    "workspace_strategy": workspace_strategy,
                    "implementation_author_continuity": author_continuity,
                    "run_request": {
                        "repo": request.repo,
                        "ref": request.ref,
                        "agent": request.agent,
                        "policy": request.policy,
                        "model": request.model,
                        "codex_resume_session_id": request.codex_resume_session_id,
                        "keep_workspace": request.keep_workspace,
                        "resume_workspace_dir": str(request.resume_workspace_dir) if request.resume_workspace_dir is not None else None,
                        "verification_commands": list(request.verification_commands),
                        "verification_timeout_seconds": request.verification_timeout_seconds,
                        "verification_reuse_mode": request.verification_reuse_mode,
                    },
                    "prompt": prompt,
                },
                indent=2,
                ensure_ascii=False,
            )
        )
        return 0

    if exec_backend == "docker":
        _require_docker_available()

    cfg = _load_runner_config(repo_root)
    result = run_once(cfg, request)
    resumed_run_dir = result.run_dir
    _write_json(
        resumed_run_dir / "resume_ref.json",
        {
            "schema_version": 1,
            "resumed_from_run_dir": str(run_dir),
            "resumed_from_resume_state_path": str(state_path),
            "resumed_from_lifecycle_state": lifecycle,
            "workspace_strategy": workspace_strategy,
            "implementation_author_continuity": author_continuity,
            "source_evidence_paths": {name.removesuffix(".json"): str(run_dir / name) for name in _REQUIRED_RESUME_ARTIFACTS},
        },
    )
    _write_json(resumed_run_dir / "ticket_ref.json", ticket_ref)

    verification_after = _read_json(resumed_run_dir / "verification.json")
    exit_code = int(result.exit_code or 0)
    if isinstance(verification_after, dict) and verification_after.get("passed") is False:
        exit_code = max(exit_code, 2)
    if isinstance(verification_after, dict) and verification_after.get("passed") is True and exit_code == 0:
        _write_json(
            resumed_run_dir / "handoff_summary.json",
            {
                "schema_version": 1,
                "branch": ref,
                "commit_requested": False,
                "commit_performed": False,
                "push_requested": False,
                "pushed": False,
                "pr_requested": False,
                "pr_created": False,
                "review_required": False,
                "final_status": "success",
                "resumed_from_run_dir": str(run_dir),
            },
        )

    try:
        new_state = write_ticket_resume_state(
            selected=selected,
            run_dir=resumed_run_dir,
            owner_root=selected.owner_root,
            branch=ref,
            exit_code=exit_code,
        )
        new_state["resumed_from_run_dir"] = str(run_dir)
        new_state["resumed_from_resume_state_path"] = str(state_path)
        new_state["workspace_strategy"] = workspace_strategy
        _write_json(resumed_run_dir / RESUME_STATE_ARTIFACT_NAME, new_state)
        _mark_original_resume_state(
            state_path=state_path,
            resumed_run_dir=resumed_run_dir,
            new_state_path=resumed_run_dir / RESUME_STATE_ARTIFACT_NAME,
        )
    except Exception as e:
        print(f"WARNING: failed to update resume state: {e}", file=sys.stderr)

    print(str(resumed_run_dir))
    return exit_code


__all__ = [name for name in globals() if not name.startswith("__")]
