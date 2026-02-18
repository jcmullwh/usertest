from run_artifacts import (
    ArtifactRef,
    CaptureResult,
    TextCapturePolicy,
    TextExcerpt,
    classify_failure_kind,
    classify_known_stderr_warnings,
    coerce_validation_errors,
    extract_error_artifacts,
    iter_report_history,
    render_failure_text,
    sanitize_error,
    write_report_history_jsonl,
)


def test_package_surface() -> None:
    assert ArtifactRef is not None
    assert CaptureResult is not None
    assert TextCapturePolicy is not None
    assert TextExcerpt is not None
    assert classify_failure_kind is not None
    assert classify_known_stderr_warnings is not None
    assert coerce_validation_errors is not None
    assert extract_error_artifacts is not None
    assert iter_report_history is not None
    assert render_failure_text is not None
    assert sanitize_error is not None
    assert write_report_history_jsonl is not None
