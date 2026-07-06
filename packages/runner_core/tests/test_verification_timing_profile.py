from __future__ import annotations

import json
from pathlib import Path

import pytest

from runner_core.verification_timing_profile import (
    build_verification_timing_profile,
    percentile,
)


def _write_verification(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_percentile_uses_linear_interpolation() -> None:
    values = [10.0, 20.0, 30.0, 40.0, 50.0]

    assert percentile(values, 0.0) == 10.0
    assert percentile(values, 0.5) == 30.0
    assert percentile(values, 0.95) == pytest.approx(48.0)


def test_build_verification_timing_profile_computes_stats_and_recommendations(
    tmp_path: Path,
) -> None:
    runs_dir = tmp_path / "runs"
    durations = [60.0, 120.0, 180.0, 240.0, 300.0]
    for idx, duration in enumerate(durations, start=1):
        _write_verification(
            runs_dir / f"run{idx}" / "verification.json",
            {
                "schema_version": 1,
                "status": "passed",
                "passed": True,
                "wall_seconds": duration,
                "commands": [
                    {
                        "command": f"pytest shard {idx}",
                        "wall_seconds": duration / 2.0,
                    }
                ],
            },
        )

    profile = build_verification_timing_profile(
        runs_dir=runs_dir,
        broker_timeout_guard_seconds=10_800.0,
        generated_utc="2026-07-06T00:00:00Z",
    )

    assert profile["run_count"] == 5
    assert profile["command_count"] == 5
    assert profile["run_wall_seconds"] == {
        "count": 5,
        "min": 60.0,
        "p05": 72.0,
        "median": 180.0,
        "mean": 180.0,
        "p95": 288.0,
        "max": 300.0,
    }
    recommendations = profile["recommendations"]
    assert recommendations["history_state"] == "sufficient"
    assert recommendations["recommended_initial_wait_seconds"] == 288.0
    assert recommendations["high_hang_guard_seconds"] == 10_800.0
    assert recommendations["insufficient_history_reason"] is None
    assert profile["slowest_commands"][0]["label"] == "pytest shard 5"
    assert profile["slowest_commands"][0]["artifact_path"].endswith("verification.json")


def test_sparse_history_returns_conservative_fallback_and_skips_workspaces(
    tmp_path: Path,
) -> None:
    runs_dir = tmp_path / "runs"
    _write_verification(
        runs_dir / "target" / "verification.json",
        {
            "schema_version": 1,
            "status": "passed",
            "passed": True,
            "wall_seconds": 123.0,
            "commands": [{"command": "pytest", "wall_seconds": 12.0}],
        },
    )
    _write_verification(
        runs_dir / "_workspaces" / "ignored" / "verification.json",
        {
            "schema_version": 1,
            "status": "passed",
            "passed": True,
            "wall_seconds": 9999.0,
            "commands": [{"command": "should not count", "wall_seconds": 9999.0}],
        },
    )

    profile = build_verification_timing_profile(
        runs_dir=runs_dir,
        broker_timeout_guard_seconds=10_800.0,
        generated_utc="2026-07-06T00:00:00Z",
    )

    assert profile["run_count"] == 1
    assert profile["command_count"] == 1
    assert profile["run_wall_seconds"]["max"] == 123.0
    assert profile["command_wall_seconds"]["max"] == 12.0
    recommendations = profile["recommendations"]
    assert recommendations["history_state"] == "insufficient"
    assert recommendations["basis"] == "insufficient_history_conservative_fallback"
    assert "Need at least 5" in recommendations["insufficient_history_reason"]
    assert recommendations["recommended_initial_wait_seconds"] == 900.0

