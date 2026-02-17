from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from pathlib import PurePosixPath
from typing import Any

DOC_EXTS = {".md", ".rst", ".txt", ".adoc"}


def _looks_like_path(token: str) -> bool:
    if not token:
        return False
    if token.startswith("-") or token.startswith("/"):
        return False
    if "\\" in token or "/" in token:
        return True
    return "." in token and not token.startswith(".")


def _maybe_doc_path(path: str) -> bool:
    try:
        return PurePosixPath(path.replace("\\", "/")).suffix.lower() in DOC_EXTS
    except Exception:
        return False


def _infer_files_from_run_command(event: dict[str, Any]) -> set[str]:
    data = event.get("data", {})
    argv = data.get("argv")
    if not isinstance(argv, list):
        return set()

    files: set[str] = set()
    for token in argv[1:]:
        if isinstance(token, str) and _looks_like_path(token):
            files.add(token)
    return files


def compute_metrics(events: Iterable[dict[str, Any]]) -> dict[str, Any]:
    event_counts: Counter[str] = Counter()
    distinct_files_read: set[str] = set()
    distinct_docs_read: set[str] = set()
    distinct_files_written: set[str] = set()

    commands_executed = 0
    commands_failed = 0

    lines_added_total = 0
    lines_removed_total = 0

    step_count = 0

    for event in events:
        event_type = event.get("type")
        if not isinstance(event_type, str):
            continue

        event_counts[event_type] += 1

        if event_type in {"read_file", "write_file", "run_command", "web_search", "tool_call"}:
            step_count += 1

        data = event.get("data", {})
        if not isinstance(data, dict):
            continue

        if event_type == "read_file":
            path = data.get("path")
            if isinstance(path, str):
                distinct_files_read.add(path)
                if _maybe_doc_path(path):
                    distinct_docs_read.add(path)

        if event_type == "write_file":
            path = data.get("path")
            if isinstance(path, str):
                distinct_files_written.add(path)
            lines_added = data.get("lines_added")
            lines_removed = data.get("lines_removed")
            if isinstance(lines_added, int) and lines_added > 0:
                lines_added_total += lines_added
            if isinstance(lines_removed, int) and lines_removed > 0:
                lines_removed_total += lines_removed

        if event_type == "run_command":
            commands_executed += 1
            exit_code = data.get("exit_code")
            if isinstance(exit_code, int) and exit_code != 0:
                commands_failed += 1
            for inferred in _infer_files_from_run_command(event):
                distinct_files_read.add(inferred)
                if _maybe_doc_path(inferred):
                    distinct_docs_read.add(inferred)

    return {
        "event_counts": dict(event_counts),
        "distinct_files_read": sorted(distinct_files_read),
        "distinct_docs_read": sorted(distinct_docs_read),
        "distinct_files_written": sorted(distinct_files_written),
        "commands_executed": commands_executed,
        "commands_failed": commands_failed,
        "lines_added_total": lines_added_total,
        "lines_removed_total": lines_removed_total,
        "step_count": step_count,
    }
