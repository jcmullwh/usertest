from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from token_monitoring.usage import TokenUsage, UsageResult, UsageSemantics

TOKEN_DIMENSIONS: tuple[str, ...] = (
    "total_tokens",
    "input_tokens",
    "cached_input_tokens",
    "uncached_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
)

_READ_COMMANDS = {
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
    "nl",
    "wc",
    "get-content",
    "select-string",
    "git",
}
_SOURCE_EXTENSIONS = (
    ".py",
    ".toml",
    ".yaml",
    ".yml",
    ".json",
    ".md",
    ".txt",
    ".ps1",
    ".sh",
    ".ts",
    ".tsx",
    ".js",
    ".jsx",
)
_SECRET_VALUE_RE = re.compile(
    r"(?i)(api[_-]?key|token|password|secret|authorization)(\s*[=:]\s*)([^\s\"']+)"
)
_VERIFY_CLIENT_COMMAND_RE = re.compile(
    r"""(?ix)
    (^|[\s'"])
    (sh|bash|python(?:3)?)(?:\.exe)?
    \s+
    [/\\]run_dir[/\\]verification_broker[/\\]client[/\\]verify_client\.(?:sh|py)
    (?=$|[\s'"])
    """
)
_TOOL_SESSION_ID_RE = re.compile(r"session ID\s+(\d+)")
_DELEGATION_TOOL_NAMES = {
    "agent",
    "invoke_agent",
    "spawn_agent",
    "delegate",
    "subagent",
    "sub_agent",
}
_DELEGATION_TOOL_SUFFIXES = (
    ".agent",
    ".invoke_agent",
    ".spawn_agent",
    "__agent",
    "__invoke_agent",
    "__spawn_agent",
)


def _is_delegation_tool_name(raw: Any) -> bool:
    if not isinstance(raw, str):
        return False
    name = raw.strip().lower().replace("-", "_")
    if not name:
        return False
    if name in _DELEGATION_TOOL_NAMES or name.endswith(_DELEGATION_TOOL_SUFFIXES):
        return True
    tail = re.split(r"[.:/]", name)[-1]
    return (
        tail in _DELEGATION_TOOL_NAMES
        or "invoke_agent" in name
        or "spawn_agent" in name
        or "subagent" in name
    )


def zero_usage() -> dict[str, int]:
    return {key: 0 for key in TOKEN_DIMENSIONS}


def add_usage(left: dict[str, int], right: dict[str, int]) -> dict[str, int]:
    return {key: int(left.get(key, 0)) + int(right.get(key, 0)) for key in TOKEN_DIMENSIONS}


def usage_from_mapping(raw: Any) -> dict[str, int]:
    out = zero_usage()
    if not isinstance(raw, dict):
        return out
    for key in TOKEN_DIMENSIONS:
        value = raw.get(key)
        if isinstance(value, int):
            out[key] = value
        elif isinstance(value, float) and value.is_integer():
            out[key] = int(value)
    if "uncached_input_tokens" not in raw:
        out["uncached_input_tokens"] = max(0, out["input_tokens"] - out["cached_input_tokens"])
    return out


def usage_equals(left: dict[str, int], right: dict[str, int]) -> bool:
    return all(int(left.get(key, 0)) == int(right.get(key, 0)) for key in TOKEN_DIMENSIONS)


def usage_decreased(prev: dict[str, int], current: dict[str, int]) -> list[str]:
    decreased: list[str] = []
    for key in TOKEN_DIMENSIONS:
        if int(current.get(key, 0)) < int(prev.get(key, 0)):
            decreased.append(key)
    return decreased


def default_codex_sessions_root() -> Path:
    codex_home = os.environ.get("CODEX_HOME")
    if codex_home:
        return Path(codex_home).expanduser() / "sessions"
    return Path.home() / ".codex" / "sessions"


def sanitize_command_for_metadata(command: str) -> str:
    scrubbed = _SECRET_VALUE_RE.sub(r"\1\2<redacted>", command)
    if len(scrubbed) > 180:
        return scrubbed[:177] + "..."
    return scrubbed


def _split_command(command: str) -> list[str]:
    try:
        return shlex.split(command, posix=True)
    except ValueError:
        try:
            return shlex.split(command, posix=False)
        except ValueError:
            return command.split()


def _maybe_json_object(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str):
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _command_from_tool_args(tool_name: str, args: dict[str, Any]) -> str | None:
    for key in ("cmd", "command", "script"):
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    if tool_name.endswith("shell_command") or tool_name in {"exec_command", "shell_command"}:
        value = args.get("parameters")
        if isinstance(value, dict):
            for key in ("cmd", "command"):
                nested = value.get(key)
                if isinstance(nested, str) and nested.strip():
                    return nested.strip()
    return None


def _paths_from_command(command: str) -> list[str]:
    tokens = _split_command(command)
    out: list[str] = []
    for token in tokens:
        stripped = token.strip("\"'")
        lower = stripped.lower()
        if lower.startswith("-"):
            continue
        if (
            "/" in stripped
            or "\\" in stripped
            or lower.endswith(_SOURCE_EXTENSIONS)
            or lower in {"pyproject.toml", "package.json", "readme.md"}
        ):
            out.append(stripped)
    return out[:12]


def _classify_command(command: str) -> str:
    lowered = command.lower()
    tokens = [t.lower() for t in _split_command(command)]
    token_set = set(tokens)
    if _is_verify_client_command(command):
        return "wait_poll"
    if (
        "start-sleep" in token_set
        or "sleep" in token_set
        or "wait-process" in token_set
        or "wait" in token_set
        or "watch" in token_set
        or "tail -f" in lowered
        or "read_thread_terminal" in lowered
    ):
        return "wait_poll"
    if any(marker in lowered for marker in ("pytest", "ruff", "mypy", "npm test", "pdm run")):
        return "verification"
    if any(
        marker in lowered for marker in ("pip install", "pdm install", "npm install", "uv sync")
    ):
        return "dependency"
    if token_set & _READ_COMMANDS:
        return "source_read"
    if "apply_patch" in lowered or "write_text" in lowered or "set-content" in lowered:
        return "edit"
    return "tool"


def _is_verify_client_command(command: str) -> bool:
    return bool(_VERIFY_CLIENT_COMMAND_RE.search(command))


def _session_id_from_output(output: str) -> int | None:
    match = _TOOL_SESSION_ID_RE.search(output)
    if match is None:
        return None
    return int(match.group(1))


def _extract_action(
    event: dict[str, Any],
    *,
    active_verify_sessions: set[int] | None = None,
) -> dict[str, Any] | None:
    payload = event.get("payload")
    if isinstance(payload, dict):
        payload_type = payload.get("type")
        if payload_type == "function_call":
            tool_name = str(payload.get("name") or "")
            args = _maybe_json_object(payload.get("arguments"))
            if _is_delegation_tool_name(tool_name):
                prompt = args.get("prompt") or args.get("task") or args.get("description")
                return {
                    "type": "delegation",
                    "tool_name": tool_name,
                    "command_class": "delegation_tool",
                    "paths": [],
                    "prompt_chars": len(prompt) if isinstance(prompt, str) else 0,
                    "input_keys": sorted(str(key) for key in args.keys()),
                }
            if tool_name == "write_stdin":
                session_id = args.get("session_id")
                is_empty_wait = args.get("chars", "") == "" and isinstance(
                    args.get("yield_time_ms"), int
                )
                if is_empty_wait:
                    is_verifier = (
                        isinstance(session_id, int)
                        and active_verify_sessions is not None
                        and session_id in active_verify_sessions
                    )
                    return {
                        "type": "wait_poll",
                        "tool_name": tool_name,
                        "command_class": (
                            "verifier_continuation_wait"
                            if is_verifier
                            else "terminal_continuation_wait"
                        ),
                        "paths": [],
                        "session_id": session_id,
                        "yield_time_ms": args.get("yield_time_ms"),
                        "max_output_tokens": args.get("max_output_tokens"),
                    }
            if tool_name == "multi_tool_use.parallel":
                tool_uses = args.get("tool_uses")
                if isinstance(tool_uses, list):
                    nested_classes: list[str] = []
                    nested_paths: list[str] = []
                    for item in tool_uses:
                        if not isinstance(item, dict):
                            continue
                        nested_name = str(item.get("recipient_name") or "")
                        params = item.get("parameters")
                        params = params if isinstance(params, dict) else {}
                        command = _command_from_tool_args(nested_name, params)
                        if command is None:
                            continue
                        nested_classes.append(_classify_command(command))
                        nested_paths.extend(_paths_from_command(command))
                    action_type = "source_read" if "source_read" in nested_classes else "tool"
                    if "wait_poll" in nested_classes:
                        action_type = "wait_poll"
                    if "verification" in nested_classes:
                        action_type = "verification"
                    return {
                        "type": action_type,
                        "tool_name": tool_name,
                        "command_class": "+".join(sorted(set(nested_classes))) or "parallel_tool",
                        "paths": nested_paths[:12],
                    }
            command = _command_from_tool_args(tool_name, args)
            if command is not None:
                action_type = _classify_command(command)
                command_class = (
                    "verifier_wait_start" if _is_verify_client_command(command) else action_type
                )
                return {
                    "type": action_type,
                    "tool_name": tool_name,
                    "command_class": command_class,
                    "command_excerpt": sanitize_command_for_metadata(command),
                    "paths": _paths_from_command(command),
                    "yield_time_ms": args.get("yield_time_ms"),
                    "max_output_tokens": args.get("max_output_tokens"),
                }
            if tool_name:
                if tool_name.endswith("read_thread_terminal"):
                    return {
                        "type": "wait_poll",
                        "tool_name": tool_name,
                        "command_class": "terminal_read",
                        "paths": [],
                    }
                return {
                    "type": "tool",
                    "tool_name": tool_name,
                    "command_class": tool_name,
                    "paths": [],
                }
        if payload_type == "message":
            return {
                "type": "assistant",
                "tool_name": None,
                "command_class": "assistant_message",
                "paths": [],
            }
        if payload_type == "function_call_output":
            return None
        if payload_type == "reasoning":
            return None

    msg = event.get("msg")
    if isinstance(msg, dict):
        msg_type = msg.get("type")
        if msg_type in {"exec_command_begin", "exec_command_end"}:
            command_raw = msg.get("command")
            command = (
                " ".join(command_raw) if isinstance(command_raw, list) else str(command_raw or "")
            )
            if command.strip():
                action_type = _classify_command(command)
                return {
                    "type": action_type,
                    "tool_name": "exec_command",
                    "command_class": action_type,
                    "command_excerpt": sanitize_command_for_metadata(command),
                    "paths": _paths_from_command(command),
                }
        if msg_type == "agent_message":
            return {
                "type": "assistant",
                "tool_name": None,
                "command_class": "assistant_message",
                "paths": [],
            }

    item = event.get("item")
    if isinstance(item, dict) and item.get("type") == "command_execution":
        command = item.get("command")
        if isinstance(command, str) and command.strip():
            action_type = _classify_command(command)
            return {
                "type": action_type,
                "tool_name": "command_execution",
                "command_class": action_type,
                "command_excerpt": sanitize_command_for_metadata(command),
                "paths": _paths_from_command(command),
            }
    return None


@dataclass(frozen=True)
class CodexSessionResult:
    path: Path
    accepted: bool
    session_id: str | None
    final_usage: dict[str, int]
    summed_last_usage: dict[str, int]
    model_call_count: int
    token_event_count: int
    trace: list[dict[str, Any]]
    exceptions: list[dict[str, Any]] = field(default_factory=list)

    @property
    def peak_call(self) -> dict[str, Any]:
        if not self.trace:
            return {"call_index": None, "token_usage": zero_usage()}
        return max(
            self.trace,
            key=lambda item: int(
                item.get("token_usage", {}).get("input_tokens", 0)
                if isinstance(item.get("token_usage"), dict)
                else 0
            ),
        )


def _event_token_usage(event: dict[str, Any]) -> tuple[dict[str, int], dict[str, int]] | None:
    payload = event.get("payload")
    if not isinstance(payload, dict) or payload.get("type") != "token_count":
        return None
    info = payload.get("info")
    if not isinstance(info, dict):
        return None
    total = usage_from_mapping(info.get("total_token_usage"))
    last = usage_from_mapping(info.get("last_token_usage"))
    if not any(last.values()):
        last = total
    return total, last


def _output_chars(event: dict[str, Any]) -> int:
    payload = event.get("payload")
    if isinstance(payload, dict) and payload.get("type") == "function_call_output":
        output = payload.get("output")
        return len(output) if isinstance(output, str) else 0
    msg = event.get("msg")
    if isinstance(msg, dict) and msg.get("type") == "exec_command_end":
        return sum(len(v) for v in (msg.get("stdout"), msg.get("stderr")) if isinstance(v, str))
    item = event.get("item")
    if isinstance(item, dict):
        return sum(
            len(v)
            for v in (
                item.get("aggregated_output"),
                item.get("output"),
                item.get("stdout"),
                item.get("stderr"),
            )
            if isinstance(v, str)
        )
    return 0


def parse_codex_session(path: Path) -> CodexSessionResult:
    exceptions: list[dict[str, Any]] = []
    if not path.exists():
        return CodexSessionResult(
            path=path,
            accepted=False,
            session_id=None,
            final_usage=zero_usage(),
            summed_last_usage=zero_usage(),
            model_call_count=0,
            token_event_count=0,
            trace=[],
            exceptions=[{"code": "session_missing", "path": str(path)}],
        )
    if path.stat().st_size == 0:
        return CodexSessionResult(
            path=path,
            accepted=False,
            session_id=None,
            final_usage=zero_usage(),
            summed_last_usage=zero_usage(),
            model_call_count=0,
            token_event_count=0,
            trace=[],
            exceptions=[{"code": "session_zero_byte", "path": str(path)}],
        )

    session_id: str | None = None
    previous_total = zero_usage()
    final_usage = zero_usage()
    summed_last = zero_usage()
    token_event_count = 0
    trace: list[dict[str, Any]] = []
    pending_usage: dict[str, int] | None = None
    pending_source_line: int | None = None
    pending_timestamp: str | None = None
    pending_context: dict[str, Any] = {}
    recent_output_chars = 0
    malformed_lines = 0
    pending_tool_calls: dict[str, dict[str, Any]] = {}
    active_verify_sessions: set[int] = set()

    with path.open("r", encoding="utf-8") as f:
        for line_number, raw_line in enumerate(f, start=1):
            line = raw_line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                malformed_lines += 1
                continue
            if not isinstance(event, dict):
                malformed_lines += 1
                continue

            if event.get("type") == "session_meta":
                payload = event.get("payload")
                if isinstance(payload, dict):
                    candidate = payload.get("session_id") or payload.get("id")
                    if isinstance(candidate, str) and candidate.strip():
                        session_id = candidate.strip()

            output_chars = _output_chars(event)
            if output_chars:
                recent_output_chars = output_chars

            payload = event.get("payload")
            if isinstance(payload, dict):
                payload_type = payload.get("type")
                if payload_type == "function_call":
                    call_id = payload.get("call_id")
                    if isinstance(call_id, str):
                        args = _maybe_json_object(payload.get("arguments"))
                        pending_tool_calls[call_id] = {
                            "name": payload.get("name"),
                            "args": args,
                            "command": _command_from_tool_args(str(payload.get("name") or ""), args)
                            or "",
                        }
                elif payload_type == "function_call_output":
                    call_id = payload.get("call_id")
                    call = pending_tool_calls.pop(call_id, {}) if isinstance(call_id, str) else {}
                    output = payload.get("output")
                    output_text = output if isinstance(output, str) else ""
                    command = str(call.get("command") or "")
                    call_args = call.get("args")
                    args = dict(call_args) if isinstance(call_args, dict) else {}
                    name = str(call.get("name") or "")
                    if name == "exec_command" and _is_verify_client_command(command):
                        verify_session_id = _session_id_from_output(output_text)
                        if (
                            verify_session_id is not None
                            and "Process running with session ID" in output_text
                        ):
                            active_verify_sessions.add(verify_session_id)
                    elif name == "write_stdin":
                        verify_session_id = args.get("session_id")
                        if (
                            isinstance(verify_session_id, int)
                            and verify_session_id in active_verify_sessions
                            and "Process exited" in output_text
                        ):
                            active_verify_sessions.discard(verify_session_id)

            usage_pair = _event_token_usage(event)
            if usage_pair is not None:
                total, last = usage_pair
                token_event_count += 1
                decreased = usage_decreased(previous_total, total)
                if decreased:
                    exceptions.append(
                        {
                            "code": "non_monotonic_cumulative_usage",
                            "line": line_number,
                            "dimensions": decreased,
                            "path": str(path),
                        }
                    )
                previous_total = total
                final_usage = total
                summed_last = add_usage(summed_last, last)
                pending_usage = last
                pending_source_line = line_number
                pending_timestamp = (
                    event.get("timestamp") if isinstance(event.get("timestamp"), str) else None
                )
                pending_context = {}
                if recent_output_chars:
                    pending_context["retained_output_chars"] = recent_output_chars
                continue

            action = _extract_action(event, active_verify_sessions=active_verify_sessions)
            if action is None or pending_usage is None:
                continue

            trace.append(
                {
                    "schema_version": 1,
                    "call_index": len(trace) + 1,
                    "timestamp": pending_timestamp,
                    "source_line": pending_source_line,
                    "token_usage": pending_usage,
                    "action": action,
                    "context_evidence": pending_context,
                    "state_change": None,
                    "confidence": "authoritative",
                }
            )
            pending_usage = None
            pending_source_line = None
            pending_timestamp = None
            pending_context = {}

    if pending_usage is not None:
        trace.append(
            {
                "schema_version": 1,
                "call_index": len(trace) + 1,
                "timestamp": pending_timestamp,
                "source_line": pending_source_line,
                "token_usage": pending_usage,
                "action": {
                    "type": "unclassified",
                    "tool_name": None,
                    "command_class": "unclassified",
                    "paths": [],
                },
                "context_evidence": pending_context,
                "state_change": None,
                "confidence": "authoritative",
            }
        )

    if malformed_lines:
        exceptions.append(
            {"code": "malformed_jsonl_lines", "count": malformed_lines, "path": str(path)}
        )
    if token_event_count == 0:
        exceptions.append({"code": "no_token_count_events", "path": str(path)})
    if token_event_count > 0 and not usage_equals(summed_last, final_usage):
        exceptions.append(
            {
                "code": "last_usage_does_not_reconcile_to_final_total",
                "path": str(path),
                "summed_last_usage": summed_last,
                "final_usage": final_usage,
            }
        )

    accepted = token_event_count > 0 and not exceptions and usage_equals(summed_last, final_usage)
    return CodexSessionResult(
        path=path,
        accepted=accepted,
        session_id=session_id,
        final_usage=final_usage,
        summed_last_usage=summed_last,
        model_call_count=len(trace),
        token_event_count=token_event_count,
        trace=trace,
        exceptions=exceptions,
    )


@dataclass(frozen=True)
class _InvocationUsageObservation:
    usage: TokenUsage
    source_kind: str
    source_line: int
    last_usage: TokenUsage | None = None
    terminal: bool = False


def _source_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _usage_observation_from_event(
    event: dict[str, Any],
    *,
    line_number: int,
) -> _InvocationUsageObservation | None:
    event_type = event.get("type")
    payload = event.get("payload")
    msg = event.get("msg")

    if event_type == "turn.completed":
        usage = event.get("usage")
        if isinstance(usage, dict):
            if not any(
                key in usage for key in ("input_tokens", "output_tokens", "total_tokens")
            ):
                raise ValueError("terminal usage has no recognized token dimensions")
            return _InvocationUsageObservation(
                usage=TokenUsage.from_mapping(usage),
                source_kind="turn.completed",
                source_line=line_number,
                terminal=True,
            )
        return None

    if isinstance(msg, dict) and msg.get("type") == "turn.completed":
        usage = msg.get("usage")
        if isinstance(usage, dict):
            if not any(
                key in usage for key in ("input_tokens", "output_tokens", "total_tokens")
            ):
                raise ValueError("terminal usage has no recognized token dimensions")
            return _InvocationUsageObservation(
                usage=TokenUsage.from_mapping(usage),
                source_kind="msg.turn.completed",
                source_line=line_number,
                terminal=True,
            )
        return None

    if not isinstance(payload, dict) or payload.get("type") != "token_count":
        return None
    info = payload.get("info")
    if not isinstance(info, dict):
        return None
    total_raw = info.get("total_token_usage")
    if not isinstance(total_raw, dict):
        return None
    if not any(
        key in total_raw for key in ("input_tokens", "output_tokens", "total_tokens")
    ):
        raise ValueError("cumulative usage has no recognized token dimensions")
    last_raw = info.get("last_token_usage")
    return _InvocationUsageObservation(
        usage=TokenUsage.from_mapping(total_raw),
        last_usage=(TokenUsage.from_mapping(last_raw) if isinstance(last_raw, dict) else None),
        source_kind="event_msg.token_count",
        source_line=line_number,
    )


def _usage_tuple(usage: TokenUsage) -> tuple[int, ...]:
    return tuple(usage.to_dict()[name] for name in TOKEN_DIMENSIONS)


def _add_token_usage(left: TokenUsage, right: TokenUsage) -> TokenUsage:
    return TokenUsage(
        **{
            name: int(getattr(left, name)) + int(getattr(right, name))
            for name in TOKEN_DIMENSIONS
        }
    )


def parse_codex_invocation_usage(
    path: Path,
    *,
    invocation_id: str,
    baseline_high_water: TokenUsage | Mapping[str, object] | None = None,
    session_id: str | None = None,
) -> UsageResult:
    """Attribute one Codex invocation from raw JSONL usage evidence.

    A supplied ``baseline_high_water`` declares a continuation of a cumulative
    provider session; the attributable usage is the final high-water mark minus
    that baseline.  With no baseline, the input is treated as a fresh invocation
    and must contain either one unique ``turn.completed`` usage value or a fully
    reconciling Codex ``token_count`` sequence.

    Repeated identical terminal events are evidence replay and count once.
    Missing, decreasing, conflicting, or otherwise ambiguous cumulative counters
    return ``unattributable`` with ``usage=None``.
    """

    diagnostics: list[dict[str, object]] = []
    baseline: TokenUsage | None = None
    if baseline_high_water is not None:
        try:
            baseline = (
                baseline_high_water
                if isinstance(baseline_high_water, TokenUsage)
                else TokenUsage.from_mapping(baseline_high_water)
            )
        except ValueError as exc:
            diagnostics.append({"code": "invalid_baseline_high_water", "detail": str(exc)})

    source_hash: str | None = None
    observations: list[_InvocationUsageObservation] = []
    malformed_lines = 0
    invalid_usage_lines: list[int] = []
    terminal_without_usage_lines: list[int] = []
    discovered_session_id: str | None = None

    if not path.exists():
        diagnostics.append({"code": "usage_source_missing"})
    elif path.stat().st_size == 0:
        source_hash = _source_sha256(path)
        diagnostics.append({"code": "usage_source_zero_byte"})
    else:
        source_hash = _source_sha256(path)
        with path.open("r", encoding="utf-8") as source:
            for line_number, raw_line in enumerate(source, start=1):
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    malformed_lines += 1
                    continue
                if not isinstance(event, dict):
                    malformed_lines += 1
                    continue

                payload = event.get("payload")
                if event.get("type") == "session_meta" and isinstance(payload, dict):
                    candidate = payload.get("session_id") or payload.get("id")
                    if isinstance(candidate, str) and candidate.strip():
                        discovered_session_id = candidate.strip()
                elif event.get("type") == "thread.started":
                    candidate = event.get("thread_id")
                    if isinstance(candidate, str) and candidate.strip():
                        discovered_session_id = candidate.strip()

                is_terminal = event.get("type") == "turn.completed" or (
                    isinstance(event.get("msg"), dict)
                    and event["msg"].get("type") == "turn.completed"
                )
                try:
                    observation = _usage_observation_from_event(
                        event, line_number=line_number
                    )
                except ValueError:
                    invalid_usage_lines.append(line_number)
                    continue
                if observation is not None:
                    observations.append(observation)
                elif is_terminal:
                    terminal_without_usage_lines.append(line_number)

    if malformed_lines:
        diagnostics.append({"code": "malformed_jsonl_lines", "count": malformed_lines})
    if invalid_usage_lines:
        diagnostics.append(
            {"code": "invalid_usage_values", "source_lines": invalid_usage_lines}
        )
    if terminal_without_usage_lines:
        diagnostics.append(
            {
                "code": "terminal_events_missing_usage",
                "source_lines": terminal_without_usage_lines,
            }
        )
    if session_id is not None and discovered_session_id not in {None, session_id}:
        diagnostics.append(
            {
                "code": "session_id_mismatch",
                "expected": session_id,
                "observed": discovered_session_id,
            }
        )

    duplicate_terminal_count = 0
    unique_observations: list[_InvocationUsageObservation] = []
    previous: _InvocationUsageObservation | None = None
    seen_terminal_values: set[tuple[int, ...]] = set()
    for observation in observations:
        observation_key = _usage_tuple(observation.usage)
        if observation.terminal:
            if observation_key in seen_terminal_values:
                duplicate_terminal_count += 1
            else:
                seen_terminal_values.add(observation_key)
        if previous is not None and observation.usage == previous.usage:
            if (
                observation.last_usage is not None
                and previous.last_usage is not None
                and observation.last_usage != previous.last_usage
            ):
                diagnostics.append(
                    {
                        "code": "conflicting_duplicate_cumulative_usage",
                        "source_line": observation.source_line,
                    }
                )
            previous = observation
            continue
        if previous is not None:
            decreased = observation.usage.decreased_dimensions(previous.usage)
            if decreased:
                diagnostics.append(
                    {
                        "code": "non_monotonic_cumulative_usage",
                        "source_line": observation.source_line,
                        "dimensions": list(decreased),
                    }
                )
        unique_observations.append(observation)
        previous = observation

    final_high_water = unique_observations[-1].usage if unique_observations else None
    fatal_codes = {
        "invalid_baseline_high_water",
        "usage_source_missing",
        "usage_source_zero_byte",
        "malformed_jsonl_lines",
        "invalid_usage_values",
        "terminal_events_missing_usage",
        "session_id_mismatch",
        "conflicting_duplicate_cumulative_usage",
        "non_monotonic_cumulative_usage",
    }

    if not observations:
        diagnostics.append({"code": "no_usage_observations"})
        fatal_codes.add("no_usage_observations")

    usage: TokenUsage | None = None
    semantics: UsageSemantics = "unattributable"
    if not any(item.get("code") in fatal_codes for item in diagnostics):
        if baseline is not None and final_high_water is not None:
            decreased = final_high_water.decreased_dimensions(baseline)
            if decreased:
                diagnostics.append(
                    {
                        "code": "final_usage_below_baseline",
                        "dimensions": list(decreased),
                    }
                )
            else:
                usage = final_high_water.delta_from(baseline)
                semantics = "session_cumulative"
        elif final_high_water is not None:
            terminal_values = {
                _usage_tuple(item.usage) for item in observations if item.terminal
            }
            if len(terminal_values) > 1:
                diagnostics.append(
                    {
                        "code": "multiple_terminal_usage_values_without_baseline",
                        "count": len(terminal_values),
                    }
                )
            elif terminal_values:
                usage = final_high_water
                semantics = "per_invocation"
            else:
                token_observations = [
                    item
                    for item in unique_observations
                    if item.source_kind == "event_msg.token_count"
                ]
                if not token_observations or any(
                    item.last_usage is None for item in token_observations
                ):
                    diagnostics.append({"code": "missing_per_call_usage_for_fresh_session"})
                else:
                    summed_last = TokenUsage.zero()
                    for item in token_observations:
                        assert item.last_usage is not None
                        summed_last = _add_token_usage(summed_last, item.last_usage)
                    if summed_last != final_high_water:
                        diagnostics.append(
                            {
                                "code": "per_call_usage_does_not_reconcile",
                                "summed_last_usage": summed_last.to_dict(),
                                "observed_high_water": final_high_water.to_dict(),
                            }
                        )
                    else:
                        usage = final_high_water
                        semantics = "per_invocation"

    if usage is None:
        semantics = "unattributable"

    return UsageResult(
        provider="openai_codex",
        invocation_id=invocation_id,
        semantics=semantics,
        usage=usage,
        observed_high_water=final_high_water,
        baseline_high_water=baseline,
        session_id=session_id or discovered_session_id,
        source_kind="codex_raw_jsonl",
        source_sha256=source_hash,
        observation_count=len(observations),
        unique_observation_count=len(unique_observations),
        duplicate_terminal_count=duplicate_terminal_count,
        diagnostics=tuple(diagnostics),
    )


parse_codex_usage_jsonl = parse_codex_invocation_usage


def find_codex_session_for_thread(
    sessions_root: Path,
    thread_id: str,
) -> tuple[Path | None, list[dict[str, Any]]]:
    exceptions: list[dict[str, Any]] = []
    if not thread_id.strip():
        return None, [{"code": "missing_thread_id"}]
    if not sessions_root.exists():
        return None, [{"code": "codex_sessions_root_missing", "path": str(sessions_root)}]

    filename_matches = sorted(sessions_root.rglob(f"*{thread_id}*.jsonl"))
    if len(filename_matches) == 1:
        return filename_matches[0], []
    if len(filename_matches) > 1:
        return None, [
            {
                "code": "ambiguous_session_filename_matches",
                "thread_id": thread_id,
                "paths": [str(p) for p in filename_matches],
            }
        ]

    matches: list[Path] = []
    for candidate in sessions_root.rglob("*.jsonl"):
        try:
            with candidate.open("r", encoding="utf-8") as f:
                head = "".join(line for _, line in zip(range(20), f, strict=False))
        except OSError:
            continue
        if thread_id in head:
            matches.append(candidate)
    if len(matches) == 1:
        return matches[0], []
    if len(matches) > 1:
        exceptions.append(
            {
                "code": "ambiguous_session_content_matches",
                "thread_id": thread_id,
                "paths": [str(p) for p in sorted(matches)],
            }
        )
    else:
        exceptions.append({"code": "codex_session_not_found", "thread_id": thread_id})
    return None, exceptions
