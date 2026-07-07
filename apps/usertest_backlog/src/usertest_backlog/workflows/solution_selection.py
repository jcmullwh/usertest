# ruff: noqa: E501,F401,F403,F405
from __future__ import annotations

from usertest_backlog.shared import *


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
        sel_fid = dec.get("selected_family_id") or "(no selected_family_id)"
        label = family_labels_by_id.get(str(sel_fid), str(sel_fid))
        needs_ux = dec.get("needs_ux_review")
        needs_ux_s = "true" if needs_ux is True else "false" if needs_ux is False else "?"

        lines.append(f"## {rec_title}")
        lines.append(
            f"**Problem ID**: `{pid}` | **Selected**: `{sel_oid}` | "
            f"**Family**: {label} (`{sel_fid}`) | **Needs UX review**: {needs_ux_s}\n"
        )

        rationale = dec.get("selection_rationale") or ""
        if rationale:
            lines.append(f"**Rationale**: {rationale}\n")
        align = dec.get("repo_intent_alignment") or ""
        if align:
            lines.append(f"**Repo intent alignment**: {align}\n")
        other = dec.get("why_other_options_were_not_selected") or ""
        if other:
            lines.append(f"**Why not other options**: {other}\n")

        change_surface = dec.get("change_surface")
        cs = change_surface if isinstance(change_surface, dict) else {}
        kinds = cs.get("kinds") or []
        kinds_list = (
            [k for k in kinds if isinstance(k, str) and k.strip()] if isinstance(kinds, list) else []
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
    atoms: list[dict[str, Any]],
    problem_records: list[dict[str, Any]],
    research_dossiers: list[dict[str, Any]],
    solution_options: list[dict[str, Any]],
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
    """Run stage 5 solution selection + selected-solution labeler and write artifacts."""
    import json as _json

    stage = "solution_selection"
    stage_artifacts_dir = artifacts_dir / "solution_selection"
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
    options_by_problem: dict[str, list[dict[str, Any]]] = {}
    options_by_id: dict[str, dict[str, Any]] = {}
    for opt in solution_options:
        pid = opt.get("problem_id")
        oid = opt.get("option_id")
        if isinstance(pid, str) and pid.strip() and isinstance(oid, str) and oid.strip():
            options_by_problem.setdefault(pid.strip(), []).append(opt)
            options_by_id[oid.strip()] = opt

    focus_ids = sorted(options_by_problem.keys())

    selector_template = pipeline_manifest.template_text(pipeline_manifest.solution_selector_template)
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
    warnings_list: list[str] = []
    status: str = "ok"

    for idx, pid in enumerate(focus_ids, start=1):
        rec = records_by_id.get(pid) or {}
        dossier = dossiers_by_id.get(pid) or {}
        opts = options_by_problem.get(pid) or []
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
            .replace("{{RESEARCH_DOSSIER_JSON}}", _json.dumps(dossier, ensure_ascii=False, indent=2))
            .replace("{{SOLUTION_OPTIONS_JSON}}", _json.dumps(opts, ensure_ascii=False, indent=2))
        )

        tag = f"solution_selection_{idx:03d}"
        run_out_dir = stage_artifacts_dir / tag
        run_out_dir.mkdir(parents=True, exist_ok=True)

        selected_dec: dict[str, Any] | None = None
        if dry_run:
            status = "dry_run_synthesized"
            (run_out_dir / f"{tag}.prompt.txt").write_text(prompt, encoding="utf-8")
            (run_out_dir / f"{tag}.response.txt").write_text(
                "[dry-run] stage-5 solution selector prompt not executed (offline mode).\n",
                encoding="utf-8",
            )
            # Deterministic selection: first problem selects the most comprehensive family
            # (drives a high-surface UX-review test), others select most direct.
            preferred_family = (
                "most_comprehensive" if idx == 1 else "most_direct"
            )
            chosen = next((o for o in opts if o.get("family_id") == preferred_family), None)
            if chosen is None:
                chosen = opts[0] if opts else None
            if chosen is None:
                status = "error"
                warnings_list.append(f"solution_selection_no_options: {pid}")
                continue
            fid = chosen.get("family_id") or "unknown"
            oid = chosen.get("option_id") or "(no option_id)"
            needs_ux = bool(fid == "most_comprehensive" or "new_command" in str(chosen.get("change_surface_hypothesis") or ""))
            candidate = {
                "problem_id": pid,
                "selected_option_id": oid,
                "selected_family_id": fid,
                "selection_rationale": "dry_run: synthesized selection; rerun without --dry-run for real rationale",
                "repo_intent_alignment": "dry_run: synthesized",
                "why_other_options_were_not_selected": "dry_run: synthesized",
                "needs_ux_review": needs_ux,
                "selection_status": "selected",
            }
            parsed, parse_warnings = parse_selection_decisions(
                _json.dumps([candidate], ensure_ascii=False)
            )
            selected_dec = parsed[0] if parsed else None
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
                parsed, parse_warnings = parse_selection_decisions(response)
                warnings_list.extend(parse_warnings)
                # Expect exactly one decision for this problem.
                selected_dec = next(
                    (d for d in parsed if d.get("problem_id") == pid),
                    (parsed[0] if parsed else None),
                )
            except Exception as exc:  # noqa: BLE001
                status = "error"
                warnings_list.append(f"solution_selector_error: {pid}: {exc}")
                continue

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
        labeler_prompt = (
            labeler_template.replace(
                "{{SELECTED_SOLUTION_JSON}}",
                _json.dumps(selected_payload, ensure_ascii=False, indent=2),
            )
            .replace(
                "{{EVIDENCE_ATOMS_JSON}}",
                _json.dumps(evidence_atoms_preview, ensure_ascii=False, indent=2),
            )
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
            # Deterministic label: comprehensive → new_command; otherwise docs_change.
            fid = str(selected_dec.get("selected_family_id") or "")
            if fid == "most_comprehensive":
                label_obj = {
                    "change_surface": {
                        "user_visible": True,
                        "kinds": ["new_command"],
                        "notes": "dry_run: synthesized label for comprehensive option",
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
                        "notes": "dry_run: synthesized label for non-comprehensive option",
                    },
                    "component": "docs",
                    "intent_risk": "low",
                    "confidence": 0.5,
                    "evidence_atom_ids_used": evidence_ids_s[:1],
                }
        else:
            try:
                response = run_stage_prompt_json(
                    stage="selected_solution_labeler",
                    prompt=labeler_prompt,
                    out_dir=labeler_out_dir,
                    tag=labeler_tag,
                    agent=agent,
                    model=model,
                    cfg=cfg,
                )
                raw_label = json.loads(response)
                if isinstance(raw_label, dict):
                    label_obj = raw_label
                else:
                    raise ValueError("selected_solution_labeler_response_not_a_dict")
            except Exception as exc:  # noqa: BLE001
                status = "error"
                warnings_list.append(f"selected_solution_labeler_error: {pid}: {exc}")
                label_obj = None

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
        },
        artifacts={
            "solution_selection_json": str(out_json),
            "solution_selection_md": str(out_md),
        },
    )

    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_md.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(_json.dumps(stage_doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

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
