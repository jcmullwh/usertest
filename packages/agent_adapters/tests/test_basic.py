from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_adapters import normalize_codex_events
from agent_adapters.codex_normalize import _map_sandbox_path_str, _resolve_candidate_path
from agent_adapters.events import iter_events_jsonl


def test_normalize_codex_events_handles_non_json_lines(tmp_path: Path) -> None:
    raw = tmp_path / "raw.jsonl"
    raw.write_text(
        "not json\n"
        + json.dumps({"id": "1", "msg": {"type": "agent_message", "message": "hi"}})
        + "\n"
        + json.dumps(
            {
                "id": "1",
                "msg": {
                    "type": "exec_command_end",
                    "command": ["find", "/n", "/v", "", "USERS.md"],
                    "exit_code": 0,
                    "cwd": str(tmp_path),
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (tmp_path / "USERS.md").write_text("# Users\n", encoding="utf-8")

    normalized = tmp_path / "normalized.jsonl"
    normalize_codex_events(
        raw_events_path=raw,
        normalized_events_path=normalized,
        workspace_root=tmp_path,
    )

    events = list(iter_events_jsonl(normalized))
    assert any(e["type"] == "error" for e in events)
    assert any(e["type"] == "agent_message" for e in events)
    assert any(e["type"] == "run_command" for e in events)
    assert not any(e["type"] == "read_file" for e in events)


def test_normalize_codex_events_joins_begin_end(tmp_path: Path) -> None:
    raw = tmp_path / "raw.jsonl"
    raw.write_text(
        json.dumps(
            {
                "id": "1",
                "msg": {
                    "type": "exec_command_begin",
                    "call_id": "call_1",
                    "command": ["type", "USERS.md"],
                    "cwd": str(tmp_path),
                },
            }
        )
        + "\n"
        + json.dumps(
            {
                "id": "1",
                "msg": {
                    "type": "exec_command_end",
                    "call_id": "call_1",
                    "stdout": "# Users\n",
                    "stderr": "",
                    "exit_code": 0,
                    "duration": {"secs": 0, "nanos": 1},
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (tmp_path / "USERS.md").write_text("# Users\n", encoding="utf-8")

    normalized = tmp_path / "normalized.jsonl"
    normalize_codex_events(
        raw_events_path=raw,
        normalized_events_path=normalized,
        workspace_root=tmp_path,
    )

    events = list(iter_events_jsonl(normalized))
    assert any(e["type"] == "run_command" for e in events)
    assert any(e["type"] == "read_file" for e in events)


def test_normalize_codex_events_writes_failure_artifacts(tmp_path: Path) -> None:
    raw = tmp_path / "raw.jsonl"
    raw.write_text(
        json.dumps(
            {
                "id": "1",
                "msg": {
                    "type": "exec_command_begin",
                    "call_id": "call_1",
                    "command": ["rg", "nope", "USERS.md"],
                    "cwd": str(tmp_path),
                },
            }
        )
        + "\n"
        + json.dumps(
            {
                "id": "1",
                "msg": {
                    "type": "exec_command_end",
                    "call_id": "call_1",
                    "stdout": "",
                    "stderr": "no matches\n",
                    "exit_code": 2,
                    "duration": {"secs": 0, "nanos": 2},
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    normalized = tmp_path / "normalized.jsonl"
    normalize_codex_events(
        raw_events_path=raw,
        normalized_events_path=normalized,
        workspace_root=tmp_path,
    )

    events = list(iter_events_jsonl(normalized))
    cmd = next(e for e in events if e["type"] == "run_command")
    artifacts = cmd.get("data", {}).get("failure_artifacts")
    assert isinstance(artifacts, dict)
    assert artifacts.get("stdout") == "command_failures/cmd_01/stdout.txt"
    assert artifacts.get("stderr") == "command_failures/cmd_01/stderr.txt"
    assert (tmp_path / "command_failures" / "cmd_01" / "stderr.txt").read_text(
        encoding="utf-8"
    ).strip() == "no matches"


def test_normalize_codex_events_maps_workspace_mount_paths(tmp_path: Path) -> None:
    raw = tmp_path / "raw.jsonl"
    raw.write_text(
        json.dumps(
            {
                "id": "1",
                "msg": {
                    "type": "exec_command_end",
                    "command": ["cat", "/workspace/USERS.md"],
                    "exit_code": 0,
                    "cwd": "/workspace",
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (tmp_path / "USERS.md").write_text("# Users\n", encoding="utf-8")

    normalized = tmp_path / "normalized.jsonl"
    normalize_codex_events(
        raw_events_path=raw,
        normalized_events_path=normalized,
        workspace_root=tmp_path,
        workspace_mount="/workspace",
    )

    events = list(iter_events_jsonl(normalized))
    read_paths = [e.get("data", {}).get("path") for e in events if e["type"] == "read_file"]
    assert "USERS.md" in read_paths


def test_normalize_codex_events_handles_responses_style_items(tmp_path: Path) -> None:
    raw = tmp_path / "raw.jsonl"
    raw.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {"id": "item_0", "type": "reasoning", "text": "thinking"},
                    }
                ),
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {
                            "id": "item_1",
                            "type": "command_execution",
                            "command": "/bin/bash -lc 'cat /workspace/USERS.md'",
                            "exit_code": 0,
                            "status": "completed",
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {"id": "item_2", "type": "agent_message", "text": "done"},
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (tmp_path / "USERS.md").write_text("# Users\n", encoding="utf-8")

    normalized = tmp_path / "normalized.jsonl"
    normalize_codex_events(
        raw_events_path=raw,
        normalized_events_path=normalized,
        workspace_root=tmp_path,
        workspace_mount="/workspace",
    )

    events = list(iter_events_jsonl(normalized))
    assert any(e["type"] == "run_command" for e in events)
    assert any(e["type"] == "agent_message" for e in events)
    read_paths = [e.get("data", {}).get("path") for e in events if e["type"] == "read_file"]
    assert "USERS.md" in read_paths


def test_normalize_codex_events_does_not_attest_chained_partial_read(tmp_path: Path) -> None:
    raw = tmp_path / "raw.jsonl"
    raw.write_text(
        json.dumps(
            {
                "id": "1",
                "msg": {
                    "type": "exec_command_end",
                    "command": ["cd", "/workspace", "&&", "sed", "-n", "1,20p", "README.md"],
                    "exit_code": 0,
                    "cwd": "/",
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (tmp_path / "README.md").write_text("# Hello\n", encoding="utf-8")

    normalized = tmp_path / "normalized.jsonl"
    normalize_codex_events(
        raw_events_path=raw,
        normalized_events_path=normalized,
        workspace_root=tmp_path,
        workspace_mount="/workspace",
    )

    events = list(iter_events_jsonl(normalized))
    read_paths = [e.get("data", {}).get("path") for e in events if e["type"] == "read_file"]
    assert "README.md" not in read_paths


def test_codex_search_and_empty_head_output_cannot_attest_source_reads(tmp_path: Path) -> None:
    source = tmp_path / "src.py"
    source.write_text("def mechanism():\n    return True\n", encoding="utf-8")
    raw = tmp_path / "raw.jsonl"
    raw.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "id": "rg",
                        "msg": {
                            "type": "exec_command_end",
                            "command": ["rg", "-l", "mechanism", "src.py"],
                            "exit_code": 0,
                            "cwd": str(tmp_path),
                            "stdout": "src.py\n",
                        },
                    }
                ),
                json.dumps(
                    {
                        "id": "head",
                        "msg": {
                            "type": "exec_command_end",
                            "command": ["head", "-n", "0", "src.py"],
                            "exit_code": 0,
                            "cwd": str(tmp_path),
                            "stdout": "unrelated output\n",
                        },
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    normalized = tmp_path / "normalized.jsonl"
    normalize_codex_events(
        raw_events_path=raw,
        normalized_events_path=normalized,
        workspace_root=tmp_path,
    )

    events = list(iter_events_jsonl(normalized))
    assert not any(event["type"] == "read_file" for event in events)


def test_codex_exact_cat_output_attests_whole_file(tmp_path: Path) -> None:
    source = tmp_path / "src.py"
    content = "def mechanism():\n    return True\n"
    source.write_text(content, encoding="utf-8")
    raw = tmp_path / "raw.jsonl"
    raw.write_text(
        json.dumps(
            {
                "id": "cat",
                "msg": {
                    "type": "exec_command_end",
                    "command": ["cat", "src.py"],
                    "exit_code": 0,
                    "cwd": str(tmp_path),
                    "stdout": content,
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    normalized = tmp_path / "normalized.jsonl"

    normalize_codex_events(
        raw_events_path=raw,
        normalized_events_path=normalized,
        workspace_root=tmp_path,
    )

    read = next(event for event in iter_events_jsonl(normalized) if event["type"] == "read_file")
    assert read["data"]["content_observed"] is True
    assert read["data"]["whole_file_observed"] is True
    assert read["data"]["observed_content"] == content


@pytest.mark.parametrize(
    "content",
    [
        "# Atom Chunk 001\n\nObserved smart quote ’ and arrow → evidence.",
        "# Atom Chunk 001\n\nObserved smart quote ’ and arrow → evidence.\n",
    ],
)
def test_codex_current_windows_powershell_event_attests_aggregated_output(
    tmp_path: Path,
    content: str,
) -> None:
    source = tmp_path / "atoms_text" / "atoms_001.md"
    source.parent.mkdir()
    source.write_text(content, encoding="utf-8")
    raw = tmp_path / "raw.jsonl"
    command = (
        '"C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe" '
        "-Command 'Get-Content -Raw -Encoding UTF8 -LiteralPath atoms_text/atoms_001.md'"
    )
    raw.write_text(
        json.dumps(
            {
                "type": "item.completed",
                "item": {
                    "id": "item_1",
                    "type": "command_execution",
                    "command": command,
                    "aggregated_output": content.replace("\n", "\r\n") + "\r\n",
                    "exit_code": 0,
                    "status": "completed",
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    normalized = tmp_path / "normalized.jsonl"

    normalize_codex_events(
        raw_events_path=raw,
        normalized_events_path=normalized,
        workspace_root=tmp_path,
    )

    events = list(iter_events_jsonl(normalized))
    command_event = next(event for event in events if event["type"] == "run_command")
    assert command_event["data"]["argv"] == [
        "Get-Content",
        "-Raw",
        "-Encoding",
        "UTF8",
        "-LiteralPath",
        "atoms_text/atoms_001.md",
    ]
    read = next(event for event in events if event["type"] == "read_file")
    assert read["data"]["path"] == "atoms_text/atoms_001.md"
    assert read["data"]["content_observed"] is True
    assert read["data"]["whole_file_observed"] is True
    assert read["data"]["observed_content"] == content
    assert read["data"]["transport_normalization"] == "single_terminal_newline"


def test_codex_windows_powershell_noprofile_event_attests_relative_backslash_read(
    tmp_path: Path,
) -> None:
    source = tmp_path / "atoms_text" / "atoms_001.md"
    source.parent.mkdir()
    content = "# Atom Chunk 001\n\nObserved smart quote ’ and arrow → evidence."
    source.write_text(content, encoding="utf-8")
    raw = tmp_path / "raw.jsonl"
    command = (
        '"C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe" '
        '-NoProfile -Command "Get-Content -Raw -Encoding UTF8 '
        '-LiteralPath .\\atoms_text\\atoms_001.md"'
    )
    raw.write_text(
        json.dumps(
            {
                "type": "item.completed",
                "item": {
                    "id": "item_1",
                    "type": "command_execution",
                    "command": command,
                    "aggregated_output": content.replace("\n", "\r\n") + "\r\n",
                    "exit_code": 0,
                    "status": "completed",
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    normalized = tmp_path / "normalized.jsonl"

    normalize_codex_events(
        raw_events_path=raw,
        normalized_events_path=normalized,
        workspace_root=tmp_path,
    )

    events = list(iter_events_jsonl(normalized))
    command_event = next(event for event in events if event["type"] == "run_command")
    assert command_event["data"]["argv"] == [
        "Get-Content",
        "-Raw",
        "-Encoding",
        "UTF8",
        "-LiteralPath",
        ".\\atoms_text\\atoms_001.md",
    ]
    read = next(event for event in events if event["type"] == "read_file")
    assert read["data"]["path"] == "atoms_text/atoms_001.md"
    assert read["data"]["content_observed"] is True
    assert read["data"]["whole_file_observed"] is True
    assert read["data"]["observed_content"] == content
    assert read["data"]["transport_normalization"] == "single_terminal_newline"


@pytest.mark.parametrize("extra_output", [False, True])
def test_codex_powershell_exact_line_range_attests_only_exact_output(
    tmp_path: Path,
    extra_output: bool,
) -> None:
    source = tmp_path / "packages" / "runner.py"
    source.parent.mkdir()
    lines = [f"line_{index:04d} = {index}\n" for index in range(1, 601)]
    source.write_text("".join(lines), encoding="utf-8")
    selected = "".join(lines[399:439])
    raw = tmp_path / "raw.jsonl"
    command = (
        '"C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe" '
        '-Command "Get-Content -Encoding UTF8 -LiteralPath packages/runner.py '
        '| Select-Object -Skip 399 -First 40"'
    )
    output = selected + ("unrelated output\n" if extra_output else "")
    raw.write_text(
        json.dumps(
            {
                "type": "item.completed",
                "item": {
                    "id": "item_1",
                    "type": "command_execution",
                    "command": command,
                    "aggregated_output": output.replace("\n", "\r\n"),
                    "exit_code": 0,
                    "status": "completed",
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    normalized = tmp_path / "normalized.jsonl"

    normalize_codex_events(
        raw_events_path=raw,
        normalized_events_path=normalized,
        workspace_root=tmp_path,
    )

    read = next(event for event in iter_events_jsonl(normalized) if event["type"] == "read_file")
    assert read["data"]["path"] == "packages/runner.py"
    assert read["data"]["attestation_kind"] == "exact_line_range"
    assert read["data"]["requested_skip_lines"] == 399
    assert read["data"]["requested_first_lines"] == 40
    assert read["data"]["content_observed"] is (not extra_output)
    if not extra_output:
        assert read["data"]["whole_file_observed"] is False
        assert read["data"]["observed_content"] == selected
        assert read["data"]["observed_start_line"] == 400
        # Existing attestation semantics record the line boundary after a terminal newline.
        assert read["data"]["observed_end_line"] == 440


def test_codex_powershell_read_does_not_strip_additional_output(tmp_path: Path) -> None:
    source = tmp_path / "evidence.txt"
    content = "exact evidence without a terminal newline"
    source.write_text(content, encoding="utf-8")
    raw = tmp_path / "raw.jsonl"
    raw.write_text(
        json.dumps(
            {
                "type": "item.completed",
                "item": {
                    "id": "item_1",
                    "type": "command_execution",
                    "command": (
                        '"C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe" '
                        "-Command 'Get-Content -Raw -LiteralPath evidence.txt'"
                    ),
                    "aggregated_output": content + "\r\nunrelated output\r\n",
                    "exit_code": 0,
                    "status": "completed",
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    normalized = tmp_path / "normalized.jsonl"

    normalize_codex_events(
        raw_events_path=raw,
        normalized_events_path=normalized,
        workspace_root=tmp_path,
    )

    read = next(event for event in iter_events_jsonl(normalized) if event["type"] == "read_file")
    assert read["data"]["content_observed"] is False
    assert read["data"]["whole_file_observed"] is False


def test_map_sandbox_path_accepts_windows_posix_drive_form(tmp_path: Path) -> None:
    mapped = _map_sandbox_path_str(
        "/c/Users/example/project/file.py",
        workspace_root=tmp_path,
        workspace_mount=None,
    )
    assert mapped.as_posix() == "C:/Users/example/project/file.py"


def test_resolve_candidate_path_accepts_windows_posix_drive_form(tmp_path: Path) -> None:
    resolved = _resolve_candidate_path(
        "/d/tmp/example.txt",
        base_dir=tmp_path,
        workspace_root=tmp_path,
        workspace_mount=None,
    )
    assert resolved is not None
    assert resolved.as_posix() == "D:/tmp/example.txt"


def test_normalize_codex_events_uses_raw_ts_iter_for_per_line_timestamps(tmp_path: Path) -> None:
    raw = tmp_path / "raw.jsonl"
    raw.write_text(
        "\n".join(
            [
                json.dumps({"id": "1", "msg": {"type": "agent_message", "message": "hi"}}),
                json.dumps(
                    {
                        "id": "1",
                        "msg": {
                            "type": "exec_command_end",
                            "command": ["cat", "USERS.md"],
                            "exit_code": 0,
                            "cwd": str(tmp_path),
                        },
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (tmp_path / "USERS.md").write_text("# Users\n", encoding="utf-8")

    normalized = tmp_path / "normalized.jsonl"
    normalize_codex_events(
        raw_events_path=raw,
        normalized_events_path=normalized,
        raw_ts_iter=iter(
            [
                "2026-02-01T00:00:00+00:00",
                "2026-02-01T00:00:05+00:00",
            ]
        ),
        workspace_root=tmp_path,
    )

    events = list(iter_events_jsonl(normalized))
    assert [e.get("ts") for e in events] == [
        "2026-02-01T00:00:00+00:00",
        "2026-02-01T00:00:05+00:00",
        "2026-02-01T00:00:05+00:00",
    ]


def test_normalize_codex_events_emits_delegation_events(tmp_path: Path) -> None:
    raw = tmp_path / "raw.jsonl"
    raw.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "function_call",
                            "name": "mcp__multi_agent__spawn_agent",
                            "call_id": "call_agent",
                            "arguments": json.dumps(
                                {"task": "Inspect token monitoring tests", "agent": "codex"}
                            ),
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "function_call_output",
                            "call_id": "call_agent",
                            "output": (
                                "Summary: tests cover paths and risks. "
                                "Next steps: patch run_analysis."
                            ),
                            "usage": {"input_tokens": 1200, "output_tokens": 150},
                        },
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    normalized = tmp_path / "normalized.jsonl"
    normalize_codex_events(raw_events_path=raw, normalized_events_path=normalized)

    events = list(iter_events_jsonl(normalized))
    invocation = next(e for e in events if e["type"] == "delegation_invocation")
    result = next(e for e in events if e["type"] == "delegation_result")
    assert invocation["data"]["tool_name"] == "mcp__multi_agent__spawn_agent"
    assert invocation["data"]["prompt_chars"] > 0
    assert result["data"]["result_kind"] == "parent_context_summary"
    assert result["data"]["token_usage"]["total_tokens"] == 1350
