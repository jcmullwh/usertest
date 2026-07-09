# ruff: noqa: E501,F401,F403,F405
from __future__ import annotations

from usertest_implement.resume_state import (
    RESUME_STATE_ARTIFACT_NAME,
    write_ticket_resume_state,
)
from usertest_implement.review_context import (
    _build_final_review_summary,
    _build_pr_review_body,
    _build_review_append_prompt,
    _coerce_pr_url,
    _collect_pr_review_context,
    _current_merge_gate_from_pr_context,
    _extract_agent_review_summary,
    _load_ledger_entry,
    _review_findings_from_report,
    _run_gh_text,
    _submit_pr_review,
)
from usertest_implement.selection import _select_review_ticket
from usertest_implement.shared import *


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

    handoff_summary = _read_json(implementation_run_dir / "handoff_summary.json")
    pr_ref = _read_json(implementation_run_dir / "pr_ref.json")
    ci_gate = _read_json(implementation_run_dir / "ci_gate.json")
    pr_url = _coerce_pr_url(handoff_summary=handoff_summary, pr_ref=pr_ref)
    if pr_url is None:
        raise SystemExit(
            f"Ticket {selected.fingerprint!r} does not have a PR to review "
            f"(run_dir={implementation_run_dir})."
        )

    pr_context = _collect_pr_review_context(workspace_dir=owner_root, pr_url=pr_url)
    pr_meta = pr_context.get("pr")
    if not isinstance(pr_meta, dict):
        raise SystemExit("Unable to read PR metadata for review.")
    current_merge_ready, current_gate = _current_merge_gate_from_pr_context(pr_context)
    if not current_merge_ready:
        raise SystemExit(
            "Refusing to run review before the PR gate is green: "
            + json.dumps(current_gate, ensure_ascii=False)
        )

    head_ref_name = pr_meta.get("headRefName")
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
        ref=str(head_ref_name).strip() if isinstance(head_ref_name, str) and head_ref_name.strip() else None,
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
            "schema_version": 1,
            "ticket_fingerprint": selected.fingerprint,
            "ticket_path": str(selected.idea_path) if selected.idea_path is not None else None,
            "implementation_run_dir": str(implementation_run_dir),
            "pr_url": pr_url,
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

    pr_context = _collect_pr_review_context(workspace_dir=owner_root, pr_url=pr_url)
    current_merge_ready, current_gate = _current_merge_gate_from_pr_context(pr_context)
    if not current_merge_ready:
        raise SystemExit(
            "Refusing to merge because the current PR gate is not green: "
            f"{json.dumps(current_gate, ensure_ascii=False)}"
        )

    proc = subprocess.run(
        ["gh", "pr", "merge", pr_url, "--merge", "--delete-branch"],
        cwd=str(owner_root),
        capture_output=True,
        text=True,
        check=False,
    )
    merge_ref = {
        "schema_version": 1,
        "pr_url": pr_url,
        "merged": proc.returncode == 0,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
        "returncode": int(proc.returncode),
        "merged_at_utc": _utc_now_z() if proc.returncode == 0 else None,
    }
    _write_json(review_run_dir / "merge_ref.json", merge_ref)
    if proc.returncode != 0:
        raise SystemExit(proc.stderr.strip() or proc.stdout.strip() or "gh pr merge failed")

    completed_ticket_path = None
    if selected.owner_root is not None:
        completed_ticket_path = move_ticket_file(
            owner_root=selected.owner_root,
            fingerprint=selected.fingerprint,
            to_bucket="5 - complete",
            dry_run=False,
        )
        _sync_ticket_atom_actions(repo_root=repo_root, owner_root=selected.owner_root)
    update_ledger_file(
        ledger_path,
        fingerprint=selected.fingerprint,
        updates={
            "last_merge_pr_url": pr_url,
            "last_merged_at": merge_ref["merged_at_utc"],
        },
    )
    review_ref = _read_json(review_run_dir / "review_ref.json")
    implementation_run_dir_raw = (
        review_ref.get("implementation_run_dir") if isinstance(review_ref, dict) else None
    )
    if isinstance(implementation_run_dir_raw, str) and implementation_run_dir_raw.strip():
        implementation_run_dir = Path(implementation_run_dir_raw)
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
    return 0




__all__ = [name for name in globals() if not name.startswith("__")]
