from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

from agent_adapters.delegation import (
    delegation_invocation_data,
    delegation_result_data,
    is_delegation_tool,
)
from agent_adapters.events import make_event
from agent_adapters.failure_artifacts import write_command_failure_artifacts
from agent_adapters.read_attestation import observed_read_attestation

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
_WINDOWS_ABS_PATH_RE = re.compile(r"[A-Za-z]:\\")
_MAX_OUTPUT_EXCERPT_CHARS = 2_000
_POWERSHELL_INERT_HOST_SWITCHES = {
    "-nologo",
    "-noninteractive",
    "-noprofile",
}


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
    #
    # Exception: when the command string contains a Windows absolute path (e.g. C:\Users\...),
    # POSIX mode treats backslashes as escape characters and corrupts the path separators
    # (e.g. C:\Users\foo -> C:Usersfoo). For such commands, prefer posix=False so that
    # backslashes are preserved as literals.
    if _WINDOWS_ABS_PATH_RE.search(command):
        try:
            return shlex.split(command, posix=False)
        except ValueError:
            return command.split()
    try:
        return shlex.split(command, posix=True)
    except ValueError:
        try:
            return shlex.split(command, posix=False)
        except ValueError:
            return command.split()


def _strip_wrapper_quotes(value: str) -> str:
    stripped = value.strip()
    while len(stripped) >= 2 and stripped[0] == stripped[-1] and stripped[0] in {"'", '"'}:
        stripped = stripped[1:-1].strip()
    return stripped


def _split_powershell_inner_command(command: str) -> list[str]:
    """Split one PowerShell command while preserving relative Windows backslashes."""

    try:
        tokens = shlex.split(command, posix=False)
    except ValueError:
        return []
    return [_strip_wrapper_quotes(token) for token in tokens]


def _maybe_unwrap_shell_command(argv: list[str]) -> list[str]:
    if len(argv) < 3:
        return argv

    exe = _strip_wrapper_quotes(argv[0]).replace("\\", "/").lower()
    base = exe.rsplit("/", 1)[-1]
    arg1 = _strip_wrapper_quotes(argv[1]).lower()

    if base in {"bash", "sh"} and arg1 in {"-lc", "-c"}:
        inner = _strip_wrapper_quotes(argv[2])
        if isinstance(inner, str) and inner.strip():
            inner_argv = _split_command(inner)
            return inner_argv or argv
        return argv

    if base in {"cmd", "cmd.exe"} and arg1 == "/c":
        inner = _strip_wrapper_quotes(argv[2])
        if isinstance(inner, str) and inner.strip():
            inner_argv = _split_command(inner)
            return inner_argv or argv
        return argv

    if base in {"powershell", "powershell.exe", "pwsh", "pwsh.exe"}:
        command_index = 1
        seen_host_switches: set[str] = set()
        while command_index < len(argv):
            host_switch = _strip_wrapper_quotes(argv[command_index]).casefold()
            if host_switch not in _POWERSHELL_INERT_HOST_SWITCHES:
                break
            if host_switch in seen_host_switches:
                return argv
            seen_host_switches.add(host_switch)
            command_index += 1
        if command_index + 2 != len(argv) or _strip_wrapper_quotes(
            argv[command_index]
        ).casefold() not in {"-command", "-c"}:
            return argv
        inner = _strip_wrapper_quotes(argv[command_index + 1])
        if inner:
            inner_argv = _split_powershell_inner_command(inner)
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
    stdout_text: str,
    source_exit_code: int,
    fallback_ts: str | None = None,
) -> Iterable[dict[str, Any]]:
    if workspace_root is None:
        return []
    range_read = _powershell_exact_range_read(argv)
    if range_read is not None:
        path_token, skip_lines, first_lines = range_read
        effective_cwd = cwd if cwd is not None else workspace_root
        candidate = _resolve_candidate_path(
            path_token,
            base_dir=effective_cwd,
            workspace_root=workspace_root,
            workspace_mount=workspace_mount,
        )
        if candidate is None or not candidate.is_file():
            return []
        try:
            file_text = candidate.read_bytes().decode("utf-8", errors="replace")
        except OSError:
            return []
        normalized_file = file_text.replace("\r\n", "\n").replace("\r", "\n")
        selected_text = "".join(
            normalized_file.splitlines(keepends=True)[skip_lines : skip_lines + first_lines]
        )
        normalized_stdout = stdout_text.replace("\r\n", "\n").replace("\r", "\n")
        observed_text = (
            selected_text
            if normalized_stdout == selected_text
            or (not selected_text.endswith("\n") and normalized_stdout == selected_text + "\n")
            else None
        )
        attestation = observed_read_attestation(
            path=candidate,
            observed_text=observed_text,
            source_exit_code=source_exit_code,
            allow_partial=True,
        )
        return [
            make_event(
                "read_file",
                {
                    "path": _safe_relpath(candidate, workspace_root),
                    "bytes": attestation.get("file_size_bytes"),
                    "read_source": "shell_command",
                    "attestation_kind": "exact_line_range",
                    "source_exit_code": source_exit_code,
                    "requested_skip_lines": skip_lines,
                    "requested_first_lines": first_lines,
                    **attestation,
                },
                ts=(next(ts_iter, fallback_ts) if ts_iter is not None else fallback_ts),
            )
        ]
    if any(token in CHAIN_OPERATORS for token in argv):
        return []
    command = argv[0].casefold() if argv else ""
    if command == "cat":
        path_tokens = argv[1:]
    elif command == "type" and os.name == "nt":
        path_tokens = argv[1:]
    elif command in {"get-content", "gc"}:
        args = argv[1:]
        if "-raw" not in {token.casefold() for token in args}:
            return []
        path_token: str | None = None
        index = 0
        while index < len(args):
            token = args[index]
            folded = token.casefold()
            if folded == "-raw":
                index += 1
                continue
            if folded == "-literalpath":
                if path_token is not None or index + 1 >= len(args):
                    return []
                path_token = args[index + 1]
                index += 2
                continue
            if folded == "-encoding":
                if index + 1 >= len(args) or args[index + 1].casefold() != "utf8":
                    return []
                index += 2
                continue
            if token.startswith("-") or path_token is not None:
                return []
            path_token = token
            index += 1
        path_tokens = [path_token] if path_token is not None else []
    else:
        return []
    if len(path_tokens) != 1:
        return []
    out: list[dict[str, Any]] = []

    def _next_ts() -> str | None:
        if ts_iter is not None:
            try:
                return next(ts_iter)
            except StopIteration:
                return fallback_ts
        return fallback_ts

    effective_cwd = cwd if cwd is not None else workspace_root
    candidate = _resolve_candidate_path(
        path_tokens[0],
        base_dir=effective_cwd,
        workspace_root=workspace_root,
        workspace_mount=workspace_mount,
    )
    if candidate is not None and candidate.exists() and candidate.is_file():
        attestation = observed_read_attestation(
            path=candidate,
            observed_text=stdout_text,
            source_exit_code=source_exit_code,
            allow_partial=False,
            allow_single_terminal_newline=command in {"get-content", "gc"},
        )
        out.append(
            make_event(
                "read_file",
                {
                    "path": _safe_relpath(candidate, workspace_root),
                    "bytes": attestation.get("file_size_bytes"),
                    "read_source": "shell_command",
                    "source_exit_code": source_exit_code,
                    **attestation,
                },
                ts=_next_ts(),
            )
        )
    return out


def _powershell_exact_range_read(argv: list[str]) -> tuple[str, int, int] | None:
    """Recognize one output-attestable PowerShell file slice and nothing broader."""
    if "|" not in argv or argv.count("|") != 1:
        return None
    pipe_index = argv.index("|")
    source = argv[:pipe_index]
    selector = argv[pipe_index + 1 :]
    if not source or source[0].casefold() not in {"get-content", "gc"}:
        return None
    path_token: str | None = None
    encoding_seen = False
    index = 1
    while index < len(source):
        token = source[index]
        folded = token.casefold()
        if folded == "-encoding":
            if encoding_seen or index + 1 >= len(source):
                return None
            if source[index + 1].casefold() != "utf8":
                return None
            encoding_seen = True
            index += 2
            continue
        if folded == "-literalpath":
            if path_token is not None or index + 1 >= len(source):
                return None
            path_token = source[index + 1]
            index += 2
            continue
        return None
    if path_token is None or not encoding_seen:
        return None
    if not selector or selector[0].casefold() not in {"select-object", "select"}:
        return None
    values: dict[str, int] = {}
    index = 1
    while index < len(selector):
        option = selector[index].casefold()
        if option not in {"-skip", "-first"} or option in values or index + 1 >= len(selector):
            return None
        try:
            value = int(selector[index + 1])
        except ValueError:
            return None
        values[option] = value
        index += 2
    skip_lines = values.get("-skip")
    first_lines = values.get("-first")
    if (
        skip_lines is None
        or first_lines is None
        or skip_lines < 0
        or first_lines < 1
        or first_lines > 2_000
    ):
        return None
    return path_token, skip_lines, first_lines


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
        delegation_calls: dict[str, dict[str, Any]] = {}

        def _record_delegation_call(
            *, call_id: str | None, tool_name: str, tool_input: dict[str, Any]
        ) -> None:
            invocation = make_event(
                "delegation_invocation",
                delegation_invocation_data(tool_name, tool_input),
                ts=_next_ts(),
            )
            out_f.write(json.dumps(invocation, ensure_ascii=False) + "\n")
            if call_id:
                delegation_calls[call_id] = {"name": tool_name, "input": tool_input}

        def _emit_delegation_result(*, call_id: str | None, result_payload: Any) -> bool:
            if not call_id or call_id not in delegation_calls:
                return False
            tool_use = delegation_calls.pop(call_id)
            data = delegation_result_data(
                tool_name=str(tool_use.get("name", "")),
                tool_input=tool_use.get("input") if isinstance(tool_use.get("input"), dict) else {},
                result_payload=result_payload,
                is_error=False,
            )
            event = make_event("delegation_result", data, ts=_next_ts())
            out_f.write(json.dumps(event, ensure_ascii=False) + "\n")
            return True

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
                stdout_text = msg.get("stdout") if isinstance(msg.get("stdout"), str) else ""

                if cwd is not None:
                    data["cwd"] = _render_path(cwd)

                if exit_code != 0:
                    command_failure_idx += 1
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
                    stdout_text=stdout_text,
                    source_exit_code=exit_code,
                    fallback_ts=line_ts,
                ):
                    out_f.write(json.dumps(read_event, ensure_ascii=False) + "\n")
                continue

            nested_payload = payload.get("payload")
            if isinstance(nested_payload, dict):
                nested_type = nested_payload.get("type")
                if nested_type == "function_call":
                    tool_name = nested_payload.get("name")
                    arguments = nested_payload.get("arguments")
                    tool_input: dict[str, Any]
                    if isinstance(arguments, str):
                        try:
                            parsed_args = json.loads(arguments)
                        except json.JSONDecodeError:
                            parsed_args = {}
                        tool_input = parsed_args if isinstance(parsed_args, dict) else {}
                    else:
                        tool_input = arguments if isinstance(arguments, dict) else {}
                    if is_delegation_tool(tool_name):
                        call_id = nested_payload.get("call_id")
                        _record_delegation_call(
                            call_id=call_id if isinstance(call_id, str) else None,
                            tool_name=str(tool_name or ""),
                            tool_input=tool_input,
                        )
                    continue
                if nested_type == "function_call_output":
                    call_id = nested_payload.get("call_id")
                    _emit_delegation_result(
                        call_id=call_id if isinstance(call_id, str) else None,
                        result_payload=nested_payload,
                    )
                    continue

            payload_type = payload.get("type")
            if payload_type == "function_call":
                tool_name = payload.get("name")
                arguments = payload.get("arguments")
                tool_input: dict[str, Any]
                if isinstance(arguments, str):
                    try:
                        parsed_args = json.loads(arguments)
                    except json.JSONDecodeError:
                        parsed_args = {}
                    tool_input = parsed_args if isinstance(parsed_args, dict) else {}
                else:
                    tool_input = arguments if isinstance(arguments, dict) else {}
                if is_delegation_tool(tool_name):
                    call_id = payload.get("call_id")
                    _record_delegation_call(
                        call_id=call_id if isinstance(call_id, str) else None,
                        tool_name=str(tool_name or ""),
                        tool_input=tool_input,
                    )
                continue

            if payload_type == "function_call_output":
                call_id = payload.get("call_id")
                _emit_delegation_result(
                    call_id=call_id if isinstance(call_id, str) else None,
                    result_payload=payload,
                )
                continue

            if not (isinstance(payload_type, str) and payload_type == "item.completed"):
                continue

            item = payload.get("item")
            if not isinstance(item, dict):
                continue

            item_type = item.get("type")
            if item_type == "function_call":
                tool_name = item.get("name")
                raw_args = item.get("arguments")
                if isinstance(raw_args, str):
                    try:
                        parsed_args = json.loads(raw_args)
                    except json.JSONDecodeError:
                        parsed_args = {}
                    tool_input = parsed_args if isinstance(parsed_args, dict) else {}
                else:
                    tool_input = raw_args if isinstance(raw_args, dict) else {}
                if is_delegation_tool(tool_name):
                    call_id = item.get("call_id") or item.get("id")
                    _record_delegation_call(
                        call_id=call_id if isinstance(call_id, str) else None,
                        tool_name=str(tool_name or ""),
                        tool_input=tool_input,
                    )
                continue

            if item_type == "function_call_output":
                call_id = item.get("call_id") or item.get("id")
                _emit_delegation_result(
                    call_id=call_id if isinstance(call_id, str) else None,
                    result_payload=item,
                )
                continue

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
            stdout_text = next(
                (
                    value
                    for key in ("stdout", "output", "aggregated_output")
                    if isinstance((value := item.get(key)), str) and value
                ),
                "",
            )
            if exit_code != 0:
                command_failure_idx += 1
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
                stdout_text=stdout_text,
                source_exit_code=exit_code,
                fallback_ts=line_ts,
            ):
                out_f.write(json.dumps(read_event, ensure_ascii=False) + "\n")
