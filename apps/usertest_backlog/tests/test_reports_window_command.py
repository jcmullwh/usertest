from __future__ import annotations

import json
from pathlib import Path

import pytest
from runner_core import find_repo_root

from usertest_backlog.cli import main


def _write_json(path: Path, obj: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def test_reports_window_writes_default_outputs_and_splits_windows(tmp_path: Path) -> None:
    repo_root = find_repo_root(Path(__file__).resolve())

    runs_dir = tmp_path / "runs" / "usertest"
    target_slug = "target_a"

    timestamps = [
        ("20260101T000000Z", 10.0),
        ("20260102T000000Z", 20.0),
        ("20260103T000000Z", 30.0),
        ("20260104T000000Z", 40.0),
    ]
    for ts_dir, wall_seconds in timestamps:
        run_dir = runs_dir / target_slug / ts_dir / "codex" / "0"
        run_dir.mkdir(parents=True, exist_ok=True)
        _write_json(
            run_dir / "target_ref.json",
            {
                "repo_input": "pip:agent-adapters",
                "agent": "codex",
                "persona_id": "p",
                "mission_id": "m",
            },
        )
        _write_json(run_dir / "effective_run_spec.json", {})
        _write_json(
            run_dir / "report.json",
            {
                "adoption_decision": {"recommendation": "adopt"},
                "confusion_points": [{"summary": "No quickstart section"}],
                "suggested_changes": [{"change": "Add quickstart docs"}],
                "confidence_signals": {"missing": ["No smoke command"]},
            },
        )
        (run_dir / "agent_stderr.txt").write_text("", encoding="utf-8")
        (run_dir / "agent_last_message.txt").write_text("", encoding="utf-8")
        _write_json(
            run_dir / "run_meta.json",
            {"schema_version": 1, "run_wall_seconds": wall_seconds},
        )
        _write_json(run_dir / "agent_attempts.json", {"attempts": [{"attempt": 1}]})

    actions_path = tmp_path / "actions.json"
    _write_json(actions_path, {"actions": []})

    with pytest.raises(SystemExit) as exc:
        main(
            [
                "reports",
                "window",
                "--repo-root",
                str(repo_root),
                "--runs-dir",
                str(runs_dir),
                "--target",
                target_slug,
                "--last",
                "2",
                "--baseline",
                "2",
                "--actions",
                str(actions_path),
            ]
        )
    assert exc.value.code == 0

    out_json = runs_dir / target_slug / "_compiled" / f"{target_slug}.window_summary.json"
    out_md = out_json.with_suffix(".md")
    assert out_json.exists()
    assert out_md.exists()

    summary = json.loads(out_json.read_text(encoding="utf-8"))
    assert summary["schema_version"] == 1
    assert summary["summary"]["current"]["runs"] == 2
    assert summary["summary"]["baseline"]["runs"] == 2
    assert summary["summary"]["current"]["median_run_wall_seconds"] == 35.0
    assert any(
        item.get("persona_id") == "p" and item.get("mission_id") == "m"
        for item in summary.get("persona_mission", [])
        if isinstance(item, dict)
    )
