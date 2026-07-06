from __future__ import annotations

import json
from pathlib import Path

from token_monitoring.codex import find_codex_session_for_thread, parse_codex_session


def _write_session(path: Path, events: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(event) for event in events) + "\n",
        encoding="utf-8",
    )


def _token_event(total: int, last: int) -> dict[str, object]:
    return {
        "timestamp": "2026-07-05T00:00:00Z",
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "info": {
                "total_token_usage": {
                    "input_tokens": total,
                    "cached_input_tokens": 0,
                    "output_tokens": 0,
                    "reasoning_output_tokens": 0,
                    "total_tokens": total,
                },
                "last_token_usage": {
                    "input_tokens": last,
                    "cached_input_tokens": 0,
                    "output_tokens": 0,
                    "reasoning_output_tokens": 0,
                    "total_tokens": last,
                },
            },
        },
    }


def _function_call(command: str) -> dict[str, object]:
    return {
        "type": "response_item",
        "payload": {
            "type": "function_call",
            "name": "exec_command",
            "arguments": json.dumps({"cmd": command}),
        },
    }


def test_reconciled_monotonic_session_is_accepted(tmp_path: Path) -> None:
    session = tmp_path / "rollout-2026-07-05T00-00-00-thread-1.jsonl"
    _write_session(
        session,
        [
            {"type": "session_meta", "payload": {"session_id": "thread-1"}},
            _token_event(10, 10),
            _function_call("sed -n '1,200p' pyproject.toml"),
            _token_event(25, 15),
            _function_call("Start-Sleep -Seconds 30"),
        ],
    )

    result = parse_codex_session(session)

    assert result.accepted is True
    assert result.token_event_count == 2
    assert result.model_call_count == 2
    assert result.final_usage["input_tokens"] == 25
    assert result.trace[0]["action"]["type"] == "source_read"
    assert result.trace[1]["action"]["type"] == "wait_poll"


def test_non_monotonic_session_is_rejected(tmp_path: Path) -> None:
    session = tmp_path / "bad.jsonl"
    _write_session(session, [_token_event(10, 10), _function_call("pwd"), _token_event(5, -5)])

    result = parse_codex_session(session)

    assert result.accepted is False
    assert any(item["code"] == "non_monotonic_cumulative_usage" for item in result.exceptions)


def test_no_counter_and_zero_byte_sessions_are_unattributable(tmp_path: Path) -> None:
    no_counter = tmp_path / "no-counter.jsonl"
    _write_session(no_counter, [{"type": "response_item", "payload": {"type": "message"}}])
    zero_byte = tmp_path / "zero.jsonl"
    zero_byte.write_text("", encoding="utf-8")

    assert any(
        item["code"] == "no_token_count_events"
        for item in parse_codex_session(no_counter).exceptions
    )
    assert any(
        item["code"] == "session_zero_byte" for item in parse_codex_session(zero_byte).exceptions
    )


def test_find_session_joins_thread_to_exactly_one_file(tmp_path: Path) -> None:
    session = tmp_path / "2026" / "07" / "05" / "rollout-thread-abc.jsonl"
    _write_session(session, [{"type": "session_meta", "payload": {"session_id": "thread-abc"}}])

    found, exceptions = find_codex_session_for_thread(tmp_path, "thread-abc")

    assert found == session
    assert exceptions == []


def test_ambiguous_thread_join_is_named(tmp_path: Path) -> None:
    _write_session(tmp_path / "rollout-thread-abc-a.jsonl", [])
    _write_session(tmp_path / "rollout-thread-abc-b.jsonl", [])

    found, exceptions = find_codex_session_for_thread(tmp_path, "thread-abc")

    assert found is None
    assert exceptions[0]["code"] == "ambiguous_session_filename_matches"
