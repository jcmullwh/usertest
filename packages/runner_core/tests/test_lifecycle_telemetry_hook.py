from __future__ import annotations

import json
from pathlib import Path

from runner_core.lifecycle_telemetry import write_run_lifecycle_telemetry


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value), encoding="utf-8")


def test_runner_telemetry_counts_retry_once_and_writes_usage_receipts(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_json(
        run_dir / "run_meta.json",
        {
            "run_started_utc": "2026-07-21T12:00:00Z",
            "run_finished_utc": "2026-07-21T12:01:00Z",
            "run_wall_seconds": 60.0,
            "runner_implementation": {"head_commit": "abc123"},
        },
    )
    _write_json(run_dir / "report.json", {"kind": "ok"})
    first_raw = run_dir / "raw_events.attempt1.jsonl"
    first_raw.write_text(
        json.dumps(
            {
                "type": "turn.completed",
                "usage": {"input_tokens": 10, "output_tokens": 2},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    second_raw = run_dir / "raw_events.attempt2.jsonl"
    second_raw.write_text(
        json.dumps(
            {
                "type": "turn.completed",
                "usage": {"input_tokens": 20, "output_tokens": 4},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    _write_json(
        run_dir / "agent_attempts.json",
        {
            "attempts": [
                {
                    "attempt": 1,
                    "attempt_started_utc": "2026-07-21T12:00:05Z",
                    "attempt_finished_utc": "2026-07-21T12:00:20Z",
                    "attempt_wall_seconds": 15,
                    "agent_exec_wall_seconds": 12,
                    "exit_code": 1,
                    "failure_subtype": "provider_capacity",
                    "raw_events_path": first_raw.name,
                },
                {
                    "attempt": 2,
                    "attempt_started_utc": "2026-07-21T12:00:25Z",
                    "attempt_finished_utc": "2026-07-21T12:00:50Z",
                    "attempt_wall_seconds": 25,
                    "agent_exec_wall_seconds": 20,
                    "exit_code": 0,
                    "continued_session": False,
                    "raw_events_path": second_raw.name,
                },
            ]
        },
    )

    first = write_run_lifecycle_telemetry(
        run_dir=run_dir,
        agent="codex",
        model="gpt-5.6-sol",
        policy="write",
        parent_case_id="case-1",
        origin_stage="implementation",
        supervisor_instruction="retry with the retained evidence",
    )
    second = write_run_lifecycle_telemetry(
        run_dir=run_dir,
        agent="codex",
        model="gpt-5.6-sol",
        policy="write",
        parent_case_id="case-1",
        origin_stage="implementation",
        supervisor_instruction="retry with the retained evidence",
    )
    assert first["event_count"] == second["event_count"]
    assert len(first["usage_receipt_paths"]) == 2
    rows = [
        json.loads(line)
        for line in (run_dir / "lifecycle_events.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    types = [row["event_type"] for row in rows]
    assert types.count("model.invocation.completed") == 2
    assert types.count("error.occurred") == 1
    assert types.count("intervention.completed") == 1
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
    run_completion = next(
        row
        for row in rows
        if row["event_type"] == "work.completed"
        and row["attributes"].get("scope") == "pipeline"
    )
    assert run_completion["active_seconds"] is None
    assert run_completion["attributes"]["wall_clock_envelope_seconds"] == 60.0
    resolved = next(row for row in rows if row["event_type"] == "error.resolved")
    assert resolved["attributes"]["resolution_mode"] == "self_healed_controller"
    assert (run_dir / "lifecycle_manifest.json").is_file()
    assert (run_dir / "case_metrics.json").is_file()
    assert (run_dir / "cohort_metrics.json").is_file()
    case_metrics = json.loads((run_dir / "case_metrics.json").read_text(encoding="utf-8"))
    assert case_metrics["reconciliation"]["ok"] is True
    case = case_metrics["cases"][0]
    assert case["accounting"]["direct"]["gross"]["total_tokens"] == 36
    assert case["timing"]["work_interval_union_seconds"] == 60.0
