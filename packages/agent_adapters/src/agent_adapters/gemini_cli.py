from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from json import JSONDecoder
from pathlib import Path
from typing import Any

from agent_adapters.docker_exec_env import inject_docker_exec_env, looks_like_docker_exec_prefix


@dataclass(frozen=True)
class GeminiRunResult:
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


def _extract_json_object_candidate(text: str) -> dict[str, Any] | None:
    raw = text.strip()
    if not raw:
        return None

    if raw.startswith("```"):
        lines = raw.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        raw = "\n".join(lines).strip()

    try:
        parsed = json.loads(raw)
    except Exception:
        parsed = None
    if isinstance(parsed, dict):
        return parsed

    decoder = JSONDecoder()
    for idx, char in enumerate(raw):
        if char != "{":
            continue
        try:
            parsed_obj, _ = decoder.raw_decode(raw[idx:])
        except Exception:
            continue
        if isinstance(parsed_obj, dict):
            return parsed_obj
    return None


def _extract_last_message_text(raw_events_path: Path) -> str:
    # Handle `--output-format json` (single JSON object).
    try:
        payload = json.loads(raw_events_path.read_text(encoding="utf-8"))
    except Exception:
        payload = None

    if isinstance(payload, dict):
        response = payload.get("response")
        if isinstance(response, str):
            return response

    # Handle `--output-format stream-json` (JSONL).
    #
    # Gemini often streams assistant output as many `delta=true` message events without emitting
    # a final `delta=false` "full" message. For runner use-cases (extracting a final report),
    # we want the last contiguous assistant segment, not the entire transcript.
    last_segment: str = ""
    current: str = ""
    recovered_json_obj: dict[str, Any] | None = None

    def _flush() -> None:
        nonlocal last_segment, current
        if current:
            last_segment = current
            current = ""

    for obj in _iter_json_lines(raw_events_path):
        event_type = obj.get("type")

        if event_type == "tool_use":
            _flush()
            tool_name = obj.get("tool_name")
            params = obj.get("parameters")
            if tool_name == "write_file" and isinstance(params, dict):
                content = params.get("content")
                if isinstance(content, str):
                    candidate = _extract_json_object_candidate(content)
                    if candidate is not None:
                        recovered_json_obj = candidate
            continue

        if event_type == "tool_result":
            _flush()
            output = obj.get("output")
            if isinstance(output, str):
                candidate = _extract_json_object_candidate(output)
                if candidate is not None:
                    recovered_json_obj = candidate
            continue

        if event_type == "message":
            role = obj.get("role")
            if role != "assistant":
                _flush()
                continue

            content = obj.get("content")
            if not isinstance(content, str) or not content:
                continue

            if obj.get("delta") is True:
                current += content
            else:
                current = content
            continue

    _flush()
    direct_candidate = _extract_json_object_candidate(last_segment)
    if direct_candidate is not None:
        return json.dumps(direct_candidate, indent=2, ensure_ascii=False)
    if recovered_json_obj is not None:
        return json.dumps(recovered_json_obj, indent=2, ensure_ascii=False)
    return last_segment


def run_gemini(
    *,
    workspace_dir: Path | str,
    prompt: str,
    raw_events_path: Path,
    last_message_path: Path,
    stderr_path: Path,
    binary: str = "gemini",
    output_format: str = "stream-json",
    sandbox: bool = True,
    model: str | None = None,
    system_prompt_file: str | Path | None = None,
    approval_mode: str = "default",
    allowed_tools: Iterable[str] = (),
    include_directories: Iterable[str] = (),
    command_prefix: Iterable[str] = (),
    env_overrides: dict[str, str] | None = None,
) -> GeminiRunResult:
    raw_events_path.parent.mkdir(parents=True, exist_ok=True)
    last_message_path.parent.mkdir(parents=True, exist_ok=True)
    stderr_path.parent.mkdir(parents=True, exist_ok=True)

    prefix = [p for p in command_prefix if isinstance(p, str) and p]
    resolved_binary = binary if prefix else _resolve_executable(binary)

    # NOTE: Gemini CLI can read the prompt from stdin for non-interactive runs. We always stream
    # the full prompt via stdin to avoid Windows command-line length limits.
    argv: list[str] = [
        resolved_binary,
        "--output-format",
        output_format,
        "--approval-mode",
        approval_mode,
    ]
    if sandbox:
        argv.append("--sandbox")
    if model is not None:
        argv.extend(["--model", model])
    if system_prompt_file is not None:
        argv.extend(["--agent-system-prompt-file", str(system_prompt_file)])

    tools = [t for t in allowed_tools if isinstance(t, str) and t.strip()]
    for tool in tools:
        argv.extend(["--allowed-tools", tool])

    include_dirs = [d for d in include_directories if isinstance(d, str) and d.strip()]
    for directory in include_dirs:
        argv.extend(["--include-directories", directory])

    full_argv = [*prefix, *argv] if prefix else argv

    with raw_events_path.open("w", encoding="utf-8", newline="\n") as stdout_f, stderr_path.open(
        "w", encoding="utf-8", newline="\n"
    ) as stderr_f:
        try:
            env: dict[str, str] | None = None
            if env_overrides is not None:
                if prefix and looks_like_docker_exec_prefix(prefix):
                    full_argv = [*inject_docker_exec_env(prefix, env_overrides), *argv]
                else:
                    env = os.environ.copy()
                    env.update(env_overrides)
            proc = subprocess.run(
                full_argv,
                input=prompt,
                stdout=stdout_f,
                stderr=stderr_f,
                text=True,
                encoding="utf-8",
                cwd=str(workspace_dir) if not prefix else None,
                env=env,
                check=False,
            )
        except FileNotFoundError as e:
            stderr_f.write(
                "Failed to launch Gemini CLI.\n"
                f"binary={binary!r}\n"
                f"resolved={resolved_binary!r}\n"
                f"argv={full_argv!r}\n"
            )
            raise RuntimeError(
                "Could not launch Gemini CLI process. "
                f"binary={binary!r} resolved={resolved_binary!r}. "
                "Ensure `gemini` is installed and on PATH, or set "
                "configs/agents.yaml `agents.gemini.binary` to the full path."
            ) from e

    last_message_path.write_text(_extract_last_message_text(raw_events_path), encoding="utf-8")

    return GeminiRunResult(
        argv=full_argv,
        exit_code=proc.returncode,
        raw_events_path=raw_events_path,
        last_message_path=last_message_path,
        stderr_path=stderr_path,
    )
