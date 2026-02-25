from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from agent_adapters.events import make_event
from agent_adapters.failure_artifacts import (
    write_command_failure_artifacts,
    write_tool_failure_artifacts,
)

_MAX_OUTPUT_EXCERPT_CHARS = 2_000
_MAX_TOOL_CONTEXT_BYTES = 1_000_000

_OCCURRENCES_RE = re.compile(
    r"expected\\s+(?P<expected>\\d+)\\s+occurrences?\\b.*?found\\s+(?P<found>\\d+)\\b",
    re.IGNORECASE | re.DOTALL,
)


def _excerpt_text(text: str, *, max_chars: int = _MAX_OUTPUT_EXCERPT_CHARS) -> tuple[str, bool]:
    if len(text) <= max_chars:
        return text, False
    marker = "\n...[truncated_output]...\n"
    available = max_chars - len(marker)
    if available <= 0:
        return text[:max_chars], True
    head_chars = available // 2
    tail_chars = available - head_chars
    return text[:head_chars] + marker + text[-tail_chars:], True


def _join_streams(stdout: Any, stderr: Any) -> str:
    parts: list[str] = []
    if isinstance(stdout, str) and stdout.strip():
        parts.append("[stdout]\n" + stdout.rstrip())
    if isinstance(stderr, str) and stderr.strip():
        parts.append("[stderr]\n" + stderr.rstrip())
    return "\n".join(parts).strip()


def _format_argv(argv: list[str]) -> str:
    if os.name == "nt":
        return subprocess.list2cmdline(argv)
    return " ".join(shlex.quote(a) for a in argv)


def _iter_raw_lines(path: Path) -> Iterator[tuple[str, dict[str, Any] | None]]:
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            raw = line.rstrip("\n")
            if not raw.strip():
                continue
            try:
                yield raw, json.loads(raw)
            except json.JSONDecodeError:
                yield raw, None


def _safe_relpath(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except Exception:
        return path.as_posix()


def _normalize_workspace_mount(workspace_mount: str | None) -> str | None:
    if workspace_mount is None:
        return None
    mount = workspace_mount.strip().replace("\\", "/").rstrip("/")
    if not mount:
        return None
    return mount if mount.startswith("/") else f"/{mount}"


def _map_sandbox_path_str(
    path_str: str, *, workspace_root: Path, workspace_mount: str | None
) -> Path:
    mount = _normalize_workspace_mount(workspace_mount)
    if mount is None:
        return Path(path_str)

    posixish = path_str.replace("\\", "/")
    if posixish == mount:
        return workspace_root
    if posixish.startswith(f"{mount}/"):
        rel = posixish[len(mount) + 1 :]
        rel_path = Path(*[p for p in rel.split("/") if p])
        return workspace_root / rel_path

    return Path(path_str)


def _split_command(command: str) -> list[str]:
    # Gemini sandbox runs a POSIX-like shell even on Windows hosts.
    try:
        return shlex.split(command, posix=True)
    except ValueError:
        return command.split()


def _tool_name(raw: Any) -> str:
    return raw.strip().lower() if isinstance(raw, str) else ""


def _coerce_text(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value
    return None


def _extract_expected_found(error_text: str) -> tuple[int | None, int | None]:
    match = _OCCURRENCES_RE.search(error_text)
    if match is None:
        return None, None
    try:
        expected = int(match.group("expected"))
        found = int(match.group("found"))
    except Exception:
        return None, None
    return expected, found


def normalize_gemini_events(
    *,
    raw_events_path: Path,
    normalized_events_path: Path,
    ts_iter: Iterator[str] | None = None,
    raw_ts_iter: Iterator[str] | None = None,
    workspace_root: Path | None = None,
    workspace_mount: str | None = None,
) -> None:
    normalized_events_path.parent.mkdir(parents=True, exist_ok=True)
    run_dir = normalized_events_path.parent
    command_failure_idx = 0
    tool_failure_idx = 0

    def _next_raw_ts() -> str | None:
        if raw_ts_iter is None:
            return None
        try:
            return next(raw_ts_iter)
        except StopIteration:
            return None

    line_ts: str | None = None

    def _next_ts() -> str | None:
        if ts_iter is not None:
            try:
                return next(ts_iter)
            except StopIteration:
                return None
        return line_ts

    tool_uses: dict[str, dict[str, Any]] = {}
    pending_message: str = ""
    pending_message_ts: str | None = None

    def _flush_message() -> None:
        nonlocal pending_message, pending_message_ts
        if not pending_message:
            pending_message_ts = None
            return
        event_ts = _next_ts() if ts_iter is not None else pending_message_ts
        event = make_event(
            "agent_message", {"kind": "message", "text": pending_message}, ts=event_ts
        )
        out_f.write(json.dumps(event, ensure_ascii=False) + "\n")
        pending_message = ""
        pending_message_ts = None

    with normalized_events_path.open("w", encoding="utf-8", newline="\n") as out_f:
        for raw_line, payload in _iter_raw_lines(raw_events_path):
            if ts_iter is None:
                line_ts = _next_raw_ts()
            else:
                line_ts = None
            if payload is None:
                _flush_message()
                event = make_event(
                    "error",
                    {"category": "raw_non_json_line", "message": raw_line},
                    ts=_next_ts(),
                )
                out_f.write(json.dumps(event, ensure_ascii=False) + "\n")
                continue

            event_type = payload.get("type")

            if event_type == "message":
                role = payload.get("role")
                content = payload.get("content")
                if role == "assistant" and isinstance(content, str) and content:
                    if payload.get("delta") is True:
                        pending_message += content
                    else:
                        pending_message = content
                    if ts_iter is None:
                        pending_message_ts = line_ts
                else:
                    _flush_message()
                continue

            if event_type == "tool_use":
                _flush_message()
                tool_id = payload.get("tool_id")
                name = payload.get("tool_name")
                params = payload.get("parameters")
                if isinstance(tool_id, str) and tool_id and isinstance(name, str):
                    tool_uses[tool_id] = {
                        "name": name,
                        "input": params if isinstance(params, dict) else {},
                    }
                continue

            if event_type != "tool_result":
                _flush_message()
                continue

            _flush_message()
            tool_id = payload.get("tool_id")
            if not isinstance(tool_id, str) or not tool_id:
                continue

            tool_use = tool_uses.pop(tool_id, None)
            if tool_use is None:
                event = make_event(
                    "error",
                    {"category": "tool_result_missing_use", "message": f"tool_id={tool_id}"},
                    ts=_next_ts(),
                )
                out_f.write(json.dumps(event, ensure_ascii=False) + "\n")
                continue

            name = _tool_name(tool_use.get("name"))
            tool_input = tool_use.get("input", {})
            if not isinstance(tool_input, dict):
                tool_input = {}

            status = payload.get("status")
            is_error = not (isinstance(status, str) and status.lower() == "success")

            if name == "read_file":
                path_raw = tool_input.get("file_path")
                if isinstance(path_raw, str) and path_raw.strip():
                    path_str = path_raw.strip()
                    bytes_read = -1
                    out_path = path_str
                    if workspace_root is not None:
                        candidate = _map_sandbox_path_str(
                            path_str,
                            workspace_root=workspace_root,
                            workspace_mount=workspace_mount,
                        )
                        if not candidate.is_absolute():
                            candidate = workspace_root / candidate
                        if candidate.exists() and candidate.is_file():
                            bytes_read = candidate.stat().st_size
                            out_path = _safe_relpath(candidate, workspace_root)
                    event = make_event(
                        "read_file",
                        {"path": out_path, "bytes": bytes_read},
                        ts=_next_ts(),
                    )
                    out_f.write(json.dumps(event, ensure_ascii=False) + "\n")
                continue

            if name in {"write_file", "replace"}:
                event_data: dict[str, Any] = {
                    "name": str(tool_use.get("name", "")),
                    "input": tool_input,
                    "is_error": is_error,
                }
                if is_error:
                    tool_failure_idx += 1
                    error_text = (
                        _coerce_text(payload.get("output"))
                        or _coerce_text(payload.get("content"))
                        or _coerce_text(payload.get("stderr"))
                    )
                    if error_text is not None:
                        excerpt, truncated = _excerpt_text(error_text)
                        event_data["error_excerpt"] = excerpt
                        if truncated:
                            event_data["error_excerpt_truncated"] = True

                    extracted: dict[str, Any] = {}
                    file_path_raw = tool_input.get("file_path") or tool_input.get("path")
                    old_string = tool_input.get("old_string") or tool_input.get("old")
                    new_string = tool_input.get("new_string") or tool_input.get("new")
                    expected = tool_input.get("expected_replacements") or tool_input.get(
                        "expected_occurrences"
                    )

                    if isinstance(file_path_raw, str) and file_path_raw.strip():
                        extracted["file_path"] = file_path_raw.strip()
                    if isinstance(expected, int):
                        extracted["expected_occurrences"] = int(expected)

                    if error_text is not None:
                        expected_parsed, found_parsed = _extract_expected_found(error_text)
                        if expected_parsed is not None:
                            extracted["expected_occurrences_from_error"] = expected_parsed
                        if found_parsed is not None:
                            extracted["found_occurrences_from_error"] = found_parsed
                        if "could not find the string to replace" in error_text.lower():
                            extracted["found_occurrences_from_error"] = 0

                    context_text: str | None = None
                    if (
                        workspace_root is not None
                        and isinstance(file_path_raw, str)
                        and file_path_raw.strip()
                        and isinstance(old_string, str)
                        and old_string
                    ):
                        candidate = _map_sandbox_path_str(
                            file_path_raw.strip(),
                            workspace_root=workspace_root,
                            workspace_mount=workspace_mount,
                        )
                        candidate = (
                            candidate if candidate.is_absolute() else (workspace_root / candidate)
                        )
                        try:
                            if candidate.exists() and candidate.is_file():
                                if candidate.stat().st_size <= _MAX_TOOL_CONTEXT_BYTES:
                                    file_text = candidate.read_text(
                                        encoding="utf-8", errors="replace"
                                    )
                                    found_occurrences = file_text.count(old_string)
                                    extracted["found_occurrences"] = found_occurrences
                                    idx = file_text.find(old_string)
                                    if idx >= 0:
                                        before = max(0, idx - 200)
                                        after = min(len(file_text), idx + len(old_string) + 200)
                                        snippet = file_text[before:after]
                                        context_text = "\n".join(
                                            [
                                                f"file={_safe_relpath(candidate, workspace_root)}",
                                                f"found_occurrences={found_occurrences}",
                                                "",
                                                snippet,
                                                "",
                                            ]
                                        )
                                        if isinstance(new_string, str) and new_string:
                                            preview_old = old_string
                                            preview_new = new_string
                                            if len(preview_old) > 400:
                                                preview_old = preview_old[:400] + "...(truncated)"
                                            if len(preview_new) > 400:
                                                preview_new = preview_new[:400] + "...(truncated)"
                                            extracted["preview"] = {
                                                "old_string_excerpt": preview_old,
                                                "new_string_excerpt": preview_new,
                                            }
                        except OSError:
                            pass

                    event_data["failure_artifacts"] = write_tool_failure_artifacts(
                        run_dir=run_dir,
                        failure_index=tool_failure_idx,
                        tool_name=name,
                        tool_input=tool_input,
                        error_text=error_text,
                        extracted=extracted if extracted else None,
                        context_text=context_text,
                        preview_text=None,
                    )

                event = make_event(
                    "tool_call",
                    event_data,
                    ts=_next_ts(),
                )
                out_f.write(json.dumps(event, ensure_ascii=False) + "\n")
                continue

            if name == "run_shell_command":
                cmd = tool_input.get("command")
                if isinstance(cmd, str) and cmd.strip():
                    argv = _split_command(cmd)
                    exit_code = payload.get("exit_code")
                    if not isinstance(exit_code, int):
                        exit_code = 1 if is_error else 0
                    data: dict[str, Any] = {
                        "argv": argv,
                        "command": _format_argv(argv),
                        "exit_code": exit_code,
                    }
                    if isinstance(tool_input.get("cwd"), str) and tool_input.get("cwd").strip():
                        data["cwd"] = tool_input.get("cwd").strip()

                    if isinstance(exit_code, int) and exit_code != 0:
                        command_failure_idx += 1
                        primary_stream = (
                            payload.get("stdout")
                            or payload.get("output")
                            or payload.get("content")
                        )
                        stdout_text = (
                            _coerce_text(payload.get("stdout"))
                            or _coerce_text(payload.get("output"))
                            or _coerce_text(payload.get("content"))
                            or ""
                        )
                        stderr_text = _coerce_text(payload.get("stderr")) or ""
                        output_text = _join_streams(
                            primary_stream,
                            payload.get("stderr"),
                        )
                        if output_text:
                            excerpt, truncated = _excerpt_text(output_text)
                            data["output_excerpt"] = excerpt
                            if truncated:
                                data["output_excerpt_truncated"] = True
                        data["failure_artifacts"] = write_command_failure_artifacts(
                            run_dir=run_dir,
                            failure_index=command_failure_idx,
                            command=_format_argv(argv),
                            argv=argv,
                            cwd=data.get("cwd") if isinstance(data.get("cwd"), str) else None,
                            exit_code=exit_code,
                            stdout_text=stdout_text,
                            stderr_text=stderr_text,
                            duration=None,
                        )
                    event = make_event("run_command", data, ts=_next_ts())
                    out_f.write(json.dumps(event, ensure_ascii=False) + "\n")
                continue

            if name == "google_web_search":
                query = tool_input.get("query")
                if isinstance(query, str) and query.strip():
                    event = make_event("web_search", {"query": query.strip()}, ts=_next_ts())
                    out_f.write(json.dumps(event, ensure_ascii=False) + "\n")
                continue

            event = make_event(
                "tool_call",
                {
                    "name": str(tool_use.get("name", "")),
                    "input": tool_input,
                    "is_error": is_error,
                },
                ts=_next_ts(),
            )
            out_f.write(json.dumps(event, ensure_ascii=False) + "\n")

        _flush_message()
