from __future__ import annotations

import json
from pathlib import Path

import pytest

from token_monitoring.codex import parse_codex_invocation_usage
from token_monitoring.usage import (
    TokenUsage,
    usage_receipt_content_sha256,
    usage_receipt_is_valid,
)

FIXTURES = Path(__file__).parent / "fixtures" / "codex_usage"


def test_fresh_invocation_is_per_invocation_and_retains_all_dimensions() -> None:
    result = parse_codex_invocation_usage(
        FIXTURES / "fresh_turn.jsonl",
        invocation_id="invocation-fresh",
    )

    assert result.attributable is True
    assert result.semantics == "per_invocation"
    assert result.session_id == "thread-fresh"
    assert result.usage == TokenUsage(
        total_tokens=120,
        input_tokens=100,
        cached_input_tokens=60,
        uncached_input_tokens=40,
        output_tokens=20,
        reasoning_output_tokens=5,
    )


def test_continuation_subtracts_baseline_high_water_once() -> None:
    result = parse_codex_invocation_usage(
        FIXTURES / "continuation_turn.jsonl",
        invocation_id="invocation-continuation",
        baseline_high_water={
            "total_tokens": 120,
            "input_tokens": 100,
            "cached_input_tokens": 60,
            "uncached_input_tokens": 40,
            "output_tokens": 20,
            "reasoning_output_tokens": 5,
        },
    )

    assert result.attributable is True
    assert result.semantics == "session_cumulative"
    assert result.usage == TokenUsage(
        total_tokens=180,
        input_tokens=150,
        cached_input_tokens=90,
        uncached_input_tokens=60,
        output_tokens=30,
        reasoning_output_tokens=5,
    )
    assert result.observed_high_water is not None
    assert result.observed_high_water.total_tokens == 300


def test_multiple_cumulative_terminals_without_baseline_are_unattributable() -> None:
    result = parse_codex_invocation_usage(
        FIXTURES / "ambiguous_turns.jsonl",
        invocation_id="invocation-ambiguous",
    )

    assert result.attributable is False
    assert result.semantics == "unattributable"
    assert result.usage is None
    assert any(
        item["code"] == "multiple_terminal_usage_values_without_baseline"
        for item in result.diagnostics
    )


def test_multiple_cumulative_terminals_with_baseline_are_unattributable() -> None:
    result = parse_codex_invocation_usage(
        FIXTURES / "ambiguous_turns.jsonl",
        invocation_id="invocation-ambiguous-resume",
        baseline_high_water={
            "total_tokens": 0,
            "input_tokens": 0,
            "cached_input_tokens": 0,
            "uncached_input_tokens": 0,
            "output_tokens": 0,
            "reasoning_output_tokens": 0,
        },
    )

    assert result.attributable is False
    assert result.semantics == "unattributable"
    assert result.usage is None
    assert any(
        item["code"] == "multiple_terminal_usage_values_with_baseline"
        for item in result.diagnostics
    )


def test_missing_terminal_usage_is_unattributable_not_zero() -> None:
    result = parse_codex_invocation_usage(
        FIXTURES / "missing_usage.jsonl",
        invocation_id="invocation-missing",
    )

    assert result.attributable is False
    assert result.usage is None
    assert any(
        item["code"] == "terminal_events_missing_usage" for item in result.diagnostics
    )


def test_duplicate_terminal_is_counted_once_and_receipt_is_content_addressed() -> None:
    first = parse_codex_invocation_usage(
        FIXTURES / "duplicate_terminal.jsonl",
        invocation_id="invocation-duplicate",
    )
    second = parse_codex_invocation_usage(
        FIXTURES / "duplicate_terminal.jsonl",
        invocation_id="invocation-duplicate",
    )

    assert first.attributable is True
    assert first.usage is not None
    assert first.usage.total_tokens == 120
    assert first.observation_count == 2
    assert first.unique_observation_count == 1
    assert first.duplicate_terminal_count == 1
    assert first.receipt_payload() == second.receipt_payload()
    assert usage_receipt_is_valid(first.receipt_payload())


def test_receipt_rejects_incomplete_attributable_token_maps() -> None:
    result = parse_codex_invocation_usage(
        FIXTURES / "fresh_turn.jsonl",
        invocation_id="invocation-incomplete-receipt",
    )
    payload = result.receipt_payload()
    payload["usage"] = {}
    payload["content_sha256"] = usage_receipt_content_sha256(payload)

    assert usage_receipt_is_valid(payload) is False

    payload = result.receipt_payload()
    observed = dict(payload["observed_high_water"])
    observed.pop("reasoning_output_tokens")
    payload["observed_high_water"] = observed
    payload["content_sha256"] = usage_receipt_content_sha256(payload)

    assert usage_receipt_is_valid(payload) is False


def test_incomplete_provider_usage_is_unattributable(tmp_path: Path) -> None:
    stream = tmp_path / "incomplete.jsonl"
    stream.write_text(
        json.dumps({"type": "turn.completed", "usage": {"total_tokens": 120}}) + "\n",
        encoding="utf-8",
    )

    result = parse_codex_invocation_usage(
        stream,
        invocation_id="invocation-incomplete-provider-usage",
    )

    assert result.semantics == "unattributable"
    assert result.usage is None
    assert any(item["code"] == "invalid_usage_values" for item in result.diagnostics)


def test_token_usage_rejects_inconsistent_total() -> None:
    with pytest.raises(
        ValueError,
        match="total_tokens must equal input_tokens \\+ output_tokens",
    ):
        TokenUsage.from_mapping(
            {
                "total_tokens": 121,
                "input_tokens": 100,
                "cached_input_tokens": 60,
                "output_tokens": 20,
            }
        )


def test_nonadjacent_terminal_replay_does_not_regress_high_water(tmp_path: Path) -> None:
    terminal = {
        "type": "turn.completed",
        "usage": {
            "input_tokens": 100,
            "cached_input_tokens": 60,
            "output_tokens": 20,
            "reasoning_output_tokens": 5,
            "total_tokens": 120,
        },
    }
    cumulative = {
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "info": {
                "total_token_usage": {
                    "input_tokens": 120,
                    "cached_input_tokens": 70,
                    "output_tokens": 30,
                    "reasoning_output_tokens": 6,
                    "total_tokens": 150,
                },
                "last_token_usage": {
                    "input_tokens": 20,
                    "cached_input_tokens": 10,
                    "output_tokens": 10,
                    "reasoning_output_tokens": 1,
                    "total_tokens": 30,
                },
            },
        },
    }
    stream = tmp_path / "terminal-replay.jsonl"
    stream.write_text(
        "\n".join(json.dumps(item) for item in (terminal, cumulative, terminal)) + "\n",
        encoding="utf-8",
    )

    result = parse_codex_invocation_usage(
        stream,
        invocation_id="invocation-terminal-replay",
    )

    assert result.attributable is True
    assert result.usage == TokenUsage(
        total_tokens=150,
        input_tokens=120,
        cached_input_tokens=70,
        uncached_input_tokens=50,
        output_tokens=30,
        reasoning_output_tokens=6,
    )
    assert result.duplicate_terminal_count == 1
    assert not any(
        item["code"] == "non_monotonic_cumulative_usage"
        for item in result.diagnostics
    )


def test_baseline_above_observed_usage_is_unattributable() -> None:
    result = parse_codex_invocation_usage(
        FIXTURES / "fresh_turn.jsonl",
        invocation_id="invocation-bad-baseline",
        baseline_high_water={
            "total_tokens": 200,
            "input_tokens": 180,
            "cached_input_tokens": 100,
            "output_tokens": 20,
            "reasoning_tokens": 5,
        },
    )

    assert result.semantics == "unattributable"
    assert result.usage is None
    assert any(item["code"] == "final_usage_below_baseline" for item in result.diagnostics)
