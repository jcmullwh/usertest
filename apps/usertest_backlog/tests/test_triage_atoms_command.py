from __future__ import annotations

import json
from pathlib import Path

import pytest

from backlog_repo.export import ticket_export_fingerprint
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
    ticket_1 = {
        "title": "Fix pytest invocation in sandbox",
        "stage": "ready_for_ticket",
        "severity": "high",
        "evidence_atom_ids": [atom_1["atom_id"], atom_2["atom_id"]],
    }
    ticket_2 = {
        "title": "Docs: add PYTHONPATH hint",
        "stage": "triage",
        "severity": "medium",
        "evidence_atom_ids": [atom_1["atom_id"]],
    }
    fp_1 = ticket_export_fingerprint(ticket_1)
    fp_2 = ticket_export_fingerprint(ticket_2)
    backlog_json.write_text(
        json.dumps(
            {
                "generated_at_utc": "2026-02-01T00:00:10Z",
                "input": {
                    "runs_dir": str(tmp_path / "runs" / "usertest_implement"),
                    "target": "usertest",
                },
                "tickets": [ticket_1, ticket_2],
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
    (complete_bucket / f"20260201_{fp_1}_done.md").write_text(
        "# done\n",
        encoding="utf-8",
    )
    (ideas_bucket / f"20260201_{fp_2}_todo.md").write_text(
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
        json.dumps({"schema_version": 1, "fingerprint": fp_1}) + "\n",
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

    tickets = {t["fingerprint"]: t for t in cluster["tickets"]}
    assert tickets[fp_1]["fingerprint"] == fp_1
    assert tickets[fp_1]["plan"]["plan_buckets"] == ["5 - complete"]
    assert tickets[fp_1]["plan"]["fingerprint"] == fp_1
    assert tickets[fp_1]["implementation_runs"][0]["pr_url"] == "https://example.invalid/p/1"
    assert tickets[fp_2]["fingerprint"] == fp_2
    assert tickets[fp_2]["plan"]["plan_buckets"] == ["1 - ideas"]
    assert tickets[fp_2]["plan"]["fingerprint"] == fp_2
    assert tickets[fp_2]["implementation_runs"] == []

    assert "Atom Cluster Report" in out_md.read_text(encoding="utf-8")


def test_triage_atoms_joins_plans_and_runs_by_fingerprint(tmp_path: Path) -> None:
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
    atoms_jsonl.write_text("\n".join([json.dumps(atom_1), json.dumps(atom_2)]) + "\n", encoding="utf-8")

    backlog_json = atoms_dir / "usertest.backlog.json"
    ticket = {
        "title": "Fix pytest invocation in sandbox",
        "stage": "ready_for_ticket",
        "severity": "high",
        "evidence_atom_ids": [atom_1["atom_id"], atom_2["atom_id"]],
    }
    fp = ticket_export_fingerprint(ticket)
    fp_other = ("0" if fp[0] != "0" else "1") + fp[1:]

    backlog_json.write_text(
        json.dumps(
            {
                "generated_at_utc": "2026-02-01T00:00:10Z",
                "input": {
                    "runs_dir": str(tmp_path / "runs" / "usertest_implement"),
                    "target": "usertest",
                },
                "tickets": [ticket],
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
    (ideas_bucket / f"20260201_{fp}_todo.md").write_text("# todo\n", encoding="utf-8")
    (complete_bucket / f"20260201_{fp_other}_done.md").write_text("# done\n", encoding="utf-8")

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
        json.dumps({"schema_version": 1, "fingerprint": fp_other}) + "\n",
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
    cluster = payload["clusters"][0]
    ticket_out = cluster["tickets"][0]
    assert ticket_out["fingerprint"] == fp
    assert "ticket_id" not in ticket_out
    assert ticket_out["plan"]["plan_buckets"] == ["1 - ideas"]
    assert ticket_out["implementation_runs"] == []


def test_triage_atoms_can_exclude_sources(tmp_path: Path) -> None:
    atoms_jsonl = tmp_path / "atoms.jsonl"
    atoms_jsonl.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "atom_id": "usertest/20260201T000000Z/codex/0:agent_last_message_artifact:1",
                        "run_id": "usertest/20260201T000000Z/codex/0",
                        "run_rel": "usertest/20260201T000000Z/codex/0",
                        "run_dir": "runs/usertest_implement/usertest/20260201T000000Z/codex/0",
                        "timestamp_utc": "2026-02-01T00:00:00Z",
                        "source": "agent_last_message_artifact",
                        "severity_hint": "low",
                        "text": "{\"kind\":\"task_run_v1\",\"status\":\"success\"}",
                    }
                ),
                json.dumps(
                    {
                        "atom_id": "usertest/20260201T000001Z/codex/0:command_failure:1",
                        "run_id": "usertest/20260201T000001Z/codex/0",
                        "run_rel": "usertest/20260201T000001Z/codex/0",
                        "run_dir": "runs/usertest_implement/usertest/20260201T000001Z/codex/0",
                        "timestamp_utc": "2026-02-01T00:00:01Z",
                        "source": "command_failure",
                        "severity_hint": "high",
                        "text": "Command failed: exit_code=1; command=python -V",
                    }
                ),
            ]
        )
        + "\n",
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
                "--overall-threshold",
                "0",
                "--min-cluster-size",
                "1",
                "--exclude-source",
                "agent_last_message_artifact",
                "--out-json",
                str(out_json),
                "--out-md",
                str(out_md),
            ]
        )
    assert exc.value.code == 0

    payload = json.loads(out_json.read_text(encoding="utf-8"))
    assert payload["config"]["exclude_sources"] == ["agent_last_message_artifact"]
    assert payload["totals"]["atoms_total_input"] == 2
    assert payload["totals"]["atoms_excluded_total"] == 1
    assert payload["totals"]["atoms_total"] == 1

    clusters = payload["clusters"]
    assert len(clusters) == 1
    assert clusters[0]["size"] == 1
    assert clusters[0]["representative_text"] == "python -V"
