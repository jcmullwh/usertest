from __future__ import annotations

from pathlib import Path

from reporter import compute_metrics, load_schema, validate_report


def test_compute_metrics_basic_counts() -> None:
    metrics = compute_metrics(
        [
            {
                "ts": "2026-01-01T00:00:00Z",
                "type": "read_file",
                "data": {"path": "README.md", "bytes": 10},
            },
            {
                "ts": "2026-01-01T00:00:01Z",
                "type": "read_file",
                "data": {"path": "src/app.py", "bytes": 20},
            },
            {
                "ts": "2026-01-01T00:00:02Z",
                "type": "run_command",
                "data": {"argv": ["rg", "TODO", "README.md"], "exit_code": 1},
            },
            {
                "ts": "2026-01-01T00:00:03Z",
                "type": "write_file",
                "data": {"path": "README.md", "lines_added": 5, "lines_removed": 0},
            },
        ]
    )
    assert metrics["commands_executed"] == 1
    assert metrics["commands_failed"] == 1
    assert metrics["failed_commands"] == [{"command": "rg TODO README.md", "exit_code": 1}]
    assert "README.md" in metrics["distinct_docs_read"]
    assert metrics["lines_added_total"] == 5


def test_validate_report_errors() -> None:
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "required": ["schema_version"],
        "properties": {"schema_version": {"type": "integer"}},
    }
    errors = validate_report({"schema_version": "nope"}, schema)
    assert errors


def test_task_run_schema_allows_nullable_output_path() -> None:
    repo_root = Path(__file__).resolve().parents[3]
    schema_path = repo_root / "configs" / "report_schemas" / "task_run_v1.schema.json"
    schema = load_schema(schema_path)

    report = {
        "schema_version": 1,
        "kind": "task_run_v1",
        "status": "success",
        "goal": "Validate install workflow.",
        "summary": "Completed successfully.",
        "steps": [
            {
                "name": "Install",
                "attempts": [{"action": "pip install ."}],
                "outcome": "success",
            }
        ],
        "outputs": [{"label": "install log", "path": None}],
        "next_actions": ["Run package smoke tests."],
    }

    errors = validate_report(report, schema)
    assert errors == []
