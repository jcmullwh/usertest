from __future__ import annotations

import json
from pathlib import Path

import pytest
from runner_core import find_repo_root

from usertest.cli import main


def test_report_command_renders_markdown(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo_root = find_repo_root(Path(__file__).resolve())

    run_dir = tmp_path / "runs" / "target" / "20260101T000000Z" / "codex" / "0"
    run_dir.mkdir(parents=True, exist_ok=True)

    report = {
        "schema_version": 1,
        "persona": {"name": "Evaluator", "description": "Tests re-rendering."},
        "mission": "Assess fit quickly and safely.",
        "minimal_mental_model": {
            "summary": "A short mental model summary.",
            "entry_points": ["README.md", "USERS.md"],
            "constraints": ["Requires Python 3.11+"],
        },
        "confidence_signals": {"found": ["Has a README"], "missing": ["No API docs"]},
        "confusion_points": [
            {
                "summary": "Docs mention X but code does Y.",
                "impact": "User wastes time.",
                "evidence": [{"kind": "file", "value": "README.md"}],
            }
        ],
        "adoption_decision": {
            "recommendation": "investigate",
            "rationale": "Looks promising but needs work.",
            "qualifiers": ["Codex-only today."],
        },
        "suggested_changes": [
            {
                "type": "doc",
                "priority": "p0",
                "location": "README.md",
                "change": "Clarify installation steps.",
                "expected_impact": "Reduce setup friction.",
            }
        ],
    }
    (run_dir / "report.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    (run_dir / "metrics.json").write_text(
        json.dumps({"events_total": 1}, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    (run_dir / "target_ref.json").write_text(
        json.dumps({"repo_input": "x"}, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(SystemExit) as exc:
        main(["report", "--repo-root", str(repo_root), "--run-dir", str(run_dir)])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert str(run_dir / "report.md") in out

    md = (run_dir / "report.md").read_text(encoding="utf-8")
    assert "# Persona exploration report" in md
    assert "## Target" in md
    assert "## Summary" in md
    assert "## Metrics" in md
    assert '"repo_input": "x"' in md
    assert '"events_total": 1' in md
