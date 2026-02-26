from __future__ import annotations

import json
import shutil
from datetime import datetime, timedelta, timezone
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
    assert "# Persona exploration report" in md
    assert "## Summary" in md
    assert "## Metrics" in md

    for rel in ["report.md", "normalized_events.jsonl", "metrics.json"]:
        assert (fixture_dst / rel).read_bytes() == (fixture_src / rel).read_bytes()


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


def test_recompute_metrics_is_stable_with_raw_ts_and_diff_numstat(tmp_path: Path) -> None:
    repo_root = find_repo_root(Path(__file__).resolve())
    fixture_src = repo_root / "examples" / "golden_runs" / "minimal_codex_run"
    fixture_dst = tmp_path / "minimal_codex_run"
    shutil.copytree(fixture_src, fixture_dst)

    diff_numstat = [{"path": "src/example.py", "lines_added": 3, "lines_removed": 1}]
    (fixture_dst / "diff_numstat.json").write_text(
        json.dumps(diff_numstat, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
        newline="\n",
    )

    raw_lines = (fixture_dst / "raw_events.jsonl").read_text(encoding="utf-8").splitlines()
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    ts_lines = [
        (base + timedelta(seconds=i)).replace(microsecond=0).isoformat()
        for i in range(len(raw_lines))
    ]
    (fixture_dst / "raw_events.ts.jsonl").write_text(
        "\n".join(ts_lines) + "\n",
        encoding="utf-8",
        newline="\n",
    )

    def _recompute() -> None:
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

    _recompute()
    norm_1 = (fixture_dst / "normalized_events.jsonl").read_bytes()
    metrics_1 = (fixture_dst / "metrics.json").read_bytes()

    _recompute()
    assert (fixture_dst / "normalized_events.jsonl").read_bytes() == norm_1
    assert (fixture_dst / "metrics.json").read_bytes() == metrics_1
