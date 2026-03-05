from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
import yaml
from backlog_repo.export import ticket_export_fingerprint
from runner_core import find_repo_root

from usertest_backlog.cli import main


def _write_json(path: Path, obj: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _write_yaml(path: Path, obj: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(obj, sort_keys=False), encoding="utf-8")


def _ticket_labeler_fingerprint(ticket: dict[str, Any]) -> str:
    title_raw = ticket.get("title")
    title = str(title_raw).strip().lower() if isinstance(title_raw, str) else ""
    evidence = sorted(item for item in ticket.get("evidence_atom_ids", []) if isinstance(item, str))
    anchor = json.dumps({"title": title, "evidence": evidence}, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(anchor).hexdigest()[:16]


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
    assert all(
        json.loads(line).get("source") != "agent_last_message_artifact"
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
    assert queued_atom_id not in atom_ids

    atom_actions_doc = yaml.safe_load(atom_actions_path.read_text(encoding="utf-8"))
    entry = next(item for item in atom_actions_doc["atoms"] if item["atom_id"] == queued_atom_id)
    assert entry["status"] == "queued"

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
    assert planned, "dry-run backlog should include at least one planned ticket"

    for ticket in planned:
        assert isinstance(ticket.get("problem_record"), dict)
        assert isinstance(ticket.get("priority"), dict)
        assert isinstance(ticket.get("research"), dict)
        assert isinstance(ticket.get("solution_options"), list)
        assert ticket["solution_options"], "planned tickets should carry solution options"
        assert isinstance(ticket.get("selected_solution"), dict)
        assert isinstance(ticket.get("change_plan"), dict)

    for ticket in tickets:
        if ticket.get("stage") == "ready_for_ticket":
            assert isinstance(ticket.get("selected_solution"), dict) or isinstance(
                ticket.get("selected_option_id"), str
            )
            assert isinstance(ticket.get("change_plan"), dict) or isinstance(
                ticket.get("change_plan_id"), str
            )

    # Atom actions: at least one cited atom should be marked ticketed with a recorded fingerprint.
    atom_actions_doc = yaml.safe_load(atom_actions_path.read_text(encoding="utf-8"))
    assert atom_actions_doc["version"] == 1
    atoms = atom_actions_doc["atoms"]

    evidence_atom_id: str | None = None
    chosen_ticket: dict[str, Any] | None = None
    for ticket in planned:
        for atom_id in ticket.get("evidence_atom_ids", []):
            if isinstance(atom_id, str) and atom_id and not atom_id.startswith("__aggregate__/"):
                evidence_atom_id = atom_id
                chosen_ticket = ticket
                break
        if evidence_atom_id is not None:
            break
    assert evidence_atom_id is not None
    assert chosen_ticket is not None

    entry = next(item for item in atoms if item["atom_id"] == evidence_atom_id)
    assert entry["status"] == "ticketed"
    assert ticket_export_fingerprint(chosen_ticket) in entry["fingerprints"]
    assert "ticket_ids" not in entry


def test_update_atom_actions_from_backlog_skips_blocked_tickets(tmp_path: Path) -> None:
    from usertest_backlog.cli import _update_atom_actions_from_backlog

    atom_actions: dict[str, dict[str, Any]] = {}
    atoms = [
        {"atom_id": "atom:1", "source": "confusion_point"},
        {"atom_id": "atom:2", "source": "confusion_point"},
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
            "title": "Normal ticket",
            "problem": "P",
            "user_impact": "U",
            "proposed_fix": "F",
            "stage": "triage",
            "evidence_atom_ids": ["atom:2"],
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
    assert atom_actions["atom:2"]["status"] == "ticketed"


def test_reports_backlog_syncs_atom_actions_from_plan_folders(tmp_path: Path) -> None:
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
    assert atom_filter["excluded_status_counts"].get("actioned", 0) >= 1
    assert atom_id in atom_filter["excluded_atom_ids_preview"]

    atom_actions_doc = yaml.safe_load(atom_actions_path.read_text(encoding="utf-8"))
    atom_entry = next(item for item in atom_actions_doc["atoms"] if item["atom_id"] == atom_id)
    assert atom_entry["status"] == "actioned"


def test_reports_backlog_excludes_queued_atoms_by_default(tmp_path: Path) -> None:
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
    assert atom_filter["excluded_atoms"] >= 1
    assert queued_atom_id in atom_filter["excluded_atom_ids_preview"]
    assert summary["totals"]["atoms"] == atom_filter["eligible_atoms"]

    atom_lines = atoms_jsonl.read_text(encoding="utf-8").splitlines()
    assert all(queued_atom_id not in line for line in atom_lines)

    atom_actions_doc = yaml.safe_load(atom_actions_path.read_text(encoding="utf-8"))
    atom_entry = next(
        item
        for item in atom_actions_doc["atoms"]
        if item["atom_id"] == queued_atom_id
    )
    assert atom_entry["status"] == "queued"


def test_reports_backlog_excludes_ticketed_atoms_by_default(tmp_path: Path) -> None:
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
    assert atom_filter["excluded_atoms"] >= 1
    assert ticketed_atom_id in atom_filter["excluded_atom_ids_preview"]
    assert summary["totals"]["atoms"] == atom_filter["eligible_atoms"]

    atom_lines = atoms_jsonl.read_text(encoding="utf-8").splitlines()
    assert all(ticketed_atom_id not in line for line in atom_lines)

    atom_actions_doc = yaml.safe_load(atom_actions_path.read_text(encoding="utf-8"))
    atom_entry = next(
        item
        for item in atom_actions_doc["atoms"]
        if item["atom_id"] == ticketed_atom_id
    )
    assert atom_entry["status"] == "ticketed"


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
    assert any(item.get("selected_for_research") is True for item in doc["items"])

    forbidden = {"proposed_fix", "selected_solution", "family_id", "option_id", "implementation_steps"}
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
    assert len(doc["items"]) >= 1, "fixtures should yield at least one selected-for-research problem"

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
    assert len(doc["items"]) >= 3, "dry-run should synthesize at least one full option set"

    forbidden = {"selected_solution"}
    families = set()
    for item in doc["items"]:
        for field in forbidden:
            assert field not in item, f"stage-4 option must not contain forbidden field: {field}"
        fid = item.get("family_id")
        if isinstance(fid, str):
            families.add(fid)
    assert {"most_direct", "most_robust", "most_comprehensive"} <= families


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
    assert isinstance(doc.get("items"), list)
    assert len(doc["items"]) >= 1

    needs_ux_values: set[bool] = set()
    for item in doc["items"]:
        assert isinstance(item.get("problem_id"), str)
        assert isinstance(item.get("selected_option_id"), str)
        assert isinstance(item.get("selected_family_id"), str)
        assert isinstance(item.get("needs_ux_review"), bool)
        needs_ux_values.add(bool(item.get("needs_ux_review")))
        cs = item.get("change_surface")
        assert isinstance(cs, dict), "selected-solution labeler must attach change_surface"
        assert isinstance(cs.get("kinds"), list)

    # Fixtures should produce at least one high-surface and one low-surface selection
    # when multiple problems are selected for research; at minimum ensure the flag exists.
    if len(doc["items"]) > 1:
        assert needs_ux_values == {False, True}


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
    assert len(doc["items"]) >= 1

    for plan in doc["items"]:
        assert isinstance(plan.get("change_plan_id"), str)
        assert isinstance(plan.get("problem_id"), str)
        assert isinstance(plan.get("selected_option_id"), str)
        assert plan.get("change_plan_status") == "planned"

        implementation_steps = plan.get("implementation_steps")
        assert isinstance(implementation_steps, list)
        assert implementation_steps, "implementation_steps must be non-empty"

        verification_steps = plan.get("verification_steps")
        assert isinstance(verification_steps, list)
        assert verification_steps, "verification_steps must be non-empty"

        success_criteria = plan.get("success_criteria")
        assert isinstance(success_criteria, list)
        assert success_criteria, "success_criteria must be non-empty"

        assert isinstance(plan.get("rollback_notes"), str)
        assert isinstance(plan.get("related_change_plan_ids"), list)
