from __future__ import annotations

import json
from pathlib import Path

from agent_adapters.gemini_cli import _extract_last_message_text


def test_extract_last_message_text_handles_output_format_json(tmp_path: Path) -> None:
    raw = tmp_path / "raw.json"
    raw.write_text(json.dumps({"response": "ok"}), encoding="utf-8")
    assert _extract_last_message_text(raw) == "ok"


def test_extract_last_message_text_returns_last_assistant_segment(tmp_path: Path) -> None:
    raw = tmp_path / "raw.jsonl"
    raw.write_text(
        "\n".join(
            [
                json.dumps(
                    {"type": "message", "role": "assistant", "content": "hello ", "delta": True}
                ),
                json.dumps(
                    {"type": "message", "role": "assistant", "content": "world", "delta": True}
                ),
                json.dumps(
                    {
                        "type": "tool_use",
                        "tool_name": "run_shell_command",
                        "tool_id": "t1",
                        "parameters": {"command": "echo hi"},
                    }
                ),
                json.dumps({"type": "tool_result", "tool_id": "t1", "status": "success"}),
                json.dumps(
                    {"type": "message", "role": "assistant", "content": "final", "delta": True}
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    assert _extract_last_message_text(raw) == "final"


def test_extract_last_message_text_prefers_non_delta_full_message(tmp_path: Path) -> None:
    raw = tmp_path / "raw.jsonl"
    raw.write_text(
        "\n".join(
            [
                json.dumps(
                    {"type": "message", "role": "assistant", "content": "draft", "delta": True}
                ),
                json.dumps(
                    {"type": "message", "role": "assistant", "content": "full", "delta": False}
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    assert _extract_last_message_text(raw) == "full"


def test_extract_last_message_text_recovers_json_from_write_file_tool_use(tmp_path: Path) -> None:
    payload = {"schema_version": 1, "mission": "x", "persona": {"name": "n"}}
    raw = tmp_path / "raw.jsonl"
    raw.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "tool_use",
                        "tool_name": "write_file",
                        "parameters": {"file_path": "report.json", "content": json.dumps(payload)},
                    }
                ),
                json.dumps(
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": "Task complete. JSON report generated.",
                        "delta": False,
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    extracted = _extract_last_message_text(raw)
    assert json.loads(extracted) == payload


def test_extract_last_message_text_recovers_json_from_tool_result_code_fence(
    tmp_path: Path,
) -> None:
    payload = {"schema_version": 1, "mission": "x", "persona": {"name": "n"}}
    raw = tmp_path / "raw.jsonl"
    raw.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "tool_result",
                        "tool_id": "t1",
                        "status": "success",
                        "output": "```json\n" + json.dumps(payload) + "\n```",
                    }
                ),
                json.dumps(
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": "JSON report generated and displayed.",
                        "delta": True,
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    extracted = _extract_last_message_text(raw)
    assert json.loads(extracted) == payload
