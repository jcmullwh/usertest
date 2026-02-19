from __future__ import annotations

import json
import os
import shlex
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from agent_adapters.events import make_event

_MAX_OUTPUT_EXCERPT_CHARS = 2_000


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


def _coerce_tool_result_text(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        chunks = [item for item in value if isinstance(item, str) and item]
        return "\n".join(chunks) if chunks else None
    return None


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
    try:
        return shlex.split(command, posix=os.name != "nt")
    except ValueError:
        return command.split()


def _tool_name(raw: Any) -> str:
    return raw.strip().lower() if isinstance(raw, str) else ""


def normalize_claude_events(
    *,
    raw_events_path: Path,
    normalized_events_path: Path,
    ts_iter: Iterator[str] | None = None,
    workspace_root: Path | None = None,
    workspace_mount: str | None = None,
) -> None:
    normalized_events_path.parent.mkdir(parents=True, exist_ok=True)

    def _next_ts() -> str | None:
        if ts_iter is None:
            return None
        try:
            return next(ts_iter)
        except StopIteration:
            return None

    tool_uses: dict[str, dict[str, Any]] = {}

    with normalized_events_path.open("w", encoding="utf-8", newline="\n") as out_f:
        for raw_line, payload in _iter_raw_lines(raw_events_path):
            if payload is None:
                event = make_event(
                    "error",
                    {"category": "raw_non_json_line", "message": raw_line},
                    ts=_next_ts(),
                )
                out_f.write(json.dumps(event, ensure_ascii=False) + "\n")
                continue

            obj_type = payload.get("type")
            msg = payload.get("message")
            if not isinstance(msg, dict):
                continue

            role = msg.get("role") if isinstance(msg.get("role"), str) else ""
            content = msg.get("content")
            if not isinstance(content, list):
                continue

            for block in content:
                if not isinstance(block, dict):
                    continue

                block_type = block.get("type")

                if obj_type == "assistant" and role == "assistant" and block_type == "text":
                    text = block.get("text")
                    if isinstance(text, str) and text:
                        event = make_event(
                            "agent_message", {"kind": "message", "text": text}, ts=_next_ts()
                        )
                        out_f.write(json.dumps(event, ensure_ascii=False) + "\n")
                    continue

                if block_type == "tool_use":
                    tool_id = block.get("id")
                    name = block.get("name")
                    tool_input = block.get("input")
                    if isinstance(tool_id, str) and tool_id and isinstance(name, str):
                        tool_uses[tool_id] = {
                            "name": name,
                            "input": tool_input if isinstance(tool_input, dict) else {},
                        }
                    continue

                if block_type != "tool_result":
                    continue

                tool_use_id = block.get("tool_use_id") or block.get("id")
                if not isinstance(tool_use_id, str) or not tool_use_id:
                    continue

                tool_use = tool_uses.pop(tool_use_id, None)
                if tool_use is None:
                    event = make_event(
                        "error",
                        {
                            "category": "tool_result_missing_use",
                            "message": f"tool_use_id={tool_use_id}",
                        },
                        ts=_next_ts(),
                    )
                    out_f.write(json.dumps(event, ensure_ascii=False) + "\n")
                    continue

                name = _tool_name(tool_use.get("name"))
                tool_input = tool_use.get("input", {})
                if not isinstance(tool_input, dict):
                    tool_input = {}

                is_error = bool(block.get("is_error", False))

                if name == "bash":
                    cmd = tool_input.get("command") or tool_input.get("cmd")
                    if isinstance(cmd, str) and cmd.strip():
                        argv = _split_command(cmd)
                        output_excerpt = None
                        output_truncated = False
                        if is_error:
                            output_text = _coerce_tool_result_text(block.get("content"))
                            if isinstance(output_text, str) and output_text.strip():
                                excerpt, truncated = _excerpt_text(output_text.strip())
                                output_excerpt = excerpt
                                output_truncated = truncated
                        data: dict[str, Any] = {
                            "argv": argv,
                            "command": _format_argv(argv),
                            "exit_code": 1 if is_error else 0,
                        }
                        if output_excerpt is not None:
                            data["output_excerpt"] = output_excerpt
                            if output_truncated:
                                data["output_excerpt_truncated"] = True
                        event = make_event("run_command", data, ts=_next_ts())
                        out_f.write(json.dumps(event, ensure_ascii=False) + "\n")
                    continue

                if name == "read":
                    path_raw = tool_input.get("path") or tool_input.get("file_path")
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
                            candidate = (
                                candidate
                                if candidate.is_absolute()
                                else (workspace_root / candidate)
                            )
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

                if name in {"edit", "write"}:
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
                    continue

                if name in {"websearch", "web_search"}:
                    query = tool_input.get("query") or tool_input.get("text")
                    if isinstance(query, str) and query.strip():
                        event = make_event("web_search", {"query": query.strip()}, ts=_next_ts())
                        out_f.write(json.dumps(event, ensure_ascii=False) + "\n")
                    continue

                if name in {"grep", "glob"}:
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
                    continue

                event = make_event(
                    "error",
                    {"category": "unhandled_tool", "message": str(tool_use.get("name", ""))},
                    ts=_next_ts(),
                )
                out_f.write(json.dumps(event, ensure_ascii=False) + "\n")
