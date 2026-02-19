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


def normalize_gemini_events(
    *,
    raw_events_path: Path,
    normalized_events_path: Path,
    workspace_root: Path | None = None,
    workspace_mount: str | None = None,
) -> None:
    normalized_events_path.parent.mkdir(parents=True, exist_ok=True)

    tool_uses: dict[str, dict[str, Any]] = {}
    pending_message: str = ""

    def _flush_message() -> None:
        nonlocal pending_message
        if not pending_message:
            return
        event = make_event("agent_message", {"kind": "message", "text": pending_message})
        out_f.write(json.dumps(event, ensure_ascii=False) + "\n")
        pending_message = ""

    with normalized_events_path.open("w", encoding="utf-8", newline="\n") as out_f:
        for raw_line, payload in _iter_raw_lines(raw_events_path):
            if payload is None:
                _flush_message()
                event = make_event("error", {"category": "raw_non_json_line", "message": raw_line})
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
                    event = make_event("read_file", {"path": out_path, "bytes": bytes_read})
                    out_f.write(json.dumps(event, ensure_ascii=False) + "\n")
                continue

            if name in {"write_file", "replace"}:
                event = make_event(
                    "tool_call",
                    {
                        "name": str(tool_use.get("name", "")),
                        "input": tool_input,
                        "is_error": is_error,
                    },
                )
                out_f.write(json.dumps(event, ensure_ascii=False) + "\n")
                continue

            if name == "run_shell_command":
                cmd = tool_input.get("command")
                if isinstance(cmd, str) and cmd.strip():
                    argv = _split_command(cmd)
                    data: dict[str, Any] = {
                        "argv": argv,
                        "command": _format_argv(argv),
                        "exit_code": 1 if is_error else 0,
                    }
                    if is_error:
                        output_text = _join_streams(
                            payload.get("stdout") or payload.get("output") or payload.get("content"),
                            payload.get("stderr"),
                        )
                        if output_text:
                            excerpt, truncated = _excerpt_text(output_text)
                            data["output_excerpt"] = excerpt
                            if truncated:
                                data["output_excerpt_truncated"] = True
                    event = make_event("run_command", data)
                    out_f.write(json.dumps(event, ensure_ascii=False) + "\n")
                continue

            if name == "google_web_search":
                query = tool_input.get("query")
                if isinstance(query, str) and query.strip():
                    event = make_event("web_search", {"query": query.strip()})
                    out_f.write(json.dumps(event, ensure_ascii=False) + "\n")
                continue

            event = make_event(
                "tool_call",
                {
                    "name": str(tool_use.get("name", "")),
                    "input": tool_input,
                    "is_error": is_error,
                },
            )
            out_f.write(json.dumps(event, ensure_ascii=False) + "\n")

        _flush_message()
