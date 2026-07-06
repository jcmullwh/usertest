from __future__ import annotations

from dataclasses import dataclass
from typing import Any

STATUS_OK = "ok"
STATUS_ERROR = "error"
STATUS_MISSING_REPORT = "missing_report"
STATUS_NONTERMINAL = "nonterminal"
STATUS_LEGACY_NO_TERMINAL_ARTIFACT = "no_terminal_artifact"
STATUS_REPORT_VALIDATION_ERROR = "report_validation_error"
STATUS_TERMINAL_ARTIFACT_UNREADABLE = "terminal_artifact_unreadable"

TERMINAL_ARTIFACT_NAMES = (
    "report.json",
    "error.json",
    "report_validation_errors.json",
)


@dataclass(frozen=True)
class JsonArtifactReadResult:
    path: str
    exists: bool
    decode_ok: bool | None
    parse_ok: bool | None
    value: Any | None
    error_phase: str | None
    error_type: str | None
    error_message: str | None
    error_line: int | None = None
    error_column: int | None = None


@dataclass(frozen=True)
class RunLifecycleClassification:
    status: str
    terminal_artifact_reads: dict[str, dict[str, Any]]
    completion_signal: str
    reason: str


def artifact_read_details(result: JsonArtifactReadResult) -> dict[str, Any]:
    details: dict[str, Any] = {
        "path": result.path,
        "exists": result.exists,
        "decode_ok": result.decode_ok,
        "parse_ok": result.parse_ok,
        "error_phase": result.error_phase,
        "error_type": result.error_type,
        "error_message": result.error_message,
    }
    if result.error_line is not None:
        details["error_line"] = result.error_line
    if result.error_column is not None:
        details["error_column"] = result.error_column
    return details


def terminal_artifact_reads(
    *,
    report: JsonArtifactReadResult,
    error: JsonArtifactReadResult,
    report_validation_errors: JsonArtifactReadResult,
) -> dict[str, dict[str, Any]]:
    return {
        "report.json": artifact_read_details(report),
        "error.json": artifact_read_details(error),
        "report_validation_errors.json": artifact_read_details(report_validation_errors),
    }


def _has_artifact_read_failure(result: JsonArtifactReadResult) -> bool:
    return result.exists and (result.decode_ok is False or result.parse_ok is False)


def _completion_signal_from_run_meta(run_meta_read: JsonArtifactReadResult | None) -> str:
    if run_meta_read is None or not run_meta_read.exists:
        return "run_meta_absent"
    if _has_artifact_read_failure(run_meta_read):
        return "run_meta_unreadable"
    run_meta = run_meta_read.value
    if not isinstance(run_meta, dict):
        return "run_meta_not_object"
    finished_utc = run_meta.get("run_finished_utc")
    if isinstance(finished_utc, str) and finished_utc.strip():
        return "run_meta_finished"
    return "run_meta_unfinished"


def _is_completed_signal(signal: str) -> bool:
    return signal == "run_meta_finished"


def classify_run_lifecycle(
    *,
    report_read: JsonArtifactReadResult,
    error_read: JsonArtifactReadResult,
    report_validation_errors_read: JsonArtifactReadResult,
    run_meta_read: JsonArtifactReadResult | None = None,
) -> RunLifecycleClassification:
    """
    Classify a run directory from its durable artifact inventory.

    Contract:
    - terminal JSON artifacts are `report.json`, `error.json`, and
      `report_validation_errors.json`;
    - unreadable terminal artifacts win so diagnostics stay loud;
    - terminal error and validation artifacts win over a successful report;
    - `report.json` is terminal success when no higher-precedence terminal artifact exists;
    - if no terminal artifact exists, `run_meta.json["run_finished_utc"]` is the durable
      completion marker used to distinguish completed-but-missing-report from a seed-only or
      interrupted nonterminal directory.

    Older directories without `run_meta.run_finished_utc` and without terminal artifacts are
    intentionally classified as `nonterminal` rather than `missing_report`: the existing artifact
    surface does not prove they completed and then lost `report.json`.
    """

    reads = terminal_artifact_reads(
        report=report_read,
        error=error_read,
        report_validation_errors=report_validation_errors_read,
    )
    completion_signal = _completion_signal_from_run_meta(run_meta_read)

    if any(
        _has_artifact_read_failure(result)
        for result in (report_read, error_read, report_validation_errors_read)
    ):
        return RunLifecycleClassification(
            status=STATUS_TERMINAL_ARTIFACT_UNREADABLE,
            terminal_artifact_reads=reads,
            completion_signal=completion_signal,
            reason="terminal_artifact_unreadable",
        )
    if isinstance(error_read.value, dict):
        return RunLifecycleClassification(
            status=STATUS_ERROR,
            terminal_artifact_reads=reads,
            completion_signal=completion_signal,
            reason="error_json_present",
        )
    if report_validation_errors_read.value is not None:
        return RunLifecycleClassification(
            status=STATUS_REPORT_VALIDATION_ERROR,
            terminal_artifact_reads=reads,
            completion_signal=completion_signal,
            reason="report_validation_errors_json_present",
        )
    if report_read.value is not None:
        return RunLifecycleClassification(
            status=STATUS_OK,
            terminal_artifact_reads=reads,
            completion_signal=completion_signal,
            reason="report_json_present",
        )
    if _is_completed_signal(completion_signal):
        return RunLifecycleClassification(
            status=STATUS_MISSING_REPORT,
            terminal_artifact_reads=reads,
            completion_signal=completion_signal,
            reason="completed_run_missing_terminal_report",
        )
    return RunLifecycleClassification(
        status=STATUS_NONTERMINAL,
        terminal_artifact_reads=reads,
        completion_signal=completion_signal,
        reason="no_terminal_artifact_without_completion_marker",
    )


def _bool_or_none(raw: Any) -> bool | None:
    return raw if isinstance(raw, bool) else None


def _read_result_from_record(
    *,
    path: str,
    value: Any,
    details: dict[str, Any] | None,
) -> JsonArtifactReadResult:
    if isinstance(details, dict):
        exists = details.get("exists")
        decode_ok = _bool_or_none(details.get("decode_ok"))
        parse_ok = _bool_or_none(details.get("parse_ok"))
        return JsonArtifactReadResult(
            path=str(details.get("path") or path),
            exists=exists if isinstance(exists, bool) else value is not None,
            decode_ok=decode_ok,
            parse_ok=parse_ok,
            value=value if parse_ok is not False and decode_ok is not False else None,
            error_phase=details.get("error_phase")
            if isinstance(details.get("error_phase"), str)
            else None,
            error_type=details.get("error_type")
            if isinstance(details.get("error_type"), str)
            else None,
            error_message=details.get("error_message")
            if isinstance(details.get("error_message"), str)
            else None,
            error_line=details.get("error_line")
            if isinstance(details.get("error_line"), int)
            else None,
            error_column=details.get("error_column")
            if isinstance(details.get("error_column"), int)
            else None,
        )
    exists = value is not None
    return JsonArtifactReadResult(
        path=path,
        exists=exists,
        decode_ok=True if exists else None,
        parse_ok=True if exists else None,
        value=value,
        error_phase=None,
        error_type=None,
        error_message=None,
    )


def classify_history_record_lifecycle(record: dict[str, Any]) -> RunLifecycleClassification:
    """Reclassify a serialized history record through the same lifecycle contract."""

    if not any(
        key in record
        for key in (
            "report",
            "error",
            "report_validation_errors",
            "run_meta",
            "terminal_artifact_reads",
        )
    ):
        status = record.get("status")
        status_s = status.strip() if isinstance(status, str) and status.strip() else "unknown"
        return RunLifecycleClassification(
            status=status_s,
            terminal_artifact_reads={},
            completion_signal="record_artifact_inventory_absent",
            reason="record_artifact_inventory_absent",
        )

    terminal_reads = record.get("terminal_artifact_reads")
    terminal_reads_dict = terminal_reads if isinstance(terminal_reads, dict) else {}

    report_read = _read_result_from_record(
        path="report.json",
        value=record.get("report"),
        details=terminal_reads_dict.get("report.json")
        if isinstance(terminal_reads_dict.get("report.json"), dict)
        else None,
    )
    error_read = _read_result_from_record(
        path="error.json",
        value=record.get("error"),
        details=terminal_reads_dict.get("error.json")
        if isinstance(terminal_reads_dict.get("error.json"), dict)
        else None,
    )
    report_validation_errors_read = _read_result_from_record(
        path="report_validation_errors.json",
        value=record.get("report_validation_errors"),
        details=terminal_reads_dict.get("report_validation_errors.json")
        if isinstance(terminal_reads_dict.get("report_validation_errors.json"), dict)
        else None,
    )
    run_meta = record.get("run_meta")
    run_meta_read = _read_result_from_record(
        path="run_meta.json",
        value=run_meta,
        details=None,
    )
    return classify_run_lifecycle(
        report_read=report_read,
        error_read=error_read,
        report_validation_errors_read=report_validation_errors_read,
        run_meta_read=run_meta_read,
    )
