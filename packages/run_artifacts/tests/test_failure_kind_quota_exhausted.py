from run_artifacts import classify_failure_kind, render_failure_text


def test_classify_failure_kind_quota_exhausted_for_agent_quota_exceeded() -> None:
    is_failure, kind = classify_failure_kind(
        status="error",
        error={
            "type": "AgentQuotaExceeded",
            "code": "claude_out_of_extra_usage",
            "provider": "claude",
            "reset_time": {"raw": "Feb 24, 8pm", "timezone": "America/New_York"},
        },
        validation_errors=[],
    )
    assert is_failure is True
    assert kind == "quota_exhausted"


def test_classify_failure_kind_quota_exhausted_for_provider_quota_subtype() -> None:
    is_failure, kind = classify_failure_kind(
        status="error",
        error={
            "type": "AgentExecFailed",
            "subtype": "provider_quota_exceeded",
        },
        validation_errors=[],
    )
    assert is_failure is True
    assert kind == "quota_exhausted"


def test_classify_failure_kind_treats_unreadable_terminal_artifact_as_failure() -> None:
    is_failure, kind = classify_failure_kind(
        status="terminal_artifact_unreadable",
        error=None,
        validation_errors=[],
    )
    assert is_failure is True
    assert kind == "terminal_artifact_unreadable"


def test_classify_failure_kind_treats_missing_report_as_failure() -> None:
    is_failure, kind = classify_failure_kind(
        status="missing_report",
        error=None,
        validation_errors=[],
    )
    assert is_failure is True
    assert kind == "missing_report"


def test_classify_failure_kind_normalizes_legacy_no_terminal_artifact_status() -> None:
    is_failure, kind = classify_failure_kind(
        status="no_terminal_artifact",
        error=None,
        validation_errors=[],
    )
    assert is_failure is True
    assert kind == "missing_report"


def test_render_failure_text_includes_terminal_artifact_read_diagnostics() -> None:
    text = render_failure_text(
        failure_kind="terminal_artifact_unreadable",
        agent="codex",
        status="terminal_artifact_unreadable",
        error=None,
        report_validation_errors=[],
        artifacts=None,
        terminal_artifact_reads={
            "report.json": {
                "exists": True,
                "error_phase": "parse",
                "error_type": "JSONDecodeError",
                "error_message": "Expecting property name enclosed in double quotes",
                "error_line": 1,
                "error_column": 2,
            }
        },
    )

    assert "terminal_artifact_reads:" in text
    assert "report.json: parse JSONDecodeError" in text
    assert "line 1, column 2" in text


def test_render_failure_text_preserves_captured_head_and_tail_for_mining() -> None:
    head_marker = "HEAD-ROOT-CAUSE"
    tail_marker = "TAIL-FINAL-DIAGNOSTIC"
    text = render_failure_text(
        failure_kind="error",
        agent="codex",
        status="error",
        error=None,
        report_validation_errors=[],
        artifacts=None,
        attachments=[
            {
                "path": "agent_stderr.txt",
                "excerpt_head": head_marker + ("h" * 4_000),
                "excerpt_tail": ("t" * 4_000) + tail_marker,
                "truncated": True,
            }
        ],
    )

    assert head_marker in text
    assert tail_marker in text
    assert "middle omitted" in text
