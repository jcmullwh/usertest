from __future__ import annotations

import json
from pathlib import Path

import pytest
from run_artifacts.history import write_report_history_jsonl
from runner_core import find_repo_root

from usertest.cli import main


def _write_json(path: Path, obj: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def test_reports_analyze_command_writes_outputs(tmp_path: Path) -> None:
    repo_root = find_repo_root(Path(__file__).resolve())

    runs_dir = tmp_path / "runs" / "usertest"
    run_ok = runs_dir / "target_a" / "20260101T000000Z" / "codex" / "0"
    run_invalid = runs_dir / "target_a" / "20260102T000000Z" / "gemini" / "0"
    run_ok.mkdir(parents=True, exist_ok=True)
    run_invalid.mkdir(parents=True, exist_ok=True)

    _write_json(
        run_ok / "target_ref.json",
        {
            "repo_input": "pip:agent-adapters",
            "agent": "codex",
            "persona_id": "routine_operator",
            "mission_id": "complete_output_smoke",
        },
    )
    _write_json(run_ok / "effective_run_spec.json", {})
    _write_json(
        run_ok / "report.json",
        {
            "adoption_decision": {"recommendation": "adopt"},
            "confusion_points": [
                {"summary": "No documentation or examples included in the package"}
            ],
            "suggested_changes": [{"change": "Add __version__ attribute"}],
            "confidence_signals": {"missing": ["No entry points are installed"]},
        },
    )
    (run_ok / "agent_stderr.txt").write_text("", encoding="utf-8")
    (run_ok / "agent_last_message.txt").write_text("", encoding="utf-8")

    _write_json(
        run_invalid / "target_ref.json",
        {
            "repo_input": "pip:agent-adapters",
            "agent": "gemini",
            "persona_id": "routine_operator",
            "mission_id": "complete_output_smoke",
        },
    )
    _write_json(run_invalid / "effective_run_spec.json", {})
    _write_json(
        run_invalid / "report_validation_errors.json",
        [
            "$: failed to parse JSON from agent output: "
            "Could not find a JSON object in agent output."
        ],
    )
    (run_invalid / "agent_stderr.txt").write_text(
        "Attempt 2 failed with status 429. Retrying with backoff...\n",
        encoding="utf-8",
    )
    (run_invalid / "agent_last_message.txt").write_text(
        "Task complete. I've produced the required JSON output and confirmed its content.\n",
        encoding="utf-8",
    )

    actions_path = tmp_path / "issue_actions.json"
    _write_json(
        actions_path,
        {
            "version": 1,
            "actions": [
                {
                    "id": "a1",
                    "date": "2026-02-06",
                    "plan": "docs/ops/example_plan.md",
                    "match": {
                        "theme_ids": ["docs_discoverability"],
                        "text_patterns": ["no documentation"],
                    },
                }
            ],
        },
    )

    with pytest.raises(SystemExit) as exc:
        main(
            [
                "reports",
                "analyze",
                "--repo-root",
                str(repo_root),
                "--runs-dir",
                str(runs_dir),
                "--target",
                "target_a",
                "--actions",
                str(actions_path),
            ]
        )
    assert exc.value.code == 0

    out_json = runs_dir / "target_a" / "_compiled" / "target_a.issue_analysis.json"
    out_md = runs_dir / "target_a" / "_compiled" / "target_a.issue_analysis.md"

    assert out_json.exists()
    assert out_md.exists()

    summary = json.loads(out_json.read_text(encoding="utf-8"))
    assert summary["totals"]["runs"] == 2
    assert summary["action_tracking"]["loaded_actions"] == 1
    theme_ids = {item["theme_id"] for item in summary["themes"]}
    assert "docs_discoverability" in theme_ids
    assert "output_contract" in theme_ids
    docs_theme = next(
        item
        for item in summary["themes"]
        if item["theme_id"] == "docs_discoverability"
    )
    assert docs_theme["addressed_mentions"] >= 1
    assert docs_theme["unaddressed_mentions"] >= 0

    markdown = out_md.read_text(encoding="utf-8")
    assert "Addressed comments (listed after unaddressed)" in markdown


def test_reports_analyze_command_accepts_history_jsonl(tmp_path: Path) -> None:
    repo_root = find_repo_root(Path(__file__).resolve())

    runs_dir = tmp_path / "runs" / "usertest"
    run_ok = runs_dir / "target_a" / "20260101T000000Z" / "codex" / "0"
    run_ok.mkdir(parents=True, exist_ok=True)

    _write_json(
        run_ok / "target_ref.json",
        {
            "repo_input": "pip:agent-adapters",
            "agent": "codex",
            "persona_id": "routine_operator",
            "mission_id": "complete_output_smoke",
        },
    )
    _write_json(run_ok / "effective_run_spec.json", {})
    _write_json(
        run_ok / "report.json",
        {
            "confusion_points": [{"summary": "No quickstart section"}],
            "suggested_changes": [{"change": "Add quickstart docs"}],
            "confidence_signals": {"missing": ["No smoke command"]},
        },
    )
    (run_ok / "agent_stderr.txt").write_text("", encoding="utf-8")
    (run_ok / "agent_last_message.txt").write_text("", encoding="utf-8")

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
