from __future__ import annotations

import json
from pathlib import Path

from usertest.cli import main


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _write_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")


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
                    "output_tokens": 1,
                    "reasoning_output_tokens": 0,
                    "total_tokens": total + 1,
                },
                "last_token_usage": {
                    "input_tokens": last,
                    "cached_input_tokens": 0,
                    "output_tokens": 1,
                    "reasoning_output_tokens": 0,
                    "total_tokens": last + 1,
                },
            },
        },
    }


def _base_run(run_dir: Path, sessions: Path) -> None:
    _write_json(run_dir / "target_ref.json", {"agent": "codex"})
    _write_jsonl(
        run_dir / "raw_events.jsonl", [{"type": "thread.started", "thread_id": "thread-1"}]
    )
    _write_jsonl(run_dir / "normalized_events.jsonl", [])
    _write_json(run_dir / "metrics.json", {})
    _write_json(run_dir / "run_meta.json", {"schema_version": 1})
    _write_json(run_dir / "report.json", {"summary": "ok"})
    _write_json(run_dir / "agent_attempts.json", {"attempts": [{"attempt": 1}]})
    _write_jsonl(
        sessions / "rollout-thread-1.jsonl",
        [
            {"type": "session_meta", "payload": {"session_id": "thread-1"}},
            _token_event(10, 10),
            {
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "name": "exec_command",
                    "arguments": json.dumps({"cmd": "pwd"}),
                },
            },
        ],
    )


def test_token_monitor_analyze_no_write_prints_json_only(tmp_path: Path, capsys: object) -> None:
    run_dir = tmp_path / "run"
    sessions = tmp_path / "sessions"
    _base_run(run_dir, sessions)

    try:
        main(
            [
                "token-monitor",
                "analyze",
                "--run-dir",
                str(run_dir),
                "--codex-sessions-root",
                str(sessions),
                "--no-write",
            ]
        )
    except SystemExit as exc:
        assert exc.code == 0

    out = capsys.readouterr().out  # type: ignore[attr-defined]
    parsed = json.loads(out)
    assert parsed["token_summary"]["authoritative"] is True
    assert not (run_dir / "token_monitoring.json").exists()


def test_token_monitor_analyze_writes_artifacts(tmp_path: Path, capsys: object) -> None:
    run_dir = tmp_path / "run"
    sessions = tmp_path / "sessions"
    _base_run(run_dir, sessions)

    try:
        main(
            [
                "token-monitor",
                "analyze",
                "--run-dir",
                str(run_dir),
                "--codex-sessions-root",
                str(sessions),
            ]
        )
    except SystemExit as exc:
        assert exc.code == 0

    out = capsys.readouterr().out  # type: ignore[attr-defined]
    assert "token_monitoring.json" in out
    assert (run_dir / "token_monitoring.json").exists()
    assert (run_dir / "token_causal_trace.jsonl").exists()


def test_token_monitor_batch_context_command_writes_artifacts(
    tmp_path: Path, capsys: object
) -> None:
    batch_dir = tmp_path / "batch"
    _write_json(
        batch_dir / "batch_summary.json",
        {"status": "blocked", "completed_count": 0, "failed_count": 1, "global_blocker_count": 1},
    )
    _write_json(batch_dir / "batch_state.json", {"status": "blocked", "completed": []})
    _write_json(batch_dir / "global_blockers.json", {"global_blockers": [{"run_dir": None}]})
    _write_jsonl(batch_dir / "ticket_outcomes.jsonl", [{"run_dir": None}])

    try:
        main(["token-monitor", "batch-context", "--batch-dir", str(batch_dir)])
    except SystemExit as exc:
        assert exc.code == 0

    out = capsys.readouterr().out  # type: ignore[attr-defined]
    assert "token_batch_context.json" in out
    assert (batch_dir / "token_batch_context.json").exists()
