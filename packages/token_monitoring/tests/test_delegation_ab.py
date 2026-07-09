from __future__ import annotations

import json
from pathlib import Path

from token_monitoring.delegation_ab import (
    analyze_delegation_ab,
    render_delegation_ab_markdown,
    write_delegation_ab_validation,
)


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


def _base_run(run_dir: Path, *, thread_id: str, delegation_events: list[dict[str, object]]) -> None:
    _write_json(run_dir / "target_ref.json", {"agent": "codex"})
    _write_jsonl(run_dir / "raw_events.jsonl", [{"type": "thread.started", "thread_id": thread_id}])
    _write_jsonl(
        run_dir / "normalized_events.jsonl",
        [
            *delegation_events,
            {
                "type": "run_command",
                "data": {"command": "python -m pytest -q", "exit_code": 0},
            },
        ],
    )
    _write_json(run_dir / "metrics.json", {"commands_failed": 0})
    _write_json(run_dir / "run_meta.json", {"schema_version": 1, "run_wall_seconds": 60.0})
    _write_json(run_dir / "timing.json", {"duration_seconds": 60.0})
    _write_json(run_dir / "verification.json", {"passed": True})
    _write_json(
        run_dir / "report.json",
        {
            "schema_version": 1,
            "kind": "task_run_v1",
            "status": "success",
            "summary": "ok",
            "steps": [{"name": "n", "attempts": [{"action": "a"}], "outcome": "o"}],
            "outputs": [],
            "next_actions": ["none"],
        },
    )
    _write_json(
        run_dir / "ticket_ref.json",
        {"fingerprint": "447b9812a01f72dd", "title": "Representative maintenance task"},
    )


def test_delegation_ab_evaluates_total_input_increase_with_parent_peak_drop(
    tmp_path: Path,
) -> None:
    sessions = tmp_path / "sessions"
    disabled = tmp_path / "disabled"
    enabled = tmp_path / "enabled"
    _base_run(disabled, thread_id="disabled-thread", delegation_events=[])
    _base_run(
        enabled,
        thread_id="enabled-thread",
        delegation_events=[
            {
                "type": "delegation_invocation",
                "data": {"tool_name": "invoke_agent", "prompt_chars": 80},
            },
            {
                "type": "delegation_result",
                "data": {
                    "tool_name": "invoke_agent",
                    "result_kind": "parent_context_summary",
                    "output_chars": 240,
                    "output_lines": 6,
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
        sessions / "rollout-disabled-thread.jsonl",
        [
            {"type": "session_meta", "payload": {"session_id": "disabled-thread"}},
            _token_event(4000, 4000),
            _call("sed -n '1,200p' packages/runner_core/src/runner_core/runner.py"),
        ],
    )
    _write_jsonl(
        sessions / "rollout-enabled-thread.jsonl",
        [
            {"type": "session_meta", "payload": {"session_id": "enabled-thread"}},
            _token_event(1000, 1000),
            _call("pwd"),
        ],
    )

    report = analyze_delegation_ab(
        disabled_run_dirs=[disabled],
        enabled_run_dirs=[enabled],
        codex_sessions_root=sessions,
    )

    assert report["evidence_strength"] == "representative_ab_evidence"
    assert report["arms"]["delegation_disabled"]["avg_parent_input_peak"] == 4000
    assert report["arms"]["delegation_enabled"]["avg_parent_input_peak"] == 1000
    assert report["tradeoff_evaluation"]["combined_input_tokens_delta"] == 2000
    assert report["tradeoff_evaluation"]["combined_total_tokens_delta"] == 2497
    assert (
        report["tradeoff_evaluation"]["conclusion"]
        == "delegation_increased_combined_tokens_with_compensating_evidence"
    )
    assert report["comparisons"][0]["parent_input_peak_delta"] == -3000
    assert "raw broad-source" not in json.dumps(report["runs"])


def test_delegation_ab_recommends_prompt_tightening_for_raw_leaks(tmp_path: Path) -> None:
    sessions = tmp_path / "sessions"
    disabled = tmp_path / "disabled"
    enabled = tmp_path / "enabled"
    _base_run(disabled, thread_id="disabled-thread", delegation_events=[])
    _base_run(
        enabled,
        thread_id="enabled-thread",
        delegation_events=[
            {"type": "delegation_invocation", "data": {"tool_name": "invoke_agent"}},
            {
                "type": "delegation_result",
                "data": {
                    "tool_name": "invoke_agent",
                    "result_kind": "raw_broad_source_leak",
                    "output_chars": 50000,
                    "output_lines": 1000,
                    "source_like_lines": 800,
                    "raw_broad_source_leak": True,
                },
            },
        ],
    )
    for thread in ("disabled-thread", "enabled-thread"):
        _write_jsonl(
            sessions / f"rollout-{thread}.jsonl",
            [
                {"type": "session_meta", "payload": {"session_id": thread}},
                _token_event(1000, 1000),
                _call("pwd"),
            ],
        )

    report = analyze_delegation_ab(
        disabled_run_dirs=[disabled],
        enabled_run_dirs=[enabled],
        codex_sessions_root=sessions,
    )

    assert report["arms"]["delegation_enabled"]["delegation_raw_broad_source_leak_count"] == 1
    assert any("Tighten delegation prompts/policy" in action for action in report["next_actions"])


def test_delegation_ab_does_not_treat_unsupported_provider_tokens_as_zero(
    tmp_path: Path,
) -> None:
    disabled = tmp_path / "disabled"
    enabled = tmp_path / "enabled"
    _base_run(disabled, thread_id="disabled-thread", delegation_events=[])
    _base_run(
        enabled,
        thread_id="enabled-thread",
        delegation_events=[
            {"type": "delegation_invocation", "data": {"tool_name": "Agent"}},
            {
                "type": "delegation_result",
                "data": {
                    "tool_name": "Agent",
                    "result_kind": "parent_context_summary",
                    "output_chars": 240,
                    "output_lines": 6,
                },
            },
        ],
    )
    _write_json(disabled / "target_ref.json", {"agent": "claude"})
    _write_json(enabled / "target_ref.json", {"agent": "claude"})

    report = analyze_delegation_ab(
        disabled_run_dirs=[disabled],
        enabled_run_dirs=[enabled],
    )

    assert report["evidence_strength"] == "partial_missing_authoritative_token_join"
    assert report["arms"]["delegation_disabled"]["authoritative_token_run_count"] == 0
    assert report["arms"]["delegation_enabled"]["authoritative_token_run_count"] == 0
    assert report["arms"]["delegation_disabled"]["avg_combined_total_tokens"] is None
    assert report["arms"]["delegation_enabled"]["avg_combined_total_tokens"] is None
    assert report["comparisons"][0]["combined_input_tokens_delta"] is None
    assert report["tradeoff_evaluation"]["combined_total_tokens_delta"] is None
    assert report["tradeoff_evaluation"]["conclusion"] == "token_tradeoff_unattributable"


def test_write_delegation_ab_validation_writes_json_and_markdown(tmp_path: Path) -> None:
    sessions = tmp_path / "sessions"
    disabled = tmp_path / "disabled"
    enabled = tmp_path / "enabled"
    out = tmp_path / "out"
    _base_run(disabled, thread_id="disabled-thread", delegation_events=[])
    _base_run(enabled, thread_id="enabled-thread", delegation_events=[])
    for thread in ("disabled-thread", "enabled-thread"):
        _write_jsonl(
            sessions / f"rollout-{thread}.jsonl",
            [
                {"type": "session_meta", "payload": {"session_id": thread}},
                _token_event(1000, 1000),
                _call("pwd"),
            ],
        )

    report = write_delegation_ab_validation(
        disabled_run_dirs=[disabled],
        enabled_run_dirs=[enabled],
        output_dir=out,
        codex_sessions_root=sessions,
    )

    assert report["schema_version"] == 1
    assert (out / "delegation_ab_validation.json").exists()
    markdown = (out / "delegation_ab_validation.md").read_text(encoding="utf-8")
    assert "# Delegation A/B validation" in markdown
    assert render_delegation_ab_markdown(report) == markdown
