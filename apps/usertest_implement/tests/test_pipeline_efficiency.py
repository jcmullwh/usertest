from __future__ import annotations

import json
from pathlib import Path

from usertest_implement.pipeline_efficiency import build_ticket_pipeline_efficiency


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _write_attempts(
    run_dir: Path,
    *,
    started: str,
    model: str,
    followup: bool = False,
    attempt_count: int = 1,
) -> None:
    attempts = []
    for index in range(attempt_count):
        raw_name = f"raw_events.attempt{index + 1}.jsonl"
        (run_dir / raw_name).write_text(
            json.dumps({"type": "turn.started"}) + "\n",
            encoding="utf-8",
        )
        attempt = {
            "attempt": index + 1,
            "attempt_started_utc": started,
            "attempt_finished_utc": started,
            "argv": ["codex", "--model", model],
            "agent_session_id": f"session-{run_dir.name}",
            "raw_events_path": raw_name,
        }
        if followup and index == 0:
            attempt["followup_scheduled"] = True
        attempts.append(attempt)
    _write_json(run_dir / "agent_attempts.json", {"attempts": attempts})


def test_efficiency_follows_durable_lineage_without_guessing_unknown_origins(
    tmp_path: Path,
) -> None:
    initial = tmp_path / "initial"
    resumed = tmp_path / "resumed"
    adopted = tmp_path / "adopted"
    prior_review = tmp_path / "prior-review"
    final_review = tmp_path / "final-review"

    _write_json(
        initial / "run_meta.json",
        {
            "run_started_utc": "2026-01-01T00:00:00Z",
            "run_finished_utc": "2026-01-01T00:10:00Z",
        },
    )
    _write_json(initial / "target_ref.json", {"agent": "codex", "model": "model-a"})
    _write_attempts(
        initial,
        started="2026-01-01T00:01:00Z",
        model="model-a",
        followup=True,
        attempt_count=2,
    )
    _write_json(
        initial / "verification.json",
        {
            "broker_request_id": "request-1",
            "commands": [
                {
                    "command": "python -m pytest tests/test_one.py::test_one",
                    "command_started_utc": "2026-01-01T00:08:00Z",
                    "exit_code": 0,
                },
                {
                    "command": "python -m pytest tests",
                    "command_started_utc": "2026-01-01T00:09:00Z",
                    "exit_code": 0,
                },
            ],
        },
    )

    _write_json(
        resumed / "run_meta.json",
        {
            "run_started_utc": "2026-01-01T00:20:00Z",
            "run_finished_utc": "2026-01-01T00:30:00Z",
        },
    )
    _write_json(resumed / "target_ref.json", {"agent": "codex", "model": "model-b"})
    _write_attempts(resumed, started="2026-01-01T00:21:00Z", model="model-b")
    _write_json(
        resumed / "resume_ref.json",
        {
            "resumed_from_run_dir": str(initial),
            "correction_origin": "system_self_correction",
            "supervisor_instructions": ["Correct the failed test."],
        },
    )
    # A copied receipt is one execution, not another verification command pair.
    _write_json(
        resumed / "verification.json",
        json.loads((initial / "verification.json").read_text(encoding="utf-8")),
    )

    _write_json(
        adopted / "adoption_ref.json",
        {
            "source_run_dir": str(resumed),
            "flags": {"model_invoked": False, "pr_adopted": True},
        },
    )
    _write_json(adopted / "target_ref.json", {"model_invoked": False})

    _write_json(
        prior_review / "run_meta.json",
        {
            "run_started_utc": "2026-01-01T00:35:00Z",
            "run_finished_utc": "2026-01-01T00:40:00Z",
        },
    )
    _write_json(prior_review / "target_ref.json", {"agent": "codex", "model": "model-r"})
    _write_attempts(prior_review, started="2026-01-01T00:36:00Z", model="model-r")

    _write_json(
        final_review / "run_meta.json",
        {
            "run_started_utc": "2026-01-01T00:50:00Z",
            "run_finished_utc": "2026-01-01T01:00:00Z",
        },
    )
    _write_json(final_review / "target_ref.json", {"agent": "codex", "model": "model-r"})
    _write_attempts(final_review, started="2026-01-01T00:51:00Z", model="model-r")
    _write_json(
        final_review / "review_ref.json",
        {
            "implementation_run_dir": str(adopted),
            "correction_of_review_run_dir": str(prior_review),
        },
    )
    _write_json(
        final_review / "merge_ref.json",
        {
            "merged": True,
            "merged_at_utc": "2026-01-01T01:01:00Z",
            "outcome_state": "mitigated",
        },
    )
    state = {
        "generated_at_utc": "2026-01-01T01:05:00Z",
        "run_dir": str(adopted),
        "ticket": {"fingerprint": "abc"},
        "lifecycle_state": "complete",
    }

    artifact = build_ticket_pipeline_efficiency(
        run_dir=adopted,
        review_run_dir=final_review,
        resume_state=state,
    )

    assert artifact["observational_only"] is True
    assert artifact["measurement_scope"] == {
        "start_boundary": "earliest_linked_implementation_or_review",
        "end_boundary": "current_resume_state_observation",
        "end_to_end": False,
        "excluded_upstream_stages": [
            "atom_mining",
            "problem_mining",
            "research",
            "optioning",
            "planning",
        ],
    }
    assert artifact["elapsed"]["status"] == "observed"
    assert artifact["elapsed"]["elapsed_wall_seconds"] == 3900.0
    assert artifact["elapsed"]["recorded_run_wall_seconds"] == 2100.0
    assert artifact["elapsed"]["outside_recorded_runs_seconds"] == 1800.0
    assert artifact["agent_activity"]["invocations"]["observed_count"] == 5
    assert artifact["agent_activity"]["invocations"]["model_counts"] == {
        "model-a": 2,
        "model-b": 1,
        "model-r": 2,
    }
    assert artifact["agent_activity"]["agent_turns"]["observed_count"] == 5
    assert artifact["correction_cycles"]["observed_cycle_count"] == 3
    assert artifact["correction_cycles"]["system_self_correction"]["count"] == 2
    assert artifact["correction_cycles"]["external_manual"]["status"] == "partial"
    assert artifact["correction_cycles"]["unknown_origin"]["count"] == 1
    assert artifact["verification"]["observed_unique_command_count"] == 2
    assert artifact["verification"]["counts_by_evidenced_scope"] == {
        "narrow": 1,
        "full": 0,
        "unclassified": 1,
    }
    assert artifact["lifecycle"] == {
        "current_state": "complete",
        "resume_state_terminal": True,
        "implementation_workflow_terminal": True,
        "outcome_terminal": True,
        "terminal_disposition": {
            "status": "observed",
            "value": "mitigated",
            "evidence_path": str(final_review / "merge_ref.json"),
        },
    }


def test_efficiency_keeps_missing_data_unknown(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    state = {
        "generated_at_utc": "2026-01-01T01:05:00Z",
        "run_dir": str(run_dir),
        "ticket": {"fingerprint": "abc"},
        "lifecycle_state": "in_progress",
    }

    artifact = build_ticket_pipeline_efficiency(
        run_dir=run_dir,
        review_run_dir=None,
        resume_state=state,
    )

    assert artifact["elapsed"]["status"] == "unknown"
    assert artifact["elapsed"]["elapsed_wall_seconds"] is None
    assert artifact["agent_activity"]["invocations"]["status"] == "unknown"
    assert artifact["verification"]["status"] == "unknown"
    assert artifact["lifecycle"]["resume_state_terminal"] is False
    assert artifact["lifecycle"]["implementation_workflow_terminal"] is False
    assert artifact["lifecycle"]["outcome_terminal"] is False
    assert artifact["lifecycle"]["terminal_disposition"]["status"] == "unknown"


def test_merge_complete_is_not_outcome_terminal_without_terminal_disposition(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    state = {
        "generated_at_utc": "2026-01-01T01:05:00Z",
        "run_dir": str(run_dir),
        "ticket": {"fingerprint": "abc"},
        "lifecycle_state": "complete",
    }

    artifact = build_ticket_pipeline_efficiency(
        run_dir=run_dir,
        review_run_dir=None,
        resume_state=state,
    )

    assert artifact["lifecycle"]["implementation_workflow_terminal"] is True
    assert artifact["lifecycle"]["outcome_terminal"] is False
    assert artifact["lifecycle"]["terminal_disposition"]["status"] == "unknown"


def test_supervisor_instruction_does_not_imply_manual_correction_origin(
    tmp_path: Path,
) -> None:
    initial = tmp_path / "initial"
    resumed = tmp_path / "resumed"
    initial.mkdir()
    resumed.mkdir()
    _write_json(
        resumed / "resume_ref.json",
        {
            "resumed_from_run_dir": str(initial),
            "supervisor_instructions": ["Correct the reported failure."],
        },
    )
    state = {
        "generated_at_utc": "2026-01-01T01:05:00Z",
        "run_dir": str(resumed),
        "ticket": {"fingerprint": "abc"},
        "lifecycle_state": "in_progress",
    }

    artifact = build_ticket_pipeline_efficiency(
        run_dir=resumed,
        review_run_dir=None,
        resume_state=state,
    )

    corrections = artifact["correction_cycles"]
    assert corrections["observed_cycle_count"] == 1
    assert corrections["system_self_correction"]["count"] == 0
    assert corrections["external_manual"]["observed_count"] == 0
    assert corrections["unknown_origin"]["count"] == 1


def test_efficiency_deduplicates_copied_agent_attempt_artifacts(tmp_path: Path) -> None:
    source = tmp_path / "source"
    adopted = tmp_path / "adopted"
    _write_json(
        source / "run_meta.json",
        {
            "run_started_utc": "2026-01-01T00:00:00Z",
            "run_finished_utc": "2026-01-01T00:01:00Z",
        },
    )
    _write_json(source / "target_ref.json", {"agent": "codex", "model": "model-a"})
    _write_attempts(source, started="2026-01-01T00:00:10Z", model="model-a")
    adopted.mkdir()
    (adopted / "agent_attempts.json").write_text(
        (source / "agent_attempts.json").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (adopted / "raw_events.attempt1.jsonl").write_text(
        (source / "raw_events.attempt1.jsonl").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    _write_json(adopted / "target_ref.json", {"model_invoked": False})
    _write_json(
        adopted / "adoption_ref.json",
        {"source_run_dir": str(source), "flags": {"model_invoked": False}},
    )
    state = {
        "generated_at_utc": "2026-01-01T00:02:00Z",
        "run_dir": str(adopted),
        "ticket": {"fingerprint": "abc"},
        "lifecycle_state": "complete",
    }

    artifact = build_ticket_pipeline_efficiency(
        run_dir=adopted,
        review_run_dir=None,
        resume_state=state,
    )

    assert artifact["agent_activity"]["invocations"]["observed_count"] == 1
    assert artifact["agent_activity"]["agent_turns"]["observed_count"] == 1
