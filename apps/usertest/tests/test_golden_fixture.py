from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from reporter import validate_report
from runner_core import find_repo_root

from usertest.cli import main


@pytest.mark.parametrize(
    ("fixture_name", "expected_doc"),
    [
        ("minimal_codex_run", "README.md"),
        ("minimal_claude_run", "USERS.md"),
        ("minimal_gemini_run", "USERS.md"),
    ],
)
def test_golden_fixture_renders_and_recomputes_metrics(
    tmp_path: Path,
    fixture_name: str,
    expected_doc: str,
) -> None:
    repo_root = find_repo_root(Path(__file__).resolve())
    fixture_src = repo_root / "examples" / "golden_runs" / fixture_name
    fixture_dst = tmp_path / fixture_name
    shutil.copytree(fixture_src, fixture_dst)

    with pytest.raises(SystemExit) as exc:
        main(
            [
                "report",
                "--repo-root",
                str(repo_root),
                "--run-dir",
                str(fixture_dst),
                "--recompute-metrics",
            ]
        )
    assert exc.value.code == 0

    metrics = json.loads((fixture_dst / "metrics.json").read_text(encoding="utf-8"))
    assert metrics["commands_executed"] == 1
    assert metrics["commands_failed"] == 0
    assert metrics["lines_added_total"] == 0
    assert expected_doc in metrics["distinct_docs_read"]

    md = (fixture_dst / "report.md").read_text(encoding="utf-8")
    assert "# Report" in md
    assert "## Metrics" in md


def test_golden_fixtures_follow_minimal_contract_and_schema() -> None:
    repo_root = find_repo_root(Path(__file__).resolve())
    fixtures_root = repo_root / "examples" / "golden_runs"
    fixture_names = ["minimal_codex_run", "minimal_claude_run", "minimal_gemini_run"]
    required_files = [
        "raw_events.jsonl",
        "normalized_events.jsonl",
        "target_ref.json",
        "metrics.json",
        "report.json",
        "report.md",
        "report.schema.json",
    ]

    for fixture_name in fixture_names:
        fixture_dir = fixtures_root / fixture_name
        assert fixture_dir.exists(), f"missing fixture dir: {fixture_dir}"
        for rel in required_files:
            assert (fixture_dir / rel).exists(), f"missing {rel} in {fixture_name}"

        report = json.loads((fixture_dir / "report.json").read_text(encoding="utf-8"))
        schema = json.loads((fixture_dir / "report.schema.json").read_text(encoding="utf-8"))
        errors = validate_report(report, schema)
        assert errors == []
