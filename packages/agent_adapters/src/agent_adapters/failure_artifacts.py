from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.write_text(text, encoding="utf-8", newline="\n")
    except OSError:
        pass


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
            newline="\n",
        )
    except OSError:
        pass


def write_command_failure_artifacts(
    *,
    run_dir: Path,
    failure_index: int,
    command: str,
    argv: list[str] | None,
    cwd: str | None,
    exit_code: int | None,
    stdout_text: str | None,
    stderr_text: str | None,
    duration: dict[str, Any] | None = None,
    env_allowlist: list[str] | None = None,
    os_error_code: int | None = None,
) -> dict[str, str]:
    """
    Persist standard command failure artifacts under the run directory.

    Contract (relative to `run_dir`):
      command_failures/cmd_XX/command.json
      command_failures/cmd_XX/stdout.txt
      command_failures/cmd_XX/stderr.txt
      command_failures/cmd_XX/timing.json
    """

    rel_dir = Path("command_failures") / f"cmd_{int(failure_index):02d}"
    abs_dir = run_dir / rel_dir
    abs_dir.mkdir(parents=True, exist_ok=True)

    command_payload: dict[str, Any] = {
        "schema_version": 1,
        "command": command,
    }
    if argv is not None:
        command_payload["argv"] = argv
    if cwd is not None:
        command_payload["cwd"] = cwd
    if exit_code is not None:
        command_payload["exit_code"] = int(exit_code)
    if os_error_code is not None:
        command_payload["os_error_code"] = int(os_error_code)
    if env_allowlist is not None:
        command_payload["env_allowlist"] = [str(k) for k in env_allowlist if str(k).strip()]

    timing_payload: dict[str, Any] = {"schema_version": 1}
    if duration is not None:
        timing_payload["duration"] = duration

    _write_json(abs_dir / "command.json", command_payload)
    _write_text(abs_dir / "stdout.txt", stdout_text or "")
    _write_text(abs_dir / "stderr.txt", stderr_text or "")
    _write_json(abs_dir / "timing.json", timing_payload)

    return {
        "dir": rel_dir.as_posix(),
        "command_json": (rel_dir / "command.json").as_posix(),
        "stdout": (rel_dir / "stdout.txt").as_posix(),
        "stderr": (rel_dir / "stderr.txt").as_posix(),
        "timing_json": (rel_dir / "timing.json").as_posix(),
    }


def write_tool_failure_artifacts(
    *,
    run_dir: Path,
    failure_index: int,
    tool_name: str,
    tool_input: dict[str, Any],
    error_text: str | None,
    extracted: dict[str, Any] | None = None,
    context_text: str | None = None,
    preview_text: str | None = None,
) -> dict[str, str]:
    rel_dir = Path("tool_failures") / f"tool_{int(failure_index):02d}_{tool_name.strip().lower()}"
    abs_dir = run_dir / rel_dir
    abs_dir.mkdir(parents=True, exist_ok=True)

    payload: dict[str, Any] = {
        "schema_version": 1,
        "tool": tool_name,
        "input": tool_input,
    }
    if error_text is not None:
        payload["error_text"] = error_text
    if extracted:
        payload["extracted"] = extracted

    _write_json(abs_dir / "tool.json", payload)
    if context_text is not None:
        _write_text(abs_dir / "context.txt", context_text)
    if preview_text is not None:
        _write_text(abs_dir / "preview.txt", preview_text)

    out: dict[str, str] = {
        "dir": rel_dir.as_posix(),
        "tool_json": (rel_dir / "tool.json").as_posix(),
    }
    if context_text is not None:
        out["context"] = (rel_dir / "context.txt").as_posix()
    if preview_text is not None:
        out["preview"] = (rel_dir / "preview.txt").as_posix()
    return out

