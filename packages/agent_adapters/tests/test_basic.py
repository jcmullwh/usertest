from __future__ import annotations

import json
from pathlib import Path

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
    assert any(e["type"] == "read_file" for e in events)


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


def test_normalize_codex_events_handles_cd_and_readlike_chain(tmp_path: Path) -> None:
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
    assert "README.md" in read_paths


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
