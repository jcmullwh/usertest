#!/usr/bin/env python3
"""Render generated lifecycle metrics as a deterministic, standalone dashboard.

The reporter's ``cohort_metrics.json`` remains authoritative.  This module only
validates and projects retained aggregates into a stable dashboard contract; it
does not infer missing measurements or make recommendations.
"""

from __future__ import annotations

import argparse
from collections.abc import Iterable, Mapping, Sequence
import hashlib
from html import escape
import json
from pathlib import Path
import string
import sys
from typing import Any


PROJECTION_SCHEMA_VERSION = 4
SUPPORTED_SOURCE_SCHEMA_VERSIONS = {1, 4}
DISPOSITIONS = (
    "already_addressed",
    "non_actionable",
    "duplicate",
    "superseded",
    "pr",
    "failed_incomplete",
)
STAT_FIELDS = ("count", "total", "median", "p75", "p90")
OBJECTIVES = {"decrease", "increase", "neutral"}
RENDERER_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


class GeneratedDashboardContractError(ValueError):
    """Raised when source metrics or a generated projection are malformed."""


def _mapping(value: object, *, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise GeneratedDashboardContractError(f"{label} must be an object")
    return value


def _list(value: object, *, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise GeneratedDashboardContractError(f"{label} must be a list")
    return value


def _text_or_none(value: object, *, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise GeneratedDashboardContractError(f"{label} must be null or non-empty text")
    return value.strip()


def _number_or_none(
    value: object,
    *,
    label: str,
    nonnegative: bool = True,
) -> int | float | None:
    if value is None:
        return None
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise GeneratedDashboardContractError(f"{label} must be null or a number")
    if nonnegative and value < 0:
        raise GeneratedDashboardContractError(f"{label} must not be negative")
    return value


def _integer_or_none(value: object, *, label: str) -> int | None:
    result = _number_or_none(value, label=label)
    if result is None:
        return None
    if not isinstance(result, int):
        raise GeneratedDashboardContractError(f"{label} must be null or an integer")
    return result


def _boolean_or_none(value: object, *, label: str) -> bool | None:
    if value is None:
        return None
    if not isinstance(value, bool):
        raise GeneratedDashboardContractError(f"{label} must be null or boolean")
    return value


def _path(value: Mapping[str, Any], dotted: str) -> object:
    current: object = value
    for part in dotted.split("."):
        if not isinstance(current, Mapping) or part not in current:
            return None
        current = current[part]
    return current


def _first(value: Mapping[str, Any], paths: Iterable[str]) -> object:
    for path in paths:
        candidate = _path(value, path)
        if candidate is not None:
            return candidate
    return None


def _stats(value: object, *, label: str) -> dict[str, int | float | None]:
    """Return the fixed distribution shape without converting unknown to zero."""

    result: dict[str, int | float | None] = dict.fromkeys(STAT_FIELDS)
    if value is None:
        return result
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        result["total"] = _number_or_none(value, label=label)
        return result
    source = _mapping(value, label=label)
    for field in STAT_FIELDS:
        candidate = source.get(field)
        if field == "count":
            result[field] = _integer_or_none(candidate, label=f"{label}.{field}")
        else:
            result[field] = _number_or_none(candidate, label=f"{label}.{field}")
    if (
        result["count"] == 0
        and result["median"] is None
        and result["p75"] is None
        and result["p90"] is None
    ):
        # Reporter distributions retain the additive identity (total=0) even
        # when no values were observed. A dashboard must not present that
        # implementation detail as a measured zero.
        result["total"] = None
    return result


def _stats_from(
    row: Mapping[str, Any],
    *paths: str,
    label: str,
) -> dict[str, int | float | None]:
    return _stats(_first(row, paths), label=label)


def _count_from(row: Mapping[str, Any], *paths: str, label: str) -> int | None:
    return _integer_or_none(_first(row, paths), label=label)


def _ratio_or_none(value: object, *, label: str) -> int | float | None:
    ratio = _number_or_none(value, label=label)
    if ratio is not None and ratio > 1:
        if ratio <= 100:
            return ratio / 100
        raise GeneratedDashboardContractError(
            f"{label} must be between 0 and 1 or 0 and 100"
        )
    return ratio


def _source_dispositions(source: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    raw = _first(
        source,
        (
            "by_disposition",
            "disposition_aggregates",
            "aggregates_by_disposition",
        ),
    )
    if raw is None:
        raise GeneratedDashboardContractError(
            "cohort metrics must include by_disposition aggregates"
        )
    rows: dict[str, Mapping[str, Any]] = {}
    if isinstance(raw, Mapping):
        for key, value in raw.items():
            disposition = str(key)
            rows[disposition] = _mapping(value, label=f"by_disposition.{disposition}")
    elif isinstance(raw, list):
        for index, value in enumerate(raw):
            row = _mapping(value, label=f"by_disposition[{index}]")
            row_disposition = _text_or_none(
                row.get("disposition"), label=f"by_disposition[{index}].disposition"
            )
            assert row_disposition is not None
            if row_disposition in rows:
                raise GeneratedDashboardContractError(
                    f"duplicate disposition aggregate: {row_disposition}"
                )
            rows[row_disposition] = row
    else:
        raise GeneratedDashboardContractError(
            "by_disposition must be an object or list"
        )
    unsupported = sorted(set(rows).difference(DISPOSITIONS))
    if unsupported:
        raise GeneratedDashboardContractError(
            "unsupported disposition aggregates: " + ", ".join(unsupported)
        )
    return rows


def _project_timing(row: Mapping[str, Any], *, prefix: str) -> dict[str, Any]:
    aliases = {
        "raw_to_disposition_seconds": (
            "case_distributions.atom_to_disposition_seconds",
            "case_distributions.raw_to_disposition_seconds",
            "time.raw_to_disposition_seconds",
            "timing.raw_to_disposition_seconds",
        ),
        "lifecycle_to_disposition_seconds": (
            "case_distributions.admission_to_disposition_seconds",
            "case_distributions.lifecycle_wall_seconds",
            "case_distributions.lifecycle_to_disposition_seconds",
            "time.lifecycle_to_disposition_seconds",
            "timing.lifecycle_to_disposition_seconds",
        ),
        "lineage_to_disposition_seconds": (
            "case_distributions.lineage_to_disposition_seconds",
            "time.lineage_to_disposition_seconds",
            "timing.lineage_to_disposition_seconds",
        ),
        "pr_creation_seconds": (
            "case_distributions.pr_creation_seconds",
            "time.pr_creation_seconds",
            "timing.pr_creation_seconds",
        ),
        "verified_outcome_seconds": (
            "case_distributions.pr_create_to_outcome_seconds",
            "case_distributions.verified_outcome_seconds",
            "time.verified_outcome_seconds",
            "timing.verified_outcome_seconds",
        ),
        "pipeline_active_seconds": (
            "case_distributions.pipeline_active_seconds",
            "case_distributions.direct_active_seconds",
            "time.pipeline_active_seconds",
        ),
        "manual_active_seconds": (
            "case_distributions.manual_active_seconds",
            "manual_actions.active_seconds",
            "time.manual_active_seconds",
        ),
        "queue_wait_seconds": (
            "case_distributions.queue_wait_seconds",
            "time.queue_wait_seconds",
            "timing.queue_wait_seconds",
        ),
        "provider_wait_seconds": (
            "case_distributions.provider_wait_seconds",
            "time.provider_wait_seconds",
            "timing.provider_wait_seconds",
        ),
        "ci_wait_seconds": (
            "case_distributions.ci_wait_seconds",
            "time.ci_wait_seconds",
            "timing.ci_wait_seconds",
        ),
        "approval_wait_seconds": (
            "case_distributions.approval_wait_seconds",
            "time.approval_wait_seconds",
            "timing.approval_wait_seconds",
        ),
        "external_wait_seconds": (
            "case_distributions.external_wait_seconds",
            "time.external_wait_seconds",
            "timing.external_wait_seconds",
        ),
        "unclassified_wall_seconds": (
            "case_distributions.unclassified_seconds",
            "case_distributions.unclassified_wall_seconds",
            "time.unclassified_wall_seconds",
            "timing.unclassified_wall_seconds",
        ),
        "worker_resource_seconds": (
            "case_distributions.all_in_accounted_resource_seconds",
            "case_distributions.worker_resource_seconds",
            "time.worker_resource_seconds",
            "timing.worker_resource_seconds",
        ),
    }
    return {
        name: _stats_from(row, *paths, label=f"{prefix}.{name}")
        for name, paths in aliases.items()
    }


def _accounting_stats(
    row: Mapping[str, Any],
    *,
    view: str,
    prefix: str,
) -> dict[str, int | float | None]:
    if view == "nonduplicative":
        value = _first(
            row,
            (
                "nonduplicative_accounting.gross.tokens.total_tokens",
                "nonduplicative_accounting.all_in.gross.total_tokens",
                "accounting.nonduplicative.gross.tokens.total_tokens",
                "tokens.nonduplicative_total_tokens",
            ),
        )
        return _stats(value, label=f"{prefix}.tokens.{view}")
    return _stats_from(
        row,
        f"case_distributions.{view}_total_tokens",
        f"tokens.{view}_total_tokens",
        f"tokens.{view}",
        label=f"{prefix}.tokens.{view}",
    )


def _project_completeness(
    row: Mapping[str, Any],
    *,
    case_count: int | None,
    certified: int | None,
    withheld: int | None,
    prefix: str,
) -> dict[str, Any]:
    source = _first(row, ("completeness", "telemetry_completeness"))
    source_map = source if isinstance(source, Mapping) else {}
    certified_value = _integer_or_none(
        _first(source_map, ("certified_case_count", "certified"))
        if source_map
        else certified,
        label=f"{prefix}.completeness.certified_case_count",
    )
    if certified_value is None:
        certified_value = certified
    withheld_value = _integer_or_none(
        _first(source_map, ("withheld_case_count", "withheld"))
        if source_map
        else withheld,
        label=f"{prefix}.completeness.withheld_case_count",
    )
    if withheld_value is None:
        withheld_value = withheld
    coverage = _ratio_or_none(
        _first(
            source_map,
            (
                "coverage",
                "ratio",
                "telemetry_coverage",
                "ratios.required_milestones_complete",
            ),
        ),
        label=f"{prefix}.completeness.coverage",
    )
    if coverage is None and case_count and certified_value is not None:
        coverage = certified_value / case_count
    status = _text_or_none(
        source_map.get("status"), label=f"{prefix}.completeness.status"
    )
    if status is None:
        complete = source_map.get("complete")
        if complete is True:
            status = "complete"
        elif complete is False:
            status = "partial"
        elif case_count is None or certified_value is None:
            status = "unknown"
        elif certified_value == case_count:
            status = "complete"
        else:
            status = "partial"
    return {
        "certified_case_count": certified_value,
        "withheld_case_count": withheld_value,
        "coverage": coverage,
        "status": status,
    }


def _project_disposition(
    disposition: str,
    row: Mapping[str, Any] | None,
) -> dict[str, Any]:
    prefix = f"by_disposition.{disposition}"
    if row is None:
        row = {}
        case_count = 0
    else:
        observed_case_count = _count_from(
            row, "case_count", label=f"{prefix}.case_count"
        )
        if observed_case_count is None:
            raise GeneratedDashboardContractError(f"{prefix}.case_count is required")
        case_count = observed_case_count
    automation_source = _first(row, ("automation_score_v1", "automation"))
    automation = automation_source if isinstance(automation_source, Mapping) else {}
    certified = _count_from(
        automation,
        "certified_case_count",
        label=f"{prefix}.automation_score_v1.certified_case_count",
    )
    withheld = _count_from(
        automation,
        "withheld_case_count",
        label=f"{prefix}.automation_score_v1.withheld_case_count",
    )
    error_clusters = _stats_from(
        row,
        "case_distributions.error_clusters",
        "errors.cluster_count",
        label=f"{prefix}.errors.cluster_count",
    )
    interventions = _stats_from(
        row,
        "case_distributions.supervisor_interventions",
        "interventions.count",
        label=f"{prefix}.interventions.count",
    )
    manual_actions = _stats_from(
        row,
        "case_distributions.manual_actions",
        "manual_actions.count",
        label=f"{prefix}.manual_actions.count",
    )
    return {
        "disposition": disposition,
        "case_count": case_count,
        "timing": _project_timing(row, prefix=prefix),
        "tokens": {
            view: _accounting_stats(row, view=view, prefix=prefix)
            for view in ("direct", "inclusive", "nonduplicative", "all_in")
        },
        "errors": {
            "clusters": error_clusters,
            "occurrences": _count_from(
                row,
                "errors.occurrence_count",
                "errors.occurrences",
                label=f"{prefix}.errors.occurrence_count",
            ),
            "self_healed_clusters": _count_from(
                row,
                "errors.self_healed_cluster_count",
                "errors.self_healed_clusters",
                label=f"{prefix}.errors.self_healed_cluster_count",
            ),
            "externally_resolved_clusters": _count_from(
                row,
                "errors.externally_resolved_cluster_count",
                "errors.externally_resolved_clusters",
                label=f"{prefix}.errors.externally_resolved_cluster_count",
            ),
            "unresolved_terminal_clusters": _count_from(
                row,
                "errors.unresolved_terminal_cluster_count",
                "errors.unresolved_terminal_clusters",
                label=f"{prefix}.errors.unresolved_terminal_cluster_count",
            ),
        },
        "interventions": {
            "supervising_agent": interventions,
        },
        "manual_actions": {
            "actions": manual_actions,
            "required_for_progress": _count_from(
                row,
                "manual_actions.required_for_progress_count",
                "manual_actions.required_count",
                label=f"{prefix}.manual_actions.required_for_progress_count",
            ),
            "policy_mandated": _count_from(
                row,
                "manual_actions.policy_mandated_count",
                label=f"{prefix}.manual_actions.policy_mandated_count",
            ),
            "passive_observations": _count_from(
                row,
                "manual_actions.passive_observation_count",
                label=f"{prefix}.manual_actions.passive_observation_count",
            ),
            "measurement_administration": _count_from(
                row,
                "manual_actions.measurement_administration_count",
                label=f"{prefix}.manual_actions.measurement_administration_count",
            ),
            "avoidable": _count_from(
                row,
                "manual_actions.avoidable_count",
                label=f"{prefix}.manual_actions.avoidable_count",
            ),
            "unavoidable": _count_from(
                row,
                "manual_actions.unavoidable_count",
                label=f"{prefix}.manual_actions.unavoidable_count",
            ),
            "unclassified": _count_from(
                row,
                "manual_actions.unclassified_count",
                label=f"{prefix}.manual_actions.unclassified_count",
            ),
            "active_seconds": _stats_from(
                row,
                "case_distributions.manual_active_seconds",
                "manual_actions.active_seconds",
                label=f"{prefix}.manual_actions.active_seconds",
            ),
        },
        "automation_score_v1": {
            "certified_case_count": certified,
            "withheld_case_count": withheld,
            "gross": _stats(
                automation.get("gross"), label=f"{prefix}.automation.gross"
            ),
            "avoidable": _stats(
                automation.get("avoidable"), label=f"{prefix}.automation.avoidable"
            ),
            "touchless_terminal_yield": _ratio_or_none(
                automation.get("touchless_terminal_yield"),
                label=f"{prefix}.automation.touchless_terminal_yield",
            ),
            "pipeline_autonomous_rate": _ratio_or_none(
                automation.get("pipeline_autonomous_rate"),
                label=f"{prefix}.automation.pipeline_autonomous_rate",
            ),
            "human_touch_free_rate": _ratio_or_none(
                automation.get("human_touch_free_rate"),
                label=f"{prefix}.automation.human_touch_free_rate",
            ),
        },
        "completeness": _project_completeness(
            row,
            case_count=case_count,
            certified=certified,
            withheld=withheld,
            prefix=prefix,
        ),
    }


def _project_cohort_summary(
    source: Mapping[str, Any],
    *,
    disposition_rows: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """Project observed burden before every lifecycle has a final disposition."""

    dispositioned_case_count = 0
    for disposition, row in disposition_rows.items():
        count = _count_from(
            row,
            "case_count",
            label=f"by_disposition.{disposition}.case_count",
        )
        if count is None:
            raise GeneratedDashboardContractError(
                f"by_disposition.{disposition}.case_count is required"
            )
        dispositioned_case_count += count

    case_count = _count_from(source, "case_count", label="cohort_metrics.case_count")
    if case_count is None:
        case_count = dispositioned_case_count
    if dispositioned_case_count > case_count:
        raise GeneratedDashboardContractError(
            "disposition case counts must not exceed cohort_metrics.case_count"
        )

    projected_source = dict(source)
    projected_source["case_count"] = case_count
    result = _project_disposition("all_cases", projected_source)
    result.pop("disposition")

    active_case_count = _count_from(
        source,
        "active_case_count",
        label="cohort_metrics.active_case_count",
    )
    if active_case_count is None:
        active_ids = source.get("active_case_lifecycle_ids")
        if isinstance(active_ids, list):
            active_case_count = len(active_ids)

    automation = source.get("automation_score_v1")
    automation_map = automation if isinstance(automation, Mapping) else {}
    terminal_case_count = _count_from(
        automation_map,
        "terminal_case_count",
        label="cohort_metrics.automation_score_v1.terminal_case_count",
    )
    if terminal_case_count is None:
        terminal_case_count = dispositioned_case_count

    reconciliation = source.get("reconciliation")
    reconciliation_map = reconciliation if isinstance(reconciliation, Mapping) else {}
    result.update(
        {
            "active_case_count": active_case_count,
            "terminal_case_count": terminal_case_count,
            "dispositioned_case_count": dispositioned_case_count,
            "disposition_pending_case_count": case_count - dispositioned_case_count,
            "reconciliation_ok": _boolean_or_none(
                reconciliation_map.get("ok"),
                label="cohort_metrics.reconciliation.ok",
            ),
        }
    )
    return result


def _fingerprint_text(value: object) -> str | None:
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, list):
        fingerprints = sorted(
            item.strip() for item in value if isinstance(item, str) and item.strip()
        )
        return ", ".join(fingerprints) if fingerprints else None
    return None


def _numeric_leaves(
    value: Mapping[str, Any], *, prefix: str = ""
) -> list[tuple[str, Any]]:
    leaves: list[tuple[str, Any]] = []
    for key in sorted(value):
        candidate = value[key]
        path = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(candidate, Mapping):
            leaves.extend(_numeric_leaves(candidate, prefix=path))
        elif candidate is None or (
            isinstance(candidate, (int, float)) and not isinstance(candidate, bool)
        ):
            leaves.append((path, candidate))
    return leaves


def _factual_comparison_rows(
    document: Mapping[str, Any], *, document_index: int
) -> list[Mapping[str, Any]]:
    factual = document.get("factual_deltas")
    if not isinstance(factual, Mapping):
        return []
    before = document.get("before")
    after = document.get("after")
    before_map = before if isinstance(before, Mapping) else {}
    after_map = after if isinstance(after, Mapping) else {}
    fingerprint = document.get("system_fingerprint_comparison")
    fingerprint_map = fingerprint if isinstance(fingerprint, Mapping) else {}
    comparison_id = (
        f"{before_map.get('cohort_id') or 'unknown'}->"
        f"{after_map.get('cohort_id') or 'unknown'}:{document_index}"
    )
    base: dict[str, Any] = {
        "comparison_id": comparison_id,
        "before_cohort_id": before_map.get("cohort_id"),
        "after_cohort_id": after_map.get("cohort_id"),
        "before_fingerprint": _fingerprint_text(fingerprint_map.get("before")),
        "after_fingerprint": _fingerprint_text(fingerprint_map.get("after")),
        "before_sample_size": before_map.get("case_count"),
        "after_sample_size": after_map.get("case_count"),
    }
    rows: list[Mapping[str, Any]] = []
    disposition_counts = factual.get("disposition_counts")
    disposition_count_map = (
        disposition_counts if isinstance(disposition_counts, Mapping) else {}
    )
    per_disposition = document.get("per_disposition")
    per_disposition_map = (
        per_disposition if isinstance(per_disposition, Mapping) else {}
    )
    for disposition in sorted(disposition_count_map):
        group = per_disposition_map.get(disposition)
        group_map = group if isinstance(group, Mapping) else {}
        rows.append(
            {
                **base,
                "disposition": disposition,
                "metric": "case_count",
                "absolute_delta": disposition_count_map[disposition],
                "before_sample_size": group_map.get("before_case_count"),
                "after_sample_size": group_map.get("after_case_count"),
            }
        )

    for key, value in factual.items():
        if key == "disposition_counts":
            continue
        if isinstance(value, Mapping):
            for metric, delta in _numeric_leaves(value, prefix=str(key)):
                rows.append({**base, "metric": metric, "absolute_delta": delta})
        elif value is None or (
            isinstance(value, (int, float)) and not isinstance(value, bool)
        ):
            rows.append({**base, "metric": str(key), "absolute_delta": value})

    for disposition in sorted(per_disposition_map):
        group = per_disposition_map[disposition]
        if not isinstance(group, Mapping):
            continue
        for key, value in group.items():
            if key in {"before_case_count", "after_case_count", "case_count_delta"}:
                continue
            if not isinstance(value, Mapping):
                continue
            for metric, delta in _numeric_leaves(value, prefix=str(key)):
                rows.append(
                    {
                        **base,
                        "disposition": disposition,
                        "metric": metric,
                        "absolute_delta": delta,
                        "before_sample_size": group.get("before_case_count"),
                        "after_sample_size": group.get("after_case_count"),
                    }
                )
    return rows


def _comparison_rows(
    source: Mapping[str, Any],
    extra_comparisons: Sequence[object] = (),
) -> list[Mapping[str, Any]]:
    documents: list[object] = []
    embedded = _first(source, ("comparisons", "cohort_comparisons", "comparison"))
    if embedded is not None:
        documents.extend(embedded if isinstance(embedded, list) else [embedded])
    documents.extend(extra_comparisons)

    rows: list[Mapping[str, Any]] = []
    for document_index, value in enumerate(documents):
        document = _mapping(value, label=f"comparisons[{document_index}]")
        factual_rows = _factual_comparison_rows(document, document_index=document_index)
        if factual_rows:
            rows.extend(factual_rows)
            continue
        if all(isinstance(item, Mapping) for item in document.values()):
            rows.extend(
                dict(item, comparison_id=str(key))
                for key, item in sorted(document.items())
                if isinstance(item, Mapping)
            )
        else:
            rows.append(document)
    return rows


def _direction(
    *,
    before: int | float | None,
    after: int | float | None,
    absolute_delta: int | float | None,
    objective: str | None,
) -> str:
    if objective not in OBJECTIVES:
        return "unknown"
    delta = (
        after - before if before is not None and after is not None else absolute_delta
    )
    if delta is None:
        return "unknown"
    if delta == 0 or objective == "neutral":
        return "unchanged"
    improved = (objective == "decrease" and delta < 0) or (
        objective == "increase" and delta > 0
    )
    return "improved" if improved else "regressed"


def _project_comparison(row: Mapping[str, Any], *, index: int) -> dict[str, Any]:
    prefix = f"comparisons[{index}]"
    before = _number_or_none(
        _first(row, ("before", "before_value", "baseline_value")),
        label=f"{prefix}.before",
        nonnegative=False,
    )
    after = _number_or_none(
        _first(row, ("after", "after_value", "current_value")),
        label=f"{prefix}.after",
        nonnegative=False,
    )
    explicit_delta = _number_or_none(
        _first(row, ("absolute_delta", "delta")),
        label=f"{prefix}.absolute_delta",
        nonnegative=False,
    )
    absolute_delta = (
        after - before if before is not None and after is not None else explicit_delta
    )
    percentage_delta = _number_or_none(
        _first(row, ("percentage_delta", "percent_delta")),
        label=f"{prefix}.percentage_delta",
        nonnegative=False,
    )
    if absolute_delta is not None and before is not None and before != 0:
        percentage_delta = 100 * absolute_delta / abs(before)
    objective = _text_or_none(
        _first(row, ("objective", "metric_objective")), label=f"{prefix}.objective"
    )
    if objective is not None:
        objective = {"lower": "decrease", "higher": "increase"}.get(
            objective, objective
        )
        if objective not in OBJECTIVES:
            raise GeneratedDashboardContractError(
                f"{prefix}.objective must be decrease, increase, or neutral"
            )
    disposition = _text_or_none(row.get("disposition"), label=f"{prefix}.disposition")
    if disposition is not None and disposition not in DISPOSITIONS:
        raise GeneratedDashboardContractError(f"{prefix}.disposition is unsupported")
    return {
        "comparison_id": _text_or_none(
            row.get("comparison_id"), label=f"{prefix}.comparison_id"
        ),
        "before_cohort_id": _text_or_none(
            _first(row, ("before_cohort_id", "before_id")),
            label=f"{prefix}.before_cohort_id",
        ),
        "after_cohort_id": _text_or_none(
            _first(row, ("after_cohort_id", "after_id")),
            label=f"{prefix}.after_cohort_id",
        ),
        "before_fingerprint": _text_or_none(
            row.get("before_fingerprint"), label=f"{prefix}.before_fingerprint"
        ),
        "after_fingerprint": _text_or_none(
            row.get("after_fingerprint"), label=f"{prefix}.after_fingerprint"
        ),
        "disposition": disposition,
        "metric": _text_or_none(
            _first(row, ("metric", "metric_name")), label=f"{prefix}.metric"
        ),
        "objective": objective,
        "before": before,
        "after": after,
        "absolute_delta": absolute_delta,
        "percentage_delta": percentage_delta,
        "before_sample_size": _integer_or_none(
            _first(row, ("before_sample_size", "before_n")),
            label=f"{prefix}.before_sample_size",
        ),
        "after_sample_size": _integer_or_none(
            _first(row, ("after_sample_size", "after_n")),
            label=f"{prefix}.after_sample_size",
        ),
        "coverage": _ratio_or_none(row.get("coverage"), label=f"{prefix}.coverage"),
        "observed_direction": _direction(
            before=before,
            after=after,
            absolute_delta=absolute_delta,
            objective=objective,
        ),
    }


def build_dashboard_projection(
    source_value: object,
    *,
    comparison_values: Sequence[object] = (),
) -> dict[str, Any]:
    """Validate reporter output and return the deterministic dashboard projection."""

    source = _mapping(source_value, label="cohort_metrics")
    schema_version = source.get("schema_version")
    if schema_version not in SUPPORTED_SOURCE_SCHEMA_VERSIONS:
        raise GeneratedDashboardContractError(
            "cohort_metrics.schema_version must be a supported generated schema version"
        )
    rows = _source_dispositions(source)
    canonical_source = json.dumps(
        source,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    comparison_hashes = [
        hashlib.sha256(
            json.dumps(
                comparison,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
        ).hexdigest()
        for comparison in comparison_values
    ]
    version_boundaries = source.get("version_boundaries")
    version_boundary_map = (
        version_boundaries if isinstance(version_boundaries, Mapping) else {}
    )
    raw_version_warnings = source.get("version_warnings")
    warning_rows = (
        raw_version_warnings if isinstance(raw_version_warnings, list) else []
    )
    version_warning_codes = sorted(
        {
            str(code)
            for warning in warning_rows
            for code in (
                [warning.get("code")] if isinstance(warning, Mapping) else [warning]
            )
            if isinstance(code, str) and code.strip()
        }
    )
    projection = {
        "schema_version": PROJECTION_SCHEMA_VERSION,
        "document_type": "generated_pipeline_metrics_dashboard",
        "source": {
            "schema_version": schema_version,
            "metric_version": _text_or_none(
                _first(source, ("metric_version", "metric_definition_version")),
                label="cohort_metrics.metric_version",
            ),
            "cohort_id": _text_or_none(
                source.get("cohort_id"), label="cohort_metrics.cohort_id"
            ),
            "generated_at": _text_or_none(
                _first(source, ("generated_at", "as_of")),
                label="cohort_metrics.generated_at",
            ),
            "sha256": hashlib.sha256(canonical_source).hexdigest(),
            "renderer_sha256": RENDERER_SHA256,
            "comparison_sha256s": comparison_hashes,
            "mixed_version_lifecycle": (
                version_boundary_map.get("mixed_system_fingerprints") is True
            ),
            "version_warning_codes": version_warning_codes,
        },
        "cohort_summary": _project_cohort_summary(
            source,
            disposition_rows=rows,
        ),
        "dispositions": [
            _project_disposition(disposition, rows.get(disposition))
            for disposition in DISPOSITIONS
        ],
        "comparisons": [
            _project_comparison(row, index=index)
            for index, row in enumerate(_comparison_rows(source, comparison_values))
        ],
        "notes": [
            "Unknown values are retained as null and rendered as unknown.",
            "This dashboard is observational and does not generate recommendations.",
        ],
    }
    validate_dashboard_projection(projection)
    return projection


def _validate_stats(value: object, *, label: str) -> None:
    stats = _mapping(value, label=label)
    if set(stats) != set(STAT_FIELDS):
        raise GeneratedDashboardContractError(
            f"{label} must contain exactly {', '.join(STAT_FIELDS)}"
        )
    for field in STAT_FIELDS:
        if field == "count":
            _integer_or_none(stats[field], label=f"{label}.{field}")
        else:
            _number_or_none(stats[field], label=f"{label}.{field}")


def _validate_metrics_projection(row: Mapping[str, Any], *, label: str) -> None:
    _integer_or_none(row.get("case_count"), label=f"{label}.case_count")
    for group_name in ("timing", "tokens"):
        group = _mapping(row.get(group_name), label=f"{label}.{group_name}")
        for metric_name, metric in group.items():
            _validate_stats(metric, label=f"{label}.{group_name}.{metric_name}")
    errors = _mapping(row.get("errors"), label=f"{label}.errors")
    _validate_stats(errors.get("clusters"), label=f"{label}.errors.clusters")
    for field in (
        "occurrences",
        "self_healed_clusters",
        "externally_resolved_clusters",
        "unresolved_terminal_clusters",
    ):
        _integer_or_none(errors.get(field), label=f"{label}.errors.{field}")
    interventions = _mapping(row.get("interventions"), label=f"{label}.interventions")
    _validate_stats(
        interventions.get("supervising_agent"),
        label=f"{label}.interventions.supervising_agent",
    )
    manual = _mapping(row.get("manual_actions"), label=f"{label}.manual_actions")
    _validate_stats(manual.get("actions"), label=f"{label}.manual_actions.actions")
    _validate_stats(
        manual.get("active_seconds"), label=f"{label}.manual_actions.active_seconds"
    )
    for field in (
        "required_for_progress",
        "policy_mandated",
        "passive_observations",
        "measurement_administration",
        "avoidable",
        "unavoidable",
        "unclassified",
    ):
        _integer_or_none(manual.get(field), label=f"{label}.manual_actions.{field}")
    automation = _mapping(
        row.get("automation_score_v1"), label=f"{label}.automation_score_v1"
    )
    _validate_stats(automation.get("gross"), label=f"{label}.automation.gross")
    _validate_stats(automation.get("avoidable"), label=f"{label}.automation.avoidable")
    for field in ("certified_case_count", "withheld_case_count"):
        _integer_or_none(automation.get(field), label=f"{label}.automation.{field}")
    for field in (
        "touchless_terminal_yield",
        "pipeline_autonomous_rate",
        "human_touch_free_rate",
    ):
        ratio = _number_or_none(
            automation.get(field), label=f"{label}.automation.{field}"
        )
        if ratio is not None and ratio > 1:
            raise GeneratedDashboardContractError(
                f"{label}.automation.{field} must not exceed 1"
            )
    completeness = _mapping(row.get("completeness"), label=f"{label}.completeness")
    for field in ("certified_case_count", "withheld_case_count"):
        _integer_or_none(completeness.get(field), label=f"{label}.completeness.{field}")
    if completeness.get("status") not in {"complete", "partial", "unknown"}:
        raise GeneratedDashboardContractError(f"{label}.completeness.status is invalid")
    coverage = _number_or_none(
        completeness.get("coverage"), label=f"{label}.completeness.coverage"
    )
    if coverage is not None and coverage > 1:
        raise GeneratedDashboardContractError(
            f"{label}.completeness.coverage must not exceed 1"
        )


def validate_dashboard_projection(value: object) -> Mapping[str, Any]:
    """Validate the stable machine-readable dashboard contract."""

    projection = _mapping(value, label="dashboard")
    if projection.get("schema_version") != PROJECTION_SCHEMA_VERSION:
        raise GeneratedDashboardContractError("dashboard.schema_version must equal 4")
    if projection.get("document_type") != "generated_pipeline_metrics_dashboard":
        raise GeneratedDashboardContractError("dashboard.document_type is invalid")
    source = _mapping(projection.get("source"), label="dashboard.source")
    if source.get("schema_version") not in SUPPORTED_SOURCE_SCHEMA_VERSIONS:
        raise GeneratedDashboardContractError(
            "dashboard.source.schema_version is unsupported"
        )
    _text_or_none(source.get("cohort_id"), label="dashboard.source.cohort_id")
    digest = _text_or_none(source.get("sha256"), label="dashboard.source.sha256")
    if (
        digest is None
        or len(digest) != 64
        or any(character not in string.hexdigits for character in digest)
    ):
        raise GeneratedDashboardContractError(
            "dashboard.source.sha256 must be a SHA-256 digest"
        )
    renderer_digest = _text_or_none(
        source.get("renderer_sha256"), label="dashboard.source.renderer_sha256"
    )
    if (
        renderer_digest is None
        or len(renderer_digest) != 64
        or any(character not in string.hexdigits for character in renderer_digest)
    ):
        raise GeneratedDashboardContractError(
            "dashboard.source.renderer_sha256 must be a SHA-256 digest"
        )
    comparison_digests = _list(
        source.get("comparison_sha256s"), label="dashboard.source.comparison_sha256s"
    )
    for index, comparison_digest in enumerate(comparison_digests):
        digest_text = _text_or_none(
            comparison_digest, label=f"dashboard.source.comparison_sha256s[{index}]"
        )
        if (
            digest_text is None
            or len(digest_text) != 64
            or any(character not in string.hexdigits for character in digest_text)
        ):
            raise GeneratedDashboardContractError(
                f"dashboard.source.comparison_sha256s[{index}] must be a SHA-256 digest"
            )
    if not isinstance(source.get("mixed_version_lifecycle"), bool):
        raise GeneratedDashboardContractError(
            "dashboard.source.mixed_version_lifecycle must be boolean"
        )
    warning_codes = _list(
        source.get("version_warning_codes"),
        label="dashboard.source.version_warning_codes",
    )
    for index, warning_code in enumerate(warning_codes):
        if (
            _text_or_none(
                warning_code,
                label=f"dashboard.source.version_warning_codes[{index}]",
            )
            is None
        ):
            raise GeneratedDashboardContractError(
                "dashboard.source.version_warning_codes must contain strings"
            )

    cohort_summary = _mapping(
        projection.get("cohort_summary"), label="dashboard.cohort_summary"
    )
    _validate_metrics_projection(cohort_summary, label="cohort_summary")
    for field in (
        "active_case_count",
        "terminal_case_count",
        "dispositioned_case_count",
        "disposition_pending_case_count",
    ):
        _integer_or_none(cohort_summary.get(field), label=f"cohort_summary.{field}")
    _boolean_or_none(
        cohort_summary.get("reconciliation_ok"),
        label="cohort_summary.reconciliation_ok",
    )

    dispositions = _list(projection.get("dispositions"), label="dashboard.dispositions")
    if len(dispositions) != len(DISPOSITIONS):
        raise GeneratedDashboardContractError(
            "dashboard must contain every disposition"
        )
    seen: list[str] = []
    for index, value in enumerate(dispositions):
        row = _mapping(value, label=f"dashboard.dispositions[{index}]")
        disposition = _text_or_none(
            row.get("disposition"), label=f"dashboard.dispositions[{index}].disposition"
        )
        assert disposition is not None
        seen.append(disposition)
        _validate_metrics_projection(row, label=disposition)
    if tuple(seen) != DISPOSITIONS:
        raise GeneratedDashboardContractError("dashboard disposition order is invalid")

    comparisons = _list(projection.get("comparisons"), label="dashboard.comparisons")
    for index, value in enumerate(comparisons):
        row = _mapping(value, label=f"dashboard.comparisons[{index}]")
        for field in ("before", "after", "absolute_delta", "percentage_delta"):
            _number_or_none(
                row.get(field), label=f"comparisons[{index}].{field}", nonnegative=False
            )
        direction = row.get("observed_direction")
        if direction not in {"improved", "regressed", "unchanged", "unknown"}:
            raise GeneratedDashboardContractError(
                f"comparisons[{index}].observed_direction is invalid"
            )
    return projection


def _format_number(value: object, *, decimals: int = 1) -> str:
    if value is None:
        return "unknown"
    if isinstance(value, int):
        return f"{value:,}"
    if isinstance(value, float):
        return f"{value:,.{decimals}f}"
    return escape(str(value))


def _format_duration(stats: Mapping[str, Any]) -> str:
    median = stats.get("median")
    p90 = stats.get("p90")
    total = stats.get("total")
    if median is None and p90 is None and total is None:
        return "unknown"
    if median is None and p90 is None:
        return f"total {_format_number(total)} s"
    return f"median {_format_number(median)} s / p90 {_format_number(p90)} s"


def _format_waits(timing: Mapping[str, Any]) -> str:
    values: list[str] = []
    for key, label in (
        ("queue_wait_seconds", "queue"),
        ("provider_wait_seconds", "provider"),
        ("ci_wait_seconds", "CI"),
        ("approval_wait_seconds", "approval"),
        ("external_wait_seconds", "external"),
    ):
        stats = _mapping(timing[key], label=f"timing.{key}")
        median = stats.get("median")
        if median is not None:
            values.append(f"{label} {_format_number(median)} s median")
    return "; ".join(values) if values else "unknown"


def _format_token_stats(stats: Mapping[str, Any]) -> str:
    median = stats.get("median")
    total = stats.get("total")
    if median is None and total is None:
        return "unknown"
    return f"median {_format_number(median, decimals=0)} / total {_format_number(total, decimals=0)}"


def _format_score(stats: Mapping[str, Any]) -> str:
    median = stats.get("median")
    p90 = stats.get("p90")
    if median is None and p90 is None:
        return "unknown"
    return f"median {_format_number(median)} / p90 {_format_number(p90)}"


def _format_ratio(value: object) -> str:
    if value is None:
        return "unknown"
    assert isinstance(value, (int, float))
    return f"{100 * value:.1f}%"


def _format_percentage_delta(value: object) -> str:
    if value is None:
        return "unknown"
    return f"{_format_number(value)}%"


def _human_label(value: str) -> str:
    if value == "pr":
        return "PR"
    return value.replace("_", " ").title()


def render_dashboard_html(projection_value: object) -> str:
    """Return deterministic standalone HTML for a validated projection."""

    projection = validate_dashboard_projection(projection_value)
    source = _mapping(projection["source"], label="dashboard.source")
    cohort = _mapping(projection["cohort_summary"], label="dashboard.cohort_summary")
    cohort_errors = _mapping(cohort["errors"], label="cohort_summary.errors")
    cohort_clusters = _mapping(
        cohort_errors["clusters"], label="cohort_summary.errors.clusters"
    )
    cohort_interventions = _mapping(
        cohort["interventions"], label="cohort_summary.interventions"
    )
    cohort_supervisor = _mapping(
        cohort_interventions["supervising_agent"],
        label="cohort_summary.interventions.supervising_agent",
    )
    cohort_manual = _mapping(
        cohort["manual_actions"], label="cohort_summary.manual_actions"
    )
    cohort_manual_actions = _mapping(
        cohort_manual["actions"], label="cohort_summary.manual_actions.actions"
    )
    cohort_automation = _mapping(
        cohort["automation_score_v1"], label="cohort_summary.automation_score_v1"
    )
    reconciliation = cohort.get("reconciliation_ok")
    reconciliation_text = (
        "complete"
        if reconciliation is True
        else "incomplete"
        if reconciliation is False
        else "unknown"
    )
    disposition_rows: list[str] = []
    for value in projection["dispositions"]:
        row = _mapping(value, label="dashboard.disposition")
        timing = _mapping(row["timing"], label="timing")
        tokens = _mapping(row["tokens"], label="tokens")
        errors = _mapping(row["errors"], label="errors")
        clusters = _mapping(errors["clusters"], label="errors.clusters")
        interventions = _mapping(row["interventions"], label="interventions")
        supervisor = _mapping(interventions["supervising_agent"], label="supervisor")
        manual = _mapping(row["manual_actions"], label="manual")
        manual_stats = _mapping(manual["actions"], label="manual.actions")
        automation = _mapping(row["automation_score_v1"], label="automation")
        completeness = _mapping(row["completeness"], label="completeness")
        error_summary = (
            f"clusters {_format_number(clusters.get('total'), decimals=0)}; "
            f"occurrences {_format_number(errors.get('occurrences'), decimals=0)}; "
            f"self-healed {_format_number(errors.get('self_healed_clusters'), decimals=0)}; "
            f"external {_format_number(errors.get('externally_resolved_clusters'), decimals=0)}; "
            f"unresolved {_format_number(errors.get('unresolved_terminal_clusters'), decimals=0)}"
        )
        manual_summary = (
            f"total {_format_number(manual_stats.get('total'), decimals=0)}; "
            f"required {_format_number(manual.get('required_for_progress'), decimals=0)}; "
            f"active {_format_duration(_mapping(manual['active_seconds'], label='manual.active_seconds'))}"
        )
        completeness_summary = (
            f"{escape(str(completeness.get('status') or 'unknown'))}; "
            f"coverage {_format_ratio(completeness.get('coverage'))}; "
            f"certified {_format_number(completeness.get('certified_case_count'), decimals=0)}"
        )
        disposition_rows.append(
            "<tr>"
            f'<th scope="row">{escape(_human_label(str(row["disposition"])))}</th>'
            f"<td>{_format_number(row.get('case_count'), decimals=0)}</td>"
            f"<td>{_format_duration(_mapping(timing['raw_to_disposition_seconds'], label='timing.raw'))}</td>"
            f"<td>{_format_duration(_mapping(timing['lifecycle_to_disposition_seconds'], label='timing.lifecycle'))}</td>"
            f"<td>{_format_duration(_mapping(timing['lineage_to_disposition_seconds'], label='timing.lineage'))}</td>"
            f"<td>{_format_duration(_mapping(timing['pr_creation_seconds'], label='timing.pr_creation'))}</td>"
            f"<td>{_format_duration(_mapping(timing['verified_outcome_seconds'], label='timing.verified_outcome'))}</td>"
            f"<td>{_format_duration(_mapping(timing['pipeline_active_seconds'], label='timing.active'))}</td>"
            f"<td>{escape(_format_waits(timing))}</td>"
            f"<td>{_format_duration(_mapping(timing['unclassified_wall_seconds'], label='timing.unclassified'))}</td>"
            f"<td>{_format_token_stats(_mapping(tokens['direct'], label='tokens.direct'))}</td>"
            f"<td>{_format_token_stats(_mapping(tokens['inclusive'], label='tokens.inclusive'))}</td>"
            f"<td>{_format_token_stats(_mapping(tokens['nonduplicative'], label='tokens.nonduplicative'))}</td>"
            f"<td>{_format_token_stats(_mapping(tokens['all_in'], label='tokens.all_in'))}</td>"
            f"<td>{escape(error_summary)}</td>"
            f"<td>{_format_number(supervisor.get('total'), decimals=0)}</td>"
            f"<td>{escape(manual_summary)}</td>"
            f"<td>{escape(_format_score(_mapping(automation['gross'], label='automation.gross')))}</td>"
            f"<td>{escape(_format_score(_mapping(automation['avoidable'], label='automation.avoidable')))}</td>"
            f"<td>{escape(completeness_summary)}</td>"
            "</tr>"
        )

    comparison_rows: list[str] = []
    for value in projection["comparisons"]:
        row = _mapping(value, label="comparison")
        comparison_rows.append(
            "<tr>"
            f"<td>{escape(str(row.get('before_fingerprint') or row.get('before_cohort_id') or 'unknown'))}</td>"
            f"<td>{escape(str(row.get('after_fingerprint') or row.get('after_cohort_id') or 'unknown'))}</td>"
            f"<td>{escape(_human_label(str(row.get('disposition') or 'unknown')))}</td>"
            f"<td>{escape(str(row.get('metric') or 'unknown'))}</td>"
            f"<td>{escape(str(row.get('objective') or 'unknown'))}</td>"
            f"<td>{_format_number(row.get('before'))}</td>"
            f"<td>{_format_number(row.get('after'))}</td>"
            f"<td>{_format_number(row.get('absolute_delta'))}</td>"
            f"<td>{_format_percentage_delta(row.get('percentage_delta'))}</td>"
            f"<td>{_format_number(row.get('before_sample_size'), decimals=0)} / {_format_number(row.get('after_sample_size'), decimals=0)}</td>"
            f"<td>{_format_ratio(row.get('coverage'))}</td>"
            f"<td>{escape(str(row.get('observed_direction') or 'unknown'))}</td>"
            "</tr>"
        )
    if not comparison_rows:
        comparison_rows.append(
            '<tr><td colspan="12" class="unknown">No retained comparison data; status unknown.</td></tr>'
        )

    cohort_id = escape(str(source.get("cohort_id") or "unknown"))
    generated_at = escape(str(source.get("generated_at") or "unknown"))
    metric_version = escape(str(source.get("metric_version") or "unknown"))
    version_warning_codes = _list(
        source.get("version_warning_codes"),
        label="dashboard.source.version_warning_codes",
    )
    version_notice = (
        '<p class="notice">Version boundary warning: '
        + escape(", ".join(str(code) for code in version_warning_codes))
        + ". Compare like-for-like fingerprint cohorts.</p>"
        if version_warning_codes
        else ""
    )
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Pipeline lifecycle metrics — {cohort_id}</title>
  <style>
    :root {{ color-scheme: light dark; font-family: Inter, ui-sans-serif, system-ui, sans-serif; }}
    body {{ margin: 0; padding: 2rem; background: #101418; color: #edf2f7; }}
    main {{ max-width: 1800px; margin: auto; }}
    h1, h2 {{ letter-spacing: -.02em; }}
    .meta {{ display: flex; flex-wrap: wrap; gap: .75rem 1.5rem; color: #aebbc8; }}
    .notice {{ border-left: .3rem solid #65a7ff; padding: .75rem 1rem; background: #17212b; }}
    .cards {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(11rem, 1fr)); gap: .75rem; margin: 1rem 0 2rem; }}
    .card {{ border: 1px solid #31404d; border-radius: .6rem; padding: .9rem 1rem; background: #151d24; }}
    .card .value {{ display: block; margin-top: .35rem; font-size: 1.55rem; color: #b8d7ff; }}
    .card .label {{ color: #aebbc8; font-size: .82rem; }}
    .table-wrap {{ overflow-x: auto; border: 1px solid #31404d; border-radius: .6rem; }}
    table {{ border-collapse: collapse; width: 100%; min-width: 1500px; font-size: .86rem; }}
    th, td {{ border-bottom: 1px solid #2b3945; padding: .65rem .7rem; text-align: left; vertical-align: top; }}
    thead th {{ position: sticky; top: 0; background: #1a242e; z-index: 1; }}
    tbody th {{ white-space: nowrap; color: #a9d0ff; }}
    tbody tr:hover {{ background: #151d24; }}
    .unknown {{ color: #aebbc8; font-style: italic; }}
    code {{ color: #b8d7ff; }}
    footer {{ margin-top: 2rem; color: #93a1af; font-size: .82rem; }}
  </style>
</head>
<body>
<main>
  <h1>Pipeline lifecycle metrics</h1>
  <div class="meta">
    <span>Cohort: <code>{cohort_id}</code></span>
    <span>Metric version: <code>{metric_version}</code></span>
    <span>Generated: <code>{generated_at}</code></span>
    <span>Source: <code>{escape(str(source["sha256"]))}</code></span>
  </div>
  <p class="notice">Observed measurements only. Missing evidence is shown as <strong>unknown</strong>; no value is estimated as zero.</p>
  {version_notice}

  <h2>All observed cases</h2>
  <p>Current burden includes active and disposition-pending lifecycles; it is not hidden until closure.</p>
  <div class="cards">
    <div class="card"><span class="label">Cases</span><span class="value">{_format_number(cohort.get("case_count"), decimals=0)}</span></div>
    <div class="card"><span class="label">Active lifecycles</span><span class="value">{_format_number(cohort.get("active_case_count"), decimals=0)}</span></div>
    <div class="card"><span class="label">Disposition pending</span><span class="value">{_format_number(cohort.get("disposition_pending_case_count"), decimals=0)}</span></div>
    <div class="card"><span class="label">Terminal lifecycles</span><span class="value">{_format_number(cohort.get("terminal_case_count"), decimals=0)}</span></div>
    <div class="card"><span class="label">Error clusters</span><span class="value">{_format_number(cohort_clusters.get("total"), decimals=0)}</span></div>
    <div class="card"><span class="label">Error occurrences</span><span class="value">{_format_number(cohort_errors.get("occurrences"), decimals=0)}</span></div>
    <div class="card"><span class="label">Supervisor interventions</span><span class="value">{_format_number(cohort_supervisor.get("total"), decimals=0)}</span></div>
    <div class="card"><span class="label">Manual actions</span><span class="value">{_format_number(cohort_manual_actions.get("total"), decimals=0)}</span></div>
    <div class="card"><span class="label">Required manual actions</span><span class="value">{_format_number(cohort_manual.get("required_for_progress"), decimals=0)}</span></div>
    <div class="card"><span class="label">Automation cases withheld</span><span class="value">{_format_number(cohort_automation.get("withheld_case_count"), decimals=0)}</span></div>
    <div class="card"><span class="label">Accounting reconciliation</span><span class="value">{escape(reconciliation_text)}</span></div>
  </div>

  <h2>Metrics by final disposition</h2>
  <div class="table-wrap">
    <table>
      <thead><tr>
        <th>Disposition</th><th>Cases</th>
        <th>Raw → disposition</th><th>Admission → disposition</th><th>Lineage → disposition</th>
        <th>PR creation</th><th>Verified outcome</th><th>Pipeline active</th><th>Wait medians</th><th>Unclassified wall</th>
        <th>Direct tokens</th><th>Inclusive tokens</th><th>Nonduplicative tokens</th><th>All-in tokens</th>
        <th>Errors</th><th>Supervisor interventions</th><th>Manual actions</th>
        <th>Automation gross</th><th>Automation avoidable</th><th>Telemetry completeness</th>
      </tr></thead>
      <tbody>{"".join(disposition_rows)}</tbody>
    </table>
  </div>

  <h2>Before/after factual deltas</h2>
  <div class="table-wrap">
    <table>
      <thead><tr>
        <th>Before</th><th>After</th><th>Disposition</th><th>Metric</th><th>Objective</th>
        <th>Before value</th><th>After value</th><th>Absolute Δ</th><th>Percentage Δ</th>
        <th>Sample size</th><th>Coverage</th><th>Observed direction</th>
      </tr></thead>
      <tbody>{"".join(comparison_rows)}</tbody>
    </table>
  </div>
  <footer>Dashboard projection schema v{PROJECTION_SCHEMA_VERSION}. Rendering is deterministic from the retained machine-readable source.</footer>
</main>
</body>
</html>
"""


def _json_text(value: object) -> str:
    return json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def _write_text_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(content, encoding="utf-8", newline="\n")
    temporary.replace(path)


def materialize_dashboard(
    cohort_metrics_path: Path,
    *,
    html_output: Path,
    json_output: Path,
    comparison_paths: Sequence[Path] = (),
    check: bool = False,
) -> dict[str, Any]:
    """Build and write, or check, both generated dashboard artifacts."""

    source = json.loads(cohort_metrics_path.read_text(encoding="utf-8"))
    comparisons = [
        json.loads(path.read_text(encoding="utf-8")) for path in comparison_paths
    ]
    projection = build_dashboard_projection(source, comparison_values=comparisons)
    projection_text = _json_text(projection)
    html_text = render_dashboard_html(projection)
    if check:
        mismatches = []
        for path, expected in (
            (json_output, projection_text),
            (html_output, html_text),
        ):
            if not path.exists() or path.read_text(encoding="utf-8") != expected:
                mismatches.append(str(path))
        if mismatches:
            raise GeneratedDashboardContractError(
                "generated dashboard artifacts are stale or missing: "
                + ", ".join(mismatches)
            )
    else:
        _write_text_atomic(json_output, projection_text)
        _write_text_atomic(html_output, html_text)
    return projection


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Render generated cohort lifecycle metrics as a standalone dashboard."
    )
    parser.add_argument(
        "cohort_metrics", type=Path, help="Generated cohort_metrics.json"
    )
    parser.add_argument("--html-output", type=Path, help="Standalone HTML output path")
    parser.add_argument(
        "--json-output", type=Path, help="Validated JSON projection path"
    )
    parser.add_argument(
        "--comparison",
        action="append",
        default=[],
        type=Path,
        help="Optional compare_cohorts JSON; repeat for multiple factual comparisons",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Fail unless both outputs exactly match the generated projection",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    source_path = args.cohort_metrics.resolve()
    html_output = (
        args.html_output or source_path.with_name("metrics_dashboard.html")
    ).resolve()
    json_output = (
        args.json_output or source_path.with_name("metrics_dashboard.json")
    ).resolve()
    try:
        materialize_dashboard(
            source_path,
            html_output=html_output,
            json_output=json_output,
            comparison_paths=tuple(path.resolve() for path in args.comparison),
            check=args.check,
        )
    except (OSError, json.JSONDecodeError, GeneratedDashboardContractError) as exc:
        print(f"metrics dashboard error: {exc}", file=sys.stderr)
        return 1
    action = "verified" if args.check else "wrote"
    print(f"{action} {json_output}")
    print(f"{action} {html_output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
