from __future__ import annotations

import json
from pathlib import Path

from run_artifacts.history import (
    iter_report_history,
    load_run_record,
    select_recent_run_dirs,
    write_report_history_jsonl,
)


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
    assert items[0]["embedded_capture_manifest"]["persona_source_md"]["exists"] is True
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
    assert items[0]["embedded_capture_manifest"] == {}


def test_write_report_history_jsonl_truncates_and_records_manifest(tmp_path: Path) -> None:
    runs_dir = tmp_path / "runs"
    run_dir = runs_dir / "tiktok_vids" / "20260101T000000Z" / "codex" / "0"
    run_dir.mkdir(parents=True)
    _write_json(run_dir / "target_ref.json", {"repo_input": "C:/repo/tiktok_vids"})
    _write_json(run_dir / "effective_run_spec.json", {})
    _write_json(run_dir / "report.json", {"schema_version": 1})
    (run_dir / "persona.source.md").write_text("A" * 512, encoding="utf-8")
    (run_dir / "persona.resolved.md").write_text("persona resolved\n", encoding="utf-8")
    (run_dir / "mission.source.md").write_text("mission source\n", encoding="utf-8")
    (run_dir / "mission.resolved.md").write_text("mission resolved\n", encoding="utf-8")
    (run_dir / "prompt.template.md").write_text("template\n", encoding="utf-8")

    out_path = tmp_path / "history.jsonl"
    write_report_history_jsonl(
        runs_dir,
        out_path=out_path,
        target_slug="tiktok_vids",
        embed="definitions",
        max_embed_bytes=100,
    )

    items = [json.loads(line) for line in out_path.read_text(encoding="utf-8").splitlines() if line]
    assert len(items) == 1

    excerpt = items[0]["embedded"]["persona_source_md"]
    assert isinstance(excerpt, str)
    assert "[truncated; see embedded_capture_manifest]" in excerpt

    manifest = items[0]["embedded_capture_manifest"]["persona_source_md"]
    assert manifest["path"] == "persona.source.md"
    assert manifest["exists"] is True
    assert manifest["size_bytes"] > 100
    assert isinstance(manifest["sha256"], str)
    assert manifest["truncated"] is True
    assert manifest["error"] is None


def test_iter_report_history_accepts_jsonl_source(tmp_path: Path) -> None:
    runs_dir = tmp_path / "runs"
    run_dir = runs_dir / "tiktok_vids" / "20260101T000000Z" / "codex" / "0"
    run_dir.mkdir(parents=True)
    _write_json(run_dir / "target_ref.json", {"repo_input": "C:/repo/tiktok_vids"})
    _write_json(run_dir / "effective_run_spec.json", {})
    _write_json(run_dir / "report.json", {"schema_version": 1})
    (run_dir / "persona.source.md").write_text("persona source\n", encoding="utf-8")
    (run_dir / "persona.resolved.md").write_text("persona resolved\n", encoding="utf-8")
    (run_dir / "mission.source.md").write_text("mission source\n", encoding="utf-8")
    (run_dir / "mission.resolved.md").write_text("mission resolved\n", encoding="utf-8")
    (run_dir / "prompt.template.md").write_text("template\n", encoding="utf-8")

    out_path = tmp_path / "history.jsonl"
    write_report_history_jsonl(runs_dir, out_path=out_path, target_slug="tiktok_vids", embed="all")

    items = list(iter_report_history(out_path, target_slug="tiktok_vids", embed="none"))
    assert len(items) == 1
    assert items[0]["embedded"] == {}
    assert items[0]["embedded_capture_manifest"] == {}


def test_iter_report_history_includes_run_meta_and_agent_attempts(tmp_path: Path) -> None:
    runs_dir = tmp_path / "runs"
    run_dir = runs_dir / "tiktok_vids" / "20260101T000000Z" / "codex" / "0"
    run_dir.mkdir(parents=True)
    _write_json(run_dir / "target_ref.json", {"repo_input": "C:/repo/tiktok_vids"})
    _write_json(run_dir / "effective_run_spec.json", {})
    _write_json(run_dir / "report.json", {"schema_version": 1})
    _write_json(run_dir / "run_meta.json", {"schema_version": 1, "run_wall_seconds": 12.25})
    _write_json(run_dir / "agent_attempts.json", {"attempts": [{"attempt": 1}]})

    items = list(iter_report_history(runs_dir, target_slug="tiktok_vids", embed="none"))
    assert len(items) == 1
    assert items[0]["run_meta"]["run_wall_seconds"] == 12.25
    assert items[0]["agent_attempts"]["attempts"][0]["attempt"] == 1


def test_iter_report_history_includes_ticket_ref_and_timing(tmp_path: Path) -> None:
    runs_dir = tmp_path / "runs"
    run_dir = runs_dir / "tiktok_vids" / "20260101T000000Z" / "codex" / "0"
    run_dir.mkdir(parents=True)
    _write_json(run_dir / "target_ref.json", {"repo_input": "C:/repo/tiktok_vids"})
    _write_json(run_dir / "effective_run_spec.json", {})
    _write_json(run_dir / "report.json", {"schema_version": 1})
    _write_json(
        run_dir / "ticket_ref.json",
        {"schema_version": 1, "fingerprint": "ab12cd34", "ticket_id": "BLG-003"},
    )
    _write_json(run_dir / "timing.json", {"schema_version": 1, "duration_seconds": 1.25})

    items = list(iter_report_history(runs_dir, target_slug="tiktok_vids", embed="none"))
    assert len(items) == 1
    assert items[0]["ticket_ref"]["fingerprint"] == "ab12cd34"
    assert items[0]["timing"]["duration_seconds"] == 1.25


def test_select_recent_run_dirs_orders_and_limits(tmp_path: Path) -> None:
    runs_dir = tmp_path / "runs"

    for ts_dir in ("20260101T000000Z", "20260102T000000Z", "20260103T000000Z"):
        run_dir = runs_dir / "tiktok_vids" / ts_dir / "codex" / "0"
        run_dir.mkdir(parents=True)
        _write_json(run_dir / "target_ref.json", {"repo_input": "C:/repo/tiktok_vids"})

    selected = select_recent_run_dirs(runs_dir, target_slug="tiktok_vids", limit=2)
    assert len(selected) == 2
    assert selected[0].parts[-4:] == ("tiktok_vids", "20260102T000000Z", "codex", "0")
    assert selected[1].parts[-4:] == ("tiktok_vids", "20260103T000000Z", "codex", "0")


def test_iter_report_history_distinguishes_incomplete_from_unreadable_terminal_artifacts(
    tmp_path: Path,
) -> None:
    runs_dir = tmp_path / "runs"

    # No terminal artifacts and no completion metadata: incomplete/interrupted run.
    incomplete_run = runs_dir / "target_a" / "20260101T000000Z" / "codex" / "0"
    incomplete_run.mkdir(parents=True)
    _write_json(incomplete_run / "target_ref.json", {"repo_input": "C:/repo/target_a"})
    _write_json(incomplete_run / "effective_run_spec.json", {})

    bad_report_run = runs_dir / "target_a" / "20260102T000000Z" / "codex" / "0"
    bad_report_run.mkdir(parents=True)
    _write_json(bad_report_run / "target_ref.json", {"repo_input": "C:/repo/target_a"})
    _write_json(bad_report_run / "effective_run_spec.json", {})
    (bad_report_run / "report.json").write_text("{not-json}\n", encoding="utf-8")

    bad_error_run = runs_dir / "target_a" / "20260103T000000Z" / "codex" / "0"
    bad_error_run.mkdir(parents=True)
    _write_json(bad_error_run / "target_ref.json", {"repo_input": "C:/repo/target_a"})
    _write_json(bad_error_run / "effective_run_spec.json", {})
    (bad_error_run / "error.json").write_text("{not-json}\n", encoding="utf-8")

    bad_validation_run = runs_dir / "target_a" / "20260104T000000Z" / "codex" / "0"
    bad_validation_run.mkdir(parents=True)
    _write_json(bad_validation_run / "target_ref.json", {"repo_input": "C:/repo/target_a"})
    _write_json(bad_validation_run / "effective_run_spec.json", {})
    (bad_validation_run / "report_validation_errors.json").write_text(
        "{not-json}\n",
        encoding="utf-8",
    )

    items = list(iter_report_history(runs_dir, target_slug="target_a", embed="none"))
    # The run with no terminal artifacts and no completion metadata is now incomplete, not
    # missing_report. Runs with corrupted terminal artifacts remain terminal_artifact_unreadable
    # via the legacy inference path (terminal artifacts present, no completion metadata).
    assert [item["status"] for item in items] == [
        "incomplete",
        "terminal_artifact_unreadable",
        "terminal_artifact_unreadable",
        "terminal_artifact_unreadable",
    ]

    incomplete_reads = items[0]["terminal_artifact_reads"]
    assert incomplete_reads["report.json"]["exists"] is False
    assert incomplete_reads["report.json"]["parse_ok"] is None

    bad_report_reads = items[1]["terminal_artifact_reads"]
    assert bad_report_reads["report.json"]["exists"] is True
    assert bad_report_reads["report.json"]["error_phase"] == "parse"
    assert bad_report_reads["report.json"]["error_type"] == "JSONDecodeError"

    bad_error_reads = items[2]["terminal_artifact_reads"]
    assert bad_error_reads["error.json"]["exists"] is True
    assert bad_error_reads["error.json"]["error_phase"] == "parse"
    assert bad_error_reads["error.json"]["error_type"] == "JSONDecodeError"

    bad_validation_reads = items[3]["terminal_artifact_reads"]
    assert bad_validation_reads["report_validation_errors.json"]["exists"] is True
    assert bad_validation_reads["report_validation_errors.json"]["error_phase"] == "parse"
    assert bad_validation_reads["report_validation_errors.json"]["error_type"] == "JSONDecodeError"

    counts = write_report_history_jsonl(
        runs_dir,
        out_path=tmp_path / "history.jsonl",
        target_slug="target_a",
        embed="none",
    )
    assert counts["incomplete"] == 1
    assert counts["missing_report"] == 0
    assert counts["no_terminal_artifact"] == 0
    assert counts["terminal_artifact_unreadable"] == 3


def test_terminal_outcome_contract(tmp_path: Path) -> None:
    """Contract test: enumerate terminal outcomes and assert consistent classification."""
    runs_dir = tmp_path / "runs"
    run_finished_utc = "2026-01-01T00:00:00Z"

    # 1. Finalized success: run_meta with run_finished_utc + report.json
    success_run = runs_dir / "target" / "20260101T000000Z" / "claude" / "0"
    success_run.mkdir(parents=True)
    _write_json(success_run / "target_ref.json", {"repo_input": "C:/repo/target"})
    _write_json(
        success_run / "run_meta.json",
        {"schema_version": 1, "run_finished_utc": run_finished_utc, "run_wall_seconds": 10.0},
    )
    _write_json(success_run / "report.json", {"schema_version": 1, "status": "pass"})

    # 2. Finalized failure: run_meta with run_finished_utc + error.json, no report.json
    failure_run = runs_dir / "target" / "20260102T000000Z" / "claude" / "0"
    failure_run.mkdir(parents=True)
    _write_json(failure_run / "target_ref.json", {"repo_input": "C:/repo/target"})
    _write_json(
        failure_run / "run_meta.json",
        {"schema_version": 1, "run_finished_utc": run_finished_utc, "run_wall_seconds": 5.0},
    )
    _write_json(failure_run / "error.json", {"type": "AgentExecFailed", "exit_code": 1})

    # 3. Finalized run missing its canonical report (contract violation for finalized runs).
    missing_report_run = runs_dir / "target" / "20260103T000000Z" / "codex" / "0"
    missing_report_run.mkdir(parents=True)
    _write_json(missing_report_run / "target_ref.json", {"repo_input": "C:/repo/target"})
    _write_json(
        missing_report_run / "run_meta.json",
        {"schema_version": 1, "run_finished_utc": run_finished_utc, "run_wall_seconds": 3.0},
    )
    # No report.json, no error.json: finalized but missing canonical report.

    # 4. Interrupted/incomplete: no run_meta.json, no terminal artifacts.
    interrupted_run = runs_dir / "target" / "20260104T000000Z" / "claude" / "0"
    interrupted_run.mkdir(parents=True)
    _write_json(interrupted_run / "target_ref.json", {"repo_input": "C:/repo/target"})
    # No run_meta.json, no terminal artifacts.

    # 5. Legacy run: has terminal artifacts but no run_meta.json (predates completion sentinel).
    legacy_run = runs_dir / "target" / "20260105T000000Z" / "codex" / "0"
    legacy_run.mkdir(parents=True)
    _write_json(legacy_run / "target_ref.json", {"repo_input": "C:/repo/target"})
    _write_json(legacy_run / "error.json", {"type": "LegacyError", "exit_code": 2})
    # No run_meta.json: legacy inference applies because terminal artifact is present.

    items = list(iter_report_history(runs_dir, target_slug="target", embed="none"))
    statuses = [item["status"] for item in items]
    assert statuses == [
        "ok",           # finalized success
        "error",        # finalized failure
        "missing_report",  # finalized but missing canonical report (contract violation)
        "incomplete",   # interrupted before any terminal artifact
        "error",        # legacy run classified via artifact inference
    ]

    # Finalized failure surfaces diagnostic fields.
    assert items[1]["error"]["type"] == "AgentExecFailed"
    assert items[1]["agent_exit_code"] == 1

    # Interrupted run has no terminal artifacts.
    incomplete_reads = items[3]["terminal_artifact_reads"]
    assert incomplete_reads["report.json"]["exists"] is False
    assert incomplete_reads["error.json"]["exists"] is False

    # Legacy run classified from artifact presence.
    assert items[4]["error"]["type"] == "LegacyError"


def test_load_run_record_includes_terminal_artifact_read_details(tmp_path: Path) -> None:
    runs_dir = tmp_path / "runs"
    run_dir = runs_dir / "target_a" / "20260102T000000Z" / "codex" / "0"
    run_dir.mkdir(parents=True)
    _write_json(run_dir / "target_ref.json", {"repo_input": "C:/repo/target_a"})
    _write_json(run_dir / "effective_run_spec.json", {})
    (run_dir / "report.json").write_text("{not-json}\n", encoding="utf-8")

    record = load_run_record(run_dir, runs_dir=runs_dir)
    assert record is not None
    assert record["status"] == "terminal_artifact_unreadable"
    assert record["terminal_artifact_reads"]["report.json"]["exists"] is True
    assert record["terminal_artifact_reads"]["report.json"]["error_phase"] == "parse"
