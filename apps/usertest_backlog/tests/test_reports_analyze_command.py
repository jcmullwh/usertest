from __future__ import annotations

import json
from pathlib import Path

import pytest
from run_artifacts.history import write_report_history_jsonl
from runner_core import find_repo_root

from usertest_backlog.cli import main


def _write_json(path: Path, obj: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def test_reports_analyze_history_source_writes_default_outputs(tmp_path: Path) -> None:
    repo_root = find_repo_root(Path(__file__).resolve())

    runs_dir = tmp_path / "runs" / "usertest"
    run_dir = runs_dir / "target_a" / "20260101T000000Z" / "codex" / "0"
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
    _write_json(
        run_dir / "report.json",
        {
            "confusion_points": [{"summary": "No quickstart section"}],
            "suggested_changes": [{"change": "Add quickstart docs"}],
            "confidence_signals": {"missing": ["No smoke command"]},
        },
    )
    (run_dir / "agent_stderr.txt").write_text("", encoding="utf-8")
    (run_dir / "agent_last_message.txt").write_text("", encoding="utf-8")

    history_path = tmp_path / "report_history.jsonl"
    write_report_history_jsonl(
        runs_dir,
        out_path=history_path,
        target_slug="target_a",
        embed="none",
    )

    with pytest.raises(SystemExit) as exc:
        main(
            [
                "reports",
                "analyze",
                "--repo-root",
                str(repo_root),
                "--history",
                str(history_path),
                "--target",
                "target_a",
            ]
        )
    assert exc.value.code == 0

    out_json = history_path.with_name("report_history.issue_analysis.json")
    out_md = history_path.with_name("report_history.issue_analysis.md")
    assert out_json.exists()
    assert out_md.exists()

    summary = json.loads(out_json.read_text(encoding="utf-8"))
    assert summary["totals"]["runs"] == 1
