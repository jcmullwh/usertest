from __future__ import annotations

import json
from pathlib import Path

import pytest

from reporter.case_metrics import LifecycleMetricsError
from reporter.materialize import (
    discover_lifecycle_event_logs,
    materialize_lifecycle_metrics,
    reconcile_event_streams,
)


def _write_events(path: Path, events: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(event, sort_keys=True) + "\n" for event in events),
        encoding="utf-8",
    )


def _event(event_id: str, event_type: str, at: str, **attributes: object) -> dict[str, object]:
    return {
        "event_id": event_id,
        "event_type": event_type,
        "occurred_at": at,
        "context": {
            "case_lifecycle_id": "life-1",
            "case_id": "case-1",
            "cycle_id": "cycle-1",
            "system_fingerprint": {"commit": "abc"},
        },
        "attributes": attributes,
    }


def test_discovery_and_materialization_are_deterministic(tmp_path: Path) -> None:
    stream = tmp_path / "runs" / "lifecycle_events.jsonl"
    events = [
        _event("open", "lifecycle.opened", "2026-07-01T00:00:00Z"),
        _event(
            "closed",
            "disposition.verified",
            "2026-07-01T00:01:00Z",
            disposition="already_addressed",
            verified=True,
        ),
    ]
    _write_events(stream, events)

    discovered = discover_lifecycle_event_logs([tmp_path])
    first = materialize_lifecycle_metrics(
        event_sources=discovered,
        output_dir=tmp_path / "metrics",
        cohort_id="cohort-a",
    )
    first_bytes = first.case_metrics_path.read_bytes()
    second = materialize_lifecycle_metrics(
        event_sources=discovered,
        output_dir=tmp_path / "metrics",
        cohort_id="cohort-a",
    )

    assert second.case_metrics_path.read_bytes() == first_bytes
    cohort = json.loads(second.cohort_metrics_path.read_text(encoding="utf-8"))
    assert cohort["cohort_id"] == "cohort-a"
    assert cohort["data_through_at"] == "2026-07-01T00:01:00Z"
    assert first.source_event_count == first.retained_event_count == 2


def test_reconciliation_deduplicates_mirrors_and_prefers_linked_event(tmp_path: Path) -> None:
    original = _event(
        "model-source",
        "model.invocation.completed",
        "2026-07-01T00:00:10Z",
        token_usage={"total_tokens": 10},
    )
    linked = _event(
        "model-linked",
        "model.invocation.completed",
        "2026-07-01T00:00:10Z",
        linked_source_event_id="model-source",
        token_usage={"total_tokens": 10},
    )
    first = tmp_path / "a" / "lifecycle_events.jsonl"
    second = tmp_path / "b" / "lifecycle_events.jsonl"
    _write_events(first, [original, linked])
    _write_events(second, [original])

    events, source_count = reconcile_event_streams([first, second])

    assert source_count == 3
    assert [event["event_id"] for event in events] == ["model-linked"]


def test_conflicting_event_id_fails_closed(tmp_path: Path) -> None:
    first = tmp_path / "a" / "lifecycle_events.jsonl"
    second = tmp_path / "b" / "lifecycle_events.jsonl"
    _write_events(first, [_event("same", "lifecycle.opened", "2026-07-01T00:00:00Z")])
    _write_events(
        second,
        [_event("same", "lifecycle.opened", "2026-07-01T00:00:01Z")],
    )

    with pytest.raises(LifecycleMetricsError, match="conflicting lifecycle events"):
        reconcile_event_streams([first, second])
