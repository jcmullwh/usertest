from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from backlog_repo.export import ticket_export_fingerprint

from usertest_backlog.cli import _cleanup_stale_ticket_idea_files, main


def _write_json(path: Path, obj: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _write_yaml(path: Path, obj: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(obj, sort_keys=False), encoding="utf-8")


def _make_repo_root(tmp_path: Path) -> Path:
    repo_root = tmp_path / "repo_root"
    repo_root.mkdir(parents=True, exist_ok=True)
    _write_yaml(repo_root / "configs" / "agents.yaml", {"agents": {}})
    _write_yaml(repo_root / "configs" / "policies.yaml", {"policies": {}})
    _write_yaml(
        repo_root / "configs" / "backlog_policy.yaml",
        {
            "backlog_policy": {
                "surface_area_high": [
                    "new_command",
                    "breaking_change",
                    "new_top_level_mode",
                    "new_config_schema",
                    "new_api",
                ],
                "breadth_min_for_surface_area_high": {
                    "missions": 2,
                    "targets": 2,
                    "repo_inputs": 2,
                },
                "default_stage_for_high_surface_low_breadth": "research_required",
                "default_stage_for_labeled": "ready_for_ticket",
                "investigation_steps_for_high_surface_low_breadth": [
                    "Validate repo intent",
                    "Check if existing commands/flags can be parameterized",
                    "Propose a consolidation plan (avoid new top-level commands)",
                ],
            }
        },
    )
    return repo_root


def test_reports_export_tickets_applies_stage_gates_and_ledger_skip(tmp_path: Path) -> None:
    repo_root = _make_repo_root(tmp_path)
    runs_dir = tmp_path / "runs" / "usertest"
    compiled_dir = runs_dir / "target_a" / "_compiled"
    owner_repo = tmp_path / "owner_repo"
    owner_repo.mkdir(parents=True, exist_ok=True)

    ticket_research = {
        "ticket_id": "BLG-001",
        "title": "Add a new top-level mode for onboarding",
        "problem": "New users struggle to discover the right entry points.",
        "severity": "medium",
        "confidence": 0.7,
        "stage": "research_required",
        "evidence_atom_ids": ["target_a/20260101T000000Z/codex/0:confusion_point:1"],
        "change_surface": {
            "user_visible": True,
            "kinds": ["new_top_level_mode"],
            "notes": "New mode proposed.",
        },
        "breadth": {"missions": 1, "targets": 1, "repo_inputs": 1, "agents": 1, "runs": 1},
        "suggested_owner": "docs",
    }
    ticket_gated = {
        "ticket_id": "BLG-002",
        "title": "Add `usertest smoke` shortcut command",
        "problem": "Operators want a single obvious entry point.",
        "severity": "low",
        "confidence": 0.6,
        "stage": "ready_for_ticket",
        "evidence_atom_ids": ["target_a/20260102T000000Z/claude/0:report_validation_error:1"],
        "change_surface": {
            "user_visible": True,
            "kinds": ["new_command"],
            "notes": "New command proposed.",
        },
        "breadth": {"missions": 3, "targets": 2, "repo_inputs": 2, "agents": 2, "runs": 8},
        "suggested_owner": "runner_core",
    }
    ticket_impl = {
        "ticket_id": "BLG-003",
        "title": "Add quickstart examples to README",
        "problem": "README lacks a runnable example.",
        "severity": "high",
        "confidence": 0.9,
        "stage": "ready_for_ticket",
        "evidence_atom_ids": ["target_a/20260101T000000Z/codex/0:suggested_change:1"],
        "change_surface": {"user_visible": True, "kinds": ["docs_change"], "notes": "Docs only."},
        "breadth": {"missions": 1, "targets": 1, "repo_inputs": 1, "agents": 1, "runs": 1},
        "suggested_owner": "docs",
    }
    ticket_triage = {
        "ticket_id": "BLG-004",
        "title": "Clarify unsupported `uvx` mention in quickstart docs",
        "problem": (
            "Docs references look inconsistent and need triage before filing "
            "implementation."
        ),
        "severity": "low",
        "confidence": 0.55,
        "stage": "triage",
        "evidence_atom_ids": ["target_a/20260103T000000Z/gemini/0:confusion_point:1"],
        "change_surface": {"user_visible": False, "kinds": ["unknown"], "notes": ""},
        "breadth": {"missions": 1, "targets": 1, "repo_inputs": 1, "agents": 1, "runs": 1},
        "suggested_owner": "docs",
    }

    backlog_path = compiled_dir / "target_a.backlog.json"
    _write_json(
        backlog_path,
        {
            "schema_version": 1,
            "scope": {"repo_input": str(owner_repo)},
            "tickets": [ticket_research, ticket_gated, ticket_impl, ticket_triage],
        },
    )

    actions_path = tmp_path / "backlog_actions.yaml"
    fingerprint_impl = ticket_export_fingerprint(ticket_impl)
    _write_yaml(
        actions_path,
        {
            "version": 1,
            "actions": [
                {
                    "fingerprint": fingerprint_impl,
                    "status": "filed",
                    "issue_url": "https://example.invalid/issues/123",
                    "notes": "Already filed.",
                }
            ],
        },
    )
    atom_actions_path = tmp_path / "backlog_atom_actions.yaml"
    _write_yaml(
        atom_actions_path,
        {
            "version": 1,
            "atoms": [
                {
                    "atom_id": "target_a/20260101T000000Z/codex/0:confusion_point:1",
                    "status": "ticketed",
                    "ticket_ids": ["BLG-001"],
                },
                {
                    "atom_id": "target_a/20260102T000000Z/claude/0:report_validation_error:1",
                    "status": "ticketed",
                    "ticket_ids": ["BLG-002"],
                },
                {
                    "atom_id": "target_a/20260103T000000Z/gemini/0:confusion_point:1",
                    "status": "ticketed",
                    "ticket_ids": ["BLG-004"],
                },
            ],
        },
    )

    with pytest.raises(SystemExit) as exc:
        main(
            [
                "reports",
                "export-tickets",
                "--repo-root",
                str(repo_root),
                "--runs-dir",
                str(runs_dir),
                "--target",
                "target_a",
                "--actions-yaml",
                str(actions_path),
                "--atom-actions-yaml",
                str(atom_actions_path),
                "--skip-plan-folder-dedupe",
            ]
        )
    assert exc.value.code == 0

    out_json = compiled_dir / "target_a.tickets_export.json"
    assert out_json.exists()
    export_doc = json.loads(out_json.read_text(encoding="utf-8"))

    assert export_doc["stats"]["exports_total"] == 3
    assert export_doc["stats"]["skipped_actioned"] == 1
    assert export_doc["stats"]["idea_files_written"] == 3
    assert export_doc["inputs"]["atom_actions_yaml"] == str(atom_actions_path)
    atom_updates = export_doc["stats"]["atom_status_updates"]
    assert atom_updates["queued_atoms_touched"] == 3
    idea_files = export_doc["idea_files"]
    assert isinstance(idea_files, list)
    assert len(idea_files) == 3
    for path_s in idea_files:
        assert Path(path_s).exists()

    exports = export_doc["exports"]
    assert isinstance(exports, list)
    kinds = {item["source_ticket"]["ticket_id"]: item["export_kind"] for item in exports}
    assert kinds["BLG-001"] == "research"
    assert kinds["BLG-002"] == "research"
    assert kinds["BLG-004"] == "implementation"
    by_ticket = {item["source_ticket"]["ticket_id"]: item for item in exports}

    owner_research = by_ticket["BLG-001"]["owner_repo"]
    assert isinstance(owner_research, dict)
    assert Path(owner_research["idea_path"]).exists()
    assert str(owner_repo) in owner_research["idea_path"]

    owner_runner = by_ticket["BLG-002"]["owner_repo"]
    assert isinstance(owner_runner, dict)
    assert Path(owner_runner["idea_path"]).exists()
    assert str(repo_root) in owner_runner["idea_path"]
    assert owner_runner["resolution"] == "suggested_owner:runner_core"

    owner_triage = by_ticket["BLG-004"]["owner_repo"]
    assert isinstance(owner_triage, dict)
    assert Path(owner_triage["idea_path"]).exists()
    assert ".agents/plans/0.5 - to_triage/" in owner_triage["idea_path"].replace("\\", "/")

    atom_actions_doc = yaml.safe_load(atom_actions_path.read_text(encoding="utf-8"))
    atoms = {item["atom_id"]: item for item in atom_actions_doc["atoms"]}
    assert atoms["target_a/20260101T000000Z/codex/0:confusion_point:1"]["status"] == "queued"
    legacy_failure_atom_id = "target_a/20260102T000000Z/claude/0:report_validation_error:1"
    canonical_failure_atom_id = "target_a/20260102T000000Z/claude/0:run_failure_event:1"
    assert atoms[canonical_failure_atom_id]["status"] == "queued"
    assert legacy_failure_atom_id in atoms[canonical_failure_atom_id]["derived_from_atom_ids"]
    assert atoms["target_a/20260103T000000Z/gemini/0:confusion_point:1"]["status"] == "queued"
    assert any(
        str(owner_research["idea_path"]) == path
        for path in atoms["target_a/20260101T000000Z/codex/0:confusion_point:1"]["queue_paths"]
    )

    research_body = next(
        item["body_markdown"]
        for item in exports
        if item["source_ticket"]["ticket_id"] == "BLG-001"
    )
    assert "Research / ADR Template" in research_body


def test_resolve_owner_repo_root_normalizes_local_and_remote_repo_inputs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from usertest_backlog import cli as backlog_cli

    repo_root = tmp_path / "repo_root"
    repo_root.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(
        backlog_cli,
        "_git_remote_urls",
        lambda _repo_root: ["https://github.com/jcmullwh/usertest.git"],
    )

    owner_root, owner_input, resolution = backlog_cli._resolve_owner_repo_root(
        ticket={
            "repo_inputs_citing": [
                str(repo_root),
                "https://github.com/jcmullwh/usertest.git",
            ]
        },
        scope_repo_input=None,
        cli_repo_input=None,
        repo_root=repo_root,
    )

    assert owner_root == repo_root
    assert owner_input == str(repo_root)
    assert resolution == "ticket_repo_inputs_citing_normalized"


def test_reports_export_tickets_skips_when_plan_ticket_fingerprint_exists(tmp_path: Path) -> None:
    repo_root = _make_repo_root(tmp_path)
    runs_dir = tmp_path / "runs" / "usertest"
    compiled_dir = runs_dir / "target_a" / "_compiled"
    owner_repo = tmp_path / "owner_repo"
    owner_repo.mkdir(parents=True, exist_ok=True)

    ticket = {
        "ticket_id": "BLG-001",
        "title": "Add `usertest smoke` shortcut command",
        "problem": "Operators want a single obvious entry point.",
        "severity": "low",
        "confidence": 0.6,
        "stage": "ready_for_ticket",
        "evidence_atom_ids": ["target_a/20260102T000000Z/claude/0:report_validation_error:1"],
        "change_surface": {
            "user_visible": True,
            "kinds": ["new_command"],
            "notes": "New command proposed.",
        },
        "breadth": {"missions": 3, "targets": 2, "repo_inputs": 2, "agents": 2, "runs": 8},
        "suggested_owner": "docs",
    }

    backlog_path = compiled_dir / "target_a.backlog.json"
    _write_json(
        backlog_path,
        {
            "schema_version": 1,
            "scope": {"repo_input": str(owner_repo)},
            "tickets": [ticket],
        },
    )

    actions_path = tmp_path / "backlog_actions.yaml"
    _write_yaml(actions_path, {"version": 1, "actions": []})

    atom_actions_path = tmp_path / "backlog_atom_actions.yaml"
    _write_yaml(atom_actions_path, {"version": 1, "atoms": []})

    fingerprint = ticket_export_fingerprint(ticket)
    complete_dir = owner_repo / ".agents" / "plans" / "5 - complete"
    complete_dir.mkdir(parents=True, exist_ok=True)
    (complete_dir / f"20260211_BLG-999_{fingerprint}_already-done.md").write_text(
        "# Already done\n",
        encoding="utf-8",
    )

    with pytest.raises(SystemExit) as exc:
        main(
            [
                "reports",
                "export-tickets",
                "--repo-root",
                str(repo_root),
                "--runs-dir",
                str(runs_dir),
                "--target",
                "target_a",
                "--actions-yaml",
                str(actions_path),
                "--atom-actions-yaml",
                str(atom_actions_path),
            ]
        )
    assert exc.value.code == 0

    out_json = compiled_dir / "target_a.tickets_export.json"
    export_doc = json.loads(out_json.read_text(encoding="utf-8"))
    assert export_doc["stats"]["exports_total"] == 0
    assert export_doc["stats"]["skipped_existing_plan"] == 1
    assert export_doc["stats"]["idea_files_written"] == 0

    atom_actions_doc = yaml.safe_load(atom_actions_path.read_text(encoding="utf-8"))
    atoms = {item["atom_id"]: item for item in atom_actions_doc["atoms"]}
    legacy_failure_atom_id = "target_a/20260102T000000Z/claude/0:report_validation_error:1"
    canonical_failure_atom_id = "target_a/20260102T000000Z/claude/0:run_failure_event:1"
    assert atoms[canonical_failure_atom_id]["status"] == "actioned"
    assert legacy_failure_atom_id in atoms[canonical_failure_atom_id]["derived_from_atom_ids"]


def test_reports_export_tickets_cleans_stale_queued_plan_files_when_actioned_plan_exists(
    tmp_path: Path,
) -> None:
    repo_root = _make_repo_root(tmp_path)
    runs_dir = tmp_path / "runs" / "usertest"
    compiled_dir = runs_dir / "target_a" / "_compiled"
    owner_repo = tmp_path / "owner_repo"
    owner_repo.mkdir(parents=True, exist_ok=True)

    ticket = {
        "ticket_id": "BLG-001",
        "title": "Add `usertest smoke` shortcut command",
        "problem": "Operators want a single obvious entry point.",
        "severity": "low",
        "confidence": 0.6,
        "stage": "ready_for_ticket",
        "evidence_atom_ids": ["target_a/20260102T000000Z/claude/0:report_validation_error:1"],
        "change_surface": {
            "user_visible": True,
            "kinds": ["new_command"],
            "notes": "New command proposed.",
        },
        "breadth": {"missions": 3, "targets": 2, "repo_inputs": 2, "agents": 2, "runs": 8},
        "suggested_owner": "docs",
    }

    backlog_path = compiled_dir / "target_a.backlog.json"
    _write_json(
        backlog_path,
        {
            "schema_version": 1,
            "scope": {"repo_input": str(owner_repo)},
            "tickets": [ticket],
        },
    )

    actions_path = tmp_path / "backlog_actions.yaml"
    _write_yaml(actions_path, {"version": 1, "actions": []})

    atom_actions_path = tmp_path / "backlog_atom_actions.yaml"
    _write_yaml(atom_actions_path, {"version": 1, "atoms": []})

    fingerprint = ticket_export_fingerprint(ticket)
    complete_dir = owner_repo / ".agents" / "plans" / "5 - complete"
    complete_dir.mkdir(parents=True, exist_ok=True)
    complete_path = complete_dir / f"20260211_BLG-001_{fingerprint}_already-done.md"
    complete_path.write_text("# Already done\n", encoding="utf-8")

    ideas_dir = owner_repo / ".agents" / "plans" / "1 - ideas"
    ideas_dir.mkdir(parents=True, exist_ok=True)
    stale_idea_path = ideas_dir / f"20260212_BLG-001_{fingerprint}_stale-queue-copy.md"
    stale_idea_path.write_text("# Stale copy\n", encoding="utf-8")

    assert complete_path.exists()
    assert stale_idea_path.exists()

    with pytest.raises(SystemExit) as exc:
        main(
            [
                "reports",
                "export-tickets",
                "--repo-root",
                str(repo_root),
                "--runs-dir",
                str(runs_dir),
                "--target",
                "target_a",
                "--actions-yaml",
                str(actions_path),
                "--atom-actions-yaml",
                str(atom_actions_path),
            ]
        )
    assert exc.value.code == 0

    export_doc = json.loads(
        (compiled_dir / "target_a.tickets_export.json").read_text(encoding="utf-8")
    )
    assert export_doc["stats"]["exports_total"] == 0
    assert export_doc["stats"]["skipped_existing_plan"] == 1
    assert export_doc["stats"]["idea_files_written"] == 0

    assert complete_path.exists()
    assert not stale_idea_path.exists()


def test_cleanup_stale_ticket_idea_files_includes_owner_repo_root_when_no_repo_inputs(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo_root"
    repo_root.mkdir(parents=True, exist_ok=True)
    owner_repo_root = tmp_path / "owner_repo_root"
    owner_repo_root.mkdir(parents=True, exist_ok=True)

    fingerprint = "deadbeef"
    ideas_dir = owner_repo_root / ".agents" / "plans" / "1 - ideas"
    ideas_dir.mkdir(parents=True, exist_ok=True)
    stale_idea_path = ideas_dir / f"20260212_BLG-001_{fingerprint}_stale-queue-copy.md"
    stale_idea_path.write_text("# Stale copy\n", encoding="utf-8")
    assert stale_idea_path.exists()

    _cleanup_stale_ticket_idea_files(
        ticket={"ticket_id": "BLG-001"},
        fingerprint=fingerprint,
        owner_repo_root=owner_repo_root,
        repo_root=repo_root,
        scope_repo_input=None,
        cli_repo_input=None,
    )

    assert not stale_idea_path.exists()


def test_reports_export_tickets_sweeps_actioned_queue_duplicates_not_in_backlog(
    tmp_path: Path,
) -> None:
    repo_root = _make_repo_root(tmp_path)
    runs_dir = tmp_path / "runs" / "usertest"
    compiled_dir = runs_dir / "target_a" / "_compiled"
    owner_repo = tmp_path / "owner_repo"
    owner_repo.mkdir(parents=True, exist_ok=True)

    backlog_path = compiled_dir / "target_a.backlog.json"
    _write_json(
        backlog_path,
        {
            "schema_version": 1,
            "scope": {"repo_input": str(owner_repo)},
            "tickets": [
                {
                    "ticket_id": "BLG-001",
                    "title": "Something else",
                    "problem": "Irrelevant for sweep.",
                    "severity": "low",
                    "confidence": 0.5,
                    "stage": "ready_for_ticket",
                    "evidence_atom_ids": [],
                    "change_surface": {"user_visible": False, "kinds": [], "notes": ""},
                    "breadth": {
                        "missions": 1,
                        "targets": 1,
                        "repo_inputs": 1,
                        "agents": 1,
                        "runs": 1,
                    },
                    "suggested_owner": "docs",
                }
            ],
        },
    )

    actions_path = tmp_path / "backlog_actions.yaml"
    _write_yaml(actions_path, {"version": 1, "actions": []})
    atom_actions_path = tmp_path / "backlog_atom_actions.yaml"
    _write_yaml(atom_actions_path, {"version": 1, "atoms": []})

    # Fingerprint not present in backlog: should still be swept.
    stale_fp = "deadbeefdeadbeef"
    complete_dir = owner_repo / ".agents" / "plans" / "5 - complete"
    complete_dir.mkdir(parents=True, exist_ok=True)
    (complete_dir / f"20260211_BLG-999_{stale_fp}_already-done.md").write_text(
        "# Already done\n",
        encoding="utf-8",
    )
    ideas_dir = owner_repo / ".agents" / "plans" / "1 - ideas"
    ideas_dir.mkdir(parents=True, exist_ok=True)
    stale_idea_path = ideas_dir / f"20260212_BLG-999_{stale_fp}_stale-queue-copy.md"
    stale_idea_path.write_text("# Stale copy\n", encoding="utf-8")
    assert stale_idea_path.exists()

    with pytest.raises(SystemExit) as exc:
        main(
            [
                "reports",
                "export-tickets",
                "--repo-root",
                str(repo_root),
                "--runs-dir",
                str(runs_dir),
                "--target",
                "target_a",
                "--actions-yaml",
                str(actions_path),
                "--atom-actions-yaml",
                str(atom_actions_path),
            ]
        )
    assert exc.value.code == 0

    export_doc = json.loads(
        (compiled_dir / "target_a.tickets_export.json").read_text(encoding="utf-8")
    )
    assert export_doc["stats"]["swept_actioned_queue_dupes_removed"] >= 1
    assert not stale_idea_path.exists()


def test_reports_export_tickets_sweeps_actioned_bucket_duplicates_not_in_backlog(
    tmp_path: Path,
) -> None:
    repo_root = _make_repo_root(tmp_path)
    runs_dir = tmp_path / "runs" / "usertest"
    compiled_dir = runs_dir / "target_a" / "_compiled"
    owner_repo = tmp_path / "owner_repo"
    owner_repo.mkdir(parents=True, exist_ok=True)

    backlog_path = compiled_dir / "target_a.backlog.json"
    _write_json(
        backlog_path,
        {
            "schema_version": 1,
            "scope": {"repo_input": str(owner_repo)},
            "tickets": [
                {
                    "ticket_id": "BLG-001",
                    "title": "Sweep trigger",
                    "problem": "Irrelevant for sweep.",
                    "severity": "low",
                    "confidence": 0.5,
                    "stage": "ready_for_ticket",
                    "evidence_atom_ids": [],
                    "change_surface": {"user_visible": False, "kinds": [], "notes": ""},
                    "breadth": {
                        "missions": 1,
                        "targets": 1,
                        "repo_inputs": 1,
                        "agents": 1,
                        "runs": 1,
                    },
                    "suggested_owner": "docs",
                }
            ],
        },
    )

    actions_path = tmp_path / "backlog_actions.yaml"
    _write_yaml(actions_path, {"version": 1, "actions": []})
    atom_actions_path = tmp_path / "backlog_atom_actions.yaml"
    _write_yaml(atom_actions_path, {"version": 1, "atoms": []})

    stale_fp = "deadbeefdeadbeef"
    in_progress_dir = owner_repo / ".agents" / "plans" / "3 - in_progress"
    complete_dir = owner_repo / ".agents" / "plans" / "5 - complete"
    in_progress_dir.mkdir(parents=True, exist_ok=True)
    complete_dir.mkdir(parents=True, exist_ok=True)

    in_progress_path = in_progress_dir / f"20260212_BLG-999_{stale_fp}_stale-in-progress.md"
    complete_path = complete_dir / f"20260212_BLG-999_{stale_fp}_done.md"
    in_progress_path.write_text("# In progress\n", encoding="utf-8")
    complete_path.write_text("# Done\n", encoding="utf-8")
    assert in_progress_path.exists()
    assert complete_path.exists()

    with pytest.raises(SystemExit) as exc:
        main(
            [
                "reports",
                "export-tickets",
                "--repo-root",
                str(repo_root),
                "--runs-dir",
                str(runs_dir),
                "--target",
                "target_a",
                "--actions-yaml",
                str(actions_path),
                "--atom-actions-yaml",
                str(atom_actions_path),
            ]
        )
    assert exc.value.code == 0

    export_doc = json.loads(
        (compiled_dir / "target_a.tickets_export.json").read_text(encoding="utf-8")
    )
    assert export_doc["stats"]["swept_actioned_bucket_dupes_removed"] >= 1
    assert not in_progress_path.exists()
    assert complete_path.exists()


def test_reports_export_tickets_attaches_ux_review_and_promotes_docs(tmp_path: Path) -> None:
    repo_root = _make_repo_root(tmp_path)
    runs_dir = tmp_path / "runs" / "usertest"
    compiled_dir = runs_dir / "target_a" / "_compiled"
    owner_repo = tmp_path / "owner_repo"
    owner_repo.mkdir(parents=True, exist_ok=True)

    ticket = {
        "ticket_id": "BLG-001",
        "title": "Add `usertest smoke` shortcut command",
        "problem": "Operators want a single obvious entry point.",
        "severity": "medium",
        "confidence": 0.6,
        "stage": "research_required",
        "evidence_atom_ids": ["target_a/20260101T000000Z/codex/0:confusion_point:1"],
        "change_surface": {
            "user_visible": True,
            "kinds": ["new_command"],
            "notes": "New command proposed.",
        },
        "breadth": {"missions": 1, "targets": 1, "repo_inputs": 1, "agents": 1, "runs": 1},
        "suggested_owner": "docs",
    }

    backlog_path = compiled_dir / "target_a.backlog.json"
    _write_json(
        backlog_path,
        {
            "schema_version": 1,
            "scope": {"repo_input": str(owner_repo)},
            "tickets": [ticket],
        },
    )
    _write_json(
        compiled_dir / "target_a.ux_review.json",
        {
            "schema_version": 1,
            "generated_at": "2026-02-21T00:00:00Z",
            "scope": {"target": "target_a", "repo_input": None},
            "status": "ok",
            "prompt_hash": "deadbeefdeadbeef",
            "review": {
                "command_surface_budget": {
                    "max_new_commands_per_quarter": 0,
                    "notes": "Keep it tight.",
                },
                "recommendations": [
                    {
                        "recommendation_id": "UX-001",
                        "ticket_ids": ["BLG-001"],
                        "recommended_approach": "docs",
                        "proposed_change_surface": {
                            "user_visible": True,
                            "kinds": ["docs_change"],
                            "notes": "Document existing commands instead of adding a new one.",
                        },
                        "rationale": "A new command isn't necessary; docs can remove friction.",
                        "next_steps": ["Update README quickstart with a clear entrypoint."],
                        "evidence_breadth_summary": {
                            "missions": 1,
                            "targets": 1,
                            "repo_inputs": 1,
                            "agents": 1,
                            "runs": 1,
                        },
                    }
                ],
                "notes": "",
                "confidence": 0.8,
            },
        },
    )

    actions_path = tmp_path / "backlog_actions.yaml"
    _write_yaml(actions_path, {"version": 1, "actions": []})
    atom_actions_path = tmp_path / "backlog_atom_actions.yaml"
    _write_yaml(atom_actions_path, {"version": 1, "atoms": []})

    with pytest.raises(SystemExit) as exc:
        main(
            [
                "reports",
                "export-tickets",
                "--repo-root",
                str(repo_root),
                "--runs-dir",
                str(runs_dir),
                "--target",
                "target_a",
                "--actions-yaml",
                str(actions_path),
                "--atom-actions-yaml",
                str(atom_actions_path),
                "--skip-plan-folder-dedupe",
            ]
        )
    assert exc.value.code == 0

    out_json = compiled_dir / "target_a.tickets_export.json"
    export_doc = json.loads(out_json.read_text(encoding="utf-8"))
    assert export_doc["stats"]["ux_recommendations_loaded"] == 1
    assert export_doc["stats"]["ux_idea_files_updated"] == 1
    assert export_doc["stats"]["exports_total"] == 1

    export = export_doc["exports"][0]
    assert export["export_kind"] == "implementation"
    assert export["source_ticket"]["stage"] == "ready_for_ticket"
    assert "ux:docs" in export["labels"]
    assert "## UX review" in export["body_markdown"]
    assert "Raw recommendation JSON" in export["body_markdown"]

    idea_path = Path(export["owner_repo"]["idea_path"])
    assert idea_path.exists()
    idea_text = idea_path.read_text(encoding="utf-8")
    assert "## UX review" in idea_text
    assert "- Export kind: `implementation`" in idea_text
    assert "- Stage: `ready_for_ticket`" in idea_text


def test_reports_export_tickets_promotes_high_surface_ready_ticket_with_docs_recommendation(
    tmp_path: Path,
) -> None:
    repo_root = _make_repo_root(tmp_path)
    runs_dir = tmp_path / "runs" / "usertest"
    compiled_dir = runs_dir / "target_a" / "_compiled"
    owner_repo = tmp_path / "owner_repo"
    owner_repo.mkdir(parents=True, exist_ok=True)

    ticket = {
        "ticket_id": "BLG-002",
        "title": "Add `usertest smoke` shortcut command",
        "problem": "Operators want a single obvious entry point.",
        "severity": "low",
        "confidence": 0.6,
        "stage": "ready_for_ticket",
        "evidence_atom_ids": ["target_a/20260102T000000Z/claude/0:report_validation_error:1"],
        "change_surface": {
            "user_visible": True,
            "kinds": ["new_command"],
            "notes": "New command proposed.",
        },
        "breadth": {"missions": 3, "targets": 2, "repo_inputs": 2, "agents": 2, "runs": 8},
        "suggested_owner": "runner_core",
    }

    backlog_path = compiled_dir / "target_a.backlog.json"
    _write_json(
        backlog_path,
        {
            "schema_version": 1,
            "scope": {"repo_input": str(owner_repo)},
            "tickets": [ticket],
        },
    )
    _write_json(
        compiled_dir / "target_a.ux_review.json",
        {
            "schema_version": 1,
            "generated_at": "2026-02-21T00:00:00Z",
            "scope": {"target": "target_a", "repo_input": None},
            "status": "ok",
            "review": {
                "recommendations": [
                    {
                        "recommendation_id": "UX-001",
                        "ticket_ids": ["BLG-002"],
                        "recommended_approach": "docs",
                        "rationale": "Prefer docs over new command.",
                        "next_steps": ["Document the existing command flow."],
                        "evidence_breadth_summary": {
                            "missions": 3,
                            "targets": 2,
                            "repo_inputs": 2,
                            "agents": 2,
                            "runs": 8,
                        },
                    }
                ],
                "confidence": 0.8,
            },
        },
    )

    actions_path = tmp_path / "backlog_actions.yaml"
    _write_yaml(actions_path, {"version": 1, "actions": []})
    atom_actions_path = tmp_path / "backlog_atom_actions.yaml"
    _write_yaml(atom_actions_path, {"version": 1, "atoms": []})

    with pytest.raises(SystemExit) as exc:
        main(
            [
                "reports",
                "export-tickets",
                "--repo-root",
                str(repo_root),
                "--runs-dir",
                str(runs_dir),
                "--target",
                "target_a",
                "--actions-yaml",
                str(actions_path),
                "--atom-actions-yaml",
                str(atom_actions_path),
                "--skip-plan-folder-dedupe",
            ]
        )
    assert exc.value.code == 0

    out_json = compiled_dir / "target_a.tickets_export.json"
    export_doc = json.loads(out_json.read_text(encoding="utf-8"))
    assert export_doc["stats"]["exports_total"] == 1
    export = export_doc["exports"][0]
    assert export["export_kind"] == "implementation"
    assert export["source_ticket"]["stage"] == "ready_for_ticket"
    assert "ux:docs" in export["labels"]

    idea_path = Path(export["owner_repo"]["idea_path"])
    assert idea_path.exists()
    idea_text = idea_path.read_text(encoding="utf-8")
    assert "- Export kind: `implementation`" in idea_text


def test_reports_export_tickets_updates_existing_plan_ticket_with_ux_review(tmp_path: Path) -> None:
    repo_root = _make_repo_root(tmp_path)
    runs_dir = tmp_path / "runs" / "usertest"
    compiled_dir = runs_dir / "target_a" / "_compiled"
    owner_repo = tmp_path / "owner_repo"
    owner_repo.mkdir(parents=True, exist_ok=True)

    ticket = {
        "ticket_id": "BLG-001",
        "title": "Add `usertest smoke` shortcut command",
        "problem": "Operators want a single obvious entry point.",
        "severity": "medium",
        "confidence": 0.6,
        "stage": "research_required",
        "evidence_atom_ids": ["target_a/20260101T000000Z/codex/0:confusion_point:1"],
        "change_surface": {
            "user_visible": True,
            "kinds": ["new_command"],
            "notes": "New command proposed.",
        },
        "breadth": {"missions": 1, "targets": 1, "repo_inputs": 1, "agents": 1, "runs": 1},
        "suggested_owner": "docs",
    }

    fingerprint = ticket_export_fingerprint(ticket)
    ready_dir = owner_repo / ".agents" / "plans" / "2 - ready"
    ready_dir.mkdir(parents=True, exist_ok=True)
    plan_path = ready_dir / f"20260221_BLG-001_{fingerprint}_existing.md"
    plan_path.write_text(
        "\n".join(
            [
                "# [Research] Existing ticket",
                "",
                f"- Fingerprint: `{fingerprint}`",
                "- Source ticket: `BLG-001`",
                "",
                "- Export kind: `research`",
                "- Stage: `research_required`",
                "",
            ]
        ),
        encoding="utf-8",
    )

    backlog_path = compiled_dir / "target_a.backlog.json"
    _write_json(
        backlog_path,
        {
            "schema_version": 1,
            "scope": {"repo_input": str(owner_repo)},
            "tickets": [ticket],
        },
    )
    _write_json(
        compiled_dir / "target_a.ux_review.json",
        {
            "schema_version": 1,
            "generated_at": "2026-02-21T00:00:00Z",
            "scope": {"target": "target_a", "repo_input": None},
            "status": "ok",
            "review": {
                "recommendations": [
                    {
                        "recommendation_id": "UX-001",
                        "ticket_ids": ["BLG-001"],
                        "recommended_approach": "docs",
                        "rationale": "A new command isn't necessary.",
                        "next_steps": ["Update docs instead."],
                        "evidence_breadth_summary": {
                            "missions": 1,
                            "targets": 1,
                            "repo_inputs": 1,
                            "agents": 1,
                            "runs": 1,
                        },
                    }
                ],
                "confidence": 0.7,
            },
        },
    )

    actions_path = tmp_path / "backlog_actions.yaml"
    _write_yaml(actions_path, {"version": 1, "actions": []})
    atom_actions_path = tmp_path / "backlog_atom_actions.yaml"
    _write_yaml(atom_actions_path, {"version": 1, "atoms": []})

    with pytest.raises(SystemExit) as exc:
        main(
            [
                "reports",
                "export-tickets",
                "--repo-root",
                str(repo_root),
                "--runs-dir",
                str(runs_dir),
                "--target",
                "target_a",
                "--actions-yaml",
                str(actions_path),
                "--atom-actions-yaml",
                str(atom_actions_path),
            ]
        )
    assert exc.value.code == 0

    out_json = compiled_dir / "target_a.tickets_export.json"
    export_doc = json.loads(out_json.read_text(encoding="utf-8"))
    assert export_doc["stats"]["exports_total"] == 0
    assert export_doc["stats"]["skipped_existing_plan"] == 1
    assert export_doc["stats"]["ux_plan_tickets_updated"] == 1

    updated = plan_path.read_text(encoding="utf-8")
    assert "- Export kind: `implementation`" in updated
    assert "- Stage: `ready_for_ticket`" in updated
    assert "## UX review" in updated


def test_reports_export_tickets_defers_existing_plan_ticket_and_updates_actions(
    tmp_path: Path,
) -> None:
    repo_root = _make_repo_root(tmp_path)
    runs_dir = tmp_path / "runs" / "usertest"
    compiled_dir = runs_dir / "target_a" / "_compiled"
    owner_repo = tmp_path / "owner_repo"
    owner_repo.mkdir(parents=True, exist_ok=True)

    ticket = {
        "ticket_id": "BLG-009",
        "title": "Add `usertest smoke` shortcut command",
        "problem": "Operators want a single obvious entry point.",
        "severity": "low",
        "confidence": 0.6,
        "stage": "ready_for_ticket",
        "evidence_atom_ids": ["target_a/20260101T000000Z/codex/0:confusion_point:1"],
        "change_surface": {
            "user_visible": True,
            "kinds": ["new_command"],
            "notes": "New command proposed.",
        },
        "breadth": {"missions": 1, "targets": 1, "repo_inputs": 1, "agents": 1, "runs": 1},
        "suggested_owner": "docs",
    }
    fingerprint = ticket_export_fingerprint(ticket)
    ready_dir = owner_repo / ".agents" / "plans" / "2 - ready"
    ready_dir.mkdir(parents=True, exist_ok=True)
    plan_path = ready_dir / f"20260221_BLG-009_{fingerprint}_existing.md"
    plan_path.write_text(
        "\n".join(
            [
                "# [Research] Existing ticket",
                "",
                f"- Fingerprint: `{fingerprint}`",
                "- Source ticket: `BLG-009`",
                "",
                "- Export kind: `research`",
                "- Stage: `ready_for_ticket`",
                "",
            ]
        ),
        encoding="utf-8",
    )

    backlog_path = compiled_dir / "target_a.backlog.json"
    _write_json(
        backlog_path,
        {
            "schema_version": 1,
            "scope": {"repo_input": str(owner_repo)},
            "tickets": [ticket],
        },
    )
    _write_json(
        compiled_dir / "target_a.ux_review.json",
        {
            "schema_version": 1,
            "generated_at": "2026-02-21T00:00:00Z",
            "scope": {"target": "target_a", "repo_input": None},
            "status": "ok",
            "review": {
                "recommendations": [
                    {
                        "recommendation_id": "UX-001",
                        "ticket_ids": ["BLG-009"],
                        "recommended_approach": "defer",
                        "rationale": "Defer the new command.",
                        "next_steps": ["No action."],
                        "evidence_breadth_summary": {
                            "missions": 1,
                            "targets": 1,
                            "repo_inputs": 1,
                            "agents": 1,
                            "runs": 1,
                        },
                    }
                ],
                "confidence": 0.7,
            },
        },
    )

    actions_path = tmp_path / "backlog_actions.yaml"
    _write_yaml(actions_path, {"version": 1, "actions": []})
    atom_actions_path = tmp_path / "backlog_atom_actions.yaml"
    _write_yaml(atom_actions_path, {"version": 1, "atoms": []})

    with pytest.raises(SystemExit) as exc:
        main(
            [
                "reports",
                "export-tickets",
                "--repo-root",
                str(repo_root),
                "--runs-dir",
                str(runs_dir),
                "--target",
                "target_a",
                "--actions-yaml",
                str(actions_path),
                "--atom-actions-yaml",
                str(atom_actions_path),
            ]
    )
    assert exc.value.code == 0

    out_json = compiled_dir / "target_a.tickets_export.json"
    export_doc = json.loads(out_json.read_text(encoding="utf-8"))
    assert export_doc["stats"]["exports_total"] == 0
    assert export_doc["stats"]["skipped_existing_plan"] == 1
    assert export_doc["stats"]["ux_tickets_deferred"] == 1

    deferred_dir = owner_repo / ".agents" / "plans" / "0.1 - deferred"
    deferred_matches = list(deferred_dir.glob(f"*{fingerprint}*.md"))
    assert deferred_matches
    assert not plan_path.exists()

    actions_doc = yaml.safe_load(actions_path.read_text(encoding="utf-8"))
    actions_by_fp = {item["fingerprint"]: item for item in actions_doc["actions"]}
    assert actions_by_fp[fingerprint]["status"] == "deferred"


def test_reports_export_tickets_defer_moves_bucket_and_skips_export(tmp_path: Path) -> None:
    repo_root = _make_repo_root(tmp_path)
    runs_dir = tmp_path / "runs" / "usertest"
    compiled_dir = runs_dir / "target_a" / "_compiled"
    owner_repo = tmp_path / "owner_repo"
    owner_repo.mkdir(parents=True, exist_ok=True)

    ticket = {
        "ticket_id": "BLG-008",
        "title": "Batch validation UX: aggregate and print all validation errors",
        "problem": "Output is unclear.",
        "severity": "high",
        "confidence": 0.8,
        "stage": "research_required",
        "evidence_atom_ids": ["target_a/20260101T000000Z/codex/0:confusion_point:1"],
        "change_surface": {
            "user_visible": True,
            "kinds": ["behavior_change"],
            "notes": "",
        },
        "breadth": {"missions": 1, "targets": 1, "repo_inputs": 1, "agents": 1, "runs": 1},
        "suggested_owner": "docs",
    }

    fingerprint = ticket_export_fingerprint(ticket)

    backlog_path = compiled_dir / "target_a.backlog.json"
    _write_json(
        backlog_path,
        {
            "schema_version": 1,
            "scope": {"repo_input": str(owner_repo)},
            "tickets": [ticket],
        },
    )
    _write_json(
        compiled_dir / "target_a.ux_review.json",
        {
            "schema_version": 1,
            "generated_at": "2026-02-21T00:00:00Z",
            "scope": {"target": "target_a", "repo_input": None},
            "status": "ok",
            "review": {
                "recommendations": [
                    {
                        "recommendation_id": "UX-001",
                        "ticket_ids": ["BLG-008"],
                        "recommended_approach": "defer",
                        "rationale": "Already implemented; defer.",
                        "next_steps": ["Re-triage as already implemented."],
                        "evidence_breadth_summary": {
                            "missions": 1,
                            "targets": 1,
                            "repo_inputs": 1,
                            "agents": 1,
                            "runs": 1,
                        },
                    }
                ],
                "confidence": 0.7,
            },
        },
    )

    actions_path = tmp_path / "backlog_actions.yaml"
    _write_yaml(actions_path, {"version": 1, "actions": []})

    atom_actions_path = tmp_path / "backlog_atom_actions.yaml"
    _write_yaml(
        atom_actions_path,
        {
            "version": 1,
            "atoms": [
                {
                    "atom_id": "target_a/20260101T000000Z/codex/0:confusion_point:1",
                    "status": "ticketed",
                    "ticket_ids": ["BLG-008"],
                }
            ],
        },
    )

    with pytest.raises(SystemExit) as exc:
        main(
            [
                "reports",
                "export-tickets",
                "--repo-root",
                str(repo_root),
                "--runs-dir",
                str(runs_dir),
                "--target",
                "target_a",
                "--actions-yaml",
                str(actions_path),
                "--atom-actions-yaml",
                str(atom_actions_path),
                "--skip-plan-folder-dedupe",
            ]
        )
    assert exc.value.code == 0

    out_json = compiled_dir / "target_a.tickets_export.json"
    export_doc = json.loads(out_json.read_text(encoding="utf-8"))
    assert export_doc["stats"]["ux_tickets_deferred"] == 1
    assert export_doc["stats"]["exports_total"] == 0
    assert export_doc["stats"]["idea_files_written"] == 1
    assert export_doc["exports"] == []

    deferred_dir = owner_repo / ".agents" / "plans" / "0.1 - deferred"
    deferred_matches = list(deferred_dir.glob(f"*{fingerprint}*.md"))
    assert deferred_matches

    actions_doc = yaml.safe_load(actions_path.read_text(encoding="utf-8"))
    assert actions_doc["version"] == 1
    actions_by_fp = {item["fingerprint"]: item for item in actions_doc["actions"]}
    assert actions_by_fp[fingerprint]["status"] == "deferred"

    atom_actions_doc = yaml.safe_load(atom_actions_path.read_text(encoding="utf-8"))
    atoms = {item["atom_id"]: item for item in atom_actions_doc["atoms"]}
    assert atoms["target_a/20260101T000000Z/codex/0:confusion_point:1"]["status"] == "actioned"
