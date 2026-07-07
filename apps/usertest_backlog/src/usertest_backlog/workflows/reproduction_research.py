# ruff: noqa: E501,F401,F403,F405
from __future__ import annotations

from usertest_backlog.shared import *


def _render_research_dossiers_markdown(
    research_dossiers: list[dict[str, Any]],
    *,
    problem_records_by_id: dict[str, dict[str, Any]],
    title: str = "Research Dossiers",
) -> str:
    """Render stage-3 research dossiers as a human-readable Markdown document."""
    lines: list[str] = [f"# {title}\n"]
    if not research_dossiers:
        lines.append("_No research dossiers produced._\n")
        return "\n".join(lines)

    for dossier in research_dossiers:
        pid = dossier.get("problem_id") or "(no id)"
        rec = problem_records_by_id.get(str(pid)) or {}
        rec_title = rec.get("title") or pid
        status = dossier.get("reproduction_status") or "unknown"
        diff_cls = dossier.get("diff_classification") or "unknown"
        impl = dossier.get("implementation_performed")
        impl_s = "true" if impl is True else "false" if impl is False else "?"

        lines.append(f"## {rec_title}")
        lines.append(
            f"**ID**: `{pid}` | **Reproduction**: `{status}` | "
            f"**Diff**: `{diff_cls}` | **Implementation performed**: {impl_s}\n"
        )

        writes_used = dossier.get("writes_used")
        writes_used_s = (
            "true" if writes_used is True else "false" if writes_used is False else "?"
        )
        writes_purpose = dossier.get("writes_purpose") or []
        purpose_list = (
            [p for p in writes_purpose if isinstance(p, str) and p.strip()]
            if isinstance(writes_purpose, list)
            else []
        )
        purpose_s = ", ".join(f"`{p}`" for p in purpose_list) if purpose_list else "`(none)`"
        lines.append(f"- Writes used: `{writes_used_s}`; purpose: {purpose_s}")

        broader = dossier.get("broader_class_assessment")
        if isinstance(broader, str) and broader.strip():
            lines.append(f"- Broader class assessment: `{broader.strip()}`")

        diff_reasons = dossier.get("diff_suspicious_reasons") or []
        diff_reasons_list = (
            [r for r in diff_reasons if isinstance(r, str) and r.strip()]
            if isinstance(diff_reasons, list)
            else []
        )
        if diff_reasons_list:
            lines.append("- Diff notes:")
            for r in diff_reasons_list[:12]:
                lines.append(f"  - {r}")

        hypos = dossier.get("root_cause_hypotheses") or []
        hypos_list = (
            [h for h in hypos if isinstance(h, str) and h.strip()]
            if isinstance(hypos, list)
            else []
        )
        if hypos_list:
            lines.append("- Root cause hypotheses:")
            for h in hypos_list[:8]:
                lines.append(f"  - {h}")

        unknowns = dossier.get("unknowns") or []
        unknowns_list = (
            [u for u in unknowns if isinstance(u, str) and u.strip()]
            if isinstance(unknowns, list)
            else []
        )
        if unknowns_list:
            lines.append("- Unknowns / next evidence needed:")
            for u in unknowns_list[:10]:
                lines.append(f"  - {u}")

        run_dir = dossier.get("run_dir")
        if isinstance(run_dir, str) and run_dir.strip():
            lines.append(f"- Run dir: `{run_dir.strip()}`")

        artifacts = dossier.get("artifacts")
        artifacts_dict = artifacts if isinstance(artifacts, dict) else {}
        report_json = artifacts_dict.get("report_json")
        patch_diff = artifacts_dict.get("patch_diff")
        if isinstance(report_json, str) and report_json.strip():
            lines.append(f"- report.json: `{report_json.strip()}`")
        if isinstance(patch_diff, str) and patch_diff.strip():
            lines.append(f"- patch.diff: `{patch_diff.strip()}`")

        warn = dossier.get("_parse_warning")
        if isinstance(warn, str) and warn.strip():
            lines.append(f"> ⚠ parse warning: {warn.strip()}")

        lines.append("")

    return "\n".join(lines)


def _run_repro_research_stage(
    *,
    repo_root: Path,
    repo_input: str | None,
    target_slug: str | None,
    selected_priority_decisions: list[dict[str, Any]],
    problem_records: list[dict[str, Any]],
    artifacts_dir: Path,
    out_json: Path,
    out_md: Path,
    agent: str,
    model: str | None,
    cfg: RunnerConfig,
    dry_run: bool,
) -> dict[str, Any]:
    """Run stage 3 reproduce-plus-research and write the stage artifacts."""
    import json as _json

    records_by_id: dict[str, dict[str, Any]] = {
        str(item.get("problem_id")): item
        for item in problem_records
        if isinstance(item, dict) and isinstance(item.get("problem_id"), str)
    }

    selected_payloads: list[dict[str, Any]] = []
    for dec in selected_priority_decisions:
        pid = dec.get("problem_id")
        if not isinstance(pid, str) or not pid.strip():
            continue
        rec = records_by_id.get(pid) or {}
        payload = {
            "problem_id": pid,
            "problem_record": rec,
            "priority_decision": dec,
        }
        selected_payloads.append(payload)

    stage_doc = run_repro_research_stage(
        repo_root=repo_root,
        repo_input=repo_input,
        target_slug=target_slug,
        selected_problems=selected_payloads,
        artifacts_dir=artifacts_dir,
        agent=agent,
        model=model,
        cfg=cfg,
        dry_run=dry_run,
    )

    artifacts = stage_doc.get("artifacts")
    artifacts_dict = artifacts if isinstance(artifacts, dict) else {}
    artifacts_dict["research_json"] = str(out_json)
    artifacts_dict["research_md"] = str(out_md)
    stage_doc["artifacts"] = artifacts_dict

    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_md.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(_json.dumps(stage_doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    items_raw = stage_doc.get("items") if isinstance(stage_doc, dict) else None
    dossiers = (
        [item for item in items_raw if isinstance(item, dict)]
        if isinstance(items_raw, list)
        else []
    )
    title = out_json.stem.removesuffix(".research") or "Research"
    out_md.write_text(
        _render_research_dossiers_markdown(
            dossiers,
            problem_records_by_id=records_by_id,
            title=f"{title} – Research Dossiers",
        ),
        encoding="utf-8",
    )

    print(f"[stage3] wrote {out_json}", file=sys.stderr)
    print(f"[stage3] wrote {out_md}", file=sys.stderr)
    return stage_doc




__all__ = [name for name in globals() if not name.startswith("__")]
