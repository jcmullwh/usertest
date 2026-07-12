from __future__ import annotations

import json
from pathlib import Path

from usertest_implement.resume_state import (
    LIFECYCLE_CI_FAILED,
    LIFECYCLE_COMPLETE,
    LIFECYCLE_MERGE_READY,
    LIFECYCLE_PR_CREATION_FAILED,
    LIFECYCLE_REVIEW_CHANGES_REQUESTED,
    LIFECYCLE_VERIFICATION_FAILED_RESUME_READY,
    build_ticket_resume_state,
    write_ticket_resume_state,
)
from usertest_implement.shared import SelectedTicket


def _write_json(path: Path, obj: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _selected(tmp_path: Path) -> SelectedTicket:
    ticket_path = tmp_path / ".agents" / "plans" / "3 - in_progress" / "ticket.md"
    ticket_path.parent.mkdir(parents=True, exist_ok=True)
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
    )


def _base_run(tmp_path: Path) -> tuple[SelectedTicket, Path, Path]:
    selected = _selected(tmp_path)
    run_dir = tmp_path / "runs" / "impl" / "0"
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True)
    _write_json(run_dir / "workspace_ref.json", {"workspace_dir": str(workspace)})
    _write_json(
        run_dir / "ticket_ref.json",
        {
            "fingerprint": selected.fingerprint,
            "owner_repo": {"root": str(tmp_path), "idea_path": str(selected.idea_path)},
        },
    )
    _write_json(run_dir / "git_ref.json", {"branch": "backlog/test", "head_commit": "abc"})
    _write_json(run_dir / "target_ref.json", {"agent": "codex"})
    (run_dir / "raw_events.jsonl").write_text(
        json.dumps(
            {
                "type": "thread.started",
                "thread_id": "019f5000-0000-7000-8000-000000000001",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return selected, run_dir, workspace


def test_resume_state_records_required_identity_fields(tmp_path: Path) -> None:
    selected, run_dir, workspace = _base_run(tmp_path)
    _write_json(run_dir / "handoff_summary.json", {"final_status": "success"})

    state = write_ticket_resume_state(
        selected=selected,
        run_dir=run_dir,
        owner_root=tmp_path,
        exit_code=0,
    )

    assert (run_dir / "ticket_resume_state.json").exists()
    assert state["ticket"]["fingerprint"] == selected.fingerprint
    assert state["ticket"]["path"] == str(selected.idea_path)
    assert state["owner_root"] == str(tmp_path)
    assert state["run_dir"] == str(run_dir)
    assert state["workspace_path"] == str(workspace)
    assert state["branch"] == "backlog/test"
    assert state["implementation_author"] == {
        "agent": "codex",
        "session_id": "019f5000-0000-7000-8000-000000000001",
        "status": "exact_session_available",
        "exact_session_available": True,
        "agent_source": str(run_dir / "target_ref.json"),
        "session_source": str(run_dir / "raw_events.jsonl"),
    }
    assert state["source_evidence_paths"]["ticket_ref"] == str(run_dir / "ticket_ref.json")
    assert state["source_evidence_paths"]["raw_events"] == str(run_dir / "raw_events.jsonl")


def test_resume_state_never_claims_exact_continuity_for_malformed_thread_id(
    tmp_path: Path,
) -> None:
    selected, run_dir, _workspace = _base_run(tmp_path)
    (run_dir / "raw_events.jsonl").write_text(
        json.dumps({"type": "thread.started", "thread_id": "not-a-canonical-session"})
        + "\n",
        encoding="utf-8",
    )

    state = build_ticket_resume_state(
        selected=selected,
        run_dir=run_dir,
        owner_root=tmp_path,
        exit_code=1,
    )

    assert state["implementation_author"]["session_id"] is None
    assert state["implementation_author"]["status"] == "author_session_unavailable"
    assert state["implementation_author"]["exact_session_available"] is False


def test_resume_state_maps_verification_failure(tmp_path: Path) -> None:
    selected, run_dir, _workspace = _base_run(tmp_path)
    _write_json(
        run_dir / "verification.json",
        {"passed": False, "commands": [{"command": "pytest", "exit_code": 1}]},
    )

    state = build_ticket_resume_state(
        selected=selected,
        run_dir=run_dir,
        owner_root=tmp_path,
        exit_code=2,
    )

    assert state["lifecycle_state"] == LIFECYCLE_VERIFICATION_FAILED_RESUME_READY
    assert state["blocking_reason"] == "Verification failed: pytest"
    assert state["source_evidence_paths"]["verification"] == str(run_dir / "verification.json")


def test_resume_state_maps_ci_failure_before_pr_failure(tmp_path: Path) -> None:
    selected, run_dir, _workspace = _base_run(tmp_path)
    _write_json(run_dir / "ci_gate.json", {"passed": False, "error": "tests failed"})
    _write_json(
        run_dir / "pr_ref.json",
        {"requested": True, "created": False, "error": "CI gate failed."},
    )

    state = build_ticket_resume_state(
        selected=selected,
        run_dir=run_dir,
        owner_root=tmp_path,
        exit_code=5,
    )

    assert state["lifecycle_state"] == LIFECYCLE_CI_FAILED
    assert state["blocking_reason"] == "tests failed"


def test_resume_state_maps_pr_creation_failure(tmp_path: Path) -> None:
    selected, run_dir, _workspace = _base_run(tmp_path)
    _write_json(
        run_dir / "pr_ref.json",
        {"requested": True, "created": False, "error": "gh auth failed"},
    )

    state = build_ticket_resume_state(
        selected=selected,
        run_dir=run_dir,
        owner_root=tmp_path,
        exit_code=5,
    )

    assert state["lifecycle_state"] == LIFECYCLE_PR_CREATION_FAILED
    assert state["blocking_reason"] == "gh auth failed"


def test_resume_state_maps_review_changes_requested(tmp_path: Path) -> None:
    selected, run_dir, _workspace = _base_run(tmp_path)
    review_run_dir = tmp_path / "runs" / "review" / "0"
    _write_json(
        review_run_dir / "review_summary.json",
        {
            "review_decision": "changes_requested",
            "merge_ready": False,
            "rationale": "Fix the edge case.",
            "pr_url": "https://example.invalid/pr/1",
        },
    )

    state = build_ticket_resume_state(
        selected=selected,
        run_dir=run_dir,
        owner_root=tmp_path,
        exit_code=0,
        review_run_dir=review_run_dir,
    )

    assert state["lifecycle_state"] == LIFECYCLE_REVIEW_CHANGES_REQUESTED
    assert state["blocking_reason"] == "Fix the edge case."
    assert state["pr_url"] == "https://example.invalid/pr/1"
    assert state["source_evidence_paths"]["review_summary"] == str(
        review_run_dir / "review_summary.json"
    )


def test_resume_state_maps_merge_ready(tmp_path: Path) -> None:
    selected, run_dir, _workspace = _base_run(tmp_path)
    review_run_dir = tmp_path / "runs" / "review" / "0"
    _write_json(
        review_run_dir / "review_summary.json",
        {"review_decision": "approved", "merge_ready": True, "head_ref_name": "backlog/review"},
    )

    state = build_ticket_resume_state(
        selected=selected,
        run_dir=run_dir,
        owner_root=tmp_path,
        exit_code=0,
        review_run_dir=review_run_dir,
    )

    assert state["lifecycle_state"] == LIFECYCLE_MERGE_READY
    assert state["blocking_reason"] is None
    assert state["branch"] == "backlog/test"


def test_resume_state_maps_complete_from_merge_ref(tmp_path: Path) -> None:
    selected, run_dir, _workspace = _base_run(tmp_path)
    review_run_dir = tmp_path / "runs" / "review" / "0"
    _write_json(
        review_run_dir / "review_summary.json",
        {"review_decision": "approved", "merge_ready": True},
    )
    _write_json(review_run_dir / "merge_ref.json", {"merged": True})

    state = build_ticket_resume_state(
        selected=selected,
        run_dir=run_dir,
        owner_root=tmp_path,
        exit_code=0,
        review_run_dir=review_run_dir,
    )

    assert state["lifecycle_state"] == LIFECYCLE_COMPLETE
    assert state["blocking_reason"] is None
    assert state["source_evidence_paths"]["merge_ref"] == str(review_run_dir / "merge_ref.json")
