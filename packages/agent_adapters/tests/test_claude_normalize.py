from __future__ import annotations

import json
from pathlib import Path

from agent_adapters import normalize_claude_events
from agent_adapters.events import iter_events_jsonl


def test_normalize_claude_events_emits_tool_events(tmp_path: Path) -> None:
    (tmp_path / "USERS.md").write_text("# Users\n", encoding="utf-8")

    raw = tmp_path / "raw.jsonl"
    raw.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "assistant",
                        "message": {
                            "role": "assistant",
                            "content": [
                                {
                                    "type": "tool_use",
                                    "id": "tool_1",
                                    "name": "Bash",
                                    "input": {"command": "type USERS.md"},
                                }
                            ],
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "user",
                        "message": {
                            "role": "user",
                            "content": [
                                {
                                    "type": "tool_result",
                                    "tool_use_id": "tool_1",
                                    "content": "# Users\n",
                                    "is_error": False,
                                }
                            ],
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "assistant",
                        "message": {
                            "role": "assistant",
                            "content": [
                                {
                                    "type": "tool_use",
                                    "id": "tool_2",
                                    "name": "Read",
                                    "input": {"file_path": "USERS.md"},
                                }
                            ],
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "user",
                        "message": {
                            "role": "user",
                            "content": [
                                {
                                    "type": "tool_result",
                                    "tool_use_id": "tool_2",
                                    "content": "# Users\n",
                                    "is_error": False,
                                }
                            ],
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "assistant",
                        "message": {
                            "role": "assistant",
                            "content": [{"type": "text", "text": "ok"}],
                        },
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    normalized = tmp_path / "normalized.jsonl"
    normalize_claude_events(
        raw_events_path=raw,
        normalized_events_path=normalized,
        workspace_root=tmp_path,
    )

    events = list(iter_events_jsonl(normalized))
    assert any(e["type"] == "agent_message" for e in events)
    assert any(e["type"] == "run_command" for e in events)
    assert any(e["type"] == "read_file" for e in events)


def test_claude_read_attests_only_the_unique_observed_range(tmp_path: Path) -> None:
    source = tmp_path / "src.py"
    source.write_text(
        "def observed():\n    return 1\n\ndef unseen():\n    return 2\n",
        encoding="utf-8",
    )
    partial = "def observed():\n    return 1\n"
    raw = tmp_path / "raw.jsonl"
    raw.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "assistant",
                        "message": {
                            "role": "assistant",
                            "content": [
                                {
                                    "type": "tool_use",
                                    "id": "tool_read",
                                    "name": "Read",
                                    "input": {"file_path": "src.py"},
                                }
                            ],
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "user",
                        "message": {
                            "role": "user",
                            "content": [
                                {
                                    "type": "tool_result",
                                    "tool_use_id": "tool_read",
                                    "content": partial,
                                    "is_error": False,
                                }
                            ],
                        },
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    normalized = tmp_path / "normalized.jsonl"

    normalize_claude_events(
        raw_events_path=raw,
        normalized_events_path=normalized,
        workspace_root=tmp_path,
    )

    read = next(
        event for event in iter_events_jsonl(normalized) if event["type"] == "read_file"
    )
    assert read["data"]["content_observed"] is True
    assert read["data"]["whole_file_observed"] is False
    assert read["data"]["observed_content"] == partial
    assert read["data"]["observed_start_line"] == 1
    assert read["data"]["observed_end_line"] == 3


def test_normalize_claude_events_writes_command_failure_artifacts(tmp_path: Path) -> None:
    raw = tmp_path / "raw.jsonl"
    raw.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "assistant",
                        "message": {
                            "role": "assistant",
                            "content": [
                                {
                                    "type": "tool_use",
                                    "id": "tool_1",
                                    "name": "Bash",
                                    "input": {"command": "echo hi"},
                                }
                            ],
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "user",
                        "message": {
                            "role": "user",
                            "content": [
                                {
                                    "type": "tool_result",
                                    "tool_use_id": "tool_1",
                                    "content": "boom",
                                    "is_error": True,
                                }
                            ],
                        },
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    normalized = tmp_path / "normalized.jsonl"
    normalize_claude_events(
        raw_events_path=raw,
        normalized_events_path=normalized,
        workspace_root=tmp_path,
    )

    events = list(iter_events_jsonl(normalized))
    cmd = next(e for e in events if e["type"] == "run_command")
    artifacts = cmd.get("data", {}).get("failure_artifacts")
    assert isinstance(artifacts, dict)
    assert (tmp_path / "command_failures" / "cmd_01" / "stderr.txt").read_text(
        encoding="utf-8"
    ).strip() == "boom"


def test_normalize_claude_events_maps_workspace_mount_paths(tmp_path: Path) -> None:
    (tmp_path / "USERS.md").write_text("# Users\n", encoding="utf-8")

    raw = tmp_path / "raw.jsonl"
    raw.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "assistant",
                        "message": {
                            "role": "assistant",
                            "content": [
                                {
                                    "type": "tool_use",
                                    "id": "tool_1",
                                    "name": "Read",
                                    "input": {"file_path": "/workspace/USERS.md"},
                                }
                            ],
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "user",
                        "message": {
                            "role": "user",
                            "content": [
                                {
                                    "type": "tool_result",
                                    "tool_use_id": "tool_1",
                                    "content": "# Users\n",
                                    "is_error": False,
                                }
                            ],
                        },
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    normalized = tmp_path / "normalized.jsonl"
    normalize_claude_events(
        raw_events_path=raw,
        normalized_events_path=normalized,
        workspace_root=tmp_path,
        workspace_mount="/workspace",
    )

    events = list(iter_events_jsonl(normalized))
    read_paths = [e.get("data", {}).get("path") for e in events if e["type"] == "read_file"]
    assert "USERS.md" in read_paths


def test_normalize_claude_events_writes_tool_failure_artifacts(tmp_path: Path) -> None:
    (tmp_path / "USERS.md").write_text("# Users\n", encoding="utf-8")

    raw = tmp_path / "raw.jsonl"
    raw.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "assistant",
                        "message": {
                            "role": "assistant",
                            "content": [
                                {
                                    "type": "tool_use",
                                    "id": "tool_1",
                                    "name": "Edit",
                                    "input": {
                                        "path": "USERS.md",
                                        "old_string": "missing",
                                        "new_string": "present",
                                        "expected_occurrences": 1,
                                    },
                                }
                            ],
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "user",
                        "message": {
                            "role": "user",
                            "content": [
                                {
                                    "type": "tool_result",
                                    "tool_use_id": "tool_1",
                                    "content": "Expected 1 occurrences but found 0.",
                                    "is_error": True,
                                }
                            ],
                        },
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    normalized = tmp_path / "normalized.jsonl"
    normalize_claude_events(
        raw_events_path=raw,
        normalized_events_path=normalized,
        workspace_root=tmp_path,
    )

    events = list(iter_events_jsonl(normalized))
    tool_call = next(e for e in events if e["type"] == "tool_call")
    artifacts = tool_call.get("data", {}).get("failure_artifacts")
    assert isinstance(artifacts, dict)
    assert (tmp_path / "tool_failures" / "tool_01_edit" / "tool.json").exists()


def test_normalize_claude_events_emits_delegation_summary(tmp_path: Path) -> None:
    raw = tmp_path / "raw.jsonl"
    raw.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "assistant",
                        "message": {
                            "role": "assistant",
                            "content": [
                                {
                                    "type": "tool_use",
                                    "id": "agent_1",
                                    "name": "Agent",
                                    "input": {
                                        "description": "review token monitor",
                                        "prompt": "Read tests and summarize risks only.",
                                    },
                                }
                            ],
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "user",
                        "message": {
                            "role": "user",
                            "content": [
                                {
                                    "type": "tool_result",
                                    "tool_use_id": "agent_1",
                                    "content": (
                                        "Findings summary: no raw source needed. "
                                        "Recommended next step: add classification test."
                                    ),
                                    "is_error": False,
                                }
                            ],
                        },
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    normalized = tmp_path / "normalized.jsonl"
    normalize_claude_events(raw_events_path=raw, normalized_events_path=normalized)

    events = list(iter_events_jsonl(normalized))
    assert [e["type"] for e in events] == ["delegation_invocation", "delegation_result"]
    result = events[1]
    assert result["data"]["tool_name"] == "Agent"
    assert result["data"]["result_kind"] == "parent_context_summary"
    assert result["data"]["raw_broad_source_leak"] is False
