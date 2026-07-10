# ruff: noqa: E501,F401,F403,F405
from __future__ import annotations

from usertest_backlog.shared import *


def _enforce_full_drain_research_policy(decisions: list[dict[str, Any]]) -> None:
    """Make urgency a research-order decision, never a permanent case filter."""

    for decision in decisions:
        if isinstance(decision.get("problem_id"), str) and not decision.get(
            "_parse_warning"
        ):
            # Eligibility is runner-owned. ``priority_status`` is model output and
            # therefore cannot be allowed to turn a real canonical case into a
            # permanent watch/defer bucket.
            decision["selected_for_research"] = True
            decision["priority_status"] = "prioritized"


def _server_normalize_priority_decisions(
    *,
    decisions: list[dict[str, Any]],
    problem_records: list[dict[str, Any]],
    signals_by_problem_id: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Return one research-eligible decision for every canonical problem.

    The model still supplies urgency and rationale when its response is valid. Missing,
    duplicate, malformed, or explicitly blocked responses fall back to deterministic
    runner signals instead of silently removing the case from stage 3.
    """

    valid_buckets = {"p0", "p1", "p2", "p3", "watch"}
    expected_records = {
        str(record["problem_id"]): record
        for record in problem_records
        if isinstance(record, dict)
        and isinstance(record.get("problem_id"), str)
        and str(record["problem_id"]).strip()
    }
    candidates: dict[str, list[dict[str, Any]]] = {}
    warnings: list[str] = []
    for decision in decisions:
        problem_id = _coerce_string(decision.get("problem_id"))
        if problem_id is None or problem_id not in expected_records:
            warnings.append(
                "prioritizer_unknown_problem_id:" + (problem_id or "(missing)")
            )
            continue
        candidates.setdefault(problem_id, []).append(dict(decision))

    normalized: list[dict[str, Any]] = []
    for problem_id, record in expected_records.items():
        model_candidates = candidates.get(problem_id, [])
        use_model = (
            len(model_candidates) == 1
            and _coerce_string(model_candidates[0].get("_parse_warning")) is None
        )
        if len(model_candidates) > 1:
            warnings.append(f"prioritizer_duplicate_problem_id:{problem_id}")
        elif not model_candidates:
            warnings.append(f"prioritizer_missing_problem_id:{problem_id}")
        elif not use_model:
            warnings.append(f"prioritizer_invalid_problem_decision:{problem_id}")

        candidate = dict(model_candidates[0]) if use_model else {}
        signals = signals_by_problem_id.get(problem_id, {})
        bucket = _coerce_string(candidate.get("priority_bucket"))
        if bucket not in valid_buckets:
            bucket = _coerce_string(signals.get("bucket_candidate"))
        if bucket not in valid_buckets:
            bucket = "watch"
        rationale = _coerce_string(candidate.get("priority_rationale"))
        if rationale is None:
            rationale = (
                "Runner fallback retained this canonical case for causal research; "
                "deterministic priority signals control ordering only."
            )
        evidence_ids = candidate.get("evidence_atom_ids_used")
        record_evidence = {
            value
            for value in (
                record.get("evidence_atom_ids")
                if isinstance(record.get("evidence_atom_ids"), list)
                else []
            )
            if isinstance(value, str) and value.strip()
        }
        cited = [
            value
            for value in (evidence_ids if isinstance(evidence_ids, list) else [])
            if isinstance(value, str) and value in record_evidence
        ]
        if not cited:
            cited = sorted(record_evidence)
        candidate.pop("_parse_warning", None)
        candidate.update(
            {
                "problem_id": problem_id,
                "priority_bucket": bucket,
                "selected_for_research": True,
                "priority_rationale": rationale,
                "evidence_atom_ids_used": cited,
                "priority_status": "prioritized",
                "selection_authority": "runner_full_drain_v1",
                "model_priority_accepted": use_model,
            }
        )
        normalized.append(candidate)
    return normalized, warnings


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
        selected_str = "true" if selected is True else "false" if selected is False else "?"
        pre_score = dec.get("pre_score")
        pre_str = f"{float(pre_score):.2f}" if isinstance(pre_score, (int, float)) else "?"
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
            # Every canonical stage-1 problem is real enough to merit causal research.
            # Priority controls ordering, never permanent eligibility; otherwise a
            # single-run p2/p3/watch case can remain unresearched forever.
            selected = True
            pre_score = signals.get("pre_score") if isinstance(signals, dict) else None
            score_breakdown = signals.get("score_breakdown") if isinstance(signals, dict) else None
            cited = (
                rec.get("evidence_atom_ids")
                if isinstance(rec.get("evidence_atom_ids"), list)
                else []
            )
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

    decisions, normalization_warnings = _server_normalize_priority_decisions(
        decisions=decisions,
        problem_records=problem_records,
        signals_by_problem_id=signals_by_problem_id,
    )
    warnings_list.extend(normalization_warnings)

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

    # Stage 1 has already excluded noise, proposals, and duplicates. Once a canonical
    # problem reaches prioritization it must enter research; the bucket determines
    # urgency/order only. Keep this runner-owned so model conservatism cannot silently
    # strand lower-frequency but legitimate problems.
    _enforce_full_drain_research_policy(decisions)

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
