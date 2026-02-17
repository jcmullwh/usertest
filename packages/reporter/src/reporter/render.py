from __future__ import annotations

import json
from typing import Any


def render_report_markdown(
    *,
    report: dict[str, Any],
    metrics: dict[str, Any] | None = None,
    target_ref: dict[str, Any] | None = None,
) -> str:
    lines: list[str] = []
    lines.append("# Report")
    lines.append("")

    if target_ref is not None:
        lines.append("## Target")
        lines.append("")
        lines.append("```json")
        lines.append(json.dumps(target_ref, indent=2, ensure_ascii=False))
        lines.append("```")
        lines.append("")

    if report:
        lines.append("## Report")
        lines.append("")
        lines.append("```json")
        lines.append(json.dumps(report, indent=2, ensure_ascii=False))
        lines.append("```")
        lines.append("")

    if metrics is not None:
        lines.append("## Metrics")
        lines.append("")
        lines.append("```json")
        lines.append(json.dumps(metrics, indent=2, ensure_ascii=False))
        lines.append("```")
        lines.append("")

    return "\n".join(lines)
