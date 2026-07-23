from __future__ import annotations

import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path


def _module():
    path = Path(__file__).resolve().parents[1] / "backfill_gpt56_lifecycle_metrics.py"
    spec = importlib.util.spec_from_file_location("backfill_gpt56_lifecycle_metrics", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_backfill_preserves_unknowns_and_structured_clusters(tmp_path: Path) -> None:
    mod = _module()
    dashboard_path = tmp_path / "dashboard.json"
    dashboard_path.write_text(
        json.dumps(
            {
                "operational_dashboard": {
                    "schema_version": 3,
                    "as_of": "2026-07-21T13:00:00Z",
                    "runs": [
                        {
                            "run_id": "case-1",
                            "entry_kind": "lifecycle_run",
                            "lifecycle_id": "life-1",
                            "lifecycle_kind": "case",
                            "timing": {
                                "start_at": "2026-07-21T12:00:00Z",
                                "end_at": "2026-07-21T12:10:00Z",
                            },
                            "errors": {"cluster_ids": ["error-1"]},
                            "automatic_self_corrections": {
                                "cluster_ids": ["error-1"]
                            },
                            "supervisor_interventions": {
                                "cluster_ids": ["intervention-1"]
                            },
                            "rework": {"author_invocations": 2},
                        }
                    ],
                }
            }
        ),
        encoding="utf-8",
    )
    output_dir = tmp_path / "out"
    manifest = mod.backfill_dashboard(
        dashboard_path=dashboard_path,
        output_dir=output_dir,
        since=datetime(2026, 7, 20, tzinfo=timezone.utc),
    )
    rows = [
        json.loads(line)
        for line in (output_dir / "lifecycle_events.jsonl").read_text(
            encoding="utf-8"
        ).splitlines()
    ]
    first_event_stream = (output_dir / "lifecycle_events.jsonl").read_bytes()
    assert len([row for row in rows if row["event_type"] == "model.invocation.completed"]) == 2
    invocation_work_ids = {
        row["context"]["work_unit_id"]
        for row in rows
        if row["event_type"] == "model.invocation.completed"
    }
    assert len(invocation_work_ids) == 2
    intervention = next(
        row for row in rows if row["event_type"] == "intervention.completed"
    )
    assert intervention["context"]["work_unit_id"] not in invocation_work_ids
    action = next(row for row in rows if row["event_type"] == "action.completed")
    assert action["intervention_id"] == intervention["intervention_id"]
    assert action["context"]["work_unit_id"] == intervention["context"]["work_unit_id"]
    assert action["attributes"]["legacy_action_cardinality"] == (
        "minimum_one_action_per_intervention"
    )
    assert action["attributes"]["resource_time_unknown"] is True
    assert next(row for row in rows if row["event_type"] == "error.occurred")[
        "attributes"
    ]["legacy_self_healed"] is True
    resolution = next(row for row in rows if row["event_type"] == "error.resolved")
    assert resolution["attributes"]["resolution_mode"] == "self_healed_same_author"
    assert manifest["selected_runs"][0]["provenance"]["tokens"] == "unknown"
    assert manifest["selected_runs"][0]["provenance"]["manual_actions"] == (
        "operator_attested_minimum"
    )
    assert manifest["selected_runs"][0]["provenance"]["disposition"] == "unknown"
    assert manifest["certification"]["eligible"] is False
    closed = next(row for row in rows if row["event_type"] == "lifecycle.closed")
    assert closed["attributes"]["cost_unknown"] is True
    assert closed["attributes"]["lifecycle_kind"] == "case"
    assert closed["attributes"]["case_cohort_eligible"] is True

    # Re-imports rebuild deterministically instead of retaining stale events.
    second = mod.backfill_dashboard(
        dashboard_path=dashboard_path,
        output_dir=output_dir,
        since=datetime(2026, 7, 20, tzinfo=timezone.utc),
    )
    rerun_rows = (output_dir / "lifecycle_events.jsonl").read_text(
        encoding="utf-8"
    ).splitlines()
    assert len(rerun_rows) == len(rows)
    assert (output_dir / "lifecycle_events.jsonl").read_bytes() == first_event_stream
    assert second["content_sha256"] == manifest["content_sha256"]


def test_backfill_derives_case_dispositions_and_preserves_count_only_measures(
    tmp_path: Path,
) -> None:
    mod = _module()
    dashboard_path = tmp_path / "dashboard.json"
    dashboard_path.write_text(
        json.dumps(
            {
                "sources": [
                    {
                        "id": "pr-source",
                        "url": "https://github.com/example/repo/pull/10",
                    }
                ],
                "operational_dashboard": {
                    "schema_version": 3,
                    "as_of": "2026-07-21T13:00:00Z",
                    "runs": [
                        {
                            "run_id": "pr-case",
                            "entry_kind": "lifecycle_run",
                            "lifecycle_id": "life-pr",
                            "lifecycle_kind": "case",
                            "current_state": "Merged with terminal outcome evidence.",
                            "source_ids": ["pr-source"],
                            "timing": {
                                "start_at": "2026-07-21T10:00:00Z",
                                "end_at": "2026-07-21T11:00:00Z",
                            },
                            "errors": {"count": 2},
                            "automatic_self_corrections": {"count": 0},
                            "supervisor_interventions": {"count": 3},
                            "rework": {"author_invocations": 1},
                        },
                        {
                            "run_id": "addressed-case",
                            "entry_kind": "lifecycle_run",
                            "lifecycle_id": "life-addressed",
                            "lifecycle_kind": "case",
                            "current_state": "Stage 4 persisted not_required/already_addressed.",
                            "source_ids": ["ledger"],
                            "timing": {
                                "start_at": "2026-07-21T11:00:00Z",
                                "end_at": "2026-07-21T11:10:00Z",
                            },
                            "errors": {"count": 0},
                            "automatic_self_corrections": {"count": 0},
                            "supervisor_interventions": {"count": 0},
                            "rework": {"author_invocations": 1},
                        },
                        {
                            "run_id": "non-actionable-case",
                            "entry_kind": "lifecycle_run",
                            "lifecycle_id": "life-non-actionable",
                            "lifecycle_kind": "case",
                            "current_state": "Stage 4 persisted not_required/non_actionable.",
                            "source_ids": ["ledger"],
                            "timing": {
                                "start_at": "2026-07-21T11:10:00Z",
                                "end_at": "2026-07-21T11:20:00Z",
                            },
                            "errors": {"count": 0},
                            "automatic_self_corrections": {"count": 0},
                            "supervisor_interventions": {"count": 0},
                            "rework": {"author_invocations": 1},
                        },
                    ],
                },
            }
        ),
        encoding="utf-8",
    )
    output_dir = tmp_path / "out"

    mod.backfill_dashboard(dashboard_path=dashboard_path, output_dir=output_dir)

    rows = [
        json.loads(line)
        for line in (output_dir / "lifecycle_events.jsonl").read_text(
            encoding="utf-8"
        ).splitlines()
    ]
    dispositions = {
        row["context"]["case_lifecycle_id"]: row["attributes"]["disposition"]
        for row in rows
        if row["event_type"] == "disposition.verified"
    }
    assert dispositions == {
        "legacy:life-addressed": "already_addressed",
        "legacy:life-non-actionable": "non_actionable",
        "legacy:life-pr": "pr",
    }
    pr_errors = [
        row
        for row in rows
        if row["event_type"] == "error.occurred"
        and row["context"]["case_lifecycle_id"] == "legacy:life-pr"
    ]
    pr_interventions = [
        row
        for row in rows
        if row["event_type"] == "intervention.completed"
        and row["context"]["case_lifecycle_id"] == "legacy:life-pr"
    ]
    pr_actions = [
        row
        for row in rows
        if row["event_type"] == "action.completed"
        and row["context"]["case_lifecycle_id"] == "legacy:life-pr"
    ]
    assert len(pr_errors) == 2
    assert len(pr_interventions) == 3
    assert len(pr_actions) == 3
    assert {row["intervention_id"] for row in pr_actions} == {
        row["intervention_id"] for row in pr_interventions
    }
