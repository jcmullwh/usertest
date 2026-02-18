from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
import yaml
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

    assert out_json.exists()
    assert out_md.exists()
    assert atoms_jsonl.exists()

    summary = json.loads(out_json.read_text(encoding="utf-8"))
    assert summary["totals"]["runs"] == 2
    assert summary["totals"]["miners_total"] == 2

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


def test_reports_backlog_uses_cached_miner_outputs(tmp_path: Path) -> None:
    repo_root = find_repo_root(Path(__file__).resolve())
    runs_dir = tmp_path / "runs" / "usertest"
    _seed_runs_fixture(runs_dir)

    compiled = runs_dir / "target_a" / "_compiled"
    artifacts_dir = compiled / "target_a.backlog_artifacts"
    miner_dir = artifacts_dir / "miner_001"
    miner_dir.mkdir(parents=True, exist_ok=True)
    atom_actions_path = tmp_path / "backlog_atom_actions.yaml"

    cached_ticket = [
        {
            "title": "Add quickstart docs",
            "problem": "No quickstart section",
            "user_impact": "users blocked",
            "severity": "high",
            "confidence": 0.8,
            "evidence_atom_ids": ["target_a/20260101T000000Z/codex/0:confusion_point:1"],
            "investigation_steps": ["inspect README"],
            "success_criteria": ["first output in <5 minutes"],
            "proposed_fix": "add quickstart snippet",
            "suggested_owner": "docs",
        }
    ]

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
                "1",
                "--coverage-miners",
                "1",
                "--bagging-miners",
                "0",
                "--sample-size",
                "8",
                "--no-merge",
                "--orphan-pass",
                "0",
                "--resume",
                "--atom-actions-yaml",
                str(atom_actions_path),
                "--skip-plan-folder-sync",
            ]
        )
    assert exc.value.code == 0

    _write_json(miner_dir / "tickets.json", cached_ticket)
    _seed_labeler_cache(artifacts_dir, cached_ticket[0])

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
                "--miners",
                "1",
                "--coverage-miners",
                "1",
                "--bagging-miners",
                "0",
                "--sample-size",
                "8",
                "--no-merge",
                "--orphan-pass",
                "0",
                "--resume",
                "--atom-actions-yaml",
                str(atom_actions_path),
                "--skip-plan-folder-sync",
            ]
        )
    assert exc.value.code == 0

    out_json = compiled / "target_a.backlog.json"
    summary = json.loads(out_json.read_text(encoding="utf-8"))
    assert summary["totals"]["tickets"] == 1
    assert summary["tickets"][0]["title"] == "Add quickstart docs"
    assert summary["tickets"][0]["change_surface"]["kinds"] == ["docs_change"]
    assert summary["tickets"][0]["stage"] == "ready_for_ticket"
    assert summary["artifacts"]["atom_actions"]["path"] == str(atom_actions_path)

    atom_actions_doc = yaml.safe_load(atom_actions_path.read_text(encoding="utf-8"))
    assert atom_actions_doc["version"] == 1
    atoms = atom_actions_doc["atoms"]
    entry = next(
        item
        for item in atoms
        if item["atom_id"] == "target_a/20260101T000000Z/codex/0:confusion_point:1"
    )
    assert entry["status"] == "ticketed"
    assert any(isinstance(tid, str) and tid.startswith("TKT-") for tid in entry["ticket_ids"])


def test_reports_backlog_does_not_ticket_atoms_for_blocked_tickets(tmp_path: Path) -> None:
    repo_root = find_repo_root(Path(__file__).resolve())
    runs_dir = tmp_path / "runs" / "usertest"
    _seed_runs_fixture(runs_dir)
    atom_actions_path = tmp_path / "backlog_atom_actions.yaml"

    compiled = runs_dir / "target_a" / "_compiled"
    artifacts_dir = compiled / "target_a.backlog_artifacts"
    miner_dir = artifacts_dir / "miner_001"
    miner_dir.mkdir(parents=True, exist_ok=True)

    # First run seeds the miner input manifest (dry-run produces empty tickets.json).
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
                "1",
                "--coverage-miners",
                "1",
                "--bagging-miners",
                "0",
                "--sample-size",
                "0",
                "--no-merge",
                "--orphan-pass",
                "0",
                "--resume",
                "--atom-actions-yaml",
                str(atom_actions_path),
                "--skip-plan-folder-sync",
            ]
        )
    assert exc.value.code == 0

    # Cached ticket is medium severity but only cites evidence from a single run,
    # so the reporter will mark it stage=blocked.
    cached_ticket = [
        {
            "title": "Single-run medium ticket should be blocked",
            "problem": "Not enough evidence breadth",
            "user_impact": "Noise if exported",
            "severity": "medium",
            "confidence": 0.6,
            "evidence_atom_ids": ["target_a/20260101T000000Z/codex/0:confusion_point:1"],
            "investigation_steps": ["Wait for more runs / gather evidence"],
            "success_criteria": ["Ticket only exported once evidence spans multiple runs"],
            "proposed_fix": "n/a",
            "suggested_owner": "docs",
        }
    ]
    _write_json(miner_dir / "tickets.json", cached_ticket)
    _seed_labeler_cache(artifacts_dir, cached_ticket[0])

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
                "--miners",
                "1",
                "--coverage-miners",
                "1",
                "--bagging-miners",
                "0",
                "--sample-size",
                "0",
                "--no-merge",
                "--orphan-pass",
                "0",
                "--resume",
                "--atom-actions-yaml",
                str(atom_actions_path),
                "--skip-plan-folder-sync",
            ]
        )
    assert exc.value.code == 0

    out_json = compiled / "target_a.backlog.json"
    summary = json.loads(out_json.read_text(encoding="utf-8"))
    assert summary["totals"]["tickets"] == 1
    assert summary["tickets"][0]["title"] == cached_ticket[0]["title"]
    assert summary["tickets"][0]["stage"] == "blocked"

    atom_actions_doc = yaml.safe_load(atom_actions_path.read_text(encoding="utf-8"))
    atoms = atom_actions_doc["atoms"]
    entry = next(
        item
        for item in atoms
        if item["atom_id"] == "target_a/20260101T000000Z/codex/0:confusion_point:1"
    )
    assert entry["status"] == "new"
    assert entry.get("ticket_ids", []) == []


def test_reports_backlog_ignores_stale_cached_miner_outputs(tmp_path: Path) -> None:
    repo_root = find_repo_root(Path(__file__).resolve())
    runs_dir = tmp_path / "runs" / "usertest"
    _seed_runs_fixture(runs_dir)
    atom_actions_path = tmp_path / "backlog_atom_actions.yaml"

    compiled = runs_dir / "target_a" / "_compiled"
    artifacts_dir = compiled / "target_a.backlog_artifacts"
    miner_dir = artifacts_dir / "miner_001"
    miner_dir.mkdir(parents=True, exist_ok=True)

    _write_json(
        miner_dir / "tickets.json",
        [
            {
                "title": "Stale cached ticket",
                "problem": "stale",
                "user_impact": "stale",
                "severity": "high",
                "confidence": 0.8,
                "evidence_atom_ids": ["target_a/20260101T000000Z/codex/0:confusion_point:1"],
                "investigation_steps": ["inspect"],
                "success_criteria": ["done"],
                "proposed_fix": "fix",
                "suggested_owner": "docs",
            }
        ],
    )
    _write_json(
        miner_dir / "input_manifest.json",
        {
            "job_tag": "miner_001",
            "pass_type": "coverage",
            "template": "miner_default.md",
            "agent": "claude",
            "model": None,
            "atom_count": 1,
            "atom_ids": ["target_a/stale:1"],
            "selection_params": {"selection_seed": 999},
            "prompt_manifest": {
                "manifest_file": "manifest.json",
                "coverage_templates": ["miner_default.md"],
                "bagging_templates": ["miner_default.md"],
                "orphan_template": "miner_default.md",
                "merge_judge_template": "merge_judge.md",
                "labeler_template": "labeler.md",
            },
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
                "1",
                "--coverage-miners",
                "1",
                "--bagging-miners",
                "0",
                "--sample-size",
                "8",
                "--no-merge",
                "--orphan-pass",
                "0",
                "--resume",
                "--atom-actions-yaml",
                str(atom_actions_path),
                "--skip-plan-folder-sync",
            ]
        )
    assert exc.value.code == 0

    out_json = compiled / "target_a.backlog.json"
    summary = json.loads(out_json.read_text(encoding="utf-8"))
    assert summary["totals"]["tickets"] == 0

    miner_meta = json.loads((miner_dir / "meta.json").read_text(encoding="utf-8"))
    assert miner_meta["cached"] is False
    assert miner_meta["status"] == "dry_run"


def test_reports_backlog_ignores_cached_tickets_outside_atom_scope(tmp_path: Path) -> None:
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
                "1",
                "--coverage-miners",
                "1",
                "--bagging-miners",
                "0",
                "--sample-size",
                "8",
                "--no-merge",
                "--orphan-pass",
                "0",
                "--resume",
                "--atom-actions-yaml",
                str(atom_actions_path),
                "--skip-plan-folder-sync",
            ]
        )
    assert exc.value.code == 0

    compiled = runs_dir / "target_a" / "_compiled"
    miner_dir = compiled / "target_a.backlog_artifacts" / "miner_001"
    _write_json(
        miner_dir / "tickets.json",
        [
            {
                "title": "Out-of-scope evidence",
                "problem": "stale",
                "user_impact": "stale",
                "severity": "high",
                "confidence": 0.9,
                "evidence_atom_ids": ["target_a/not-in-current-scope:1"],
                "investigation_steps": ["inspect"],
                "success_criteria": ["done"],
                "proposed_fix": "fix",
                "suggested_owner": "docs",
            }
        ],
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
                "1",
                "--coverage-miners",
                "1",
                "--bagging-miners",
                "0",
                "--sample-size",
                "8",
                "--no-merge",
                "--orphan-pass",
                "0",
                "--resume",
                "--atom-actions-yaml",
                str(atom_actions_path),
                "--skip-plan-folder-sync",
            ]
        )
    assert exc.value.code == 0

    miner_meta = json.loads((miner_dir / "meta.json").read_text(encoding="utf-8"))
    assert miner_meta["cached"] is False
    assert miner_meta["status"] == "dry_run"
def test_reports_backlog_sample_size_zero_keeps_uncapped_semantics_and_full_orphan_pool(
    tmp_path: Path,
) -> None:
    repo_root = find_repo_root(Path(__file__).resolve())
    runs_dir = tmp_path / "runs" / "usertest"
    _seed_many_high_severity_runs(runs_dir, count=40)
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
                "0",
                "--orphan-pass",
                "1",
                "--atom-actions-yaml",
                str(atom_actions_path),
                "--skip-plan-folder-sync",
            ]
        )
    assert exc.value.code == 0

    compiled = runs_dir / "target_a" / "_compiled"
    out_json = compiled / "target_a.backlog.json"
    summary = json.loads(out_json.read_text(encoding="utf-8"))
    assert summary["input"]["sample_size"] == 0
    assert summary["input"]["sample_size_semantics"] == "all_atoms"

    orphan_manifest_path = (
        compiled
        / "target_a.backlog_artifacts"
        / "orphan_pass"
        / "orphan_001"
        / "input_manifest.json"
    )
    orphan_manifest = json.loads(orphan_manifest_path.read_text(encoding="utf-8"))
    atom_ids = orphan_manifest["atom_ids"]
    assert len(atom_ids) >= 40
    assert any("20260101T003900Z" in atom_id for atom_id in atom_ids)


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
    (prompts_dir / "manifest.json").write_text(
        json.dumps(
            {
                "version": 1,
                "miners": {
                    "coverage_templates": ["missing_template.md"],
                    "bagging_templates": ["missing_template.md"],
                    "orphan_template": "missing_template.md",
                },
                "merge_judge_template": "missing_template.md",
                "labeler_template": "missing_template.md",
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(FileNotFoundError, match="Missing prompt template"):
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
