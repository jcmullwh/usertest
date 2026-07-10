# ruff: noqa: E501,F401,F403,F405
from __future__ import annotations

from backlog_core import (
    assess_research_readiness,
    assess_selection_readiness,
    assign_plan_revision_id,
    bind_plan_outcome_oracle,
    infer_live_verification_requirement,
)
from backlog_repo.plan_scope import build_plan_target_contract

from usertest_backlog.shared import *
from usertest_backlog.workflows.depth_contracts import (
    assess_repo_grounding,
    change_plan_quality_errors,
    read_only_stage_tools,
    read_repo_revision,
    stage_include_directories,
)


def _render_change_plans_markdown(
    plans: list[dict[str, Any]],
    *,
    problem_records_by_id: dict[str, dict[str, Any]],
    title: str,
) -> str:
    """Render stage 6 change plans to a Markdown summary."""

    lines: list[str] = []
    lines.append(f"# {title}")
    lines.append("")

    if not plans:
        lines.append("_No change plans were produced._")
        lines.append("")
        return "\n".join(lines)

    by_problem: dict[str, list[dict[str, Any]]] = {}
    for plan in plans:
        pid = plan.get("problem_id")
        if isinstance(pid, str) and pid.strip():
            by_problem.setdefault(pid.strip(), []).append(plan)

    for pid in sorted(by_problem):
        rec = problem_records_by_id.get(pid) or {}
        problem_title = _coerce_string(rec.get("title")) or pid
        lines.append(f"## {problem_title}")
        lines.append("")
        lines.append(f"**Problem ID**: `{pid}`")
        lines.append("")

        for plan in by_problem[pid]:
            cid = _coerce_string(plan.get("change_plan_id")) or "(no change_plan_id)"
            lines.append(f"### {cid}")
            lines.append("")

            plan_revision_id = _coerce_string(plan.get("plan_revision_id"))
            if plan_revision_id:
                lines.append(f"- Plan revision ID: `{plan_revision_id}`")
                lines.append("- Plan revision source: `server_content_addressed_v1`")

            status = _coerce_string(plan.get("change_plan_status")) or "planned"
            lines.append(f"- Status: `{status}`")

            owner = _coerce_string(plan.get("suggested_owner"))
            if owner:
                lines.append(f"- Suggested owner: `{owner}`")

            selected_option_id = _coerce_string(plan.get("selected_option_id"))
            if selected_option_id:
                lines.append(f"- Selected option ID: `{selected_option_id}`")

            proposed_fix = _coerce_string(plan.get("proposed_fix"))
            if proposed_fix:
                lines.append(f"- Proposed fix: {proposed_fix}")

            rollback_notes = _coerce_string(plan.get("rollback_notes"))
            if rollback_notes:
                lines.append(f"- Rollback notes: {rollback_notes}")

            related_ids = _coerce_string_list(plan.get("related_change_plan_ids"))
            if related_ids:
                rel_s = ", ".join(f"`{rid}`" for rid in related_ids[:8])
                lines.append(f"- Related change plans: {rel_s}")

            implementation_steps = _coerce_string_list(plan.get("implementation_steps"))
            if implementation_steps:
                lines.append("- Implementation steps:")
                for step in implementation_steps[:12]:
                    lines.append(f"  - {step}")

            targets = plan.get("change_targets")
            if isinstance(targets, list) and targets:
                lines.append("- Exact change targets:")
                for target in targets[:12]:
                    if not isinstance(target, dict):
                        continue
                    path = target.get("path") or "(missing path)"
                    symbols = target.get("symbols") or []
                    symbol_text = ", ".join(map(str, symbols)) if symbols else "file-level"
                    lines.append(f"  - `{path}` ({symbol_text}): {target.get('change') or ''}")

            verification_steps = _coerce_string_list(plan.get("verification_steps"))
            if verification_steps:
                lines.append("- Verification steps:")
                for step in verification_steps[:12]:
                    lines.append(f"  - {step}")

            verification_commands = _coerce_string_list(plan.get("verification_commands"))
            if verification_commands:
                lines.append("- Verification commands:")
                for command in verification_commands[:12]:
                    lines.append(f"  - `{command}`")

            success_criteria = _coerce_string_list(plan.get("success_criteria"))
            if success_criteria:
                lines.append("- Success criteria:")
                for criterion in success_criteria[:12]:
                    lines.append(f"  - {criterion}")

            warn = _coerce_string(plan.get("_parse_warning"))
            if warn:
                lines.append(f"> ⚠ parse warning: {warn}")

            lines.append("")

        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def _run_implementation_planning_stage(
    *,
    repo_root: Path,
    target_repo_roots_by_problem: dict[str, Path] | None = None,
    problem_records: list[dict[str, Any]],
    research_dossiers: list[dict[str, Any]],
    solution_options: list[dict[str, Any]],
    selection_decisions: list[dict[str, Any]],
    pipeline_manifest: PipelinePromptManifest,
    artifacts_dir: Path,
    out_json: Path,
    out_md: Path,
    agent: str,
    model: str | None,
    cfg: RunnerConfig,
    dry_run: bool,
    stage_guidance_text: str,
) -> dict[str, Any]:
    """Run stage 6 using orchestrator prompts and per-problem target workspaces."""
    import json as _json

    stage = "implementation_planning"
    stage_artifacts_dir = artifacts_dir / stage
    stage_artifacts_dir.mkdir(parents=True, exist_ok=True)

    template_path = pipeline_manifest.change_planner_template
    if template_path is None:
        raise ValueError(
            "implementation_planning: pipeline_manifest.json is missing change_planner_template"
        )

    repo_intent_path = repo_root / "configs" / "repo_intent.md"
    if not repo_intent_path.exists():
        raise FileNotFoundError(f"Missing repo intent doc: {repo_intent_path}")
    repo_intent_text = repo_intent_path.read_text(encoding="utf-8", errors="replace")
    orchestrator_head_revision = read_repo_revision(repo_root)

    records_by_id: dict[str, dict[str, Any]] = {
        str(item.get("problem_id")): item
        for item in problem_records
        if isinstance(item, dict) and isinstance(item.get("problem_id"), str)
    }
    dossiers_by_id: dict[str, dict[str, Any]] = {
        str(item.get("problem_id")): item
        for item in research_dossiers
        if isinstance(item, dict) and isinstance(item.get("problem_id"), str)
    }
    options_by_id: dict[str, dict[str, Any]] = {
        str(item.get("option_id")): item
        for item in solution_options
        if isinstance(item, dict) and isinstance(item.get("option_id"), str)
    }
    decisions_by_problem: dict[str, dict[str, Any]] = {
        str(item.get("problem_id")): item
        for item in selection_decisions
        if isinstance(item, dict) and isinstance(item.get("problem_id"), str)
    }

    focus_ids = sorted(decisions_by_problem)
    template_text = pipeline_manifest.template_text(template_path)

    all_plans: list[dict[str, Any]] = []
    seen_plan_revision_ids: set[str] = set()
    rejected_plans: list[dict[str, Any]] = []
    warnings_list: list[str] = []
    status: str = "ok"

    for idx, pid in enumerate(focus_ids, start=1):
        decision_raw = decisions_by_problem.get(pid) or {}
        decision: dict[str, Any] = dict(decision_raw)

        selected_option_id = _coerce_string(decision.get("selected_option_id"))
        if selected_option_id and "selected_option" not in decision:
            opt = options_by_id.get(selected_option_id)
            if opt is not None:
                decision["selected_option"] = opt

        if selected_option_id is not None and not isinstance(decision.get("selected_option"), dict):
            warnings_list.append(f"implementation_planning_missing_selected_option: {pid}")

        rec = records_by_id.get(pid) or {}
        dossier = dossiers_by_id.get(pid) or {}
        target_repo_root = (
            target_repo_roots_by_problem.get(pid)
            if target_repo_roots_by_problem is not None
            else None
        )
        expected_case_id = _coerce_string(decision.get("case_id")) or _coerce_string(
            rec.get("case_id")
        )
        research_ready, research_blockers = assess_research_readiness(dossier)
        if research_ready:
            receipt_ready, receipt_blockers = verify_persisted_research_evidence(dossier)
            if not receipt_ready:
                research_ready = False
                research_blockers = [
                    f"persisted_research_evidence_invalid:{blocker}" for blocker in receipt_blockers
                ]
        research_revision = _coerce_string(dossier.get("repo_revision")) or ""
        if target_repo_root is None:
            grounded = False
            grounding_reasons = ["target_workspace_missing"]
            case_repo_context = {
                "requested_revision": research_revision,
                "access": "read_only",
            }
        else:
            grounded, grounding_reasons, case_repo_context = assess_repo_grounding(
                target_repo_root, research_revision
            )
        selection_ready, selection_blockers = assess_selection_readiness(
            decision,
            options=list(options_by_id.values()),
            research=dossier,
        )
        falsification = decision.get("falsification_review")
        falsification_verdict = (
            _coerce_string(falsification.get("verdict"))
            if isinstance(falsification, dict)
            else None
        )
        if (
            not research_ready
            or falsification_verdict != "accept"
            or not selection_ready
            or expected_case_id is None
            or not grounded
        ):
            reasons = []
            if not research_ready:
                reasons.append("research_not_ready: " + ", ".join(research_blockers))
            if falsification_verdict != "accept":
                reasons.append(
                    f"selection_not_falsification_accepted: {falsification_verdict or 'missing'}"
                )
            if not selection_ready:
                reasons.append("selection_not_ready: " + ", ".join(selection_blockers))
            if expected_case_id is None:
                reasons.append("case_id_missing")
            if not grounded:
                reasons.append("repository_not_grounded: " + ", ".join(grounding_reasons))
            warnings_list.extend(
                f"implementation_planning_blocked: {pid}: {item}" for item in reasons
            )
            rejected_plans.append(
                {
                    "problem_id": pid,
                    "selected_option_id": selected_option_id,
                    "planning_status": "blocked",
                    "reasons": reasons,
                }
            )
            continue
        prompt_dossier = research_prompt_projection(dossier)
        requires_live_verification, live_verification_reasons = infer_live_verification_requirement(
            rec, dossier
        )

        prompt = (
            template_text.replace("{{REPO_INTENT_MD}}", repo_intent_text)
            .replace("{{STAGE_GUIDANCE}}", stage_guidance_text)
            .replace(
                "{{REPO_CONTEXT_JSON}}",
                _json.dumps(case_repo_context, ensure_ascii=False, indent=2),
            )
            .replace("{{PROBLEM_RECORD_JSON}}", _json.dumps(rec, ensure_ascii=False, indent=2))
            .replace(
                "{{RESEARCH_DOSSIER_JSON}}",
                _json.dumps(prompt_dossier, ensure_ascii=False, indent=2),
            )
            .replace(
                "{{SELECTION_DECISION_JSON}}", _json.dumps(decision, ensure_ascii=False, indent=2)
            )
            .replace(
                "{{LIVE_VERIFICATION_REQUIREMENT_JSON}}",
                _json.dumps(
                    {
                        "requires_live_verification": requires_live_verification,
                        "reasons": live_verification_reasons,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
            )
        )

        tag = f"implementation_planning_{idx:03d}"
        run_out_dir = stage_artifacts_dir / tag
        run_out_dir.mkdir(parents=True, exist_ok=True)

        parsed_plans: list[dict[str, Any]] = []
        if dry_run:
            status = "dry_run_synthesized"
            (run_out_dir / f"{tag}.prompt.txt").write_text(prompt, encoding="utf-8")
            (run_out_dir / f"{tag}.response.txt").write_text(
                "[dry-run] stage-6 change planner prompt not executed (offline mode).\n",
                encoding="utf-8",
            )

            slug = pid.removeprefix("problem:").strip() or pid.strip()
            plan1_id = f"plan:{slug}:1"
            case_id = (
                _coerce_string(decision.get("case_id"))
                or _coerce_string(rec.get("case_id"))
                or f"case:{slug}"
            )

            base_plan = {
                "change_plan_id": plan1_id,
                "case_id": case_id,
                "problem_id": pid,
                "selected_option_id": selected_option_id or "(no selected_option_id)",
                "title": _coerce_string(rec.get("title")) or f"Plan for {pid}",
                "problem": _coerce_string(rec.get("problem"))
                or "dry_run: synthesized problem summary",
                "user_impact": _coerce_string(rec.get("user_impact"))
                or "dry_run: synthesized user impact",
                "proposed_fix": (
                    _coerce_string(decision.get("selected_option", {}).get("summary"))
                    if isinstance(decision.get("selected_option"), dict)
                    else None
                )
                or "dry_run: synthesized proposed fix",
                "repo_revision": research_revision,
                "change_targets": [
                    {
                        "action": "modify",
                        "path": (
                            "apps/usertest_backlog/src/usertest_backlog/workflows/"
                            "implementation_planning.py"
                        ),
                        "symbols": ["_run_implementation_planning_stage"],
                        "change": "dry_run: apply the selected mechanism at the named stage boundary",
                    }
                ],
                "implementation_steps": [
                    (
                        "Update `apps/usertest_backlog/src/usertest_backlog/workflows/"
                        "implementation_planning.py` at `_run_implementation_planning_stage` "
                        "according to the selected mechanism."
                    ),
                    "Add the corresponding regression fixture to the named test module.",
                ],
                "verification_steps": [
                    "Execute the focused backlog workflow tests with the recorded command.",
                ],
                "verification_commands": [
                    (
                        "pdm -p apps/usertest_backlog run pytest "
                        "tests/test_reports_backlog_command.py -q"
                    )
                ],
                "outcome_verification_roles": {
                    "original_scenario": None,
                    "live": None,
                    "mitigation_effect": None,
                    "recurrence": None,
                },
                "before_after_reproduction": {
                    "original_scenario": "dry_run: no original scenario was executed",
                    "before_change": None,
                    "after_change": None,
                    "proof_limitation": "Dry-run mode does not execute research or reproduction.",
                    "alternate_verification": (
                        "Run the focused workflow test command after a real evidence-sufficient run."
                    ),
                },
                "compatibility_and_failure_modes": {
                    "preserved_behaviors": ["Existing staged artifact envelopes remain readable."],
                    "intentional_changes": [],
                    "failure_modes": ["Reject output that does not satisfy the depth contract."],
                    "migration_required": False,
                },
                "causal_coverage": (
                    decision.get("selected_option", {}).get("causal_coverage")
                    if isinstance(decision.get("selected_option"), dict)
                    else {}
                )
                or {"mechanism_addressed": "dry_run placeholder"},
                "requires_live_verification": requires_live_verification,
                "live_verification_rationale": (
                    "Dry-run cannot establish whether the originating runtime behavior changed."
                ),
                "success_criteria": [
                    "The original failure no longer reproduces",
                    "A regression test prevents recurrence",
                ],
                "rollback_notes": "Revert the change set and remove the regression test if needed.",
                "suggested_owner": _coerce_string(decision.get("component"))
                or _coerce_string(rec.get("suggested_owner"))
                or "unknown",
                "change_plan_status": "planned",
                "related_change_plan_ids": [],
                "_dry_run_synthesized": True,
            }

            plans_payload: list[dict[str, Any]] = [base_plan]

            parsed_plans, parse_warnings = parse_change_plan_list(
                _json.dumps(plans_payload, ensure_ascii=False),
                allow_pending_target_contract=True,
            )
            warnings_list.extend(parse_warnings)
        else:
            try:
                response = run_stage_prompt_json(
                    stage=stage,
                    prompt=prompt,
                    out_dir=run_out_dir,
                    tag=tag,
                    agent=agent,
                    model=model,
                    cfg=cfg,
                    workspace_dir=target_repo_root,
                    allowed_tools=read_only_stage_tools(agent),
                    include_directories=stage_include_directories(agent, target_repo_root),
                )
                parsed_plans, parse_warnings = parse_change_plan_list(
                    response,
                    allow_pending_target_contract=True,
                )
                warnings_list.extend(parse_warnings)
            except Exception as exc:  # noqa: BLE001
                status = "error"
                warnings_list.append(f"change_planner_error: {pid}: {exc}")
                continue

        for plan in parsed_plans:
            # The model never owns lifecycle identity. Assign a deterministic server
            # content address after runner grounding and before readiness validation.
            # The target contract is derived from the clean research revision; model
            # output cannot provide or override its file/symbol hashes and ranges.
            target_contract_error: str | None = None
            outcome_oracle_error: str | None = None
            try:
                plan = {
                    **plan,
                    "target_contract": build_plan_target_contract(
                        plan,
                        repo_root=target_repo_root,
                    ),
                }
            except (OSError, UnicodeError, ValueError) as exc:
                target_contract_error = str(exc)
            if plan.get("_dry_run_synthesized") is not True:
                try:
                    plan = bind_plan_outcome_oracle(
                        plan,
                        research=dossier,
                        selection=decision,
                    )
                except ValueError as exc:
                    outcome_oracle_error = str(exc)
            plan = assign_plan_revision_id(plan)
            plan_errors: list[str] = []
            if target_contract_error is not None:
                plan_errors.append(
                    f"change_plan_target_contract_invalid:{pid}:{target_contract_error}"
                )
            if outcome_oracle_error is not None:
                plan_errors.append(
                    f"change_plan_outcome_oracle_invalid:{pid}:{outcome_oracle_error}"
                )
            plan_pid = _coerce_string(plan.get("problem_id"))
            if plan_pid is not None and plan_pid != pid:
                plan_errors.append(
                    f"change_plan_problem_id_mismatch: expected={pid} got={plan_pid}"
                )
            plan_oid = _coerce_string(plan.get("selected_option_id"))
            if (
                selected_option_id is not None
                and plan_oid is not None
                and plan_oid != selected_option_id
            ):
                plan_errors.append(
                    f"change_plan_selected_option_id_mismatch: problem_id={pid} "
                    f"expected={selected_option_id} got={plan_oid}"
                )
            plan_errors.extend(
                change_plan_quality_errors(
                    plan,
                    expected_revision=research_revision,
                    expected_case_id=expected_case_id,
                    repo_root=target_repo_root,
                    problem_record=rec,
                    research_dossier=dossier,
                    selection_decision=decision,
                )
            )
            plan_revision_id = _coerce_string(plan.get("plan_revision_id"))
            if plan_revision_id is not None and plan_revision_id in seen_plan_revision_ids:
                plan_errors.append(f"change_plan_duplicate_plan_revision_id: {plan_revision_id}")
            if plan_errors:
                status = "error"
                warnings_list.extend(plan_errors)
                rejected_plans.append(
                    {
                        "problem_id": pid,
                        "selected_option_id": selected_option_id,
                        "change_plan_id": plan.get("change_plan_id"),
                        "planning_status": "invalid_output",
                        "reasons": plan_errors,
                    }
                )
                continue
            assert plan_revision_id is not None
            seen_plan_revision_ids.add(plan_revision_id)
            all_plans.append(plan)

    stage_doc = build_stage_document(
        stage,
        all_plans,
        input_meta={
            "problem_record_count": len(problem_records),
            "research_dossier_count": len(research_dossiers),
            "option_count": len(solution_options),
            "decision_count": len(selection_decisions),
            "change_plan_count": len(all_plans),
            "rejected_plan_count": len(rejected_plans),
            "rejected_plans": rejected_plans,
            "orchestrator_head_revision": orchestrator_head_revision,
            "orchestrator_repo_root": str(repo_root.resolve()),
            "target_workspace_count": len(
                {str(path.resolve()) for path in (target_repo_roots_by_problem or {}).values()}
            )
            if target_repo_roots_by_problem is not None
            else 0,
            "repo_access": "read_only",
            "dry_run": dry_run,
            "implementation_planning_status": status,
            "implementation_planning_warnings": warnings_list,
            "change_planner_prompt_template": str(template_path),
        },
        artifacts={
            "change_plans_json": str(out_json),
            "change_plans_md": str(out_md),
        },
    )

    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_md.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(
        _json.dumps(stage_doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    title = out_json.stem.removesuffix(".change_plans") or "Change Plans"
    out_md.write_text(
        _render_change_plans_markdown(
            all_plans,
            problem_records_by_id=records_by_id,
            title=f"{title} – Change Plans",
        ),
        encoding="utf-8",
    )

    print(f"[stage6] wrote {out_json}", file=sys.stderr)
    print(f"[stage6] wrote {out_md}", file=sys.stderr)
    return stage_doc


__all__ = [name for name in globals() if not name.startswith("__")]
