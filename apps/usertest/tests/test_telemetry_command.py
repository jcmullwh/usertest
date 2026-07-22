from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from run_artifacts.lifecycle_events import TelemetryArtifactError

from usertest.cli import main


def _invoke(argv: list[str]) -> int:
    with pytest.raises(SystemExit) as exc:
        main(argv)
    return int(exc.value.code)


def test_telemetry_exec_records_redacted_unknown_boundary(tmp_path: Path) -> None:
    events = tmp_path / "lifecycle_events.jsonl"
    code = _invoke(
        [
            "telemetry",
            "exec",
            "--events",
            str(events),
            "--case-id",
            "case-1",
            "--",
            sys.executable,
            "-c",
            "print('ok')",
            "--api-key=sk-this-must-never-be-retained",
        ]
    )
    assert code == 0
    rows = [json.loads(line) for line in events.read_text(encoding="utf-8").splitlines()]
    assert [row["event_type"] for row in rows] == ["action.started", "action.completed"]
    assert rows[0]["context"]["work_unit_id"] == rows[1]["context"]["work_unit_id"]
    assert all(row["origin"] == "unknown_external" for row in rows)
    serialized = json.dumps(rows)
    assert "sk-this-must-never-be-retained" not in serialized
    assert "redacted" in serialized.casefold()
    assert (tmp_path / "case_metrics.json").is_file()
    assert (tmp_path / "cohort_metrics.json").is_file()


def test_telemetry_action_record_retains_manual_burden(tmp_path: Path) -> None:
    events = tmp_path / "lifecycle_events.jsonl"
    code = _invoke(
        [
            "telemetry",
            "action",
            "record",
            "--events",
            str(events),
            "--case-lifecycle-id",
            "life-1",
            "--case-id",
            "case-1",
            "--actor",
            "supervising_agent",
            "--action-family",
            "pull_request",
            "--operation",
            "create_pr_in_ui",
            "--interface",
            "browser",
            "--started-at",
            "2026-07-21T12:00:00Z",
            "--active-seconds",
            "90",
            "--external-wait-seconds",
            "30",
            "--wait-category",
            "approval",
            "--dependency-work-unit-id",
            "qualification-shared-1",
            "--all-in-dependency-work-unit-id",
            "controller-repair-1",
            "--result",
            "created",
        ]
    )
    assert code == 0
    row = json.loads(events.read_text(encoding="utf-8"))
    assert row["actor_type"] == "supervising_agent"
    assert row["origin"] == "supervising_agent"
    assert row["active_seconds"] == 90
    assert row["context"]["work_unit_id"].startswith("work:")
    assert row["external_wait_seconds"] == 30
    assert row["attributes"]["action_family"] == "pull_request"
    assert row["attributes"]["required_for_progress"] is True
    assert row["attributes"]["wait_seconds_by_category"] == {"approval": 30.0}
    assert row["attributes"]["dependency_ids"] == ["qualification-shared-1"]
    assert row["attributes"]["all_in_dependency_ids"] == ["controller-repair-1"]


def test_telemetry_materialize_discovers_streams_and_writes_comparison(
    tmp_path: Path,
) -> None:
    events = tmp_path / "runs" / "one" / "lifecycle_events.jsonl"
    assert (
        _invoke(
            [
                "telemetry",
                "action",
                "record",
                "--events",
                str(events),
                "--case-lifecycle-id",
                "life-1",
                "--case-id",
                "case-1",
                "--actor",
                "human",
                "--started-at",
                "2026-07-21T12:00:00Z",
                "--result",
                "observed",
            ]
        )
        == 0
    )
    prior = events.parent / "cohort_metrics.json"
    output = tmp_path / "published"

    assert (
        _invoke(
            [
                "telemetry",
                "materialize",
                "--discover-root",
                str(tmp_path / "runs"),
                "--output-dir",
                str(output),
                "--cohort-id",
                "current",
                "--compare-to",
                str(prior),
            ]
        )
        == 0
    )
    assert (output / "case_metrics.json").is_file()
    cohort = json.loads((output / "cohort_metrics.json").read_text(encoding="utf-8"))
    assert cohort["cohort_id"] == "current"
    assert (output / "cohort_comparison.json").is_file()


def test_telemetry_validate_rejects_malformed_stream(tmp_path: Path) -> None:
    events = tmp_path / "lifecycle_events.jsonl"
    events.write_text("{}\n", encoding="utf-8")
    with pytest.raises(TelemetryArtifactError):
        _invoke(["telemetry", "validate", "--events", str(events)])
