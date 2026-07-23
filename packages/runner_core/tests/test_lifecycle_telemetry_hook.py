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
        case_lifecycle_id="case-lifecycle-1",
        origin_stage="implementation",
        supervisor_instruction="retry with the retained evidence",
    )
    second = write_run_lifecycle_telemetry(
        run_dir=run_dir,
        agent="codex",
        model="gpt-5.6-sol",
        policy="write",
        parent_case_id="case-1",
        case_lifecycle_id="case-lifecycle-1",
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
    assert {row["context"]["case_lifecycle_id"] for row in rows} == {"case-lifecycle-1"}
    assert types.count("model.invocation.completed") == 2
    assert types.count("error.occurred") == 1
    assert types.count("intervention.completed") == 1
    invocation_work_ids = {
        row["context"]["work_unit_id"]
        for row in rows
        if row["event_type"] == "model.invocation.completed"
    }
    assert len(invocation_work_ids) == 2
    intervention = next(row for row in rows if row["event_type"] == "intervention.completed")
    assert intervention["context"]["work_unit_id"] not in invocation_work_ids
    run_completion = next(
        row
        for row in rows
        if row["event_type"] == "work.completed" and row["attributes"].get("scope") == "pipeline"
    )
    assert run_completion["active_seconds"] is None
    assert run_completion["attributes"]["wall_clock_envelope_seconds"] == 60.0
    resolved = next(row for row in rows if row["event_type"] == "error.resolved")
    assert resolved["attributes"]["resolution_mode"] == "self_healed_controller"
    assert (run_dir / "lifecycle_manifest.json").is_file()
    assert (run_dir / "case_metrics.json").is_file()
    assert (run_dir / "cohort_metrics.json").is_file()
    case_metrics = json.loads((run_dir / "case_metrics.json").read_text(encoding="utf-8"))
    assert case_metrics["reconciliation"]["ok"] is False
    assert {
        issue["code"] for issue in case_metrics["reconciliation"]["issues"]
    } == {"supervising_agent_tokens_missing"}
    case = case_metrics["cases"][0]
    assert case["accounting"]["direct"]["gross"]["total_tokens"] == 36
    assert case["accounting"]["all_in"]["gross"]["total_tokens"] is None
    assert case["timing"]["work_interval_union_seconds"] == 60.0


def test_runner_telemetry_separates_recurrent_failure_episodes(tmp_path: Path) -> None:
    run_dir = tmp_path / "recurrent-failure-run"
    run_dir.mkdir()
    _write_json(
        run_dir / "run_meta.json",
        {
            "run_started_utc": "2026-07-21T12:00:00Z",
            "run_finished_utc": "2026-07-21T12:01:00Z",
            "run_wall_seconds": 60.0,
        },
    )
    _write_json(run_dir / "report.json", {"kind": "retained-partial-report"})
    attempts: list[dict[str, object]] = []
    for attempt, exit_code in ((1, 1), (2, 0), (3, 1)):
        raw_path = run_dir / f"raw_events.attempt{attempt}.jsonl"
        raw_path.write_text(
            json.dumps(
                {
                    "type": "turn.completed",
                    "usage": {
                        "input_tokens": 10 * attempt,
                        "output_tokens": 2 * attempt,
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )
        attempts.append(
            {
                "attempt": attempt,
                "attempt_started_utc": f"2026-07-21T12:00:{attempt * 10 - 9:02d}Z",
                "attempt_finished_utc": f"2026-07-21T12:00:{attempt * 10:02d}Z",
                "attempt_wall_seconds": 9,
                "agent_exec_wall_seconds": 8,
                "exit_code": exit_code,
                "failure_subtype": "provider_capacity" if exit_code else None,
                "raw_events_path": raw_path.name,
            }
        )
    _write_json(run_dir / "agent_attempts.json", {"attempts": attempts})

    write_run_lifecycle_telemetry(
        run_dir=run_dir,
        agent="codex",
        model="gpt-5.6-sol",
        policy="write",
        parent_case_id="case-recurrent-error",
        case_lifecycle_id="lifecycle-recurrent-error",
        origin_stage="implementation",
        supervisor_instruction=None,
    )

    rows = [
        json.loads(line)
        for line in (run_dir / "lifecycle_events.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    occurrences = [row for row in rows if row["event_type"] == "error.occurred"]
    resolutions = [row for row in rows if row["event_type"] == "error.resolved"]
    assert len(occurrences) == 2
    assert occurrences[0]["error_cluster_id"] != occurrences[1]["error_cluster_id"]
    assert {
        row["attributes"]["resolution_mode"] for row in resolutions
    } == {"self_healed_controller", "unresolved_terminal"}

    case = json.loads((run_dir / "case_metrics.json").read_text(encoding="utf-8"))["cases"][0]
    assert case["errors"]["cluster_count"] == 2
    assert case["errors"]["self_healed_cluster_count"] == 1
    assert case["errors"]["unresolved_terminal_cluster_count"] == 1


def _write_single_codex_run(
    run_dir: Path,
    *,
    session_id: str,
    usage: dict[str, int],
    continued: bool,
    started_at: str,
    ended_at: str,
    source_run_dir: Path | None = None,
) -> dict[str, object]:
    run_dir.mkdir()
    _write_json(
        run_dir / "run_meta.json",
        {
            "run_started_utc": started_at,
            "run_finished_utc": ended_at,
            "run_wall_seconds": 10.0,
            "runner_implementation": {"head_commit": "abc123"},
        },
    )
    _write_json(run_dir / "report.json", {"kind": "ok"})
    raw_path = run_dir / "raw_events.attempt1.jsonl"
    raw_path.write_text(
        json.dumps({"type": "turn.completed", "usage": usage}) + "\n",
        encoding="utf-8",
    )
    _write_json(
        run_dir / "agent_attempts.json",
        {
            "attempts": [
                {
                    "attempt": 1,
                    "attempt_started_utc": started_at,
                    "attempt_finished_utc": ended_at,
                    "attempt_wall_seconds": 10.0,
                    "agent_exec_wall_seconds": 9.0,
                    "exit_code": 0,
                    "agent_session_id": session_id,
                    "continued_session": continued,
                    "raw_events_path": raw_path.name,
                }
            ]
        },
    )
    return write_run_lifecycle_telemetry(
        run_dir=run_dir,
        agent="codex",
        model="gpt-5.6-sol",
        policy="write",
        parent_case_id="case-cumulative",
        origin_stage="repro_research",
        supervisor_instruction=None,
        codex_resume_session_id=session_id if continued else None,
        codex_resume_usage_source_run_dir=source_run_dir,
    )


def _completed_model_event(run_dir: Path) -> dict[str, object]:
    return next(
        json.loads(line)
        for line in (run_dir / "lifecycle_events.jsonl").read_text(encoding="utf-8").splitlines()
        if json.loads(line)["event_type"] == "model.invocation.completed"
    )


def test_runner_telemetry_deltas_resumed_session_from_retained_predecessor(
    tmp_path: Path,
) -> None:
    session_id = "019f8934-cdb5-70f3-806a-1c5748f385f7"
    prior_run = tmp_path / "prior"
    current_run = tmp_path / "current"
    _write_single_codex_run(
        prior_run,
        session_id=session_id,
        usage={"input_tokens": 100, "cached_input_tokens": 60, "output_tokens": 20},
        continued=False,
        started_at="2026-07-21T12:00:00Z",
        ended_at="2026-07-21T12:00:10Z",
    )

    _write_single_codex_run(
        current_run,
        session_id=session_id,
        usage={"input_tokens": 145, "cached_input_tokens": 90, "output_tokens": 32},
        continued=True,
        started_at="2026-07-21T12:01:00Z",
        ended_at="2026-07-21T12:01:10Z",
        source_run_dir=prior_run,
    )

    event = _completed_model_event(current_run)
    assert event["attributes"]["usage_semantics"] == "session_cumulative"
    assert event["attributes"]["token_usage"] == {
        "total_tokens": 57,
        "input_tokens": 45,
        "cached_input_tokens": 30,
        "uncached_input_tokens": 15,
        "output_tokens": 12,
        "reasoning_output_tokens": 0,
    }
    assert event["attributes"]["usage_unknown_reason"] is None
    assert len(event["evidence_paths"]) == 2


def test_runner_telemetry_withholds_resumed_session_without_predecessor_high_water(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "continued-without-baseline"
    result = _write_single_codex_run(
        run_dir,
        session_id="019f8934-cdb5-70f3-806a-1c5748f385f7",
        usage={"input_tokens": 145, "cached_input_tokens": 90, "output_tokens": 32},
        continued=True,
        started_at="2026-07-21T12:01:00Z",
        ended_at="2026-07-21T12:01:10Z",
    )

    event = _completed_model_event(run_dir)
    assert event["attributes"]["usage_semantics"] == "unattributable"
    assert event["attributes"]["token_usage"] is None
    assert (
        event["attributes"]["usage_unknown_reason"] == "continued_session_missing_prior_high_water"
    )
    receipt_path = run_dir / str(result["usage_receipt_paths"][0])
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["observed_usage"]["total_tokens"] == 177
    case_metrics = json.loads((run_dir / "case_metrics.json").read_text(encoding="utf-8"))
    assert case_metrics["reconciliation"]["ok"] is False
    assert case_metrics["cases"][0]["accounting"]["direct"]["gross"]["total_tokens"] is None


def test_runner_telemetry_rejects_unattributable_predecessor_high_water(
    tmp_path: Path,
) -> None:
    session_id = "019f8934-cdb5-70f3-806a-1c5748f385f7"
    invalid_prior = tmp_path / "invalid-prior"
    current_run = tmp_path / "current"
    _write_single_codex_run(
        invalid_prior,
        session_id=session_id,
        usage={"input_tokens": 100, "cached_input_tokens": 60, "output_tokens": 20},
        continued=True,
        started_at="2026-07-21T12:00:00Z",
        ended_at="2026-07-21T12:00:10Z",
    )

    _write_single_codex_run(
        current_run,
        session_id=session_id,
        usage={"input_tokens": 145, "cached_input_tokens": 90, "output_tokens": 32},
        continued=True,
        started_at="2026-07-21T12:01:00Z",
        ended_at="2026-07-21T12:01:10Z",
        source_run_dir=invalid_prior,
    )

    event = _completed_model_event(current_run)
    assert event["attributes"]["usage_semantics"] == "unattributable"
    assert event["attributes"]["token_usage"] is None
    assert (
        event["attributes"]["usage_unknown_reason"]
        == "continued_session_missing_prior_high_water"
    )


def test_runner_telemetry_does_not_promote_unattributable_attempt_to_baseline(
    tmp_path: Path,
) -> None:
    session_id = "019f8934-cdb5-70f3-806a-1c5748f385f7"
    run_dir = tmp_path / "same-run-retry"
    run_dir.mkdir()
    _write_json(
        run_dir / "run_meta.json",
        {
            "run_started_utc": "2026-07-21T12:00:00Z",
            "run_finished_utc": "2026-07-21T12:01:00Z",
        },
    )
    _write_json(run_dir / "report.json", {"kind": "ok"})
    first_raw = run_dir / "raw_events.attempt1.jsonl"
    first_raw.write_text(
        "{malformed\n"
        + json.dumps(
            {
                "type": "turn.completed",
                "usage": {
                    "input_tokens": 100,
                    "cached_input_tokens": 60,
                    "output_tokens": 20,
                },
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
                "usage": {
                    "input_tokens": 145,
                    "cached_input_tokens": 90,
                    "output_tokens": 32,
                },
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
                    "attempt_started_utc": "2026-07-21T12:00:00Z",
                    "attempt_finished_utc": "2026-07-21T12:00:20Z",
                    "exit_code": 1,
                    "agent_session_id": session_id,
                    "continued_session": False,
                    "raw_events_path": first_raw.name,
                },
                {
                    "attempt": 2,
                    "attempt_started_utc": "2026-07-21T12:00:21Z",
                    "attempt_finished_utc": "2026-07-21T12:00:40Z",
                    "exit_code": 0,
                    "agent_session_id": session_id,
                    "continued_session": True,
                    "raw_events_path": second_raw.name,
                },
            ]
        },
    )

    write_run_lifecycle_telemetry(
        run_dir=run_dir,
        agent="codex",
        model="gpt-5.6-sol",
        policy="write",
        parent_case_id="case-cumulative",
        origin_stage="implementation",
        supervisor_instruction=None,
    )

    completed = [
        json.loads(line)
        for line in (run_dir / "lifecycle_events.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if json.loads(line)["event_type"] == "model.invocation.completed"
    ]
    assert [event["attributes"]["usage_semantics"] for event in completed] == [
        "unattributable",
        "unattributable",
    ]
    assert completed[1]["attributes"]["token_usage"] is None
