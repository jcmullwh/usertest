from __future__ import annotations

import json
from pathlib import Path

import pytest

from usertest_backlog.cli import main


def test_triage_atoms_clusters_and_links_tickets(tmp_path: Path) -> None:
    atoms_dir = tmp_path / "runs" / "usertest_implement" / "usertest" / "_compiled"
    atoms_dir.mkdir(parents=True, exist_ok=True)
    atoms_jsonl = atoms_dir / "usertest.backlog.atoms.jsonl"

    atom_1 = {
        "atom_id": "usertest/20260201T000000Z/codex/0:command_failure:1",
        "run_id": "usertest/20260201T000000Z/codex/0",
        "run_rel": "usertest/20260201T000000Z/codex/0",
        "run_dir": "runs/usertest_implement/usertest/20260201T000000Z/codex/0",
        "timestamp_utc": "2026-02-01T00:00:00Z",
        "source": "command_failure",
        "severity_hint": "high",
        "text": "Command failed: exit_code=1; command=python -m pytest -q",
    }
    atom_2 = {
        "atom_id": "usertest/20260201T000001Z/codex/0:command_failure:1",
        "run_id": "usertest/20260201T000001Z/codex/0",
        "run_rel": "usertest/20260201T000001Z/codex/0",
        "run_dir": "runs/usertest_implement/usertest/20260201T000001Z/codex/0",
        "timestamp_utc": "2026-02-01T00:00:01Z",
        "source": "command_failure",
        "severity_hint": "high",
        "text": "Command failed: exit_code=2; command=python -m pytest -q",
    }
    atom_3 = {
        "atom_id": "usertest/20260201T000002Z/codex/0:suggested_change:1",
        "run_id": "usertest/20260201T000002Z/codex/0",
        "run_rel": "usertest/20260201T000002Z/codex/0",
        "run_dir": "runs/usertest_implement/usertest/20260201T000002Z/codex/0",
        "timestamp_utc": "2026-02-01T00:00:02Z",
        "source": "suggested_change",
        "severity_hint": "low",
        "text": "Update docs to mention PYTHONPATH configuration.",
    }
    atoms_jsonl.write_text(
        "\n".join([json.dumps(atom_1), json.dumps(atom_2), json.dumps(atom_3)]) + "\n",
        encoding="utf-8",
    )

    backlog_json = atoms_dir / "usertest.backlog.json"
    backlog_json.write_text(
        json.dumps(
            {
                "generated_at_utc": "2026-02-01T00:00:10Z",
                "input": {
                    "runs_dir": str(tmp_path / "runs" / "usertest_implement"),
                    "target": "usertest",
                },
                "tickets": [
                    {
                        "ticket_id": "BLG-001",
                        "title": "Fix pytest invocation in sandbox",
                        "stage": "ready_for_ticket",
                        "severity": "high",
                        "evidence_atom_ids": [atom_1["atom_id"], atom_2["atom_id"]],
                    },
                    {
                        "ticket_id": "BLG-002",
                        "title": "Docs: add PYTHONPATH hint",
                        "stage": "triage",
                        "severity": "medium",
                        "evidence_atom_ids": [atom_1["atom_id"]],
                    },
                ],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    complete_bucket = tmp_path / ".agents" / "plans" / "5 - complete"
    ideas_bucket = tmp_path / ".agents" / "plans" / "1 - ideas"
    complete_bucket.mkdir(parents=True, exist_ok=True)
    ideas_bucket.mkdir(parents=True, exist_ok=True)
    (complete_bucket / "20260201_BLG-001_0123456789abcdef_done.md").write_text(
        "# done\n",
        encoding="utf-8",
    )
    (ideas_bucket / "20260201_BLG-002_fedcba9876543210_todo.md").write_text(
        "# todo\n",
        encoding="utf-8",
    )

    impl_run_dir = (
        tmp_path
        / "runs"
        / "usertest_implement"
        / "usertest"
        / "20260201T010203Z"
        / "codex"
        / "0"
    )
    impl_run_dir.mkdir(parents=True, exist_ok=True)
    (impl_run_dir / "ticket_ref.json").write_text(
        json.dumps({"schema_version": 1, "ticket_id": "BLG-001"}) + "\n",
        encoding="utf-8",
    )
    (impl_run_dir / "timing.json").write_text(
        json.dumps({"schema_version": 1, "started_at": "2026-02-01T01:02:03Z"}) + "\n",
        encoding="utf-8",
    )
    (impl_run_dir / "pr_ref.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "created": True,
                "url": "https://example.invalid/p/1",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (impl_run_dir / "git_ref.json").write_text(
        json.dumps({"schema_version": 1, "branch": "backlog/blg-001", "head_commit": "abc123"})
        + "\n",
        encoding="utf-8",
    )
    (impl_run_dir / "diff_numstat.json").write_text(
        json.dumps([{"path": "README.md", "lines_added": 1, "lines_removed": 0}], indent=2) + "\n",
        encoding="utf-8",
    )

    out_json = tmp_path / "triage_atoms.json"
    out_md = tmp_path / "triage_atoms.md"

    with pytest.raises(SystemExit) as exc:
        main(
            [
                "triage-atoms",
                "--in",
                str(atoms_jsonl),
                "--repo-root",
                str(tmp_path),
                "--out-json",
                str(out_json),
                "--out-md",
                str(out_md),
            ]
        )
    assert exc.value.code == 0

    payload = json.loads(out_json.read_text(encoding="utf-8"))
    assert payload["totals"]["clusters_emitted"] == 1

    clusters = payload["clusters"]
    assert len(clusters) == 1
    cluster = clusters[0]
    assert cluster["size"] == 2
    assert cluster["tickets_total"] == 2
    assert cluster["representative_text"] == "python -m pytest -q"

    tickets = {t["ticket_id"]: t for t in cluster["tickets"]}
    assert tickets["BLG-001"]["plan"]["plan_buckets"] == ["5 - complete"]
    assert tickets["BLG-001"]["implementation_runs"][0]["pr_url"] == "https://example.invalid/p/1"
    assert tickets["BLG-002"]["plan"]["plan_buckets"] == ["1 - ideas"]
    assert tickets["BLG-002"]["implementation_runs"] == []

    assert "Atom Cluster Report" in out_md.read_text(encoding="utf-8")
