from __future__ import annotations

from pathlib import Path

from runner_core.runner import _sanitize_agent_stderr_file


def test_sanitize_agent_stderr_file_strips_gemini_credential_line(tmp_path: Path) -> None:
    path = tmp_path / "agent_stderr.txt"
    path.write_text("Loaded cached credentials.\nSomething else.\n", encoding="utf-8")

    _sanitize_agent_stderr_file(agent="gemini", path=path)

    text = path.read_text(encoding="utf-8")
    assert "Loaded cached credentials." not in text
    assert "Something else." in text


def test_sanitize_agent_stderr_file_strips_gemini_zero_hook_registry_line(
    tmp_path: Path,
) -> None:
    path = tmp_path / "agent_stderr.txt"
    path.write_text(
        "Hook registry initialized with 0 hook entries.\nSomething else.\n",
        encoding="utf-8",
    )

    _sanitize_agent_stderr_file(agent="gemini", path=path)

    text = path.read_text(encoding="utf-8")
    assert "Hook registry initialized with 0 hook entries." not in text
    assert "Something else." in text


def test_sanitize_agent_stderr_file_strips_gemini_zero_hook_registry_line_without_period(
    tmp_path: Path,
) -> None:
    path = tmp_path / "agent_stderr.txt"
    path.write_text(
        "Hook registry initialized with 0 hook entries\nSomething else.\n",
        encoding="utf-8",
    )

    _sanitize_agent_stderr_file(agent="gemini", path=path)

    text = path.read_text(encoding="utf-8")
    assert "Hook registry initialized with 0 hook entries" not in text
    assert "Something else." in text


def test_sanitize_agent_stderr_file_dedupes_codex_personality_warning(tmp_path: Path) -> None:
    path = tmp_path / "agent_stderr.txt"
    warning = (
        "2026-02-11T07:26:19.697569Z  WARN codex_protocol::openai_models: "
        "Model personality requested but model_messages is missing, falling back to base "
        "instructions. model=gpt-5.2 personality=pragmatic"
    )
    path.write_text(
        "\n".join(
            [
                "before",
                warning,
                "after",
                "",
            ]
        ),
        encoding="utf-8",
    )

    _sanitize_agent_stderr_file(agent="codex", path=path)

    text = path.read_text(encoding="utf-8")
    assert "Model personality requested but model_messages is missing" in text
    assert "before" in text
    assert "after" in text


def test_sanitize_agent_stderr_file_summarizes_known_codex_capability_warnings(
    tmp_path: Path,
) -> None:
    path = tmp_path / "agent_stderr.txt"
    shell_snapshot = (
        "2026-02-18T00:00:00Z WARN codex_core::shell_snapshot: "
        "Shell snapshot not supported yet for PowerShell"
    )
    turn_metadata = (
        "2026-02-18T00:00:01Z WARN codex_core::turn_metadata: "
        "timed out after 250ms while building turn metadata header"
    )
    model_refresh = (
        "2026-02-19T00:36:28.774151Z ERROR codex_core::models_manager::manager: "
        "failed to refresh available models: timeout waiting for child process to exit"
    )
    path.write_text(
        "\n".join(
            [
                "before",
                shell_snapshot,
                shell_snapshot,
                turn_metadata,
                model_refresh,
                "after",
                "",
            ]
        ),
        encoding="utf-8",
    )

    _sanitize_agent_stderr_file(agent="codex", path=path)

    text = path.read_text(encoding="utf-8")
    assert "before" in text
    assert "after" in text
    assert "Shell snapshot not supported yet for PowerShell" not in text
    assert "code=shell_snapshot_powershell_unsupported" in text
    assert "occurrences=2" in text
    assert "code=turn_metadata_header_timeout" in text
    assert "failed to refresh available models" not in text
    assert "code=codex_model_refresh_timeout" in text


def test_sanitize_agent_stderr_file_dedupes_claude_missing_config_warning(tmp_path: Path) -> None:
    path = tmp_path / "agent_stderr.txt"
    block = "\n".join(
        [
            "Claude configuration file not found at: /root/.claude.json",
            "A backup file exists at: /root/.claude/backups/.claude.json.backup.1771509292967",
            'You can manually restore it by running: cp "/root/.claude/backups/.claude.json.backup.1771509292967" "/root/.claude.json"',
        ]
    )
    slow_warning = (
        '{"level":"warn","message":"[BashTool] Pre-flight check is taking longer than expected."}'
    )
    path.write_text(
        "\n\n".join([block, block, block, slow_warning]) + "\n",
        encoding="utf-8",
    )

    _sanitize_agent_stderr_file(agent="claude", path=path)

    text = path.read_text(encoding="utf-8")
    assert text.count("Claude configuration file not found at:") == 1
    assert "code=claude_config_missing" in text
    assert "occurrences=3" in text
    assert slow_warning in text


def test_sanitize_agent_stderr_file_appends_hint_for_nested_claude_sessions(tmp_path: Path) -> None:
    path = tmp_path / "agent_stderr.txt"
    path.write_text(
        "Error: Claude Code cannot be launched inside another Claude Code session.\n",
        encoding="utf-8",
    )

    _sanitize_agent_stderr_file(agent="claude", path=path)

    text = path.read_text(encoding="utf-8")
    assert "code=claude_nested_session" in text
    assert "hint=Claude Code cannot be launched inside another Claude Code session" in text


def test_sanitize_agent_stderr_file_appends_hint_for_gemini_invalid_regex(tmp_path: Path) -> None:
    path = tmp_path / "agent_stderr.txt"
    path.write_text(
        (
            "Error executing tool grep_search: Invalid regular expression pattern provided: "
            "parser_batch.add_argument(\"--mission-id\". Error: Invalid regular expression: "
            "/parser_batch.add_argument(\"--mission-id\"/: Unterminated group\n"
        ),
        encoding="utf-8",
    )

    _sanitize_agent_stderr_file(agent="gemini", path=path)

    text = path.read_text(encoding="utf-8")
    assert "Error executing tool grep_search" in text
    assert "tool=grep_search" in text
    assert "code=invalid_regex" in text
    assert "hint=Gemini grep_search patterns are regular expressions" in text


def test_sanitize_agent_stderr_file_appends_hint_for_gemini_replace_not_found(tmp_path: Path) -> None:
    path = tmp_path / "agent_stderr.txt"
    path.write_text(
        "Error executing tool replace: Error: Failed to edit, could not find the string to replace.\n",
        encoding="utf-8",
    )

    _sanitize_agent_stderr_file(agent="gemini", path=path)

    text = path.read_text(encoding="utf-8")
    assert "Error executing tool replace" in text
    assert "tool=replace" in text
    assert "code=string_not_found" in text
    assert "hint=Gemini replace requires an exact match" in text
