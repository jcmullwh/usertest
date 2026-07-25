from __future__ import annotations

import json
import os
import statistics
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MIN_HISTORY_RUNS_FOR_EXPECTED_WAIT = 5
DEFAULT_FALLBACK_INITIAL_WAIT_SECONDS = 900.0
DEFAULT_SUCCESS_MARGIN_SECONDS = 300.0
SLOWEST_COMMAND_LIMIT = 5


@dataclass(frozen=True)
class _RunRecord:
    wall_seconds: float
    artifact_path: Path
    passed: bool


@dataclass(frozen=True)
class _CommandRecord:
    wall_seconds: float
    artifact_path: Path
    label: str
    command: str | None


def _round_seconds(value: float) -> float:
    return round(max(0.0, float(value)), 2)


def percentile(values: list[float] | tuple[float, ...], fraction: float) -> float:
    """Return a linearly-interpolated percentile for ``values``.

    ``fraction`` is expressed from 0.0 to 1.0.  Empty input is invalid because
    callers need to decide whether to emit real history or an explicit fallback.
    """

    if not values:
        raise ValueError("percentile requires at least one value")
    sorted_values = sorted(float(value) for value in values)
    if len(sorted_values) == 1:
        return sorted_values[0]
    bounded = min(1.0, max(0.0, float(fraction)))
    position = bounded * (len(sorted_values) - 1)
    lower_index = int(position)
    upper_index = min(lower_index + 1, len(sorted_values) - 1)
    weight = position - lower_index
    lower = sorted_values[lower_index]
    upper = sorted_values[upper_index]
    return lower + ((upper - lower) * weight)


def _stats(values: list[float]) -> dict[str, Any] | None:
    if not values:
        return None
    return {
        "count": len(values),
        "min": _round_seconds(min(values)),
        "p05": _round_seconds(percentile(values, 0.05)),
        "median": _round_seconds(statistics.median(values)),
        "mean": _round_seconds(statistics.fmean(values)),
        "p95": _round_seconds(percentile(values, 0.95)),
        "max": _round_seconds(max(values)),
    }


def _iter_verification_json_paths(root: Path) -> list[Path]:
    paths: list[Path] = []
    if not root.exists():
        return paths
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            dirname
            for dirname in dirnames
            if dirname not in {"_workspaces", ".git", ".venv", "__pycache__"}
        ]
        if "verification.json" not in filenames:
            continue
        paths.append(Path(dirpath) / "verification.json")
    paths.sort(key=lambda path: path.stat().st_mtime if path.exists() else 0.0, reverse=True)
    return paths


def _coerce_seconds(value: Any) -> float | None:
    if not isinstance(value, (int, float)):
        return None
    seconds = float(value)
    if seconds < 0.0:
        return None
    return seconds


def _is_passed(payload: dict[str, Any]) -> bool:
    status = payload.get("status")
    return bool(payload.get("passed") is True or status == "passed")


def _command_label(item: dict[str, Any], *, index: int) -> tuple[str, str | None]:
    command = item.get("command")
    command_s = command.strip() if isinstance(command, str) and command.strip() else None
    label = item.get("label")
    label_s = label.strip() if isinstance(label, str) and label.strip() else None
    if label_s is None:
        label_s = command_s or f"command #{index}"
    return label_s[:240], command_s[:500] if command_s is not None else None


def _records_from_payload(
    payload: dict[str, Any],
    *,
    artifact_path: Path,
) -> tuple[_RunRecord | None, list[_CommandRecord]]:
    skipped = bool(payload.get("skipped") is True or payload.get("status") == "disabled")
    run_seconds = None if skipped else _coerce_seconds(payload.get("wall_seconds"))
    run_record = (
        _RunRecord(
            wall_seconds=run_seconds,
            artifact_path=artifact_path,
            passed=_is_passed(payload),
        )
        if run_seconds is not None
        else None
    )

    command_records: list[_CommandRecord] = []
    commands = payload.get("commands")
    if isinstance(commands, list):
        for index, item in enumerate(commands, start=1):
            if not isinstance(item, dict):
                continue
            command_seconds = _coerce_seconds(item.get("wall_seconds"))
            if command_seconds is None:
                continue
            label, command = _command_label(item, index=index)
            command_records.append(
                _CommandRecord(
                    wall_seconds=command_seconds,
                    artifact_path=artifact_path,
                    label=label,
                    command=command,
                )
            )
    return run_record, command_records


def _load_records(
    paths: list[Path],
    *,
    exclude_paths: set[Path],
    max_artifacts: int,
) -> tuple[int, list[_RunRecord], list[_CommandRecord]]:
    run_records: list[_RunRecord] = []
    command_records: list[_CommandRecord] = []
    scanned = 0
    for path in paths:
        try:
            resolved = path.resolve()
        except OSError:
            resolved = path
        if any(resolved == excluded or excluded in resolved.parents for excluded in exclude_paths):
            continue
        if scanned >= max_artifacts:
            break
        scanned += 1
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        if not isinstance(payload, dict):
            continue
        run_record, records = _records_from_payload(payload, artifact_path=path)
        if run_record is not None:
            run_records.append(run_record)
        command_records.extend(records)
    return scanned, run_records, command_records


def _recommendations(
    *,
    run_stats: dict[str, Any] | None,
    run_records: list[_RunRecord],
    broker_timeout_guard_seconds: float,
) -> dict[str, Any]:
    successful_run_seconds = [record.wall_seconds for record in run_records if record.passed]
    enough_history = (
        run_stats is not None
        and int(run_stats["count"]) >= MIN_HISTORY_RUNS_FOR_EXPECTED_WAIT
    )
    max_successful = max(successful_run_seconds) if successful_run_seconds else None
    high_hang_guard = float(broker_timeout_guard_seconds)
    if max_successful is not None:
        high_hang_guard = max(
            high_hang_guard,
            max_successful + max(DEFAULT_SUCCESS_MARGIN_SECONDS, max_successful * 0.25),
        )

    if enough_history and run_stats is not None:
        recommended_initial_wait = float(run_stats["p95"])
        reason = "sufficient_history_p95"
        insufficient_reason = None
        low = float(run_stats["p05"])
        typical = float(run_stats["median"])
        high = float(run_stats["p95"])
    else:
        observed_max = max((record.wall_seconds for record in run_records), default=0.0)
        recommended_initial_wait = max(
            DEFAULT_FALLBACK_INITIAL_WAIT_SECONDS,
            observed_max + DEFAULT_SUCCESS_MARGIN_SECONDS if observed_max > 0.0 else 0.0,
        )
        recommended_initial_wait = min(recommended_initial_wait, high_hang_guard)
        reason = "insufficient_history_conservative_fallback"
        insufficient_reason = (
            f"Need at least {MIN_HISTORY_RUNS_FOR_EXPECTED_WAIT} completed verification "
            f"runs with wall_seconds; found {len(run_records)}."
        )
        low = float(run_stats["p05"]) if run_stats is not None else 0.0
        typical = float(run_stats["median"]) if run_stats is not None else recommended_initial_wait
        high = float(run_stats["p95"]) if run_stats is not None else recommended_initial_wait

    return {
        "history_state": "sufficient" if enough_history else "insufficient",
        "insufficient_history_reason": insufficient_reason,
        "recommended_initial_wait_seconds": _round_seconds(recommended_initial_wait),
        "reasonable_check_after_seconds": _round_seconds(recommended_initial_wait),
        "high_hang_guard_seconds": _round_seconds(high_hang_guard),
        "expected_duration_range_seconds": {
            "low": _round_seconds(low),
            "typical": _round_seconds(typical),
            "high": _round_seconds(high),
        },
        "basis": reason,
        "notes": [
            "recommended_initial_wait_seconds is an expected wait before a model-side check",
            "high_hang_guard_seconds is a hang guard, not an expected duration",
        ],
    }


def build_verification_timing_profile(
    *,
    runs_dir: Path,
    broker_timeout_guard_seconds: float,
    generated_utc: str,
    current_run_dir: Path | None = None,
    max_artifacts: int = 200,
) -> dict[str, Any]:
    """Build a compact timing profile from recent ``verification.json`` artifacts."""

    exclude_paths: set[Path] = set()
    if current_run_dir is not None:
        try:
            exclude_paths.add(current_run_dir.resolve())
        except OSError:
            exclude_paths.add(current_run_dir)

    all_paths = _iter_verification_json_paths(runs_dir)
    scanned, run_records, command_records = _load_records(
        all_paths,
        exclude_paths=exclude_paths,
        max_artifacts=max(1, int(max_artifacts)),
    )

    run_seconds = [record.wall_seconds for record in run_records]
    command_seconds = [record.wall_seconds for record in command_records]
    run_stats = _stats(run_seconds)
    command_stats = _stats(command_seconds)
    recommendations = _recommendations(
        run_stats=run_stats,
        run_records=run_records,
        broker_timeout_guard_seconds=broker_timeout_guard_seconds,
    )

    slowest_commands = sorted(
        command_records,
        key=lambda record: record.wall_seconds,
        reverse=True,
    )[:SLOWEST_COMMAND_LIMIT]

    return {
        "schema_version": 1,
        "generated_utc": generated_utc,
        "source": {
            "runs_dir": str(runs_dir),
            "excluded_directory_names": ["_workspaces"],
            "scanned_artifact_count": scanned,
            "max_artifacts": max(1, int(max_artifacts)),
        },
        "run_count": len(run_records),
        "command_count": len(command_records),
        "run_wall_seconds": run_stats,
        "command_wall_seconds": command_stats,
        "slowest_commands": [
            {
                "label": record.label,
                "command": record.command,
                "wall_seconds": _round_seconds(record.wall_seconds),
                "artifact_path": str(record.artifact_path),
            }
            for record in slowest_commands
        ],
        "recommendations": recommendations,
    }
