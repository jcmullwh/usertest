from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from agent_adapters import (
    normalize_claude_events,
    normalize_codex_events,
    normalize_gemini_events,
)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line:
            continue
        rows.append(json.loads(line))
    return rows


def _strip_timestamps(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for event in events:
        cleaned = dict(event)
        cleaned.pop("ts", None)
        normalized.append(cleaned)
    return normalized


@pytest.mark.parametrize(
    ("fixture_name", "normalizer"),
    [
        ("minimal_codex_run", normalize_codex_events),
        ("minimal_claude_run", normalize_claude_events),
        ("minimal_gemini_run", normalize_gemini_events),
    ],
)
def test_normalization_matches_checked_in_golden_fixtures(
    tmp_path: Path,
    fixture_name: str,
    normalizer: Any,
) -> None:
    fixture_dir = _repo_root() / "examples" / "golden_runs" / fixture_name
    raw_events = fixture_dir / "raw_events.jsonl"
    expected_normalized = fixture_dir / "normalized_events.jsonl"

    assert raw_events.exists()
    assert expected_normalized.exists()

    actual_path = tmp_path / f"{fixture_name}.normalized.jsonl"
    normalizer(
        raw_events_path=raw_events,
        normalized_events_path=actual_path,
        workspace_root=None,
    )

    actual_events = _strip_timestamps(_load_jsonl(actual_path))
    expected_events = _strip_timestamps(_load_jsonl(expected_normalized))
    assert actual_events == expected_events
