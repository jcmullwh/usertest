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
