# ruff: noqa: E501,F401,F403,F405
from __future__ import annotations

from collections.abc import Mapping

from backlog_core import (
    assess_research_readiness,
    bind_falsification_review,
    verified_mechanism_evidence,
    verified_outcome_oracles,
)

from usertest_backlog.shared import *
from usertest_backlog.workflows.depth_contracts import (
    assess_repo_grounding,
    falsification_review_errors,
    read_only_stage_tools,
    read_repo_revision,
    research_contract_view,
    selection_quality_errors,
    stage_include_directories,
)
from usertest_backlog.workflows.selection_healing import run_stage5_live_case


def _render_solution_selection_markdown(
    decisions: list[dict[str, Any]],
    *,
    problem_records_by_id: dict[str, dict[str, Any]],
    family_labels_by_id: dict[str, str],
    title: str = "Solution Selection",
) -> str:
    """Render stage-5 selection decisions as a human-readable Markdown document."""
    lines: list[str] = [f"# {title}\n"]
    if not decisions:
        lines.append("_No solution selection decisions produced._\n")
        return "\n".join(lines)

    for dec in decisions:
        pid = dec.get("problem_id") or "(no problem_id)"
        rec = problem_records_by_id.get(str(pid)) or {}
        rec_title = rec.get("title") or pid

        sel_oid = dec.get("selected_option_id") or "(no selected_option_id)"
        sel_fid = _coerce_string(dec.get("selected_family_id"))
        needs_ux = dec.get("needs_ux_review")
        needs_ux_s = "true" if needs_ux is True else "false" if needs_ux is False else "?"

        lines.append(f"## {rec_title}")
        selection_line = f"**Problem ID**: `{pid}` | **Selected**: `{sel_oid}`"
        if sel_fid is not None:
            label = family_labels_by_id.get(sel_fid, sel_fid)
            selection_line += f" | **Family telemetry**: {label} (`{sel_fid}`)"
        selection_line += f" | **Needs UX review**: {needs_ux_s}\n"
        lines.append(selection_line)

        rationale = dec.get("selection_rationale") or ""
        if rationale:
            lines.append(f"**Rationale**: {rationale}\n")
        align = dec.get("repo_intent_alignment") or ""
        if align:
            lines.append(f"**Repo intent alignment**: {align}\n")
        other = dec.get("why_other_options_were_not_selected") or ""
        if other:
            lines.append(f"**Why not other options**: {other}\n")

        falsification = dec.get("falsification_review")
        if isinstance(falsification, dict):
            verdict = falsification.get("verdict") or "unknown"
            counterargument = falsification.get("strongest_counterargument") or ""
            lines.append(f"- Falsification verdict: `{verdict}`")
            if counterargument:
                lines.append(f"- Strongest counterargument: {counterargument}")
            evidence_refs = falsification.get("evidence_refs")
            if isinstance(evidence_refs, list) and evidence_refs:
                lines.append("- Falsification evidence:")
                for evidence_ref in evidence_refs:
                    if not isinstance(evidence_ref, dict):
                        continue
                    lines.append(
                        "  - `"
                        + str(evidence_ref.get("ref") or "unknown")
                        + "` (`"
                        + str(evidence_ref.get("effect") or "unknown")
                        + "`): "
                        + str(evidence_ref.get("finding") or "")
                    )
            risk_dispositions = falsification.get("material_risk_dispositions")
            if isinstance(risk_dispositions, list) and risk_dispositions:
                lines.append("- Material risk dispositions:")
                for disposition in risk_dispositions:
                    if not isinstance(disposition, dict):
                        continue
                    lines.append(
                        "  - `"
                        + str(disposition.get("disposition") or "unknown")
                        + "`: "
                        + str(disposition.get("risk") or "")
                    )

        change_surface = dec.get("change_surface")
        cs = change_surface if isinstance(change_surface, dict) else {}
        kinds = cs.get("kinds") or []
        kinds_list = (
            [k for k in kinds if isinstance(k, str) and k.strip()]
            if isinstance(kinds, list)
            else []
        )
        if kinds_list:
            lines.append(
                "- Labeled change surface: "
                + ", ".join(f"`{k}`" for k in kinds_list[:8])
                + (" …" if len(kinds_list) > 8 else "")
            )
        comp = dec.get("component")
        if isinstance(comp, str) and comp.strip():
            lines.append(f"- Component: `{comp.strip()}`")
        risk = dec.get("intent_risk")
        if isinstance(risk, str) and risk.strip():
            lines.append(f"- Intent risk: `{risk.strip()}`")

        warn = dec.get("_parse_warning")
        if isinstance(warn, str) and warn.strip():
            lines.append(f"> ⚠ parse warning: {warn.strip()}")

        lines.append("")

    return "\n".join(lines)


def _run_solution_selection_stage(
    *,
    repo_root: Path,
    target_repo_roots_by_problem: dict[str, Path] | None = None,
    atoms: list[dict[str, Any]],
    problem_records: list[dict[str, Any]],
    research_dossiers: list[dict[str, Any]],
    solution_options: list[dict[str, Any]],
    solution_optioning_stage_doc: dict[str, Any] | None = None,
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
    external_corrections_by_problem: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Run stage 5 using orchestrator prompts and per-problem target workspaces."""
    import json as _json

    stage = "solution_selection"
    stage_artifacts_dir = artifacts_dir / "solution_selection"
    stage_artifacts_dir.mkdir(parents=True, exist_ok=True)
    invocation_tracker = ModelInvocationTracker(stage_artifacts_dir)
    live_prompt_expected_count = 0

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

    repo_intent_path = repo_root / "configs" / "repo_intent.md"
    if not repo_intent_path.exists():
        raise FileNotFoundError(f"Missing repo intent doc: {repo_intent_path}")
    repo_intent_text = repo_intent_path.read_text(encoding="utf-8", errors="replace")
    orchestrator_head_revision = read_repo_revision(repo_root)

    falsifier_template_path = pipeline_manifest.solution_falsifier_template
    if falsifier_template_path is None:
        raise ValueError(
            "solution_selection: pipeline_manifest.json is missing solution_falsifier_template"
        )

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
    options_by_problem: dict[str, list[dict[str, Any]]] = {}
    options_by_id: dict[str, dict[str, Any]] = {}
    for opt in solution_options:
        pid = opt.get("problem_id")
        oid = opt.get("option_id")
        if isinstance(pid, str) and pid.strip() and isinstance(oid, str) and oid.strip():
            options_by_problem.setdefault(pid.strip(), []).append(opt)
            options_by_id[oid.strip()] = opt

    focus_ids = sorted(options_by_problem.keys())

    selector_template = pipeline_manifest.template_text(
        pipeline_manifest.solution_selector_template
    )
    falsifier_template = pipeline_manifest.template_text(falsifier_template_path)
    labeler_template = pipeline_manifest.template_text(
        pipeline_manifest.selected_solution_labeler_template
    )

    atoms_by_id: dict[str, dict[str, Any]] = {
        str(a.get("atom_id")): a
        for a in atoms
        if isinstance(a, dict) and isinstance(a.get("atom_id"), str)
    }
    batch_breadth = compute_batch_breadth(atoms)

    decisions: list[dict[str, Any]] = []
    selection_outcomes: list[dict[str, Any]] = []
    role_healing_runs: list[dict[str, Any]] = []
    cross_role_feedback: list[dict[str, Any]] = []
    option_revisions: list[dict[str, Any]] = []
    warnings_list: list[str] = []
    status: str = "ok"

    for idx, pid in enumerate(focus_ids, start=1):
        rec = records_by_id.get(pid) or {}
        dossier = research_contract_view(dossiers_by_id.get(pid))
        opts = options_by_problem.get(pid) or []
        research_ready, research_blockers = assess_research_readiness(dossier)
        if research_ready:
            receipt_ready, receipt_blockers = verify_persisted_research_evidence(dossier)
            if not receipt_ready:
                research_ready = False
                research_blockers = [
                    f"persisted_research_evidence_invalid:{blocker}" for blocker in receipt_blockers
                ]
        if not research_ready:
            selection_outcomes.append(
                {
                    "problem_id": pid,
                    "selection_status": "insufficient_evidence",
                    "reasons": research_blockers,
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
            warning = f"solution_selection_target_workspace_missing: {pid}"
            status = "error"
            warnings_list.append(warning)
            selection_outcomes.append(
                {
                    "problem_id": pid,
                    "selection_status": "insufficient_evidence",
                    "reasons": [warning],
                }
            )
            continue
        research_revision = _coerce_string(dossier.get("repo_revision")) or ""
        grounded, grounding_reasons, case_repo_context = assess_repo_grounding(
            target_repo_root, research_revision
        )
        if not grounded:
            warning = (
                f"solution_selection_research_revision_unavailable: {pid}: {research_revision!r}"
            )
            status = "error"
            warnings_list.append(warning)
            selection_outcomes.append(
                {
                    "problem_id": pid,
                    "selection_status": "insufficient_evidence",
                    "reasons": grounding_reasons,
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
            selector_template.replace("{{REPO_INTENT_MD}}", repo_intent_text)
            .replace("{{STAGE_GUIDANCE}}", stage_guidance_text)
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
            .replace(
                "{{RESEARCH_DOSSIER_JSON}}",
                _json.dumps(prompt_dossier, ensure_ascii=False, indent=2),
            )
            .replace("{{SOLUTION_OPTIONS_JSON}}", _json.dumps(opts, ensure_ascii=False, indent=2))
        )

        tag = f"solution_selection_{idx:03d}"
        run_out_dir = stage_artifacts_dir / tag
        run_out_dir.mkdir(parents=True, exist_ok=True)

        if not dry_run:
            live_prompt_expected_count += 1
            evidence_atoms_preview: list[dict[str, Any]] = []
            for eid in evidence_ids_s[:12]:
                atom = atoms_by_id.get(eid)
                if atom is None:
                    continue
                evidence_atoms_preview.append(
                    {
                        "atom_id": atom.get("atom_id"),
                        "run_rel": atom.get("run_rel"),
                        "source": atom.get("source"),
                        "severity_hint": atom.get("severity_hint"),
                        "text": atom.get("text"),
                        "artifact_ref": atom.get("artifact_ref"),
                    }
                )
            external_correction = (
                external_corrections_by_problem.get(pid)
                if external_corrections_by_problem is not None
                else None
            )
            live_result = run_stage5_live_case(
                problem_id=pid,
                index=idx,
                selector_prompt=prompt,
                falsifier_template=falsifier_template,
                labeler_template=labeler_template,
                repo_context=case_repo_context,
                problem_record=rec,
                prompt_dossier=prompt_dossier,
                research_dossier=dossier,
                initial_options=opts,
                stage4_doc=solution_optioning_stage_doc,
                stage_artifacts_dir=stage_artifacts_dir,
                target_repo_root=target_repo_root,
                repo_revision=research_revision,
                evidence_atoms_preview=evidence_atoms_preview,
                evidence_atom_ids=evidence_ids_s,
                known_family_ids=set(family_order),
                agent=agent,
                model=model,
                cfg=cfg,
                initial_resume_session_id=(
                    _coerce_string(external_correction.get("agent_session_id"))
                    if isinstance(external_correction, Mapping)
                    else None
                ),
                author_cost_seconds=(
                    external_correction.get("original_author_cost_seconds")
                    if isinstance(external_correction, Mapping)
                    else None
                ),
                external_feedback=(
                    external_correction.get("feedback")
                    if isinstance(external_correction, Mapping)
                    and isinstance(external_correction.get("feedback"), Mapping)
                    else None
                ),
            )
            selection_outcomes.append(dict(live_result["outcome"]))
            warnings_list.extend(live_result["warnings"])
            role_healing_runs.extend(live_result["role_runs"])
            cross_role_feedback.extend(live_result["feedback"])
            revised = [
                dict(option)
                for option in live_result["revised_options"]
                if isinstance(option, dict)
            ]
            if revised:
                revised_ids = {
                    str(option.get("option_id"))
                    for option in revised
                    if isinstance(option.get("option_id"), str)
                }
                option_revisions.append(
                    {
                        "problem_id": pid,
                        "revised_option_ids": sorted(revised_ids),
                        "options": revised,
                    }
                )
                options_by_problem[pid] = revised
                for option_id, option in list(options_by_id.items()):
                    if option.get("problem_id") == pid and option_id not in revised_ids:
                        options_by_id.pop(option_id, None)
                for option in revised:
                    option_id = option.get("option_id")
                    if isinstance(option_id, str):
                        options_by_id[option_id] = option
            selected_live = live_result.get("decision")
            if not isinstance(selected_live, dict):
                if live_result["status"] not in {
                    "insufficient_evidence",
                    "no_safe_option",
                }:
                    status = "completed_with_repairable_cases"
                continue
            selected_dec = dict(selected_live)
            for key in (
                "title",
                "problem",
                "user_impact",
                "severity",
                "confidence",
                "evidence_atom_ids",
                "evidence_summary",
            ):
                if key in rec and key not in selected_dec:
                    selected_dec[key] = rec.get(key)
            selected_dec["breadth"] = dict(problem_breadth)
            selected_dec["problem_breadth"] = dict(problem_breadth)
            selected_dec["breadth_profile"] = breadth_profile
            selected_dec["decision_basis"] = decision_basis
            selected_dec["batch_breadth"] = batch_breadth
            selected_dec["structurally_constant_batch_dimensions"] = batch_breadth.get(
                "structurally_constant_dimensions",
                [],
            )
            selected_dec["review_domain"] = _infer_review_domain(
                change_surface=selected_dec.get("change_surface")
                if isinstance(selected_dec.get("change_surface"), dict)
                else None,
                needs_ux_review=bool(selected_dec.get("needs_ux_review") is True),
            )
            selected_dec.setdefault("stage", "research_required")
            decisions.append(selected_dec)
            continue

        selected_dec: dict[str, Any] | None = None
        if dry_run:
            status = "dry_run_synthesized"
            (run_out_dir / f"{tag}.prompt.txt").write_text(prompt, encoding="utf-8")
            (run_out_dir / f"{tag}.response.txt").write_text(
                "[dry-run] stage-5 solution selector prompt not executed (offline mode).\n",
                encoding="utf-8",
            )
            # Dry-run preserves the first supplied mechanism without imposing a family
            # ranking. Real selection is always evidence-scored by the agent.
            chosen = opts[0] if opts else None
            if chosen is None:
                status = "error"
                warnings_list.append(f"solution_selection_no_options: {pid}")
                continue
            fid = _coerce_string(chosen.get("family_id"))
            oid = chosen.get("option_id") or "(no option_id)"
            needs_ux = bool("new_command" in str(chosen.get("change_surface_hypothesis") or ""))
            candidate = {
                "problem_id": pid,
                "selected_option_id": oid,
                "selection_rationale": "dry_run: synthesized selection; rerun without --dry-run for real rationale",
                "repo_intent_alignment": "dry_run: synthesized",
                "why_other_options_were_not_selected": "dry_run: synthesized",
                "needs_ux_review": needs_ux,
                "selection_status": "selected",
                "causal_coverage_evaluation": {
                    "mechanism_fit": "dry_run: supplied mechanism retained",
                    "accepted_unsupported_assumptions": [],
                    "accepted_residual_risks": [],
                    "class_level_evidence_sufficient": False,
                },
            }
            if fid is not None:
                candidate["selected_family_id"] = fid
            parsed, parse_warnings = parse_selection_decisions(
                _json.dumps([candidate], ensure_ascii=False)
            )
            selected_dec = parsed[0] if parsed else None
            warnings_list.extend(parse_warnings)
        if selected_dec is None:
            status = "error"
            warnings_list.append(f"solution_selector_missing_decision: {pid}")
            continue

        selected_option_id = selected_dec.get("selected_option_id")
        selected_option = (
            options_by_id.get(str(selected_option_id))
            if isinstance(selected_option_id, str)
            else None
        )
        if selected_option is not None:
            selected_dec["selected_option"] = selected_option

        quality_errors = selection_quality_errors(
            selected_dec,
            expected_problem_id=pid,
            options_by_id=options_by_id,
            research_dossier=dossier,
        )
        if quality_errors:
            status = "error"
            warnings_list.extend(quality_errors)
            selection_outcomes.append(
                {
                    "problem_id": pid,
                    "selection_status": "invalid_output",
                    "selected_option_id": selected_option_id,
                    "reasons": quality_errors,
                }
            )
            continue

        falsifier_prompt = (
            falsifier_template.replace(
                "{{REPO_CONTEXT_JSON}}",
                _json.dumps(case_repo_context, ensure_ascii=False, indent=2),
            )
            .replace("{{PROBLEM_RECORD_JSON}}", _json.dumps(rec, ensure_ascii=False, indent=2))
            .replace(
                "{{RESEARCH_DOSSIER_JSON}}",
                _json.dumps(prompt_dossier, ensure_ascii=False, indent=2),
            )
            .replace("{{SOLUTION_OPTIONS_JSON}}", _json.dumps(opts, ensure_ascii=False, indent=2))
            .replace(
                "{{SELECTION_DECISION_JSON}}",
                _json.dumps(selected_dec, ensure_ascii=False, indent=2),
            )
        )
        falsifier_tag = f"solution_falsification_{idx:03d}"
        falsifier_out_dir = stage_artifacts_dir / falsifier_tag
        falsifier_out_dir.mkdir(parents=True, exist_ok=True)
        review: dict[str, Any]
        if dry_run:
            (falsifier_out_dir / f"{falsifier_tag}.prompt.txt").write_text(
                falsifier_prompt, encoding="utf-8"
            )
            (falsifier_out_dir / f"{falsifier_tag}.response.txt").write_text(
                "[dry-run] stage-5 falsification prompt not executed (offline mode).\n",
                encoding="utf-8",
            )
            dry_evidence_ref = next(
                iter(verified_mechanism_evidence(dossier)),
                "",
            )
            dry_material_risks: list[str] = []
            dry_coverage = (
                selected_option.get("causal_coverage")
                if isinstance(selected_option, dict)
                else None
            )
            if isinstance(dry_coverage, dict):
                for risk_field in (
                    "unsupported_assumptions",
                    "residual_recurrence_paths",
                    "compatibility_risks",
                ):
                    dry_material_risks.extend(_coerce_string_list(dry_coverage.get(risk_field)))
            dry_review_risk = "dry_run output is not implementation-ready"
            dry_material_risks.append(dry_review_risk)
            dry_oracles = list(verified_outcome_oracles(dossier).values())
            dry_positive_contracts = [
                contract
                for oracle in dry_oracles
                for contract in oracle.get("positive_outcome_contracts", [])
                if isinstance(contract, dict)
                and isinstance(contract.get("positive_outcome_contract_id"), str)
            ]
            dry_selected_contract_ids = [
                str(contracts[0]["positive_outcome_contract_id"])
                for oracle in dry_oracles
                for contracts in [
                    [
                        contract
                        for contract in oracle.get("positive_outcome_contracts", [])
                        if isinstance(contract, dict)
                        and isinstance(contract.get("positive_outcome_contract_id"), str)
                    ]
                ]
                if contracts
            ]
            review = {
                "problem_id": pid,
                "selected_option_id": str(selected_option_id),
                "verdict": "accept",
                "strongest_counterargument": "dry_run: evidence was not independently tested",
                "evidence_refs": [
                    {
                        "ref": dry_evidence_ref,
                        "finding": "dry_run: supplied evidence reference was not re-tested",
                        "effect": "limits_scope",
                    }
                ],
                "unsupported_assumptions": [],
                "residual_risks": [dry_review_risk],
                "critical_findings": [],
                "evidence_that_would_change_verdict": (
                    "Run stage 5 against an evidence-sufficient dossier and repository revision."
                ),
                "material_risk_dispositions": [
                    {
                        "risk": risk,
                        "disposition": "accepted",
                        "evidence_refs": [dry_evidence_ref],
                        "rationale": "dry_run: disposition is not implementation-ready",
                    }
                    for risk in dry_material_risks
                ],
                "selected_positive_outcome_contract_id": (
                    dry_selected_contract_ids[0] if len(dry_selected_contract_ids) == 1 else None
                ),
                "selected_positive_outcome_contract_ids": dry_selected_contract_ids,
                "outcome_contract_reviews": [
                    {
                        "positive_outcome_contract_id": contract["positive_outcome_contract_id"],
                        "verdict": "sufficient",
                        "semantic_relation_assessment": (
                            "dry_run placeholder; no independent semantic review ran"
                        ),
                        "proves_intended_operation": True,
                        "problem_coverage": "full",
                        "residual_untested_paths": [dry_review_risk],
                        "evidence_refs": [dry_evidence_ref],
                    }
                    for contract in dry_positive_contracts
                ],
                "outcome_strategy_review": {
                    "verdict": "sufficient",
                    "semantic_relation_assessment": (
                        "dry_run placeholder; no independent outcome-strategy review ran"
                    ),
                    "proves_intended_operation": True,
                    "problem_coverage": "partial",
                    "residual_untested_paths": [dry_review_risk],
                    "evidence_refs": [dry_evidence_ref],
                },
            }
            if not dry_positive_contracts:
                review.pop("selected_positive_outcome_contract_id", None)
                review.pop("selected_positive_outcome_contract_ids", None)
                review.pop("outcome_contract_reviews", None)
        try:
            review = bind_falsification_review(
                review,
                problem_id=pid,
                selected_option=selected_option,
                research=dossier,
            )
        except ValueError as exc:
            status = "error"
            warning = f"solution_falsifier_server_binding_error: {pid}: {exc}"
            warnings_list.append(warning)
            selection_outcomes.append(
                {
                    "problem_id": pid,
                    "selection_status": "invalid_output",
                    "selected_option_id": selected_option_id,
                    "reasons": [warning],
                }
            )
            continue

        review_errors = falsification_review_errors(
            review,
            expected_problem_id=pid,
            expected_option_id=str(selected_option_id),
            research_dossier=dossier,
            selected_option=selected_option,
        )
        if review_errors:
            status = "error"
            warnings_list.extend(review_errors)
            selection_outcomes.append(
                {
                    "problem_id": pid,
                    "selection_status": "invalid_output",
                    "selected_option_id": selected_option_id,
                    "reasons": review_errors,
                }
            )
            continue
        if review.get("verdict") != "accept":
            verdict = str(review.get("verdict"))
            selection_outcomes.append(
                {
                    "problem_id": pid,
                    "selection_status": verdict,
                    "selected_option_id": selected_option_id,
                    "reasons": [str(review.get("strongest_counterargument") or "")],
                }
            )
            continue
        selected_dec["falsification_review"] = review
        # Run selected-solution labeler (post-selection).
        evidence_atoms_preview: list[dict[str, Any]] = []
        for eid in evidence_ids_s[:12]:
            atom = atoms_by_id.get(eid)
            if atom is None:
                continue
            preview = {
                "atom_id": atom.get("atom_id"),
                "run_rel": atom.get("run_rel"),
                "source": atom.get("source"),
                "severity_hint": atom.get("severity_hint"),
                "text": atom.get("text"),
                "artifact_ref": atom.get("artifact_ref"),
            }
            evidence_atoms_preview.append(preview)

        selected_payload = {
            "problem_id": pid,
            "title": rec.get("title") or pid,
            "problem": rec.get("problem") or "",
            "user_impact": rec.get("user_impact") or "",
            "selected_option_id": selected_dec.get("selected_option_id"),
            "selected_family_id": selected_dec.get("selected_family_id"),
            "selection_rationale": selected_dec.get("selection_rationale"),
            "selected_option": selected_option or {},
        }
        labeler_prompt = labeler_template.replace(
            "{{SELECTED_SOLUTION_JSON}}",
            _json.dumps(selected_payload, ensure_ascii=False, indent=2),
        ).replace(
            "{{EVIDENCE_ATOMS_JSON}}",
            _json.dumps(evidence_atoms_preview, ensure_ascii=False, indent=2),
        )

        labeler_tag = f"selected_solution_labeler_{idx:03d}"
        labeler_out_dir = stage_artifacts_dir / labeler_tag
        labeler_out_dir.mkdir(parents=True, exist_ok=True)

        label_obj: dict[str, Any] | None = None
        if dry_run:
            (labeler_out_dir / f"{labeler_tag}.prompt.txt").write_text(
                labeler_prompt, encoding="utf-8"
            )
            (labeler_out_dir / f"{labeler_tag}.response.txt").write_text(
                "[dry-run] selected-solution labeler prompt not executed (offline mode).\n",
                encoding="utf-8",
            )
            # Dry-run labeling follows the inferred surface, never a family label.
            if selected_dec.get("needs_ux_review") is True:
                label_obj = {
                    "change_surface": {
                        "user_visible": True,
                        "kinds": ["new_command"],
                        "notes": "dry_run: synthesized label for the inferred user-visible surface",
                    },
                    "component": "unknown",
                    "intent_risk": "med",
                    "confidence": 0.5,
                    "evidence_atom_ids_used": evidence_ids_s[:1],
                }
            else:
                label_obj = {
                    "change_surface": {
                        "user_visible": True,
                        "kinds": ["docs_change"],
                        "notes": "dry_run: synthesized label for the inferred internal surface",
                    },
                    "component": "docs",
                    "intent_risk": "low",
                    "confidence": 0.5,
                    "evidence_atom_ids_used": evidence_ids_s[:1],
                }
        if isinstance(label_obj, dict):
            # Merge labeler output into the selection decision payload.
            if "change_surface" in label_obj:
                selected_dec["change_surface"] = label_obj.get("change_surface")
            if "component" in label_obj:
                selected_dec["component"] = label_obj.get("component")
            if "intent_risk" in label_obj:
                selected_dec["intent_risk"] = label_obj.get("intent_risk")
            if "confidence" in label_obj:
                selected_dec["labeler_confidence"] = label_obj.get("confidence")
            if "evidence_atom_ids_used" in label_obj:
                selected_dec["evidence_atom_ids_used"] = label_obj.get("evidence_atom_ids_used")

        # Enrich with problem narrative fields so downstream tools can treat this as a ticket-like payload.
        for key in (
            "title",
            "problem",
            "user_impact",
            "severity",
            "confidence",
            "evidence_atom_ids",
            "evidence_summary",
        ):
            if key in rec and key not in selected_dec:
                selected_dec[key] = rec.get(key)

        selected_dec["breadth"] = dict(problem_breadth)
        selected_dec["problem_breadth"] = dict(problem_breadth)
        selected_dec["breadth_profile"] = breadth_profile
        selected_dec["decision_basis"] = decision_basis
        selected_dec["batch_breadth"] = batch_breadth
        selected_dec["structurally_constant_batch_dimensions"] = batch_breadth.get(
            "structurally_constant_dimensions",
            [],
        )
        selected_dec["review_domain"] = _infer_review_domain(
            change_surface=selected_dec.get("change_surface")
            if isinstance(selected_dec.get("change_surface"), dict)
            else None,
            needs_ux_review=bool(selected_dec.get("needs_ux_review") is True),
        )

        complete_errors = selection_quality_errors(
            selected_dec,
            expected_problem_id=pid,
            options_by_id=options_by_id,
            research_dossier=dossier,
            require_complete=True,
        )
        if complete_errors:
            status = "error"
            warnings_list.extend(complete_errors)
            selection_outcomes.append(
                {
                    "problem_id": pid,
                    "selection_status": "invalid_output",
                    "selected_option_id": selected_option_id,
                    "reasons": complete_errors,
                }
            )
            continue
        selection_outcomes.append(
            {
                "problem_id": pid,
                "selection_status": "selected",
                "selected_option_id": selected_option_id,
                "falsification_verdict": "accept",
            }
        )

        # Stage gating: after selection but before planning, items are still research_required.
        if "stage" not in selected_dec:
            selected_dec["stage"] = "research_required"

        decisions.append(selected_dec)

    stage_doc = build_stage_document(
        stage,
        decisions,
        input_meta={
            "problem_record_count": len(problem_records),
            "research_dossier_count": len(research_dossiers),
            "option_count": len(solution_options),
            "decision_count": len(decisions),
            "orchestrator_head_revision": orchestrator_head_revision,
            "orchestrator_repo_root": str(repo_root.resolve()),
            "target_workspace_count": len(
                {str(path.resolve()) for path in (target_repo_roots_by_problem or {}).values()}
            )
            if target_repo_roots_by_problem is not None
            else 0,
            "repo_access": "read_only",
            "selection_outcomes": selection_outcomes,
            "role_healing_runs": role_healing_runs,
            "cross_role_feedback": cross_role_feedback,
            "option_revisions": option_revisions,
            "role_healing_summary": {
                "role_run_count": len(role_healing_runs),
                "selector_run_count": sum(
                    1 for run in role_healing_runs if run.get("role") == "selector"
                ),
                "falsifier_run_count": sum(
                    1 for run in role_healing_runs if run.get("role") == "falsifier"
                ),
                "optioner_revision_run_count": sum(
                    1 for run in role_healing_runs if run.get("role") == "optioner"
                ),
                "labeler_run_count": sum(
                    1 for run in role_healing_runs if run.get("role") == "labeler"
                ),
                "feedback_count": len(cross_role_feedback),
                "revised_case_count": len(option_revisions),
                "independently_accepted_selection_count": sum(
                    1
                    for outcome in selection_outcomes
                    if outcome.get("selection_status") == "selected"
                    and outcome.get("falsification_verdict") == "accept"
                ),
                "neutral_label_count": sum(
                    1
                    for outcome in selection_outcomes
                    if outcome.get("label_status") == "neutral_fallback"
                ),
                "repairable_or_stalled_case_count": sum(
                    1
                    for outcome in selection_outcomes
                    if str(outcome.get("selection_status") or "").startswith(
                        ("repairable_paused:", "stalled:")
                    )
                ),
                "total_role_elapsed_seconds": sum(
                    float(run.get("metrics", {}).get("total_elapsed_seconds") or 0.0)
                    for run in role_healing_runs
                ),
                "accepted_good": None,
                "accepted_bad": None,
                "false_rejected": None,
                "quality_label_status": "runtime_ground_truth_unavailable",
            },
            "dry_run": dry_run,
            "breadth_profile": breadth_profile,
            "batch_breadth": batch_breadth,
            "structurally_constant_batch_dimensions": batch_breadth.get(
                "structurally_constant_dimensions", []
            ),
            "solution_selection_status": status,
            "solution_selection_warnings": warnings_list,
            "labeler_prompt_template": str(pipeline_manifest.selected_solution_labeler_template)
            if pipeline_manifest.selected_solution_labeler_template is not None
            else None,
            "falsifier_prompt_template": str(falsifier_template_path),
        },
        artifacts={
            "solution_selection_json": str(out_json),
            "solution_selection_md": str(out_md),
        },
    )
    stage_doc = attach_stage_model_invocation_contract(
        stage_doc,
        agent=agent,
        dry_run=dry_run,
        manifest_refs=invocation_tracker.collect(),
        invocation_expected=live_prompt_expected_count > 0,
    )

    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_md.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(
        _json.dumps(stage_doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    title = out_json.stem.removesuffix(".solution_selection") or "Solution Selection"
    out_md.write_text(
        _render_solution_selection_markdown(
            decisions,
            problem_records_by_id=records_by_id,
            family_labels_by_id=family_labels_by_id,
            title=f"{title} – Solution Selection",
        ),
        encoding="utf-8",
    )

    print(f"[stage5] wrote {out_json}", file=sys.stderr)
    print(f"[stage5] wrote {out_md}", file=sys.stderr)
    return stage_doc


__all__ = [name for name in globals() if not name.startswith("__")]
