# ruff: noqa: E501,F401,F403,F405
from __future__ import annotations

from usertest_backlog.shared import *


def _render_prioritized_problems_markdown(
    priority_decisions: list[dict[str, Any]],
    *,
    problem_records_by_id: dict[str, dict[str, Any]],
    title: str = "Prioritized Problems",
) -> str:
    """Render stage-2 prioritization decisions as a human-readable Markdown document."""
    lines: list[str] = [f"# {title}\n"]
    if not priority_decisions:
        lines.append("_No prioritization decisions produced._\n")
        return "\n".join(lines)

    for dec in priority_decisions:
        pid = dec.get("problem_id") or "(no id)"
        rec = problem_records_by_id.get(pid) or {}
        rec_title = rec.get("title") or pid
        bucket = dec.get("priority_bucket") or "watch"
        selected = dec.get("selected_for_research")
        selected_str = (
            "true" if selected is True else "false" if selected is False else "?"
        )
        pre_score = dec.get("pre_score")
        pre_str = (
            f"{float(pre_score):.2f}" if isinstance(pre_score, (int, float)) else "?"
        )
        lines.append(f"## {rec_title}")
        lines.append(
            f"**ID**: `{pid}` | **Bucket**: {bucket} | "
            f"**Selected for research**: {selected_str} | **Pre-score**: {pre_str}\n"
        )
        rationale = dec.get("priority_rationale") or ""
        if rationale:
            lines.append(f"**Rationale**: {rationale}\n")
        used = dec.get("evidence_atom_ids_used") or []
        if isinstance(used, list) and used:
            used_list = [e for e in used if isinstance(e, str) and e.strip()]
            if used_list:
                lines.append(
                    f"**Evidence atoms used** ({len(used_list)}): "
                    + ", ".join(f"`{e}`" for e in used_list[:10])
                    + (" …" if len(used_list) > 10 else "")
                    + "\n"
                )
        warn = dec.get("_parse_warning")
        if warn:
            lines.append(f"> ⚠ parse warning: {warn}\n")
        lines.append("")

    return "\n".join(lines)


def _run_problem_prioritization_stage(
    *,
    atoms: list[dict[str, Any]],
    problem_records: list[dict[str, Any]],
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
    """Run stage 2 prioritization and write the stage artifacts."""
    import json as _json

    stage = "problem_prioritization"
    stage_artifacts_dir = artifacts_dir / "problem_prioritization"
    stage_artifacts_dir.mkdir(parents=True, exist_ok=True)

    relation_config_raw = yaml.safe_load(
        pipeline_manifest.relation_review_config_path.read_text(encoding="utf-8")
    )
    relation_config = relation_config_raw if isinstance(relation_config_raw, dict) else {}

    neighborhoods = rank_stage_related_items(
        problem_records,
        stage=stage,
        relation_config=relation_config,
        embedder=None,
    )
    priority_signals = compute_problem_priority_signals(problem_records, atoms)
    signals_by_problem_id: dict[str, dict[str, Any]] = {
        str(item.get("problem_id")): item
        for item in priority_signals
        if isinstance(item, dict) and isinstance(item.get("problem_id"), str)
    }

    problem_records_json = _json.dumps(problem_records, ensure_ascii=False, indent=2)
    signals_json = _json.dumps(priority_signals, ensure_ascii=False, indent=2)
    neighborhoods_json = _json.dumps(neighborhoods, ensure_ascii=False, indent=2)

    template_text = pipeline_manifest.template_text(pipeline_manifest.prioritizer_template)
    prompt = (
        template_text.replace("{{STAGE_GUIDANCE}}", stage_guidance_text)
        .replace("{{PROBLEM_RECORDS_JSON}}", problem_records_json)
        .replace("{{PRIORITY_SIGNALS_JSON}}", signals_json)
        .replace("{{NEIGHBORHOODS_JSON}}", neighborhoods_json)
    )

    tag = "problem_prioritization_001"
    run_out_dir = stage_artifacts_dir / tag
    run_out_dir.mkdir(parents=True, exist_ok=True)

    decisions: list[dict[str, Any]] = []
    warnings_list: list[str] = []
    status: str = "ok"
    error: str | None = None

    if dry_run:
        status = "dry_run_heuristic"
        (run_out_dir / f"{tag}.prompt.txt").write_text(prompt, encoding="utf-8")
        (run_out_dir / f"{tag}.response.txt").write_text(
            "[dry-run] stage-2 prioritizer prompt not executed (offline mode).\n",
            encoding="utf-8",
        )
        for rec in problem_records:
            pid = rec.get("problem_id")
            if not isinstance(pid, str) or not pid.strip():
                continue
            signals = signals_by_problem_id.get(pid, {})
            bucket = signals.get("bucket_candidate") if isinstance(signals, dict) else None
            bucket_s = bucket if isinstance(bucket, str) else "watch"
            selected = bucket_s in {"p0", "p1"}
            pre_score = signals.get("pre_score") if isinstance(signals, dict) else None
            score_breakdown = (
                signals.get("score_breakdown") if isinstance(signals, dict) else None
            )
            cited = rec.get("evidence_atom_ids") if isinstance(rec.get("evidence_atom_ids"), list) else []
            cited_ids = [e for e in cited if isinstance(e, str) and e.strip()]
            decisions.append(
                {
                    "problem_id": pid,
                    "priority_bucket": bucket_s,
                    "selected_for_research": selected,
                    "priority_rationale": (
                        "Dry-run heuristic (offline): selected bucket from deterministic pre-score "
                        f"(pre_score={pre_score!r})."
                    ),
                    "evidence_atom_ids_used": cited_ids,
                    "priority_status": "prioritized",
                    "pre_score": pre_score,
                    "bucket_candidate": bucket_s,
                    "score_breakdown": score_breakdown,
                    "_dry_run_synthesized": True,
                }
            )
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
            parsed, parse_warnings = parse_priority_decision_list(response)
            decisions = parsed
            warnings_list.extend(parse_warnings)
        except Exception as exc:  # noqa: BLE001
            status = "error"
            error = str(exc)
            warnings_list.append(f"prioritizer_error: {exc}")

    # Enrich with deterministic signals so the artifact always shows the pre-score breakdown.
    for dec in decisions:
        pid = dec.get("problem_id")
        if not isinstance(pid, str):
            continue
        signals = signals_by_problem_id.get(pid)
        if isinstance(signals, dict):
            if "pre_score" not in dec:
                dec["pre_score"] = signals.get("pre_score")
            if "bucket_candidate" not in dec:
                dec["bucket_candidate"] = signals.get("bucket_candidate")
            if "score_breakdown" not in dec:
                dec["score_breakdown"] = signals.get("score_breakdown")

    # Guardrail: stage 2 must not contain solution fields.
    forbidden_solution_fields = {
        "proposed_fix",
        "selected_solution",
        "family_id",
        "option_id",
        "implementation_steps",
    }
    for dec in decisions:
        pid = dec.get("problem_id") or "(no problem_id)"
        bad = [k for k in forbidden_solution_fields if k in dec]
        if bad:
            warnings_list.append(
                f"priority_decision_forbidden_solution_fields: {pid}: {', '.join(sorted(bad))}"
            )
            existing = dec.get("_parse_warning")
            msg = "forbidden fields present: " + ", ".join(sorted(bad))
            if isinstance(existing, str) and existing.strip():
                dec["_parse_warning"] = existing.strip() + "; " + msg
            else:
                dec["_parse_warning"] = msg

    expected_ids = [
        item.get("problem_id")
        for item in problem_records
        if isinstance(item, dict) and isinstance(item.get("problem_id"), str)
    ]
    expected_set = {pid for pid in expected_ids if isinstance(pid, str)}
    found_set = {
        pid for pid in (item.get("problem_id") for item in decisions) if isinstance(pid, str)
    }
    missing = sorted(expected_set - found_set)
    if missing:
        status = "error"
        warnings_list.append(
            "prioritizer_missing_problem_ids: missing decisions for: " + ", ".join(missing)
        )

    stage_doc = build_stage_document(
        stage,
        decisions,
        input_meta={
            "atom_count": len(atoms),
            "problem_record_count": len(problem_records),
            "dry_run": dry_run,
            "prioritizer_status": status,
            "prioritizer_error": error,
            "prioritizer_warnings": warnings_list,
            "neighborhood_count": len(neighborhoods),
        },
        artifacts={
            "prioritized_problems_json": str(out_json),
            "prioritized_problems_md": str(out_md),
            "prioritizer_prompt": str(run_out_dir / f"{tag}.prompt.txt"),
            "prioritizer_response": str(run_out_dir / f"{tag}.response.txt"),
        },
    )

    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(
        _json.dumps(stage_doc, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    records_by_id: dict[str, dict[str, Any]] = {
        str(item.get("problem_id")): item
        for item in problem_records
        if isinstance(item, dict) and isinstance(item.get("problem_id"), str)
    }
    title = out_json.stem.removesuffix(".prioritized_problems") or "Prioritized Problems"
    out_md.write_text(
        _render_prioritized_problems_markdown(
            decisions,
            problem_records_by_id=records_by_id,
            title=f"{title} – Prioritized Problems",
        ),
        encoding="utf-8",
    )

    print(f"[stage2] wrote {out_json}", file=sys.stderr)
    print(f"[stage2] wrote {out_md}", file=sys.stderr)
    return stage_doc




__all__ = [name for name in globals() if not name.startswith("__")]
