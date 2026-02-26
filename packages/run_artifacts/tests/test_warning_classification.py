from __future__ import annotations

from run_artifacts.run_failure_event import classify_known_stderr_warnings


def test_classify_known_stderr_warnings_detects_warning_only_payload() -> None:
    text = "\n".join(
        [
            (
                "[codex_notice_summary] "
                "code=shell_snapshot_powershell_unsupported "
                "occurrences=3 classification=capability_notice"
            ),
            (
                "note=PowerShell shell snapshot metadata isn't available yet; continuing without it."
            ),
        ]
    )
    meta = classify_known_stderr_warnings(text)
    assert meta["warning_only"] is True
    assert meta["codes"] == ["shell_snapshot_powershell_unsupported"]
    assert meta["unknown_lines"] == []


def test_classify_known_stderr_warnings_marks_mixed_payload_as_not_warning_only() -> None:
    text = "\n".join(
        [
            (
                "[codex_warning_summary] "
                "code=turn_metadata_header_timeout "
                "occurrences=2 classification=capability_notice"
            ),
            "real error line",
        ]
    )
    meta = classify_known_stderr_warnings(text)
    assert meta["warning_only"] is False
    assert meta["codes"] == ["turn_metadata_header_timeout"]
    assert meta["unknown_lines"] == ["real error line"]


def test_classify_known_stderr_warnings_detects_codex_model_refresh_timeout_warning_only() -> None:
    text = (
        "2026-02-19T00:36:28.774151Z ERROR codex_core::models_manager::manager: "
        "failed to refresh available models: timeout waiting for child process to exit"
    )
    meta = classify_known_stderr_warnings(text)
    assert meta["warning_only"] is True
    assert meta["codes"] == ["codex_model_refresh_timeout"]
    assert meta["unknown_lines"] == []


def test_classify_known_stderr_warnings_detects_claude_bashtool_preflight_slow_warning_only(
) -> None:
    line = (
        '{"level":"warn","message":"[BashTool] Pre-flight check is taking longer than expected. '
        'Run with ANTHROPIC_LOG=debug to check for failed or slow API requests."}'
    )
    text = "\n".join(
        [
            line,
            line,
        ]
    )
    meta = classify_known_stderr_warnings(text)
    assert meta["warning_only"] is True
    assert meta["codes"] == ["bash_tool_preflight_slow"]
    assert meta["counts"]["bash_tool_preflight_slow"] == 2
    assert meta["unknown_lines"] == []
