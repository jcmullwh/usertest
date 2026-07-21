from __future__ import annotations

from typing import Any

from run_artifacts.lifecycle import (
    JsonArtifactReadResult,
    classify_history_record_lifecycle,
    classify_run_lifecycle,
)


def _missing(path: str) -> JsonArtifactReadResult:
    return JsonArtifactReadResult(
        path=path,
        exists=False,
        decode_ok=None,
        parse_ok=None,
        value=None,
        error_phase=None,
        error_type=None,
        error_message=None,
    )


def _present(path: str, value: Any) -> JsonArtifactReadResult:
    return JsonArtifactReadResult(
        path=path,
        exists=True,
        decode_ok=True,
        parse_ok=True,
        value=value,
        error_phase=None,
        error_type=None,
        error_message=None,
    )


def _unreadable(path: str) -> JsonArtifactReadResult:
    return JsonArtifactReadResult(
        path=path,
        exists=True,
        decode_ok=True,
        parse_ok=False,
        value=None,
        error_phase="parse",
        error_type="JSONDecodeError",
        error_message="Expecting value",
        error_line=1,
        error_column=1,
    )


def _classify(
    *,
    report: JsonArtifactReadResult | None = None,
    error: JsonArtifactReadResult | None = None,
    validation: JsonArtifactReadResult | None = None,
    run_meta: JsonArtifactReadResult | None = None,
) -> str:
    return classify_run_lifecycle(
        report_read=report or _missing("report.json"),
        error_read=error or _missing("error.json"),
        report_validation_errors_read=validation
        or _missing("report_validation_errors.json"),
        run_meta_read=run_meta,
    ).status


def test_classify_run_lifecycle_matrix() -> None:
    assert _classify(
        report=_present("report.json", {"schema_version": 1}),
    ) == "ok"
    assert _classify(
        error=_present("error.json", {"type": "AgentExecFailed"}),
    ) == "error"
    assert _classify(
        validation=_present("report_validation_errors.json", ["bad report"]),
    ) == "report_validation_error"
    assert _classify(
        report=_unreadable("report.json"),
    ) == "terminal_artifact_unreadable"
    assert _classify(
        run_meta=_present("run_meta.json", {"run_finished_utc": "2026-01-01T00:00:00Z"}),
    ) == "missing_report"
    assert _classify() == "nonterminal"
    assert _classify(
        run_meta=_present("run_meta.json", {"run_started_utc": "2026-01-01T00:00:00Z"}),
    ) == "nonterminal"


def test_classify_run_lifecycle_precedence_keeps_terminal_diagnostics_loud() -> None:
    assert _classify(
        report=_present("report.json", {"schema_version": 1}),
        error=_present("error.json", {"type": "AgentExecFailed"}),
    ) == "error"
    assert _classify(
        report=_present("report.json", {"schema_version": 1}),
        error=_unreadable("error.json"),
    ) == "terminal_artifact_unreadable"


def test_classify_run_lifecycle_verification_error_json_precedes_report_validation() -> None:
    classification = classify_run_lifecycle(
        report_read=_present(
            "report.json",
            {"schema_version": 1, "kind": "task_run_v1", "status": "success"},
        ),
        error_read=_present(
            "error.json",
            {
                "type": "VerificationFailed",
                "code": "verification_failed",
                "failure_phase": "verification",
                "exit_code": 1,
            },
        ),
        report_validation_errors_read=_present(
            "report_validation_errors.json",
            ["defensive stale validation artifact"],
        ),
    )

    assert classification.status == "error"
    assert classification.reason == "error_json_present"


def test_classify_history_record_lifecycle_uses_same_contract() -> None:
    record = {
        "status": "missing_report",
        "report": None,
        "error": None,
        "report_validation_errors": None,
        "run_meta": {"run_started_utc": "2026-01-01T00:00:00Z"},
        "terminal_artifact_reads": {
            "report.json": {"path": "report.json", "exists": False},
            "error.json": {"path": "error.json", "exists": False},
            "report_validation_errors.json": {
                "path": "report_validation_errors.json",
                "exists": False,
            },
        },
    }

    classification = classify_history_record_lifecycle(record)

    assert classification.status == "nonterminal"
    assert classification.reason == "no_terminal_artifact_without_completion_marker"
