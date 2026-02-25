from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

from agent_adapters.events import make_event
from agent_adapters.failure_artifacts import write_command_failure_artifacts

READLIKE_COMMANDS = {
    "cat",
    "type",
    "sed",
    "find",
    "findstr",
    "rg",
    "grep",
    "more",
    "head",
    "tail",
}
CHAIN_OPERATORS = {"&&", ";", "||", "|"}
_WINDOWS_DRIVE_POSIX_RE = re.compile(r"^/([a-zA-Z])/(.*)$")
_WINDOWS_DRIVE_CLEAN_RE = re.compile(r"^([a-zA-Z]):/{2,}")
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


def _strip_windows_extended_prefix(path_str: str) -> str:
    return path_str[4:] if path_str.startswith("\\\\?\\") else path_str


def _render_path(path: Path) -> str:
    rendered = str(path).replace("\\", "/")
    return _WINDOWS_DRIVE_CLEAN_RE.sub(r"\1:/", rendered)


def _maybe_windows_drive_posix_path(path_str: str) -> Path | None:
    posixish = path_str.replace("\\", "/")
    match = _WINDOWS_DRIVE_POSIX_RE.match(posixish)
    if match is None:
        return None
    drive = match.group(1).upper()
    remainder = match.group(2)
    return Path(f"{drive}:/{remainder}")


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
    win_drive = _maybe_windows_drive_posix_path(path_str)
    if win_drive is not None:
        return win_drive

    mount = _normalize_workspace_mount(workspace_mount)
    if mount is None:
        return Path(_strip_windows_extended_prefix(path_str))

    posixish = path_str.replace("\\", "/")
    if posixish == mount:
        return workspace_root
    if posixish.startswith(f"{mount}/"):
        rel = posixish[len(mount) + 1 :]
        rel_path = Path(*[p for p in rel.split("/") if p])
        return workspace_root / rel_path

    return Path(_strip_windows_extended_prefix(path_str))


def _iter_codex_raw_lines(path: Path) -> Iterator[tuple[str, dict[str, Any] | None]]:
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            raw = line.rstrip("\n")
            if not raw.strip():
                continue
            try:
                yield raw, json.loads(raw)
            except json.JSONDecodeError:
                yield raw, None


def _split_command(command: str) -> list[str]:
    # Codex commands frequently run through a POSIX shell wrapper (even on Windows hosts when
    # sandboxed). Prefer POSIX parsing but fall back to a conservative split.
    try:
        return shlex.split(command, posix=True)
    except ValueError:
        try:
            return shlex.split(command, posix=False)
        except ValueError:
            return command.split()


def _maybe_unwrap_shell_command(argv: list[str]) -> list[str]:
    if len(argv) < 3:
        return argv

    exe = argv[0].replace("\\", "/").lower()
    base = exe.rsplit("/", 1)[-1]
    arg1 = argv[1].lower()

    if base in {"bash", "sh"} and arg1 in {"-lc", "-c"}:
        inner = argv[2]
        if isinstance(inner, str) and inner.strip():
            inner_argv = _split_command(inner)
            return inner_argv or argv
        return argv

    if base in {"cmd", "cmd.exe"} and arg1 == "/c":
        inner = argv[2]
        if isinstance(inner, str) and inner.strip():
            inner_argv = _split_command(inner)
            return inner_argv or argv
        return argv

    if base in {"powershell", "powershell.exe", "pwsh", "pwsh.exe"} and arg1 in {
        "-command",
        "-c",
    }:
        inner = argv[2]
        if isinstance(inner, str) and inner.strip():
            inner_argv = _split_command(inner)
            return inner_argv or argv
        return argv

    return argv


def _split_chain_segments(argv: list[str]) -> list[list[str]]:
    segments: list[list[str]] = []
    current: list[str] = []
    for token in argv:
        if token in CHAIN_OPERATORS:
            if current:
                segments.append(current)
                current = []
            continue
        current.append(token)
    if current:
        segments.append(current)
    return segments


def _resolve_candidate_path(
    token: str,
    *,
    base_dir: Path,
    workspace_root: Path,
    workspace_mount: str | None,
) -> Path | None:
    if token.startswith("/"):
        win_drive = _maybe_windows_drive_posix_path(token)
        if win_drive is not None:
            return win_drive

        mount = _normalize_workspace_mount(workspace_mount)
        if mount is not None:
            return _map_sandbox_path_str(
                token,
                workspace_root=workspace_root,
                workspace_mount=workspace_mount,
            )
        if os.name == "nt":
            return None
        return Path(token)

    p = Path(token)
    return p if p.is_absolute() else (base_dir / p)


def _infer_read_candidate_paths(
    *,
    argv: list[str],
    cwd: Path | None,
    workspace_root: Path,
    workspace_mount: str | None,
) -> list[Path]:
    if not argv:
        return []

    segments = _split_chain_segments(argv)
    if not segments:
        return []

    effective_cwd = cwd if cwd is not None else workspace_root
    candidates: list[Path] = []

    for segment in segments:
        if not segment:
            continue
        cmd = segment[0].lower()

        if cmd == "cd":
            if len(segment) >= 2:
                target = _resolve_candidate_path(
                    segment[1],
                    base_dir=effective_cwd,
                    workspace_root=workspace_root,
                    workspace_mount=workspace_mount,
                )
                if target is not None:
                    effective_cwd = target
            continue

        if cmd not in READLIKE_COMMANDS:
            continue

        for token in segment[1:]:
            if not isinstance(token, str) or not token:
                continue
            if token.startswith("-"):
                continue
            candidate = _resolve_candidate_path(
                token,
                base_dir=effective_cwd,
                workspace_root=workspace_root,
                workspace_mount=workspace_mount,
            )
            if candidate is not None:
                candidates.append(candidate)

    return candidates


def _safe_relpath(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except Exception:
        return path.as_posix()


def _maybe_emit_read_events(
    *,
    argv: list[str],
    cwd: Path | None,
    workspace_root: Path | None,
    workspace_mount: str | None,
    ts_iter: Iterator[str] | None,
    fallback_ts: str | None = None,
) -> Iterable[dict[str, Any]]:
    if workspace_root is None:
        return []
    out: list[dict[str, Any]] = []

    def _next_ts() -> str | None:
        if ts_iter is not None:
            try:
                return next(ts_iter)
            except StopIteration:
                return fallback_ts
        return fallback_ts

    for candidate in _infer_read_candidate_paths(
        argv=argv,
        cwd=cwd,
        workspace_root=workspace_root,
        workspace_mount=workspace_mount,
    ):
        if not candidate.exists() or not candidate.is_file():
            continue
        out.append(
            make_event(
                "read_file",
                {
                    "path": _safe_relpath(candidate, workspace_root),
                    "bytes": candidate.stat().st_size,
                },
                ts=_next_ts(),
            )
        )
    return out


def normalize_codex_events(
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

    with normalized_events_path.open("w", encoding="utf-8", newline="\n") as out_f:
        call_ctx: dict[str, dict[str, Any]] = {}
        for raw_line, payload in _iter_codex_raw_lines(raw_events_path):
            if ts_iter is None:
                line_ts = _next_raw_ts()
            else:
                line_ts = None
            if payload is None:
                event = make_event(
                    "error",
                    {"category": "raw_non_json_line", "message": raw_line},
                    ts=_next_ts(),
                )
                out_f.write(json.dumps(event, ensure_ascii=False) + "\n")
                continue

            msg = payload.get("msg")
            if isinstance(msg, dict):
                msg_type = msg.get("type")
                if msg_type == "agent_message":
                    message = msg.get("message")
                    if isinstance(message, str):
                        event = make_event(
                            "agent_message",
                            {"kind": "message", "text": message},
                            ts=_next_ts(),
                        )
                        out_f.write(json.dumps(event, ensure_ascii=False) + "\n")
                    continue

                if msg_type == "agent_reasoning":
                    text = msg.get("text")
                    if isinstance(text, str):
                        event = make_event(
                            "agent_message",
                            {"kind": "observation", "text": text},
                            ts=_next_ts(),
                        )
                        out_f.write(json.dumps(event, ensure_ascii=False) + "\n")
                    continue

                if msg_type == "exec_command_begin":
                    call_id = msg.get("call_id")
                    begin_argv = msg.get("command")
                    if not isinstance(call_id, str) or not call_id:
                        continue
                    if not isinstance(begin_argv, list) or not all(
                        isinstance(a, str) for a in begin_argv
                    ):
                        continue
                    cwd_raw = msg.get("cwd")
                    begin_cwd: Path | None = None
                    if isinstance(cwd_raw, str) and cwd_raw:
                        if workspace_root is not None and workspace_mount is not None:
                            begin_cwd = _map_sandbox_path_str(
                                cwd_raw,
                                workspace_root=workspace_root,
                                workspace_mount=workspace_mount,
                            )
                        else:
                            begin_cwd = Path(_strip_windows_extended_prefix(cwd_raw))
                    call_ctx[call_id] = {"argv": begin_argv, "cwd": begin_cwd}
                    continue

                if msg_type != "exec_command_end":
                    continue

                call_id = msg.get("call_id")
                argv: list[str] | None = None
                cwd: Path | None = None
                if isinstance(call_id, str) and call_id in call_ctx:
                    stored = call_ctx.pop(call_id)
                    stored_argv = stored.get("argv")
                    if isinstance(stored_argv, list) and all(
                        isinstance(a, str) for a in stored_argv
                    ):
                        argv = stored_argv
                    stored_cwd = stored.get("cwd")
                    cwd = stored_cwd if isinstance(stored_cwd, Path) else None

                if argv is None:
                    argv_raw = msg.get("command")
                    if isinstance(argv_raw, list) and all(isinstance(a, str) for a in argv_raw):
                        argv = argv_raw

                if cwd is None:
                    cwd_raw = msg.get("cwd")
                    if isinstance(cwd_raw, str) and cwd_raw:
                        if workspace_root is not None and workspace_mount is not None:
                            cwd = _map_sandbox_path_str(
                                cwd_raw,
                                workspace_root=workspace_root,
                                workspace_mount=workspace_mount,
                            )
                        else:
                            cwd = Path(_strip_windows_extended_prefix(cwd_raw))

                if argv is None:
                    continue

                argv = _maybe_unwrap_shell_command(argv)

                exit_code = msg.get("exit_code")
                if not isinstance(exit_code, int):
                    exit_code = -1

                data: dict[str, Any] = {
                    "argv": argv,
                    "command": _format_argv(argv),
                    "exit_code": exit_code,
                }

                if cwd is not None:
                    data["cwd"] = _render_path(cwd)

                if exit_code != 0:
                    command_failure_idx += 1
                    stdout_text = msg.get("stdout") if isinstance(msg.get("stdout"), str) else ""
                    stderr_text = msg.get("stderr") if isinstance(msg.get("stderr"), str) else ""
                    duration_raw = msg.get("duration")
                    duration = duration_raw if isinstance(duration_raw, dict) else None
                    data["failure_artifacts"] = write_command_failure_artifacts(
                        run_dir=run_dir,
                        failure_index=command_failure_idx,
                        command=_format_argv(argv),
                        argv=argv,
                        cwd=_render_path(cwd) if cwd is not None else None,
                        exit_code=exit_code,
                        stdout_text=stdout_text,
                        stderr_text=stderr_text,
                        duration=duration,
                    )
                    output_text = _join_streams(msg.get("stdout"), msg.get("stderr"))
                    if output_text:
                        excerpt, truncated = _excerpt_text(output_text)
                        data["output_excerpt"] = excerpt
                        if truncated:
                            data["output_excerpt_truncated"] = True

                event = make_event(
                    "run_command",
                    data,
                    ts=_next_ts(),
                )
                out_f.write(json.dumps(event, ensure_ascii=False) + "\n")

                for read_event in _maybe_emit_read_events(
                    argv=argv,
                    cwd=cwd,
                    workspace_root=workspace_root,
                    workspace_mount=workspace_mount,
                    ts_iter=ts_iter,
                    fallback_ts=line_ts,
                ):
                    out_f.write(json.dumps(read_event, ensure_ascii=False) + "\n")
                continue

            payload_type = payload.get("type")
            if not (isinstance(payload_type, str) and payload_type == "item.completed"):
                continue

            item = payload.get("item")
            if not isinstance(item, dict):
                continue

            item_type = item.get("type")
            if item_type == "reasoning":
                text = item.get("text")
                if isinstance(text, str) and text:
                    event = make_event(
                        "agent_message",
                        {"kind": "observation", "text": text},
                        ts=_next_ts(),
                    )
                    out_f.write(json.dumps(event, ensure_ascii=False) + "\n")
                continue

            if item_type == "agent_message":
                text = item.get("text")
                if isinstance(text, str) and text:
                    event = make_event(
                        "agent_message", {"kind": "message", "text": text}, ts=_next_ts()
                    )
                    out_f.write(json.dumps(event, ensure_ascii=False) + "\n")
                continue

            if item_type != "command_execution":
                continue

            cmd = item.get("command")
            if not isinstance(cmd, str) or not cmd.strip():
                continue

            argv_raw = _split_command(cmd)
            argv = _maybe_unwrap_shell_command(argv_raw)

            exit_code = item.get("exit_code")
            if not isinstance(exit_code, int):
                status = item.get("status")
                exit_code = 1 if isinstance(status, str) and status.lower() == "failed" else -1

            data: dict[str, Any] = {
                "argv": argv,
                "command": _format_argv(argv),
                "exit_code": exit_code,
            }
            if exit_code != 0:
                command_failure_idx += 1
                stdout_text = (
                    item.get("stdout")
                    if isinstance(item.get("stdout"), str)
                    else (item.get("output") if isinstance(item.get("output"), str) else "")
                )
                stderr_text = item.get("stderr") if isinstance(item.get("stderr"), str) else ""
                data["failure_artifacts"] = write_command_failure_artifacts(
                    run_dir=run_dir,
                    failure_index=command_failure_idx,
                    command=_format_argv(argv),
                    argv=argv,
                    cwd=None,
                    exit_code=exit_code,
                    stdout_text=stdout_text,
                    stderr_text=stderr_text,
                    duration=None,
                )
                output_text = _join_streams(
                    item.get("stdout") or item.get("output"),
                    item.get("stderr"),
                )
                if output_text:
                    excerpt, truncated = _excerpt_text(output_text)
                    data["output_excerpt"] = excerpt
                    if truncated:
                        data["output_excerpt_truncated"] = True

            event = make_event(
                "run_command",
                data,
                ts=_next_ts(),
            )
            out_f.write(json.dumps(event, ensure_ascii=False) + "\n")

            for read_event in _maybe_emit_read_events(
                argv=argv,
                cwd=None,
                workspace_root=workspace_root,
                workspace_mount=workspace_mount,
                ts_iter=ts_iter,
                fallback_ts=line_ts,
            ):
                out_f.write(json.dumps(read_event, ensure_ascii=False) + "\n")
