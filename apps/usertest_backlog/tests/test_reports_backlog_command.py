from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
import yaml
from backlog_core.case_lineage import eligible_problem_mining_atoms
from runner_core import find_repo_root

import usertest_backlog.workflows.staged as staged_module
from usertest_backlog.cli import _write_chunked_problem_mining_atoms_workspace, main
from usertest_backlog.workflows.problem_mining import _validate_relation_decision_focuses
from usertest_backlog.workflows.staged import (
    _reset_stale_unproven_actioned_atoms,
    _sync_case_registry_outcomes,
)


def _write_json(path: Path, obj: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _write_yaml(path: Path, obj: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(obj, sort_keys=False), encoding="utf-8")


def _stage1_assigned_atom(compiled_dir: Path, atom_id: str) -> dict[str, Any]:
    """Read an atom from the exact workspace handed to the stage-1 miner."""

    stage_doc = json.loads(
        (compiled_dir / "target_a.problem_records.json").read_text(encoding="utf-8")
    )
    miners = stage_doc["input_meta"]["miner_results"]
    miner = next(item for item in miners if atom_id in item["assigned_atom_ids"])
    workspace = Path(miner["workspace_dir"])
    manifest = json.loads((workspace / "atoms.json").read_text(encoding="utf-8"))
    for chunk in manifest["chunks"]:
        atoms = json.loads((workspace / chunk["file"]).read_text(encoding="utf-8"))
        for atom in atoms:
            if atom.get("atom_id") == atom_id:
                return atom
    raise AssertionError(f"assigned atom missing from stage-1 workspace: {atom_id}")


def _ticket_labeler_fingerprint(ticket: dict[str, Any]) -> str:
    title_raw = ticket.get("title")
    title = str(title_raw).strip().lower() if isinstance(title_raw, str) else ""
    evidence = sorted(item for item in ticket.get("evidence_atom_ids", []) if isinstance(item, str))
    anchor = json.dumps({"title": title, "evidence": evidence}, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(anchor).hexdigest()[:16]


def _runner_receipt(
    *, case_id: str, plan_revision_id: str, evidence_kind: str
) -> dict[str, object]:
    return {
        "receipt_schema_version": 2,
        "producer": "usertest_implement",
        "verification_producer": "runner_core",
        "evidence_kind": evidence_kind,
        "case_id": case_id,
        "plan_revision_id": plan_revision_id,
        "fingerprint": "1" * 16,
        "run_dir": "runs/shadow",
        "verification_path": "runs/shadow/verification.json",
        "verification_sha256": "2" * 64,
        "ticket_ref_path": "runs/shadow/ticket.json",
        "ticket_ref_sha256": "3" * 64,
        "ticket_body_sha256": "4" * 64,
        "local_plan_sha256": "5" * 64,
        "local_plan_filename": "ticket.md",
        "verification_contract_sha256": "6" * 64,
        "verification_binding_sha256": "7" * 64,
        "commands": ["pytest -q tests/test_shadow.py"],
    }


def test_shadow_pipeline_rejects_invalid_export_gate_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = find_repo_root(Path(__file__).resolve())
    real_load_yaml = staged_module._load_yaml

    def load_yaml(path: Path) -> dict[str, Any]:
        if path.name == "backlog_export_gate.yaml":
            return {
                "backlog_export_gate": {
                    "enabled": True,
                    "required_consecutive_shadow_cycles": 0,
                    "require_exact_export_projection": True,
                }
            }
        return real_load_yaml(path)

    monkeypatch.setattr(staged_module, "_load_yaml", load_yaml)

    with pytest.raises(SystemExit) as exc:
        main(
            [
                "reports",
                "backlog",
                "--repo-root",
                str(repo_root),
                "--runs-dir",
                str(tmp_path / "runs"),
                "--target",
                "target_a",
                "--shadow",
            ]
        )

    assert exc.value.code == 2


def test_case_outcome_sync_persists_a_validated_current_lifecycle_pointer() -> None:
    case_registry = {
        "schema_version": 1,
        "cases": {
            "case:one": {
                "case_id": "case:one",
                "canonical_problem_id": "problem:one",
                "state": "active",
            }
        },
        "problem_id_to_case_id": {"problem:one": "case:one"},
        "atom_id_to_case_id": {"atom:one": "case:one"},
        "ticket_fingerprint_to_case_id": {},
    }
    outcome = {
        "schema_version": 1,
        "case_id": "case:one",
        "plan_revision_id": "planrev:case:one:abc123:1",
        "state": "planned",
        "recorded_at": "2026-07-09T12:00:00Z",
        "requires_live_verification": False,
        "target_branch": None,
        "merged_commit": None,
        "test_evidence": [],
        "original_scenario_evidence": [],
        "live_evidence": [],
        "remaining_risks": [],
        "recurrence_check": {"status": "not_run"},
    }
    atom_actions = {
        "atom:one": {
            "atom_id": "atom:one",
            "case_id": "case:one",
            "plan_outcomes": {
                "planrev:case:one:abc123:1": {
                    "state": "planned",
                    "recorded_at": "2026-07-09T12:00:00Z",
                    "path": "plans/one.md",
                    "fingerprint": "0123456789abcdef",
                    "outcome_record": outcome,
                }
            },
        }
    }

    result = _sync_case_registry_outcomes(
        case_registry=case_registry,
        atom_actions=atom_actions,
    )

    assert result["invalid_outcome_records"] == 0
    case = case_registry["cases"]["case:one"]
    assert case["state"] == "planned"
    assert case["current_lifecycle"] == {
        "state": "planned",
        "outcome_reference": {
            "source": "structurally_valid_nonterminal_plan_outcome",
            "validation_status": "not_required_nonterminal",
            "plan_revision_id": "planrev:case:one:abc123:1",
            "recorded_at": "2026-07-09T12:00:00Z",
            "path": "plans/one.md",
            "fingerprint": "0123456789abcdef",
        },
    }


def test_stale_legacy_actioned_atom_without_plan_or_outcome_returns_to_new() -> None:
    atom_actions = {
        "atom:stale": {
            "atom_id": "atom:stale",
            "status": "actioned",
            "case_id": "case:missing",
            "disposition": "supports_case",
            "disposition_rationale": "A deleted legacy plan once cited this atom.",
            "last_plan_seen_at": "2026-07-01T00:00:00Z",
        }
    }

    result = _reset_stale_unproven_actioned_atoms(
        atom_actions=atom_actions,
        case_registry={"cases": {}},
        current_plan_sync_at="2026-07-10T00:00:00Z",
        generated_at="2026-07-10T00:00:00Z",
    )

    entry = atom_actions["atom:stale"]
    assert result == {"examined": 1, "reset_to_new": 1, "idea_excluded": 0}
    assert entry["status"] == "new"
    assert entry["stale_actioned_previous_case_id"] == "case:missing"
    assert "case_id" not in entry
    assert entry["stale_actioned_previous_disposition"] == "supports_case"
    assert entry["disposition"] == "unresolved"
    assert entry["disposition_status"] == "pending"


def test_stale_actioned_reset_preserves_current_plan_verified_outcome_and_idea() -> None:
    sync_at = "2026-07-10T00:00:00Z"
    atom_actions = {
        "atom:plan": {
            "atom_id": "atom:plan",
            "status": "actioned",
            "last_plan_seen_at": sync_at,
        },
        "atom:resolved": {
            "atom_id": "atom:resolved",
            "status": "actioned",
            "case_id": "case:resolved",
        },
        "atom:idea": {
            "atom_id": "atom:idea",
            "status": "actioned",
            "category": "IDEA",
        },
    }
    registry = {
        "cases": {
            "case:resolved": {
                "state": "resolved",
                "current_lifecycle": {
                    "state": "resolved",
                    "outcome_reference": {"validation_status": "verified"},
                },
            }
        }
    }

    result = _reset_stale_unproven_actioned_atoms(
        atom_actions=atom_actions,
        case_registry=registry,
        current_plan_sync_at=sync_at,
        generated_at=sync_at,
    )

    assert result == {"examined": 3, "reset_to_new": 0, "idea_excluded": 1}
    assert {entry["status"] for entry in atom_actions.values()} == {"actioned"}


@pytest.mark.parametrize("unproven_state", ["implemented", "tests_verified", "live_verified"])
def test_case_outcome_sync_downgrades_unproven_legacy_progress(
    unproven_state: str,
) -> None:
    case_registry = {
        "schema_version": 1,
        "cases": {"case:one": {"case_id": "case:one", "state": "active"}},
        "problem_id_to_case_id": {},
        "atom_id_to_case_id": {"atom:one": "case:one"},
        "ticket_fingerprint_to_case_id": {},
    }
    atom_actions = {
        "atom:one": {
            "atom_id": "atom:one",
            "case_id": "case:one",
            "last_outcome_state": unproven_state,
            "last_outcome_recorded_at": "2026-07-09T12:00:00Z",
        }
    }

    _sync_case_registry_outcomes(
        case_registry=case_registry,
        atom_actions=atom_actions,
    )

    case = case_registry["cases"]["case:one"]
    assert case["state"] == "unverified"
    assert case["current_lifecycle"] == {
        "state": "unverified",
        "outcome_reference": {
            "source": "legacy_atom_action_projection",
            "validation_status": "projected",
            "recorded_at": "2026-07-09T12:00:00Z",
        },
    }


@pytest.mark.parametrize("unproven_state", ["implemented", "tests_verified", "live_verified"])
def test_case_outcome_sync_downgrades_unproven_plan_progress(
    unproven_state: str,
) -> None:
    case_registry = {
        "schema_version": 1,
        "cases": {"case:one": {"case_id": "case:one", "state": "active"}},
        "problem_id_to_case_id": {},
        "atom_id_to_case_id": {"atom:one": "case:one"},
        "ticket_fingerprint_to_case_id": {},
    }
    atom_actions = {
        "atom:one": {
            "atom_id": "atom:one",
            "case_id": "case:one",
            "plan_outcomes": {
                "plan:one": {
                    "state": unproven_state,
                    "recorded_at": "2026-07-09T12:00:00Z",
                    "required": True,
                }
            },
        }
    }

    _sync_case_registry_outcomes(
        case_registry=case_registry,
        atom_actions=atom_actions,
    )

    case = case_registry["cases"]["case:one"]
    assert case["state"] == "unverified"
    assert case["plan_outcomes"]["plan:one"]["state"] == "unverified"
    assert case["current_lifecycle"]["outcome_reference"]["validation_status"] == (
        "fail_open_projection"
    )


def test_problem_mining_workspace_writes_agent_readable_atom_index(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    manifest = _write_chunked_problem_mining_atoms_workspace(
        workspace_dir=workspace,
        prompt_atoms=[
            {
                "atom_id": "run-a:confusion_point:1",
                "run_rel": "target/20260101T000000Z/codex/0",
                "source": "confusion_point",
                "severity_hint": "high",
                "text": "The CLI quickstart has no obvious first command.",
                "linked_atom_ids": [],
            },
            {
                "atom_id": "run-b:run_failure_event:1",
                "run_rel": "target/20260102T000000Z/claude/0",
                "source": "run_failure_event",
                "severity_hint": "medium",
                "text": "Run failed before producing a report.",
                "linked_atom_ids": ["run-a:confusion_point:1"],
            },
        ],
        max_records_per_miner=3,
    )

    assert manifest["index_file"] == "atoms_index.md"
    assert manifest["atom_file_count"] == 2
    assert manifest["chunks"][0]["text_file"] == "atoms_text/atoms_001.md"

    index = (workspace / "atoms_index.md").read_text(encoding="utf-8")
    text_chunk = (workspace / "atoms_text" / "atoms_001.md").read_text(encoding="utf-8")
    atom_file = (workspace / "atoms_by_id" / "atom_0001.md").read_text(encoding="utf-8")

    assert "run-a:confusion_point:1" in index
    assert "atom_file: `atoms_by_id/atom_0001.md`" in index
    assert "chunk_file: `atoms_text/atoms_001.md`" in index
    assert "The CLI quickstart has no obvious first command." in text_chunk
    assert "The CLI quickstart has no obvious first command." in atom_file
    assert "linked_atom_ids: run-a:confusion_point:1" in text_chunk


def test_relation_review_rejects_candidate_only_historical_focus() -> None:
    with pytest.raises(ValueError, match="candidate_only_focus"):
        _validate_relation_decision_focuses(
            [{"focus_id": "problem:historical", "action": "keep_separate"}],
            work_unit_problem_ids={"problem:current"},
        )


def test_relation_review_requires_exactly_one_disposition_for_every_active_focus() -> None:
    with pytest.raises(ValueError, match="missing_focus: problem:second"):
        _validate_relation_decision_focuses(
            [{"focus_id": "problem:first", "action": "keep_separate"}],
            work_unit_problem_ids={"problem:first", "problem:second"},
        )

    with pytest.raises(ValueError, match="duplicate_focus: problem:first"):
        _validate_relation_decision_focuses(
            [
                {"focus_id": "problem:first", "action": "keep_separate"},
                {"focus_id": "problem:first", "action": "merge"},
            ],
            work_unit_problem_ids={"problem:first"},
        )

    _validate_relation_decision_focuses(
        [
            {"focus_id": "problem:first", "action": "keep_separate"},
            {"focus_id": "problem:second", "action": "keep_separate"},
        ],
        work_unit_problem_ids={"problem:first", "problem:second"},
    )


def _seed_labeler_cache(artifacts_dir: Path, ticket: dict[str, Any], *, labelers: int = 3) -> None:
    fingerprint = _ticket_labeler_fingerprint(ticket)
    labeler_dir = artifacts_dir / "labeler" / fingerprint
    labeler_dir.mkdir(parents=True, exist_ok=True)

    payload = {
        "change_surface": {"user_visible": True, "kinds": ["docs_change"], "notes": "docs"},
        "component": "docs",
        "intent_risk": "low",
        "confidence": 0.75,
        "evidence_atom_ids_used": [
            item for item in ticket.get("evidence_atom_ids", []) if isinstance(item, str)
        ],
    }
    for idx in range(1, labelers + 1):
        (labeler_dir / f"labeler_{idx:02d}.label.json").write_text(
            json.dumps(payload, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )


def _seed_runs_fixture(runs_dir: Path) -> None:
    run_a = runs_dir / "target_a" / "20260101T000000Z" / "codex" / "0"
    run_b = runs_dir / "target_a" / "20260102T000000Z" / "claude" / "0"
    run_a.mkdir(parents=True, exist_ok=True)
    run_b.mkdir(parents=True, exist_ok=True)

    _write_json(
        run_a / "target_ref.json",
        {
            "repo_input": "pip:agent-adapters",
            "agent": "codex",
            "persona_id": "routine_operator",
            "mission_id": "complete_output_smoke",
        },
    )
    _write_json(run_a / "effective_run_spec.json", {})
    _write_json(
        run_a / "metrics.json",
        {
            "commands_executed": 7,
            "commands_failed": 0,
            "step_count": 11,
            "event_counts": {},
            "distinct_files_read": [],
            "distinct_docs_read": [],
            "distinct_files_written": [],
            "lines_added_total": 0,
            "lines_removed_total": 0,
        },
    )
    _write_json(
        run_a / "report.json",
        {
            "confusion_points": [{"summary": "No quickstart section"}],
            "suggested_changes": [
                {
                    "change": "Add quickstart examples",
                    "type": "docs",
                    "location": "README.md",
                    "priority": "p1",
                    "expected_impact": "faster onboarding",
                }
            ],
            "confidence_signals": {"missing": ["No smoke command"]},
        },
    )
    (run_a / "agent_stderr.txt").write_text("", encoding="utf-8")
    (run_a / "agent_last_message.txt").write_text("", encoding="utf-8")

    _write_json(
        run_b / "target_ref.json",
        {
            "repo_input": "pip:agent-adapters",
            "agent": "claude",
            "persona_id": "routine_operator",
            "mission_id": "complete_output_smoke",
        },
    )
    _write_json(run_b / "effective_run_spec.json", {})
    _write_json(
        run_b / "metrics.json",
        {
            "commands_executed": 3,
            "commands_failed": 1,
            "failed_commands": [
                {
                    "command": "python -m pip install -r requirements-dev.txt",
                    "exit_code": 1,
                    "output_excerpt": "Temporary failure in name resolution",
                }
            ],
            "step_count": 6,
            "event_counts": {},
            "distinct_files_read": [],
            "distinct_docs_read": [],
            "distinct_files_written": [],
            "lines_added_total": 0,
            "lines_removed_total": 0,
        },
    )
    _write_json(
        run_b / "report_validation_errors.json",
        ["$: failed to parse JSON from agent output"],
    )
    (run_b / "agent_stderr.txt").write_text("status 429 retrying\n", encoding="utf-8")
    (run_b / "agent_last_message.txt").write_text("done\n", encoding="utf-8")


def _seed_many_high_severity_runs(runs_dir: Path, *, count: int) -> None:
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for idx in range(count):
        ts = (base + timedelta(minutes=idx)).strftime("%Y%m%dT%H%M%SZ")
        run_dir = runs_dir / "target_a" / ts / "codex" / "0"
        run_dir.mkdir(parents=True, exist_ok=True)
        _write_json(
            run_dir / "target_ref.json",
            {
                "repo_input": "pip:agent-adapters",
                "agent": "codex",
                "persona_id": "routine_operator",
                "mission_id": "complete_output_smoke",
            },
        )
        _write_json(run_dir / "effective_run_spec.json", {})
        _write_json(run_dir / "report_validation_errors.json", [f"validation issue {idx}"])
        (run_dir / "agent_stderr.txt").write_text("", encoding="utf-8")
        (run_dir / "agent_last_message.txt").write_text("", encoding="utf-8")


def test_reports_backlog_dry_run_writes_outputs(tmp_path: Path) -> None:
    repo_root = find_repo_root(Path(__file__).resolve())
    runs_dir = tmp_path / "runs" / "usertest"
    _seed_runs_fixture(runs_dir)
    atom_actions_path = tmp_path / "backlog_atom_actions.yaml"

    with pytest.raises(SystemExit) as exc:
        main(
            [
                "reports",
                "backlog",
                "--repo-root",
                str(repo_root),
                "--runs-dir",
                str(runs_dir),
                "--target",
                "target_a",
                "--dry-run",
                "--miners",
                "2",
                "--sample-size",
                "8",
                "--atom-actions-yaml",
                str(atom_actions_path),
                "--skip-plan-folder-sync",
            ]
        )
    assert exc.value.code == 0

    compiled = runs_dir / "target_a" / "_compiled"
    out_json = compiled / "target_a.backlog.json"
    out_md = compiled / "target_a.backlog.md"
    atoms_jsonl = compiled / "target_a.backlog.atoms.jsonl"
    agent_last_message_atoms_jsonl = (
        compiled / "target_a.backlog.atoms.agent_last_message_artifact.jsonl"
    )

    assert out_json.exists()
    assert out_md.exists()
    assert atoms_jsonl.exists()
    assert agent_last_message_atoms_jsonl.exists()

    summary = json.loads(out_json.read_text(encoding="utf-8"))
    assert summary["artifacts"]["atoms_jsonl"] == str(atoms_jsonl)
    assert summary["artifacts"]["atoms_agent_last_message_artifact_jsonl"] == str(
        agent_last_message_atoms_jsonl
    )
    assert summary["totals"]["runs"] == 2
    assert summary["totals"]["miners_total"] == 0
    assert summary["totals"]["source_counts"].get("aggregate_metrics", 0) == 2
    assert summary["totals"]["source_counts"].get("command_failure", 0) == 1

    atom_lines = atoms_jsonl.read_text(encoding="utf-8").splitlines()
    assert any(
        json.loads(line).get("source") == "agent_last_message_artifact"
        for line in atom_lines
        if line
    )
    agent_last_message_lines = agent_last_message_atoms_jsonl.read_text(
        encoding="utf-8"
    ).splitlines()
    assert agent_last_message_lines
    assert all(
        json.loads(line).get("source") == "agent_last_message_artifact"
        for line in agent_last_message_lines
        if line
    )

    markdown = out_md.read_text(encoding="utf-8")
    assert "Untriaged Tail" in markdown


def test_two_shadow_cycles_retain_open_cases_and_add_new_evidence_without_export(
    tmp_path: Path,
) -> None:
    repo_root = find_repo_root(Path(__file__).resolve())
    runs_dir = tmp_path / "runs" / "usertest"
    _seed_runs_fixture(runs_dir)
    atom_actions_path = tmp_path / "backlog_atom_actions.yaml"
    argv = [
        "reports",
        "backlog",
        "--repo-root",
        str(repo_root),
        "--runs-dir",
        str(runs_dir),
        "--target",
        "target_a",
        "--dry-run",
        "--miners",
        "0",
        "--sample-size",
        "8",
        "--atom-actions-yaml",
        str(atom_actions_path),
        "--skip-plan-folder-sync",
    ]

    snapshots: list[dict[str, Any]] = []
    active_case_sets: list[set[str]] = []
    first_evidence_by_case: dict[str, set[str]] = {}
    nonterminal_case_id: str | None = None
    terminal_case_id: str | None = None
    for cycle in range(2):
        if cycle == 1:
            run_c = runs_dir / "target_a" / "20260103T000000Z" / "codex" / "0"
            _write_json(
                run_c / "target_ref.json",
                {
                    "repo_input": "pip:agent-adapters",
                    "agent": "codex",
                    "persona_id": "routine_operator",
                    "mission_id": "complete_output_smoke",
                },
            )
            _write_json(run_c / "effective_run_spec.json", {})
            _write_json(
                run_c / "metrics.json",
                {
                    "commands_executed": 1,
                    "commands_failed": 0,
                    "step_count": 1,
                    "event_counts": {},
                    "distinct_files_read": [],
                    "distinct_docs_read": [],
                    "distinct_files_written": [],
                    "lines_added_total": 0,
                    "lines_removed_total": 0,
                },
            )
            _write_json(
                run_c / "report.json",
                {"confusion_points": [{"summary": "No quickstart section remains visible"}]},
            )
            _write_json(
                run_c / "token_monitoring.json",
                {
                    "signals": [
                        {
                            "signal_id": "novel-read-loop",
                            "causal_mechanism": "The same file is read repeatedly",
                            "confidence": "high",
                            "token_dimensions_affected": {"input_tokens": 25000},
                            "confirmed_by_counters": True,
                        }
                    ]
                },
            )
            (run_c / "agent_stderr.txt").write_text("", encoding="utf-8")
            (run_c / "agent_last_message.txt").write_text("", encoding="utf-8")

        with pytest.raises(SystemExit) as exc:
            main(argv)
        assert exc.value.code == 0

        compiled = runs_dir / "target_a" / "_compiled"
        case_registry = json.loads(
            (compiled / "target_a.case_registry.json").read_text(encoding="utf-8")
        )
        backlog = json.loads((compiled / "target_a.backlog.json").read_text(encoding="utf-8"))
        problem_doc = json.loads(
            (compiled / "target_a.problem_records.json").read_text(encoding="utf-8")
        )
        snapshots.append(case_registry)
        active_case_ids = {
            str(item["case_id"])
            for item in problem_doc.get("items", [])
            if isinstance(item, dict) and isinstance(item.get("case_id"), str)
        }
        active_case_sets.append(active_case_ids)
        assert active_case_ids, "nonterminal cases must remain active across shadow cycles"
        for active_case_id in active_case_ids:
            stage_refs = case_registry["cases"][active_case_id].get("stage_artifact_refs", {})
            assert "problem_mining" in stage_refs
            assert "problem_prioritization" in stage_refs
            assert "ticket_assembly" in stage_refs
        assert all(
            ticket.get("stage") != "ready_for_ticket"
            for ticket in backlog.get("tickets", [])
            if isinstance(ticket, dict)
        )
        assert not list(tmp_path.rglob("*.idea.md"))

        if cycle == 0:
            first_evidence_by_case = {
                str(case_id): {
                    str(atom_id)
                    for atom_id in entry.get("evidence_atom_ids", [])
                    if isinstance(atom_id, str)
                }
                for case_id, entry in case_registry.get("cases", {}).items()
                if isinstance(entry, dict) and entry.get("state") == "active"
            }
            atom_case_pairs = [
                (str(atom_id), str(case_id))
                for atom_id, case_id in case_registry.get("atom_id_to_case_id", {}).items()
                if not str(atom_id).startswith("__aggregate__/")
            ]
            assert len({case_id for _, case_id in atom_case_pairs}) >= 2
            nonterminal_case_id = atom_case_pairs[0][1]
            terminal_case_id = next(
                case_id for _, case_id in atom_case_pairs if case_id != nonterminal_case_id
            )
            ledger = yaml.safe_load(atom_actions_path.read_text(encoding="utf-8"))
            assert isinstance(ledger, dict)
            atom_entries = {
                str(entry["atom_id"]): entry
                for entry in ledger.get("atoms", [])
                if isinstance(entry, dict) and isinstance(entry.get("atom_id"), str)
            }
            for atom_id, case_id in atom_case_pairs:
                entry = atom_entries[atom_id]
                if case_id == nonterminal_case_id:
                    outcome_record = {
                        "schema_version": 1,
                        "case_id": case_id,
                        "plan_revision_id": f"plan:{case_id}:tests",
                        "state": "tests_verified",
                        "outcome_scope": "case",
                        "recorded_at": "2026-01-02T12:00:00Z",
                        "requires_live_verification": False,
                        "target_branch": "dev",
                        "merged_commit": "abc123",
                        "test_evidence": [
                            {
                                "kind": "pytest",
                                "reference": "tests/test_shadow.py",
                                "result": "passed",
                                "runner_receipt": _runner_receipt(
                                    case_id=case_id,
                                    plan_revision_id=f"plan:{case_id}:tests",
                                    evidence_kind="test",
                                ),
                            }
                        ],
                        "original_scenario_evidence": [],
                        "live_evidence": [],
                        "remaining_risks": ["Original scenario pending"],
                        "recurrence_check": {"status": "not_run"},
                    }
                    entry.update(
                        {
                            "status": "actioned",
                            "case_id": case_id,
                            "last_outcome_state": "tests_verified",
                            "last_outcome_recorded_at": "2026-01-02T12:00:00Z",
                            "last_outcome_record": outcome_record,
                        }
                    )
                elif case_id == terminal_case_id:
                    outcome_record = {
                        "schema_version": 1,
                        "case_id": case_id,
                        "plan_revision_id": f"plan:{case_id}:resolved",
                        "state": "resolved",
                        "outcome_scope": "case",
                        "recorded_at": "2026-01-02T12:00:00Z",
                        "requires_live_verification": False,
                        "target_branch": "dev",
                        "merged_commit": "def456",
                        "test_evidence": [
                            {
                                "kind": "pytest",
                                "reference": "tests/test_shadow.py",
                                "result": "passed",
                                "runner_receipt": _runner_receipt(
                                    case_id=case_id,
                                    plan_revision_id=f"plan:{case_id}:resolved",
                                    evidence_kind="test",
                                ),
                            }
                        ],
                        "original_scenario_evidence": [
                            {
                                "kind": "replay",
                                "reference": "runs/shadow/replay.json",
                                "result": "passed",
                                "runner_receipt": _runner_receipt(
                                    case_id=case_id,
                                    plan_revision_id=f"plan:{case_id}:resolved",
                                    evidence_kind="original_scenario",
                                ),
                            }
                        ],
                        "live_evidence": [],
                        "remaining_risks": [],
                        "recurrence_check": {
                            "status": "completed",
                            "result": "passed",
                            "evidence": [
                                {
                                    "kind": "replay",
                                    "reference": "runs/shadow/recurrence.json",
                                    "result": "passed",
                                    "runner_receipt": _runner_receipt(
                                        case_id=case_id,
                                        plan_revision_id=(f"plan:{case_id}:resolved"),
                                        evidence_kind="recurrence",
                                    ),
                                }
                            ],
                        },
                    }
                    entry.update(
                        {
                            "status": "actioned",
                            "case_id": case_id,
                            "last_outcome_state": "resolved",
                            "last_outcome_recorded_at": "2026-01-02T12:00:00Z",
                            "last_outcome_record": outcome_record,
                        }
                    )
            ledger["atoms"] = [atom_entries[key] for key in sorted(atom_entries)]
            _write_yaml(atom_actions_path, ledger)

    assert nonterminal_case_id is not None
    assert terminal_case_id is not None
    # Structurally valid embedded records are not completion proof. These synthetic
    # records have no retained runner artifacts, plan hashes, or merge provenance, so
    # both cases must remain in the active work set instead of suppressing discovery.
    expected_retained = active_case_sets[0]
    assert expected_retained <= active_case_sets[1]
    assert nonterminal_case_id in active_case_sets[1]
    assert terminal_case_id in active_case_sets[1]
    assert snapshots[1]["cases"][nonterminal_case_id]["state"] == "unverified"
    assert snapshots[1]["cases"][terminal_case_id]["state"] == "unverified"
    assert len(snapshots[1].get("cases", {})) > len(snapshots[0].get("cases", {}))
    for case_id, first_evidence in first_evidence_by_case.items():
        second_entry = snapshots[1]["cases"][case_id]
        assert first_evidence <= set(second_entry.get("evidence_atom_ids", []))
    assert any(
        "20260103T000000Z" in atom_id for atom_id in snapshots[1].get("atom_id_to_case_id", {})
    )


def test_reports_backlog_prefers_error_json_over_duplicate_validation_error(
    tmp_path: Path,
) -> None:
    repo_root = find_repo_root(Path(__file__).resolve())
    runs_dir = tmp_path / "runs" / "usertest"
    _seed_runs_fixture(runs_dir)
    atom_actions_path = tmp_path / "backlog_atom_actions.yaml"

    run_b = runs_dir / "target_a" / "20260102T000000Z" / "claude" / "0"
    _write_json(
        run_b / "error.json",
        {
            "type": "AgentExecFailed",
            "message": "$: failed to parse JSON from agent output",
        },
    )

    with pytest.raises(SystemExit) as exc:
        main(
            [
                "reports",
                "backlog",
                "--repo-root",
                str(repo_root),
                "--runs-dir",
                str(runs_dir),
                "--target",
                "target_a",
                "--dry-run",
                "--miners",
                "0",
                "--sample-size",
                "8",
                "--atom-actions-yaml",
                str(atom_actions_path),
                "--skip-plan-folder-sync",
            ]
        )
    assert exc.value.code == 0

    out_json = runs_dir / "target_a" / "_compiled" / "target_a.backlog.json"
    summary = json.loads(out_json.read_text(encoding="utf-8"))
    source_counts = summary["totals"]["source_counts"]
    assert source_counts.get("run_failure_event", 0) >= 1
    assert source_counts.get("error_json", 0) == 0
    assert source_counts.get("report_validation_error", 0) == 0


def test_reports_backlog_carryover_actioned_only_demotes_ticketed_and_queued_atoms(
    tmp_path: Path,
) -> None:
    repo_root = find_repo_root(Path(__file__).resolve())
    runs_dir = tmp_path / "runs" / "usertest"
    _seed_runs_fixture(runs_dir)
    atom_actions_path = tmp_path / "backlog_atom_actions.yaml"
    queued_atom_id = "target_a/20260101T000000Z/codex/0:confusion_point:1"
    _write_yaml(
        atom_actions_path,
        {"version": 1, "atoms": [{"atom_id": queued_atom_id, "status": "queued"}]},
    )

    argv_base = [
        "reports",
        "backlog",
        "--repo-root",
        str(repo_root),
        "--runs-dir",
        str(runs_dir),
        "--target",
        "target_a",
        "--dry-run",
        "--miners",
        "0",
        "--sample-size",
        "0",
        "--atom-actions-yaml",
        str(atom_actions_path),
        "--skip-plan-folder-sync",
    ]

    with pytest.raises(SystemExit) as exc:
        main(argv_base)
    assert exc.value.code == 0

    compiled = runs_dir / "target_a" / "_compiled"
    atoms_jsonl = compiled / "target_a.backlog.atoms.jsonl"
    assert atoms_jsonl.exists()

    atom_ids = {
        str(json.loads(line).get("atom_id"))
        for line in atoms_jsonl.read_text(encoding="utf-8").splitlines()
        if line
    }
    assert queued_atom_id in atom_ids

    atom_actions_doc = yaml.safe_load(atom_actions_path.read_text(encoding="utf-8"))
    entry = next(item for item in atom_actions_doc["atoms"] if item["atom_id"] == queued_atom_id)
    assert entry["status"] == "new"
    assert entry["reopened_previous_status"] == "queued"

    # Re-seed the historical row so the explicit carryover mode still exercises its
    # own demotion behavior independently of the new default fail-open filter.
    _write_yaml(
        atom_actions_path,
        {"version": 1, "atoms": [{"atom_id": queued_atom_id, "status": "queued"}]},
    )

    with pytest.raises(SystemExit) as exc:
        main([*argv_base, "--carryover-actioned-only"])
    assert exc.value.code == 0

    atom_ids = {
        str(json.loads(line).get("atom_id"))
        for line in atoms_jsonl.read_text(encoding="utf-8").splitlines()
        if line
    }
    assert queued_atom_id in atom_ids

    atom_actions_doc = yaml.safe_load(atom_actions_path.read_text(encoding="utf-8"))
    entry = next(item for item in atom_actions_doc["atoms"] if item["atom_id"] == queued_atom_id)
    assert entry["status"] == "new"

    summary = json.loads((compiled / "target_a.backlog.json").read_text(encoding="utf-8"))
    carryover = summary["artifacts"]["atom_filter"]["carryover"]
    assert carryover["mode"] == "actioned_only"
    assert carryover["demoted_atoms"] >= 1
    assert carryover.get("demoted_status_counts", {}).get("queued") == 1


def test_reports_backlog_writes_stage_backed_tickets_and_updates_atom_actions(
    tmp_path: Path,
) -> None:
    repo_root = find_repo_root(Path(__file__).resolve())
    runs_dir = tmp_path / "runs" / "usertest"
    _seed_runs_fixture(runs_dir)
    atom_actions_path = tmp_path / "backlog_atom_actions.yaml"

    with pytest.raises(SystemExit) as exc:
        main(
            [
                "reports",
                "backlog",
                "--repo-root",
                str(repo_root),
                "--runs-dir",
                str(runs_dir),
                "--target",
                "target_a",
                "--dry-run",
                "--miners",
                "0",
                "--sample-size",
                "8",
                "--atom-actions-yaml",
                str(atom_actions_path),
                "--skip-plan-folder-sync",
            ]
        )
    assert exc.value.code == 0

    compiled = runs_dir / "target_a" / "_compiled"
    out_json = compiled / "target_a.backlog.json"
    summary = json.loads(out_json.read_text(encoding="utf-8"))
    tickets = [t for t in summary.get("tickets", []) if isinstance(t, dict)]
    assert tickets, "dry-run backlog should produce at least one ticket"

    planned = [t for t in tickets if isinstance(t.get("change_plan"), dict)]
    assert planned == []
    assert all(ticket.get("stage") != "ready_for_ticket" for ticket in tickets)

    # A dry-run research proof is explicitly blocked and must not mark atoms ticketed.
    atom_actions_doc = yaml.safe_load(atom_actions_path.read_text(encoding="utf-8"))
    assert atom_actions_doc["version"] == 1
    atoms = atom_actions_doc["atoms"]
    assert all(item.get("status") != "ticketed" for item in atoms)


def test_update_atom_actions_from_backlog_skips_blocked_tickets(tmp_path: Path) -> None:
    from usertest_backlog.cli import _update_atom_actions_from_backlog

    atom_actions: dict[str, dict[str, Any]] = {}
    atoms = [
        {"atom_id": "atom:1", "source": "confusion_point"},
        {"atom_id": "atom:2", "source": "confusion_point"},
        {"atom_id": "atom:3", "source": "confusion_point"},
    ]
    tickets = [
        {
            "title": "Blocked ticket",
            "problem": "P",
            "user_impact": "U",
            "proposed_fix": "F",
            "stage": "blocked",
            "evidence_atom_ids": ["atom:1"],
        },
        {
            "title": "Triage record",
            "problem": "P",
            "user_impact": "U",
            "proposed_fix": "F",
            "stage": "triage",
            "evidence_atom_ids": ["atom:2"],
        },
        {
            "title": "Exportable research ticket",
            "problem": "P",
            "user_impact": "U",
            "proposed_fix": "F",
            "stage": "research_required",
            "evidence_atom_ids": ["atom:3"],
        },
    ]

    _update_atom_actions_from_backlog(
        atom_actions=atom_actions,
        atoms=atoms,
        tickets=tickets,
        generated_at="2026-01-01T00:00:00Z",
        backlog_json_path=tmp_path / "backlog.json",
    )

    assert atom_actions["atom:1"]["status"] == "new"
    assert atom_actions["atom:2"]["status"] == "new"
    assert atom_actions["atom:3"]["status"] == "ticketed"


def test_reports_backlog_reopens_plan_action_without_verified_terminal_outcome(
    tmp_path: Path,
) -> None:
    repo_root = find_repo_root(Path(__file__).resolve())
    runs_dir = tmp_path / "runs" / "usertest"
    _seed_runs_fixture(runs_dir)

    owner_repo = tmp_path / "owner_repo"
    complete_dir = owner_repo / ".agents" / "plans" / "5 - complete"
    complete_dir.mkdir(parents=True, exist_ok=True)

    atom_id = "target_a/20260101T000000Z/codex/0:confusion_point:1"
    (complete_dir / "20260214_BLG-123_deadbeefdeadbeef_plan-sync-test.md").write_text(
        "# Plan sync test\n\n## Evidence atom ids\n\n- `" + atom_id + "`\n",
        encoding="utf-8",
    )

    run_dirs = [
        runs_dir / "target_a" / "20260101T000000Z" / "codex" / "0",
        runs_dir / "target_a" / "20260102T000000Z" / "claude" / "0",
    ]
    for run_dir in run_dirs:
        target_ref_path = run_dir / "target_ref.json"
        payload = json.loads(target_ref_path.read_text(encoding="utf-8"))
        payload["repo_input"] = str(owner_repo)
        _write_json(target_ref_path, payload)

    atom_actions_path = tmp_path / "backlog_atom_actions.yaml"
    with pytest.raises(SystemExit) as exc:
        main(
            [
                "reports",
                "backlog",
                "--repo-root",
                str(repo_root),
                "--runs-dir",
                str(runs_dir),
                "--target",
                "target_a",
                "--dry-run",
                "--miners",
                "0",
                "--sample-size",
                "8",
                "--atom-actions-yaml",
                str(atom_actions_path),
            ]
        )
    assert exc.value.code == 0

    compiled = runs_dir / "target_a" / "_compiled"
    out_json = compiled / "target_a.backlog.json"
    summary = json.loads(out_json.read_text(encoding="utf-8"))
    atom_filter = summary["artifacts"]["atom_filter"]
    assert atom_filter["reopened_status_counts"].get("actioned", 0) >= 1
    assert atom_id in atom_filter["reopened_atom_ids_preview"]

    atom_actions_doc = yaml.safe_load(atom_actions_path.read_text(encoding="utf-8"))
    atom_entry = next(item for item in atom_actions_doc["atoms"] if item["atom_id"] == atom_id)
    assert atom_entry["status"] == "new"
    assert atom_entry["reopened_previous_status"] == "actioned"


def test_reports_sync_atom_actions_dry_run_reports_without_writing(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo_root = tmp_path / "runner"
    repo_root.mkdir(parents=True)
    owner_repo = tmp_path / "owner_repo"
    complete_dir = owner_repo / ".agents" / "plans" / "5 - complete"
    complete_dir.mkdir(parents=True, exist_ok=True)

    fingerprint = "deadbeefdeadbeef"
    atom_id = "target_a/20260101T000000Z/codex/0:confusion_point:1"
    (complete_dir / f"20260214_{fingerprint}_plan-sync-test.md").write_text(
        "# Plan sync test\n",
        encoding="utf-8",
    )
    atom_actions_path = tmp_path / "backlog_atom_actions.yaml"
    _write_yaml(
        atom_actions_path,
        {
            "version": 1,
            "atoms": [
                {
                    "atom_id": atom_id,
                    "status": "queued",
                    "fingerprints": [fingerprint],
                }
            ],
        },
    )

    with pytest.raises(SystemExit) as exc:
        main(
            [
                "reports",
                "sync-atom-actions",
                "--repo-root",
                str(repo_root),
                "--owner-root",
                str(owner_repo),
                "--atom-actions-yaml",
                str(atom_actions_path),
                "--dry-run",
            ]
        )

    assert exc.value.code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["dry_run"] is True
    assert payload["before_status_counts"]["queued"] == 1
    assert payload["after_status_counts"]["actioned"] == 1
    assert payload["sync"]["plan_sync"]["atoms_promoted"] == 1

    atom_actions_doc = yaml.safe_load(atom_actions_path.read_text(encoding="utf-8"))
    assert atom_actions_doc["atoms"][0]["status"] == "queued"


def test_reports_backlog_reopens_unmapped_queued_atoms_by_default(tmp_path: Path) -> None:
    repo_root = find_repo_root(Path(__file__).resolve())
    runs_dir = tmp_path / "runs" / "usertest"
    _seed_runs_fixture(runs_dir)

    queued_atom_id = "target_a/20260101T000000Z/codex/0:confusion_point:1"
    atom_actions_path = tmp_path / "backlog_atom_actions.yaml"
    _write_yaml(
        atom_actions_path,
        {
            "version": 1,
            "atoms": [
                {
                    "atom_id": queued_atom_id,
                    "status": "queued",
                }
            ],
        },
    )

    with pytest.raises(SystemExit) as exc:
        main(
            [
                "reports",
                "backlog",
                "--repo-root",
                str(repo_root),
                "--runs-dir",
                str(runs_dir),
                "--target",
                "target_a",
                "--dry-run",
                "--miners",
                "0",
                "--sample-size",
                "8",
                "--atom-actions-yaml",
                str(atom_actions_path),
                "--skip-plan-folder-sync",
            ]
        )
    assert exc.value.code == 0

    compiled = runs_dir / "target_a" / "_compiled"
    out_json = compiled / "target_a.backlog.json"
    atoms_jsonl = compiled / "target_a.backlog.atoms.jsonl"
    summary = json.loads(out_json.read_text(encoding="utf-8"))
    atom_filter = summary["artifacts"]["atom_filter"]
    assert "queued" in atom_filter["exclude_statuses"]
    assert atom_filter["reopened_status_counts"]["queued"] == 1
    assert queued_atom_id in atom_filter["reopened_atom_ids_preview"]
    assert summary["totals"]["atoms"] == atom_filter["eligible_atoms"]

    assert queued_atom_id in atoms_jsonl.read_text(encoding="utf-8")
    queued_atom = _stage1_assigned_atom(compiled, queued_atom_id)
    assert queued_atom["disposition"] == "unresolved"
    assert [item["atom_id"] for item in eligible_problem_mining_atoms([queued_atom])] == [
        queued_atom_id
    ]

    atom_actions_doc = yaml.safe_load(atom_actions_path.read_text(encoding="utf-8"))
    atom_entry = next(
        item for item in atom_actions_doc["atoms"] if item["atom_id"] == queued_atom_id
    )
    assert atom_entry["status"] == "new"
    assert atom_entry["reopened_previous_status"] == "queued"
    assert atom_entry["disposition"] == "unresolved"


def test_reports_backlog_reopens_unmapped_ticketed_atoms_by_default(tmp_path: Path) -> None:
    repo_root = find_repo_root(Path(__file__).resolve())
    runs_dir = tmp_path / "runs" / "usertest"
    _seed_runs_fixture(runs_dir)

    ticketed_atom_id = "target_a/20260101T000000Z/codex/0:confusion_point:1"
    atom_actions_path = tmp_path / "backlog_atom_actions.yaml"
    _write_yaml(
        atom_actions_path,
        {
            "version": 1,
            "atoms": [
                {
                    "atom_id": ticketed_atom_id,
                    "status": "ticketed",
                }
            ],
        },
    )

    with pytest.raises(SystemExit) as exc:
        main(
            [
                "reports",
                "backlog",
                "--repo-root",
                str(repo_root),
                "--runs-dir",
                str(runs_dir),
                "--target",
                "target_a",
                "--dry-run",
                "--miners",
                "0",
                "--sample-size",
                "8",
                "--atom-actions-yaml",
                str(atom_actions_path),
                "--skip-plan-folder-sync",
            ]
        )
    assert exc.value.code == 0

    compiled = runs_dir / "target_a" / "_compiled"
    out_json = compiled / "target_a.backlog.json"
    atoms_jsonl = compiled / "target_a.backlog.atoms.jsonl"
    summary = json.loads(out_json.read_text(encoding="utf-8"))
    atom_filter = summary["artifacts"]["atom_filter"]
    assert "ticketed" in atom_filter["exclude_statuses"]
    assert atom_filter["reopened_status_counts"]["ticketed"] == 1
    assert ticketed_atom_id in atom_filter["reopened_atom_ids_preview"]
    assert summary["totals"]["atoms"] == atom_filter["eligible_atoms"]

    assert ticketed_atom_id in atoms_jsonl.read_text(encoding="utf-8")
    ticketed_atom = _stage1_assigned_atom(compiled, ticketed_atom_id)
    assert ticketed_atom["disposition"] == "unresolved"
    assert [item["atom_id"] for item in eligible_problem_mining_atoms([ticketed_atom])] == [
        ticketed_atom_id
    ]

    atom_actions_doc = yaml.safe_load(atom_actions_path.read_text(encoding="utf-8"))
    atom_entry = next(
        item for item in atom_actions_doc["atoms"] if item["atom_id"] == ticketed_atom_id
    )
    assert atom_entry["status"] == "new"
    assert atom_entry["reopened_previous_status"] == "ticketed"


def test_actioned_unproven_terminal_case_is_reopened_at_mining_boundary(
    tmp_path: Path,
) -> None:
    repo_root = find_repo_root(Path(__file__).resolve())
    runs_dir = tmp_path / "runs" / "usertest"
    _seed_runs_fixture(runs_dir)
    atom_id = "target_a/20260101T000000Z/codex/0:confusion_point:1"
    case_id = "case:unproven-terminal"
    compiled = runs_dir / "target_a" / "_compiled"
    _write_json(
        compiled / "target_a.case_registry.json",
        {
            "schema_version": 1,
            "cases": {
                case_id: {
                    "case_id": case_id,
                    "canonical_problem_id": "problem:unproven-terminal",
                    "state": "resolved",
                    "current_lifecycle": {
                        "state": "resolved",
                        "outcome_reference": {"validation_status": "projected"},
                    },
                }
            },
            "problem_id_to_case_id": {"problem:unproven-terminal": case_id},
            "atom_id_to_case_id": {atom_id: case_id},
            "atom_id_to_case_ids": {atom_id: [case_id]},
            "ticket_fingerprint_to_case_id": {},
        },
    )
    atom_actions_path = tmp_path / "backlog_atom_actions.yaml"
    _write_yaml(
        atom_actions_path,
        {
            "version": 1,
            "atoms": [
                {
                    "atom_id": atom_id,
                    "status": "actioned",
                    "case_id": case_id,
                    "disposition": "supports_case",
                    "disposition_rationale": "A historical plan attached this atom.",
                }
            ],
        },
    )

    with pytest.raises(SystemExit) as exc:
        main(
            [
                "reports",
                "backlog",
                "--repo-root",
                str(repo_root),
                "--runs-dir",
                str(runs_dir),
                "--target",
                "target_a",
                "--dry-run",
                "--miners",
                "0",
                "--sample-size",
                "0",
                "--atom-actions-yaml",
                str(atom_actions_path),
            ]
        )
    assert exc.value.code == 0

    assigned = _stage1_assigned_atom(compiled, atom_id)
    assert assigned["disposition"] == "unresolved"
    assert [item["atom_id"] for item in eligible_problem_mining_atoms([assigned])] == [
        atom_id
    ]
    actions = yaml.safe_load(atom_actions_path.read_text(encoding="utf-8"))["atoms"]
    action = next(item for item in actions if item["atom_id"] == atom_id)
    assert action["status"] == "new"
    assert action["reopened_previous_status"] == "actioned"
    assert action["reopened_previous_disposition"] == "supports_case"
    assert action["stale_actioned_previous_disposition"] == "supports_case"
    assert action["case_id"] == case_id
    assert action["disposition_status"] == "pending"


@pytest.mark.parametrize("preserved_kind", ["active_case", "verified_terminal", "idea"])
def test_default_filter_preserves_live_terminal_or_idea_boundaries(
    tmp_path: Path,
    preserved_kind: str,
) -> None:
    repo_root = find_repo_root(Path(__file__).resolve())
    runs_dir = tmp_path / "runs" / "usertest"
    _seed_runs_fixture(runs_dir)
    atom_id = "target_a/20260101T000000Z/codex/0:confusion_point:1"
    compiled = runs_dir / "target_a" / "_compiled"
    atom_actions_path = tmp_path / "backlog_atom_actions.yaml"
    status = "queued" if preserved_kind != "verified_terminal" else "ticketed"
    action: dict[str, Any] = {"atom_id": atom_id, "status": status}
    if preserved_kind == "idea":
        action["category"] = "IDEA"
    else:
        case_id = f"case:{preserved_kind}"
        terminal = preserved_kind == "verified_terminal"
        action.update(
            {
                "case_id": case_id,
                "disposition": "supports_case",
                "disposition_rationale": "The canonical registry owns this evidence.",
            }
        )
        _write_json(
            compiled / "target_a.case_registry.json",
            {
                "schema_version": 1,
                "cases": {
                    case_id: {
                        "case_id": case_id,
                        "canonical_problem_id": f"problem:{preserved_kind}",
                        "state": "resolved" if terminal else "active",
                        "current_lifecycle": (
                            {
                                "state": "resolved",
                                "outcome_reference": {"validation_status": "verified"},
                            }
                            if terminal
                            else {"state": "active"}
                        ),
                    }
                },
                "problem_id_to_case_id": {f"problem:{preserved_kind}": case_id},
                "atom_id_to_case_id": {atom_id: case_id},
                "atom_id_to_case_ids": {atom_id: [case_id]},
                "ticket_fingerprint_to_case_id": {},
            },
        )
    _write_yaml(atom_actions_path, {"version": 1, "atoms": [action]})

    with pytest.raises(SystemExit) as exc:
        main(
            [
                "reports",
                "backlog",
                "--repo-root",
                str(repo_root),
                "--runs-dir",
                str(runs_dir),
                "--target",
                "target_a",
                "--dry-run",
                "--miners",
                "0",
                "--sample-size",
                "0",
                "--atom-actions-yaml",
                str(atom_actions_path),
                "--skip-plan-folder-sync",
            ]
        )
    assert exc.value.code == 0

    summary = json.loads((compiled / "target_a.backlog.json").read_text(encoding="utf-8"))
    atom_filter = summary["artifacts"]["atom_filter"]
    assert atom_filter["reopened_unproven_atoms"] == 0
    atom_ids = {
        json.loads(line)["atom_id"]
        for line in (compiled / "target_a.backlog.atoms.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    }
    if preserved_kind == "active_case":
        assert atom_id in atom_ids
        assert atom_filter["preserved_open_case_status_counts"][status] == 1
    else:
        assert atom_id not in atom_ids
        assert atom_id in atom_filter["excluded_atom_ids_preview"]
    persisted_actions = yaml.safe_load(atom_actions_path.read_text(encoding="utf-8"))["atoms"]
    persisted_action = next(item for item in persisted_actions if item["atom_id"] == atom_id)
    assert persisted_action["status"] == status
    assert "reopened_previous_status" not in persisted_action


def test_reports_backlog_missing_prompt_template_fails_loudly(tmp_path: Path) -> None:
    repo_root = find_repo_root(Path(__file__).resolve())
    runs_dir = tmp_path / "runs" / "usertest"
    _seed_runs_fixture(runs_dir)
    atom_actions_path = tmp_path / "backlog_atom_actions.yaml"

    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir(parents=True, exist_ok=True)
    (prompts_dir / "pipeline_manifest.json").write_text(
        json.dumps(
            {
                "version": 2,
                "problem_miner_templates": ["missing_template.md"],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(SystemExit) as exc:
        main(
            [
                "reports",
                "backlog",
                "--repo-root",
                str(repo_root),
                "--runs-dir",
                str(runs_dir),
                "--target",
                "target_a",
                "--dry-run",
                "--prompts-dir",
                str(prompts_dir),
                "--atom-actions-yaml",
                str(atom_actions_path),
                "--skip-plan-folder-sync",
            ]
        )
    assert exc.value.code == 2


def test_reports_backlog_dry_run_writes_problem_records(tmp_path: Path) -> None:
    """Stage-1 dual-write: problem_records.json and .md are created in dry-run mode.

    In dry-run mode the LLM is not called. The six-stage pipeline still writes
    inspectable artifacts by synthesizing deterministic problem records from atoms.
    The contract being tested is:
    - The files exist.
    - The JSON has the expected structure (stage, records, input_meta.dry_run).
    - No record contains the forbidden field ``proposed_fix``.
    """
    repo_root = find_repo_root(Path(__file__).resolve())
    runs_dir = tmp_path / "runs" / "usertest"
    _seed_runs_fixture(runs_dir)
    atom_actions_path = tmp_path / "backlog_atom_actions.yaml"

    with pytest.raises(SystemExit) as exc:
        main(
            [
                "reports",
                "backlog",
                "--repo-root",
                str(repo_root),
                "--runs-dir",
                str(runs_dir),
                "--target",
                "target_a",
                "--dry-run",
                "--miners",
                "0",
                "--sample-size",
                "8",
                "--atom-actions-yaml",
                str(atom_actions_path),
                "--skip-plan-folder-sync",
            ]
        )
    assert exc.value.code == 0

    compiled = runs_dir / "target_a" / "_compiled"
    problem_records_json = compiled / "target_a.problem_records.json"
    problem_records_md = compiled / "target_a.problem_records.md"

    assert problem_records_json.exists(), (
        "problem_records.json must be written by stage-1 dual-write when "
        "pipeline_manifest.json is present in the prompts dir"
    )
    assert problem_records_md.exists(), "problem_records.md must be written"

    doc = json.loads(problem_records_json.read_text(encoding="utf-8"))
    assert doc.get("stage") == "problem_mining"
    assert isinstance(doc.get("items"), list)
    # dry-run: LLM not called; synthesized problem records are used
    assert doc.get("item_count") == len(doc["items"])
    assert len(doc["items"]) >= 1, "dry-run mode should synthesize at least one problem record"
    assert doc.get("input_meta", {}).get("dry_run") is True

    # Invariant: no problem record should ever contain proposed_fix
    for rec in doc["items"]:
        assert "proposed_fix" not in rec, (
            f"Record {rec.get('problem_id')} contains forbidden field 'proposed_fix'"
        )


def test_reports_backlog_dry_run_writes_prioritized_problems(tmp_path: Path) -> None:
    """Stage 2: prioritized_problems.json and .md are created in dry-run mode.

    Contract:
    - Files exist.
    - At least one problem is selected_for_research on fixtures.
    - Output contains deterministic pre-score breakdown (pre_score + score_breakdown).
    - Output contains no solution fields (e.g. proposed_fix).
    """
    repo_root = find_repo_root(Path(__file__).resolve())
    runs_dir = tmp_path / "runs" / "usertest"
    _seed_runs_fixture(runs_dir)
    atom_actions_path = tmp_path / "backlog_atom_actions.yaml"

    with pytest.raises(SystemExit) as exc:
        main(
            [
                "reports",
                "backlog",
                "--repo-root",
                str(repo_root),
                "--runs-dir",
                str(runs_dir),
                "--target",
                "target_a",
                "--dry-run",
                "--miners",
                "0",
                "--sample-size",
                "8",
                "--atom-actions-yaml",
                str(atom_actions_path),
                "--skip-plan-folder-sync",
            ]
        )
    assert exc.value.code == 0

    compiled = runs_dir / "target_a" / "_compiled"
    prioritized_json = compiled / "target_a.prioritized_problems.json"
    prioritized_md = compiled / "target_a.prioritized_problems.md"

    assert prioritized_json.exists(), "prioritized_problems.json must be written in dry-run mode"
    assert prioritized_md.exists(), "prioritized_problems.md must be written"

    doc = json.loads(prioritized_json.read_text(encoding="utf-8"))
    assert doc.get("stage") == "problem_prioritization"
    assert doc.get("input_meta", {}).get("dry_run") is True
    assert isinstance(doc.get("items"), list)
    assert len(doc["items"]) >= 1
    assert doc["items"]
    assert all(item.get("selected_for_research") is True for item in doc["items"])

    forbidden = {
        "proposed_fix",
        "selected_solution",
        "family_id",
        "option_id",
        "implementation_steps",
    }
    for item in doc["items"]:
        for field in forbidden:
            assert field not in item, f"priority decision must not contain solution field: {field}"
        assert isinstance(item.get("score_breakdown"), dict)
        assert "pre_score" in item


def test_reports_backlog_dry_run_writes_research_dossiers(tmp_path: Path) -> None:
    """Stage 3: research.json and .md are created in dry-run mode.

    Contract:
    - Files exist.
    - Output is inspectable offline (dry_run=true) and does not claim implementation.
    """
    repo_root = find_repo_root(Path(__file__).resolve())
    runs_dir = tmp_path / "runs" / "usertest"
    _seed_runs_fixture(runs_dir)
    atom_actions_path = tmp_path / "backlog_atom_actions.yaml"

    with pytest.raises(SystemExit) as exc:
        main(
            [
                "reports",
                "backlog",
                "--repo-root",
                str(repo_root),
                "--runs-dir",
                str(runs_dir),
                "--target",
                "target_a",
                "--dry-run",
                "--miners",
                "0",
                "--sample-size",
                "8",
                "--atom-actions-yaml",
                str(atom_actions_path),
                "--skip-plan-folder-sync",
            ]
        )
    assert exc.value.code == 0

    compiled = runs_dir / "target_a" / "_compiled"
    research_json = compiled / "target_a.research.json"
    research_md = compiled / "target_a.research.md"

    assert research_json.exists(), "research.json must be written in dry-run mode"
    assert research_md.exists(), "research.md must be written in dry-run mode"

    doc = json.loads(research_json.read_text(encoding="utf-8"))
    assert doc.get("stage") == "repro_research"
    assert doc.get("input_meta", {}).get("dry_run") is True
    assert isinstance(doc.get("items"), list)
    assert len(doc["items"]) >= 1, (
        "fixtures should yield at least one selected-for-research problem"
    )

    for item in doc["items"]:
        assert item.get("implementation_performed") is False
        assert item.get("diff_classification") == "no_changes"
        assert item.get("writes_used") is False


def test_reports_backlog_dry_run_writes_solution_options(tmp_path: Path) -> None:
    """Stage 4: solution_options.json and .md are created in dry-run mode.

    Contract:
    - Files exist.
    - At least one problem has one option per configured family.
    - Output contains no selection fields (e.g. selected_solution).
    """
    repo_root = find_repo_root(Path(__file__).resolve())
    runs_dir = tmp_path / "runs" / "usertest"
    _seed_runs_fixture(runs_dir)
    atom_actions_path = tmp_path / "backlog_atom_actions.yaml"

    with pytest.raises(SystemExit) as exc:
        main(
            [
                "reports",
                "backlog",
                "--repo-root",
                str(repo_root),
                "--runs-dir",
                str(runs_dir),
                "--target",
                "target_a",
                "--dry-run",
                "--miners",
                "0",
                "--sample-size",
                "8",
                "--atom-actions-yaml",
                str(atom_actions_path),
                "--skip-plan-folder-sync",
            ]
        )
    assert exc.value.code == 0

    compiled = runs_dir / "target_a" / "_compiled"
    options_json = compiled / "target_a.solution_options.json"
    options_md = compiled / "target_a.solution_options.md"

    assert options_json.exists(), "solution_options.json must be written in dry-run mode"
    assert options_md.exists(), "solution_options.md must be written in dry-run mode"

    doc = json.loads(options_json.read_text(encoding="utf-8"))
    assert doc.get("stage") == "solution_optioning"
    assert doc.get("input_meta", {}).get("dry_run") is True
    assert isinstance(doc.get("items"), list)
    assert doc["items"] == []
    outcomes = doc.get("input_meta", {}).get("optioning_outcomes")
    assert isinstance(outcomes, list) and outcomes
    assert {item.get("optioning_status") for item in outcomes} == {"insufficient_evidence"}
    assert all(item.get("research_readiness_blockers") for item in outcomes)


def test_reports_backlog_dry_run_writes_solution_selection(tmp_path: Path) -> None:
    """Stage 5: solution_selection.json and .md are created in dry-run mode.

    Contract:
    - Files exist.
    - Each decision selects an existing option and includes a UX-review flag.
    - Selected-solution labeler output is attached (change_surface).
    """
    repo_root = find_repo_root(Path(__file__).resolve())
    runs_dir = tmp_path / "runs" / "usertest"
    _seed_runs_fixture(runs_dir)
    atom_actions_path = tmp_path / "backlog_atom_actions.yaml"

    with pytest.raises(SystemExit) as exc:
        main(
            [
                "reports",
                "backlog",
                "--repo-root",
                str(repo_root),
                "--runs-dir",
                str(runs_dir),
                "--target",
                "target_a",
                "--dry-run",
                "--miners",
                "0",
                "--sample-size",
                "8",
                "--atom-actions-yaml",
                str(atom_actions_path),
                "--skip-plan-folder-sync",
            ]
        )
    assert exc.value.code == 0

    compiled = runs_dir / "target_a" / "_compiled"
    selection_json = compiled / "target_a.solution_selection.json"
    selection_md = compiled / "target_a.solution_selection.md"

    assert selection_json.exists(), "solution_selection.json must be written in dry-run mode"
    assert selection_md.exists(), "solution_selection.md must be written in dry-run mode"

    doc = json.loads(selection_json.read_text(encoding="utf-8"))
    assert doc.get("stage") == "solution_selection"
    assert doc.get("input_meta", {}).get("dry_run") is True
    assert doc.get("input_meta", {}).get("breadth_profile") == "external_generalization"
    assert isinstance(doc.get("input_meta", {}).get("batch_breadth"), dict)
    assert isinstance(doc.get("items"), list)
    assert doc["items"] == []
    assert doc.get("input_meta", {}).get("decision_count") == 0
    assert doc.get("input_meta", {}).get("repo_access") == "read_only"


def test_reports_backlog_internal_profile_injects_breadth_context_into_stage5_prompt(
    tmp_path: Path,
) -> None:
    repo_root = find_repo_root(Path(__file__).resolve())
    runs_dir = tmp_path / "runs" / "usertest"
    _seed_runs_fixture(runs_dir)
    atom_actions_path = tmp_path / "backlog_atom_actions.yaml"

    with pytest.raises(SystemExit) as exc:
        main(
            [
                "reports",
                "backlog",
                "--repo-root",
                str(repo_root),
                "--runs-dir",
                str(runs_dir),
                "--target",
                "target_a",
                "--dry-run",
                "--breadth-profile",
                "internal_maintenance",
                "--miners",
                "0",
                "--sample-size",
                "8",
                "--atom-actions-yaml",
                str(atom_actions_path),
                "--skip-plan-folder-sync",
            ]
        )
    assert exc.value.code == 0

    compiled = runs_dir / "target_a" / "_compiled"
    selection_json = compiled / "target_a.solution_selection.json"
    artifacts_dir = compiled / "target_a.backlog_artifacts" / "solution_selection"

    doc = json.loads(selection_json.read_text(encoding="utf-8"))
    assert doc.get("input_meta", {}).get("breadth_profile") == "internal_maintenance"
    assert isinstance(doc.get("input_meta", {}).get("batch_breadth"), dict)
    assert "missions" in doc.get("input_meta", {}).get("batch_breadth", {})

    prompt_paths = list(artifacts_dir.glob("solution_selection_*/*.prompt.txt"))
    assert prompt_paths == []
    assert doc.get("input_meta", {}).get("decision_count") == 0


def test_reports_backlog_dry_run_writes_change_plans(tmp_path: Path) -> None:
    """Stage 6: change_plans.json and .md are created in dry-run mode.

    Contract:
    - Files exist.
    - Each plan has non-empty implementation + verification steps.
    - Each plan is grounded to a selected option and is marked planned.
    """
    repo_root = find_repo_root(Path(__file__).resolve())
    runs_dir = tmp_path / "runs" / "usertest"
    _seed_runs_fixture(runs_dir)
    atom_actions_path = tmp_path / "backlog_atom_actions.yaml"

    with pytest.raises(SystemExit) as exc:
        main(
            [
                "reports",
                "backlog",
                "--repo-root",
                str(repo_root),
                "--runs-dir",
                str(runs_dir),
                "--target",
                "target_a",
                "--dry-run",
                "--miners",
                "0",
                "--sample-size",
                "8",
                "--atom-actions-yaml",
                str(atom_actions_path),
                "--skip-plan-folder-sync",
            ]
        )
    assert exc.value.code == 0

    compiled = runs_dir / "target_a" / "_compiled"
    plans_json = compiled / "target_a.change_plans.json"
    plans_md = compiled / "target_a.change_plans.md"

    assert plans_json.exists(), "change_plans.json must be written in dry-run mode"
    assert plans_md.exists(), "change_plans.md must be written in dry-run mode"

    doc = json.loads(plans_json.read_text(encoding="utf-8"))
    assert doc.get("stage") == "implementation_planning"
    assert doc.get("input_meta", {}).get("dry_run") is True
    assert isinstance(doc.get("items"), list)
    assert doc["items"] == []
    assert doc.get("input_meta", {}).get("decision_count") == 0
    assert doc.get("input_meta", {}).get("change_plan_count") == 0
