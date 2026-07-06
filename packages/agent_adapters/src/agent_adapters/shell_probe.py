from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_adapters.claude_cli import run_claude_print
from agent_adapters.codex_cli import run_codex_exec
from agent_adapters.gemini_cli import run_gemini

_SHELL_PROBE_MARKER = "shell_probe=ok"
_TAIL_BYTES = 24_000


@dataclass(frozen=True)
class AgentShellProbeResult:
    agent: str
    argv: list[str]
    exit_code: int
    raw_events_path: Path
    last_message_path: Path
    stderr_path: Path
    marker_seen: bool
    marker_source: str | None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        stderr_tail = _read_tail(self.stderr_path)
        stdout_tail = _read_tail(self.raw_events_path)
        last_message = _read_text(self.last_message_path)
        ok = self.exit_code == 0 and self.marker_seen and self.error is None
        reason = None
        if self.error is not None:
            reason = self.error
        elif self.exit_code != 0:
            reason = f"Agent shell probe exited non-zero: exit_code={self.exit_code}"
        elif not self.marker_seen:
            reason = f"Agent shell probe did not emit required marker {_SHELL_PROBE_MARKER!r}."
        return {
            "kind": "agent_shell_payload",
            "agent": self.agent,
            "ok": ok,
            "exit_code": self.exit_code,
            "marker_seen": self.marker_seen,
            "marker_source": self.marker_source,
            "stdout_excerpt": _excerpt(stdout_tail),
            "stderr_excerpt": _excerpt(stderr_tail),
            "last_message_excerpt": _excerpt(last_message),
            "reason": reason,
            "argv": list(self.argv),
            "raw_events_path": str(self.raw_events_path),
            "last_message_path": str(self.last_message_path),
            "stderr_path": str(self.stderr_path),
        }


def probe_agent_shell_launch(
    *,
    agent: str,
    workspace_dir: Path | str,
    artifacts_dir: Path,
    binary: str,
    model: str | None = None,
    command_prefix: list[str] | tuple[str, ...] = (),
    env_overrides: dict[str, str] | None = None,
    codex_sandbox: str | None = None,
    codex_ask_for_approval: str | None = None,
    codex_subcommand: str = "exec",
    codex_config_overrides: list[str] | tuple[str, ...] = (),
    codex_agent_last_message_path: str | None = None,
    claude_output_format: str = "stream-json",
    claude_allowed_tools: list[str] | tuple[str, ...] = (),
    claude_permission_mode: str | None = None,
    gemini_output_format: str = "stream-json",
    gemini_sandbox: bool = True,
    gemini_approval_mode: str = "default",
    gemini_allowed_tools: list[str] | tuple[str, ...] = (),
    gemini_include_directories: list[str] | tuple[str, ...] = (),
) -> AgentShellProbeResult:
    """
    Probe shell launchability through the selected agent adapter.

    This intentionally uses the same adapter entrypoints as normal mission dispatch, so preflight
    evidence represents the effective agent/backend/sandbox path rather than a generic host shell
    command that may bypass the failing agent shell backend.
    """

    agent_norm = agent.strip().lower()
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    raw_events_path = artifacts_dir / "raw_events.jsonl"
    last_message_path = artifacts_dir / "agent_last_message.txt"
    stderr_path = artifacts_dir / "agent_stderr.txt"
    prompt = _probe_prompt(agent_norm)
    prefix = [p for p in command_prefix if isinstance(p, str) and p]

    try:
        if agent_norm == "codex":
            result = run_codex_exec(
                workspace_dir=workspace_dir,
                prompt=prompt,
                raw_events_path=raw_events_path,
                last_message_path=last_message_path,
                stderr_path=stderr_path,
                sandbox=str(codex_sandbox or "read-only"),
                ask_for_approval=str(codex_ask_for_approval or "never"),
                binary=binary,
                subcommand=codex_subcommand,
                model=model,
                config_overrides=codex_config_overrides,
                ignore_rules=True,
                command_prefix=prefix,
                env_overrides=env_overrides,
                agent_last_message_path=codex_agent_last_message_path,
            )
            return _result_from_paths(
                agent=agent_norm,
                argv=result.argv,
                exit_code=result.exit_code,
                raw_events_path=raw_events_path,
                last_message_path=last_message_path,
                stderr_path=stderr_path,
            )

        if agent_norm == "claude":
            result = run_claude_print(
                workspace_dir=workspace_dir,
                prompt=prompt,
                raw_events_path=raw_events_path,
                last_message_path=last_message_path,
                stderr_path=stderr_path,
                binary=binary,
                output_format=claude_output_format,
                model=model,
                allowed_tools=claude_allowed_tools,
                permission_mode=claude_permission_mode,
                command_prefix=prefix,
                env_overrides=env_overrides,
            )
            return _result_from_paths(
                agent=agent_norm,
                argv=result.argv,
                exit_code=result.exit_code,
                raw_events_path=raw_events_path,
                last_message_path=last_message_path,
                stderr_path=stderr_path,
            )

        if agent_norm == "gemini":
            result = run_gemini(
                workspace_dir=workspace_dir,
                prompt=prompt,
                raw_events_path=raw_events_path,
                last_message_path=last_message_path,
                stderr_path=stderr_path,
                binary=binary,
                output_format=gemini_output_format,
                sandbox=gemini_sandbox,
                model=model,
                approval_mode=gemini_approval_mode,
                allowed_tools=gemini_allowed_tools,
                include_directories=gemini_include_directories,
                command_prefix=prefix,
                env_overrides=env_overrides,
            )
            return _result_from_paths(
                agent=agent_norm,
                argv=result.argv,
                exit_code=result.exit_code,
                raw_events_path=raw_events_path,
                last_message_path=last_message_path,
                stderr_path=stderr_path,
            )
    except Exception as exc:  # noqa: BLE001
        stderr_path.parent.mkdir(parents=True, exist_ok=True)
        stderr_path.write_text(str(exc) + "\n", encoding="utf-8", newline="\n")
        return AgentShellProbeResult(
            agent=agent_norm or agent,
            argv=[binary],
            exit_code=1,
            raw_events_path=raw_events_path,
            last_message_path=last_message_path,
            stderr_path=stderr_path,
            marker_seen=False,
            marker_source=None,
            error=str(exc),
        )

    raise ValueError(f"Unsupported agent for shell launch probe: {agent!r}")


def _probe_prompt(agent: str) -> str:
    shell_hint = (
        "Use Bash"
        if agent == "claude"
        else ("Use run_shell_command" if agent == "gemini" else "Use the shell command tool")
    )
    return (
        "Shell capability preflight probe.\n"
        f"{shell_hint} to run a command that prints exactly {_SHELL_PROBE_MARKER}.\n"
        "Do not read or write repository files. After the shell command completes, briefly report "
        "that the preflight probe finished.\n"
    )


def _result_from_paths(
    *,
    agent: str,
    argv: list[str],
    exit_code: int,
    raw_events_path: Path,
    last_message_path: Path,
    stderr_path: Path,
) -> AgentShellProbeResult:
    marker_source = _find_shell_marker_source(agent=agent, raw_events_path=raw_events_path)
    return AgentShellProbeResult(
        agent=agent,
        argv=argv,
        exit_code=exit_code,
        raw_events_path=raw_events_path,
        last_message_path=last_message_path,
        stderr_path=stderr_path,
        marker_seen=marker_source is not None,
        marker_source=marker_source,
    )


def _find_shell_marker_source(*, agent: str, raw_events_path: Path) -> str | None:
    for payload in _iter_json_payloads(raw_events_path):
        if agent == "codex":
            source = _codex_marker_source(payload)
        elif agent == "claude":
            source = _claude_marker_source(payload)
        elif agent == "gemini":
            source = _gemini_marker_source(payload)
        else:
            source = None
        if source is not None:
            return source
    return None


def _iter_json_payloads(path: Path) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return payloads
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            payload = json.loads(stripped)
        except Exception:
            continue
        if isinstance(payload, dict):
            payloads.append(payload)
    return payloads


def _codex_marker_source(payload: dict[str, Any]) -> str | None:
    msg = payload.get("msg")
    msg_dict = msg if isinstance(msg, dict) else {}
    if msg_dict.get("type") == "exec_command_end" and _marker_in_shell_fields(msg_dict):
        return "codex.exec_command_end"

    item = payload.get("item")
    item_dict = item if isinstance(item, dict) else {}
    if item_dict.get("type") == "command_execution" and _marker_in_shell_fields(item_dict):
        return "codex.command_execution"
    return None


def _claude_marker_source(payload: dict[str, Any]) -> str | None:
    message = payload.get("message")
    message_dict = message if isinstance(message, dict) else {}
    content = message_dict.get("content")
    if not isinstance(content, list):
        return None
    for block in content:
        block_dict = block if isinstance(block, dict) else {}
        if block_dict.get("type") != "tool_result":
            continue
        content_value = block_dict.get("content")
        if isinstance(content_value, str) and _SHELL_PROBE_MARKER in content_value:
            return "claude.tool_result"
        if isinstance(content_value, list):
            for item in content_value:
                item_dict = item if isinstance(item, dict) else {}
                text = item_dict.get("text") or item_dict.get("content")
                if isinstance(text, str) and _SHELL_PROBE_MARKER in text:
                    return "claude.tool_result"
    return None


def _gemini_marker_source(payload: dict[str, Any]) -> str | None:
    if payload.get("type") != "tool_result":
        return None
    if _marker_in_shell_fields(payload):
        return "gemini.tool_result"
    return None


def _marker_in_shell_fields(payload: dict[str, Any]) -> bool:
    for key in ("stdout", "output", "stderr", "aggregated_output"):
        value = payload.get(key)
        if isinstance(value, str) and _SHELL_PROBE_MARKER in value:
            return True
    return False


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _read_tail(path: Path) -> str:
    try:
        size = path.stat().st_size
    except OSError:
        return ""
    if size <= 0:
        return ""
    try:
        with path.open("rb") as f:
            f.seek(max(0, size - _TAIL_BYTES))
            data = f.read()
    except OSError:
        return ""
    return data.decode("utf-8", errors="replace")


def _excerpt(text: str, *, limit: int = 1000) -> str:
    stripped = text.strip()
    if not stripped:
        return ""
    try:
        parsed = json.loads(stripped)
    except Exception:
        parsed = None
    if isinstance(parsed, dict):
        compact = json.dumps(parsed, ensure_ascii=False)
        return compact[-limit:]
    return stripped[-limit:]
