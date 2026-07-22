from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
from reporter.case_metrics import aggregate_case_metrics
from run_artifacts.lifecycle_events import (
    LifecycleManifest,
    read_lifecycle_events,
    read_lifecycle_manifest,
    write_lifecycle_manifest,
)

import usertest_implement.lifecycle_telemetry as lifecycle_telemetry
from usertest_implement.resume_state import (
    build_ticket_resume_state,
    write_ticket_resume_state,
)
from usertest_implement.shared import SelectedTicket


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def _selected(tmp_path: Path) -> SelectedTicket:
    ticket_path = tmp_path / "ticket.md"
    ticket_path.write_text("# Ticket\n", encoding="utf-8")
    return SelectedTicket(
        fingerprint="abc123abc123abcd",
        title="Ticket",
        export_kind="implementation",
        stage="ready_for_ticket",
        owner_root=tmp_path,
        idea_path=ticket_path,
        ticket_markdown="# Ticket\n",
        tickets_export_path=None,
        export_index=None,
        case_id="case-42",
        plan_revision_id="plan-7",
    )


def test_resume_state_reuses_manifest_identity_and_emits_delivery_lifecycle(
    tmp_path: Path,
) -> None:
    selected = _selected(tmp_path)
    run_dir = tmp_path / "runs" / "implement" / "1"
    review_dir = tmp_path / "runs" / "review" / "1"
    _write_json(
        run_dir / "run_meta.json",
        {
            "run_started_utc": "2026-07-20T12:00:00Z",
            "run_finished_utc": "2026-07-20T12:03:00Z",
        },
    )
    _write_json(
        run_dir / "verification.json",
        {"passed": True, "wall_seconds": 30.0, "commands": []},
    )
    _write_json(
        run_dir / "git_ref.json",
        {
            "commit_attempted": True,
            "commit_performed": True,
            "commit_observed": False,
            "head_commit": "a" * 40,
            "error": None,
        },
    )
    _write_json(run_dir / "push_ref.json", {"pushed": True, "error": None})
    _write_json(
        run_dir / "pr_ref.json",
        {
            "requested": True,
            "created": True,
            "url": "https://example.invalid/pull/42",
            "created_at_utc": "2026-07-20T12:05:30Z",
            "error": None,
        },
    )
    _write_json(
        run_dir / "ci_gate.json",
        {
            "passed": True,
            "status": "completed",
            "conclusion": "success",
            "started_at_utc": "2026-07-20T12:04:00Z",
            "finished_at_utc": "2026-07-20T12:05:00Z",
        },
    )
    _write_json(
        review_dir / "review_summary.json",
        {
            "review_decision": "approved",
            "merge_ready": True,
            "reviewed_at_utc": "2026-07-20T12:06:00Z",
        },
    )
    _write_json(
        review_dir / "merge_ref.json",
        {
            "merged": True,
            "returncode": 0,
            "merged_at_utc": "2026-07-20T12:07:00Z",
        },
    )
    _write_json(
        review_dir / "outcome_progression.json",
        {
            "complete": True,
            "final_state": "resolved",
            "generated_at_utc": "2026-07-20T12:08:00Z",
        },
    )
    write_lifecycle_manifest(
        run_dir / "lifecycle_manifest.json",
        LifecycleManifest(
            case_lifecycle_id="case-lifecycle-from-controller",
            case_id="case-42",
            created_at="2026-07-20T12:00:00Z",
            updated_at="2026-07-20T12:03:00Z",
            status="active",
            system_fingerprint={"controller_context_verified": "true"},
        ),
    )

    state = write_ticket_resume_state(
        selected=selected,
        run_dir=run_dir,
        owner_root=tmp_path,
        exit_code=0,
        review_run_dir=review_dir,
    )
    first_events = read_lifecycle_events(run_dir / "lifecycle_events.jsonl")
    write_ticket_resume_state(
        selected=selected,
        run_dir=run_dir,
        owner_root=tmp_path,
        exit_code=0,
        review_run_dir=review_dir,
    )
    replayed_events = read_lifecycle_events(run_dir / "lifecycle_events.jsonl")

    assert state["case_id"] == "case-42"
    assert state["plan_revision_id"] == "plan-7"
    assert state["case_lifecycle_id"] == "case-lifecycle-from-controller"
    assert state["ticket"]["case_lifecycle_id"] == "case-lifecycle-from-controller"
    assert (run_dir / "case_metrics.json").is_file()
    assert (run_dir / "cohort_metrics.json").is_file()
    assert len(replayed_events) == len(first_events)
    assert {event.context.milestone_id for event in first_events} >= {
        "implementation_verified",
        "commit",
        "push",
        "pr_created",
        "ci",
        "review",
        "merge",
        "outcome_verified",
    }
    assert all(event.origin == "automatic" for event in first_events)
    assert any(
        event.event_type == "disposition.reached"
        and event.attributes.get("disposition") == "pr"
        for event in first_events
    )
    assert any(
        event.event_type == "disposition.verified"
        and event.attributes.get("disposition") == "pr"
        for event in first_events
    )
    assert any(
        event.event_type == "outcome.verified"
        and event.attributes.get("outcome_state") == "resolved"
        for event in first_events
    )
    ci_event = next(
        event
        for event in first_events
        if event.event_type == "delivery.completed"
        and event.context.milestone_id == "ci"
    )
    assert ci_event.external_wait_seconds == 60.0
    assert ci_event.attributes["wait_seconds_by_category"] == {"ci": 60.0}
    manifest = read_lifecycle_manifest(run_dir / "lifecycle_manifest.json")
    assert manifest.status == "terminal"
    assert manifest.metadata["plan_revision_id"] == "plan-7"
    metrics = aggregate_case_metrics(run_dir / "lifecycle_events.jsonl")
    case = metrics["cases"][0]
    assert case["disposition"] == "pr"
    assert case["disposition_verified"] is True
    assert case["timing"]["pr_created_at"] is not None
    assert case["timing"]["outcome_verified_at"] == "2026-07-20T12:08:00Z"
    assert case["timing"]["external_wait_seconds"] == 60.0
    assert case["timing"]["unclassified_seconds"] == 270.0
    assert case["timing"]["pr_create_to_outcome_seconds"] == 150.0


def test_resume_ready_verification_failure_keeps_lifecycle_open(tmp_path: Path) -> None:
    selected = _selected(tmp_path)
    run_dir = tmp_path / "runs" / "implement" / "failed"
    _write_json(
        run_dir / "verification.json",
        {"passed": False, "commands": [{"command": "pytest", "exit_code": 1}]},
    )

    state = write_ticket_resume_state(
        selected=selected,
        run_dir=run_dir,
        owner_root=tmp_path,
        exit_code=2,
    )
    events = read_lifecycle_events(run_dir / "lifecycle_events.jsonl")

    assert state["lifecycle_state"] == "verification_failed_resume_ready"
    assert all(event.origin == "unknown_external" for event in events)
    failure = next(event for event in events if event.event_type == "error.occurred")
    assert failure.error_cluster_id is not None
    assert all(event.event_type != "error.resolved" for event in events)
    assert all(event.event_type != "lifecycle.closed" for event in events)
    manifest = read_lifecycle_manifest(run_dir / "lifecycle_manifest.json")
    assert manifest.status == "active"


def test_successful_resume_resolves_predecessor_error_cluster(tmp_path: Path) -> None:
    selected = replace(_selected(tmp_path), case_lifecycle_id="case-lifecycle-resume")
    failed_run = tmp_path / "runs" / "implement" / "failed-predecessor"
    _write_json(failed_run / "verification.json", {"passed": False, "commands": []})
    write_ticket_resume_state(
        selected=selected,
        run_dir=failed_run,
        owner_root=tmp_path,
        exit_code=2,
    )
    predecessor_error = next(
        event
        for event in read_lifecycle_events(failed_run / "lifecycle_events.jsonl")
        if event.event_type == "error.occurred"
    )

    successful_run = tmp_path / "runs" / "implement" / "successful-resume"
    _write_json(successful_run / "verification.json", {"passed": True, "commands": []})
    _write_json(
        successful_run / "resume_ref.json",
        {
            "resumed_from_run_dir": str(failed_run),
            "correction_origin": "system_self_correction",
        },
    )
    write_ticket_resume_state(
        selected=selected,
        run_dir=successful_run,
        owner_root=tmp_path,
        exit_code=0,
    )

    resolution = next(
        event
        for event in read_lifecycle_events(successful_run / "lifecycle_events.jsonl")
        if event.event_type == "error.resolved"
    )
    assert resolution.error_cluster_id == predecessor_error.error_cluster_id
    assert resolution.attributes["resolution_mode"] == "self_healed_controller"


def test_case_lifecycle_fallback_is_stable_per_implementation_run(tmp_path: Path) -> None:
    selected = _selected(tmp_path)
    run_dir = tmp_path / "runs" / "implement" / "stable"
    run_dir.mkdir(parents=True)

    first = build_ticket_resume_state(
        selected=selected,
        run_dir=run_dir,
        owner_root=tmp_path,
    )
    second = build_ticket_resume_state(
        selected=selected,
        run_dir=run_dir,
        owner_root=tmp_path,
    )

    assert first["case_lifecycle_id"] == second["case_lifecycle_id"]
    assert first["case_lifecycle_id"].startswith("case-lifecycle:")


def test_qualification_lifecycle_identity_flows_into_implementation(tmp_path: Path) -> None:
    selected = replace(_selected(tmp_path), case_lifecycle_id="qualification-life-1")
    run_dir = tmp_path / "runs" / "implement" / "linked"
    run_dir.mkdir(parents=True)

    state = build_ticket_resume_state(
        selected=selected,
        run_dir=run_dir,
        owner_root=tmp_path,
    )

    assert state["case_lifecycle_id"] == "qualification-life-1"
    assert state["ticket"]["case_lifecycle_id"] == "qualification-life-1"


def test_explicit_external_correction_is_not_scored_as_automatic(tmp_path: Path) -> None:
    selected = _selected(tmp_path)
    run_dir = tmp_path / "runs" / "implement" / "manual-correction"
    _write_json(run_dir / "resume_ref.json", {"correction_origin": "external_manual"})
    _write_json(run_dir / "verification.json", {"passed": True, "commands": []})

    write_ticket_resume_state(
        selected=selected,
        run_dir=run_dir,
        owner_root=tmp_path,
        exit_code=0,
    )
    verification = next(
        event
        for event in read_lifecycle_events(run_dir / "lifecycle_events.jsonl")
        if event.context.milestone_id == "implementation_verified"
    )

    assert verification.origin == "manual"
    assert verification.root_initiator_type == "human"
    assert verification.attributes["automated"] is False


def test_lifecycle_telemetry_failure_never_blocks_resume_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    selected = _selected(tmp_path)
    run_dir = tmp_path / "runs" / "implement" / "telemetry-failure"
    run_dir.mkdir(parents=True)

    def fail(**_kwargs: object) -> dict[str, object]:
        raise RuntimeError("telemetry unavailable")

    monkeypatch.setattr(
        lifecycle_telemetry,
        "write_implementation_lifecycle_telemetry",
        fail,
    )
    with pytest.warns(RuntimeWarning, match="observational lifecycle telemetry"):
        state = write_ticket_resume_state(
            selected=selected,
            run_dir=run_dir,
            owner_root=tmp_path,
        )

    assert state["case_id"] == "case-42"
    assert (run_dir / "ticket_resume_state.json").is_file()
