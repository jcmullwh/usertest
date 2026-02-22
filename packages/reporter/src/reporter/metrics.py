from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from pathlib import PurePosixPath
from typing import Any

DOC_EXTS = {".md", ".rst", ".txt", ".adoc"}
_MAX_FAILED_COMMANDS = 10


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
    failed_commands: list[dict[str, Any]] = []
    failed_commands_total = 0

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
                failed_commands_total += 1
                if len(failed_commands) < _MAX_FAILED_COMMANDS:
                    command = data.get("command")
                    if not isinstance(command, str) or not command.strip():
                        command = " ".join(str(tok) for tok in data.get("argv", []) if tok)
                    entry: dict[str, Any] = {"command": command, "exit_code": exit_code}
                    cwd = data.get("cwd")
                    if isinstance(cwd, str) and cwd.strip():
                        entry["cwd"] = cwd.strip()
                    output_excerpt = data.get("output_excerpt")
                    if isinstance(output_excerpt, str) and output_excerpt.strip():
                        excerpt = output_excerpt.strip()
                        entry["output_excerpt"] = excerpt
                        if data.get("output_excerpt_truncated") is True:
                            entry["output_excerpt_truncated"] = True
                        lowered_excerpt = excerpt.lower()
                        if (
                            "tool execution denied by policy" in lowered_excerpt
                            or "denied by policy" in lowered_excerpt
                        ):
                            entry["failure_category"] = "policy_denial"
                            argv = data.get("argv")
                            argv_list = argv if isinstance(argv, list) else []
                            has_heredoc = "<<" in command or any(
                                isinstance(tok, str) and tok.strip().startswith("<<")
                                for tok in argv_list
                            )
                            if has_heredoc:
                                entry["policy_category"] = "bash_heredoc_unsupported"
                                entry["hint"] = (
                                    "Avoid heredocs (for example `<<EOF`) in "
                                    "sandboxed shell commands. "
                                    "Use file tools like write_file/replace "
                                    "for multiline content."
                                )
                            else:
                                entry["policy_category"] = "policy_denied"
                                entry["hint"] = (
                                    "This command was blocked by sandbox/policy. "
                                    "Consult preflight.json for allowed capabilities "
                                    "or rewrite using file tools."
                                )
                    failed_commands.append(entry)
            for inferred in _infer_files_from_run_command(event):
                distinct_files_read.add(inferred)
                if _maybe_doc_path(inferred):
                    distinct_docs_read.add(inferred)

    metrics: dict[str, Any] = {
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

    if failed_commands_total:
        metrics["failed_commands"] = failed_commands
        omitted = max(0, int(failed_commands_total) - len(failed_commands))
        if omitted:
            metrics["failed_commands_truncated"] = True
            metrics["failed_commands_omitted_count"] = omitted
            metrics["failed_commands_max"] = _MAX_FAILED_COMMANDS

    return metrics
