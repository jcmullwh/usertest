# ruff: noqa: E501,F401,F403,F405
from __future__ import annotations

from usertest_implement.ci import _ci_timeout_seconds_arg, _git_head_sha, _wait_for_ci_success
from usertest_implement.commands.review import _run_review_for_selected_ticket
from usertest_implement.review_context import _run_gh_text
from usertest_implement.selection import (
    _compose_ticket_blob,
    _default_branch_name,
    _require_stage6_implementation_ticket,
    _resolve_default_branch_name,
    _resolve_remote_url_for_push,
    _select_ticket_from_export,
    _select_ticket_from_owner_root,
    _select_ticket_from_path,
    _should_move_ticket_to_review,
    _write_pr_manifest,
)
from usertest_implement.settings import (
    _apply_cli_settings,
    _load_cli_settings_doc,
    _resolve_settings_path,
)
from usertest_implement.shared import *


def _default_backlog_runs_dir(repo_root: Path) -> Path:
    return repo_root / "runs" / "usertest"


def _list_backlog_targets(runs_dir: Path) -> list[str]:
    if not runs_dir.exists():
        return []
    if not runs_dir.is_dir():
        return []
    slugs: list[str] = []
    for child in runs_dir.iterdir():
        if not child.is_dir():
            continue
        name = child.name
        if not name or name.startswith("_"):
            continue
        slugs.append(name)
    slugs.sort()
    return slugs


def _resolve_backlog_target(*, runs_dir: Path, target: str | None) -> str:
    if isinstance(target, str) and target.strip():
        return target.strip()
    candidates = _list_backlog_targets(runs_dir)
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        raise SystemExit(
            "Unable to infer --backlog-target because there are no target directories under "
            f"{runs_dir}. Provide --backlog-target or --no-refresh-backlog."
        )
    raise SystemExit(
        "Unable to infer --backlog-target because multiple targets exist under "
        f"{runs_dir}: {', '.join(candidates)}. Provide --backlog-target or --no-refresh-backlog."
    )


def _run_workflow_step(argv: list[str], *, cwd: Path, label: str) -> None:
    cmd = " ".join(argv)
    print(f"[workflow] {label}: {cmd}", file=sys.stderr)
    proc = subprocess.run(argv, cwd=str(cwd), check=False)
    if proc.returncode != 0:
        raise SystemExit(proc.returncode)


def _refresh_backlog_for_ticket_implementation(
    *,
    args: argparse.Namespace,
    repo_root: Path,
) -> None:
    runs_dir = (
        args.backlog_runs_dir.resolve()
        if args.backlog_runs_dir is not None
        else _default_backlog_runs_dir(repo_root)
    )
    target = _resolve_backlog_target(runs_dir=runs_dir, target=args.backlog_target)

    backlog_agent = str(args.backlog_agent) if args.backlog_agent else "claude"
    backlog_model = (
        str(args.backlog_model).strip()
        if isinstance(args.backlog_model, str) and args.backlog_model.strip()
        else None
    )
    review_agent = (
        str(args.review_agent).strip()
        if isinstance(args.review_agent, str) and args.review_agent.strip()
        else backlog_agent
    )
    review_model = (
        str(args.review_model).strip()
        if isinstance(args.review_model, str) and args.review_model.strip()
        else None
    )

    base = [sys.executable, "-m", "usertest_backlog.cli"]
    common = ["--repo-root", str(repo_root), "--runs-dir", str(runs_dir), "--target", target]

    backlog_cmd = base + ["reports", "backlog", *common, "--agent", backlog_agent]
    if backlog_model is not None:
        backlog_cmd.extend(["--model", backlog_model])
    _run_workflow_step(backlog_cmd, cwd=repo_root, label="reports backlog")

    intent_cmd = base + ["reports", "intent-snapshot", *common]
    _run_workflow_step(intent_cmd, cwd=repo_root, label="reports intent-snapshot")

    review_cmd = base + ["reports", "review-ux", *common, "--agent", review_agent]
    if review_model is not None:
        review_cmd.extend(["--model", review_model])
    _run_workflow_step(review_cmd, cwd=repo_root, label="reports review-ux")

    export_cmd = base + ["reports", "export-tickets", *common]
    export_cmd.extend(["--stage", "ready_for_ticket"])
    _run_workflow_step(export_cmd, cwd=repo_root, label="reports export-tickets")



def _build_handoff_summary(
    *,
    branch: str,
    commit_requested: bool,
    commit_performed: bool,
    push_requested: bool,
    push_ref: dict[str, Any] | None,
    pr_requested: bool,
    pr_ref: dict[str, Any] | None,
    ci_gate: dict[str, Any] | None,
    review_required: bool,
    review_run_dir: Path | None,
    review_summary: dict[str, Any] | None,
    review_error: str | None,
) -> dict[str, Any]:
    ci_status = None
    ci_conclusion = None
    ci_run_url = None
    if isinstance(ci_gate, dict):
        ci_status_raw = ci_gate.get("status")
        ci_conclusion_raw = ci_gate.get("conclusion")
        ci_status = str(ci_status_raw).strip() if isinstance(ci_status_raw, str) and ci_status_raw.strip() else None
        ci_conclusion = (
            str(ci_conclusion_raw).strip().lower()
            if isinstance(ci_conclusion_raw, str) and ci_conclusion_raw.strip()
            else None
        )
        ci_run_url_raw = ci_gate.get("run_url")
        if isinstance(ci_run_url_raw, str) and ci_run_url_raw.strip():
            ci_run_url = ci_run_url_raw.strip()
        if ci_status is None and ci_gate.get("skipped") is True:
            ci_status = "skipped"
        if ci_conclusion is None:
            if ci_gate.get("passed") is True:
                ci_conclusion = "success"
                ci_status = ci_status or "completed"
            elif ci_gate.get("passed") is False:
                ci_conclusion = "failure"
                ci_status = ci_status or "completed"

    pr_url = None
    pr_created = False
    if isinstance(pr_ref, dict):
        pr_created = bool(pr_ref.get("created") is True)
        pr_url_raw = pr_ref.get("url")
        if isinstance(pr_url_raw, str) and pr_url_raw.strip():
            pr_url = pr_url_raw.strip()

    pushed = bool(isinstance(push_ref, dict) and push_ref.get("pushed") is True)
    review_decision = None
    review_merge_ready = None
    if isinstance(review_summary, dict):
        review_decision_raw = review_summary.get("review_decision")
        if isinstance(review_decision_raw, str) and review_decision_raw.strip():
            review_decision = review_decision_raw.strip()
        review_merge_ready = bool(review_summary.get("merge_ready") is True)

    final_status = "success"
    if pr_created:
        if review_error is not None:
            final_status = "failure"

    return {
        "schema_version": 1,
        "branch": branch,
        "commit_requested": bool(commit_requested),
        "commit_performed": bool(commit_performed),
        "push_requested": bool(push_requested),
        "pushed": pushed,
        "pr_requested": bool(pr_requested),
        "pr_created": pr_created,
        "pr_url": pr_url,
        "ci_required": pr_created,
        "ci_status": ci_status,
        "ci_run_url": ci_run_url,
        "ci_conclusion": ci_conclusion,
        "review_required": bool(review_required),
        "review_run_dir": str(review_run_dir) if review_run_dir is not None else None,
        "review_decision": review_decision,
        "review_merge_ready": review_merge_ready,
        "review_error": review_error,
        "final_status": final_status,
    }


def _run_selected_ticket(
    *,
    args: argparse.Namespace,
    repo_root: Path,
    cfg: RunnerConfig,
    selected: SelectedTicket,
) -> int:
    _require_stage6_implementation_ticket(selected)
    settings_info = getattr(args, "_settings_info", None)

    repo_input: str | None = None
    repo_is_explicit = False
    if isinstance(args.repo, str) and args.repo.strip():
        repo_input = args.repo.strip()
        repo_is_explicit = True
    elif selected.owner_root is not None:
        repo_input = str(selected.owner_root)
    elif selected.idea_path is not None:
        inferred = _infer_git_root(selected.idea_path.parent)
        repo_input = str(inferred) if inferred is not None else str(selected.idea_path.parent)
    else:
        raise SystemExit("Unable to infer target repo. Provide --repo.")

    # Default handoff flags may be enabled on some subcommands (e.g. tickets run-next).
    # Normalize so disabling an earlier step disables dependent later steps.
    if not bool(args.commit):
        args.push = False
        args.pr = False
    elif not bool(args.push):
        args.pr = False

    if args.push or args.pr:
        if not args.commit:
            raise SystemExit("--push/--pr requires --commit")

    keep_workspace = bool(args.keep_workspace) or bool(args.commit) or bool(args.push) or bool(args.pr)

    verification_commands: list[str] = []
    for cmd in getattr(args, "verification_commands", None) or []:
        if not isinstance(cmd, str) or not cmd.strip():
            raise SystemExit(f"--verify-command entries must be non-empty strings; got {cmd!r}.")
        verification_commands.append(cmd.strip())

    verification_timeout_seconds = getattr(args, "verification_timeout_seconds", None)
    if verification_timeout_seconds is not None and verification_timeout_seconds <= 0:
        verification_timeout_seconds = None
    verification_profile = str(
        getattr(args, "verification_profile", "default_handoff") or "default_handoff"
    ).strip().lower()
    if verification_profile not in {"default_handoff", "none"}:
        raise SystemExit(
            "verification_profile must be one of {'default_handoff', 'none'}; "
            f"got {getattr(args, 'verification_profile', None)!r}."
        )
    verification_reuse_mode = str(getattr(args, "verify_reuse", "auto") or "auto").strip().lower()
    if verification_reuse_mode not in {"auto", "off"}:
        raise SystemExit(
            f"--verify-reuse must be one of auto/off; got {getattr(args, 'verify_reuse', None)!r}."
        )

    wants_handoff = bool(args.commit) or bool(args.push) or bool(args.pr)

    # For ticket implementation workflows that create branches/PRs, it's easy to accidentally run
    # the next ticket off whatever branch your local repo currently has checked out.
    #
    # Default to the PR base branch (dev by default) unless the user explicitly provided --ref.
    effective_ref = args.ref
    if wants_handoff and (effective_ref is None or not str(effective_ref).strip()):
        base = str(getattr(args, "base_branch", "") or "").strip()
        if base:
            effective_ref = base

    # Similarly, when a ticket is being turned into a PR, prefer cloning from the repo's
    # configured remote (e.g. origin) so merged changes on the base branch are picked up even
    # if the local checkout is behind.
    effective_repo_input = repo_input
    if (
        wants_handoff
        and not repo_is_explicit
        and isinstance(repo_input, str)
        and _looks_like_local_path(repo_input)
    ):
        repo_path = Path(repo_input).expanduser()
        git_root = _infer_git_root(repo_path)
        if git_root is not None:
            remote_url = _git_remote_url(
                repo_dir=git_root,
                remote_name=str(getattr(args, "remote_name", "origin") or "origin"),
            )
            if remote_url is not None:
                effective_repo_input = remote_url

    exec_backend = str(args.exec_backend).strip().lower()
    maintenance_profile_eligible = _maintenance_profile_is_eligible(
        repo_root=repo_root,
        repo_input=str(effective_repo_input),
    )
    exec_docker_profile = _resolve_exec_docker_profile(
        exec_backend=exec_backend,
        requested_profile=getattr(args, "exec_docker_profile", None),
        maintenance_eligible=maintenance_profile_eligible,
    )

    if (
        wants_handoff
        and verification_profile == "default_handoff"
        and not verification_commands
        and not bool(getattr(args, "skip_verify", False))
    ):
        install_gate = "python tools/scaffold/scaffold.py run --all --skip-missing install"
        lint_gate = "python tools/scaffold/scaffold.py run --all --skip-missing lint"
        test_gate = "python tools/scaffold/scaffold.py run --all --skip-missing test"

        if exec_backend == "docker":
            scaffold_prefix = (
                'PYTHON_BIN=python; command -v "$PYTHON_BIN" >/dev/null 2>&1 || PYTHON_BIN=python3; '
                '"$PYTHON_BIN" tools/scaffold/scaffold.py run --all --skip-missing '
            )
            smoke_cmd = "bash ./scripts/smoke.sh"
            if exec_docker_profile == "maintenance":
                smoke_cmd = "bash ./scripts/smoke.sh --skip-install --use-pythonpath"
            verification_commands = [
                smoke_cmd,
                f"{scaffold_prefix}install",
                f"{scaffold_prefix}lint",
                f"{scaffold_prefix}test",
            ]
        elif os.name == "nt":
            verification_commands = [
                "powershell -NoProfile -ExecutionPolicy Bypass -File .\\scripts\\smoke.ps1",
                install_gate,
                lint_gate,
                test_gate,
            ]
        else:
            verification_commands = [
                "bash ./scripts/smoke.sh",
                install_gate,
                lint_gate,
                test_gate,
            ]

    exec_cache = str(getattr(args, "exec_cache", "cold") or "cold")
    exec_cache_dir = getattr(args, "exec_cache_dir", None)
    if exec_cache_dir is not None:
        exec_cache_dir = exec_cache_dir.resolve()
    if exec_cache == "warm" and exec_cache_dir is None:
        exec_cache_dir = repo_root / "runs" / "_cache" / "usertest_implement"
    maintenance_venv_cache = bool(
        exec_backend == "docker"
        and exec_cache == "warm"
        and bool(getattr(args, "maintenance_venv_cache", True))
    )

    ticket_blob = _compose_ticket_blob(selected)
    request = RunRequest(
        repo=str(effective_repo_input),
        ref=effective_ref,
        agent=str(args.agent),
        policy=str(args.policy),
        persona_id=args.persona_id,
        mission_id=args.mission_id,
        seed=int(args.seed),
        model=args.model,
        agent_config_overrides=tuple(args.agent_config_override or []),
        agent_append_system_prompt=ticket_blob,
        keep_workspace=keep_workspace,
        verification_commands=tuple(verification_commands),
        verification_timeout_seconds=verification_timeout_seconds,
        verification_reuse_mode=verification_reuse_mode,
        exec_backend=exec_backend,
        exec_docker_profile=exec_docker_profile,
        exec_keep_container=bool(args.exec_keep_container),
        exec_cache=exec_cache,
        exec_cache_dir=exec_cache_dir,
        exec_maintenance_venv_cache=maintenance_venv_cache,
        exec_use_host_agent_login=bool(args.exec_use_host_agent_login),
        exec_use_target_sandbox_cli_install=bool(args.exec_use_target_sandbox_cli_install),
    )

    if args.dry_run:
        selected_dict = asdict(selected)
        selected_dict["owner_root"] = (
            str(selected.owner_root) if selected.owner_root is not None else None
        )
        selected_dict["idea_path"] = str(selected.idea_path) if selected.idea_path is not None else None
        selected_dict["tickets_export_path"] = (
            str(selected.tickets_export_path) if selected.tickets_export_path is not None else None
        )
        payload = {
            "selected_ticket": selected_dict,
            "settings": settings_info,
            "run_request": {
                "repo": request.repo,
                "ref": request.ref,
                "agent": request.agent,
                "policy": request.policy,
                "persona_id": request.persona_id,
                "mission_id": request.mission_id,
                "seed": request.seed,
                "model": request.model,
                "keep_workspace": request.keep_workspace,
                "exec_backend": request.exec_backend,
                "exec_docker_profile": request.exec_docker_profile,
                "exec_docker_profile_eligible": maintenance_profile_eligible,
                "exec_keep_container": request.exec_keep_container,
                "exec_cache": request.exec_cache,
                "exec_maintenance_venv_cache": request.exec_maintenance_venv_cache,
                "verification_profile": verification_profile,
                "verification_commands": list(request.verification_commands),
                "verification_timeout_seconds": request.verification_timeout_seconds,
                "verification_reuse_mode": request.verification_reuse_mode,
                "commit": bool(args.commit),
                "push": bool(args.push),
                "pr": bool(args.pr),
                "move_on_start": bool(args.move_on_start),
                "move_on_commit": bool(args.move_on_commit),
            },
        }
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0

    if exec_backend == "docker":
        _require_docker_available()

    if args.move_on_start and selected.owner_root is not None and selected.idea_path is not None:
        try:
            move_ticket_file(
                owner_root=selected.owner_root,
                fingerprint=selected.fingerprint,
                to_bucket="3 - in_progress",
                dry_run=False,
            )
            _sync_ticket_atom_actions(repo_root=repo_root, owner_root=selected.owner_root)
        except Exception as e:
            print(f"WARNING: failed to move ticket to in_progress: {e}", file=sys.stderr)

    started_at = _utc_now_z()
    wall_start = time.monotonic()
    result = run_once(cfg, request)
    finished_at = _utc_now_z()
    duration_seconds = max(0.0, time.monotonic() - wall_start)

    run_dir = result.run_dir
    timing_payload = {
        "schema_version": 1,
        "started_at": started_at,
        "finished_at": finished_at,
        "duration_seconds": duration_seconds,
    }
    run_meta = _read_json(run_dir / "run_meta.json")
    if isinstance(run_meta, dict):
        phases = run_meta.get("phases")
        if isinstance(phases, dict):
            timing_payload["phases"] = phases
    _write_json(run_dir / "timing.json", timing_payload)
    _write_json(
        run_dir / "settings_ref.json",
        {
            "schema_version": 1,
            "settings": settings_info,
        },
    )
    _write_json(
        run_dir / "ticket_ref.json",
        {
            "schema_version": 1,
            "fingerprint": selected.fingerprint,
            "title": selected.title,
            "export_kind": selected.export_kind,
            "tickets_export_path": (
                str(selected.tickets_export_path) if selected.tickets_export_path is not None else None
            ),
            "export_index": selected.export_index,
            "owner_repo": {
                "root": str(selected.owner_root) if selected.owner_root is not None else None,
                "idea_path": str(selected.idea_path) if selected.idea_path is not None else None,
            },
        },
    )

    exit_code = int(result.exit_code or 0)
    verification_failed = False
    failing_verification_command: str | None = None

    verification_configured = bool(request.verification_commands)
    if verification_configured and not bool(getattr(args, "skip_verify", False)):
        verification = _read_json(run_dir / "verification.json")
        if isinstance(verification, dict) and verification.get("passed") is False:
            verification_failed = True
            exit_code = max(exit_code, 2)
            commands = verification.get("commands")
            if isinstance(commands, list):
                for cmd in commands:
                    if not isinstance(cmd, dict):
                        continue
                    cmd_exit = cmd.get("exit_code")
                    if isinstance(cmd_exit, int) and cmd_exit != 0:
                        raw_cmd = cmd.get("command")
                        if isinstance(raw_cmd, str) and raw_cmd.strip():
                            failing_verification_command = raw_cmd.strip()
                        break

            if wants_handoff:
                print(
                    "[implement] ERROR: Verification gate failed; refusing to commit/push/PR.",
                    file=sys.stderr,
                )
            else:
                print("[implement] ERROR: Verification gate failed.", file=sys.stderr)
            print(f"  Run dir: {run_dir}", file=sys.stderr)
            if failing_verification_command is not None:
                print(f"  Failing command: {failing_verification_command}", file=sys.stderr)
            print(
                "  Override (debugging only): rerun with --skip-verify",
                file=sys.stderr,
            )

    handoff_blocked = bool(wants_handoff and verification_failed and not args.skip_verify)

    workspace_ref = _read_json(run_dir / "workspace_ref.json")
    workspace_dir: Path | None = None
    if isinstance(workspace_ref, dict):
        ws = workspace_ref.get("workspace_dir")
        if isinstance(ws, str) and ws.strip():
            workspace_dir = Path(ws)

    push_candidates: list[Path] = []
    if selected.owner_root is not None and (selected.owner_root / ".git").exists():
        push_candidates.append(selected.owner_root)
    if _looks_like_local_path(repo_input) and (Path(repo_input) / ".git").exists():
        push_candidates.append(Path(repo_input))

    if args.branch:
        branch = args.branch
    else:
        branch = _resolve_default_branch_name(
            selected=selected,
            remote_name=str(args.remote_name),
            remote_url=args.remote_url,
            candidate_repo_dirs=push_candidates,
            wants_remote_handoff=bool(args.push or args.pr),
        )
    commit_message = (
        args.commit_message
        or f"{selected.fingerprint}: {selected.title or 'Implement backlog ticket'}"
    )

    git_ref: dict[str, Any] | None = None
    push_ref: dict[str, Any] | None = None
    pr_ref: dict[str, Any] | None = None

    observed_model = infer_observed_model(run_dir=run_dir)
    commit_performed = False

    if args.commit and not handoff_blocked:
        git_ref = finalize_commit(
            run_dir=run_dir,
            branch=branch,
            commit_message=commit_message,
            git_user_name=args.git_user_name,
            git_user_email=args.git_user_email,
        )
        commit_performed = bool(git_ref.get("commit_performed") is True)

    if args.push and not handoff_blocked:
        if not commit_performed:
            push_ref = {
                "schema_version": 1,
                "remote_name": str(args.remote_name),
                "remote_url": args.remote_url,
                "branch": branch,
                "force_with_lease": bool(args.force_push),
                "pushed": False,
                "stdout": None,
                "stderr": None,
                "error": "Skipping push: no commit was performed.",
            }
            _write_json(run_dir / "push_ref.json", push_ref)
        else:
            push_ref = finalize_push(
                run_dir=run_dir,
                remote_name=str(args.remote_name),
                remote_url=args.remote_url,
                candidate_repo_dirs=push_candidates,
                branch=branch,
                force_with_lease=bool(args.force_push),
            )

    if (args.push or args.pr) and not handoff_blocked:
        title, body = _write_pr_manifest(
            run_dir=run_dir,
            selected=selected,
            branch=branch,
            agent=str(args.agent),
            model=observed_model,
        )
        pr_ref = {
            "schema_version": 1,
            "requested": bool(args.pr),
            "created": False,
            "url": None,
            "title": title,
            "body": body,
            "agent": str(args.agent),
            "model": observed_model,
            "error": None,
        }
        if args.pr:
            if not commit_performed:
                pr_ref["error"] = "Skipping PR creation: no commit was performed."
            else:
                if workspace_dir is None:
                    pr_ref["error"] = "Missing workspace_ref.json; cannot locate workspace"
                else:
                    create_draft = False
                    pr_body = body

                    if bool(args.skip_ci_wait):
                        head_sha = _git_head_sha(workspace_dir)
                        _write_json(
                            run_dir / "ci_gate.json",
                            {
                                "schema_version": 1,
                                "workflow": "CI",
                                "branch": branch,
                                "head_sha": head_sha,
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
                                "timeout_seconds": _ci_timeout_seconds_arg(
                                    args.ci_timeout_seconds
                                ),
                            },
                        )
                    else:
                        if not (push_ref is not None and push_ref.get("pushed") is True):
                            pr_ref["error"] = (
                                "Refusing to create PR before CI: branch was not pushed successfully "
                                "(rerun with --push or pass --skip-ci-wait)."
                            )
                            _write_json(
                                run_dir / "ci_gate.json",
                                {
                                    "schema_version": 1,
                                    "workflow": "CI",
                                    "branch": branch,
                                    "head_sha": None,
                                    "run_id": None,
                                    "run_url": None,
                                    "status": None,
                                    "conclusion": None,
                                    "passed": None,
                                    "error": None,
                                    "skipped": True,
                                    "skip_reason": "branch_not_pushed",
                                    "started_at_utc": _utc_now_z(),
                                    "finished_at_utc": _utc_now_z(),
                                    "timeout_seconds": _ci_timeout_seconds_arg(
                                        args.ci_timeout_seconds
                                    ),
                                },
                            )
                        else:
                            head_sha = _git_head_sha(workspace_dir)
                            if head_sha is None:
                                pr_ref["error"] = "Unable to determine HEAD SHA for CI gating."
                                _write_json(
                                    run_dir / "ci_gate.json",
                                    {
                                        "schema_version": 1,
                                        "workflow": "CI",
                                        "branch": branch,
                                        "head_sha": None,
                                        "run_id": None,
                                        "run_url": None,
                                        "status": None,
                                        "conclusion": None,
                                        "passed": None,
                                        "error": pr_ref["error"],
                                        "skipped": True,
                                        "skip_reason": "head_sha_unavailable",
                                        "started_at_utc": _utc_now_z(),
                                        "finished_at_utc": _utc_now_z(),
                                        "timeout_seconds": _ci_timeout_seconds_arg(
                                            args.ci_timeout_seconds
                                        ),
                                    },
                                )
                            else:
                                ci_timeout = _ci_timeout_seconds_arg(args.ci_timeout_seconds)
                                ci_ref = _wait_for_ci_success(
                                    run_dir=run_dir,
                                    workspace_dir=workspace_dir,
                                    branch=branch,
                                    head_sha=head_sha,
                                    workflow="CI",
                                    timeout_seconds=ci_timeout,
                                )
                                pr_ref["ci_gate_passed"] = bool(ci_ref.get("passed") is True)
                                pr_ref["ci_gate_run_url"] = ci_ref.get("run_url")
                                if ci_ref.get("passed") is not True:
                                    if bool(args.draft_pr_on_ci_failure):
                                        create_draft = True
                                        ci_err = ci_ref.get("error") or "CI gate failed."
                                        pr_ref["ci_gate_error"] = ci_err
                                        pr_body = (
                                            pr_body.rstrip()
                                            + "\n\n---\n\nCI gate failed (draft PR created):\n\n"
                                            + f"- {ci_err}\n"
                                        )
                                    else:
                                        pr_ref["error"] = ci_ref.get("error") or "CI gate failed."
                                if create_draft:
                                    pr_ref["draft"] = True

                    if pr_ref.get("error"):
                        pass
                    else:
                        pr_ref["body"] = pr_body
                        try:
                            pr_url = _run_gh_text(
                                cwd=workspace_dir,
                                argv=[
                                    "gh",
                                    "pr",
                                    "create",
                                    "--base",
                                    str(args.base_branch),
                                    "--title",
                                    title,
                                    "--body",
                                    pr_body,
                                    *(["--draft"] if create_draft else []),
                                ],
                            ).strip()
                            pr_ref["created"] = True
                            pr_ref["url"] = pr_url or None
                        except RuntimeError as exc:
                            pr_ref["error"] = str(exc)
        _write_json(run_dir / "pr_ref.json", pr_ref)

    if (
        args.move_on_commit
        and selected.owner_root is not None
        and selected.idea_path is not None
        and _should_move_ticket_to_review(
            commit_performed=commit_performed,
            push_requested=bool(args.push),
            pr_requested=bool(args.pr),
            push_ref=push_ref,
            pr_ref=pr_ref,
        )
    ):
        try:
            move_ticket_file(
                owner_root=selected.owner_root,
                fingerprint=selected.fingerprint,
                to_bucket="4 - for_review",
                dry_run=False,
            )
            _sync_ticket_atom_actions(repo_root=repo_root, owner_root=selected.owner_root)
        except Exception as e:
            print(f"WARNING: failed to move ticket to for_review: {e}", file=sys.stderr)

    ledger_path: Path | None = None
    if args.ledger is not None:
        ledger_path = _resolve_ledger_path(repo_root=repo_root, raw=args.ledger)
        updates: dict[str, Any] = {
            "title": selected.title,
            "owner_root": str(selected.owner_root) if selected.owner_root is not None else None,
            "idea_path": str(selected.idea_path) if selected.idea_path is not None else None,
            "last_run_dir": str(run_dir),
            "last_exit_code": int(result.exit_code),
            "last_started_at": started_at,
            "last_finished_at": finished_at,
            "last_duration_seconds": duration_seconds,
        }
        if git_ref is not None:
            updates["last_branch"] = git_ref.get("branch")
            updates["last_head_commit"] = git_ref.get("head_commit")
        if push_ref is not None and push_ref.get("pushed") is True:
            updates["last_push_remote"] = push_ref.get("remote_name")
            updates["last_push_remote_url"] = push_ref.get("remote_url")
        if pr_ref is not None and isinstance(pr_ref.get("url"), str):
            updates["last_pr_url"] = pr_ref.get("url")

        try:
            update_ledger_file(ledger_path, fingerprint=selected.fingerprint, updates=updates)
        except Exception as e:
            print(f"WARNING: failed to update ledger: {e}", file=sys.stderr)

    review_required = bool(
        args.pr
        and isinstance(pr_ref, dict)
        and pr_ref.get("created") is True
        and isinstance(args.implementation_review_agent, str)
        and args.implementation_review_agent.strip()
    )
    review_run_dir: Path | None = None
    review_summary: dict[str, Any] | None = None
    review_error: str | None = None
    if review_required:
        owner_root = selected.owner_root if selected.owner_root is not None else repo_root
        resolved_ledger_path = (
            _resolve_ledger_path(repo_root=repo_root, raw=args.ledger)
            if args.ledger is not None
            else _DEFAULT_LEDGER_PATH
        )
        try:
            review_run_dir, review_summary = _run_review_for_selected_ticket(
                repo_root=repo_root,
                cfg=cfg,
                owner_root=owner_root,
                selected=selected,
                implementation_run_dir=run_dir,
                ledger_path=resolved_ledger_path,
                review_agent=str(args.implementation_review_agent),
                review_model=args.implementation_review_model,
                review_policy=str(args.policy),
                review_persona_id=_DEFAULT_REVIEW_PERSONA_ID,
                review_mission_id=_DEFAULT_REVIEW_MISSION_ID,
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
        except SystemExit as exc:
            review_error = str(exc)

    handoff_summary = _build_handoff_summary(
        branch=branch,
        commit_requested=bool(args.commit),
        commit_performed=commit_performed,
        push_requested=bool(args.push),
        push_ref=push_ref,
        pr_requested=bool(args.pr),
        pr_ref=pr_ref,
        ci_gate=_read_json(run_dir / "ci_gate.json"),
        review_required=review_required,
        review_run_dir=review_run_dir,
        review_summary=review_summary,
        review_error=review_error,
    )
    _write_json(run_dir / "handoff_summary.json", handoff_summary)

    if result.report_validation_errors:
        print("[implement] WARNING: report validation failed:", file=sys.stderr)
        for err in result.report_validation_errors:
            print(f"  - {err}", file=sys.stderr)
        exit_code = max(exit_code, 2)

    workspace_dir_str = str(workspace_dir) if workspace_dir else "<workspace not kept>"

    # Best-effort git operations: if the user asked for them and they failed, return non-zero and
    # provide a clear remediation path (changes may remain in the kept workspace).
    if args.commit and git_ref is not None and git_ref.get("error"):
        print("[implement] ERROR: git commit step failed:", file=sys.stderr)
        print(f"  {git_ref.get('error')}", file=sys.stderr)
        print(f"  Workspace: {workspace_dir_str}", file=sys.stderr)
        print("  Remediation:", file=sys.stderr)
        print(f"    cd {workspace_dir_str}", file=sys.stderr)
        print("    git status", file=sys.stderr)
        print("    # fix the issue, then retry commit/push/PR manually or rerun this command", file=sys.stderr)
        exit_code = max(exit_code, 3)

    if (args.push or args.pr) and push_ref is not None and push_ref.get("error"):
        print("[implement] ERROR: git push step failed:", file=sys.stderr)
        print(f"  {push_ref.get('error')}", file=sys.stderr)
        print(f"  Workspace: {workspace_dir_str}", file=sys.stderr)
        print("  Remediation:", file=sys.stderr)
        remote = push_ref.get("remote_name") or args.remote_name
        branch = None
        if isinstance(git_ref, dict):
            branch = git_ref.get("branch")
        if not branch:
            branch = args.branch or "<branch>"
        print(f"    cd {workspace_dir_str}", file=sys.stderr)
        print(f"    git push --set-upstream {remote} {branch}", file=sys.stderr)
        exit_code = max(exit_code, 4)

    if args.pr and pr_ref is not None and pr_ref.get("error"):
        print("[implement] ERROR: PR creation failed:", file=sys.stderr)
        print(f"  {pr_ref.get('error')}", file=sys.stderr)
        print(f"  Workspace: {workspace_dir_str}", file=sys.stderr)
        print("  Remediation:", file=sys.stderr)
        print(f"    cd {workspace_dir_str}", file=sys.stderr)
        print("    gh auth status", file=sys.stderr)
        print("    gh pr create --help", file=sys.stderr)
        exit_code = max(exit_code, 5)

    print(str(run_dir))
    return exit_code


def _cmd_run(args: argparse.Namespace) -> int:
    repo_root = _resolve_repo_root(args.repo_root)
    cfg = _load_runner_config(repo_root)

    selected: SelectedTicket
    if args.ticket_path is not None:
        selected = _select_ticket_from_path(args.ticket_path)
    else:
        if not args.fingerprint:
            raise SystemExit("Provide --fingerprint with --tickets-export.")
        selected = _select_ticket_from_export(
            tickets_export_path=args.tickets_export,
            fingerprint=str(args.fingerprint),
        )

    return _run_selected_ticket(args=args, repo_root=repo_root, cfg=cfg, selected=selected)




__all__ = [name for name in globals() if not name.startswith("__")]
