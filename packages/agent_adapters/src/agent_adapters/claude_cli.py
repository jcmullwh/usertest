from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_adapters.docker_exec_env import inject_docker_exec_env, looks_like_docker_exec_prefix
from agent_adapters.events import utc_now_iso


@dataclass(frozen=True)
class ClaudePrintResult:
    argv: list[str]
    exit_code: int
    raw_events_path: Path
    last_message_path: Path
    stderr_path: Path


def _resolve_executable(binary: str) -> str:
    p = Path(binary)
    if p.is_absolute():
        return str(p)

    if any(sep in binary for sep in ("/", "\\")) or (os.name == "nt" and ":" in binary):
        return binary

    resolved = shutil.which(binary)
    return resolved if resolved is not None else binary


def _iter_json_lines(path: Path) -> Iterator[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            raw = line.strip()
            if not raw:
                continue
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                yield payload


def _extract_last_message_text(raw_events_path: Path) -> str:
    try:
        payload = json.loads(raw_events_path.read_text(encoding="utf-8"))
    except Exception:
        payload = None

    if isinstance(payload, dict):
        result = payload.get("result")
        if isinstance(result, str):
            return result

    last_text: str | None = None
    for obj in _iter_json_lines(raw_events_path):
        obj_type = obj.get("type")
        if obj_type == "result":
            result = obj.get("result")
            if isinstance(result, str) and result.strip():
                last_text = result
            continue

        if obj_type != "assistant":
            continue

        msg = obj.get("message")
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue

        parts: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") != "text":
                continue
            text = block.get("text")
            if isinstance(text, str) and text:
                parts.append(text)
        if parts:
            last_text = "".join(parts)

    return last_text or ""


def run_claude_print(
    *,
    workspace_dir: Path | str,
    prompt: str,
    raw_events_path: Path,
    last_message_path: Path,
    stderr_path: Path,
    binary: str = "claude",
    output_format: str = "stream-json",
    model: str | None = None,
    allowed_tools: Iterable[str] = (),
    permission_mode: str | None = None,
    system_prompt: str | None = None,
    system_prompt_file: str | Path | None = None,
    append_system_prompt: str | None = None,
    append_system_prompt_file: str | Path | None = None,
    max_turns: int | None = None,
    command_prefix: Iterable[str] = (),
    env_overrides: dict[str, str] | None = None,
) -> ClaudePrintResult:
    raw_events_path.parent.mkdir(parents=True, exist_ok=True)
    last_message_path.parent.mkdir(parents=True, exist_ok=True)
    stderr_path.parent.mkdir(parents=True, exist_ok=True)
    raw_events_ts_path = raw_events_path.with_suffix(".ts.jsonl")

    prefix = [p for p in command_prefix if isinstance(p, str) and p]
    resolved_binary = binary if prefix else _resolve_executable(binary)
    argv: list[str] = [resolved_binary, "-p", "--output-format", output_format]
    if output_format.strip().lower() == "stream-json":
        argv.append("--verbose")
    if model is not None:
        argv.extend(["--model", model])
    if max_turns is not None:
        argv.extend(["--max-turns", str(max_turns)])
    if permission_mode is not None:
        argv.extend(["--permission-mode", permission_mode])

    if system_prompt is not None and system_prompt_file is not None:
        raise ValueError("Claude system_prompt and system_prompt_file are mutually exclusive.")
    if append_system_prompt is not None and append_system_prompt_file is not None:
        raise ValueError(
            "Claude append_system_prompt and append_system_prompt_file are mutually exclusive."
        )

    if system_prompt is not None:
        argv.extend(["--system-prompt", system_prompt])
    if system_prompt_file is not None:
        argv.extend(["--system-prompt-file", str(system_prompt_file)])
    if append_system_prompt is not None:
        argv.extend(["--append-system-prompt", append_system_prompt])
    if append_system_prompt_file is not None:
        argv.extend(["--append-system-prompt-file", str(append_system_prompt_file)])

    tools = [t for t in allowed_tools if isinstance(t, str) and t.strip()]
    if tools:
        argv.extend(["--allowedTools", ",".join(tools)])

    full_argv = [*prefix, *argv] if prefix else argv

    with (
        raw_events_path.open("w", encoding="utf-8", newline="\n") as stdout_f,
        raw_events_ts_path.open("w", encoding="utf-8", newline="\n") as ts_f,
        stderr_path.open("w", encoding="utf-8", newline="\n") as stderr_f,
    ):
        try:
            env: dict[str, str] | None = None
            if env_overrides is not None:
                if prefix and looks_like_docker_exec_prefix(prefix):
                    full_argv = [*inject_docker_exec_env(prefix, env_overrides), *argv]
                    env = None
                else:
                    env = os.environ.copy()
                    env.update(env_overrides)
            proc = subprocess.Popen(
                full_argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=stderr_f,
                text=True,
                encoding="utf-8",
                cwd=str(workspace_dir) if not prefix else None,
                env=env,
            )
        except FileNotFoundError as e:
            stderr_f.write(
                "Failed to launch Claude CLI.\n"
                f"binary={binary!r}\n"
                f"resolved={resolved_binary!r}\n"
                f"argv={full_argv!r}\n"
            )
            raise RuntimeError(
                "Could not launch Claude CLI process. "
                f"binary={binary!r} resolved={resolved_binary!r}. "
                "Ensure `claude` is installed and on PATH, or set "
                "configs/agents.yaml `agents.claude.binary` to the full path."
            ) from e

        if proc.stdin is not None:
            try:
                proc.stdin.write(prompt)
            except BrokenPipeError:
                pass
            finally:
                try:
                    proc.stdin.close()
                except Exception:
                    pass

        if proc.stdout is not None:
            for line in proc.stdout:
                stdout_f.write(line)
                stdout_f.flush()
                if line.strip():
                    ts_f.write(utc_now_iso() + "\n")
                    ts_f.flush()

        proc.wait()

    last_message_path.write_text(_extract_last_message_text(raw_events_path), encoding="utf-8")

    return ClaudePrintResult(
        argv=full_argv,
        exit_code=proc.returncode if proc.returncode is not None else 1,
        raw_events_path=raw_events_path,
        last_message_path=last_message_path,
        stderr_path=stderr_path,
    )
