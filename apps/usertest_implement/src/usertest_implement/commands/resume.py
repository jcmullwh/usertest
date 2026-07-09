# ruff: noqa: E501,F401,F403,F405
from __future__ import annotations

from runner_core.verification_prompts import _build_verification_followup_prompt

from usertest_implement.resume_state import (
    LIFECYCLE_VERIFICATION_FAILED,
    LIFECYCLE_VERIFICATION_FAILED_RESUME_READY,
    RESUME_STATE_ARTIFACT_NAME,
    write_ticket_resume_state,
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


def _cmd_resume(args: argparse.Namespace) -> int:
    repo_root = _resolve_repo_root(args.repo_root)

    state_path = args.resume_state.resolve() if args.resume_state is not None else (args.run_dir.resolve() / RESUME_STATE_ARTIFACT_NAME)
    run_dir = args.run_dir.resolve() if args.run_dir is not None else state_path.parent.resolve()
    resume_state = _read_json(state_path)
    if not isinstance(resume_state, dict):
        raise SystemExit(f"Cannot resume: missing or invalid resume state: {state_path}")
    lifecycle = _clean_str(resume_state.get("lifecycle_state"))
    if lifecycle not in _VALID_VERIFICATION_RESUME_STATES:
        raise SystemExit(
            "Cannot resume: resume state must be verification_failed_resume_ready; "
            f"got {lifecycle!r} in {state_path}."
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

    request = RunRequest(
        repo=repo_input,
        ref=ref,
        agent=str(args.agent),
        policy=str(args.policy),
        persona_id=args.persona_id,
        mission_id=args.mission_id,
        seed=int(args.seed),
        model=args.model,
        agent_config_overrides=tuple(args.agent_config_override or []),
        agent_append_system_prompt=prompt,
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
                    "run_request": {
                        "repo": request.repo,
                        "ref": request.ref,
                        "agent": request.agent,
                        "policy": request.policy,
                        "model": request.model,
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
