from __future__ import annotations

import json
from typing import Any


def _append_json_section(lines: list[str], heading: str, payload: dict[str, Any]) -> None:
    lines.append(heading)
    lines.append("")
    lines.append("```json")
    lines.append(json.dumps(payload, indent=2, ensure_ascii=False))
    lines.append("```")
    lines.append("")


def _render_persona_exploration_report(
    *,
    report: dict[str, Any],
    metrics: dict[str, Any] | None,
    target_ref: dict[str, Any] | None,
) -> str:
    persona = report.get("persona") if isinstance(report.get("persona"), dict) else {}
    persona_name = persona.get("name") if isinstance(persona.get("name"), str) else None
    persona_desc = (
        persona.get("description") if isinstance(persona.get("description"), str) else None
    )
    mission = report.get("mission") if isinstance(report.get("mission"), str) else None

    adoption = (
        report.get("adoption_decision")
        if isinstance(report.get("adoption_decision"), dict)
        else {}
    )
    recommendation = (
        adoption.get("recommendation")
        if isinstance(adoption.get("recommendation"), str)
        else None
    )

    lines: list[str] = []
    lines.append("# Persona exploration report")
    lines.append("")

    if target_ref is not None:
        _append_json_section(lines, "## Target", target_ref)

    lines.append("## Summary")
    lines.append("")
    if persona_name:
        lines.append(f"- Persona: {persona_name}")
    if persona_desc:
        lines.append(f"- Persona description: {persona_desc}")
    if mission:
        lines.append(f"- Mission: {mission}")
    if recommendation:
        lines.append(f"- Recommendation: {recommendation}")
    lines.append("")

    minimal = (
        report.get("minimal_mental_model")
        if isinstance(report.get("minimal_mental_model"), dict)
        else {}
    )
    minimal_summary = (
        minimal.get("summary") if isinstance(minimal.get("summary"), str) else None
    )
    entry_points = minimal.get("entry_points")
    entry_points_list = (
        [x for x in entry_points if isinstance(x, str) and x.strip()]
        if isinstance(entry_points, list)
        else []
    )

    lines.append("## Minimal mental model")
    lines.append("")
    if minimal_summary:
        lines.append(minimal_summary.strip())
        lines.append("")
    if entry_points_list:
        lines.append("### Entry points")
        lines.append("")
        for entry in entry_points_list:
            lines.append(f"- {entry.strip()}")
        lines.append("")

    confidence = (
        report.get("confidence_signals")
        if isinstance(report.get("confidence_signals"), dict)
        else {}
    )
    found = confidence.get("found")
    missing = confidence.get("missing")
    found_list = (
        [x for x in found if isinstance(x, str) and x.strip()] if isinstance(found, list) else []
    )
    missing_list = (
        [x for x in missing if isinstance(x, str) and x.strip()]
        if isinstance(missing, list)
        else []
    )

    lines.append("## Confidence signals")
    lines.append("")
    lines.append("### Found")
    lines.append("")
    if found_list:
        for item in found_list:
            lines.append(f"- {item.strip()}")
    else:
        lines.append("_None reported._")
    lines.append("")

    lines.append("### Missing")
    lines.append("")
    if missing_list:
        for item in missing_list:
            lines.append(f"- {item.strip()}")
    else:
        lines.append("_None reported._")
    lines.append("")

    confusion = report.get("confusion_points")
    confusion_list = (
        [x for x in confusion if isinstance(x, str) and x.strip()]
        if isinstance(confusion, list)
        else []
    )
    lines.append("## Confusion points")
    lines.append("")
    if confusion_list:
        for item in confusion_list:
            lines.append(f"- {item.strip()}")
    else:
        lines.append("_None reported._")
    lines.append("")

    suggested = report.get("suggested_changes")
    suggested_list = (
        [x for x in suggested if isinstance(x, str) and x.strip()]
        if isinstance(suggested, list)
        else []
    )
    lines.append("## Suggested changes")
    lines.append("")
    if suggested_list:
        for item in suggested_list:
            lines.append(f"- {item.strip()}")
    else:
        lines.append("_None suggested._")
    lines.append("")

    if metrics is not None:
        _append_json_section(lines, "## Metrics", metrics)

    return "\n".join(lines).rstrip() + "\n"


def _render_task_run_report(
    *,
    report: dict[str, Any],
    metrics: dict[str, Any] | None,
    target_ref: dict[str, Any] | None,
) -> str:
    status = report.get("status") if isinstance(report.get("status"), str) else None
    confidence_raw = report.get("confidence")
    confidence = confidence_raw if isinstance(confidence_raw, (int, float)) else None
    goal = report.get("goal") if isinstance(report.get("goal"), str) else None
    summary = report.get("summary") if isinstance(report.get("summary"), str) else None

    lines: list[str] = []
    lines.append("# Task run report")
    lines.append("")

    if target_ref is not None:
        _append_json_section(lines, "## Target", target_ref)

    lines.append("## Status")
    lines.append("")
    if status:
        lines.append(f"- Status: {status}")
    if confidence is not None:
        lines.append(f"- Confidence: {confidence}")
    lines.append("")

    if goal:
        lines.append("## Goal")
        lines.append("")
        lines.append(goal.strip())
        lines.append("")

    if summary:
        lines.append("## Summary")
        lines.append("")
        lines.append(summary.strip())
        lines.append("")

    steps = report.get("steps")
    steps_list = steps if isinstance(steps, list) else []
    if steps_list:
        lines.append("## Steps")
        lines.append("")
        for step in steps_list:
            if not isinstance(step, dict):
                continue
            name = step.get("name") if isinstance(step.get("name"), str) else ""
            outcome = step.get("outcome") if isinstance(step.get("outcome"), str) else ""
            attempts = step.get("attempts")
            attempts_list = attempts if isinstance(attempts, list) else []

            lines.append(f"### {name}" if name else "### Step")
            lines.append("")
            if outcome:
                lines.append(f"- Outcome: {outcome.strip()}")
            if attempts_list:
                lines.append("- Attempts:")
                for attempt in attempts_list:
                    if not isinstance(attempt, dict):
                        continue
                    action = attempt.get("action") if isinstance(attempt.get("action"), str) else ""
                    result = attempt.get("result") if isinstance(attempt.get("result"), str) else ""
                    evidence = attempt.get("evidence") if isinstance(attempt.get("evidence"), str) else ""
                    if action:
                        lines.append(f"  - Action: {action.strip()}")
                    else:
                        lines.append("  - Action: (missing)")
                    if result:
                        lines.append(f"    Result: {result.strip()}")
                    if evidence:
                        lines.append(f"    Evidence: {evidence.strip()}")
            lines.append("")

    outputs = report.get("outputs")
    outputs_list = outputs if isinstance(outputs, list) else []
    lines.append("## Outputs")
    lines.append("")
    if outputs_list:
        for item in outputs_list:
            if not isinstance(item, dict):
                continue
            label = item.get("label") if isinstance(item.get("label"), str) else ""
            path = item.get("path") if isinstance(item.get("path"), str) else None
            desc = item.get("description") if isinstance(item.get("description"), str) else ""
            bits = [label.strip() or "output"]
            if path:
                bits.append(f"({path.strip()})")
            if desc:
                bits.append(f"- {desc.strip()}")
            lines.append(f"- {' '.join(bits)}")
    else:
        lines.append("_None._")
    lines.append("")

    issues = report.get("issues")
    issues_list = issues if isinstance(issues, list) else []
    if issues_list:
        lines.append("## Issues")
        lines.append("")
        for issue in issues_list:
            if not isinstance(issue, dict):
                continue
            severity = issue.get("severity") if isinstance(issue.get("severity"), str) else ""
            title = issue.get("title") if isinstance(issue.get("title"), str) else ""
            details = issue.get("details") if isinstance(issue.get("details"), str) else ""
            header = f"- [{severity}] {title}".strip() if severity or title else "- Issue"
            lines.append(header)
            if details:
                lines.append(f"  {details.strip()}")
            evidence = issue.get("evidence") if isinstance(issue.get("evidence"), str) else ""
            if evidence:
                lines.append(f"  Evidence: {evidence.strip()}")
            suggested_fix = (
                issue.get("suggested_fix") if isinstance(issue.get("suggested_fix"), str) else ""
            )
            if suggested_fix:
                lines.append(f"  Suggested fix: {suggested_fix.strip()}")
        lines.append("")

    next_actions = report.get("next_actions")
    next_actions_list = (
        [x for x in next_actions if isinstance(x, str) and x.strip()]
        if isinstance(next_actions, list)
        else []
    )
    lines.append("## Next actions")
    lines.append("")
    if next_actions_list:
        for action in next_actions_list:
            lines.append(f"- {action.strip()}")
    else:
        lines.append("_None._")
    lines.append("")

    if metrics is not None:
        _append_json_section(lines, "## Metrics", metrics)

    return "\n".join(lines).rstrip() + "\n"


def render_report_markdown(
    *,
    report: dict[str, Any],
    metrics: dict[str, Any] | None = None,
    target_ref: dict[str, Any] | None = None,
) -> str:
    if (
        isinstance(report.get("persona"), dict)
        and isinstance(report.get("adoption_decision"), dict)
        and isinstance(report.get("mission"), str)
    ):
        return _render_persona_exploration_report(
            report=report, metrics=metrics, target_ref=target_ref
        )

    if report.get("kind") == "task_run_v1":
        return _render_task_run_report(report=report, metrics=metrics, target_ref=target_ref)

    lines: list[str] = []
    lines.append("# Report")
    lines.append("")
    if target_ref is not None:
        _append_json_section(lines, "## Target", target_ref)
    if report:
        _append_json_section(lines, "## Raw report.json", report)
    if metrics is not None:
        _append_json_section(lines, "## Metrics", metrics)
    return "\n".join(lines).rstrip() + "\n"
