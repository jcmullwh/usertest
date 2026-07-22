from __future__ import annotations

import json
import math
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from typing import Any, TextIO, cast

CASE_METRICS_VERSION = "lifecycle_case_metrics_v2"
AUTOMATION_SCORE_VERSION = "automation_score_v1"

TOKEN_DIMENSIONS = (
    "total_tokens",
    "input_tokens",
    "cached_input_tokens",
    "uncached_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
)

TOKEN_SCOPES = (
    "qualification",
    "implementation",
    "supervising_agent",
    "outside_platform",
    "unclassified",
)

WAIT_CATEGORIES = (
    "queue",
    "provider",
    "ci",
    "approval",
    "external",
    "unknown",
)

_NO_CHANGE_MILESTONES = (
    "origin",
    "stage1.problem_mining.completed",
    "stage2.prioritization.completed",
    "stage3.research.completed",
    "disposition.verified",
)
_PULL_REQUEST_MILESTONES = (
    "origin",
    "stage1.problem_mining.completed",
    "stage2.prioritization.completed",
    "stage3.research.completed",
    "stage4.optioning.completed",
    "stage5.selection.completed",
    "stage6.planning.completed",
    "delivery.completed",
    "disposition.verified",
)

# Disposition strings remain exact in reports.  Multiple producer spellings can select the
# same fixed score path without being collapsed into one reporting category.
AUTOMATION_SCORE_V1_MILESTONE_PATHS: dict[str, tuple[str, ...]] = {
    "already_addressed": _NO_CHANGE_MILESTONES,
    "non_actionable": _NO_CHANGE_MILESTONES,
    "pr": _PULL_REQUEST_MILESTONES,
    "duplicate": (
        "origin",
        "stage1.problem_mining.completed",
        "disposition.verified",
    ),
    "superseded": (
        "origin",
        "stage1.problem_mining.completed",
        "disposition.verified",
    ),
    "failed_incomplete": ("origin", "disposition.verified"),
}

EXACT_DISPOSITIONS = frozenset(AUTOMATION_SCORE_V1_MILESTONE_PATHS)

_DISPOSITION_ALIASES = {
    "already.addressed": "already_addressed",
    "already_addressed": "already_addressed",
    "non.actionable": "non_actionable",
    "non_actionable": "non_actionable",
    "duplicate": "duplicate",
    "superseded": "superseded",
    "pr": "pr",
    "pull.request": "pr",
    "pull_request": "pr",
    "implemented.pr": "pr",
    "implemented_pr": "pr",
    "merged.pr": "pr",
    "merged_pr": "pr",
    "failed.incomplete": "failed_incomplete",
    "failed_incomplete": "failed_incomplete",
    "failed": "failed_incomplete",
    "incomplete": "failed_incomplete",
    "cancelled": "failed_incomplete",
    "canceled": "failed_incomplete",
    "unresolved": "failed_incomplete",
}

_CANONICAL_EVENT_TYPES = {
    "lifecycle.opened",
    "lifecycle.closed",
    "stage.started",
    "stage.completed",
    "work.started",
    "work.created",
    "work.completed",
    "work.reused",
    "model.invocation.started",
    "model.invocation.completed",
    "error.occurred",
    "error.resolved",
    "intervention.started",
    "intervention.completed",
    "action.started",
    "action.completed",
    "disposition.reached",
    "disposition.verified",
    "delivery.started",
    "delivery.completed",
    "outcome.verified",
}

_EVENT_TYPE_ALIASES = {
    "case.opened": "lifecycle.opened",
    "case.started": "lifecycle.opened",
    "lifecycle.started": "lifecycle.opened",
    "lifecycle.open": "lifecycle.opened",
    "case.closed": "lifecycle.closed",
    "case.completed": "lifecycle.closed",
    "lifecycle.completed": "lifecycle.closed",
    "lifecycle.close": "lifecycle.closed",
    "stage.start": "stage.started",
    "stage.complete": "stage.completed",
    "work.unit.started": "work.started",
    "work.start": "work.started",
    "work.unit.created": "work.created",
    "work.unit.completed": "work.completed",
    "work.complete": "work.completed",
    "work.unit.reused": "work.reused",
    "model.call.started": "model.invocation.started",
    "model.call.completed": "model.invocation.completed",
    "author.invocation.started": "model.invocation.started",
    "author.invocation.completed": "model.invocation.completed",
    "error.cluster.occurred": "error.occurred",
    "error.cluster.resolved": "error.resolved",
    "error.detected": "error.occurred",
    "supervisor.intervention.started": "intervention.started",
    "supervisor.intervention.completed": "intervention.completed",
    "manual.action.started": "action.started",
    "manual.action.completed": "action.completed",
    "final.disposition": "disposition.reached",
    "case.disposition.reached": "disposition.reached",
    "case.disposition.verified": "disposition.verified",
    "pr.started": "delivery.started",
    "pr.created": "delivery.started",
    "pr.completed": "delivery.completed",
    "pr.merged": "delivery.completed",
    "case.outcome.verified": "outcome.verified",
    "delivery.outcome.verified": "outcome.verified",
}

_MILESTONE_ALIASES = {
    "origin": "origin",
    "raw.atoms": "origin",
    "raw.atoms.available": "origin",
    "stage1": "stage1.problem_mining.completed",
    "stage.1": "stage1.problem_mining.completed",
    "stage1.completed": "stage1.problem_mining.completed",
    "stage1.problem.mining.completed": "stage1.problem_mining.completed",
    "problem.mining": "stage1.problem_mining.completed",
    "problem.mining.completed": "stage1.problem_mining.completed",
    "stage2": "stage2.prioritization.completed",
    "stage.2": "stage2.prioritization.completed",
    "stage2.completed": "stage2.prioritization.completed",
    "stage2.prioritization.completed": "stage2.prioritization.completed",
    "prioritization": "stage2.prioritization.completed",
    "prioritization.completed": "stage2.prioritization.completed",
    "stage3": "stage3.research.completed",
    "stage.3": "stage3.research.completed",
    "stage3.completed": "stage3.research.completed",
    "stage3.research.completed": "stage3.research.completed",
    "research": "stage3.research.completed",
    "research.completed": "stage3.research.completed",
    "stage4": "stage4.optioning.completed",
    "stage.4": "stage4.optioning.completed",
    "stage4.completed": "stage4.optioning.completed",
    "stage4.optioning.completed": "stage4.optioning.completed",
    "optioning": "stage4.optioning.completed",
    "optioning.completed": "stage4.optioning.completed",
    "stage5": "stage5.selection.completed",
    "stage.5": "stage5.selection.completed",
    "stage5.completed": "stage5.selection.completed",
    "stage5.selection.completed": "stage5.selection.completed",
    "selection": "stage5.selection.completed",
    "selection.completed": "stage5.selection.completed",
    "stage6": "stage6.planning.completed",
    "stage.6": "stage6.planning.completed",
    "stage6.completed": "stage6.planning.completed",
    "stage6.planning.completed": "stage6.planning.completed",
    "planning": "stage6.planning.completed",
    "planning.completed": "stage6.planning.completed",
    "delivery": "delivery.completed",
    "delivery.completed": "delivery.completed",
    "disposition": "disposition.verified",
    "disposition.verified": "disposition.verified",
}


class LifecycleMetricsError(ValueError):
    """Raised when lifecycle input cannot be decoded as event objects."""


LifecycleSource = (
    str
    | Path
    | TextIO
    | Mapping[str, Any]
    | Iterable[Mapping[str, Any]]
    | Iterable[str | Path]
)


def _read_jsonl(stream: TextIO, *, source_name: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for line_number, line in enumerate(stream, start=1):
        if not line.strip():
            continue
        try:
            decoded = json.loads(line)
        except json.JSONDecodeError as exc:
            raise LifecycleMetricsError(
                f"Invalid lifecycle JSONL at {source_name}:{line_number}: {exc.msg}"
            ) from exc
        if not isinstance(decoded, dict):
            raise LifecycleMetricsError(
                f"Lifecycle event at {source_name}:{line_number} must be an object"
            )
        events.append(decoded)
    return events


def load_lifecycle_events(source: LifecycleSource) -> list[dict[str, Any]]:
    """Load lifecycle events without importing or depending on ``run_artifacts``.

    ``source`` may be a JSONL path, an open text stream, one event mapping, an
    iterable of event mappings, or an iterable of JSONL paths.  Returned objects
    are shallow copies so aggregation never mutates producer-owned dictionaries.
    """

    if isinstance(source, Mapping):
        return [dict(source)]
    if isinstance(source, (str, Path)):
        path = Path(source)
        with path.open("r", encoding="utf-8") as path_stream:
            return _read_jsonl(path_stream, source_name=str(path))
    if hasattr(source, "read"):
        source_stream = cast(TextIO, source)
        return _read_jsonl(
            source_stream,
            source_name=str(getattr(source_stream, "name", "<stream>")),
        )

    events: list[dict[str, Any]] = []
    for item in source:
        if isinstance(item, Mapping):
            events.append(dict(item))
        elif isinstance(item, (str, Path)):
            events.extend(load_lifecycle_events(item))
        else:
            raise LifecycleMetricsError(
                "Lifecycle event iterable items must be mappings or JSONL paths"
            )
    return events


def _containers(event: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    containers: list[Mapping[str, Any]] = [event]
    for key in ("context", "attributes", "data", "payload"):
        value = event.get(key)
        if isinstance(value, Mapping):
            containers.append(value)
            nested_context = value.get("context")
            if isinstance(nested_context, Mapping):
                containers.append(nested_context)
            nested_attributes = value.get("attributes")
            if isinstance(nested_attributes, Mapping):
                containers.append(nested_attributes)
    return containers


def _field(event: Mapping[str, Any], *names: str) -> Any:
    for container in _containers(event):
        for name in names:
            if name in container and container[name] is not None:
                return container[name]
    return None


def _string(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        cleaned = value.strip()
        return [cleaned] if cleaned else []
    if isinstance(value, (set, frozenset)):
        items: Iterable[Any] = sorted(value, key=str)
    elif isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        items = value
    else:
        return []
    out: list[str] = []
    for item in items:
        candidate = _string(item)
        if candidate is not None and candidate not in out:
            out.append(candidate)
    return out


def _json_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _json_value(value[key]) for key in sorted(value, key=str)}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_json_value(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _fingerprint(value: Any) -> tuple[str, Any] | None:
    if isinstance(value, Mapping):
        normalized = _json_value(value)
        if not normalized:
            return None
        key = json.dumps(normalized, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return key, normalized
    cleaned = _string(value)
    if cleaned is None:
        return None
    return cleaned, cleaned


def _system_fingerprint_complete(value: Any) -> bool:
    if not isinstance(value, Mapping):
        return False
    missing_markers = {"", "unknown", "none", "null", "unavailable"}

    def has_value(*names: str) -> bool:
        for name in names:
            candidate = value.get(name)
            if isinstance(candidate, str) and candidate.strip().casefold() not in missing_markers:
                return True
        return False

    return all(
        (
            has_value("code_commit", "commit", "system_commit"),
            has_value("prompt_hash", "prompt_config_hash", "prompts"),
            has_value("config_hash", "configuration_hash"),
            has_value("policy_hash", "policy"),
            has_value("model", "models"),
            has_value("provider", "providers", "agent", "agents"),
            has_value("score_version", "automation_score_version"),
        )
    )


def _event_type(event: Mapping[str, Any]) -> str | None:
    raw = _string(_field(event, "type", "event_type", "event_kind"))
    if raw is None:
        return None
    normalized = raw.lower().replace("_", ".").replace("-", ".")
    while ".." in normalized:
        normalized = normalized.replace("..", ".")
    if normalized in _CANONICAL_EVENT_TYPES:
        return normalized
    return _EVENT_TYPE_ALIASES.get(normalized)


def _case_id(event: Mapping[str, Any]) -> str | None:
    return _string(
        _field(
            event,
            "case_lifecycle_id",
            "case_id",
            "lifecycle_id",
        )
    )


def _beneficiary_case_ids(event: Mapping[str, Any]) -> list[str]:
    return _strings(
        _field(
            event,
            "beneficiary_case_lifecycle_ids",
            "beneficiary_case_ids",
            "beneficiaries",
        )
    )


def _normalize_label(value: str) -> str:
    normalized = value.strip().lower().replace("_", ".").replace("-", ".")
    normalized = ".".join(part for part in normalized.split(".") if part)
    return normalized


def _stage_milestone(value: Any) -> str | None:
    cleaned = _string(value)
    if cleaned is None:
        if isinstance(value, int) and 1 <= value <= 6:
            cleaned = str(value)
        else:
            return None
    normalized = _normalize_label(cleaned)
    if normalized.isdigit() and 1 <= int(normalized) <= 6:
        normalized = f"stage{normalized}"
    return _MILESTONE_ALIASES.get(normalized)


def _event_milestone(event: Mapping[str, Any], event_type: str) -> str | None:
    if event_type == "lifecycle.opened":
        return "origin"
    if event_type == "disposition.verified":
        return "disposition.verified"
    if event_type == "delivery.completed":
        return "delivery.completed"
    explicit = _stage_milestone(_field(event, "milestone_id", "milestone"))
    if explicit is not None:
        return explicit
    if event_type == "stage.completed":
        return _stage_milestone(_field(event, "stage", "stage_id", "stage_name"))
    return None


def _parse_timestamp(value: Any) -> datetime | None:
    raw = _string(value)
    if raw is None:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _timestamp(event: Mapping[str, Any], event_type: str | None = None) -> datetime | None:
    if event_type is not None and (
        event_type.endswith("completed")
        or event_type.endswith("resolved")
        or event_type.endswith("verified")
        or event_type in {"lifecycle.closed", "disposition.reached"}
    ):
        terminal = _parse_timestamp(_field(event, "ended_at", "completed_at", "resolved_at"))
        if terminal is not None:
            return terminal
    if event_type is not None and (
        event_type.endswith("started")
        or event_type.endswith("occurred")
        or event_type == "lifecycle.opened"
    ):
        initial = _parse_timestamp(_field(event, "started_at", "opened_at", "occurred_at"))
        if initial is not None:
            return initial
    return _parse_timestamp(
        _field(
            event,
            "ts",
            "timestamp",
            "timestamp_utc",
            "occurred_at",
            "ended_at",
            "completed_at",
            "started_at",
        )
    )


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number) or number < 0:
        return None
    return number


def _integer(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number) or number < 0 or not number.is_integer():
        return None
    return int(number)


def _token_mapping(event: Mapping[str, Any], *field_names: str) -> Mapping[str, Any] | None:
    raw = _field(event, *field_names)
    if isinstance(raw, Mapping):
        combined = raw.get("combined_token_dimensions")
        if isinstance(combined, Mapping):
            return combined
        dimensions = raw.get("dimensions")
        if isinstance(dimensions, Mapping):
            return dimensions
        usage = raw.get("usage")
        if isinstance(usage, Mapping):
            return usage
        return raw
    return None


def _extract_tokens(event: Mapping[str, Any]) -> tuple[dict[str, int], bool, list[str]]:
    issues: list[str] = []
    token_map = _token_mapping(event, "token_usage", "tokens", "token_receipt")
    scalar_total = _integer(_field(event, "total_tokens", "token_count"))
    explicit = token_map is not None or scalar_total is not None
    values: dict[str, int] = {}
    if token_map is not None:
        aliases = {
            "total_tokens": ("total_tokens", "total", "combined_total_tokens"),
            "input_tokens": ("input_tokens", "input"),
            "cached_input_tokens": ("cached_input_tokens", "cached_input", "cached"),
            "uncached_input_tokens": ("uncached_input_tokens", "uncached_input", "uncached"),
            "output_tokens": ("output_tokens", "output"),
            "reasoning_output_tokens": (
                "reasoning_output_tokens",
                "reasoning_tokens",
                "reasoning_output",
            ),
        }
        for dimension, names in aliases.items():
            for name in names:
                parsed = _integer(token_map.get(name))
                if parsed is not None:
                    values[dimension] = parsed
                    break
    if scalar_total is not None:
        existing = values.get("total_tokens")
        if existing is not None and existing != scalar_total:
            issues.append("token_total_conflict")
        values["total_tokens"] = scalar_total

    input_tokens = values.get("input_tokens")
    output_tokens = values.get("output_tokens")
    total_tokens = values.get("total_tokens")
    if total_tokens is None and input_tokens is not None and output_tokens is not None:
        values["total_tokens"] = input_tokens + output_tokens
        total_tokens = values["total_tokens"]
    if (
        total_tokens is not None
        and input_tokens is not None
        and output_tokens is not None
        and total_tokens != input_tokens + output_tokens
    ):
        issues.append("token_dimensions_do_not_reconcile")
    cached = values.get("cached_input_tokens")
    uncached = values.get("uncached_input_tokens")
    if input_tokens is not None and cached is not None:
        if cached > input_tokens:
            issues.append("cached_input_exceeds_input")
        elif uncached is None:
            values["uncached_input_tokens"] = input_tokens - cached
    if input_tokens is not None and cached is not None and uncached is not None:
        if cached + uncached != input_tokens:
            issues.append("input_cache_dimensions_do_not_reconcile")
    return values, explicit, issues


def _empty_tokens() -> dict[str, int]:
    return {dimension: 0 for dimension in TOKEN_DIMENSIONS}


def _sum_token_maps(items: Iterable[Mapping[str, int]]) -> dict[str, int]:
    total = _empty_tokens()
    for item in items:
        for dimension in TOKEN_DIMENSIONS:
            total[dimension] += int(item.get(dimension, 0))
    return total


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat().replace("+00:00", "Z")


def _new_case(case_id: str) -> dict[str, Any]:
    return {
        "case_id": case_id,
        "stable_case_ids": set(),
        "lifecycle_ids": set(),
        "cycle_ids": set(),
        "system_fingerprints": {},
        "opened_at": [],
        "closed_at": [],
        "atom_at": [],
        "admission_at": [],
        "lineage_at": [],
        "pr_created_at": [],
        "outcome_verified_at": [],
        "origin_telemetry_known": False,
        "lifecycle_opened_event_count": 0,
        "case_cohort_eligible": None,
        "raw_dispositions": set(),
        "milestones": {},
        "milestone_manual": defaultdict(list),
        "dispositions_reached": [],
        "dispositions_verified": [],
        "direct_work_unit_ids": set(),
        "inclusive_work_unit_ids": set(),
        "all_in_work_unit_ids": set(),
        "errors": {},
        "attested_self_healed_cluster_ids": set(),
        "interventions": {},
        "manual_actions": {},
        "manual_action_telemetry_complete": None,
        "issues": [],
        "event_count": 0,
    }


def _new_work_unit(work_unit_id: str) -> dict[str, Any]:
    return {
        "work_unit_id": work_unit_id,
        "shared_work_pool_ids": set(),
        "owner_case_ids": set(),
        "beneficiary_case_ids": set(),
        "dependency_ids": set(),
        "all_in_dependency_ids": set(),
        "scope": None,
        "stage": None,
        "token_scope": None,
        "actor_types": set(),
        "manual": None,
        "tokens": None,
        "tokens_explicit": False,
        "cost_unknown": False,
        "cost_unknown_reasons": set(),
        "resource_time_unknown": False,
        "resource_time_unknown_reasons": set(),
        "active_seconds": None,
        "machine_wait_seconds": None,
        "external_wait_seconds": None,
        "wait_seconds_by_category": None,
        "avoidable": None,
        "avoidable_tokens": None,
        "avoidable_active_seconds": None,
        "avoidable_machine_wait_seconds": None,
        "avoidable_external_wait_seconds": None,
        "avoidable_wait_seconds_by_category": None,
        "started_at": None,
        "completed_at": None,
        "token_receipt_paths": set(),
        "event_types": set(),
        "source_event_count": 0,
    }


def _issue(
    issues: list[dict[str, Any]],
    code: str,
    *,
    case_id: str | None = None,
    work_unit_id: str | None = None,
    detail: str | None = None,
) -> None:
    item: dict[str, Any] = {"code": code}
    if case_id is not None:
        item["case_id"] = case_id
    if work_unit_id is not None:
        item["work_unit_id"] = work_unit_id
    if detail is not None:
        item["detail"] = detail
    if item not in issues:
        issues.append(item)


def _duration_seconds(event: Mapping[str, Any]) -> float | None:
    value = _number(
        _field(event, "active_seconds", "duration_seconds", "elapsed_seconds", "time_seconds")
    )
    if value is not None:
        return value
    timing = _field(event, "timing")
    if isinstance(timing, Mapping):
        return _number(
            timing.get("elapsed_seconds", timing.get("duration_seconds", timing.get("seconds")))
        )
    return None


def _legacy_exec_active_time_unattributable(event: Mapping[str, Any]) -> bool:
    """Identify pre-v2 exec boundaries whose wall time was mislabeled active."""

    if _field(event, "telemetry_exec_timing_version") is not None:
        return False
    if _string(_field(event, "redacted_command")) is None or _string(
        _field(event, "command_fingerprint")
    ) is None:
        return False
    origin = _string(_field(event, "origin"))
    if origin is not None and _normalize_label(origin) == "automatic":
        return False
    return _canonical_actor(event) in {"human", "supervisor", "external", "unknown"}


def _wait_seconds(event: Mapping[str, Any], field: str) -> float | None:
    value = _number(_field(event, field))
    if value is not None:
        return value
    timing = _field(event, "timing")
    if isinstance(timing, Mapping):
        return _number(timing.get(field))
    return None


def _wait_breakdown(
    event: Mapping[str, Any], *, avoidable: bool = False
) -> tuple[dict[str, float] | None, list[str]]:
    """Return a non-overlapping wait breakdown while preserving generic waits.

    Producers may emit only the canonical machine/external totals, or may additionally
    emit named categories.  Named values consume the generic totals; any remaining
    machine wait is unknown and any remaining external wait stays external.
    """

    prefix = "avoidable_" if avoidable else ""
    values = {category: 0.0 for category in WAIT_CATEGORIES}
    found = False
    issues: list[str] = []

    mapping_names = (
        f"{prefix}wait_seconds_by_category",
        f"{prefix}wait_breakdown",
        f"{prefix}wait_categories",
    )
    raw_mapping = _field(event, *mapping_names)
    if isinstance(raw_mapping, Mapping):
        for raw_category, raw_value in raw_mapping.items():
            category = _canonical_wait_category(raw_category)
            value = _number(raw_value)
            if category is not None and value is not None:
                values[category] += value
                found = True

    for category in WAIT_CATEGORIES:
        # external_wait_seconds is the canonical generic external total.  It is
        # allocated below so it is not counted twice as a named category.
        if category == "external":
            names = (f"{prefix}categorized_external_wait_seconds",)
        else:
            names = (f"{prefix}{category}_wait_seconds",)
        value = _number(_field(event, *names))
        if value is not None:
            values[category] += value
            found = True

    raw_category = _field(event, f"{prefix}wait_category")
    category = _canonical_wait_category(raw_category)
    categorized_seconds = _number(
        _field(event, f"{prefix}categorized_wait_seconds", f"{prefix}wait_seconds")
    )
    if category is not None and categorized_seconds is not None:
        values[category] += categorized_seconds
        found = True

    if avoidable:
        machine = _number(_field(event, "avoidable_machine_wait_seconds"))
        external = _number(_field(event, "avoidable_external_wait_seconds"))
    else:
        machine = _wait_seconds(event, "machine_wait_seconds")
        external = _wait_seconds(event, "external_wait_seconds")

    if machine is None and external is None and not found:
        return None, issues

    machine_total = machine or 0.0
    external_total = external or 0.0
    external_named = sum(
        values[name] for name in ("provider", "ci", "approval", "external")
    )
    if external is not None and external_named < external_total:
        values["external"] += external_total - external_named
    elif external is not None and external_named > external_total and not math.isclose(
        external_named, external_total
    ):
        issues.append("wait_category_external_total_conflict")

    generic_total = machine_total + external_total
    categorized_total = sum(values.values())
    if (machine is not None or external is not None) and categorized_total < generic_total:
        values["unknown"] += generic_total - categorized_total
    elif (machine is not None or external is not None) and categorized_total > generic_total:
        if not math.isclose(categorized_total, generic_total):
            issues.append("wait_category_total_conflict")

    return values, issues


def _event_interval(
    event: Mapping[str, Any], event_type: str
) -> tuple[datetime | None, datetime | None]:
    start_at = _parse_timestamp(_field(event, "started_at", "start_at"))
    end_at = _parse_timestamp(_field(event, "ended_at", "completed_at", "end_at"))
    timestamp = _timestamp(event, event_type)
    if start_at is None and event_type.endswith("started"):
        start_at = timestamp
    if end_at is None and event_type.endswith("completed"):
        end_at = timestamp
    return start_at, end_at


def _timestamp_values(value: Any) -> list[datetime]:
    if isinstance(value, str):
        parsed = _parse_timestamp(value)
        return [parsed] if parsed is not None else []
    if not isinstance(value, Sequence) or isinstance(value, (bytes, bytearray)):
        return []
    parsed_values: list[datetime] = []
    for item in value:
        parsed = _parse_timestamp(item)
        if parsed is not None:
            parsed_values.append(parsed)
    return parsed_values


def _bool_field(event: Mapping[str, Any], *names: str) -> bool | None:
    value = _field(event, *names)
    return value if isinstance(value, bool) else None


def _work_unit_id(event: Mapping[str, Any], event_type: str, index: int) -> tuple[str, bool]:
    explicit = _string(_field(event, "work_unit_id", "work_id"))
    if explicit is not None:
        return explicit, True
    candidates: tuple[str, ...]
    prefix: str
    if event_type.startswith("model.invocation"):
        candidates = ("model_invocation_id", "invocation_id", "attempt_id", "event_id")
        prefix = "model"
    elif event_type.startswith("intervention"):
        candidates = ("intervention_id", "event_id")
        prefix = "intervention"
    elif event_type.startswith("action") or event_type.startswith("delivery"):
        candidates = ("action_id", "delivery_id", "event_id")
        prefix = "action"
    else:
        candidates = ("shared_work_id", "shared_work_pool_id", "event_id")
        prefix = "event"
    stable = _string(_field(event, *candidates))
    if stable is not None:
        return f"{prefix}:{stable}", True
    return f"synthetic:event:{index:08d}", False


def _work_scope(
    event: Mapping[str, Any],
    event_type: str,
    *,
    owner_case_id: str | None,
    beneficiary_case_ids: Sequence[str],
) -> str:
    raw = _string(
        _field(event, "accounting_scope", "cost_scope", "attribution_scope", "scope")
    )
    if raw is not None:
        normalized = _normalize_label(raw)
        aliases = {
            "direct": "direct",
            "case": "direct",
            "case.direct": "direct",
            "shared": "shared",
            "shared.pipeline": "shared",
            "dependency": "dependency",
            "prerequisite": "dependency",
            "support": "support",
            "campaign": "support",
            "overhead": "support",
            "all.in": "support",
            "outside": "support",
        }
        if normalized in aliases:
            return aliases[normalized]
    if event_type == "work.reused":
        return "shared"
    if event_type.startswith("intervention") or event_type.startswith("action"):
        return "support"
    if event_type.startswith("error") and _string(
        _field(event, "telemetry_exec_attempt_group_id")
    ):
        # telemetry-exec error events describe the same concrete support action
        # as their action.started/action.completed parents. Older streams did
        # not copy the explicit accounting scope onto the error event.
        return "support"
    if owner_case_id is None or len(beneficiary_case_ids) > 1:
        return "shared"
    return "direct"


def _canonical_wait_category(value: Any) -> str | None:
    cleaned = _string(value)
    if cleaned is None:
        return None
    normalized = _normalize_label(cleaned)
    aliases = {
        "queue": "queue",
        "queued": "queue",
        "provider": "provider",
        "model.provider": "provider",
        "ci": "ci",
        "continuous.integration": "ci",
        "approval": "approval",
        "review.approval": "approval",
        "external": "external",
        "external.service": "external",
        "unknown": "unknown",
        "unclassified": "unknown",
    }
    return aliases.get(normalized)


def _canonical_token_scope(event: Mapping[str, Any]) -> str | None:
    raw = _string(_field(event, "token_scope", "effort_scope", "work_scope"))
    if raw is not None:
        normalized = _normalize_label(raw)
        aliases = {
            "qualification": "qualification",
            "pipeline": "qualification",
            "implementation": "implementation",
            "verification": "implementation",
            "review": "implementation",
            "delivery": "implementation",
            "supervisor": "supervising_agent",
            "supervising.agent": "supervising_agent",
            "outside.platform": "outside_platform",
            "external": "outside_platform",
            "measurement": "outside_platform",
            "unclassified": "unclassified",
            "unknown": "unclassified",
        }
        canonical = aliases.get(normalized)
        if canonical is not None:
            return canonical

    if _string(_field(event, "telemetry_exec_attempt_group_id")) is not None:
        # Legacy telemetry-exec error events omitted work_scope. Do not infer a
        # conflicting scope from the actor; the paired action event remains the
        # authoritative source for this concrete work unit.
        return None

    actor = _normalize_label(
        _string(_field(event, "actor_type", "actor", "origin")) or ""
    )
    if actor in {"supervisor", "supervising.agent"}:
        return "supervising_agent"
    if actor in {"human", "operator", "external.service", "unknown.external"}:
        return "outside_platform"
    stage = _normalize_label(_string(_field(event, "stage")) or "")
    if stage in {"implementation", "verification", "review", "delivery"}:
        return "implementation"
    return None


def _work_stage(event: Mapping[str, Any], event_type: str) -> str | None:
    raw = _field(event, "stage", "stage_id", "stage_name")
    milestone = _stage_milestone(raw)
    if milestone is not None:
        return milestone.removesuffix(".completed")
    cleaned = _string(raw)
    if cleaned is not None:
        return _normalize_label(cleaned)
    explicit_milestone = _event_milestone(event, event_type)
    if explicit_milestone is not None:
        return explicit_milestone.removesuffix(".completed")
    if event_type.startswith("delivery"):
        return "delivery"
    return None


def _canonical_actor(event: Mapping[str, Any]) -> str | None:
    raw = _string(_field(event, "actor_type", "actor"))
    if raw is None:
        return None
    normalized = _normalize_label(raw)
    aliases = {
        "supervisor": "supervisor",
        "supervising.agent": "supervisor",
        "human": "human",
        "operator": "human",
        "controller": "controller",
        "model": "model",
        "external.service": "external",
        "unknown.external": "external",
        "unknown": "unknown",
    }
    return aliases.get(normalized, normalized.replace(".", "_"))


def _resolution_category(value: Any) -> str | None:
    cleaned = _string(value)
    if cleaned is None:
        return None
    normalized = _normalize_label(cleaned)
    aliases = {
        "self.healed.same.author": "self_healed_same_author",
        "same.author": "self_healed_same_author",
        "self.heal": "self_healed_same_author",
        "self.healed": "self_healed_same_author",
        "self.healed.controller": "self_healed_controller",
        "controller": "self_healed_controller",
        "automatic": "self_healed_controller",
        "automatic.self.correction": "self_healed_controller",
        "resolved.supervisor": "resolved_supervisor",
        "supervisor": "resolved_supervisor",
        "supervisor.intervention": "resolved_supervisor",
        "resolved.human": "resolved_human",
        "manual": "resolved_human",
        "human": "resolved_human",
        "resolved.external": "resolved_external",
        "external": "resolved_external",
        "tolerated.nonblocking": "tolerated_nonblocking",
        "tolerated": "tolerated_nonblocking",
        "unresolved.terminal": "unresolved_terminal",
        "unresolved": "unresolved_terminal",
        "open": "open",
    }
    return aliases.get(normalized)


def _canonical_disposition(value: Any) -> tuple[str | None, str | None]:
    raw = _string(value)
    if raw is None:
        return None, None
    canonical = _DISPOSITION_ALIASES.get(_normalize_label(raw))
    return canonical, raw


def _event_case_ids(event: Mapping[str, Any]) -> list[str]:
    case_ids: list[str] = []
    owner = _case_id(event)
    if owner is not None:
        case_ids.append(owner)
    for beneficiary in _beneficiary_case_ids(event):
        if beneficiary not in case_ids:
            case_ids.append(beneficiary)
    return case_ids


def _record_case_context(case: dict[str, Any], event: Mapping[str, Any]) -> None:
    lifecycle_id = _string(_field(event, "case_lifecycle_id", "lifecycle_id"))
    if lifecycle_id is not None:
        case["lifecycle_ids"].add(lifecycle_id)
    stable_case_id = _string(_field(event, "case_id"))
    if stable_case_id is not None:
        case["stable_case_ids"].add(stable_case_id)
    cycle_id = _string(_field(event, "cycle_id"))
    if cycle_id is not None:
        case["cycle_ids"].add(cycle_id)
    fingerprint = _fingerprint(
        _field(
            event,
            "system_fingerprint",
            "pipeline_system_fingerprint",
            "controller_fingerprint",
        )
    )
    if fingerprint is not None:
        key, value = fingerprint
        case["system_fingerprints"][key] = value
    origin_known = _bool_field(event, "origin_telemetry_known")
    origin_ids = _strings(
        _field(
            event,
            "origin_id",
            "origin_ids",
            "atom_id",
            "atom_ids",
            "raw_atom_ids",
            "source_atom_ids",
        )
    )
    if origin_known is True or origin_ids:
        case["origin_telemetry_known"] = True


def _merge_work_cost(
    work: dict[str, Any],
    event: Mapping[str, Any],
    issues: list[dict[str, Any]],
) -> None:
    work_unit_id = str(work["work_unit_id"])
    cost_unknown = _bool_field(event, "cost_unknown", "prior_cost_unknown")
    if cost_unknown is True:
        work["cost_unknown"] = True
        reason = _string(_field(event, "cost_unknown_reason", "prior_cost_unknown_reason"))
        if reason is not None:
            work["cost_unknown_reasons"].add(reason)
    legacy_exec_time_unknown = _legacy_exec_active_time_unattributable(event)
    if legacy_exec_time_unknown:
        work["resource_time_unknown"] = True
        work["resource_time_unknown_reasons"].add(
            "legacy_manual_exec_subprocess_wall_unattributable"
        )
    resource_time_unknown = _bool_field(event, "resource_time_unknown")
    if resource_time_unknown is True:
        work["resource_time_unknown"] = True
        reason = _string(_field(event, "resource_time_unknown_reason"))
        if reason is not None:
            work["resource_time_unknown_reasons"].add(reason)
    tokens, explicit_tokens, token_issues = _extract_tokens(event)
    for code in token_issues:
        _issue(issues, code, work_unit_id=work_unit_id)
    if explicit_tokens:
        previous = work["tokens"]
        if previous is not None and previous != tokens:
            _issue(issues, "work_unit_token_conflict", work_unit_id=work_unit_id)
        elif previous is None:
            work["tokens"] = tokens
            work["tokens_explicit"] = True

    duration = None if legacy_exec_time_unknown else _duration_seconds(event)
    if duration is not None:
        previous_duration = work["active_seconds"]
        if previous_duration is not None and not math.isclose(previous_duration, duration):
            _issue(issues, "work_unit_active_time_conflict", work_unit_id=work_unit_id)
        elif previous_duration is None:
            work["active_seconds"] = duration
    for wait_field in ("machine_wait_seconds", "external_wait_seconds"):
        wait_value = _wait_seconds(event, wait_field)
        if wait_value is None:
            continue
        previous_wait = work[wait_field]
        if previous_wait is not None and not math.isclose(previous_wait, wait_value):
            _issue(issues, f"work_unit_{wait_field}_conflict", work_unit_id=work_unit_id)
        elif previous_wait is None:
            work[wait_field] = wait_value

    wait_breakdown, wait_issues = _wait_breakdown(event)
    for code in wait_issues:
        _issue(issues, code, work_unit_id=work_unit_id)
    if wait_breakdown is not None:
        previous_breakdown = work["wait_seconds_by_category"]
        if previous_breakdown is not None and previous_breakdown != wait_breakdown:
            _issue(issues, "work_unit_wait_breakdown_conflict", work_unit_id=work_unit_id)
        elif previous_breakdown is None:
            work["wait_seconds_by_category"] = wait_breakdown
        if work["machine_wait_seconds"] is None:
            work["machine_wait_seconds"] = (
                wait_breakdown["queue"] + wait_breakdown["unknown"]
            )
        if work["external_wait_seconds"] is None:
            work["external_wait_seconds"] = sum(
                wait_breakdown[category]
                for category in ("provider", "ci", "approval", "external")
            )

    avoidable = _bool_field(event, "avoidable", "avoidable_work")
    if avoidable is not None:
        if work["avoidable"] is not None and work["avoidable"] != avoidable:
            _issue(issues, "work_unit_avoidable_conflict", work_unit_id=work_unit_id)
        else:
            work["avoidable"] = avoidable

    avoidable_map = _token_mapping(event, "avoidable_token_usage", "avoidable_tokens")
    if avoidable_map is not None:
        wrapper = {"token_usage": dict(avoidable_map)}
        avoidable_tokens, _, avoidable_issues = _extract_tokens(wrapper)
        for code in avoidable_issues:
            _issue(issues, f"avoidable_{code}", work_unit_id=work_unit_id)
        if work["avoidable_tokens"] is not None and work["avoidable_tokens"] != avoidable_tokens:
            _issue(issues, "work_unit_avoidable_token_conflict", work_unit_id=work_unit_id)
        else:
            work["avoidable_tokens"] = avoidable_tokens
    avoidable_time_aliases = {
        "avoidable_active_seconds": (
            "avoidable_active_seconds",
            "avoidable_duration_seconds",
            "avoidable_time_seconds",
        ),
        "avoidable_machine_wait_seconds": ("avoidable_machine_wait_seconds",),
        "avoidable_external_wait_seconds": ("avoidable_external_wait_seconds",),
    }
    for target, aliases in avoidable_time_aliases.items():
        value = _number(_field(event, *aliases))
        if value is None:
            continue
        previous = work[target]
        if previous is not None and not math.isclose(previous, value):
            _issue(issues, f"work_unit_{target}_conflict", work_unit_id=work_unit_id)
        else:
            work[target] = value

    avoidable_wait_breakdown, avoidable_wait_issues = _wait_breakdown(
        event, avoidable=True
    )
    for code in avoidable_wait_issues:
        _issue(issues, f"avoidable_{code}", work_unit_id=work_unit_id)
    if avoidable_wait_breakdown is not None:
        previous_avoidable_breakdown = work["avoidable_wait_seconds_by_category"]
        if (
            previous_avoidable_breakdown is not None
            and previous_avoidable_breakdown != avoidable_wait_breakdown
        ):
            _issue(
                issues,
                "work_unit_avoidable_wait_breakdown_conflict",
                work_unit_id=work_unit_id,
            )
        elif previous_avoidable_breakdown is None:
            work["avoidable_wait_seconds_by_category"] = avoidable_wait_breakdown
        if work["avoidable_machine_wait_seconds"] is None:
            work["avoidable_machine_wait_seconds"] = (
                avoidable_wait_breakdown["queue"]
                + avoidable_wait_breakdown["unknown"]
            )
        if work["avoidable_external_wait_seconds"] is None:
            work["avoidable_external_wait_seconds"] = sum(
                avoidable_wait_breakdown[category]
                for category in ("provider", "ci", "approval", "external")
            )


def _action_id(event: Mapping[str, Any], event_type: str, index: int) -> tuple[str, bool]:
    # A lifecycle event always has its own event_id, while paired start/completion
    # events share the action identity in their attributes. Resolve action-scoped
    # identities across every container before falling back to the event identity;
    # otherwise one concrete manual action is counted once per lifecycle event.
    stable = _string(_field(event, "intervention_id", "action_id", "delivery_id"))
    if stable is not None:
        return stable, True
    stable = _string(_field(event, "event_id"))
    if stable is not None:
        return stable, True
    return f"synthetic:{event_type}:{index:08d}", False


def _legacy_action_work_unit_aliases(
    events: Sequence[Mapping[str, Any]],
) -> tuple[dict[int, str], dict[str, tuple[str, ...]], list[dict[str, Any]]]:
    """Recover concrete action identities from legacy reused work-unit IDs.

    Early telemetry CLI callers sometimes supplied one descriptive ``work_unit_id``
    for several separate actions. The stream still retained a unique ``action_id``
    (or ``intervention_id``), so that ambiguity is recoverable without estimating
    cost. A non-action claimant remains genuinely ambiguous and is not normalized.
    """

    event_bindings: dict[int, tuple[str, str, str]] = {}
    bindings_by_source: dict[str, set[tuple[str, str]]] = defaultdict(set)
    explicit_work_ids: set[str] = set()
    non_action_claims: set[str] = set()

    for index, event in enumerate(events):
        source_work_id = _string(_field(event, "work_unit_id", "work_id"))
        if source_work_id is None:
            continue
        explicit_work_ids.add(source_work_id)
        event_type = _event_type(event)
        identity_type: str | None = None
        if event_type is not None and event_type.startswith("action"):
            identity_type = "action"
        elif event_type is not None and event_type.startswith("intervention"):
            identity_type = "intervention"
        if identity_type is None:
            non_action_claims.add(source_work_id)
            continue
        assert event_type is not None
        action_id, explicit_action_id = _action_id(event, event_type, index)
        if not explicit_action_id:
            continue
        binding = (identity_type, action_id)
        event_bindings[index] = (source_work_id, *binding)
        bindings_by_source[source_work_id].add(binding)

    aliases_by_event: dict[int, str] = {}
    aliases_by_source: dict[str, tuple[str, ...]] = {}
    migrations: list[dict[str, Any]] = []
    for source_work_id in sorted(bindings_by_source):
        bindings = sorted(bindings_by_source[source_work_id])
        if len(bindings) < 2 or source_work_id in non_action_claims:
            continue
        concrete_bindings = [
            {
                "identity_type": identity_type,
                "action_id": action_id,
                "work_unit_id": (
                    f"{source_work_id}::legacy-concrete::{identity_type}:{action_id}"
                ),
            }
            for identity_type, action_id in bindings
        ]
        concrete_ids = [item["work_unit_id"] for item in concrete_bindings]
        if len(set(concrete_ids)) != len(concrete_ids) or any(
            concrete_id in explicit_work_ids for concrete_id in concrete_ids
        ):
            # Never overwrite a producer-owned identity. The original events will
            # instead retain their reconciliation conflicts.
            continue
        alias_for_binding = {
            (item["identity_type"], item["action_id"]): item["work_unit_id"]
            for item in concrete_bindings
        }
        aliases_by_source[source_work_id] = tuple(sorted(concrete_ids))
        source_event_count = 0
        for index, (event_source, identity_type, action_id) in event_bindings.items():
            if event_source != source_work_id:
                continue
            aliases_by_event[index] = alias_for_binding[(identity_type, action_id)]
            source_event_count += 1
        migrations.append(
            {
                "source_work_unit_id": source_work_id,
                "source_event_count": source_event_count,
                "concrete_bindings": concrete_bindings,
            }
        )
    return aliases_by_event, aliases_by_source, migrations


def _expand_legacy_work_unit_references(
    value: Any,
    aliases_by_source: Mapping[str, Sequence[str]],
    *,
    current_work_unit_id: str | None = None,
) -> list[str]:
    expanded: list[str] = []
    seen: set[str] = set()
    for work_unit_id in _strings(value):
        replacements = aliases_by_source.get(work_unit_id, (work_unit_id,))
        for replacement in replacements:
            if replacement == current_work_unit_id or replacement in seen:
                continue
            seen.add(replacement)
            expanded.append(replacement)
    return expanded


def _new_error_cluster(cluster_id: str) -> dict[str, Any]:
    return {
        "error_cluster_id": cluster_id,
        "occurred_at": None,
        "resolved_at": None,
        "resolution_category": None,
        "event_count": 0,
        "occurrence_count": 0,
        "resolution_event_count": 0,
        "resolution_work_unit_ids": set(),
        "resolution_cost_attribution_complete": False,
        "resolution_tokens": None,
        "intervention_ids": set(),
        "manual_action_ids": set(),
        "resolution_evidence_unknown": False,
        "resolution_timing_unknown": False,
    }


def _mark_resolution(
    case: dict[str, Any],
    cluster_id: str,
    category: str,
    *,
    timestamp: datetime | None,
) -> None:
    cluster = case["errors"].setdefault(cluster_id, _new_error_cluster(cluster_id))
    previous = cluster.get("resolution_category")
    if previous is not None and previous != category:
        _issue(
            case["issues"],
            "error_resolution_conflict",
            case_id=case["case_id"],
            detail=f"{cluster_id}:{previous!s}->{category}",
        )
    else:
        cluster["resolution_category"] = category
        if timestamp is not None:
            prior_resolved = cluster.get("resolved_at")
            if not isinstance(prior_resolved, datetime) or timestamp < prior_resolved:
                cluster["resolved_at"] = timestamp


def _collect_events(
    events: list[dict[str, Any]],
    *,
    work_unit_aliases_by_event: Mapping[int, str] | None = None,
    work_unit_aliases_by_source: Mapping[str, Sequence[str]] | None = None,
) -> tuple[
    dict[str, dict[str, Any]], dict[str, dict[str, Any]], list[dict[str, Any]], int
]:
    aliases_by_event = work_unit_aliases_by_event or {}
    aliases_by_source = work_unit_aliases_by_source or {}
    cases: dict[str, dict[str, Any]] = {}
    work_units: dict[str, dict[str, Any]] = {}
    global_issues: list[dict[str, Any]] = []
    ignored_event_count = 0

    for index, event in enumerate(events):
        event_type = _event_type(event)
        if event_type is None:
            ignored_event_count += 1
            continue
        timestamp = _timestamp(event, event_type)
        case_ids = _event_case_ids(event)
        for case_id in case_ids:
            case = cases.setdefault(case_id, _new_case(case_id))
            case["event_count"] += 1
            _record_case_context(case, event)
            case["attested_self_healed_cluster_ids"].update(
                _strings(_field(event, "attested_self_healed_cluster_ids"))
            )
            manual_action_telemetry_complete = _bool_field(
                event, "manual_action_telemetry_complete"
            )
            if manual_action_telemetry_complete is False:
                # A producer's explicit incompleteness declaration dominates any
                # other event that only describes the actions it happened to see.
                case["manual_action_telemetry_complete"] = False
            elif (
                manual_action_telemetry_complete is True
                and case["manual_action_telemetry_complete"] is None
            ):
                case["manual_action_telemetry_complete"] = True
            cohort_eligible = _bool_field(
                event, "case_cohort_eligible", "cohort_eligible"
            )
            lifecycle_kind = _string(_field(event, "lifecycle_kind"))
            if cohort_eligible is False:
                case["case_cohort_eligible"] = False
            elif (
                cohort_eligible is True
                or (
                    lifecycle_kind is not None
                    and _normalize_label(lifecycle_kind) == "case"
                )
            ) and case["case_cohort_eligible"] is None:
                case["case_cohort_eligible"] = True
            atom_timestamps = _timestamp_values(
                    _field(
                        event,
                        "atom_created_at",
                        "raw_atom_created_at",
                        "origin_observed_at",
                        "atom_timestamps",
                    )
                )
            case["atom_at"].extend(atom_timestamps)
            if atom_timestamps:
                case["origin_telemetry_known"] = True
            case["admission_at"].extend(
                _timestamp_values(_field(event, "admitted_at", "admission_at"))
            )
            case["lineage_at"].extend(
                _timestamp_values(
                    _field(event, "lineage_started_at", "lifecycle_started_at")
                )
            )
            case["pr_created_at"].extend(
                _timestamp_values(_field(event, "pr_created_at", "pull_request_created_at"))
            )
            case["outcome_verified_at"].extend(
                _timestamp_values(_field(event, "outcome_verified_at"))
            )

        owner_case_id = _case_id(event)
        milestone = _event_milestone(event, event_type)
        if milestone is not None:
            for case_id in case_ids:
                case = cases[case_id]
                milestone_times: list[datetime | None] = case["milestones"].setdefault(
                    milestone, []
                )
                milestone_times.append(timestamp)

        if event_type == "lifecycle.opened":
            for case_id in case_ids:
                cases[case_id]["lifecycle_opened_event_count"] += 1
                if cases[case_id]["case_cohort_eligible"] is None:
                    cases[case_id]["case_cohort_eligible"] = True
                cases[case_id]["opened_at"].append(timestamp)
                if timestamp is not None:
                    cases[case_id]["lineage_at"].append(timestamp)
        elif event_type == "lifecycle.closed":
            for case_id in case_ids:
                cases[case_id]["closed_at"].append(timestamp)
        elif event_type == "delivery.started":
            for case_id in case_ids:
                if timestamp is not None:
                    cases[case_id]["pr_created_at"].append(timestamp)
        elif event_type == "outcome.verified":
            for case_id in case_ids:
                if timestamp is not None:
                    cases[case_id]["outcome_verified_at"].append(timestamp)

        disposition, raw_disposition = _canonical_disposition(
            _field(event, "disposition", "final_disposition", "disposition_category")
        )
        if raw_disposition is not None:
            for case_id in case_ids:
                cases[case_id]["raw_dispositions"].add(raw_disposition)
        if raw_disposition is not None and disposition is None:
            for case_id in case_ids:
                _issue(
                    cases[case_id]["issues"],
                    "unsupported_disposition",
                    case_id=case_id,
                    detail=raw_disposition,
                )
            if event_type in {"disposition.verified", "lifecycle.closed"}:
                disposition = "failed_incomplete"
        if disposition is not None and event_type in {
            "disposition.reached",
            "disposition.verified",
            "lifecycle.closed",
            "delivery.completed",
        }:
            for case_id in case_ids:
                cases[case_id]["dispositions_reached"].append((disposition, timestamp))
                if event_type == "disposition.verified" or _bool_field(
                    event, "disposition_verified", "verified"
                ) is True:
                    cases[case_id]["dispositions_verified"].append((disposition, timestamp))
                    verified_times: list[datetime | None] = cases[case_id]["milestones"].setdefault(
                        "disposition.verified", []
                    )
                    if timestamp not in verified_times:
                        verified_times.append(timestamp)

        if event_type in {"error.occurred", "error.resolved"}:
            cluster_id = _string(_field(event, "error_cluster_id", "cluster_id", "error_id"))
            if cluster_id is None:
                cluster_id = f"synthetic:error:{index:08d}"
                for case_id in case_ids:
                    _issue(
                        cases[case_id]["issues"],
                        "error_cluster_id_missing",
                        case_id=case_id,
                    )
            for case_id in case_ids:
                case = cases[case_id]
                cluster = case["errors"].setdefault(
                    cluster_id, _new_error_cluster(cluster_id)
                )
                cluster["event_count"] += 1
                if event_type == "error.occurred":
                    cluster["occurrence_count"] += 1
                    prior_occurred = cluster.get("occurred_at")
                    if timestamp is not None and (
                        not isinstance(prior_occurred, datetime)
                        or timestamp < prior_occurred
                    ):
                        cluster["occurred_at"] = timestamp
                    if _bool_field(event, "resolution_evidence_unknown") is True:
                        cluster["resolution_evidence_unknown"] = True
                    if _bool_field(event, "resolution_timing_unknown") is True:
                        cluster["resolution_timing_unknown"] = True
                else:
                    cluster["resolution_event_count"] += 1
                    resolution_work_ids = _expand_legacy_work_unit_references(
                        _field(
                            event,
                            "resolution_work_unit_ids",
                            "attributed_work_unit_ids",
                        ),
                        aliases_by_source,
                    )
                    cluster["resolution_work_unit_ids"].update(resolution_work_ids)
                    attribution_complete = _bool_field(
                        event,
                        "resolution_cost_attribution_complete",
                        "resolution_attribution_complete",
                    )
                    if attribution_complete is True:
                        cluster["resolution_cost_attribution_complete"] = True
                    resolution_token_map = _token_mapping(
                        event, "resolution_token_usage", "resolution_tokens"
                    )
                    if resolution_token_map is not None:
                        resolution_tokens, _, token_issues = _extract_tokens(
                            {"token_usage": dict(resolution_token_map)}
                        )
                        for code in token_issues:
                            _issue(
                                case["issues"],
                                f"resolution_{code}",
                                case_id=case_id,
                                detail=cluster_id,
                            )
                        previous_tokens = cluster.get("resolution_tokens")
                        if previous_tokens is not None and previous_tokens != resolution_tokens:
                            _issue(
                                case["issues"],
                                "resolution_token_conflict",
                                case_id=case_id,
                                detail=cluster_id,
                            )
                        else:
                            cluster["resolution_tokens"] = resolution_tokens
                            cluster["resolution_cost_attribution_complete"] = True
                    raw_resolution = _field(
                        event,
                        "resolution_mode",
                        "resolution_category",
                        "resolution",
                        "resolved_by",
                    )
                    category = _resolution_category(raw_resolution)
                    if category is None:
                        if _string(raw_resolution) is not None:
                            _issue(
                                case["issues"],
                                "unsupported_error_resolution_mode",
                                case_id=case_id,
                                detail=str(raw_resolution),
                            )
                        category = "open"
                    if _bool_field(event, "resolution_timing_unknown") is True:
                        cluster["resolution_timing_unknown"] = True
                    _mark_resolution(case, cluster_id, category, timestamp=timestamp)

        if event_type.startswith("intervention") or event_type.startswith("action"):
            action_id, explicit_action_id = _action_id(event, event_type, index)
            is_intervention = event_type.startswith("intervention")
            manual = is_intervention or _bool_field(event, "manual", "is_manual") is True
            actor = _canonical_actor(event)
            if actor in {"human", "supervisor", "operator"}:
                manual = True
            explicit_avoidable = _bool_field(event, "avoidable", "avoidable_action")
            policy_mandated = _bool_field(event, "policy_mandated")
            avoidable = explicit_avoidable
            avoidability_conflict = False
            if policy_mandated is not None:
                policy_avoidable = not policy_mandated
                avoidability_conflict = (
                    explicit_avoidable is not None
                    and explicit_avoidable != policy_avoidable
                )
                avoidable = policy_avoidable
            elif explicit_avoidable is False:
                # v1 excludes only explicitly policy-mandated actions.
                avoidability_conflict = True
            for case_id in case_ids:
                case = cases[case_id]
                collection_name = "interventions" if is_intervention else "manual_actions"
                if not is_intervention and not manual:
                    continue
                collection = case[collection_name]
                item = collection.setdefault(
                    action_id,
                    {
                        "id": action_id,
                        "started_at": None,
                        "completed_at": None,
                        "milestone_id": milestone
                        or _stage_milestone(_field(event, "milestone_id", "stage")),
                        "avoidable": avoidable,
                        "actor": actor,
                        "action_family": _string(_field(event, "action_family")),
                        "operation": _string(_field(event, "operation")),
                        "interface": _string(_field(event, "interface")),
                        "work_scope": _canonical_token_scope(event),
                        "policy_mandated": policy_mandated,
                        "passive_observation": _bool_field(event, "passive_observation"),
                        "measurement_administration": _bool_field(
                            event, "measurement_administration"
                        ),
                        "required_for_progress": _bool_field(
                            event, "required_for_progress", "blocking_manual_action"
                        ),
                        "active_seconds": None,
                    },
                )
                if not explicit_action_id:
                    _issue(
                        case["issues"],
                        f"{collection_name[:-1]}_id_missing",
                        case_id=case_id,
                    )
                if avoidability_conflict:
                    _issue(
                        case["issues"],
                        "manual_action_avoidability_policy_conflict",
                        case_id=case_id,
                        detail=action_id,
                    )
                action_start, action_end = _event_interval(event, event_type)
                if action_start is not None:
                    item["started_at"] = action_start
                elif event_type.endswith("started"):
                    item["started_at"] = timestamp
                if action_end is not None:
                    item["completed_at"] = action_end
                elif event_type.endswith("completed"):
                    item["completed_at"] = timestamp
                explicit_active = (
                    None
                    if _legacy_exec_active_time_unattributable(event)
                    else _duration_seconds(event)
                )
                if explicit_active is not None:
                    item["active_seconds"] = explicit_active
                if avoidable is not None:
                    prior_avoidable = item.get("avoidable")
                    if prior_avoidable is not None and prior_avoidable != avoidable:
                        _issue(
                            case["issues"],
                            "manual_action_avoidable_conflict",
                            case_id=case_id,
                            detail=action_id,
                        )
                    item["avoidable"] = avoidable
                required_for_progress = _bool_field(
                    event, "required_for_progress", "blocking_manual_action"
                )
                if required_for_progress is not None:
                    item["required_for_progress"] = required_for_progress
                for bool_field in (
                    "policy_mandated",
                    "passive_observation",
                    "measurement_administration",
                ):
                    bool_value = _bool_field(event, bool_field)
                    if bool_value is not None:
                        item[bool_field] = bool_value
                item_milestone = item.get("milestone_id")
                if isinstance(item_milestone, str):
                    case["milestone_manual"][item_milestone].append(
                        {
                            "id": action_id,
                            "avoidable": (
                                True if is_intervention and avoidable is None else avoidable
                            ),
                            "kind": "intervention" if is_intervention else "manual_action",
                            "actor": actor,
                        }
                    )

                linked_errors = _strings(
                    _field(event, "error_cluster_ids", "resolved_error_cluster_ids")
                )
                single_linked_error = _string(_field(event, "error_cluster_id"))
                if single_linked_error is not None and single_linked_error not in linked_errors:
                    linked_errors.append(single_linked_error)
                for cluster_id in linked_errors:
                    category = "resolved_supervisor" if is_intervention else "resolved_human"
                    _mark_resolution(case, cluster_id, category, timestamp=timestamp)
                    cluster = case["errors"][cluster_id]
                    linked_field = (
                        "intervention_ids" if is_intervention else "manual_action_ids"
                    )
                    cluster[linked_field].add(action_id)

        cost_event = event_type in {
            "work.created",
            "work.completed",
            "work.reused",
            "model.invocation.completed",
            "intervention.completed",
            "action.completed",
            "delivery.completed",
        }
        has_work_identity = _field(
            event,
            "work_unit_id",
            "work_id",
            "model_invocation_id",
            "invocation_id",
        ) is not None
        has_cost = (
            _duration_seconds(event) is not None
            or _wait_seconds(event, "machine_wait_seconds") is not None
            or _wait_seconds(event, "external_wait_seconds") is not None
            or _wait_breakdown(event)[0] is not None
            or _token_mapping(event, "token_usage", "tokens", "token_receipt") is not None
            or _integer(_field(event, "total_tokens", "token_count")) is not None
            or _bool_field(event, "cost_unknown", "prior_cost_unknown") is True
        )
        if event_type in {
            "work.started",
            "work.created",
            "work.completed",
            "work.reused",
            "model.invocation.started",
        }:
            has_work_identity = True
        should_materialize_work = (
            event_type
            in {
                "work.created",
                "work.started",
                "work.completed",
                "work.reused",
                "model.invocation.started",
                "model.invocation.completed",
            }
            or has_work_identity
            or has_cost
        )
        if should_materialize_work:
            source_work_unit_id, explicit_work_id = _work_unit_id(
                event, event_type, index
            )
            work_unit_id = aliases_by_event.get(index, source_work_unit_id)
            work = work_units.setdefault(work_unit_id, _new_work_unit(work_unit_id))
            work["source_event_count"] += 1
            work["event_types"].add(event_type)
            if not explicit_work_id:
                _issue(global_issues, "work_unit_id_missing", work_unit_id=work_unit_id)
            shared_id = _string(
                _field(event, "shared_work_id", "shared_work_pool_id", "shared_id")
            )
            if shared_id is not None:
                work["shared_work_pool_ids"].add(shared_id)
            if owner_case_id is not None:
                work["owner_case_ids"].add(owner_case_id)
            beneficiaries = _beneficiary_case_ids(event)
            work["beneficiary_case_ids"].update(beneficiaries)
            dependencies = _expand_legacy_work_unit_references(
                _field(event, "dependency_ids", "work_unit_dependency_ids", "depends_on"),
                aliases_by_source,
                current_work_unit_id=work_unit_id,
            )
            all_in_dependencies = _expand_legacy_work_unit_references(
                _field(
                    event,
                    "all_in_dependency_ids",
                    "support_dependency_ids",
                    "overhead_work_unit_ids",
                ),
                aliases_by_source,
                current_work_unit_id=work_unit_id,
            )
            work["dependency_ids"].update(dependencies)
            work["all_in_dependency_ids"].update(all_in_dependencies)
            scope = _work_scope(
                event,
                event_type,
                owner_case_id=owner_case_id,
                beneficiary_case_ids=beneficiaries,
            )
            if work["scope"] is not None and work["scope"] != scope:
                _issue(global_issues, "work_unit_scope_conflict", work_unit_id=work_unit_id)
            else:
                work["scope"] = scope
            stage = _work_stage(event, event_type)
            if stage is not None:
                if work["stage"] is not None and work["stage"] != stage:
                    _issue(global_issues, "work_unit_stage_conflict", work_unit_id=work_unit_id)
                else:
                    work["stage"] = stage
            token_scope = _canonical_token_scope(event)
            if token_scope is not None:
                if work["token_scope"] is not None and work["token_scope"] != token_scope:
                    _issue(
                        global_issues,
                        "work_unit_token_scope_conflict",
                        work_unit_id=work_unit_id,
                    )
                else:
                    work["token_scope"] = token_scope
            work_actor = _canonical_actor(event)
            if work_actor is not None:
                work["actor_types"].add(work_actor)
            manual_work = (
                event_type.startswith("intervention")
                or _bool_field(event, "manual", "is_manual") is True
                or work_actor in {"human", "supervisor"}
            )
            if manual_work:
                work["manual"] = True
            elif work["manual"] is None and _bool_field(event, "manual", "is_manual") is False:
                work["manual"] = False
            interval_start, interval_end = _event_interval(event, event_type)
            if interval_start is not None:
                if work["started_at"] is not None and work["started_at"] != interval_start:
                    _issue(
                        global_issues,
                        "work_unit_start_time_conflict",
                        work_unit_id=work_unit_id,
                    )
                elif work["started_at"] is None:
                    work["started_at"] = interval_start
            if interval_end is not None:
                if work["completed_at"] is not None and work["completed_at"] != interval_end:
                    _issue(
                        global_issues,
                        "work_unit_end_time_conflict",
                        work_unit_id=work_unit_id,
                    )
                elif work["completed_at"] is None:
                    work["completed_at"] = interval_end
            if cost_event or has_cost:
                _merge_work_cost(work, event, global_issues)
            receipt_path = _string(
                _field(
                    event,
                    "token_receipt_path",
                    "model_token_receipt_path",
                    "usage_receipt_path",
                )
            )
            if receipt_path is not None:
                work["token_receipt_paths"].add(receipt_path)

            all_members = set(beneficiaries)
            if owner_case_id is not None:
                all_members.add(owner_case_id)
            if scope == "direct" and owner_case_id is not None:
                cases[owner_case_id]["direct_work_unit_ids"].add(work_unit_id)
                cases[owner_case_id]["inclusive_work_unit_ids"].add(work_unit_id)
            elif scope in {"shared", "dependency"}:
                for case_id in all_members:
                    cases[case_id]["inclusive_work_unit_ids"].add(work_unit_id)
            for case_id in all_members:
                cases[case_id]["all_in_work_unit_ids"].add(work_unit_id)

        if milestone is not None and not event_type.startswith(
            ("action", "intervention")
        ):
            automated = _bool_field(event, "automated", "is_automated")
            actor = _canonical_actor(event)
            if automated is False or actor in {"human", "supervisor"}:
                avoidable = _bool_field(event, "avoidable", "avoidable_action")
                policy_mandated = _bool_field(event, "policy_mandated")
                if policy_mandated is not None:
                    avoidable = not policy_mandated
                for case_id in case_ids:
                    cases[case_id]["milestone_manual"][milestone].append(
                        {
                            "id": _string(_field(event, "event_id")) or f"event:{index:08d}",
                            "avoidable": avoidable,
                            "kind": "milestone",
                            "actor": actor,
                        }
                    )

    return cases, work_units, global_issues, ignored_event_count


def _finalize_work_units(
    work_units: dict[str, dict[str, Any]], issues: list[dict[str, Any]]
) -> None:
    for work_unit_id, work in work_units.items():
        started_at = work.get("started_at")
        completed_at = work.get("completed_at")
        if isinstance(started_at, datetime) and isinstance(completed_at, datetime):
            if completed_at < started_at:
                _issue(
                    issues,
                    "work_unit_time_order_invalid",
                    work_unit_id=work_unit_id,
                )
        event_types = work["event_types"]
        if work["cost_unknown"]:
            _issue(
                issues,
                "work_unit_cost_unknown",
                work_unit_id=work_unit_id,
                detail=",".join(sorted(work["cost_unknown_reasons"])) or None,
            )
        if work["resource_time_unknown"]:
            _issue(
                issues,
                "work_unit_resource_time_unknown",
                work_unit_id=work_unit_id,
                detail=",".join(sorted(work["resource_time_unknown_reasons"])) or None,
            )
        if "model.invocation.completed" in event_types and not work["tokens_explicit"]:
            code = (
                "token_receipt_unmaterialized"
                if work["token_receipt_paths"]
                else "model_invocation_tokens_missing"
            )
            _issue(issues, code, work_unit_id=work_unit_id)
        actor_types = work.get("actor_types")
        actors = actor_types if isinstance(actor_types, set) else set()
        if "supervisor" in actors and not work["tokens_explicit"]:
            _issue(
                issues,
                "supervising_agent_tokens_missing",
                work_unit_id=work_unit_id,
            )


def _dependency_closure(
    seed: set[str],
    work_units: Mapping[str, dict[str, Any]],
    *,
    include_all_in: bool,
) -> tuple[set[str], set[str], bool]:
    found: set[str] = set()
    missing: set[str] = set()
    cycle_detected = False
    active: set[str] = set()

    def visit(work_unit_id: str) -> None:
        nonlocal cycle_detected
        if work_unit_id in active:
            cycle_detected = True
            return
        if work_unit_id in found:
            return
        work = work_units.get(work_unit_id)
        if work is None:
            missing.add(work_unit_id)
            return
        active.add(work_unit_id)
        found.add(work_unit_id)
        dependencies = set(work["dependency_ids"])
        if include_all_in:
            dependencies.update(work["all_in_dependency_ids"])
        for dependency_id in sorted(dependencies):
            visit(dependency_id)
        active.remove(work_unit_id)

    for work_unit_id in sorted(seed):
        visit(work_unit_id)
    return found, missing, cycle_detected


def _interval_metrics(intervals: Sequence[tuple[datetime, datetime]]) -> dict[str, Any]:
    if not intervals:
        return {
            "wall_clock_interval_union_seconds": 0.0,
            "observed_span_seconds": None,
            "unclassified_gap_seconds": None,
            "interval_start_at": None,
            "interval_end_at": None,
        }
    ordered = sorted(intervals, key=lambda item: (item[0], item[1]))
    merged: list[tuple[datetime, datetime]] = []
    for start_at, end_at in ordered:
        if not merged or start_at > merged[-1][1]:
            merged.append((start_at, end_at))
            continue
        previous_start, previous_end = merged[-1]
        if end_at > previous_end:
            merged[-1] = (previous_start, end_at)
    union_seconds = sum((end_at - start_at).total_seconds() for start_at, end_at in merged)
    interval_start = ordered[0][0]
    interval_end = max(end_at for _, end_at in ordered)
    span_seconds = (interval_end - interval_start).total_seconds()
    return {
        "wall_clock_interval_union_seconds": union_seconds,
        "observed_span_seconds": span_seconds,
        "unclassified_gap_seconds": max(0.0, span_seconds - union_seconds),
        "interval_start_at": _iso(interval_start),
        "interval_end_at": _iso(interval_end),
    }


def _token_cost_for_work_ids(
    work_unit_ids: set[str],
    work_units: Mapping[str, dict[str, Any]],
    *,
    avoidable: bool,
) -> dict[str, Any]:
    known = _empty_tokens()
    dimension_coverage: Counter[str] = Counter()
    expected_work_units = 0

    for work_unit_id in sorted(work_unit_ids):
        work = work_units[work_unit_id]
        gross_tokens = work.get("tokens")
        actor_types = work.get("actor_types")
        actors = actor_types if isinstance(actor_types, set) else set()
        model_usage_expected = "model.invocation.completed" in work.get(
            "event_types", set()
        ) or bool(actors.intersection({"model", "supervisor"}))
        cost_unknown = work.get("cost_unknown") is True
        token_map: Mapping[str, Any] | None = None
        expects_tokens = False
        if avoidable:
            explicit_avoidable = work.get("avoidable_tokens")
            if isinstance(explicit_avoidable, Mapping):
                token_map = explicit_avoidable
                expects_tokens = True
            elif work.get("avoidable") is True:
                token_map = gross_tokens if isinstance(gross_tokens, Mapping) else None
                expects_tokens = model_usage_expected or token_map is not None
            elif cost_unknown and work.get("avoidable") is not False:
                expects_tokens = True
        else:
            token_map = gross_tokens if isinstance(gross_tokens, Mapping) else None
            expects_tokens = cost_unknown or model_usage_expected or token_map is not None

        if not expects_tokens:
            continue
        expected_work_units += 1
        if token_map is None:
            continue
        for dimension in TOKEN_DIMENSIONS:
            value = token_map.get(dimension)
            if isinstance(value, int) and not isinstance(value, bool):
                known[dimension] += value
                dimension_coverage[dimension] += 1

    published: dict[str, int | None] = {}
    dimension_completeness: dict[str, Any] = {}
    for dimension in TOKEN_DIMENSIONS:
        covered = dimension_coverage[dimension]
        complete = expected_work_units == 0 or covered == expected_work_units
        published[dimension] = known[dimension] if complete else None
        dimension_completeness[dimension] = {
            "known_work_units": covered,
            "expected_work_units": expected_work_units,
            "ratio": covered / expected_work_units if expected_work_units else 1.0,
            "complete": complete,
        }
    return {
        "tokens": published,
        "known_token_subtotal": known,
        "expected_work_units": expected_work_units,
        "dimension_completeness": dimension_completeness,
    }


def _cost_for_work_ids(
    work_unit_ids: set[str],
    work_units: Mapping[str, dict[str, Any]],
    *,
    include_breakdowns: bool = True,
) -> dict[str, Any]:
    gross_active_seconds = 0.0
    gross_machine_wait_seconds = 0.0
    gross_external_wait_seconds = 0.0
    avoidable_active_seconds = 0.0
    avoidable_machine_wait_seconds = 0.0
    avoidable_external_wait_seconds = 0.0
    resource_time_coverage = 0
    shared_pool_ids: set[str] = set()
    intervals: list[tuple[datetime, datetime]] = []
    avoidable_intervals: list[tuple[datetime, datetime]] = []
    gross_waits = {category: 0.0 for category in WAIT_CATEGORIES}
    avoidable_waits = {category: 0.0 for category in WAIT_CATEGORIES}
    unknown_gross_cost_work_units = 0
    unknown_avoidable_cost_work_units = 0
    unknown_gross_resource_time_work_units = 0
    unknown_avoidable_resource_time_work_units = 0

    for work_unit_id in sorted(work_unit_ids):
        work = work_units[work_unit_id]
        if work.get("cost_unknown") is True:
            unknown_gross_cost_work_units += 1
            if work.get("avoidable") is not False:
                unknown_avoidable_cost_work_units += 1
        if work.get("resource_time_unknown") is True:
            unknown_gross_resource_time_work_units += 1
            if work.get("avoidable") is not False:
                unknown_avoidable_resource_time_work_units += 1
        active_seconds = work.get("active_seconds")
        machine_wait_seconds = work.get("machine_wait_seconds")
        external_wait_seconds = work.get("external_wait_seconds")
        has_resource_time = False
        if isinstance(active_seconds, (int, float)):
            gross_active_seconds += float(active_seconds)
            has_resource_time = True
        if isinstance(machine_wait_seconds, (int, float)):
            gross_machine_wait_seconds += float(machine_wait_seconds)
            has_resource_time = True
        if isinstance(external_wait_seconds, (int, float)):
            gross_external_wait_seconds += float(external_wait_seconds)
            has_resource_time = True
        if has_resource_time:
            resource_time_coverage += 1
        raw_waits = work.get("wait_seconds_by_category")
        if isinstance(raw_waits, Mapping):
            for category in WAIT_CATEGORIES:
                value = raw_waits.get(category)
                if isinstance(value, (int, float)):
                    gross_waits[category] += float(value)
        else:
            if isinstance(machine_wait_seconds, (int, float)):
                gross_waits["unknown"] += float(machine_wait_seconds)
            if isinstance(external_wait_seconds, (int, float)):
                gross_waits["external"] += float(external_wait_seconds)

        raw_avoidable_waits = work.get("avoidable_wait_seconds_by_category")
        if isinstance(raw_avoidable_waits, Mapping):
            for category in WAIT_CATEGORIES:
                value = raw_avoidable_waits.get(category)
                if isinstance(value, (int, float)):
                    avoidable_waits[category] += float(value)
        elif work.get("avoidable") is True:
            if isinstance(raw_waits, Mapping):
                for category in WAIT_CATEGORIES:
                    value = raw_waits.get(category)
                    if isinstance(value, (int, float)):
                        avoidable_waits[category] += float(value)
            else:
                if isinstance(machine_wait_seconds, (int, float)):
                    avoidable_waits["unknown"] += float(machine_wait_seconds)
                if isinstance(external_wait_seconds, (int, float)):
                    avoidable_waits["external"] += float(external_wait_seconds)
        for field, target_name in (
            ("avoidable_active_seconds", "active"),
            ("avoidable_machine_wait_seconds", "machine"),
            ("avoidable_external_wait_seconds", "external"),
        ):
            explicit_value = work.get(field)
            source_field = field.removeprefix("avoidable_")
            source_value = work.get(source_field)
            avoidable_value: float | None = None
            if isinstance(explicit_value, (int, float)):
                avoidable_value = float(explicit_value)
            elif work.get("avoidable") is True and isinstance(source_value, (int, float)):
                avoidable_value = float(source_value)
            if avoidable_value is None:
                continue
            if target_name == "active":
                avoidable_active_seconds += avoidable_value
            elif target_name == "machine":
                avoidable_machine_wait_seconds += avoidable_value
            else:
                avoidable_external_wait_seconds += avoidable_value
        started_at = work.get("started_at")
        completed_at = work.get("completed_at")
        if (
            isinstance(started_at, datetime)
            and isinstance(completed_at, datetime)
            and completed_at >= started_at
        ):
            intervals.append((started_at, completed_at))
            if work.get("avoidable") is True:
                avoidable_intervals.append((started_at, completed_at))
        shared_pool_ids.update(work["shared_work_pool_ids"])

    gross_token_cost = _token_cost_for_work_ids(
        work_unit_ids, work_units, avoidable=False
    )
    avoidable_token_cost = _token_cost_for_work_ids(
        work_unit_ids, work_units, avoidable=True
    )
    gross_token_totals = cast(dict[str, int | None], gross_token_cost["tokens"])
    avoidable_token_totals = cast(
        dict[str, int | None], avoidable_token_cost["tokens"]
    )
    wall = _interval_metrics(intervals)
    avoidable_wall = _interval_metrics(avoidable_intervals)
    gross_accounted_seconds = (
        gross_active_seconds + gross_machine_wait_seconds + gross_external_wait_seconds
    )
    avoidable_accounted_seconds = (
        avoidable_active_seconds
        + avoidable_machine_wait_seconds
        + avoidable_external_wait_seconds
    )
    gross_time_complete = (
        unknown_gross_cost_work_units == 0
        and unknown_gross_resource_time_work_units == 0
    )
    avoidable_time_complete = (
        unknown_avoidable_cost_work_units == 0
        and unknown_avoidable_resource_time_work_units == 0
    )
    gross_wall = (
        wall
        if unknown_gross_cost_work_units == 0
        else {key: None for key in wall}
    )
    avoidable_wall_published = (
        avoidable_wall
        if unknown_avoidable_cost_work_units == 0
        else {key: None for key in avoidable_wall}
    )
    gross_waits_published: dict[str, float | None] = {}
    avoidable_waits_published: dict[str, float | None] = {}
    for category in WAIT_CATEGORIES:
        gross_waits_published[category] = (
            gross_waits[category] if gross_time_complete else None
        )
        avoidable_waits_published[category] = (
            avoidable_waits[category] if avoidable_time_complete else None
        )
    result: dict[str, Any] = {
        "work_unit_count": len(work_unit_ids),
        "work_unit_ids": sorted(work_unit_ids),
        "shared_work_pool_ids": sorted(shared_pool_ids),
        "gross": {
            "tokens": gross_token_totals,
            "known_token_subtotal": gross_token_cost["known_token_subtotal"],
            "total_tokens": gross_token_totals["total_tokens"],
            "active_seconds": gross_active_seconds if gross_time_complete else None,
            "machine_wait_seconds": (
                gross_machine_wait_seconds if gross_time_complete else None
            ),
            "external_wait_seconds": (
                gross_external_wait_seconds if gross_time_complete else None
            ),
            "wait_seconds_by_category": gross_waits_published,
            "accounted_resource_seconds": (
                gross_accounted_seconds if gross_time_complete else None
            ),
            "known_active_seconds": gross_active_seconds,
            "known_machine_wait_seconds": gross_machine_wait_seconds,
            "known_external_wait_seconds": gross_external_wait_seconds,
            "known_wait_seconds_by_category": gross_waits,
            "known_accounted_resource_seconds": gross_accounted_seconds,
            "unknown_cost_work_units": unknown_gross_cost_work_units,
            "unknown_resource_time_work_units": (
                unknown_gross_resource_time_work_units
            ),
            **gross_wall,
        },
        "avoidable": {
            "tokens": avoidable_token_totals,
            "known_token_subtotal": avoidable_token_cost["known_token_subtotal"],
            "total_tokens": avoidable_token_totals["total_tokens"],
            "active_seconds": (
                avoidable_active_seconds if avoidable_time_complete else None
            ),
            "machine_wait_seconds": (
                avoidable_machine_wait_seconds if avoidable_time_complete else None
            ),
            "external_wait_seconds": (
                avoidable_external_wait_seconds if avoidable_time_complete else None
            ),
            "wait_seconds_by_category": avoidable_waits_published,
            "accounted_resource_seconds": (
                avoidable_accounted_seconds if avoidable_time_complete else None
            ),
            "known_active_seconds": avoidable_active_seconds,
            "known_machine_wait_seconds": avoidable_machine_wait_seconds,
            "known_external_wait_seconds": avoidable_external_wait_seconds,
            "known_wait_seconds_by_category": avoidable_waits,
            "known_accounted_resource_seconds": avoidable_accounted_seconds,
            "unknown_cost_work_units": unknown_avoidable_cost_work_units,
            "unknown_resource_time_work_units": (
                unknown_avoidable_resource_time_work_units
            ),
            **avoidable_wall_published,
        },
        "completeness": {
            "token_work_units": gross_token_cost["dimension_completeness"][
                "total_tokens"
            ]["known_work_units"],
            "expected_token_work_units": gross_token_cost["expected_work_units"],
            "resource_time_work_units": resource_time_coverage,
            "interval_work_units": len(intervals),
            "work_units": len(work_unit_ids),
            "unknown_cost_work_units": unknown_gross_cost_work_units,
            "unknown_resource_time_work_units": (
                unknown_gross_resource_time_work_units
            ),
            "cost_complete": gross_time_complete,
            "token_ratio": gross_token_cost["dimension_completeness"]["total_tokens"][
                "ratio"
            ],
            "total_tokens_complete": gross_token_cost["dimension_completeness"][
                "total_tokens"
            ]["complete"],
            "token_dimensions": gross_token_cost["dimension_completeness"],
            "resource_time_ratio": (
                resource_time_coverage / len(work_unit_ids) if work_unit_ids else 1.0
            ),
            "interval_ratio": (len(intervals) / len(work_unit_ids)) if work_unit_ids else 1.0,
        },
    }
    if include_breakdowns:
        stage_work_ids: dict[str, set[str]] = defaultdict(set)
        token_scope_work_ids: dict[str, set[str]] = {
            scope: set() for scope in TOKEN_SCOPES
        }
        for work_unit_id in work_unit_ids:
            work = work_units[work_unit_id]
            stage = _string(work.get("stage")) or "unclassified"
            stage_work_ids[stage].add(work_unit_id)
            token_scope = _string(work.get("token_scope")) or "unclassified"
            if token_scope not in token_scope_work_ids:
                token_scope = "unclassified"
            token_scope_work_ids[token_scope].add(work_unit_id)
        result["by_stage"] = {
            stage: _cost_for_work_ids(ids, work_units, include_breakdowns=False)
            for stage, ids in sorted(stage_work_ids.items())
        }
        result["by_token_scope"] = {
            scope: _cost_for_work_ids(
                token_scope_work_ids[scope], work_units, include_breakdowns=False
            )
            for scope in TOKEN_SCOPES
        }
    return result


def _select_disposition(case: dict[str, Any]) -> tuple[str | None, bool]:
    verified_values = [item[0] for item in case["dispositions_verified"]]
    reached_values = [item[0] for item in case["dispositions_reached"]]
    distinct_verified = sorted(set(verified_values))
    distinct_reached = sorted(set(reached_values))
    if len(distinct_verified) > 1:
        _issue(
            case["issues"],
            "verified_disposition_conflict",
            case_id=case["case_id"],
            detail=",".join(distinct_verified),
        )
    if len(distinct_reached) > 1:
        _issue(
            case["issues"],
            "reached_disposition_conflict",
            case_id=case["case_id"],
            detail=",".join(distinct_reached),
        )
    if distinct_verified:
        return distinct_verified[-1], len(distinct_verified) == 1
    if distinct_reached:
        return distinct_reached[-1], False
    return None, False


def _automation_score(
    case: dict[str, Any],
    *,
    disposition: str | None,
    disposition_verified: bool,
    reconciliation_ok: bool,
    system_fingerprint_complete: bool,
) -> dict[str, Any]:
    required = AUTOMATION_SCORE_V1_MILESTONE_PATHS.get(disposition or "", ())
    completed = set(case["milestones"])
    missing = [milestone for milestone in required if milestone not in completed]
    manual_milestones: set[str] = set()
    human_manual_milestones: set[str] = set()
    supervisor_manual_milestones: set[str] = set()
    unavoidable_manual: set[str] = set()
    avoidability_unclassified: set[str] = set()
    milestone_details: list[dict[str, Any]] = []

    for milestone in required:
        annotations = case["milestone_manual"].get(milestone, [])
        is_manual = bool(annotations)
        if is_manual:
            manual_milestones.add(milestone)
            actors = {item.get("actor") for item in annotations}
            if "human" in actors:
                human_manual_milestones.add(milestone)
            if "supervisor" in actors:
                supervisor_manual_milestones.add(milestone)
            classifications = {item.get("avoidable") for item in annotations}
            if classifications == {False}:
                unavoidable_manual.add(milestone)
            elif None in classifications:
                avoidability_unclassified.add(milestone)
        milestone_details.append(
            {
                "milestone_id": milestone,
                "completed": milestone in completed,
                "automated": milestone in completed and not is_manual,
                "manual": is_manual,
                "manual_actors": sorted(
                    {
                        str(item.get("actor"))
                        for item in annotations
                        if item.get("actor") is not None
                    }
                ),
                "manual_avoidable": (
                    None
                    if not is_manual or milestone in avoidability_unclassified
                    else milestone not in unavoidable_manual
                ),
            }
        )

    automated_count = sum(
        1
        for milestone in required
        if milestone in completed and milestone not in manual_milestones
    )
    gross_score = (100.0 * automated_count / len(required)) if required else None
    avoidable_required = len(required) - len(unavoidable_manual)
    avoidable_automated = sum(
        1
        for milestone in required
        if milestone not in unavoidable_manual
        and milestone in completed
        and milestone not in manual_milestones
    )
    avoidable_score = (
        100.0 * avoidable_automated / avoidable_required if avoidable_required else 100.0
    ) if required else None

    order_issues: list[str] = []
    previous_time: datetime | None = None
    previous_milestone: str | None = None
    for milestone in required:
        times = [value for value in case["milestones"].get(milestone, []) if value is not None]
        current_time = min(times) if times else None
        if (
            previous_time is not None
            and current_time is not None
            and current_time < previous_time
        ):
            order_issues.append(f"{previous_milestone}->{milestone}")
        if current_time is not None:
            previous_time = current_time
            previous_milestone = milestone

    withheld_reasons: list[str] = []
    if "origin" not in completed:
        withheld_reasons.append("origin_missing")
    if not case["origin_telemetry_known"]:
        withheld_reasons.append("origin_telemetry_unknown")
    if disposition is None:
        withheld_reasons.append("disposition_missing")
    elif not required:
        withheld_reasons.append("disposition_path_unknown")
    if not disposition_verified:
        withheld_reasons.append("disposition_not_verified")
    if missing:
        withheld_reasons.append("required_milestones_missing")
    if order_issues:
        withheld_reasons.append("milestone_order_invalid")
    if not reconciliation_ok:
        withheld_reasons.append("accounting_reconciliation_failed")
    if not system_fingerprint_complete:
        withheld_reasons.append(
            "system_fingerprint_incomplete"
            if case["system_fingerprints"]
            else "system_fingerprint_missing"
        )
    if avoidability_unclassified:
        withheld_reasons.append("manual_avoidability_unclassified")

    is_pending = disposition is None and not case["closed_at"]
    is_failed_or_invalid = disposition == "failed_incomplete"
    if is_failed_or_invalid:
        gross_score = 0.0
        avoidable_score = 0.0
    elif withheld_reasons:
        gross_score = None
        avoidable_score = None
    status = (
        "pending"
        if is_pending
        else "failed_or_invalid"
        if is_failed_or_invalid
        else "certified"
        if not withheld_reasons
        else "withheld"
    )

    return {
        "version": AUTOMATION_SCORE_VERSION,
        "status": status,
        "certified": status == "certified",
        "withheld_reasons": withheld_reasons,
        "required_milestone_path": list(required),
        "milestones": milestone_details,
        "missing_milestones": missing,
        "milestone_order_issues": order_issues,
        "gross": {
            "score": gross_score,
            "automated_milestones": automated_count,
            "required_milestones": len(required),
            "manual_milestones": len(manual_milestones),
            "human_manual_milestones": len(human_manual_milestones),
            "supervisor_manual_milestones": len(supervisor_manual_milestones),
        },
        "avoidable": {
            "score": avoidable_score,
            "automated_milestones": avoidable_automated,
            "required_milestones": avoidable_required,
            "avoidable_manual_milestones": len(manual_milestones - unavoidable_manual),
            "unavoidable_manual_milestones": len(unavoidable_manual),
            "unclassified_manual_milestones": len(avoidability_unclassified),
        },
    }


def _resolution_tokens(
    raw: Mapping[str, Any], work_units: Mapping[str, dict[str, Any]]
) -> tuple[dict[str, int | None] | None, list[str]]:
    direct = raw.get("resolution_tokens")
    if isinstance(direct, Mapping):
        tokens = {
            dimension: (
                int(direct[dimension])
                if isinstance(direct.get(dimension), int)
                and not isinstance(direct.get(dimension), bool)
                else None
            )
            for dimension in TOKEN_DIMENSIONS
        }
        return (tokens if tokens["total_tokens"] is not None else None), []

    work_ids = set(_strings(raw.get("resolution_work_unit_ids")))
    if raw.get("resolution_cost_attribution_complete") is not True:
        return None, sorted(work_ids)
    missing = sorted(work_id for work_id in work_ids if work_id not in work_units)
    if missing:
        return None, missing
    if not work_ids:
        # An explicit empty attribution proves no token-bearing repair work.
        return cast(dict[str, int | None], _empty_tokens()), []
    cost = _token_cost_for_work_ids(work_ids, work_units, avoidable=False)
    tokens = cast(dict[str, int | None], cost["tokens"])
    if tokens["total_tokens"] is None:
        return None, []
    return tokens, []


def _serialize_error_metrics(
    case: dict[str, Any],
    *,
    terminal: bool,
    work_units: Mapping[str, dict[str, Any]],
) -> dict[str, Any]:
    clusters: list[dict[str, Any]] = []
    resolution_counts: Counter[str] = Counter()
    occurrence_count = 0
    resolution_elapsed_values: list[float] = []
    derived_self_healed_ids: set[str] = set()
    for cluster_id in sorted(case["errors"]):
        raw = case["errors"][cluster_id]
        category = raw.get("resolution_category") or (
            "unresolved_terminal"
            if terminal and raw.get("resolution_evidence_unknown") is not True
            else "open"
        )
        if (
            category == "open"
            and terminal
            and raw.get("resolution_evidence_unknown") is not True
        ):
            category = "unresolved_terminal"
        if category in {"self_healed_same_author", "self_healed_controller"}:
            derived_self_healed_ids.add(cluster_id)
        resolution_counts[str(category)] += 1
        occurrence_count += int(raw.get("occurrence_count", 0))
        occurred_at = raw.get("occurred_at")
        resolved_at = raw.get("resolved_at")
        resolution_elapsed_seconds: float | None = None
        if (
            category not in {"open", "unresolved_terminal"}
            and raw.get("resolution_timing_unknown") is not True
            and isinstance(occurred_at, datetime)
            and isinstance(resolved_at, datetime)
        ):
            elapsed = (resolved_at - occurred_at).total_seconds()
            if elapsed >= 0:
                resolution_elapsed_seconds = elapsed
                resolution_elapsed_values.append(elapsed)
        resolution_tokens, missing_resolution_work_ids = _resolution_tokens(
            raw, work_units
        )
        intervention_ids = sorted(_strings(raw.get("intervention_ids")))
        manual_action_ids = sorted(_strings(raw.get("manual_action_ids")))
        clusters.append(
            {
                "error_cluster_id": cluster_id,
                "occurred_at": _iso(occurred_at),
                "resolved_at": _iso(resolved_at),
                "resolution_elapsed_seconds": resolution_elapsed_seconds,
                "resolution_timing_complete": (
                    raw.get("resolution_timing_unknown") is not True
                    and isinstance(occurred_at, datetime)
                    and isinstance(resolved_at, datetime)
                    and category not in {"open", "unresolved_terminal"}
                ),
                "resolution_category": category,
                "event_count": raw.get("event_count", 0),
                "occurrence_count": raw.get("occurrence_count", 0),
                "resolution_event_count": raw.get("resolution_event_count", 0),
                "linked_intervention_count": len(intervention_ids),
                "linked_intervention_ids": intervention_ids,
                "linked_manual_action_count": len(manual_action_ids),
                "linked_manual_action_ids": manual_action_ids,
                "resolution_cost_attribution_complete": (
                    raw.get("resolution_cost_attribution_complete") is True
                    and not missing_resolution_work_ids
                ),
                "resolution_work_unit_ids": sorted(
                    _strings(raw.get("resolution_work_unit_ids"))
                ),
                "missing_resolution_work_unit_ids": missing_resolution_work_ids,
                "resolution_tokens": resolution_tokens,
                "resolution_total_tokens": (
                    resolution_tokens.get("total_tokens")
                    if resolution_tokens is not None
                    else None
                ),
            }
        )
    attested_self_healed_ids = set(case["attested_self_healed_cluster_ids"])
    self_healed_count = len(derived_self_healed_ids | attested_self_healed_ids)
    return {
        "cluster_count": len(clusters),
        "occurrence_count": occurrence_count,
        "by_resolution": dict(sorted(resolution_counts.items())),
        "self_healed": self_healed_count,
        "self_healed_cluster_count": self_healed_count,
        "attested_self_healed_cluster_ids": sorted(attested_self_healed_ids),
        "supervisor_intervention": resolution_counts.get("resolved_supervisor", 0),
        "manual": resolution_counts.get("resolved_human", 0),
        "unresolved": resolution_counts.get("unresolved_terminal", 0),
        "externally_resolved_cluster_count": (
            resolution_counts.get("resolved_supervisor", 0)
            + resolution_counts.get("resolved_human", 0)
            + resolution_counts.get("resolved_external", 0)
        ),
        "unresolved_terminal_cluster_count": resolution_counts.get(
            "unresolved_terminal", 0
        ),
        "open_cluster_count": resolution_counts.get("open", 0),
        "tolerated_nonblocking_cluster_count": resolution_counts.get(
            "tolerated_nonblocking", 0
        ),
        "resolution_elapsed_seconds": _distribution(resolution_elapsed_values),
        "clusters": clusters,
    }


def _serialize_actions(items: Mapping[str, dict[str, Any]]) -> dict[str, Any]:
    serialized: list[dict[str, Any]] = []
    avoidable_count = 0
    unavoidable_count = 0
    unclassified_count = 0
    incomplete_count = 0
    required_for_progress_count = 0
    policy_mandated_count = 0
    passive_observation_count = 0
    measurement_administration_count = 0
    by_actor: Counter[str] = Counter()
    active_seconds = 0.0
    missing_active_seconds_count = 0
    for action_id in sorted(items):
        item = items[action_id]
        avoidable = item.get("avoidable")
        if avoidable is True:
            avoidable_count += 1
        elif avoidable is False:
            unavoidable_count += 1
        else:
            unclassified_count += 1
        if item.get("completed_at") is None:
            incomplete_count += 1
        if item.get("required_for_progress") is True:
            required_for_progress_count += 1
        if item.get("policy_mandated") is True:
            policy_mandated_count += 1
        if item.get("passive_observation") is True:
            passive_observation_count += 1
        if item.get("measurement_administration") is True:
            measurement_administration_count += 1
        actor = _string(item.get("actor")) or "unknown"
        by_actor[actor] += 1
        explicit_active = item.get("active_seconds")
        elapsed_seconds: float | None = (
            float(explicit_active) if isinstance(explicit_active, (int, float)) else None
        )
        if elapsed_seconds is not None:
            active_seconds += elapsed_seconds
        else:
            missing_active_seconds_count += 1
        serialized.append(
            {
                "id": action_id,
                "started_at": _iso(item.get("started_at")),
                "completed_at": _iso(item.get("completed_at")),
                "milestone_id": item.get("milestone_id"),
                "avoidable": avoidable,
                "actor": actor,
                "action_family": item.get("action_family"),
                "operation": item.get("operation"),
                "interface": item.get("interface"),
                "work_scope": item.get("work_scope"),
                "policy_mandated": item.get("policy_mandated"),
                "passive_observation": item.get("passive_observation"),
                "measurement_administration": item.get("measurement_administration"),
                "required_for_progress": item.get("required_for_progress"),
                "active_seconds": elapsed_seconds,
            }
        )
    return {
        "count": len(serialized),
        "avoidable_count": avoidable_count,
        "unavoidable_count": unavoidable_count,
        "unclassified_count": unclassified_count,
        "incomplete_count": incomplete_count,
        "required_for_progress_count": required_for_progress_count,
        "policy_mandated_count": policy_mandated_count,
        "passive_observation_count": passive_observation_count,
        "measurement_administration_count": measurement_administration_count,
        "by_actor": dict(sorted(by_actor.items())),
        "active_seconds": (
            active_seconds if missing_active_seconds_count == 0 else None
        ),
        "known_active_seconds": active_seconds,
        "missing_active_seconds_count": missing_active_seconds_count,
        "active_seconds_complete": missing_active_seconds_count == 0,
        "items": serialized,
    }


def _with_action_telemetry_completeness(
    actions: dict[str, Any], telemetry_complete: bool | None
) -> dict[str, Any]:
    """Withhold totals that an authoritative producer says are incomplete."""

    actions["telemetry_complete"] = telemetry_complete
    if telemetry_complete is not False:
        return actions

    count_fields = (
        "count",
        "avoidable_count",
        "unavoidable_count",
        "unclassified_count",
        "incomplete_count",
        "required_for_progress_count",
        "policy_mandated_count",
        "passive_observation_count",
        "measurement_administration_count",
    )
    for field in count_fields:
        actions[f"known_{field}"] = actions[field]
        actions[field] = None
    actions["known_by_actor"] = actions["by_actor"]
    actions["by_actor"] = None
    actions["active_seconds"] = None
    actions["active_seconds_complete"] = False
    return actions


def _work_issue_applies(issue: Mapping[str, Any], all_in_ids: set[str]) -> bool:
    work_unit_id = issue.get("work_unit_id")
    return work_unit_id is None or work_unit_id in all_in_ids


def _serialize_work_unit(work: Mapping[str, Any]) -> dict[str, Any]:
    tokens = work.get("tokens")
    avoidable_tokens = work.get("avoidable_tokens")
    return {
        "work_unit_id": work["work_unit_id"],
        "shared_work_pool_ids": sorted(work["shared_work_pool_ids"]),
        "owner_case_ids": sorted(work["owner_case_ids"]),
        "beneficiary_case_ids": sorted(work["beneficiary_case_ids"]),
        "dependency_ids": sorted(work["dependency_ids"]),
        "all_in_dependency_ids": sorted(work["all_in_dependency_ids"]),
        "scope": work["scope"],
        "stage": work["stage"],
        "token_scope": work["token_scope"] or "unclassified",
        "actor_types": sorted(work["actor_types"]),
        "manual": work["manual"],
        "cost_unknown": work["cost_unknown"],
        "cost_unknown_reasons": sorted(work["cost_unknown_reasons"]),
        "resource_time_unknown": work["resource_time_unknown"],
        "resource_time_unknown_reasons": sorted(
            work["resource_time_unknown_reasons"]
        ),
        "tokens": dict(tokens) if isinstance(tokens, Mapping) else None,
        "active_seconds": work["active_seconds"],
        "machine_wait_seconds": work["machine_wait_seconds"],
        "external_wait_seconds": work["external_wait_seconds"],
        "wait_seconds_by_category": work["wait_seconds_by_category"],
        "avoidable": work["avoidable"],
        "avoidable_tokens": (
            dict(avoidable_tokens) if isinstance(avoidable_tokens, Mapping) else None
        ),
        "avoidable_active_seconds": work["avoidable_active_seconds"],
        "avoidable_machine_wait_seconds": work["avoidable_machine_wait_seconds"],
        "avoidable_external_wait_seconds": work["avoidable_external_wait_seconds"],
        "avoidable_wait_seconds_by_category": work[
            "avoidable_wait_seconds_by_category"
        ],
        "started_at": _iso(work.get("started_at")),
        "completed_at": _iso(work.get("completed_at")),
        "token_receipt_paths": sorted(work["token_receipt_paths"]),
        "event_types": sorted(work["event_types"]),
        "source_event_count": work["source_event_count"],
    }


def aggregate_case_metrics(events: LifecycleSource) -> dict[str, Any]:
    """Aggregate exact lifecycle facts into per-case metrics and a reusable work graph."""

    loaded = load_lifecycle_events(events)
    aliases_by_event, aliases_by_source, legacy_action_splits = (
        _legacy_action_work_unit_aliases(loaded)
    )
    cases, work_units, global_issues, ignored_event_count = _collect_events(
        loaded,
        work_unit_aliases_by_event=aliases_by_event,
        work_unit_aliases_by_source=aliases_by_source,
    )
    _finalize_work_units(work_units, global_issues)
    serialized_cases: list[dict[str, Any]] = []

    for case_id in sorted(cases):
        case = cases[case_id]
        direct_ids = set(case["direct_work_unit_ids"])
        inclusive_seed = set(case["inclusive_work_unit_ids"]) | direct_ids
        inclusive_ids, inclusive_missing, inclusive_cycle = _dependency_closure(
            inclusive_seed, work_units, include_all_in=False
        )
        all_in_seed = set(case["all_in_work_unit_ids"]) | inclusive_ids
        all_in_ids, all_in_missing, all_in_cycle = _dependency_closure(
            all_in_seed, work_units, include_all_in=True
        )
        for missing_id in sorted(inclusive_missing | all_in_missing):
            _issue(
                case["issues"],
                "work_unit_dependency_missing",
                case_id=case_id,
                work_unit_id=missing_id,
            )
        if inclusive_cycle or all_in_cycle:
            _issue(case["issues"], "work_unit_dependency_cycle", case_id=case_id)

        applicable_global_issues = [
            dict(issue) for issue in global_issues if _work_issue_applies(issue, all_in_ids)
        ]
        case_issues = [*case["issues"], *applicable_global_issues]
        reconciliation_ok = not case_issues
        disposition, disposition_verified = _select_disposition(case)
        # Selecting the disposition can add conflicts to case issues.
        case_issues = [*case["issues"], *applicable_global_issues]
        reconciliation_ok = not case_issues

        opened_times = [value for value in case["opened_at"] if value is not None]
        atom_times = [value for value in case["atom_at"] if value is not None]
        admission_times = [value for value in case["admission_at"] if value is not None]
        lineage_times = [value for value in case["lineage_at"] if value is not None]
        pr_created_times = [value for value in case["pr_created_at"] if value is not None]
        outcome_verified_times = [
            value for value in case["outcome_verified_at"] if value is not None
        ]
        verified_times = [
            timestamp for _, timestamp in case["dispositions_verified"] if timestamp is not None
        ]
        closed_times = [value for value in case["closed_at"] if value is not None]
        atom_at = min(atom_times) if atom_times else None
        admission_at = min(admission_times) if admission_times else None
        lineage_at = min(lineage_times or opened_times) if (lineage_times or opened_times) else None
        pr_created_at = min(pr_created_times) if pr_created_times else None
        outcome_verified_at = max(outcome_verified_times) if outcome_verified_times else None
        start_at = lineage_at
        end_candidates = verified_times or closed_times
        end_at = max(end_candidates) if end_candidates else None
        lifecycle_wall_seconds: float | None = None
        if start_at is not None and end_at is not None:
            candidate_seconds = (end_at - start_at).total_seconds()
            if candidate_seconds < 0:
                _issue(case_issues, "lifecycle_time_order_invalid", case_id=case_id)
                reconciliation_ok = False
            else:
                lifecycle_wall_seconds = candidate_seconds

        fingerprint_keys = sorted(case["system_fingerprints"])
        fingerprint_values = [case["system_fingerprints"][key] for key in fingerprint_keys]
        fingerprint_complete = (
            len(fingerprint_values) == 1
            and _system_fingerprint_complete(fingerprint_values[0])
        )
        if len(fingerprint_keys) > 1:
            _issue(
                case_issues,
                "system_fingerprint_conflict",
                case_id=case_id,
                detail=",".join(fingerprint_keys),
            )
            reconciliation_ok = False

        accounting = {
            "direct": _cost_for_work_ids(direct_ids, work_units),
            "inclusive": _cost_for_work_ids(inclusive_ids, work_units),
            "all_in": _cost_for_work_ids(all_in_ids, work_units),
        }
        all_in_gross = accounting["all_in"]["gross"]
        wait_breakdown = all_in_gross["wait_seconds_by_category"]
        interval_union = all_in_gross["wall_clock_interval_union_seconds"]
        lifecycle_unclassified_seconds: float | None = None
        if lifecycle_wall_seconds is not None and isinstance(interval_union, (int, float)):
            lifecycle_unclassified_seconds = max(
                0.0, lifecycle_wall_seconds - float(interval_union)
            )

        def boundary_seconds(start: datetime | None, end: datetime | None) -> float | None:
            if start is None or end is None:
                return None
            elapsed = (end - start).total_seconds()
            return elapsed if elapsed >= 0 else None

        errors = _serialize_error_metrics(
            case,
            terminal=disposition is not None or bool(case["closed_at"]),
            work_units=work_units,
        )
        interventions = _serialize_actions(case["interventions"])
        manual_actions = _with_action_telemetry_completeness(
            _serialize_actions(case["manual_actions"]),
            case["manual_action_telemetry_complete"],
        )
        manual_work = _manual_work_summary(all_in_ids, work_units)
        automation = _automation_score(
            case,
            disposition=disposition,
            disposition_verified=disposition_verified,
            reconciliation_ok=reconciliation_ok,
            system_fingerprint_complete=fingerprint_complete,
        )
        required_milestones = automation["required_milestone_path"]
        present_required = sum(
            1 for milestone in required_milestones if milestone in case["milestones"]
        )

        stable_case_ids = sorted(case["stable_case_ids"])
        serialized_cases.append(
            {
                "case_lifecycle_id": case_id,
                "case_id": stable_case_ids[0] if len(stable_case_ids) == 1 else case_id,
                "stable_case_ids": stable_case_ids,
                "case_lifecycle_ids": sorted(case["lifecycle_ids"]),
                "cycle_ids": sorted(case["cycle_ids"]),
                "system_fingerprint": (
                    fingerprint_values[0] if len(fingerprint_values) == 1 else None
                ),
                "system_fingerprint_key": (
                    fingerprint_keys[0] if len(fingerprint_keys) == 1 else None
                ),
                "system_fingerprints": fingerprint_values,
                "system_fingerprint_keys": fingerprint_keys,
                "disposition": disposition,
                "raw_dispositions": sorted(case["raw_dispositions"]),
                "lifecycle_status": (
                    "terminal"
                    if disposition is not None
                    else "closed_unclassified"
                    if case["closed_at"]
                    else "active"
                ),
                "disposition_verified": disposition_verified,
                "cohort_eligible": case["case_cohort_eligible"] is True,
                "timing": {
                    "atom_at": _iso(atom_at),
                    "admission_at": _iso(admission_at),
                    "lineage_at": _iso(lineage_at),
                    "origin_at": _iso(lineage_at),
                    "final_disposition_at": _iso(end_at),
                    "pr_created_at": _iso(pr_created_at),
                    "outcome_verified_at": _iso(outcome_verified_at),
                    "atom_to_disposition_seconds": boundary_seconds(atom_at, end_at),
                    "admission_to_disposition_seconds": boundary_seconds(
                        admission_at, end_at
                    ),
                    "lineage_to_disposition_seconds": boundary_seconds(lineage_at, end_at),
                    "lifecycle_wall_seconds": lifecycle_wall_seconds,
                    "pr_create_to_outcome_seconds": boundary_seconds(
                        pr_created_at, outcome_verified_at
                    ),
                    "work_interval_union_seconds": interval_union,
                    "active_seconds": all_in_gross["active_seconds"],
                    "manual_active_seconds": (
                        interventions["active_seconds"] + manual_actions["active_seconds"]
                        if isinstance(interventions["active_seconds"], (int, float))
                        and isinstance(manual_actions["active_seconds"], (int, float))
                        else None
                    ),
                    "machine_wait_seconds": all_in_gross["machine_wait_seconds"],
                    "external_wait_seconds": all_in_gross["external_wait_seconds"],
                    "wait_seconds_by_category": wait_breakdown,
                    "queue_wait_seconds": wait_breakdown["queue"],
                    "provider_wait_seconds": wait_breakdown["provider"],
                    "ci_wait_seconds": wait_breakdown["ci"],
                    "approval_wait_seconds": wait_breakdown["approval"],
                    "categorized_external_wait_seconds": wait_breakdown["external"],
                    "unknown_wait_seconds": wait_breakdown["unknown"],
                    "accounted_resource_seconds": all_in_gross[
                        "accounted_resource_seconds"
                    ],
                    "unclassified_seconds": lifecycle_unclassified_seconds,
                },
                "accounting": accounting,
                "errors": errors,
                "interventions": interventions,
                "manual_actions": manual_actions,
                "manual_work": manual_work,
                "automation_score_v1": automation,
                "milestones_completed": sorted(case["milestones"]),
                "completeness": {
                    "lifecycle_opened_present": case["lifecycle_opened_event_count"] > 0,
                    "origin_present": "origin" in case["milestones"],
                    "disposition_present": disposition is not None,
                    "disposition_verified": disposition_verified,
                    "system_fingerprint_present": len(fingerprint_keys) == 1,
                    "system_fingerprint_complete": fingerprint_complete,
                    "origin_telemetry_known": case["origin_telemetry_known"],
                    "manual_action_telemetry_complete": case[
                        "manual_action_telemetry_complete"
                    ],
                    "required_milestones_present": present_required,
                    "required_milestones": len(required_milestones),
                    "required_milestone_ratio": (
                        present_required / len(required_milestones)
                        if required_milestones
                        else None
                    ),
                    "lifecycle_wall_time_present": lifecycle_wall_seconds is not None,
                    "accounting_reconciled": reconciliation_ok,
                },
                "reconciliation": {
                    "ok": reconciliation_ok,
                    "issues": sorted(
                        case_issues,
                        key=lambda item: (
                            str(item.get("code", "")),
                            str(item.get("work_unit_id", "")),
                            str(item.get("detail", "")),
                        ),
                    ),
                },
                "event_count": case["event_count"],
            }
        )

    all_issues = list(global_issues)
    for case in serialized_cases:
        for issue in case["reconciliation"]["issues"]:
            if issue not in all_issues:
                all_issues.append(issue)
    return {
        "schema_version": 1,
        "metric_version": CASE_METRICS_VERSION,
        "source_event_count": len(loaded),
        "recognized_event_count": len(loaded) - ignored_event_count,
        "ignored_event_count": ignored_event_count,
        "case_count": len(serialized_cases),
        "cases": serialized_cases,
        "work_units": [
            _serialize_work_unit(work_units[work_unit_id])
            for work_unit_id in sorted(work_units)
        ],
        "normalization": {
            "legacy_action_work_unit_splits": legacy_action_splits,
        },
        "reconciliation": {
            "ok": not all_issues,
            "issues": sorted(
                all_issues,
                key=lambda item: (
                    str(item.get("code", "")),
                    str(item.get("case_id", "")),
                    str(item.get("work_unit_id", "")),
                    str(item.get("detail", "")),
                ),
            ),
        },
    }


def _rehydrate_work_units(report: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    raw_units = report.get("work_units")
    units = raw_units if isinstance(raw_units, list) else []
    work_units: dict[str, dict[str, Any]] = {}
    for raw in units:
        if not isinstance(raw, Mapping):
            continue
        work_unit_id = _string(raw.get("work_unit_id"))
        if work_unit_id is None:
            continue
        work_units[work_unit_id] = {
            "work_unit_id": work_unit_id,
            "shared_work_pool_ids": set(_strings(raw.get("shared_work_pool_ids"))),
            "owner_case_ids": set(_strings(raw.get("owner_case_ids"))),
            "beneficiary_case_ids": set(_strings(raw.get("beneficiary_case_ids"))),
            "dependency_ids": set(_strings(raw.get("dependency_ids"))),
            "all_in_dependency_ids": set(_strings(raw.get("all_in_dependency_ids"))),
            "scope": raw.get("scope"),
            "stage": raw.get("stage"),
            "token_scope": raw.get("token_scope"),
            "actor_types": set(_strings(raw.get("actor_types"))),
            "manual": raw.get("manual"),
            "cost_unknown": raw.get("cost_unknown") is True,
            "cost_unknown_reasons": set(_strings(raw.get("cost_unknown_reasons"))),
            "tokens": dict(raw["tokens"]) if isinstance(raw.get("tokens"), Mapping) else None,
            "active_seconds": raw.get("active_seconds", raw.get("duration_seconds")),
            "machine_wait_seconds": raw.get("machine_wait_seconds"),
            "external_wait_seconds": raw.get("external_wait_seconds"),
            "wait_seconds_by_category": (
                dict(raw["wait_seconds_by_category"])
                if isinstance(raw.get("wait_seconds_by_category"), Mapping)
                else None
            ),
            "avoidable": raw.get("avoidable"),
            "avoidable_tokens": (
                dict(raw["avoidable_tokens"])
                if isinstance(raw.get("avoidable_tokens"), Mapping)
                else None
            ),
            "avoidable_active_seconds": raw.get(
                "avoidable_active_seconds", raw.get("avoidable_duration_seconds")
            ),
            "avoidable_machine_wait_seconds": raw.get("avoidable_machine_wait_seconds"),
            "avoidable_external_wait_seconds": raw.get("avoidable_external_wait_seconds"),
            "avoidable_wait_seconds_by_category": (
                dict(raw["avoidable_wait_seconds_by_category"])
                if isinstance(raw.get("avoidable_wait_seconds_by_category"), Mapping)
                else None
            ),
            "started_at": _parse_timestamp(raw.get("started_at")),
            "completed_at": _parse_timestamp(raw.get("completed_at")),
            "token_receipt_paths": set(_strings(raw.get("token_receipt_paths"))),
            "event_types": set(_strings(raw.get("event_types"))),
            "source_event_count": raw.get("source_event_count", 0),
        }
    return work_units


def _nearest_rank(values: Sequence[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(float(value) for value in values)
    rank = max(1, math.ceil(percentile * len(ordered)))
    return ordered[rank - 1]


def _distribution(values: Iterable[int | float | None]) -> dict[str, Any]:
    present = [float(value) for value in values if isinstance(value, (int, float))]
    if not present:
        return {
            "count": 0,
            "total": 0.0,
            "median": None,
            "p75": None,
            "p90": None,
        }
    return {
        "count": len(present),
        "total": sum(present),
        "median": float(median(present)),
        "p75": _nearest_rank(present, 0.75),
        "p90": _nearest_rank(present, 0.90),
    }


def _case_metric_value(case: Mapping[str, Any], key: str) -> int | float | None:
    accounting = case.get("accounting")
    accounting_map = accounting if isinstance(accounting, Mapping) else {}
    timing = case.get("timing")
    timing_map = timing if isinstance(timing, Mapping) else {}
    errors = case.get("errors")
    errors_map = errors if isinstance(errors, Mapping) else {}
    interventions = case.get("interventions")
    interventions_map = interventions if isinstance(interventions, Mapping) else {}
    manual_actions = case.get("manual_actions")
    manual_actions_map = manual_actions if isinstance(manual_actions, Mapping) else {}
    if key in {
        "lifecycle_wall_seconds",
        "atom_to_disposition_seconds",
        "admission_to_disposition_seconds",
        "lineage_to_disposition_seconds",
        "pr_create_to_outcome_seconds",
        "work_interval_union_seconds",
        "manual_active_seconds",
        "machine_wait_seconds",
        "external_wait_seconds",
        "queue_wait_seconds",
        "provider_wait_seconds",
        "ci_wait_seconds",
        "approval_wait_seconds",
        "categorized_external_wait_seconds",
        "unknown_wait_seconds",
        "unclassified_seconds",
    }:
        value = timing_map.get(key)
        return value if isinstance(value, (int, float)) else None
    if key == "error_clusters":
        value = errors_map.get("cluster_count")
        return value if isinstance(value, int) else None
    if key == "supervisor_interventions":
        value = interventions_map.get("count")
        return value if isinstance(value, int) else None
    if key == "manual_actions":
        value = manual_actions_map.get("count")
        return value if isinstance(value, int) else None
    for scope in ("direct", "inclusive", "all_in"):
        scope_raw = accounting_map.get(scope)
        scope_map = scope_raw if isinstance(scope_raw, Mapping) else {}
        gross_raw = scope_map.get("gross")
        gross = gross_raw if isinstance(gross_raw, Mapping) else {}
        if key == f"{scope}_total_tokens":
            value = gross.get("total_tokens")
            return value if isinstance(value, (int, float)) else None
        if key == f"{scope}_active_seconds":
            value = gross.get("active_seconds")
            return value if isinstance(value, (int, float)) else None
        if key == f"{scope}_accounted_resource_seconds":
            value = gross.get("accounted_resource_seconds")
            return value if isinstance(value, (int, float)) else None
        if key == f"{scope}_interval_union_seconds":
            value = gross.get("wall_clock_interval_union_seconds")
            return value if isinstance(value, (int, float)) else None
    return None


_DISTRIBUTION_METRICS = (
    "direct_total_tokens",
    "inclusive_total_tokens",
    "all_in_total_tokens",
    "lifecycle_wall_seconds",
    "atom_to_disposition_seconds",
    "admission_to_disposition_seconds",
    "lineage_to_disposition_seconds",
    "pr_create_to_outcome_seconds",
    "work_interval_union_seconds",
    "manual_active_seconds",
    "machine_wait_seconds",
    "external_wait_seconds",
    "queue_wait_seconds",
    "provider_wait_seconds",
    "ci_wait_seconds",
    "approval_wait_seconds",
    "categorized_external_wait_seconds",
    "unknown_wait_seconds",
    "unclassified_seconds",
    "direct_active_seconds",
    "inclusive_active_seconds",
    "all_in_active_seconds",
    "direct_accounted_resource_seconds",
    "inclusive_accounted_resource_seconds",
    "all_in_accounted_resource_seconds",
    "direct_interval_union_seconds",
    "inclusive_interval_union_seconds",
    "all_in_interval_union_seconds",
    "error_clusters",
    "supervisor_interventions",
    "manual_actions",
)


def _union_work_ids(cases: Sequence[Mapping[str, Any]], scope: str) -> set[str]:
    result: set[str] = set()
    for case in cases:
        accounting = case.get("accounting")
        if not isinstance(accounting, Mapping):
            continue
        scope_value = accounting.get(scope)
        if not isinstance(scope_value, Mapping):
            continue
        result.update(_strings(scope_value.get("work_unit_ids")))
    return result


def _aggregate_error_metrics(cases: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    totals: Counter[str] = Counter()
    for case in cases:
        errors = case.get("errors")
        if not isinstance(errors, Mapping):
            continue
        totals["cluster_count"] += int(errors.get("cluster_count", 0) or 0)
        totals["occurrence_count"] += int(errors.get("occurrence_count", 0) or 0)
        totals["self_healed_cluster_count"] += int(
            errors.get("self_healed_cluster_count", errors.get("self_healed", 0)) or 0
        )
        totals["externally_resolved_cluster_count"] += int(
            errors.get("externally_resolved_cluster_count", 0) or 0
        )
        totals["unresolved_terminal_cluster_count"] += int(
            errors.get("unresolved_terminal_cluster_count", errors.get("unresolved", 0)) or 0
        )
        totals["open_cluster_count"] += int(errors.get("open_cluster_count", 0) or 0)
        totals["tolerated_nonblocking_cluster_count"] += int(
            errors.get("tolerated_nonblocking_cluster_count", 0) or 0
        )
    return dict(totals)


def _aggregate_action_metrics(
    cases: Sequence[Mapping[str, Any]], field: str
) -> dict[str, Any]:
    totals: Counter[str] = Counter()
    known_totals: Counter[str] = Counter()
    known_by_actor: Counter[str] = Counter()
    active_seconds = 0.0
    incomplete_fields: set[str] = set()
    active_seconds_incomplete = False
    telemetry_incomplete_case_count = 0
    for case in cases:
        raw = case.get(field)
        if not isinstance(raw, Mapping):
            telemetry_incomplete_case_count += 1
            continue
        if raw.get("telemetry_complete") is False:
            telemetry_incomplete_case_count += 1
        for key in (
            "count",
            "avoidable_count",
            "unavoidable_count",
            "unclassified_count",
            "incomplete_count",
            "required_for_progress_count",
            "policy_mandated_count",
            "passive_observation_count",
            "measurement_administration_count",
        ):
            value = raw.get(key)
            if isinstance(value, int):
                totals[key] += value
                known_totals[key] += value
            else:
                incomplete_fields.add(key)
                known_value = raw.get(f"known_{key}")
                if isinstance(known_value, int):
                    known_totals[key] += known_value
        raw_by_actor = raw.get("by_actor")
        if isinstance(raw_by_actor, Mapping):
            for actor, count in raw_by_actor.items():
                totals[f"actor:{actor}"] += int(count or 0)
                known_by_actor[str(actor)] += int(count or 0)
        else:
            incomplete_fields.add("by_actor")
            raw_known_by_actor = raw.get("known_by_actor")
            if isinstance(raw_known_by_actor, Mapping):
                for actor, count in raw_known_by_actor.items():
                    known_by_actor[str(actor)] += int(count or 0)
        value = raw.get("active_seconds")
        if isinstance(value, (int, float)):
            active_seconds += float(value)
        else:
            active_seconds_incomplete = True
            known_active_seconds = raw.get("known_active_seconds")
            if isinstance(known_active_seconds, (int, float)):
                active_seconds += float(known_active_seconds)
    result: dict[str, Any] = dict(totals)
    result["by_actor"] = {
        key.removeprefix("actor:"): value
        for key, value in sorted(totals.items())
        if key.startswith("actor:")
    }
    for key in [key for key in result if key.startswith("actor:")]:
        del result[key]
    for key in incomplete_fields:
        if key == "by_actor":
            result["known_by_actor"] = dict(sorted(known_by_actor.items()))
            result["by_actor"] = None
        else:
            result[f"known_{key}"] = known_totals[key]
            result[key] = None
    result["known_active_seconds"] = active_seconds
    result["active_seconds"] = None if active_seconds_incomplete else active_seconds
    result["active_seconds_complete"] = not active_seconds_incomplete
    result["telemetry_complete"] = telemetry_incomplete_case_count == 0
    result["telemetry_incomplete_case_count"] = telemetry_incomplete_case_count
    return result


def _manual_work_summary(
    work_unit_ids: set[str], work_units: Mapping[str, dict[str, Any]]
) -> dict[str, Any]:
    manual_ids = sorted(
        work_unit_id
        for work_unit_id in work_unit_ids
        if work_units[work_unit_id].get("manual") is True
    )
    known_active_seconds = 0.0
    missing_active_seconds = 0
    by_actor: Counter[str] = Counter()
    by_token_scope: Counter[str] = Counter()
    for work_unit_id in manual_ids:
        work = work_units[work_unit_id]
        active_seconds = work.get("active_seconds")
        if isinstance(active_seconds, (int, float)):
            known_active_seconds += float(active_seconds)
        else:
            missing_active_seconds += 1
        actor_types = work.get("actor_types")
        actors = actor_types if isinstance(actor_types, set) else set()
        if actors:
            by_actor.update(str(actor) for actor in actors)
        else:
            by_actor["unknown"] += 1
        token_scope = _string(work.get("token_scope")) or "unclassified"
        by_token_scope[token_scope] += 1
    complete_active_seconds = missing_active_seconds == 0
    active_seconds_total = known_active_seconds if complete_active_seconds else None
    return {
        "work_unit_count": len(manual_ids),
        "work_unit_ids": manual_ids,
        "active_seconds": active_seconds_total,
        "active_minutes": (
            active_seconds_total / 60.0 if active_seconds_total is not None else None
        ),
        "known_active_seconds": known_active_seconds,
        "known_active_minutes": known_active_seconds / 60.0,
        "missing_active_seconds_work_units": missing_active_seconds,
        "active_seconds_complete": complete_active_seconds,
        "by_actor": dict(sorted(by_actor.items())),
        "by_token_scope": dict(sorted(by_token_scope.items())),
    }


def _automation_summary(cases: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    gross_scores: list[float] = []
    avoidable_scores: list[float] = []
    certified = 0
    withheld_reasons: Counter[str] = Counter()
    terminal_cases = 0
    touchless_terminal_cases = 0
    pipeline_autonomous_cases = 0
    human_touch_free_cases = 0
    rate_eligible_terminal_cases = 0
    for case in cases:
        raw = case.get("automation_score_v1")
        if not isinstance(raw, Mapping):
            continue
        if raw.get("certified") is True:
            certified += 1
        reasons = raw.get("withheld_reasons")
        if isinstance(reasons, list):
            withheld_reasons.update(str(reason) for reason in reasons)
        for field, target in (("gross", gross_scores), ("avoidable", avoidable_scores)):
            block = raw.get(field)
            if isinstance(block, Mapping):
                score = block.get("score")
                if isinstance(score, (int, float)):
                    target.append(float(score))

        if case.get("disposition") is None:
            continue
        terminal_cases += 1
        interventions = case.get("interventions")
        intervention_map = interventions if isinstance(interventions, Mapping) else {}
        manual_actions = case.get("manual_actions")
        manual_action_map = manual_actions if isinstance(manual_actions, Mapping) else {}
        gross = raw.get("gross")
        gross_map = gross if isinstance(gross, Mapping) else {}
        raw_intervention_count = intervention_map.get("count")
        raw_manual_action_count = manual_action_map.get("count")
        if not isinstance(raw_intervention_count, int) or not isinstance(
            raw_manual_action_count, int
        ):
            continue
        rate_eligible_terminal_cases += 1
        intervention_count = raw_intervention_count
        manual_action_count = raw_manual_action_count
        manual_milestones = int(gross_map.get("manual_milestones", 0) or 0)
        required_manual = int(
            intervention_map.get("required_for_progress_count", 0) or 0
        ) + int(manual_action_map.get("required_for_progress_count", 0) or 0)
        if intervention_count == 0 and manual_action_count == 0 and manual_milestones == 0:
            touchless_terminal_cases += 1
        if required_manual == 0 and manual_milestones == 0:
            pipeline_autonomous_cases += 1

        human_actions = 0
        for action_map in (intervention_map, manual_action_map):
            by_actor = action_map.get("by_actor")
            if isinstance(by_actor, Mapping):
                human_actions += int(by_actor.get("human", 0) or 0)
        human_manual_milestones = int(
            gross_map.get("human_manual_milestones", 0) or 0
        )
        supervisor_manual_milestones = int(
            gross_map.get("supervisor_manual_milestones", 0) or 0
        )
        unknown_manual_milestones = max(
            0,
            manual_milestones
            - human_manual_milestones
            - supervisor_manual_milestones,
        )
        if human_actions == 0 and human_manual_milestones == 0 and unknown_manual_milestones == 0:
            human_touch_free_cases += 1

    def terminal_rate(numerator: int) -> float | None:
        return (
            numerator / rate_eligible_terminal_cases
            if rate_eligible_terminal_cases
            else None
        )

    return {
        "version": AUTOMATION_SCORE_VERSION,
        "certified_case_count": certified,
        "withheld_case_count": len(cases) - certified,
        "withheld_reasons": dict(sorted(withheld_reasons.items())),
        "gross": _distribution(gross_scores),
        "avoidable": _distribution(avoidable_scores),
        "terminal_case_count": terminal_cases,
        "rate_eligible_terminal_case_count": rate_eligible_terminal_cases,
        "rate_ineligible_terminal_case_count": (
            terminal_cases - rate_eligible_terminal_cases
        ),
        "touchless_terminal_case_count": touchless_terminal_cases,
        "touchless_terminal_yield": terminal_rate(touchless_terminal_cases),
        "pipeline_autonomous_case_count": pipeline_autonomous_cases,
        "pipeline_autonomous_rate": terminal_rate(pipeline_autonomous_cases),
        "human_touch_free_case_count": human_touch_free_cases,
        "human_touch_free_rate": terminal_rate(human_touch_free_cases),
        "rate_denominator": "terminal_cases_with_complete_manual_action_counts",
    }


def _completeness_summary(cases: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    fields = (
        "lifecycle_opened_present",
        "origin_present",
        "origin_telemetry_known",
        "manual_action_telemetry_complete",
        "disposition_present",
        "disposition_verified",
        "system_fingerprint_present",
        "system_fingerprint_complete",
        "lifecycle_wall_time_present",
        "accounting_reconciled",
    )
    counts: dict[str, int] = {field: 0 for field in fields}
    complete_required_milestones = 0
    for case in cases:
        raw = case.get("completeness")
        if not isinstance(raw, Mapping):
            continue
        for field in fields:
            if raw.get(field) is True:
                counts[field] += 1
        present = raw.get("required_milestones_present")
        required = raw.get("required_milestones")
        if isinstance(present, int) and isinstance(required, int) and required > 0:
            if present == required:
                complete_required_milestones += 1
    case_count = len(cases)
    return {
        "case_count": case_count,
        "counts": {**counts, "required_milestones_complete": complete_required_milestones},
        "ratios": {
            **{
                field: (counts[field] / case_count) if case_count else 1.0
                for field in fields
            },
            "required_milestones_complete": (
                complete_required_milestones / case_count if case_count else 1.0
            ),
        },
        "complete": (
            case_count == 0
            or (
                all(counts[field] == case_count for field in fields)
                and complete_required_milestones == case_count
            )
        ),
    }


def _group_summary(
    cases: Sequence[Mapping[str, Any]],
    work_units: Mapping[str, dict[str, Any]],
) -> dict[str, Any]:
    accounting: dict[str, Any] = {}
    work_ids_by_scope: dict[str, set[str]] = {}
    missing_work_ids: set[str] = set()
    for scope in ("direct", "inclusive", "all_in"):
        work_ids = _union_work_ids(cases, scope)
        missing_work_ids.update(work_id for work_id in work_ids if work_id not in work_units)
        present_ids = {work_id for work_id in work_ids if work_id in work_units}
        work_ids_by_scope[scope] = present_ids
        accounting[scope] = _cost_for_work_ids(present_ids, work_units)
    distributions = {
        key: _distribution(_case_metric_value(case, key) for case in cases)
        for key in _DISTRIBUTION_METRICS
    }
    return {
        "case_count": len(cases),
        "case_lifecycle_ids": sorted(
            str(case.get("case_lifecycle_id") or case.get("case_id")) for case in cases
        ),
        "case_ids": sorted(
            {
                str(case.get("case_id"))
                for case in cases
                if case.get("case_id") is not None
            }
        ),
        "nonduplicative_accounting": accounting,
        "case_distributions": distributions,
        "errors": _aggregate_error_metrics(cases),
        "interventions": _aggregate_action_metrics(cases, "interventions"),
        "manual_actions": _aggregate_action_metrics(cases, "manual_actions"),
        "manual_work": _manual_work_summary(work_ids_by_scope["all_in"], work_units),
        "automation_score_v1": _automation_summary(cases),
        "completeness": _completeness_summary(cases),
        "missing_work_unit_ids": sorted(missing_work_ids),
    }


def aggregate_cohort_metrics(
    events_or_case_report: LifecycleSource | Mapping[str, Any],
    *,
    cohort_id: str | None = None,
    case_ids: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Aggregate a cohort, counting every referenced work unit at most once."""

    if (
        isinstance(events_or_case_report, Mapping)
        and events_or_case_report.get("metric_version") == CASE_METRICS_VERSION
        and isinstance(events_or_case_report.get("cases"), list)
    ):
        case_report = dict(events_or_case_report)
    else:
        case_report = aggregate_case_metrics(events_or_case_report)
    raw_cases = case_report.get("cases")
    all_cases = [
        case
        for case in (raw_cases if isinstance(raw_cases, list) else [])
        if isinstance(case, Mapping)
    ]
    selected_ids = set(case_ids) if case_ids is not None else None
    cohort_eligible_cases = [
        case for case in all_cases if case.get("cohort_eligible") is True
    ]
    excluded_cases = [
        case for case in all_cases if case.get("cohort_eligible") is not True
    ]
    selection_pool = all_cases if selected_ids is not None else cohort_eligible_cases
    cases = [
        case
        for case in selection_pool
        if selected_ids is None
        or _string(case.get("case_lifecycle_id")) in selected_ids
        or _string(case.get("case_id")) in selected_ids
    ]
    work_units = _rehydrate_work_units(case_report)
    overall = _group_summary(cases, work_units)
    raw_normalization = case_report.get("normalization")
    normalization = (
        dict(raw_normalization) if isinstance(raw_normalization, Mapping) else {}
    )

    disposition_groups: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    active_case_ids: list[str] = []
    for case in cases:
        disposition = _string(case.get("disposition"))
        if disposition is None:
            active_case_ids.append(
                str(case.get("case_lifecycle_id") or case.get("case_id"))
            )
        else:
            disposition_groups[disposition].append(case)
    by_disposition = {
        disposition: _group_summary(disposition_groups[disposition], work_units)
        for disposition in sorted(disposition_groups)
    }
    disposition_counts = {
        disposition: group["case_count"] for disposition, group in by_disposition.items()
    }

    fingerprints: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    fingerprint_values: dict[str, Any] = {}
    missing_fingerprint_case_ids: list[str] = []
    incomplete_fingerprint_case_ids: list[str] = []
    for case in cases:
        fingerprint_key = _string(case.get("system_fingerprint_key"))
        if fingerprint_key is None:
            missing_fingerprint_case_ids.append(
                str(case.get("case_lifecycle_id") or case.get("case_id"))
            )
        else:
            fingerprints[fingerprint_key].append(case)
            fingerprint_values[fingerprint_key] = case.get("system_fingerprint")
            completeness = case.get("completeness")
            if not (
                isinstance(completeness, Mapping)
                and completeness.get("system_fingerprint_complete") is True
            ):
                incomplete_fingerprint_case_ids.append(
                    str(case.get("case_lifecycle_id") or case.get("case_id"))
                )
    by_fingerprint: dict[str, Any] = {}
    for fingerprint in sorted(fingerprints):
        group = fingerprints[fingerprint]
        summary = _group_summary(group, work_units)
        summary["disposition_counts"] = dict(
            sorted(
                Counter(
                    str(case.get("disposition"))
                    for case in group
                    if case.get("disposition") is not None
                ).items()
            )
        )
        summary["system_fingerprint"] = fingerprint_values[fingerprint]
        by_fingerprint[fingerprint] = summary

    issues: list[dict[str, Any]] = []
    for case in cases:
        reconciliation = case.get("reconciliation")
        if not isinstance(reconciliation, Mapping):
            continue
        raw_issues = reconciliation.get("issues")
        if isinstance(raw_issues, list):
            for issue in raw_issues:
                if isinstance(issue, Mapping) and dict(issue) not in issues:
                    issues.append(dict(issue))
    for missing_id in overall["missing_work_unit_ids"]:
        _issue(issues, "cohort_work_unit_missing", work_unit_id=missing_id)

    accounting = overall["nonduplicative_accounting"]
    version_warnings: list[dict[str, Any]] = []
    if len(fingerprints) > 1:
        version_warnings.append(
            {
                "code": "mixed_system_fingerprints",
                "fingerprint_count": len(fingerprints),
                "system_fingerprint_keys": sorted(fingerprints),
            }
        )
    if missing_fingerprint_case_ids:
        version_warnings.append(
            {
                "code": "missing_system_fingerprints",
                "case_lifecycle_ids": sorted(missing_fingerprint_case_ids),
            }
        )
    if incomplete_fingerprint_case_ids:
        version_warnings.append(
            {
                "code": "incomplete_system_fingerprints",
                "case_lifecycle_ids": sorted(incomplete_fingerprint_case_ids),
            }
        )
    if excluded_cases:
        version_warnings.append(
            {
                "code": "case_cohort_eligibility_missing",
                "excluded_case_count": len(excluded_cases),
                "case_lifecycle_ids": sorted(
                    str(case.get("case_lifecycle_id") or case.get("case_id"))
                    for case in excluded_cases
                ),
            }
        )
    return {
        "schema_version": 1,
        "metric_version": CASE_METRICS_VERSION,
        "cohort_id": cohort_id or "cohort",
        "observed_case_count": len(all_cases),
        "case_count": len(cases),
        "excluded_case_count": len(excluded_cases),
        "excluded_case_lifecycle_ids": sorted(
            str(case.get("case_lifecycle_id") or case.get("case_id"))
            for case in excluded_cases
        ),
        "case_lifecycle_ids": sorted(
            str(case.get("case_lifecycle_id") or case.get("case_id")) for case in cases
        ),
        "case_ids": sorted(
            {
                str(case.get("case_id"))
                for case in cases
                if case.get("case_id") is not None
            }
        ),
        "active_case_count": len(active_case_ids),
        "active_case_lifecycle_ids": sorted(active_case_ids),
        "disposition_counts": disposition_counts,
        "accounting": accounting,
        "nonduplicative_accounting": accounting,
        "case_distributions": overall["case_distributions"],
        "errors": overall["errors"],
        "interventions": overall["interventions"],
        "manual_actions": overall["manual_actions"],
        "manual_work": overall["manual_work"],
        "automation_score_v1": overall["automation_score_v1"],
        "normalization": normalization,
        "by_disposition": by_disposition,
        "by_system_fingerprint": by_fingerprint,
        "system_fingerprints": [fingerprint_values[key] for key in sorted(fingerprints)],
        "system_fingerprint_keys": sorted(fingerprints),
        "missing_system_fingerprint_case_ids": sorted(missing_fingerprint_case_ids),
        "version_boundaries": {
            "mixed_system_fingerprints": len(fingerprints) > 1,
            "system_fingerprint_count": len(fingerprints),
            "missing_system_fingerprint_count": len(missing_fingerprint_case_ids),
            "incomplete_system_fingerprint_count": len(
                incomplete_fingerprint_case_ids
            ),
        },
        "version_warnings": version_warnings,
        "incomplete_system_fingerprint_case_ids": sorted(
            incomplete_fingerprint_case_ids
        ),
        "completeness": overall["completeness"],
        "reconciliation": {
            "ok": not issues,
            "issues": sorted(
                issues,
                key=lambda item: (
                    str(item.get("code", "")),
                    str(item.get("case_id", "")),
                    str(item.get("work_unit_id", "")),
                ),
            ),
        },
    }


def _numeric_delta(after: Any, before: Any) -> float | None:
    if isinstance(after, (int, float)) and isinstance(before, (int, float)):
        return float(after) - float(before)
    return None


def _accounting_delta(before: Mapping[str, Any], after: Mapping[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for scope in ("direct", "inclusive", "all_in"):
        before_scope = before.get(scope)
        after_scope = after.get(scope)
        before_map = before_scope if isinstance(before_scope, Mapping) else {}
        after_map = after_scope if isinstance(after_scope, Mapping) else {}
        scope_delta: dict[str, Any] = {}
        for cost_kind in ("gross", "avoidable"):
            before_cost = before_map.get(cost_kind)
            after_cost = after_map.get(cost_kind)
            before_cost_map = before_cost if isinstance(before_cost, Mapping) else {}
            after_cost_map = after_cost if isinstance(after_cost, Mapping) else {}
            scope_delta[cost_kind] = {
                "total_tokens": _numeric_delta(
                    after_cost_map.get("total_tokens"), before_cost_map.get("total_tokens")
                ),
                "active_seconds": _numeric_delta(
                    after_cost_map.get("active_seconds"), before_cost_map.get("active_seconds")
                ),
            }
        result[scope] = scope_delta
    return result


def _cohort(value: Any, *, default_id: str) -> dict[str, Any]:
    if (
        isinstance(value, Mapping)
        and value.get("metric_version") == CASE_METRICS_VERSION
        and "by_disposition" in value
    ):
        return dict(value)
    return aggregate_cohort_metrics(value, cohort_id=default_id)


def _nested_number(value: Mapping[str, Any], path: Sequence[str]) -> float | None:
    current: Any = value
    for part in path:
        if not isinstance(current, Mapping):
            return None
        current = current.get(part)
    return float(current) if isinstance(current, (int, float)) else None


def _metric_row(
    metric: str,
    before: float | None,
    after: float | None,
    *,
    objective: str,
) -> dict[str, Any]:
    absolute_delta = (
        after - before if before is not None and after is not None else None
    )
    percentage_delta: float | None = None
    if absolute_delta is not None and before is not None and before != 0.0:
        percentage_delta = 100.0 * absolute_delta / abs(float(before))
    if absolute_delta is None:
        observed_direction = "unknown"
    elif absolute_delta > 0:
        observed_direction = "increased"
    elif absolute_delta < 0:
        observed_direction = "decreased"
    else:
        observed_direction = "unchanged"
    if objective == "lower_is_better" and absolute_delta is not None:
        alignment = (
            "improved" if absolute_delta < 0 else "regressed" if absolute_delta > 0 else "unchanged"
        )
    elif objective == "higher_is_better" and absolute_delta is not None:
        alignment = (
            "improved" if absolute_delta > 0 else "regressed" if absolute_delta < 0 else "unchanged"
        )
    else:
        alignment = "not_scored"
    return {
        "metric": metric,
        "before": before,
        "after": after,
        "absolute_delta": absolute_delta,
        "percentage_delta": percentage_delta,
        "objective": objective,
        "observed_direction": observed_direction,
        "objective_alignment": alignment,
    }


def compare_cohorts(
    before: Any,
    after: Any,
    *,
    objectives: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Return factual before/after deltas without making a causal claim."""

    before_cohort = _cohort(before, default_id="before")
    after_cohort = _cohort(after, default_id="after")
    before_dispositions = before_cohort.get("disposition_counts")
    after_dispositions = after_cohort.get("disposition_counts")
    before_disp_map = before_dispositions if isinstance(before_dispositions, Mapping) else {}
    after_disp_map = after_dispositions if isinstance(after_dispositions, Mapping) else {}
    disposition_keys = sorted(set(before_disp_map) | set(after_disp_map))
    disposition_deltas = {
        str(key): int(after_disp_map.get(key, 0) or 0) - int(before_disp_map.get(key, 0) or 0)
        for key in disposition_keys
    }

    before_by_disposition = before_cohort.get("by_disposition")
    after_by_disposition = after_cohort.get("by_disposition")
    before_by = before_by_disposition if isinstance(before_by_disposition, Mapping) else {}
    after_by = after_by_disposition if isinstance(after_by_disposition, Mapping) else {}
    per_disposition: dict[str, Any] = {}
    for disposition in disposition_keys:
        before_group = before_by.get(disposition)
        after_group = after_by.get(disposition)
        before_group_map = before_group if isinstance(before_group, Mapping) else {}
        after_group_map = after_group if isinstance(after_group, Mapping) else {}
        before_accounting = before_group_map.get("nonduplicative_accounting")
        after_accounting = after_group_map.get("nonduplicative_accounting")
        per_disposition[str(disposition)] = {
            "before_case_count": int(before_group_map.get("case_count", 0) or 0),
            "after_case_count": int(after_group_map.get("case_count", 0) or 0),
            "case_count_delta": int(after_group_map.get("case_count", 0) or 0)
            - int(before_group_map.get("case_count", 0) or 0),
            "accounting_delta": _accounting_delta(
                before_accounting if isinstance(before_accounting, Mapping) else {},
                after_accounting if isinstance(after_accounting, Mapping) else {},
            ),
        }

    before_errors = before_cohort.get("errors")
    after_errors = after_cohort.get("errors")
    before_error_map = before_errors if isinstance(before_errors, Mapping) else {}
    after_error_map = after_errors if isinstance(after_errors, Mapping) else {}
    error_fields = (
        "cluster_count",
        "occurrence_count",
        "self_healed_cluster_count",
        "externally_resolved_cluster_count",
        "unresolved_terminal_cluster_count",
        "open_cluster_count",
        "tolerated_nonblocking_cluster_count",
    )
    error_deltas = {
        field: int(after_error_map.get(field, 0) or 0)
        - int(before_error_map.get(field, 0) or 0)
        for field in error_fields
    }

    before_interventions = before_cohort.get("interventions")
    after_interventions = after_cohort.get("interventions")
    before_intervention_map = (
        before_interventions if isinstance(before_interventions, Mapping) else {}
    )
    after_intervention_map = (
        after_interventions if isinstance(after_interventions, Mapping) else {}
    )
    before_manual = before_cohort.get("manual_actions")
    after_manual = after_cohort.get("manual_actions")
    before_manual_map = before_manual if isinstance(before_manual, Mapping) else {}
    after_manual_map = after_manual if isinstance(after_manual, Mapping) else {}
    before_auto = before_cohort.get("automation_score_v1")
    after_auto = after_cohort.get("automation_score_v1")
    before_auto_map = before_auto if isinstance(before_auto, Mapping) else {}
    after_auto_map = after_auto if isinstance(after_auto, Mapping) else {}

    automation_delta: dict[str, Any] = {
        "certified_case_count": int(after_auto_map.get("certified_case_count", 0) or 0)
        - int(before_auto_map.get("certified_case_count", 0) or 0),
        "withheld_case_count": int(after_auto_map.get("withheld_case_count", 0) or 0)
        - int(before_auto_map.get("withheld_case_count", 0) or 0),
    }
    for score_kind in ("gross", "avoidable"):
        before_score = before_auto_map.get(score_kind)
        after_score = after_auto_map.get(score_kind)
        before_score_map = before_score if isinstance(before_score, Mapping) else {}
        after_score_map = after_score if isinstance(after_score, Mapping) else {}
        automation_delta[score_kind] = {
            key: _numeric_delta(after_score_map.get(key), before_score_map.get(key))
            for key in ("median", "p75", "p90", "total")
        }

    before_accounting = before_cohort.get("accounting")
    after_accounting = after_cohort.get("accounting")
    before_fingerprints = sorted(_strings(before_cohort.get("system_fingerprint_keys")))
    after_fingerprints = sorted(_strings(after_cohort.get("system_fingerprint_keys")))
    before_missing_fingerprints = _strings(
        before_cohort.get("missing_system_fingerprint_case_ids")
    )
    after_missing_fingerprints = _strings(
        after_cohort.get("missing_system_fingerprint_case_ids")
    )
    fingerprint_complete = not before_missing_fingerprints and not after_missing_fingerprints
    single_fingerprint_each = len(before_fingerprints) == 1 and len(after_fingerprints) == 1

    default_objectives = {
        "case_count": "context_only",
        "accounting.direct.gross.total_tokens": "lower_is_better",
        "accounting.inclusive.gross.total_tokens": "lower_is_better",
        "accounting.all_in.gross.total_tokens": "lower_is_better",
        "accounting.direct.gross.active_seconds": "lower_is_better",
        "accounting.inclusive.gross.active_seconds": "lower_is_better",
        "accounting.all_in.gross.active_seconds": "lower_is_better",
        "accounting.all_in.gross.wall_clock_interval_union_seconds": "lower_is_better",
        "errors.cluster_count": "lower_is_better",
        "errors.self_healed_cluster_count": "context_only",
        "errors.externally_resolved_cluster_count": "lower_is_better",
        "errors.unresolved_terminal_cluster_count": "lower_is_better",
        "interventions.count": "lower_is_better",
        "manual_actions.count": "lower_is_better",
        "automation_score_v1.gross.median": "higher_is_better",
        "automation_score_v1.avoidable.median": "higher_is_better",
    }
    if objectives is not None:
        default_objectives.update(
            {
                str(metric): str(direction)
                for metric, direction in objectives.items()
                if direction in {"lower_is_better", "higher_is_better", "context_only"}
            }
        )
    metric_paths: dict[str, tuple[str, ...]] = {
        "case_count": ("case_count",),
        "accounting.direct.gross.total_tokens": (
            "accounting",
            "direct",
            "gross",
            "total_tokens",
        ),
        "accounting.inclusive.gross.total_tokens": (
            "accounting",
            "inclusive",
            "gross",
            "total_tokens",
        ),
        "accounting.all_in.gross.total_tokens": (
            "accounting",
            "all_in",
            "gross",
            "total_tokens",
        ),
        "accounting.direct.gross.active_seconds": (
            "accounting",
            "direct",
            "gross",
            "active_seconds",
        ),
        "accounting.inclusive.gross.active_seconds": (
            "accounting",
            "inclusive",
            "gross",
            "active_seconds",
        ),
        "accounting.all_in.gross.active_seconds": (
            "accounting",
            "all_in",
            "gross",
            "active_seconds",
        ),
        "accounting.all_in.gross.wall_clock_interval_union_seconds": (
            "accounting",
            "all_in",
            "gross",
            "wall_clock_interval_union_seconds",
        ),
        "errors.cluster_count": ("errors", "cluster_count"),
        "errors.self_healed_cluster_count": ("errors", "self_healed_cluster_count"),
        "errors.externally_resolved_cluster_count": (
            "errors",
            "externally_resolved_cluster_count",
        ),
        "errors.unresolved_terminal_cluster_count": (
            "errors",
            "unresolved_terminal_cluster_count",
        ),
        "interventions.count": ("interventions", "count"),
        "manual_actions.count": ("manual_actions", "count"),
        "automation_score_v1.gross.median": (
            "automation_score_v1",
            "gross",
            "median",
        ),
        "automation_score_v1.avoidable.median": (
            "automation_score_v1",
            "avoidable",
            "median",
        ),
    }
    factual_metric_rows = [
        _metric_row(
            metric,
            _nested_number(before_cohort, path),
            _nested_number(after_cohort, path),
            objective=default_objectives[metric],
        )
        for metric, path in metric_paths.items()
    ]

    return {
        "schema_version": 1,
        "metric_version": CASE_METRICS_VERSION,
        "comparison_kind": "factual_before_after",
        "interpretation": "Factual deltas only; no causal attribution is inferred.",
        "before": {
            "cohort_id": before_cohort.get("cohort_id"),
            "case_count": before_cohort.get("case_count"),
            "system_fingerprints": before_fingerprints,
            "system_fingerprint_values": before_cohort.get("system_fingerprints"),
            "completeness": before_cohort.get("completeness"),
        },
        "after": {
            "cohort_id": after_cohort.get("cohort_id"),
            "case_count": after_cohort.get("case_count"),
            "system_fingerprints": after_fingerprints,
            "system_fingerprint_values": after_cohort.get("system_fingerprints"),
            "completeness": after_cohort.get("completeness"),
        },
        "system_fingerprint_comparison": {
            "before": before_fingerprints,
            "after": after_fingerprints,
            "complete": fingerprint_complete,
            "single_fingerprint_each": single_fingerprint_each,
            "changed": (
                before_fingerprints[0] != after_fingerprints[0]
                if fingerprint_complete and single_fingerprint_each
                else None
            ),
            "before_missing_case_ids": sorted(before_missing_fingerprints),
            "after_missing_case_ids": sorted(after_missing_fingerprints),
        },
        "factual_deltas": {
            "case_count": int(after_cohort.get("case_count", 0) or 0)
            - int(before_cohort.get("case_count", 0) or 0),
            "disposition_counts": disposition_deltas,
            "accounting": _accounting_delta(
                before_accounting if isinstance(before_accounting, Mapping) else {},
                after_accounting if isinstance(after_accounting, Mapping) else {},
            ),
            "errors": error_deltas,
            "interventions": {
                "count": int(after_intervention_map.get("count", 0) or 0)
                - int(before_intervention_map.get("count", 0) or 0),
                "active_seconds": _numeric_delta(
                    after_intervention_map.get("active_seconds"),
                    before_intervention_map.get("active_seconds"),
                ),
            },
            "manual_actions": {
                "count": int(after_manual_map.get("count", 0) or 0)
                - int(before_manual_map.get("count", 0) or 0),
                "required_for_progress_count": int(
                    after_manual_map.get("required_for_progress_count", 0) or 0
                )
                - int(before_manual_map.get("required_for_progress_count", 0) or 0),
                "active_seconds": _numeric_delta(
                    after_manual_map.get("active_seconds"),
                    before_manual_map.get("active_seconds"),
                ),
            },
            "automation_score_v1": automation_delta,
        },
        "factual_metric_rows": factual_metric_rows,
        "per_disposition": per_disposition,
        "completeness": {
            "before": before_cohort.get("completeness"),
            "after": after_cohort.get("completeness"),
            "system_fingerprint_complete": fingerprint_complete,
            "before_reconciled": bool(
                isinstance(before_cohort.get("reconciliation"), Mapping)
                and before_cohort["reconciliation"].get("ok") is True
            ),
            "after_reconciled": bool(
                isinstance(after_cohort.get("reconciliation"), Mapping)
                and after_cohort["reconciliation"].get("ok") is True
            ),
        },
    }
