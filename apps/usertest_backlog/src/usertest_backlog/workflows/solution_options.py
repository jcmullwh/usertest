# ruff: noqa: E501,F401,F403,F405
from __future__ import annotations

from backlog_core import assess_research_readiness

from usertest_backlog.shared import *
from usertest_backlog.workflows.depth_contracts import (
    assess_repo_grounding,
    parse_optioning_response,
    read_only_stage_tools,
    read_repo_revision,
    stage_include_directories,
)


def _render_solution_options_markdown(
    options: list[dict[str, Any]],
    *,
    problem_records_by_id: dict[str, dict[str, Any]],
    family_order: list[str],
    family_labels_by_id: dict[str, str],
    optioning_outcomes_by_id: dict[str, dict[str, Any]] | None = None,
    title: str = "Solution Options",
) -> str:
    """Render stage-4 solution options as a human-readable Markdown document."""
    lines: list[str] = [f"# {title}\n"]
    outcomes = optioning_outcomes_by_id or {}
    if not options and not outcomes:
        lines.append("_No solution options produced._\n")
        return "\n".join(lines)

    by_problem: dict[str, list[dict[str, Any]]] = {}
    for opt in options:
        pid = opt.get("problem_id")
        if not isinstance(pid, str) or not pid.strip():
            continue
        by_problem.setdefault(pid.strip(), []).append(opt)

    family_rank = {family_id: index for index, family_id in enumerate(family_order)}
    for pid in sorted(set(by_problem) | set(outcomes)):
        rec = problem_records_by_id.get(pid) or {}
        rec_title = rec.get("title") or pid
        lines.append(f"## {rec_title}")
        lines.append(f"**Problem ID**: `{pid}`\n")

        outcome = outcomes.get(pid) or {}
        outcome_status = outcome.get("optioning_status")
        if outcome_status:
            lines.append(f"- Optioning status: `{outcome_status}`")
        outcome_rationale = outcome.get("decision_rationale")
        if isinstance(outcome_rationale, str) and outcome_rationale.strip():
            lines.append(f"- Decision rationale: {outcome_rationale.strip()}")
        if outcome_status:
            lines.append("")

        opts = sorted(
            by_problem.get(pid, []),
            key=lambda item: (
                family_rank.get(str(item.get("family_id") or ""), len(family_rank)),
                str(item.get("option_id") or ""),
            ),
        )
        if not opts:
            lines.append("_No evidence-backed option advanced._\n")
            continue

        for opt in opts:
            fid = str(opt.get("family_id") or "unknown")
            label = family_labels_by_id.get(fid, fid)
            lines.append(f"### {label} (`{fid}`)")

            oid = opt.get("option_id") or "(no option_id)"
            lines.append(f"- Option ID: `{oid}`")
            summary = opt.get("summary") or ""
            if summary:
                lines.append(f"- Summary: {summary}")
            cs = opt.get("change_surface_hypothesis") or ""
            if cs:
                lines.append(f"- Change surface hypothesis: `{cs}`")
            tradeoffs = opt.get("tradeoffs") or ""
            if tradeoffs:
                lines.append(f"- Tradeoffs: {tradeoffs}")
            recurrence = opt.get("recurrence_prevention") or ""
            if recurrence:
                lines.append(f"- Recurrence prevention: {recurrence}")
            tests = opt.get("test_implications") or ""
            if tests:
                lines.append(f"- Test implications: {tests}")
            rationale = opt.get("rationale") or ""
            if rationale:
                lines.append(f"- Rationale: {rationale}")
            coverage = opt.get("causal_coverage")
            if isinstance(coverage, dict):
                mechanism = coverage.get("mechanism_addressed")
                if mechanism:
                    lines.append(f"- Mechanism addressed: {mechanism}")
                residual = coverage.get("residual_recurrence_paths")
                if isinstance(residual, list) and residual:
                    lines.append("- Residual recurrence paths: " + "; ".join(map(str, residual)))
            scope = opt.get("scope_evidence")
            if isinstance(scope, dict) and scope.get("scope_level"):
                lines.append(f"- Scope evidence: `{scope.get('scope_level')}`")
            warn = opt.get("_parse_warning")
            if isinstance(warn, str) and warn.strip():
                lines.append(f"> ⚠ parse warning: {warn.strip()}")
            lines.append("")

        lines.append("")

    return "\n".join(lines)


def _run_solution_optioning_stage(
    *,
    repo_root: Path,
    target_repo_roots_by_problem: dict[str, Path] | None = None,
    atoms: list[dict[str, Any]],
    problem_records: list[dict[str, Any]],
    priority_decisions: list[dict[str, Any]],
    research_dossiers: list[dict[str, Any]],
    pipeline_manifest: PipelinePromptManifest,
    artifacts_dir: Path,
    out_json: Path,
    out_md: Path,
    agent: str,
    model: str | None,
    cfg: RunnerConfig,
    dry_run: bool,
    breadth_profile: str,
    stage_guidance_text: str,
) -> dict[str, Any]:
    """Run stage 4 using orchestrator prompts and per-problem target workspaces."""
    import json as _json

    stage = "solution_optioning"
    stage_artifacts_dir = artifacts_dir / "solution_optioning"
    stage_artifacts_dir.mkdir(parents=True, exist_ok=True)

    taxonomy = pipeline_manifest.load_taxonomy()
    families_raw = taxonomy.get("solution_families")
    families = (
        [f for f in families_raw if isinstance(f, dict)] if isinstance(families_raw, list) else []
    )
    family_order: list[str] = []
    family_labels_by_id: dict[str, str] = {}
    for fam in families:
        fid = fam.get("family_id")
        if isinstance(fid, str) and fid.strip():
            family_order.append(fid.strip())
            label = fam.get("label")
            family_labels_by_id[fid.strip()] = (
                label.strip() if isinstance(label, str) and label.strip() else fid.strip()
            )
    known_family_ids = set(family_order)
    if not known_family_ids:
        raise ValueError(
            "solution_optioning: taxonomy.solution_families is empty; "
            f"check {pipeline_manifest.taxonomy_path}"
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
    priority_by_id: dict[str, dict[str, Any]] = {
        str(item.get("problem_id")): item
        for item in priority_decisions
        if isinstance(item, dict) and isinstance(item.get("problem_id"), str)
    }

    # Stable order for artifacts (deterministic).
    focus_ids = sorted(
        {
            str(d.get("problem_id"))
            for d in research_dossiers
            if isinstance(d, dict) and isinstance(d.get("problem_id"), str)
        }
    )

    template_text = pipeline_manifest.template_text(pipeline_manifest.solution_optioner_template)
    taxonomy_json = _json.dumps(taxonomy, ensure_ascii=False, indent=2)
    atoms_by_id: dict[str, dict[str, Any]] = {
        str(a.get("atom_id")): a
        for a in atoms
        if isinstance(a, dict) and isinstance(a.get("atom_id"), str)
    }
    batch_breadth = compute_batch_breadth(atoms)

    all_options: list[dict[str, Any]] = []
    optioning_outcomes: list[dict[str, Any]] = []
    warnings_list: list[str] = []
    status: str = "ok"

    for idx, pid in enumerate(focus_ids, start=1):
        dossier = next(
            (d for d in research_dossiers if isinstance(d, dict) and d.get("problem_id") == pid),
            {},
        )
        rec = records_by_id.get(pid) or {}
        dec = priority_by_id.get(pid) or {}
        priority_blockers: list[str] = []
        if _coerce_string(dec.get("_parse_warning")) is not None:
            priority_blockers.append("priority_parse_warning_present")
        if dec.get("selected_for_research") is not True:
            priority_blockers.append("priority_not_selected_for_research")
        if _coerce_string(dec.get("problem_id")) != pid:
            priority_blockers.append("priority_problem_id_mismatch")
        if _coerce_string(dec.get("case_id")) != _coerce_string(rec.get("case_id")):
            priority_blockers.append("priority_case_id_mismatch")
        if priority_blockers:
            optioning_outcomes.append(
                {
                    "problem_id": pid,
                    "optioning_status": "insufficient_evidence",
                    "decision_rationale": (
                        "The upstream priority decision is not valid for progression: "
                        + ", ".join(priority_blockers)
                    ),
                    "research_readiness_blockers": priority_blockers,
                    "option_count": 0,
                    "rejected_option_count": 0,
                }
            )
            continue
        research_ready, research_blockers = assess_research_readiness(dossier)
        if research_ready:
            receipt_ready, receipt_blockers = verify_persisted_research_evidence(dossier)
            if not receipt_ready:
                research_ready = False
                research_blockers = [
                    f"persisted_research_evidence_invalid:{blocker}" for blocker in receipt_blockers
                ]
        if not research_ready:
            optioning_outcomes.append(
                {
                    "problem_id": pid,
                    "optioning_status": "insufficient_evidence",
                    "decision_rationale": (
                        "Stage 3 research did not satisfy the evidence-readiness gate: "
                        + ", ".join(research_blockers)
                    ),
                    "research_readiness_blockers": research_blockers,
                    "option_count": 0,
                    "rejected_option_count": 0,
                }
            )
            continue
        prompt_dossier = research_prompt_projection(dossier)
        target_repo_root = (
            target_repo_roots_by_problem.get(pid)
            if target_repo_roots_by_problem is not None
            else None
        )
        if target_repo_root is None:
            optioning_outcomes.append(
                {
                    "problem_id": pid,
                    "optioning_status": "insufficient_evidence",
                    "decision_rationale": "No retained exact target workspace is available.",
                    "research_readiness_blockers": ["target_workspace_missing"],
                    "option_count": 0,
                    "rejected_option_count": 0,
                }
            )
            continue
        research_revision = _coerce_string(dossier.get("repo_revision")) or ""
        grounded, grounding_reasons, case_repo_context = assess_repo_grounding(
            target_repo_root, research_revision
        )
        if not grounded:
            optioning_outcomes.append(
                {
                    "problem_id": pid,
                    "optioning_status": "insufficient_evidence",
                    "decision_rationale": (
                        "The read-only planning workspace cannot inspect the exact "
                        f"research revision {research_revision!r}."
                    ),
                    "research_readiness_blockers": grounding_reasons,
                    "option_count": 0,
                    "rejected_option_count": 0,
                }
            )
            continue
        evidence_ids = (
            rec.get("evidence_atom_ids") if isinstance(rec.get("evidence_atom_ids"), list) else []
        )
        evidence_ids_s = [item for item in evidence_ids if isinstance(item, str) and item.strip()]
        problem_breadth = compute_problem_breadth(evidence_ids_s, atoms_by_id)
        decision_basis = _build_decision_basis(
            problem_breadth=problem_breadth,
            batch_breadth=batch_breadth,
        )

        prompt = (
            template_text.replace("{{REPO_INTENT_MD}}", repo_intent_text)
            .replace("{{STAGE_GUIDANCE}}", stage_guidance_text)
            .replace("{{TAXONOMY_JSON}}", taxonomy_json)
            .replace(
                "{{REPO_CONTEXT_JSON}}",
                _json.dumps(case_repo_context, ensure_ascii=False, indent=2),
            )
            .replace("{{BREADTH_PROFILE}}", breadth_profile)
            .replace(
                "{{PROBLEM_BREADTH_JSON}}",
                _json.dumps(problem_breadth, ensure_ascii=False, indent=2),
            )
            .replace(
                "{{BATCH_BREADTH_JSON}}",
                _json.dumps(batch_breadth, ensure_ascii=False, indent=2),
            )
            .replace(
                "{{STRUCTURALLY_CONSTANT_BATCH_DIMENSIONS_JSON}}",
                _json.dumps(
                    batch_breadth.get("structurally_constant_dimensions", []),
                    ensure_ascii=False,
                    indent=2,
                ),
            )
            .replace(
                "{{DECISION_BASIS_JSON}}",
                _json.dumps(decision_basis, ensure_ascii=False, indent=2),
            )
            .replace("{{PROBLEM_RECORD_JSON}}", _json.dumps(rec, ensure_ascii=False, indent=2))
            .replace("{{PRIORITY_DECISION_JSON}}", _json.dumps(dec, ensure_ascii=False, indent=2))
            .replace(
                "{{RESEARCH_DOSSIER_JSON}}",
                _json.dumps(prompt_dossier, ensure_ascii=False, indent=2),
            )
        )

        tag = f"solution_optioning_{idx:03d}"
        run_out_dir = stage_artifacts_dir / tag
        run_out_dir.mkdir(parents=True, exist_ok=True)

        options: list[dict[str, Any]] = []
        if dry_run:
            status = "dry_run_synthesized"
            (run_out_dir / f"{tag}.prompt.txt").write_text(prompt, encoding="utf-8")
            (run_out_dir / f"{tag}.response.txt").write_text(
                "[dry-run] stage-4 solution optioner prompt not executed (offline mode).\n",
                encoding="utf-8",
            )
            dry_response = {
                "problem_id": pid,
                "optioning_status": "insufficient_evidence",
                "decision_rationale": (
                    "dry_run: no research execution or repository inspection occurred"
                ),
                "options": [],
            }
            outcome, options, parse_warnings = parse_optioning_response(
                _json.dumps(dry_response, ensure_ascii=False),
                expected_problem_id=pid,
                known_family_ids=known_family_ids,
                research_dossier=dossier,
            )
            optioning_outcomes.append(outcome)
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
                outcome, options, parse_warnings = parse_optioning_response(
                    response,
                    expected_problem_id=pid,
                    known_family_ids=known_family_ids,
                    research_dossier=dossier,
                )
                optioning_outcomes.append(outcome)
                warnings_list.extend(parse_warnings)
                if outcome.get("optioning_status") == "invalid_output":
                    status = "error"
            except Exception as exc:  # noqa: BLE001
                status = "error"
                warnings_list.append(f"solution_optioner_error: {pid}: {exc}")
                optioning_outcomes.append(
                    {
                        "problem_id": pid,
                        "optioning_status": "invalid_output",
                        "decision_rationale": str(exc),
                        "option_count": 0,
                        "rejected_option_count": 0,
                    }
                )
                continue

        # Attach pid for any malformed options that forgot it.
        for opt in options:
            if opt.get("problem_id") is None:
                opt["problem_id"] = pid

        all_options.extend(options)

    stage_doc = build_stage_document(
        stage,
        all_options,
        input_meta={
            "problem_record_count": len(problem_records),
            "priority_decision_count": len(priority_decisions),
            "research_dossier_count": len(research_dossiers),
            "family_ids": family_order,
            "orchestrator_head_revision": orchestrator_head_revision,
            "orchestrator_repo_root": str(repo_root.resolve()),
            "target_workspace_count": len(
                {str(path.resolve()) for path in (target_repo_roots_by_problem or {}).values()}
            )
            if target_repo_roots_by_problem is not None
            else 0,
            "repo_access": "read_only",
            "optioning_outcomes": optioning_outcomes,
            "dry_run": dry_run,
            "breadth_profile": breadth_profile,
            "batch_breadth": batch_breadth,
            "structurally_constant_batch_dimensions": batch_breadth.get(
                "structurally_constant_dimensions", []
            ),
            "solution_optioning_status": status,
            "solution_optioning_warnings": warnings_list,
            "post_research_relation_review": (
                "runner_verified_mechanism_identity_v2; matching researched cases "
                "are canonicalized before optioning"
            ),
            "post_research_canonical_bundle_count": sum(
                1
                for dossier in research_dossiers
                if isinstance(dossier.get("post_research_same_mechanism_bundle"), dict)
            ),
        },
        artifacts={
            "solution_options_json": str(out_json),
            "solution_options_md": str(out_md),
        },
    )

    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_md.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(
        _json.dumps(stage_doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    title = out_json.stem.removesuffix(".solution_options") or "Solution Options"
    out_md.write_text(
        _render_solution_options_markdown(
            [item for item in all_options if isinstance(item, dict)],
            problem_records_by_id=records_by_id,
            family_order=family_order,
            family_labels_by_id=family_labels_by_id,
            optioning_outcomes_by_id={
                str(item["problem_id"]): item
                for item in optioning_outcomes
                if isinstance(item.get("problem_id"), str)
            },
            title=f"{title} – Solution Options",
        ),
        encoding="utf-8",
    )

    print(f"[stage4] wrote {out_json}", file=sys.stderr)
    print(f"[stage4] wrote {out_md}", file=sys.stderr)
    return stage_doc


__all__ = [name for name in globals() if not name.startswith("__")]
