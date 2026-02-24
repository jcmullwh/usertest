from __future__ import annotations

import json
from pathlib import Path

from agent_adapters import normalize_gemini_events
from agent_adapters.events import iter_events_jsonl


def test_normalize_gemini_events_emits_expected_events(tmp_path: Path) -> None:
    (tmp_path / "USERS.md").write_text("# Users\n", encoding="utf-8")

    raw = tmp_path / "raw.jsonl"
    raw.write_text(
        "\n".join(
            [
                "Loaded cached credentials.",
                json.dumps(
                    {
                        "type": "tool_use",
                        "tool_name": "read_file",
                        "tool_id": "t1",
                        "parameters": {"file_path": "USERS.md", "limit": 2},
                    }
                ),
                json.dumps({"type": "tool_result", "tool_id": "t1", "status": "success"}),
                json.dumps(
                    {
                        "type": "tool_use",
                        "tool_name": "run_shell_command",
                        "tool_id": "t2",
                        "parameters": {"command": "echo hi"},
                    }
                ),
                json.dumps(
                    {
                        "type": "tool_result",
                        "tool_id": "t2",
                        "status": "error",
                        "output": "denied",
                    }
                ),
                json.dumps(
                    {
                        "type": "tool_use",
                        "tool_name": "search_file_content",
                        "tool_id": "t3",
                        "parameters": {"pattern": "Users", "dir_path": "USERS.md"},
                    }
                ),
                json.dumps({"type": "tool_result", "tool_id": "t3", "status": "success"}),
                json.dumps(
                    {
                        "type": "tool_use",
                        "tool_name": "write_file",
                        "tool_id": "t4",
                        "parameters": {"file_path": "out.txt", "content": "hi"},
                    }
                ),
                json.dumps({"type": "tool_result", "tool_id": "t4", "status": "success"}),
                json.dumps(
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": "{\"schema_version\": 1}",
                        "delta": True,
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    normalized = tmp_path / "normalized.jsonl"
    normalize_gemini_events(
        raw_events_path=raw,
        normalized_events_path=normalized,
        workspace_root=tmp_path,
    )

    events = list(iter_events_jsonl(normalized))
    assert any(e["type"] == "read_file" for e in events)
    assert any(e["type"] == "run_command" for e in events)
    assert any(e["type"] == "tool_call" for e in events)
    assert any(
        e["type"] == "tool_call" and e.get("data", {}).get("name") == "write_file" for e in events
    )
    assert any(e["type"] == "agent_message" for e in events)

    cmd = next(e for e in events if e["type"] == "run_command")
    artifacts = cmd.get("data", {}).get("failure_artifacts")
    assert isinstance(artifacts, dict)
    assert (tmp_path / "command_failures" / "cmd_01" / "stdout.txt").read_text(
        encoding="utf-8"
    ).strip() == "denied"


def test_normalize_gemini_events_maps_workspace_mount_paths(tmp_path: Path) -> None:
    (tmp_path / "USERS.md").write_text("# Users\n", encoding="utf-8")

    raw = tmp_path / "raw.jsonl"
    raw.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "tool_use",
                        "tool_name": "read_file",
                        "tool_id": "t1",
                        "parameters": {"file_path": "/workspace/USERS.md", "limit": 2},
                    }
                ),
                json.dumps({"type": "tool_result", "tool_id": "t1", "status": "success"}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    normalized = tmp_path / "normalized.jsonl"
    normalize_gemini_events(
        raw_events_path=raw,
        normalized_events_path=normalized,
        workspace_root=tmp_path,
        workspace_mount="/workspace",
    )

    events = list(iter_events_jsonl(normalized))
    read_paths = [e.get("data", {}).get("path") for e in events if e["type"] == "read_file"]
    assert "USERS.md" in read_paths


def test_normalize_gemini_events_merges_delta_messages(tmp_path: Path) -> None:
    raw = tmp_path / "raw.jsonl"
    raw.write_text(
        "\n".join(
            [
                json.dumps({"type": "message", "role": "assistant", "content": "a", "delta": True}),
                json.dumps({"type": "message", "role": "assistant", "content": "b", "delta": True}),
                json.dumps(
                    {
                        "type": "tool_use",
                        "tool_name": "run_shell_command",
                        "tool_id": "t1",
                        "parameters": {"command": "echo hi"},
                    }
                ),
                json.dumps({"type": "tool_result", "tool_id": "t1", "status": "success"}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    normalized = tmp_path / "normalized.jsonl"
    normalize_gemini_events(
        raw_events_path=raw,
        normalized_events_path=normalized,
        workspace_root=tmp_path,
    )

    events = list(iter_events_jsonl(normalized))
    msgs = [e for e in events if e["type"] == "agent_message"]
    assert len(msgs) == 1
    assert msgs[0].get("data", {}).get("text") == "ab"
