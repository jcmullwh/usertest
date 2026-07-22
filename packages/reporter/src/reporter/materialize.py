from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from reporter.case_metrics import (
    LifecycleMetricsError,
    aggregate_case_metrics,
    aggregate_cohort_metrics,
    compare_cohorts,
    load_lifecycle_events,
)

CASE_METRICS_FILENAME = "case_metrics.json"
COHORT_METRICS_FILENAME = "cohort_metrics.json"
COHORT_COMPARISON_FILENAME = "cohort_comparison.json"
LIFECYCLE_EVENTS_FILENAME = "lifecycle_events.jsonl"


@dataclass(frozen=True)
class MaterializedMetrics:
    case_metrics_path: Path
    cohort_metrics_path: Path
    comparison_path: Path | None
    source_event_count: int
    retained_event_count: int


def discover_lifecycle_event_logs(roots: Iterable[Path]) -> list[Path]:
    """Return all retained lifecycle streams below one or more roots.

    Discovery is deterministic and does not follow directory symlinks. Callers may
    instead pass authoritative stream paths directly when their controller already
    maintains a global event log.
    """

    discovered: set[Path] = set()
    for root in roots:
        candidate = root.resolve()
        if candidate.is_file():
            if candidate.name != LIFECYCLE_EVENTS_FILENAME:
                raise LifecycleMetricsError(
                    f"event source must be named {LIFECYCLE_EVENTS_FILENAME}: {candidate}"
                )
            discovered.add(candidate)
            continue
        if not candidate.exists():
            continue
        for path in candidate.rglob(LIFECYCLE_EVENTS_FILENAME):
            if path.is_file() and not path.is_symlink():
                discovered.add(path.resolve())
    return sorted(discovered, key=lambda item: item.as_posix().casefold())


def _canonical_event(event: Mapping[str, Any]) -> str:
    return json.dumps(
        event,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _event_id(event: Mapping[str, Any]) -> str | None:
    value = event.get("event_id")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _linked_source_event_id(event: Mapping[str, Any]) -> str | None:
    attributes = event.get("attributes")
    if not isinstance(attributes, Mapping):
        return None
    value = attributes.get("linked_source_event_id")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _data_through_at(events: Sequence[Mapping[str, Any]]) -> str | None:
    """Return the latest retained evidence timestamp without consulting the clock."""

    observed: list[datetime] = []
    for event in events:
        for field in ("recorded_at", "occurred_at", "ended_at", "started_at"):
            value = event.get(field)
            if not isinstance(value, str) or not value.strip():
                continue
            try:
                parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
            except ValueError:
                continue
            if parsed.tzinfo is not None:
                observed.append(parsed.astimezone(timezone.utc))
    if not observed:
        return None
    return max(observed).isoformat().replace("+00:00", "Z")


def reconcile_event_streams(sources: Sequence[Path]) -> tuple[list[dict[str, Any]], int]:
    """Merge streams without double-counting mirrored or linked events.

    Exact event-id replays are collapsed. A linked event supersedes its local source
    event because it carries the case beneficiaries and shared-work attribution that
    the source invocation could not know. Conflicting reuse of an event id is rejected
    rather than silently choosing one version.
    """

    loaded: list[dict[str, Any]] = []
    for source in sources:
        loaded.extend(load_lifecycle_events(source))

    by_id: dict[str, tuple[str, dict[str, Any]]] = {}
    without_id: list[dict[str, Any]] = []
    for event in loaded:
        event_id = _event_id(event)
        if event_id is None:
            without_id.append(event)
            continue
        canonical = _canonical_event(event)
        previous = by_id.get(event_id)
        if previous is not None and previous[0] != canonical:
            raise LifecycleMetricsError(
                f"conflicting lifecycle events share event_id {event_id!r}"
            )
        by_id[event_id] = (canonical, event)

    linked_source_ids = {
        linked
        for _, event in by_id.values()
        if (linked := _linked_source_event_id(event)) is not None
    }
    retained = [
        event
        for event_id, (_, event) in sorted(by_id.items())
        if event_id not in linked_source_ids
    ]
    # Events without an id cannot be safely collapsed; the aggregator will withhold
    # reconciliation/certification where the missing identity matters.
    retained.extend(without_id)
    retained.sort(
        key=lambda event: (
            str(event.get("occurred_at", event.get("timestamp", ""))),
            str(event.get("event_id", "")),
            _canonical_event(event),
        )
    )
    return retained, len(loaded)


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
        + "\n"
    ).encode("utf-8")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
    finally:
        try:
            temporary_path.unlink(missing_ok=True)
        except OSError:
            pass


def materialize_lifecycle_metrics(
    *,
    event_sources: Sequence[Path],
    output_dir: Path,
    cohort_id: str | None = None,
    case_lifecycle_ids: Iterable[str] | None = None,
    comparison_cohort: Mapping[str, Any] | Path | None = None,
) -> MaterializedMetrics:
    """Deterministically derive the authoritative case/cohort metric artifacts."""

    sources = [path.resolve() for path in event_sources]
    events, source_event_count = reconcile_event_streams(sources)
    case_report = aggregate_case_metrics(events)
    cohort_report = aggregate_cohort_metrics(
        case_report,
        cohort_id=cohort_id,
        case_ids=case_lifecycle_ids,
    )
    cohort_report["data_through_at"] = _data_through_at(events)

    output = output_dir.resolve()
    case_path = output / CASE_METRICS_FILENAME
    cohort_path = output / COHORT_METRICS_FILENAME
    _atomic_write_json(case_path, case_report)
    _atomic_write_json(cohort_path, cohort_report)

    comparison_path: Path | None = None
    if comparison_cohort is not None:
        if isinstance(comparison_cohort, Path):
            decoded = json.loads(comparison_cohort.read_text(encoding="utf-8"))
            if not isinstance(decoded, Mapping):
                raise LifecycleMetricsError("comparison cohort must be a JSON object")
            prior = decoded
        else:
            prior = comparison_cohort
        comparison = compare_cohorts(prior, cohort_report)
        comparison_path = output / COHORT_COMPARISON_FILENAME
        _atomic_write_json(comparison_path, comparison)

    return MaterializedMetrics(
        case_metrics_path=case_path,
        cohort_metrics_path=cohort_path,
        comparison_path=comparison_path,
        source_event_count=source_event_count,
        retained_event_count=len(events),
    )


__all__ = [
    "CASE_METRICS_FILENAME",
    "COHORT_COMPARISON_FILENAME",
    "COHORT_METRICS_FILENAME",
    "LIFECYCLE_EVENTS_FILENAME",
    "MaterializedMetrics",
    "discover_lifecycle_event_logs",
    "materialize_lifecycle_metrics",
    "reconcile_event_streams",
]
