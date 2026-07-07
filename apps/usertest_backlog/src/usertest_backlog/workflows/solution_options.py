# ruff: noqa: E501,F401,F403,F405
from __future__ import annotations

from usertest_backlog.shared import *


def _render_solution_options_markdown(
    options: list[dict[str, Any]],
    *,
    problem_records_by_id: dict[str, dict[str, Any]],
    family_order: list[str],
    family_labels_by_id: dict[str, str],
    title: str = "Solution Options",
) -> str:
    """Render stage-4 solution options as a human-readable Markdown document."""
    lines: list[str] = [f"# {title}\n"]
    if not options:
        lines.append("_No solution options produced._\n")
        return "\n".join(lines)

    by_problem: dict[str, list[dict[str, Any]]] = {}
    for opt in options:
        pid = opt.get("problem_id")
        if not isinstance(pid, str) or not pid.strip():
            continue
        by_problem.setdefault(pid.strip(), []).append(opt)

    for pid in sorted(by_problem):
        rec = problem_records_by_id.get(pid) or {}
        rec_title = rec.get("title") or pid
        lines.append(f"## {rec_title}")
        lines.append(f"**Problem ID**: `{pid}`\n")

        opts = by_problem[pid]
        by_family: dict[str, dict[str, Any]] = {}
        for opt in opts:
            fid = opt.get("family_id")
            if isinstance(fid, str) and fid.strip() and fid.strip() not in by_family:
                by_family[fid.strip()] = opt

        for fid in family_order or sorted(by_family):
            opt = by_family.get(fid)
            label = family_labels_by_id.get(fid, fid)
            lines.append(f"### {label} (`{fid}`)")
            if opt is None:
                lines.append("_Missing option for this family._\n")
                continue

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
            warn = opt.get("_parse_warning")
            if isinstance(warn, str) and warn.strip():
                lines.append(f"> ⚠ parse warning: {warn.strip()}")
            lines.append("")

        lines.append("")

    return "\n".join(lines)


def _run_solution_optioning_stage(
    *,
    repo_root: Path,
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
    """Run stage 4 solution optioning and write the stage artifacts."""
    import json as _json

    stage = "solution_optioning"
    stage_artifacts_dir = artifacts_dir / "solution_optioning"
    stage_artifacts_dir.mkdir(parents=True, exist_ok=True)

    taxonomy = pipeline_manifest.load_taxonomy()
    families_raw = taxonomy.get("solution_families")
    families = [f for f in families_raw if isinstance(f, dict)] if isinstance(families_raw, list) else []
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
    warnings_list: list[str] = []
    status: str = "ok"

    for idx, pid in enumerate(focus_ids, start=1):
        dossier = next(
            (d for d in research_dossiers if isinstance(d, dict) and d.get("problem_id") == pid),
            {},
        )
        rec = records_by_id.get(pid) or {}
        dec = priority_by_id.get(pid) or {}
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
            .replace("{{RESEARCH_DOSSIER_JSON}}", _json.dumps(dossier, ensure_ascii=False, indent=2))
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
            problem_slug = slugify(pid) or pid.replace(":", "_")
            for fid in family_order:
                label = family_labels_by_id.get(fid, fid)
                opt = {
                    "option_id": f"option:{problem_slug}:{fid}",
                    "problem_id": pid,
                    "family_id": fid,
                    "summary": f"[dry-run] {label} option for {rec.get('title') or pid}",
                    "tradeoffs": "dry_run: synthesized option; rerun without --dry-run for real tradeoffs",
                    "recurrence_prevention": "dry_run: unknown (research not executed)",
                    "change_surface_hypothesis": "unknown",
                    "test_implications": "dry_run: add/adjust tests once research is complete",
                    "rationale": "dry_run: synthesized from staged inputs; no agent executed",
                    "option_status": "optioned",
                }
                options.append(opt)
            parsed, parse_warnings = parse_solution_option_sets(
                _json.dumps(options, ensure_ascii=False), known_family_ids=known_family_ids
            )
            options = parsed
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
                parsed, parse_warnings = parse_solution_option_sets(
                    response, known_family_ids=known_family_ids
                )
                options = parsed
                warnings_list.extend(parse_warnings)
            except Exception as exc:  # noqa: BLE001
                status = "error"
                warnings_list.append(f"solution_optioner_error: {pid}: {exc}")
                continue

        # Enforce exactly one option per family per problem_id.
        by_family: dict[str, list[dict[str, Any]]] = {}
        for opt in options:
            fid = opt.get("family_id")
            if isinstance(fid, str) and fid.strip():
                by_family.setdefault(fid.strip(), []).append(opt)
        for fid in family_order:
            count = len(by_family.get(fid, []))
            if count != 1:
                status = "error"
                warnings_list.append(
                    f"solution_optioner_family_count_error: {pid}: {fid}: expected 1, got {count}"
                )
        # Attach pid for any malformed options that forgot it.
        for opt in options:
            if opt.get("problem_id") is None:
                opt["problem_id"] = pid

        all_options.extend(options)

    # Relation-review stage after option generation.
    relation_config_raw = yaml.safe_load(
        pipeline_manifest.relation_review_config_path.read_text(encoding="utf-8")
    )
    relation_config = relation_config_raw if isinstance(relation_config_raw, dict) else {}

    # Build one synthetic item per problem_id for relation review.
    option_sets: list[dict[str, Any]] = []
    for pid in focus_ids:
        rec = records_by_id.get(pid) or {}
        dossier = next(
            (d for d in research_dossiers if isinstance(d, dict) and d.get("problem_id") == pid),
            {},
        )
        opts = [o for o in all_options if o.get("problem_id") == pid]
        joined = " | ".join(
            [
                f"{o.get('family_id')}: {o.get('summary')}"
                for o in opts
                if isinstance(o, dict) and o.get("summary")
            ]
        )
        option_sets.append(
            {
                "problem_id": pid,
                "title": rec.get("title") or pid,
                "problem": rec.get("problem") or "",
                "user_impact": rec.get("user_impact") or "",
                "evidence_summary": rec.get("evidence_summary") or "",
                "evidence_atom_ids": rec.get("evidence_atom_ids") or [],
                "summary": joined,
                "root_cause_hypotheses": dossier.get("root_cause_hypotheses") or [],
            }
        )

    neighborhoods = rank_stage_related_items(
        option_sets,
        stage=stage,
        relation_config=relation_config,
        embedder=None,
    )

    allowed_actions = ["merge", "split", "same_cause_group", "keep_separate"]
    rel_template = pipeline_manifest.template_text(pipeline_manifest.relation_reviewer_template)
    rel_prompt = (
        rel_template.replace("{{STAGE_GUIDANCE}}", stage_guidance_text)
        .replace("{{ALLOWED_ACTIONS}}", _json.dumps(allowed_actions, ensure_ascii=False, indent=2))
        .replace("{{NEIGHBORHOODS_JSON}}", _json.dumps(neighborhoods, ensure_ascii=False, indent=2))
    )
    rel_tag = "solution_optioning_relation_review_001"
    rel_out_dir = stage_artifacts_dir / rel_tag
    rel_out_dir.mkdir(parents=True, exist_ok=True)
    relation_decisions: list[dict[str, Any]] = []
    if dry_run:
        (rel_out_dir / f"{rel_tag}.prompt.txt").write_text(rel_prompt, encoding="utf-8")
        (rel_out_dir / f"{rel_tag}.response.txt").write_text(
            "[dry-run] relation-review prompt not executed (offline mode).\n",
            encoding="utf-8",
        )
    else:
        try:
            rel_response = run_stage_prompt_json(
                stage=stage,
                prompt=rel_prompt,
                out_dir=rel_out_dir,
                tag=rel_tag,
                agent=agent,
                model=model,
                cfg=cfg,
            )
            raw = json.loads(rel_response)
            if not isinstance(raw, list):
                raise ValueError("relation_reviewer_response_not_a_list")
            relation_decisions = [d for d in raw if isinstance(d, dict)]
        except Exception as exc:  # noqa: BLE001
            status = "error"
            warnings_list.append(f"solution_optioning_relation_review_error: {exc}")
            relation_decisions = []

    updated_option_sets = apply_relation_decisions(
        option_sets,
        relation_decisions,
        stage=stage,
    )
    kept_ids = {
        str(item.get("problem_id"))
        for item in updated_option_sets
        if isinstance(item, dict) and isinstance(item.get("problem_id"), str)
    }
    if kept_ids:
        all_options = [
            opt
            for opt in all_options
            if isinstance(opt, dict) and isinstance(opt.get("problem_id"), str) and opt.get("problem_id") in kept_ids
        ]

    stage_doc = build_stage_document(
        stage,
        all_options,
        input_meta={
            "problem_record_count": len(problem_records),
            "priority_decision_count": len(priority_decisions),
            "research_dossier_count": len(research_dossiers),
            "family_ids": family_order,
            "dry_run": dry_run,
            "breadth_profile": breadth_profile,
            "batch_breadth": batch_breadth,
            "structurally_constant_batch_dimensions": batch_breadth.get(
                "structurally_constant_dimensions", []
            ),
            "solution_optioning_status": status,
            "solution_optioning_warnings": warnings_list,
            "relation_review_decisions": len(relation_decisions),
            "neighborhood_count": len(neighborhoods),
        },
        artifacts={
            "solution_options_json": str(out_json),
            "solution_options_md": str(out_md),
            "relation_review_prompt": str(rel_out_dir / f"{rel_tag}.prompt.txt"),
            "relation_review_response": str(rel_out_dir / f"{rel_tag}.response.txt"),
        },
    )

    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_md.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(_json.dumps(stage_doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    title = out_json.stem.removesuffix(".solution_options") or "Solution Options"
    out_md.write_text(
        _render_solution_options_markdown(
            [item for item in all_options if isinstance(item, dict)],
            problem_records_by_id=records_by_id,
            family_order=family_order,
            family_labels_by_id=family_labels_by_id,
            title=f"{title} – Solution Options",
        ),
        encoding="utf-8",
    )

    print(f"[stage4] wrote {out_json}", file=sys.stderr)
    print(f"[stage4] wrote {out_md}", file=sys.stderr)
    return stage_doc




__all__ = [name for name in globals() if not name.startswith("__")]
