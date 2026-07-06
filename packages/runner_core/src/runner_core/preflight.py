from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from runner_core.python_interpreter_probe import resolve_usable_python_interpreter

# These values bound small preflight probes only. They are deliberately named
# probe budgets rather than implementation-run timeouts so they cannot be
# mistaken for a cap on agent or verification execution.
_PYTHON_INTERPRETER_PROBE_BUDGET_SECONDS = 5.0
_PDM_VERSION_PROBE_BUDGET_SECONDS = 2.5
_BASH_LAUNCH_PROBE_BUDGET_SECONDS = 2.0
_LOCAL_SHELL_PAYLOAD_PROBE_BUDGET_SECONDS = 2.5

_BASE_PREFLIGHT_COMMANDS = [
    "git",
    "rg",
    "bash",
    "python3",
    "python",
    "py",
    "pip",
    "pip3",
    "pdm",
    "node",
    "npm",
    # Common package managers / installers (useful for dependency bootstrapping).
    "apt-get",
    "apk",
    "dnf",
    "yum",
    "pacman",
    "brew",
    "choco",
    "winget",
    "scoop",
]

def _build_preflight_command_list(request: Any) -> list[str]:
    """
    Build the ordered list of command names to probe during preflight.

    Preflight is intended to be generic: the baseline list contains common developer tooling and
    installer entry points, while repo-specific dependencies can be supplied per run via
    `RunRequest.preflight_commands` (CLI: `--preflight-command`) and required checks can be
    supplied via `RunRequest.preflight_required_commands` (CLI: `--require-preflight-command`).
    """

    merged: list[str] = []
    seen: set[str] = set()

    candidates: list[str] = [
        *_BASE_PREFLIGHT_COMMANDS,
        *request.preflight_commands,
        *request.preflight_required_commands,
    ]
    for raw in candidates:
        if not isinstance(raw, str):
            continue
        cmd = raw.strip()
        if not cmd or cmd in seen:
            continue
        merged.append(cmd)
        seen.add(cmd)

    return merged

def _agent_binary_for_preflight_probe(*, agent: str, agent_cfg: dict[str, Any]) -> str | None:
    default_binary = {
        "codex": "codex",
        "claude": "claude",
        "gemini": "gemini",
    }.get(agent, "")
    raw_binary = agent_cfg.get("binary", default_binary)
    if not isinstance(raw_binary, str) or not raw_binary.strip():
        return None

    binary = raw_binary.strip()
    if Path(binary).is_absolute():
        return None
    if any(sep in binary for sep in ("/", "\\")):
        return None
    if os.name == "nt" and ":" in binary:
        return None

    return binary


def _probe_commands_local(
    commands: list[str],
    *,
    workspace_dir: Path | None = None,
    env_overrides: dict[str, str] | None = None,
) -> tuple[dict[str, bool], dict[str, Any]]:
    out: dict[str, bool] = {}
    probe_details: dict[str, dict[str, Any]] = {}
    effective_env: dict[str, str] | None = None
    effective_path: str | None = None
    if env_overrides:
        effective_env = dict(os.environ)
        for key, value in env_overrides.items():
            if not isinstance(key, str) or not isinstance(value, str):
                continue
            effective_env[key] = value
        effective_path = env_overrides.get("PATH")
    python_commands = [cmd for cmd in commands if cmd in {"python", "python3", "py"}]
    python_probe = (
        resolve_usable_python_interpreter(
            workspace_dir=workspace_dir,
            candidate_commands=python_commands,
            timeout_seconds=_PYTHON_INTERPRETER_PROBE_BUDGET_SECONDS,
            path=effective_path,
        )
        if python_commands
        else None
    )
    python_by_command = python_probe.by_command() if python_probe is not None else {}
    for cmd in commands:
        if not isinstance(cmd, str) or not cmd.strip():
            continue
        if cmd in python_by_command:
            candidate = python_by_command[cmd]
            out[cmd] = bool(candidate.usable)
            probe_details[cmd] = candidate.to_dict()
            continue

        resolved = (
            shutil.which(cmd, path=effective_path)
            if effective_path is not None
            else shutil.which(cmd)
        )
        present = resolved is not None
        usable = present
        reason_code: str | None = None if present else "not_found"
        reason: str | None = None if present else f"`{cmd}` was not found on PATH."

        if resolved is not None and cmd in {"pdm"}:
            # Some environments can resolve `pdm` but block execution or hang at import time.
            try:
                proc = subprocess.run(
                    [resolved, "--version"],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=_PDM_VERSION_PROBE_BUDGET_SECONDS,
                    check=False,
                    env=effective_env,
                )
                usable = int(proc.returncode or 0) == 0
                probe_details[cmd] = {
                    "command": cmd,
                    "resolved_path": resolved,
                    "present": present,
                    "usable": bool(usable),
                    "probe_argv": [resolved, "--version"],
                    "probe_exit_code": int(proc.returncode or 0),
                    "probe_stdout_excerpt": (proc.stdout or "").strip()[:300] or None,
                    "probe_stderr_excerpt": (proc.stderr or "").strip()[:300] or None,
                }
                if not usable:
                    reason_code = "probe_failed"
                    details_parts = [
                        (proc.stderr or "").strip(),
                        (proc.stdout or "").strip(),
                    ]
                    details = "; ".join([p for p in details_parts if p]) or (
                        f"exit_code={proc.returncode}"
                    )
                    reason = f"pdm probe exited non-zero: {details}"
            except subprocess.TimeoutExpired:
                usable = False
                reason_code = "unresponsive"
                reason = "pdm probe timed out (2.5s) running `pdm --version`."
            except OSError as e:
                usable = False
                reason_code = "blocked"
                reason = f"pdm probe failed: {e}"
            if cmd in probe_details:
                probe_details[cmd]["reason_code"] = reason_code
                probe_details[cmd]["reason"] = reason

        if cmd == "bash" and os.name == "nt" and resolved is not None:
            # On some Windows sandboxes, bash.exe may be on PATH (e.g., Git Bash) but execution is
            # blocked by policy ("Access is denied"). Probe by actually starting bash.
            try:
                proc = subprocess.run(
                    [resolved, "-lc", "echo ok"],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=_BASH_LAUNCH_PROBE_BUDGET_SECONDS,
                    check=False,
                    env=effective_env,
                )
                usable = int(proc.returncode or 0) == 0
                if not usable:
                    reason_code = "probe_failed"
                    stderr = (proc.stderr or "").strip()
                    reason = "bash probe exited non-zero" + (
                        f": {stderr}" if stderr else f" (exit_code={proc.returncode})"
                    )
            except subprocess.TimeoutExpired:
                usable = False
                reason_code = "unresponsive"
                reason = 'bash probe timed out (2.0s) running `bash -lc "echo ok"`.'
            except OSError as e:
                usable = False
                reason_code = "blocked"
                reason = f"bash probe failed: {e}"

        out[cmd] = bool(usable)
        probe_details.setdefault(
            cmd,
            {
                "command": cmd,
                "resolved_path": resolved,
                "present": present,
                "usable": bool(usable),
                "reason_code": reason_code,
                "reason": reason,
            },
        )

    meta: dict[str, Any] = {"command_probe_details": probe_details}
    meta["shell_probe"] = _probe_local_shell_payload(
        workspace_dir=workspace_dir,
        env=effective_env,
    )
    if python_probe is not None:
        meta["python_interpreter"] = python_probe.to_dict()
    return out, meta


def _probe_local_shell_payload(
    *,
    workspace_dir: Path | None,
    env: dict[str, str] | None,
) -> dict[str, Any]:
    """
    Launch a payload-equivalent no-op through the local shell backend.

    Command discovery alone is not enough to prove shell capability: process creation can still be
    blocked by sandbox or host policy.  This probe is deliberately tiny, bounded, and records the
    same canonical shape as the container probe so the shell capability resolver can distinguish
    "command exists" from "payload launch works".
    """

    if os.name == "nt":
        resolved = shutil.which("powershell", path=(env or os.environ).get("PATH"))
        if resolved is None:
            resolved = shutil.which("pwsh", path=(env or os.environ).get("PATH"))
        if resolved is None:
            return {
                "kind": "backend_shell_payload",
                "shell_family": "powershell",
                "exit_code": 1,
                "stdout": "",
                "stderr": "PowerShell executable was not found for local shell payload probe.",
                "reason_code": "not_found",
            }
        argv = [
            resolved,
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            "Write-Output 'shell_probe=ok'",
        ]
        shell_family = "powershell"
    else:
        resolved = shutil.which("sh", path=(env or os.environ).get("PATH")) or "sh"
        argv = [resolved, "-lc", "printf 'shell_probe=ok\\n'"]
        shell_family = "sh"

    try:
        proc = subprocess.run(
            argv,
            cwd=str(workspace_dir) if workspace_dir is not None else None,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_PDM_VERSION_PROBE_BUDGET_SECONDS,
            check=False,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return {
            "kind": "backend_shell_payload",
            "shell_family": shell_family,
            "exit_code": 124,
            "stdout": "",
            "stderr": "Local shell payload probe timed out.",
            "reason_code": "unresponsive",
            "probe_argv": argv,
        }
    except OSError as e:
        return {
            "kind": "backend_shell_payload",
            "shell_family": shell_family,
            "exit_code": 1,
            "stdout": "",
            "stderr": f"Local shell payload probe failed to launch: {e}",
            "reason_code": "blocked",
            "probe_argv": argv,
        }

    stdout = (proc.stdout or "").strip()
    stderr = (proc.stderr or "").strip()
    marker_seen = "shell_probe=ok" in stdout.splitlines()
    return {
        "kind": "backend_shell_payload",
        "shell_family": shell_family,
        "exit_code": int(proc.returncode or 0) if marker_seen else 1,
        "stdout": "shell_probe=ok" if marker_seen else stdout[:300],
        "stderr": (
            stderr[:300]
            if marker_seen
            else (stderr[:300] or "Local shell payload probe did not emit sentinel output.")
        ),
        "probe_argv": argv,
    }


def _format_windows_python_preflight_error(probe: Any) -> str:
    payload = probe.to_dict() if hasattr(probe, "to_dict") else {}
    candidates = payload.get("candidates") if isinstance(payload, dict) else None
    candidates_list = candidates if isinstance(candidates, list) else []
    lines = [
        "Python preflight failed on Windows: no usable interpreter could be resolved within ~5s.",
        "",
        "Tried:",
    ]
    for item in candidates_list:
        if not isinstance(item, dict):
            continue
        command = item.get("command")
        resolved_path = item.get("resolved_path")
        reason_code = item.get("reason_code")
        reason = item.get("reason")
        summary = f"{command} -> {resolved_path} ({reason_code})"
        lines.append("  - " + summary)
        if isinstance(reason, str) and reason.strip():
            tail = reason.strip()
            if len(tail) > 300:
                tail = tail[:300].rstrip() + "…"
            lines.append("      " + tail.replace("\n", "\n      "))
    lines.extend(
        [
            "",
            "Fix options:",
            "  1) Install CPython (python.org) or via winget: "
            "winget install -e --id Python.Python.3.13",
            "  2) Disable App Execution Alias shims: Settings -> Apps -> Advanced app settings -> "
            "App execution aliases -> turn off python.exe/python3.exe",
            "  3) Use a portable/vendored Python and put its folder first on PATH "
            "(or use --exec-backend docker)",
        ]
    )
    return "\n".join(lines)


def _ensure_windows_python_on_path(
    *,
    workspace_dir: Path,
    env_overrides: dict[str, str] | None,
) -> dict[str, str]:
    base = dict(env_overrides or {})
    probe = resolve_usable_python_interpreter(
        workspace_dir=workspace_dir,
        candidate_commands=("python", "python3", "py"),
        timeout_seconds=_PYTHON_INTERPRETER_PROBE_BUDGET_SECONDS,
        include_sys_executable=True,
    )
    if probe.selected_command is None:
        raise RuntimeError(_format_windows_python_preflight_error(probe))

    python_exe = probe.selected_executable or probe.selected_resolved_path or ""
    python_exe_s = python_exe.strip()
    if python_exe_s:
        base.setdefault("USERTEST_PYTHON", python_exe_s)
        python_dir = str(Path(python_exe_s).parent)
        prior_path = base.get("PATH", os.environ.get("PATH", ""))
        if prior_path:
            base["PATH"] = f"{python_dir}{os.pathsep}{prior_path}"
        else:
            base["PATH"] = python_dir
    return base

__all__ = (
    "_BASE_PREFLIGHT_COMMANDS",
    "_agent_binary_for_preflight_probe",
    "_build_preflight_command_list",
    "_ensure_windows_python_on_path",
    "_format_windows_python_preflight_error",
    "_probe_commands_local",
    "_probe_local_shell_payload",
)
