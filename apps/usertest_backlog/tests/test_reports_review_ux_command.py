from __future__ import annotations

import json
from pathlib import Path

import pytest
from backlog_repo.export import ticket_export_fingerprint
from runner_core import find_repo_root

from usertest_backlog.cli import main


def _write_json(path: Path, obj: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def test_reports_review_ux_dry_run_writes_prompt_and_outputs(tmp_path: Path) -> None:
    repo_root = find_repo_root(Path(__file__).resolve())
    runs_dir = tmp_path / "runs" / "usertest"

    compiled_dir = runs_dir / "target_a" / "_compiled"
    backlog_path = compiled_dir / "target_a.backlog.json"
    intent_snapshot_path = compiled_dir / "target_a.intent_snapshot.json"

    _write_json(
        backlog_path,
        {
            "schema_version": 1,
            "tickets": [
                {
                    "ticket_id": "BLG-001",
                    "title": "Add single-command shortcut for running the full pipeline",
                    "problem": "Users get confused by multiple steps in docs.",
                    "severity": "medium",
                    "confidence": 0.6,
                    "stage": "research_required",
                    "change_surface": {
                        "user_visible": True,
                        "kinds": ["new_command"],
                        "notes": "Proposes a new command entry point.",
                    },
                    "breadth": {
                        "missions": 1,
                        "targets": 1,
                        "repo_inputs": 1,
                        "agents": 1,
                        "runs": 1,
                    },
                    "risks": ["overfitting_risk"],
                    "investigation_steps": ["Validate repo intent"],
                    "success_criteria": ["Existing commands can be parameterized instead."],
                    "suggested_owner": "runner_core",
                }
            ],
        },
    )
    _write_json(
        intent_snapshot_path,
        {
            "schema_version": 1,
            "generated_at": "2026-02-09T00:00:00Z",
            "scope": {"target": "target_a", "repo_input": None},
            "commands": [{"command": "usertest reports backlog", "help": "Generate a backlog."}],
        },
    )

    with pytest.raises(SystemExit) as exc:
        main(
            [
                "reports",
                "review-ux",
                "--repo-root",
                str(repo_root),
                "--runs-dir",
                str(runs_dir),
                "--target",
                "target_a",
                "--dry-run",
            ]
        )
    assert exc.value.code == 0

    out_json = compiled_dir / "target_a.ux_review.json"
    out_md = compiled_dir / "target_a.ux_review.md"
    artifacts_dir = compiled_dir / "target_a.ux_review_artifacts"

    assert out_json.exists()
    assert out_md.exists()
    assert artifacts_dir.exists()
    assert list(artifacts_dir.glob("*.dry_run.prompt.txt"))

    doc = json.loads(out_json.read_text(encoding="utf-8"))
    assert doc["status"] == "dry_run"
    assert doc["review"] is None
    assert doc["tickets_meta"]["research_required_total"] == 1
    assert doc["tickets_meta"]["high_surface_ready_total"] == 0
    assert doc["tickets_meta"]["review_total"] == 1


def test_reports_review_ux_dry_run_includes_high_surface_ready_tickets(tmp_path: Path) -> None:
    repo_root = find_repo_root(Path(__file__).resolve())
    runs_dir = tmp_path / "runs" / "usertest"

    compiled_dir = runs_dir / "target_a" / "_compiled"
    backlog_path = compiled_dir / "target_a.backlog.json"
    intent_snapshot_path = compiled_dir / "target_a.intent_snapshot.json"
    tickets = [
        {
            "title": "Add `usertest smoke` shortcut command",
            "problem": "Operators want a single obvious entry point.",
            "severity": "low",
            "confidence": 0.5,
            "stage": "ready_for_ticket",
            "change_surface": {"user_visible": True, "kinds": ["new_command"], "notes": ""},
            "breadth": {
                "missions": 1,
                "targets": 1,
                "repo_inputs": 1,
                "agents": 1,
                "runs": 1,
            },
        },
        {
            "title": "Add extra flag",
            "problem": "Make it configurable.",
            "severity": "low",
            "confidence": 0.5,
            "stage": "ready_for_ticket",
            "change_surface": {"user_visible": True, "kinds": ["new_flag"], "notes": ""},
            "breadth": {
                "missions": 1,
                "targets": 1,
                "repo_inputs": 1,
                "agents": 1,
                "runs": 1,
            },
        },
    ]
    _write_json(backlog_path, {"schema_version": 1, "tickets": tickets})
    fingerprint_high_surface = ticket_export_fingerprint(tickets[0])
    fingerprint_not_high_surface = ticket_export_fingerprint(tickets[1])
    _write_json(
        intent_snapshot_path,
        {
            "schema_version": 1,
            "generated_at": "2026-02-09T00:00:00Z",
            "scope": {"target": "target_a", "repo_input": None},
            "commands": [{"command": "usertest reports backlog", "help": "Generate a backlog."}],
        },
    )

    with pytest.raises(SystemExit) as exc:
        main(
            [
                "reports",
                "review-ux",
                "--repo-root",
                str(repo_root),
                "--runs-dir",
                str(runs_dir),
                "--target",
                "target_a",
                "--dry-run",
            ]
        )
    assert exc.value.code == 0

    out_json = compiled_dir / "target_a.ux_review.json"
    artifacts_dir = compiled_dir / "target_a.ux_review_artifacts"
    prompt_paths = list(artifacts_dir.glob("*.dry_run.prompt.txt"))
    assert prompt_paths

    doc = json.loads(out_json.read_text(encoding="utf-8"))
    assert doc["tickets_meta"]["research_required_total"] == 0
    assert doc["tickets_meta"]["high_surface_ready_total"] == 1
    assert doc["tickets_meta"]["review_total"] == 1

    prompt_text = prompt_paths[0].read_text(encoding="utf-8")
    assert fingerprint_high_surface in prompt_text
    assert fingerprint_not_high_surface not in prompt_text
