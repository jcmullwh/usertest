from __future__ import annotations

import json
from pathlib import Path

from run_artifacts.history import iter_report_history, write_report_history_jsonl


def _write_json(path: Path, obj: object) -> None:
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def test_write_report_history_jsonl_filters_and_embeds(tmp_path: Path) -> None:
    runs_dir = tmp_path / "runs"

    ok_run = runs_dir / "tiktok_vids" / "20260101T000000Z" / "codex" / "0"
    ok_run.mkdir(parents=True)
    _write_json(
        ok_run / "target_ref.json",
        {
            "repo_input": "C:/repo/tiktok_vids/",
            "agent": "codex",
            "policy": "inspect",
            "seed": 0,
            "persona_id": "p",
            "mission_id": "m",
        },
    )
    _write_json(ok_run / "effective_run_spec.json", {"persona_id": "p", "mission_id": "m"})
    _write_json(
        ok_run / "report.json",
        {
            "schema_version": 1,
            "repo": "tiktok_vids",
            "persona": "Persona",
            "mission": "Mission",
        },
    )
    _write_json(ok_run / "metrics.json", {"commands_executed": 1})
    _write_json(ok_run / "report.schema.json", {"type": "object"})
    (ok_run / "persona.source.md").write_text("persona source\n", encoding="utf-8")
    (ok_run / "persona.resolved.md").write_text("persona resolved\n", encoding="utf-8")
    (ok_run / "mission.source.md").write_text("mission source\n", encoding="utf-8")
    (ok_run / "mission.resolved.md").write_text("mission resolved\n", encoding="utf-8")
    (ok_run / "prompt.template.md").write_text("template\n", encoding="utf-8")

    error_run = runs_dir / "tiktok_vids" / "20260102T000000Z" / "codex" / "0"
    error_run.mkdir(parents=True)
    _write_json(
        error_run / "target_ref.json",
        {
            "repo_input": "C:/repo/tiktok_vids",
            "agent": "codex",
            "policy": "inspect",
            "seed": 0,
        },
    )
    _write_json(error_run / "effective_run_spec.json", {})
    _write_json(error_run / "error.json", {"type": "AgentExecFailed", "exit_code": 2})

    # Should be ignored by iter_run_dirs (leading underscore).
    ignored = runs_dir / "_workspaces" / "tiktok_vids" / "20260103T000000Z" / "codex" / "0"
    ignored.mkdir(parents=True)
    _write_json(ignored / "target_ref.json", {"repo_input": "C:/repo/tiktok_vids"})

    out_path = tmp_path / "history.jsonl"
    counts = write_report_history_jsonl(
        runs_dir,
        out_path=out_path,
        repo_input="C:/repo/tiktok_vids",
        embed="definitions",
    )

    assert counts["total"] == 2
    assert counts["ok"] == 1
    assert counts["error"] == 1

    items = [json.loads(line) for line in out_path.read_text(encoding="utf-8").splitlines() if line]
    assert items[0]["status"] == "ok"
    assert items[0]["embedded"]["persona_source_md"].startswith("persona source")
    assert items[1]["status"] == "error"
    assert items[1]["agent_exit_code"] == 2


def test_iter_report_history_embed_none(tmp_path: Path) -> None:
    runs_dir = tmp_path / "runs"
    run_dir = runs_dir / "tiktok_vids" / "20260101T000000Z" / "codex" / "0"
    run_dir.mkdir(parents=True)
    _write_json(run_dir / "target_ref.json", {"repo_input": "C:/repo/tiktok_vids"})
    _write_json(run_dir / "effective_run_spec.json", {})
    _write_json(run_dir / "report.json", {"schema_version": 1})

    items = list(iter_report_history(runs_dir, target_slug="tiktok_vids", embed="none"))
    assert len(items) == 1
    assert items[0]["embedded"] == {}
