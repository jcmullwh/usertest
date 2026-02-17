from __future__ import annotations

import json
from pathlib import Path

import pytest
from runner_core import find_repo_root

from usertest_backlog.cli import main


def test_reports_intent_snapshot_writes_outputs(tmp_path: Path) -> None:
    repo_root = find_repo_root(Path(__file__).resolve())
    runs_dir = tmp_path / "runs" / "usertest"

    with pytest.raises(SystemExit) as exc:
        main(
            [
                "reports",
                "intent-snapshot",
                "--repo-root",
                str(repo_root),
                "--runs-dir",
                str(runs_dir),
                "--target",
                "target_a",
            ]
        )
    assert exc.value.code == 0

    compiled = runs_dir / "target_a" / "_compiled"
    out_json = compiled / "target_a.intent_snapshot.json"
    out_md = compiled / "target_a.intent_snapshot.md"

    assert out_json.exists()
    assert out_md.exists()

    snapshot = json.loads(out_json.read_text(encoding="utf-8"))
    assert snapshot["schema_version"] == 1
    assert snapshot["llm_summary"] is None
    assert snapshot["llm_summary_meta"]["status"] == "not_requested"

    commands = snapshot.get("commands")
    assert isinstance(commands, list)
    assert any(
        isinstance(item, dict) and item.get("command") == "usertest-backlog reports backlog"
        for item in commands
    )

    docs_index = snapshot.get("docs_index")
    assert isinstance(docs_index, list)

    markdown = out_md.read_text(encoding="utf-8")
    assert "# Repo Intent Snapshot" in markdown
