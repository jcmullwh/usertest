from __future__ import annotations

import importlib.util
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType


def _load_tool() -> ModuleType:
    path = Path(__file__).resolve().parents[1] / "refresh_pipeline_metrics.py"
    spec = importlib.util.spec_from_file_location("refresh_pipeline_metrics", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


tool = _load_tool()


def _write_event(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "event_id": "event-1",
                "event_type": "lifecycle.opened",
                "occurred_at": "2026-07-01T00:00:00Z",
                "context": {"case_lifecycle_id": "life-1", "case_id": "case-1"},
            }
        )
        + "\n",
        encoding="utf-8",
    )


def test_missing_outputs_force_refresh_and_main_materializes(tmp_path: Path) -> None:
    _write_event(tmp_path / "runs" / "lifecycle_events.jsonl")
    output = tmp_path / "metrics"
    decision = tool.decide_refresh(
        roots=[tmp_path / "runs"],
        output_dir=output,
        stale_after=timedelta(hours=24),
        now=datetime(2026, 7, 21, tzinfo=timezone.utc),
    )
    assert decision.refresh is True
    assert "derived_artifact_missing" in decision.reasons

    assert (
        tool.main(
            [
                "--root",
                str(tmp_path / "runs"),
                "--output-dir",
                str(output),
                "--cohort-id",
                "daily",
            ]
        )
        == 0
    )
    assert (output / "case_metrics.json").is_file()
    assert (output / "cohort_metrics.json").is_file()
    assert (output / "metrics_dashboard.json").is_file()
    assert (output / "metrics_dashboard.html").is_file()


def test_open_lifecycle_refreshes_after_daily_threshold(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    _write_event(root / "lifecycle_events.jsonl")
    (root / "lifecycle_manifest.json").write_text(
        json.dumps({"status": "active"}) + "\n", encoding="utf-8"
    )
    output = tmp_path / "metrics"
    output.mkdir()
    (output / "case_metrics.json").write_text(
        json.dumps({"metric_version": tool.CASE_METRICS_VERSION}) + "\n",
        encoding="utf-8",
    )
    (output / "cohort_metrics.json").write_text(
        json.dumps(
            {
                "metric_version": tool.CASE_METRICS_VERSION,
                "automation_score_v1": {"version": tool.AUTOMATION_SCORE_VERSION},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (output / "metrics_dashboard.json").write_text(
        json.dumps({"schema_version": 4}) + "\n", encoding="utf-8"
    )
    (output / "metrics_dashboard.html").write_text("generated", encoding="utf-8")
    old = datetime(2026, 7, 19, tzinfo=timezone.utc).timestamp()
    source_old = datetime(2026, 7, 18, tzinfo=timezone.utc).timestamp()
    for path in output.iterdir():
        os.utime(path, (old, old))
    os.utime(root / "lifecycle_events.jsonl", (source_old, source_old))

    decision = tool.decide_refresh(
        roots=[root],
        output_dir=output,
        stale_after=timedelta(hours=24),
        now=datetime(2026, 7, 21, tzinfo=timezone.utc),
    )
    assert decision.open_manifest_count == 1
    assert decision.reasons == ("open_lifecycle_daily_refresh_due",)


def test_closed_unchanged_lifecycle_does_not_refresh(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    _write_event(root / "lifecycle_events.jsonl")
    (root / "lifecycle_manifest.json").write_text(
        json.dumps({"status": "terminal"}) + "\n", encoding="utf-8"
    )
    output = tmp_path / "metrics"
    output.mkdir()
    (output / "case_metrics.json").write_text(
        json.dumps({"metric_version": tool.CASE_METRICS_VERSION}) + "\n",
        encoding="utf-8",
    )
    (output / "cohort_metrics.json").write_text(
        json.dumps(
            {
                "metric_version": tool.CASE_METRICS_VERSION,
                "automation_score_v1": {"version": tool.AUTOMATION_SCORE_VERSION},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (output / "metrics_dashboard.json").write_text(
        json.dumps({"schema_version": 4}) + "\n", encoding="utf-8"
    )
    (output / "metrics_dashboard.html").write_text("generated", encoding="utf-8")
    source_old = datetime(2026, 7, 18, tzinfo=timezone.utc).timestamp()
    derived_new = datetime(2026, 7, 20, tzinfo=timezone.utc).timestamp()
    os.utime(root / "lifecycle_events.jsonl", (source_old, source_old))
    for path in output.iterdir():
        os.utime(path, (derived_new, derived_new))

    decision = tool.decide_refresh(
        roots=[root],
        output_dir=output,
        stale_after=timedelta(hours=24),
        now=datetime(2026, 7, 21, tzinfo=timezone.utc),
    )
    assert decision.refresh is False


def test_definition_versions_force_refresh_without_new_source_events(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    _write_event(root / "lifecycle_events.jsonl")
    output = tmp_path / "metrics"
    output.mkdir()
    (output / "case_metrics.json").write_text(
        json.dumps({"metric_version": tool.CASE_METRICS_VERSION}) + "\n",
        encoding="utf-8",
    )
    (output / "cohort_metrics.json").write_text(
        json.dumps({"automation_score_v1": {"version": "obsolete"}}) + "\n",
        encoding="utf-8",
    )
    (output / "metrics_dashboard.json").write_text(
        json.dumps({"schema_version": 3}) + "\n", encoding="utf-8"
    )
    (output / "metrics_dashboard.html").write_text("obsolete", encoding="utf-8")
    source_old = datetime(2026, 7, 18, tzinfo=timezone.utc).timestamp()
    derived_new = datetime(2026, 7, 20, tzinfo=timezone.utc).timestamp()
    os.utime(root / "lifecycle_events.jsonl", (source_old, source_old))
    for path in output.iterdir():
        os.utime(path, (derived_new, derived_new))

    decision = tool.decide_refresh(
        roots=[root],
        output_dir=output,
        stale_after=timedelta(hours=24),
        now=datetime(2026, 7, 21, tzinfo=timezone.utc),
    )

    assert "score_definition_changed" in decision.reasons
    assert "dashboard_definition_changed" in decision.reasons
