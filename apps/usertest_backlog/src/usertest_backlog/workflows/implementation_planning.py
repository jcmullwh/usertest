# ruff: noqa: E501,F401,F403,F405
from __future__ import annotations

from usertest_backlog.shared import *


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

            verification_steps = _coerce_string_list(plan.get("verification_steps"))
            if verification_steps:
                lines.append("- Verification steps:")
                for step in verification_steps[:12]:
                    lines.append(f"  - {step}")

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
    """Run stage 6 implementation planning and write the stage artifacts."""
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

        prompt = (
            template_text.replace("{{REPO_INTENT_MD}}", repo_intent_text)
            .replace("{{STAGE_GUIDANCE}}", stage_guidance_text)
            .replace("{{PROBLEM_RECORD_JSON}}", _json.dumps(rec, ensure_ascii=False, indent=2))
            .replace("{{RESEARCH_DOSSIER_JSON}}", _json.dumps(dossier, ensure_ascii=False, indent=2))
            .replace("{{SELECTION_DECISION_JSON}}", _json.dumps(decision, ensure_ascii=False, indent=2))
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

            base_plan = {
                "change_plan_id": plan1_id,
                "problem_id": pid,
                "selected_option_id": selected_option_id or "(no selected_option_id)",
                "title": _coerce_string(rec.get("title")) or f"Plan for {pid}",
                "problem": _coerce_string(rec.get("problem")) or "dry_run: synthesized problem summary",
                "user_impact": _coerce_string(rec.get("user_impact")) or "dry_run: synthesized user impact",
                "proposed_fix": (
                    _coerce_string(decision.get("selected_option", {}).get("summary"))
                    if isinstance(decision.get("selected_option"), dict)
                    else None
                )
                or "dry_run: synthesized proposed fix",
                "implementation_steps": [
                    "Identify change location(s) based on research dossier",
                    "Apply the selected approach with minimal surface area",
                ],
                "verification_steps": [
                    "Run relevant unit/integration tests",
                    "Re-run the failing scenario (or fixture) to confirm resolution",
                ],
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

            # Exercise split-plan handling: when the selected family is comprehensive, synthesize
            # a second plan that focuses on verification/docs, cross-linked via related IDs.
            if _coerce_string(decision.get("selected_family_id")) == "most_comprehensive":
                plan2_id = f"plan:{slug}:2"
                plans_payload[0]["related_change_plan_ids"] = [plan2_id]
                plans_payload.append(
                    {
                        **base_plan,
                        "change_plan_id": plan2_id,
                        "title": f"{base_plan['title']} (follow-up verification)",
                        "implementation_steps": [
                            "Add focused regression tests for edge cases",
                            "Update docs/help text if user-visible behavior changed",
                        ],
                        "verification_steps": [
                            "Run full test suite (or relevant subset) to confirm no regressions",
                        ],
                        "success_criteria": [
                            "Tests cover the class of failure described in research",
                        ],
                        "related_change_plan_ids": [plan1_id],
                    }
                )

            parsed_plans, parse_warnings = parse_change_plan_list(
                _json.dumps(plans_payload, ensure_ascii=False)
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
                )
                parsed_plans, parse_warnings = parse_change_plan_list(response)
                warnings_list.extend(parse_warnings)
            except Exception as exc:  # noqa: BLE001
                status = "error"
                warnings_list.append(f"change_planner_error: {pid}: {exc}")
                continue

        for plan in parsed_plans:
            plan_pid = _coerce_string(plan.get("problem_id"))
            if plan_pid is not None and plan_pid != pid:
                warnings_list.append(
                    f"change_plan_problem_id_mismatch: expected={pid} got={plan_pid}"
                )
            plan_oid = _coerce_string(plan.get("selected_option_id"))
            if selected_option_id is not None and plan_oid is not None and plan_oid != selected_option_id:
                warnings_list.append(
                    f"change_plan_selected_option_id_mismatch: problem_id={pid} "
                    f"expected={selected_option_id} got={plan_oid}"
                )
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
    out_json.write_text(_json.dumps(stage_doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

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
