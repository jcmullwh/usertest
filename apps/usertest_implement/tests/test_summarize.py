from __future__ import annotations

import json
from pathlib import Path

from usertest_implement.summarize import iter_implementation_rows


def _write_json(path: Path, obj: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def test_iter_implementation_rows_includes_ticket_and_heuristics(tmp_path: Path) -> None:
    runs_dir = tmp_path / "runs"
    run_dir = runs_dir / "target_a" / "20260220T010203Z" / "codex" / "0"
    run_dir.mkdir(parents=True)

    _write_json(
        run_dir / "target_ref.json",
        {
            "repo_input": "C:/repo/x",
            "commit_sha": "abc123",
            "agent": "codex",
            "policy": "write",
            "seed": 0,
        },
    )
    _write_json(run_dir / "effective_run_spec.json", {})
    _write_json(
        run_dir / "report.json",
        {
            "schema_version": 1,
            "kind": "task_run_v1",
            "status": "success",
            "goal": "g",
            "summary": "s",
            "steps": [
                {
                    "name": "n",
                    "attempts": [{"action": "a"}],
                    "outcome": "o",
                }
            ],
            "outputs": [],
            "next_actions": ["x"],
        },
    )
    _write_json(
        run_dir / "metrics.json",
        {
            "step_count": 5,
            "commands_failed": 1,
            "distinct_files_written": ["a.py", "b.py"],
            "lines_added_total": 10,
            "lines_removed_total": 2,
        },
    )
    _write_json(
        run_dir / "ticket_ref.json",
        {
            "schema_version": 1,
            "fingerprint": "deadbeefdeadbeef",
            "title": "Do thing",
        },
    )
    _write_json(
        run_dir / "timing.json",
        {
            "schema_version": 1,
            "started_at": "2026-02-20T01:02:03Z",
            "finished_at": "2026-02-20T01:02:13Z",
            "duration_seconds": 10.0,
        },
    )
    (run_dir / "normalized_events.jsonl").write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "run_command",
                        "data": {"command": "pytest -q", "exit_code": 1},
                    }
                ),
                json.dumps(
                    {
                        "type": "run_command",
                        "data": {"command": "pytest -q", "exit_code": 0},
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    rows = list(iter_implementation_rows(runs_dir))
    assert len(rows) == 1
    row = rows[0]
    assert row["ticket"]["fingerprint"] == "deadbeefdeadbeef"
    assert "ticket_id" not in row["ticket"]
    assert row["run"]["duration_seconds"] == 10.0
    assert row["metrics"]["files_written"] == 2
    assert row["heuristics"]["test_runs_total"] == 2
    assert row["heuristics"]["test_runs_failed_before_success"] == 1
