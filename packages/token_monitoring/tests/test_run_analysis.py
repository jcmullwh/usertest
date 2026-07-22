from __future__ import annotations

import json
from pathlib import Path

from token_monitoring.batch import analyze_batch_context
from token_monitoring.run_analysis import analyze_run, write_run_monitoring


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
                    "output_tokens": total // 1000,
                    "reasoning_output_tokens": 0,
                    "total_tokens": total + (total // 1000),
                },
                "last_token_usage": {
                    "input_tokens": last,
                    "cached_input_tokens": 0,
                    "output_tokens": last // 1000,
                    "reasoning_output_tokens": 0,
                    "total_tokens": last + (last // 1000),
                },
            },
        },
    }


def _call(command: str) -> dict[str, object]:
    return {
        "type": "response_item",
        "payload": {
            "type": "function_call",
            "name": "exec_command",
            "arguments": json.dumps({"cmd": command}),
        },
    }


def _base_run(run_dir: Path, *, agent: str = "codex", thread_id: str = "thread-1") -> None:
    _write_json(run_dir / "target_ref.json", {"agent": agent})
    _write_jsonl(run_dir / "raw_events.jsonl", [{"type": "thread.started", "thread_id": thread_id}])
    _write_jsonl(
        run_dir / "normalized_events.jsonl",
        [
            {
                "type": "read_file",
                "data": {"path": "packages/runner_core/src/runner_core/runner.py", "bytes": 250000},
            }
        ],
    )
    _write_json(run_dir / "metrics.json", {"event_counts": {"read_file": 1}})
    _write_json(run_dir / "run_meta.json", {"schema_version": 1})
    _write_json(run_dir / "report.json", {"summary": "ok"})
    _write_json(run_dir / "agent_attempts.json", {"attempts": [{"attempt": 1}]})


def test_run_analysis_emits_actionable_signals_without_raw_output(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    sessions = tmp_path / "sessions"
    thread_id = "thread-1"
    _base_run(run_dir, thread_id=thread_id)
    _write_jsonl(
        sessions / f"rollout-{thread_id}.jsonl",
        [
            {"type": "session_meta", "payload": {"session_id": thread_id}},
            _token_event(120000, 120000),
            _call("sed -n '1,2000p' packages/runner_core/src/runner_core/runner.py"),
            {
                "type": "response_item",
                "payload": {"type": "function_call_output", "output": "SECRET_PROMPT_TEXT" * 5000},
            },
            _token_event(260000, 140000),
            _call("Start-Sleep -Seconds 60"),
            _token_event(400000, 140000),
            _call("Start-Sleep -Seconds 60"),
        ],
    )

    analysis = analyze_run(run_dir, codex_sessions_root=sessions)

    signal_ids = {signal["signal_id"] for signal in analysis["signals"]}
    assert "broad_source_config_read" in signal_ids
    assert "wait_poll_resend" in signal_ids
    assert "retained_large_output" in signal_ids
    assert "large_context_resend" in signal_ids
    rendered = json.dumps(analysis)
    assert "SECRET_PROMPT_TEXT" not in rendered
    assert analysis["privacy"]["contains_raw_command_output"] is False


def test_source_read_evidence_ranks_observed_bytes_not_total_file_size(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "run"
    sessions = tmp_path / "sessions"
    thread_id = "thread-1"
    _base_run(run_dir, thread_id=thread_id)
    _write_jsonl(
        run_dir / "normalized_events.jsonl",
        [
            {
                "type": "read_file",
                "data": {
                    "path": "docs/large.json",
                    "bytes": 792_933,
                    "file_size_bytes": 792_933,
                    "observed_bytes": 1_466,
                    "whole_file_observed": False,
                },
            },
            {
                "type": "read_file",
                "data": {
                    "path": "src/whole.py",
                    "bytes": 2_048,
                    "file_size_bytes": 2_048,
                    "observed_bytes": 2_048,
                    "whole_file_observed": True,
                },
            },
        ],
    )
    _write_jsonl(
        sessions / f"rollout-{thread_id}.jsonl",
        [
            {"type": "session_meta", "payload": {"session_id": thread_id}},
            _token_event(120_000, 120_000),
            _call("Get-Content docs/large.json | Select-Object -First 25"),
            _token_event(260_000, 140_000),
        ],
    )

    analysis = analyze_run(run_dir, codex_sessions_root=sessions)

    signal = next(
        signal
        for signal in analysis["signals"]
        if signal["signal_id"] == "broad_source_config_read"
    )
    largest = signal["evidence"]["largest_read_files"]
    assert [item["path"] for item in largest] == ["src/whole.py", "docs/large.json"]
    assert largest[1] == {
        "path": "docs/large.json",
        "bytes": 1_466,
        "observed_bytes": 1_466,
        "file_size_bytes": 792_933,
        "whole_file_observed": False,
        "source": str(run_dir / "normalized_events.jsonl"),
    }


def test_write_run_monitoring_writes_artifacts_and_trace(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    sessions = tmp_path / "sessions"
    _base_run(run_dir)
    _write_jsonl(
        sessions / "rollout-thread-1.jsonl",
        [
            {"type": "session_meta", "payload": {"session_id": "thread-1"}},
            _token_event(10, 10),
            _call("pwd"),
        ],
    )

    write_run_monitoring(run_dir, codex_sessions_root=sessions)

    assert (run_dir / "token_monitoring.json").exists()
    assert (run_dir / "token_monitoring.md").exists()
    assert (run_dir / "token_causal_trace.jsonl").exists()
    assert json.loads((run_dir / "token_monitoring.json").read_text(encoding="utf-8"))[
        "token_summary"
    ]["authoritative"]


def test_non_codex_run_gets_provider_gap_not_fake_totals(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    _base_run(run_dir, agent="claude")

    analysis = analyze_run(run_dir, codex_sessions_root=tmp_path / "sessions")

    assert analysis["token_summary"]["authoritative"] is False
    assert analysis["signals"][0]["signal_id"] == "unsupported_provider_gap"
    assert analysis["signals"][0]["confirmed_by_counters"] is False


def test_batch_context_flags_zero_completed_control_plane_only(tmp_path: Path) -> None:
    batch_dir = tmp_path / "batch"
    _write_json(
        batch_dir / "batch_summary.json",
        {
            "schema_version": 1,
            "status": "blocked",
            "phase": "blocking",
            "completed_count": 0,
            "failed_count": 1,
            "global_blocker_count": 1,
        },
    )
    _write_json(batch_dir / "batch_state.json", {"status": "blocked", "completed": []})
    _write_json(batch_dir / "global_blockers.json", {"global_blockers": [{"run_dir": None}]})
    _write_jsonl(batch_dir / "ticket_outcomes.jsonl", [{"run_dir": None}])

    analysis = analyze_batch_context(batch_dir)

    assert analysis["completed_count"] == 0
    assert analysis["signals"][0]["signal_id"] == "control_plane_spin"
    assert analysis["signals"][0]["confirmed_by_counters"] is False


def test_run_analysis_classifies_no_delegation_explicitly(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    sessions = tmp_path / "sessions"
    _base_run(run_dir)
    _write_jsonl(
        sessions / "rollout-thread-1.jsonl",
        [
            {"type": "session_meta", "payload": {"session_id": "thread-1"}},
            _token_event(100, 100),
            _call("pwd"),
        ],
    )

    analysis = analyze_run(run_dir, codex_sessions_root=sessions)

    assert analysis["delegation_summary"]["classification"] == "no_delegation"
    assert analysis["delegation_summary"]["invocation_count"] == 0


def test_run_analysis_distinguishes_delegation_tradeoff_from_waste(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    sessions = tmp_path / "sessions"
    _base_run(run_dir)
    _write_jsonl(
        run_dir / "normalized_events.jsonl",
        [
            {
                "type": "delegation_invocation",
                "data": {
                    "tool_name": "invoke_agent",
                    "prompt_chars": 80,
                    "input_keys": ["prompt"],
                },
            },
            {
                "type": "delegation_result",
                "data": {
                    "tool_name": "invoke_agent",
                    "result_kind": "parent_context_summary",
                    "output_chars": 320,
                    "output_lines": 8,
                    "raw_broad_source_leak": False,
                    "token_usage": {
                        "input_tokens": 5000,
                        "cached_input_tokens": 0,
                        "uncached_input_tokens": 5000,
                        "output_tokens": 500,
                        "reasoning_output_tokens": 0,
                        "total_tokens": 5500,
                    },
                },
            },
        ],
    )
    _write_jsonl(
        sessions / "rollout-thread-1.jsonl",
        [
            {"type": "session_meta", "payload": {"session_id": "thread-1"}},
            _token_event(1000, 1000),
            _call("pwd"),
        ],
    )

    analysis = analyze_run(run_dir, codex_sessions_root=sessions)

    assert analysis["delegation_summary"]["classification"] == "delegation_parent_context_tradeoff"
    assert analysis["token_summary"]["parent_input_tokens"] == 1000
    assert analysis["token_summary"]["delegated_token_dimensions"]["total_tokens"] == 5500
    assert analysis["token_summary"]["combined_total_tokens"] == 6501
    assert "delegation_parent_context_tradeoff" in {
        signal["signal_id"] for signal in analysis["signals"]
    }


def test_run_analysis_flags_delegation_raw_source_leak_for_non_codex(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    _base_run(run_dir, agent="claude")
    _write_jsonl(
        run_dir / "normalized_events.jsonl",
        [
            {"type": "delegation_invocation", "data": {"tool_name": "Agent"}},
            {
                "type": "delegation_result",
                "data": {
                    "tool_name": "Agent",
                    "result_kind": "raw_broad_source_leak",
                    "output_chars": 50000,
                    "output_lines": 1000,
                    "source_like_lines": 800,
                    "raw_broad_source_leak": True,
                },
            },
        ],
    )

    analysis = analyze_run(run_dir, codex_sessions_root=tmp_path / "sessions")

    assert analysis["delegation_summary"]["classification"] == "delegation_raw_broad_source_leak"
    signal_ids = {signal["signal_id"] for signal in analysis["signals"]}
    assert "unsupported_provider_gap" in signal_ids
    assert "delegation_raw_broad_source_leak" in signal_ids
