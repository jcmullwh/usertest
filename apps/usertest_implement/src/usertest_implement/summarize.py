from __future__ import annotations

import json
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from run_artifacts.history import iter_report_history

_DEFAULT_TEST_COMMAND_PATTERNS = (
    r"(^|\s)pytest(\s|$)",
    r"(^|\s)python\s+-m\s+pytest(\s|$)",
    r"(^|\s)npm\s+test(\s|$)",
    r"(^|\s)pnpm\s+test(\s|$)",
    r"(^|\s)yarn\s+test(\s|$)",
    r"(^|\s)go\s+test(\s|$)",
    r"(^|\s)cargo\s+test(\s|$)",
)


@dataclass(frozen=True)
class TestHeuristics:
    test_runs_total: int
    test_runs_failed_before_success: int


def _coerce_int(value: Any) -> int | None:
    if isinstance(value, int):
        return value
    return None


def _coerce_float(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _load_events(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    events: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as handle:
            for raw_line in handle:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(event, dict):
                    events.append(event)
    except OSError:
        return []
    return events


def _event_command_string(event: dict[str, Any]) -> str | None:
    data = event.get("data")
    if not isinstance(data, dict):
        return None
    cmd = data.get("command")
    if isinstance(cmd, str) and cmd.strip():
        return cmd.strip()
    argv = data.get("argv")
    if isinstance(argv, list) and argv:
        tokens = [str(tok) for tok in argv if tok is not None]
        joined = " ".join(tokens).strip()
        return joined if joined else None
    return None


def _compute_test_heuristics(
    *,
    run_dir: Path,
    test_command_regexes: list[str] | None = None,
) -> TestHeuristics:
    patterns = (
        list(test_command_regexes)
        if test_command_regexes is not None
        else list(_DEFAULT_TEST_COMMAND_PATTERNS)
    )
    combined = re.compile("|".join(f"(?:{p})" for p in patterns), flags=re.IGNORECASE)

    events = _load_events(run_dir / "normalized_events.jsonl")
    test_exit_codes: list[int] = []
    for event in events:
        if event.get("type") != "run_command":
            continue
        command = _event_command_string(event)
        if command is None or not combined.search(command):
            continue
        data = event.get("data")
        if not isinstance(data, dict):
            continue
        exit_code = _coerce_int(data.get("exit_code"))
        if exit_code is None:
            continue
        test_exit_codes.append(exit_code)

    first_success_idx: int | None = None
    for idx, code in enumerate(test_exit_codes):
        if code == 0:
            first_success_idx = idx
            break

    if first_success_idx is None:
        failures_before_success = sum(1 for code in test_exit_codes if code != 0)
    else:
        failures_before_success = sum(
            1 for code in test_exit_codes[:first_success_idx] if code != 0
        )

    return TestHeuristics(
        test_runs_total=len(test_exit_codes),
        test_runs_failed_before_success=failures_before_success,
    )


def iter_implementation_rows(
    runs_dir: Path,
    *,
    target_slug: str | None = None,
    repo_input: str | None = None,
    test_command_regexes: list[str] | None = None,
) -> Iterator[dict[str, Any]]:
    for record in iter_report_history(
        runs_dir,
        target_slug=target_slug,
        repo_input=repo_input,
        embed="none",
    ):
        run_dir_raw = record.get("run_dir")
        run_dir = Path(run_dir_raw) if isinstance(run_dir_raw, str) else None

        ticket_ref = record.get("ticket_ref")
        ticket_ref_dict = ticket_ref if isinstance(ticket_ref, dict) else {}
        timing = record.get("timing")
        timing_dict = timing if isinstance(timing, dict) else {}

        started_at = (
            timing_dict.get("started_at")
            if isinstance(timing_dict.get("started_at"), str)
            else None
        )
        finished_at = (
            timing_dict.get("finished_at")
            if isinstance(timing_dict.get("finished_at"), str)
            else None
        )
        duration_seconds = _coerce_float(timing_dict.get("duration_seconds"))

        metrics = record.get("metrics")
        metrics_dict = metrics if isinstance(metrics, dict) else {}

        target_ref = record.get("target_ref")
        target_ref_dict = target_ref if isinstance(target_ref, dict) else {}

        heuristics = (
            _compute_test_heuristics(run_dir=run_dir, test_command_regexes=test_command_regexes)
            if run_dir is not None
            else TestHeuristics(test_runs_total=0, test_runs_failed_before_success=0)
        )

        distinct_files_written = metrics_dict.get("distinct_files_written")
        files_written = (
            len(distinct_files_written) if isinstance(distinct_files_written, list) else None
        )

        yield {
            "schema_version": 1,
            "ticket": {
                "fingerprint": ticket_ref_dict.get("fingerprint"),
                "title": ticket_ref_dict.get("title"),
            },
            "repo": {
                "target_slug": record.get("target_slug"),
                "commit_sha": target_ref_dict.get("commit_sha"),
            },
            "run": {
                "run_dir": record.get("run_dir"),
                "run_rel": record.get("run_rel"),
                "timestamp_utc": record.get("timestamp_utc"),
                "started_at": started_at,
                "finished_at": finished_at,
                "duration_seconds": duration_seconds,
            },
            "outcomes": {
                "status": record.get("status"),
                "agent_exit_code": record.get("agent_exit_code"),
                "has_error_json": record.get("error") is not None,
            },
            "metrics": {
                "step_count": metrics_dict.get("step_count"),
                "commands_failed": metrics_dict.get("commands_failed"),
                "files_written": files_written,
                "lines_added_total": metrics_dict.get("lines_added_total"),
                "lines_removed_total": metrics_dict.get("lines_removed_total"),
            },
            "heuristics": {
                "test_runs_total": heuristics.test_runs_total,
                "test_runs_failed_before_success": heuristics.test_runs_failed_before_success,
            },
        }


def write_jsonl(rows: Iterable[dict[str, Any]], out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
