from __future__ import annotations

from pathlib import Path

from runner_core.runner import (
    _augment_tool_file_not_found_diagnostics,
    _classify_failure_subtype,
)


def test_classify_failure_subtype_provider_capacity() -> None:
    text = "Attempt failed with status 429: RESOURCE_EXHAUSTED and MODEL_CAPACITY_EXHAUSTED."
    assert _classify_failure_subtype(text) == "provider_capacity"


def test_classify_failure_subtype_provider_capacity_claude_limit_message() -> None:
    text = "You've hit your limit · resets 4am (America/New_York)"
    assert _classify_failure_subtype(text) == "provider_capacity"


def test_classify_failure_subtype_permission_policy() -> None:
    text = "Tool execution denied by policy while waiting for interactive approval."
    assert _classify_failure_subtype(text) == "permission_policy"


def test_classify_failure_subtype_binary_missing() -> None:
    text = "Failed to launch Claude CLI process: command not found."
    assert _classify_failure_subtype(text) == "binary_or_command_missing"


def test_classify_failure_subtype_provider_auth() -> None:
    text = "HTTP 401 Unauthorized: Invalid API key."
    assert _classify_failure_subtype(text) == "provider_auth"


def test_classify_failure_subtype_invalid_agent_config() -> None:
    text = "invalid value for model_reasoning_effort: xhigh (expected enum low|medium|high)"
    assert _classify_failure_subtype(text) == "invalid_agent_config"


def test_classify_failure_subtype_disk_full() -> None:
    text = "Failed to write log: ENOSPC: no space left on device"
    assert _classify_failure_subtype(text) == "disk_full"


def test_augment_tool_file_not_found_diagnostics_includes_drive_letter_path() -> None:
    text = "Error executing tool read_file: File not found: C:\\Temp\\missing.py"
    augmented = _augment_tool_file_not_found_diagnostics(
        stderr_text=text,
        workspace_root=Path("I:/repo/workspace"),
    )
    assert "raw_path=C:\\Temp\\missing.py" in augmented
    assert "resolved_path=" in augmented
    assert "hint=On Windows, both /c/... and C:\\... are accepted" in augmented
