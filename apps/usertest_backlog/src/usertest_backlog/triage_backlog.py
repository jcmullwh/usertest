from __future__ import annotations

import importlib
import json
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from typing import Any

from triage_engine import cluster_items_knn, dedupe_clusters
from triage_engine.embeddings import Embedder, dot, get_default_embedder
from triage_engine.similarity import build_item_vectors

_TEXT_FIELDS: tuple[str, ...] = (
    "problem",
    "user_impact",
    "proposed_fix",
    "body",
    "notes",
)
_LIST_FIELDS: tuple[str, ...] = (
    "investigation_steps",
    "success_criteria",
    "files",
    "paths",
)


def _coerce_string(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if value is None:
        return ""
    return str(value).strip()


def _coerce_list_strings(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        if isinstance(item, str):
            cleaned = item.strip()
            if cleaned:
                out.append(cleaned)
        elif isinstance(item, dict):
            for key in ("path", "file", "name", "title", "value"):
                candidate = _coerce_string(item.get(key))
                if candidate:
                    out.append(candidate)
                    break
    return out


def load_issue_items(path: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    metadata: dict[str, Any] = {}

    raw_items: Any = payload
    if isinstance(payload, dict):
        if isinstance(payload.get("tickets"), list):
            raw_items = payload["tickets"]
            metadata = {key: value for key, value in payload.items() if key != "tickets"}
        elif isinstance(payload.get("issues"), list):
            raw_items = payload["issues"]
            metadata = {key: value for key, value in payload.items() if key != "issues"}

    if not isinstance(raw_items, list):
        raise ValueError(
            "Input JSON must be either a list of issue-like objects or an object with "
            "a `tickets` list."
        )

    issues: list[dict[str, Any]] = []
    for idx, item in enumerate(raw_items):
        if not isinstance(item, dict):
            raise ValueError(f"Issue list item at index {idx} is not an object.")
        issues.append(dict(item))
    return issues, metadata


def issue_title(item: dict[str, Any]) -> str:
    if "title" not in item:
        raise ValueError("Issue item is missing required field `title`.")
    return _coerce_string(item.get("title"))


def issue_text_chunks(item: dict[str, Any]) -> list[str]:
    chunks: list[str] = []

    title = issue_title(item)
    if title:
        chunks.append(title)

    for field in _TEXT_FIELDS:
        value = _coerce_string(item.get(field))
        if value:
            chunks.append(value)

    for field in _LIST_FIELDS:
        chunks.extend(_coerce_list_strings(item.get(field)))

    return chunks


def _resolve_group_key(items: Sequence[dict[str, Any]], requested: str | None) -> str | None:
    if requested is not None:
        cleaned = requested.strip()
        if cleaned:
            return cleaned

    for item in items:
        value = item.get("package")
        if isinstance(value, str) and value.strip():
            return "package"
    return None


def _resolve_group(item: dict[str, Any], group_key: str | None) -> str | None:
    if group_key is None:
        return None
    value = _coerce_string(item.get(group_key))
    return value or None


def _base_global_id(item: dict[str, Any], *, group: str | None, fallback_index: int) -> str:
    ticket_id = _coerce_string(item.get("ticket_id")) or _coerce_string(item.get("id"))
    if group and ticket_id:
        return f"{group}/{ticket_id}"
    return f"issue-{fallback_index + 1:04d}"


def _assign_global_ids(items: Sequence[dict[str, Any]], groups: Sequence[str | None]) -> list[str]:
    seen: dict[str, int] = {}
    ids: list[str] = []
    for idx, item in enumerate(items):
        base = _base_global_id(item, group=groups[idx], fallback_index=idx)
        count = seen.get(base, 0) + 1
        seen[base] = count
        ids.append(base if count == 1 else f"{base}#{count}")
    return ids


def _embedding_similarity(left: tuple[float, ...], right: tuple[float, ...]) -> float:
    cosine = dot(left, right)
    cosine = max(-1.0, min(1.0, cosine))
    return (cosine + 1.0) / 2.0


def _select_medoid(relative_indices: Sequence[int], vectors: Sequence[tuple[float, ...]]) -> int:
    if not relative_indices:
        raise ValueError("Cannot select representative from empty indices.")
    if len(relative_indices) == 1:
        return relative_indices[0]

    best = relative_indices[0]
    best_score = -1.0
    for candidate in relative_indices:
        sims: list[float] = []
        for other in relative_indices:
            if other == candidate:
                continue
            sims.append(_embedding_similarity(vectors[candidate], vectors[other]))
        score = sum(sims) / float(len(sims)) if sims else 1.0
        if score > best_score or (score == best_score and candidate < best):
            best = candidate
            best_score = score
    return best


def triage_issues(
    items: Sequence[dict[str, Any]],
    *,
    group_key: str | None = None,
    dedupe_overall_threshold: float = 0.90,
    theme_overall_threshold: float = 0.78,
    theme_k: int = 10,
    theme_representative_threshold: float | None = 0.75,
    embedder: Embedder | None = None,
) -> dict[str, Any]:
    issues = [dict(item) for item in items]
    for issue in issues:
        issue_title(issue)

    effective_group_key = _resolve_group_key(issues, group_key)
    issue_groups = [_resolve_group(issue, effective_group_key) for issue in issues]
    issue_ids = _assign_global_ids(issues, issue_groups)

    chosen_embedder = embedder or get_default_embedder()

    dedupe_index_clusters = dedupe_clusters(
        issues,
        get_title=issue_title,
        get_text_chunks=issue_text_chunks,
        overall_similarity_threshold=float(dedupe_overall_threshold),
        include_singletons=True,
        embedder=chosen_embedder,
    )

    issue_to_dedupe_cluster: dict[int, int] = {}
    representative_issue_indices: list[int] = []
    dedupe_clusters_payload: list[dict[str, Any]] = []

    for cluster_number, indexes in enumerate(dedupe_index_clusters, start=1):
        representative_index = indexes[0]
        representative_issue_indices.append(representative_index)

        groups = sorted(
            {
                issue_groups[idx]
                for idx in indexes
                if issue_groups[idx] is not None and issue_groups[idx] != ""
            }
        )
        dedupe_clusters_payload.append(
            {
                "id": cluster_number,
                "size": len(indexes),
                "groups_count": len(groups),
                "groups": groups,
                "representative_title": issue_title(issues[representative_index]),
                "issue_ids": [issue_ids[idx] for idx in indexes],
            }
        )
        for idx in indexes:
            issue_to_dedupe_cluster[idx] = cluster_number

    representative_items = [issues[idx] for idx in representative_issue_indices]
    theme_index_clusters = cluster_items_knn(
        representative_items,
        get_title=issue_title,
        get_text_chunks=issue_text_chunks,
        embedder=chosen_embedder,
        k=int(theme_k),
        overall_similarity_threshold=float(theme_overall_threshold),
        representative_similarity_threshold=theme_representative_threshold,
        include_singletons=True,
    )

    representative_vectors = build_item_vectors(
        representative_items,
        get_title=issue_title,
        get_text_chunks=issue_text_chunks,
        embedder=chosen_embedder,
    )
    vector_values = [item.vector for item in representative_vectors]

    issue_to_theme_cluster: dict[int, int] = {}
    themes_payload: list[dict[str, Any]] = []
    for theme_number, representative_positions in enumerate(theme_index_clusters, start=1):
        dedupe_cluster_ids = sorted([position + 1 for position in representative_positions])

        expanded_issue_indices = sorted(
            {
                issue_index
                for dedupe_position in representative_positions
                for issue_index in dedupe_index_clusters[dedupe_position]
            }
        )
        for issue_index in expanded_issue_indices:
            issue_to_theme_cluster[issue_index] = theme_number

        representative_position = _select_medoid(representative_positions, vector_values)
        cohesion_values = [
            _embedding_similarity(vector_values[representative_position], vector_values[position])
            for position in representative_positions
        ]
        theme_groups = sorted(
            {
                issue_groups[idx]
                for idx in expanded_issue_indices
                if issue_groups[idx] is not None and issue_groups[idx] != ""
            }
        )

        themes_payload.append(
            {
                "id": theme_number,
                "size": len(expanded_issue_indices),
                "dedupe_clusters_count": len(dedupe_cluster_ids),
                "dedupe_cluster_ids": dedupe_cluster_ids,
                "groups_count": len(theme_groups),
                "groups": theme_groups,
                "representative_title": issue_title(representative_items[representative_position]),
                "cohesion_min_similarity": min(cohesion_values) if cohesion_values else 0.0,
                "cohesion_median_similarity": median(cohesion_values) if cohesion_values else 0.0,
                "issue_ids": [issue_ids[idx] for idx in expanded_issue_indices],
                "issues": [
                    {
                        "global_id": issue_ids[idx],
                        "title": issue_title(issues[idx]),
                        "group": issue_groups[idx],
                    }
                    for idx in expanded_issue_indices
                ],
            }
        )

    issues_payload = [
        {
            "global_id": issue_ids[idx],
            "title": issue_title(issue),
            "group": issue_groups[idx],
            "dedupe_cluster_id": issue_to_dedupe_cluster[idx],
            "theme_cluster_id": issue_to_theme_cluster[idx],
        }
        for idx, issue in enumerate(issues)
    ]

    unique_groups = sorted({group for group in issue_groups if group is not None and group != ""})
    common_themes = [theme for theme in themes_payload if int(theme.get("groups_count", 0)) >= 2]

    return {
        "schema_version": 1,
        "generated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "config": {
            "group_key": effective_group_key,
            "dedupe_overall_threshold": float(dedupe_overall_threshold),
            "theme_overall_threshold": float(theme_overall_threshold),
            "theme_k": int(theme_k),
            "theme_representative_threshold": theme_representative_threshold,
        },
        "totals": {
            "issues_total": len(issues),
            "dedupe_clusters_total": len(dedupe_clusters_payload),
            "theme_clusters_total": len(themes_payload),
            "groups_total": len(unique_groups),
            "common_themes_total": len(common_themes),
        },
        "issues": issues_payload,
        "dedupe_clusters": dedupe_clusters_payload,
        "themes": themes_payload,
    }


def render_triage_markdown(report: dict[str, Any], title: str) -> str:
    totals = report.get("totals", {})
    config = report.get("config", {})
    themes_raw = report.get("themes")
    themes = (
        [item for item in themes_raw if isinstance(item, dict)]
        if isinstance(themes_raw, list)
        else []
    )

    lines: list[str] = []
    lines.append(f"# {title}")
    lines.append("")
    lines.append(f"- Generated (UTC): `{report.get('generated_at_utc', '')}`")
    lines.append(f"- Issues: **{int(totals.get('issues_total', 0))}**")
    lines.append(f"- Dedupe clusters: **{int(totals.get('dedupe_clusters_total', 0))}**")
    lines.append(f"- Theme clusters: **{int(totals.get('theme_clusters_total', 0))}**")
    lines.append(f"- Group key: `{config.get('group_key')}`")
    lines.append(f"- Groups observed: **{int(totals.get('groups_total', 0))}**")
    lines.append(
        f"- Themes spanning multiple groups: **{int(totals.get('common_themes_total', 0))}**"
    )
    lines.append("")

    if not themes:
        lines.append("No themes were produced.")
        lines.append("")
        return "\n".join(lines)

    common = [theme for theme in themes if int(theme.get("groups_count", 0)) >= 2]
    if common:
        lines.append("## Common Across Groups")
        for theme in common:
            lines.append(
                f"- Theme {int(theme.get('id', 0))}: "
                f"{_coerce_string(theme.get('representative_title')) or 'Untitled'} "
                f"(groups={int(theme.get('groups_count', 0))}, "
                f"size={int(theme.get('size', 0))})"
            )
        lines.append("")

    for theme in themes:
        lines.append(f"## Theme {int(theme.get('id', 0))}")
        lines.append(
            f"- Representative: {_coerce_string(theme.get('representative_title')) or 'Untitled'}"
        )
        lines.append(f"- Size: **{int(theme.get('size', 0))}**")
        lines.append(
            f"- Groups: **{int(theme.get('groups_count', 0))}** "
            f"({', '.join(theme.get('groups', [])) if theme.get('groups') else 'none'})"
        )
        lines.append(
            f"- Cohesion min/median: **{float(theme.get('cohesion_min_similarity', 0.0)):.3f}** / "
            f"**{float(theme.get('cohesion_median_similarity', 0.0)):.3f}**"
        )

        members_raw = theme.get("issues")
        members = (
            [item for item in members_raw if isinstance(item, dict)]
            if isinstance(members_raw, list)
            else []
        )
        for member in members:
            member_id = _coerce_string(member.get("global_id")) or "unknown"
            member_title = _coerce_string(member.get("title")) or "Untitled"
            group = _coerce_string(member.get("group"))
            if group:
                lines.append(f"- {member_id} [{group}] {member_title}")
            else:
                lines.append(f"- {member_id} {member_title}")
        lines.append("")

    return "\n".join(lines)


def write_triage_xlsx(report: dict[str, Any], out_path: Path) -> None:
    try:
        openpyxl = importlib.import_module("openpyxl")
    except ModuleNotFoundError as exc:  # pragma: no cover
        raise RuntimeError(
            "XLSX output requested but `openpyxl` is not installed. "
            "Install it or omit --out-xlsx."
        ) from exc

    workbook = openpyxl.Workbook()

    themes_sheet = workbook.active
    themes_sheet.title = "themes"
    themes_sheet.append(
        [
            "id",
            "size",
            "groups_count",
            "groups",
            "representative_title",
            "cohesion_min_similarity",
            "cohesion_median_similarity",
            "dedupe_cluster_ids",
        ]
    )
    themes = report.get("themes", [])
    for theme in themes if isinstance(themes, list) else []:
        if not isinstance(theme, dict):
            continue
        themes_sheet.append(
            [
                int(theme.get("id", 0)),
                int(theme.get("size", 0)),
                int(theme.get("groups_count", 0)),
                ",".join([str(value) for value in theme.get("groups", [])]),
                _coerce_string(theme.get("representative_title")),
                float(theme.get("cohesion_min_similarity", 0.0)),
                float(theme.get("cohesion_median_similarity", 0.0)),
                ",".join([str(value) for value in theme.get("dedupe_cluster_ids", [])]),
            ]
        )

    issues_sheet = workbook.create_sheet("issues")
    issues_sheet.append(["global_id", "group", "title", "dedupe_cluster_id", "theme_cluster_id"])
    issues = report.get("issues", [])
    for issue in issues if isinstance(issues, list) else []:
        if not isinstance(issue, dict):
            continue
        issues_sheet.append(
            [
                _coerce_string(issue.get("global_id")),
                _coerce_string(issue.get("group")),
                _coerce_string(issue.get("title")),
                int(issue.get("dedupe_cluster_id", 0)),
                int(issue.get("theme_cluster_id", 0)),
            ]
        )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    workbook.save(str(out_path))
