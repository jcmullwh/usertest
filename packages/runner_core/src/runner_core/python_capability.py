from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from agent_adapters.docker_exec_env import inject_docker_exec_env, looks_like_docker_exec_prefix

from runner_core.artifacts import _tail_text_for_prompt
from runner_core.python_runtime import (
    PythonRuntimeSelection,
    select_python_runtime,
    verification_commands_need_python,
)

VerificationShellArgvFunc = Callable[..., list[str]]
RewriteVerificationCommandForPythonFunc = Callable[..., tuple[str, bool]]
SelectPythonRuntimeFunc = Callable[..., PythonRuntimeSelection]
ProbePythonContextCapabilityFunc = Callable[..., dict[str, Any]]

_COMMAND_RESOLUTION_PROBE_BUDGET_SECONDS = 3.0
_PYTHON_COMMAND_PROBE_BUDGET_SECONDS = 6.0
_WRAPPER_COMMAND_PROBE_BUDGET_SECONDS = 6.0
_PYTHON_CONTEXT_PROBE_BUDGET_SECONDS = 6.0


def _default_verification_shell_argv(*, command_prefix: list[str], command: str) -> list[str]:
    if command_prefix:
        return [*command_prefix, "sh", "-lc", command]
    if os.name == "nt":
        return ["powershell", "-NoProfile", "-NonInteractive", "-Command", command]
    return ["sh", "-lc", command]


def _default_rewrite_verification_command_for_python(
    command: str,
    *,
    python_executable: str | None,
    is_powershell: bool,
) -> tuple[str, bool]:
    del python_executable, is_powershell
    return command, False


def _is_windows() -> bool:
    return os.name == "nt"


_PYTHON_CONTEXT_HEALTH_PROBE = (
    "import encodings, json, os, sys; "
    "print(json.dumps({"
    '"executable": sys.executable, '
    '"version": sys.version.split()[0], '
    '"prefix": sys.prefix, '
    '"base_prefix": getattr(sys, "base_prefix", None), '
    '"real_prefix": getattr(sys, "real_prefix", None), '
    '"exec_prefix": sys.exec_prefix, '
    '"base_exec_prefix": getattr(sys, "base_exec_prefix", None), '
    '"virtual_env": os.environ.get("VIRTUAL_ENV")'
    "}))"
)


def _reason_type_for_code(reason_code: str | None) -> str | None:
    if not isinstance(reason_code, str) or not reason_code.strip():
        return None
    code = reason_code.strip().lower()
    if code in {"not_found", "windowsapps_alias"}:
        return "discovery"
    if code in {
        "launch_failed",
        "access_denied",
        "timeout",
        "blocked",
        "unresponsive",
        "context_mismatch",
        "codex_windows_process_launch_blocked_by_policy",
        "codex_windows_powershell_prepayload_failed",
        "codex_windows_shell_launch_failed",
    }:
        return "execution"
    if code in {"pip_missing", "pytest_missing", "pdm_missing"}:
        return "dependency"
    if code in {"shell_policy_blocked"}:
        return "configuration"
    if code in {
        "missing_stdlib",
        "runtime_probe_failed",
        "pip_probe_failed",
        "pytest_probe_failed",
        "pdm_probe_failed",
        "probe_failed",
        "shell_probe_failed",
        "codex_windows_sandbox_panic",
        "codex_windows_shell_unprobed",
        "shell_capability_unprobed",
        "shell_command_discovered_without_launchability",
    }:
        return "runtime"
    return "unknown"


def _python_probe_remediation(reason_code: str | None) -> str | None:
    if not isinstance(reason_code, str):
        return None
    code = reason_code.strip().lower()
    if code == "windowsapps_alias":
        return "Install/select a full CPython interpreter (not a WindowsApps alias), then retry."
    if code in {"launch_failed", "access_denied"}:
        return "Python execution is blocked in this environment. Verify sandbox/policy access."
    if code == "missing_stdlib":
        return "Selected Python runtime is incomplete (missing stdlib). Reinstall Python."
    if code == "pytest_missing":
        return "Install pytest into the selected interpreter/environment, then retry."
    if code == "pdm_missing":
        return "Install PDM into the selected interpreter/environment, then retry."
    if code == "timeout":
        return "Python interpreter probe timed out. Verify interpreter health and policy limits."
    if code == "not_found":
        return "Python command is unavailable in the effective agent execution context."
    if code == "context_mismatch":
        return (
            "Selected Python runtime does not match the effective execution context. "
            "Clear leaked host runtime hints or select a backend-local interpreter."
        )
    return "Inspect probe stderr/stdout and selected interpreter metadata in preflight.json."


def _tool_command_probe_remediation(command_name: str, reason_code: str | None) -> str | None:
    code = reason_code.strip().lower() if isinstance(reason_code, str) else None
    if command_name == "pytest" and code in {"not_found", "pytest_missing"}:
        return "Install pytest into the selected interpreter/environment, then retry."
    if command_name == "pdm" and code in {
        "not_found",
        "pdm_missing",
        "probe_failed",
        "pdm_probe_failed",
    }:
        return (
            "Install `pdm` into the selected interpreter/environment "
            "(python -m pip install -U pdm), then retry."
        )
    return _python_probe_remediation(reason_code)


def _prepare_probe_environment(
    *,
    command_prefix: list[str],
    env_overrides: dict[str, str] | None,
) -> tuple[list[str], dict[str, str] | None]:
    merged_env: dict[str, str] | None = None
    effective_prefix = list(command_prefix)
    if env_overrides:
        safe_overrides = {
            key: value
            for key, value in env_overrides.items()
            if isinstance(key, str) and isinstance(value, str)
        }
        if safe_overrides:
            if effective_prefix and looks_like_docker_exec_prefix(effective_prefix):
                effective_prefix = inject_docker_exec_env(effective_prefix, safe_overrides)
            elif not effective_prefix:
                merged_env = dict(os.environ)
                merged_env.update(safe_overrides)
    return effective_prefix, merged_env


def _resolve_command_in_execution_context(
    *,
    command_name: str,
    command_prefix: list[str],
    cwd: Path,
    env_overrides: dict[str, str] | None,
    verification_shell_argv: VerificationShellArgvFunc = _default_verification_shell_argv,
    timeout_seconds: float = _COMMAND_RESOLUTION_PROBE_BUDGET_SECONDS,
) -> str | None:
    effective_prefix, merged_env = _prepare_probe_environment(
        command_prefix=command_prefix,
        env_overrides=env_overrides,
    )
    if not effective_prefix:
        effective_path = (
            env_overrides.get("PATH")
            if isinstance(env_overrides, dict) and isinstance(env_overrides.get("PATH"), str)
            else None
        )
        return (
            shutil.which(command_name, path=effective_path)
            if effective_path is not None
            else shutil.which(command_name)
        )

    proc = subprocess.run(
        verification_shell_argv(
            command_prefix=effective_prefix,
            command=f"command -v {shlex.quote(command_name)}",
        ),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(cwd),
        timeout=max(0.1, float(timeout_seconds)),
        check=False,
        env=merged_env,
    )
    if proc.returncode != 0:
        return None
    for line in proc.stdout.splitlines():
        candidate = line.strip()
        if candidate:
            return candidate
    return None


def _probe_same_shell_python_command(
    *,
    command_name: str,
    command_prefix: list[str],
    cwd: Path,
    env_overrides: dict[str, str] | None,
    verification_shell_argv: VerificationShellArgvFunc = _default_verification_shell_argv,
    timeout_seconds: float = _PYTHON_COMMAND_PROBE_BUDGET_SECONDS,
) -> dict[str, Any]:
    effective_prefix, merged_env = _prepare_probe_environment(
        command_prefix=command_prefix,
        env_overrides=env_overrides,
    )
    resolved_path: str | None = None
    stdout_text = ""
    stderr_text = ""
    exit_code = 0
    timed_out = False
    exception: str | None = None
    payload: dict[str, Any] | None = None

    try:
        resolved_path = _resolve_command_in_execution_context(
            command_name=command_name,
            command_prefix=command_prefix,
            cwd=cwd,
            env_overrides=env_overrides,
        )
        if resolved_path is None:
            return {
                "command": command_name,
                "resolved_path": None,
                "present": False,
                "usable": False,
                "status": "missing",
                "reason_code": "not_found",
                "reason_type": _reason_type_for_code("not_found"),
                "reason": f"`{command_name}` was not found in the verification runtime.",
                "remediation": _tool_command_probe_remediation(command_name, "not_found"),
            }

        if _is_windows() and "\\windowsapps\\" in resolved_path.replace("/", "\\").lower():
            return {
                "command": command_name,
                "resolved_path": resolved_path,
                "present": True,
                "usable": False,
                "status": "unusable",
                "reason_code": "windowsapps_alias",
                "reason_type": _reason_type_for_code("windowsapps_alias"),
                "reason": (
                    "Resolved to a WindowsApps launcher alias. "
                    "Install/select a full Python interpreter and retry."
                ),
                "remediation": _python_probe_remediation("windowsapps_alias"),
            }

        if effective_prefix:
            health_probe_command = (
                f"{shlex.quote(command_name)} -c {shlex.quote(_PYTHON_CONTEXT_HEALTH_PROBE)}"
            )
            argv = verification_shell_argv(
                command_prefix=effective_prefix,
                command=health_probe_command,
            )
        else:
            argv = [resolved_path, "-c", _PYTHON_CONTEXT_HEALTH_PROBE]

        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(cwd),
            timeout=max(0.1, float(timeout_seconds)),
            check=False,
            env=merged_env,
        )
        exit_code = int(proc.returncode or 0)
        stdout_text = proc.stdout or ""
        stderr_text = proc.stderr or ""
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        exit_code = 124
        if isinstance(exc.stdout, bytes):
            stdout_text = exc.stdout.decode("utf-8", "replace")
        else:
            stdout_text = exc.stdout or ""
        if isinstance(exc.stderr, bytes):
            stderr_text = exc.stderr.decode("utf-8", "replace")
        else:
            stderr_text = exc.stderr or ""
    except OSError as exc:
        exit_code = 1
        exception = str(exc)

    if exit_code == 0 and not timed_out and exception is None:
        for line in reversed(stdout_text.splitlines()):
            candidate = line.strip()
            if not candidate:
                continue
            try:
                decoded = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            if isinstance(decoded, dict):
                payload = decoded
                break

    merged = "\n".join(value for value in (stderr_text, stdout_text, exception) if value).strip()
    lowered = merged.lower()
    reason_code: str | None = None
    reason: str | None = None
    if timed_out:
        reason_code = "timeout"
        reason = "Python context probe timed out."
    elif exception is not None:
        reason_code = "launch_failed"
        reason = exception
    elif exit_code != 0:
        if "encodings" in lowered and (
            "modulenotfounderror" in lowered or "no module named" in lowered
        ):
            reason_code = "missing_stdlib"
        elif "access is denied" in lowered or "permission denied" in lowered:
            reason_code = "access_denied"
        elif "cannot be accessed by the system" in lowered:
            reason_code = "access_denied"
        elif "windowsapps" in lowered:
            reason_code = "windowsapps_alias"
        elif "not found" in lowered or "not recognized" in lowered:
            reason_code = "not_found"
        else:
            reason_code = "runtime_probe_failed"
        reason = merged or f"Probe command exited with code {exit_code}."
    elif payload is None:
        reason_code = "runtime_probe_failed"
        reason = "Probe command succeeded but did not emit parseable JSON metadata."

    usable = bool(exit_code == 0 and not timed_out and exception is None and payload is not None)
    return {
        "command": command_name,
        "resolved_path": resolved_path,
        "present": resolved_path is not None,
        "usable": usable,
        "status": "present" if usable else ("missing" if resolved_path is None else "unusable"),
        "reason_code": reason_code,
        "reason_type": _reason_type_for_code(reason_code),
        "reason": reason,
        "remediation": _tool_command_probe_remediation(command_name, reason_code),
        "exit_code": exit_code,
        "timed_out": timed_out,
        "stdout_tail": _tail_text_for_prompt(stdout_text),
        "stderr_tail": _tail_text_for_prompt(stderr_text),
        "exception": exception,
        "version": payload.get("version") if isinstance(payload, dict) else None,
        "executable": payload.get("executable") if isinstance(payload, dict) else None,
    }


def _probe_same_shell_wrapper_command(
    *,
    command_name: str,
    argv_suffix: list[str],
    command_prefix: list[str],
    cwd: Path,
    env_overrides: dict[str, str] | None,
    verification_shell_argv: VerificationShellArgvFunc = _default_verification_shell_argv,
    timeout_seconds: float = _WRAPPER_COMMAND_PROBE_BUDGET_SECONDS,
) -> dict[str, Any]:
    effective_prefix, merged_env = _prepare_probe_environment(
        command_prefix=command_prefix,
        env_overrides=env_overrides,
    )
    resolved_path: str | None = None
    stdout_text = ""
    stderr_text = ""
    exit_code = 0
    timed_out = False
    exception: str | None = None

    try:
        resolved_path = _resolve_command_in_execution_context(
            command_name=command_name,
            command_prefix=command_prefix,
            cwd=cwd,
            env_overrides=env_overrides,
        )
        if resolved_path is None:
            return {
                "command": command_name,
                "resolved_path": None,
                "present": False,
                "usable": False,
                "status": "missing",
                "reason_code": "not_found",
                "reason_type": _reason_type_for_code("not_found"),
                "reason": f"`{command_name}` was not found in the verification runtime.",
                "remediation": _python_probe_remediation("not_found"),
            }

        if effective_prefix:
            quoted_suffix = " ".join(shlex.quote(part) for part in argv_suffix)
            argv = verification_shell_argv(
                command_prefix=effective_prefix,
                command=f"{shlex.quote(command_name)} {quoted_suffix}".strip(),
            )
        else:
            argv = [resolved_path, *argv_suffix]

        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(cwd),
            timeout=max(0.1, float(timeout_seconds)),
            check=False,
            env=merged_env,
        )
        exit_code = int(proc.returncode or 0)
        stdout_text = proc.stdout or ""
        stderr_text = proc.stderr or ""
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        exit_code = 124
        if isinstance(exc.stdout, bytes):
            stdout_text = exc.stdout.decode("utf-8", "replace")
        else:
            stdout_text = exc.stdout or ""
        if isinstance(exc.stderr, bytes):
            stderr_text = exc.stderr.decode("utf-8", "replace")
        else:
            stderr_text = exc.stderr or ""
    except OSError as exc:
        exit_code = 1
        exception = str(exc)

    merged = "\n".join(value for value in (stderr_text, stdout_text, exception) if value).strip()
    lowered = merged.lower()
    reason_code: str | None = None
    if timed_out:
        reason_code = "timeout"
    elif exception is not None:
        reason_code = "launch_failed"
    elif exit_code != 0:
        if command_name == "pytest" and (
            "no module named pytest" in lowered
            or ("modulenotfounderror" in lowered and "pytest" in lowered)
        ):
            reason_code = "pytest_missing"
        elif command_name == "pdm" and (
            "no module named pdm" in lowered
            or ("modulenotfounderror" in lowered and "pdm" in lowered)
        ):
            reason_code = "pdm_missing"
        elif (
            "access is denied" in lowered
            or "permission denied" in lowered
            or "cannot be accessed by the system" in lowered
        ):
            reason_code = "access_denied"
        else:
            reason_code = f"{command_name}_probe_failed"

    usable = bool(exit_code == 0 and not timed_out and exception is None)
    return {
        "command": command_name,
        "resolved_path": resolved_path,
        "present": resolved_path is not None,
        "usable": usable,
        "status": "present" if usable else ("missing" if resolved_path is None else "unusable"),
        "reason_code": reason_code,
        "reason_type": _reason_type_for_code(reason_code),
        "reason": merged or exception,
        "remediation": _python_probe_remediation(reason_code),
        "argv": [command_name, *argv_suffix],
        "exit_code": exit_code,
        "timed_out": timed_out,
        "stdout_tail": _tail_text_for_prompt(stdout_text),
        "stderr_tail": _tail_text_for_prompt(stderr_text),
        "exception": exception,
    }


_REMOTE_RUNTIME_HINT_ENV_KEYS: tuple[str, ...] = (
    "VIRTUAL_ENV",
    "USERTEST_PYTHON",
    "PDM_PYTHON",
    "UV_PYTHON",
    "PYTHONHOME",
    "__PYVENV_LAUNCHER__",
    "CONDA_PREFIX",
)


def _normalize_runtime_path_key(path_text: str | None) -> str | None:
    if not isinstance(path_text, str):
        return None
    raw = path_text.strip()
    if not raw:
        return None
    normalized = raw.replace("\\", "/")
    return normalized.lower() if _is_windows() else normalized


def _context_verified_runtime_candidate(
    python_context_probe: dict[str, Any] | None,
) -> dict[str, Any] | None:
    probe_passed = bool(
        isinstance(python_context_probe, dict) and python_context_probe.get("passed", False)
    )
    if not probe_passed:
        return None
    metadata = (
        python_context_probe.get("metadata")
        if isinstance(python_context_probe.get("metadata"), dict)
        else None
    )
    if metadata is None:
        return None
    executable = metadata.get("executable")
    executable_s = executable.strip() if isinstance(executable, str) else ""
    if not executable_s:
        return None

    candidate: dict[str, Any] = {
        "source": "context_verified",
        "path": executable_s,
        "present": True,
        "usable": True,
        "reason_code": None,
        "reason_type": None,
        "reason": None,
    }
    for key in (
        "version",
        "executable",
        "prefix",
        "base_prefix",
        "real_prefix",
        "exec_prefix",
        "base_exec_prefix",
        "virtual_env",
    ):
        value = metadata.get(key)
        candidate[key] = value if isinstance(value, str) else None
    return candidate


def _rebuild_runtime_summary(runtime_summary: dict[str, Any]) -> dict[str, Any]:
    candidates = runtime_summary.get("candidates")
    candidate_list = (
        [item for item in candidates if isinstance(item, dict)]
        if isinstance(candidates, list)
        else []
    )
    selected = runtime_summary.get("selected")
    selected_dict = dict(selected) if isinstance(selected, dict) else None
    return {
        "selected": selected_dict,
        "candidates": candidate_list,
        "rejected": [item for item in candidate_list if not bool(item.get("usable", False))],
    }


def _reconcile_python_runtime_summary_with_context(
    *,
    python_runtime_summary: dict[str, Any],
    python_context_probe: dict[str, Any] | None,
    prefer_context_selection: bool,
) -> dict[str, Any]:
    summary = _rebuild_runtime_summary(python_runtime_summary)
    selected = summary.get("selected")
    selected_dict = dict(selected) if isinstance(selected, dict) else None
    verified_candidate = _context_verified_runtime_candidate(python_context_probe)
    selected_path_key = _normalize_runtime_path_key(
        selected_dict.get("path") if isinstance(selected_dict, dict) else None
    )

    if verified_candidate is not None and prefer_context_selection:
        verified_path_key = _normalize_runtime_path_key(verified_candidate.get("path"))
        candidates: list[dict[str, Any]] = [verified_candidate]
        if selected_dict is not None and selected_path_key != verified_path_key:
            demoted = dict(selected_dict)
            demoted["usable"] = False
            demoted["reason_code"] = "context_mismatch"
            demoted["reason_type"] = _reason_type_for_code("context_mismatch")
            demoted["reason"] = (
                "Host-selected interpreter does not match the execution backend verified runtime."
            )
            candidates.append(demoted)
        for candidate in summary.get("candidates", []):
            if not isinstance(candidate, dict):
                continue
            candidate_path_key = _normalize_runtime_path_key(candidate.get("path"))
            if candidate_path_key == verified_path_key:
                continue
            if selected_path_key is not None and candidate_path_key == selected_path_key:
                continue
            candidates.append(dict(candidate))
        return _rebuild_runtime_summary({"selected": verified_candidate, "candidates": candidates})

    probe_failed = isinstance(python_context_probe, dict) and not bool(
        python_context_probe.get("passed", False)
    )
    if probe_failed:
        if selected_dict is None:
            return summary
        reason_code = python_context_probe.get("reason_code")
        reason_code_s = reason_code if isinstance(reason_code, str) else "runtime_probe_failed"
        reason = python_context_probe.get("reason")
        demoted = dict(selected_dict)
        demoted["usable"] = False
        demoted["reason_code"] = reason_code_s
        demoted["reason_type"] = _reason_type_for_code(reason_code_s)
        demoted["reason"] = (
            reason
            if isinstance(reason, str) and reason.strip()
            else "Execution-context verification failed."
        )
        candidates = [demoted]
        for candidate in summary.get("candidates", []):
            if not isinstance(candidate, dict):
                continue
            candidate_path_key = _normalize_runtime_path_key(candidate.get("path"))
            if selected_path_key is not None and candidate_path_key == selected_path_key:
                continue
            candidates.append(dict(candidate))
        return _rebuild_runtime_summary({"selected": None, "candidates": candidates})

    return summary


def _sanitize_runtime_env_overrides(
    *,
    env_overrides: dict[str, str] | None,
    command_prefix: list[str],
) -> dict[str, str]:
    sanitized = dict(env_overrides or {})
    if command_prefix:
        for key in _REMOTE_RUNTIME_HINT_ENV_KEYS:
            sanitized.setdefault(key, "")
    return sanitized


def _primary_runtime_rejection(python_runtime_summary: dict[str, Any]) -> dict[str, Any]:
    rejected = python_runtime_summary.get("rejected")
    if isinstance(rejected, list):
        for item in rejected:
            if not isinstance(item, dict):
                continue
            reason_code = item.get("reason_code")
            reason = item.get("reason")
            if isinstance(reason_code, str) and reason_code.strip():
                return {
                    "reason_code": reason_code.strip(),
                    "reason_type": _reason_type_for_code(reason_code),
                    "reason": reason if isinstance(reason, str) and reason.strip() else None,
                    "remediation": _python_probe_remediation(reason_code),
                }
    return {
        "reason_code": "python_unavailable",
        "reason_type": "discovery",
        "reason": "No usable Python runtime candidate was selected.",
        "remediation": _python_probe_remediation("not_found"),
    }


def _align_python_command_diagnostics(
    *,
    command_diagnostics: dict[str, Any],
    python_runtime_summary: dict[str, Any],
    python_context_probe: dict[str, Any] | None,
    python_validation_required: bool,
    prefer_context_selection: bool = False,
    validated_python_executable: str | None = None,
) -> None:
    selected = python_runtime_summary.get("selected")
    selected_ok = isinstance(selected, dict)

    failure: dict[str, Any] | None = None
    if not selected_ok:
        failure = _primary_runtime_rejection(python_runtime_summary)
    elif isinstance(python_context_probe, dict):
        if not bool(python_context_probe.get("passed", False)):
            reason_code = python_context_probe.get("reason_code")
            reason = python_context_probe.get("reason")
            failure = {
                "reason_code": (
                    reason_code if isinstance(reason_code, str) else "runtime_probe_failed"
                ),
                "reason_type": _reason_type_for_code(
                    reason_code if isinstance(reason_code, str) else None
                ),
                "reason": reason if isinstance(reason, str) and reason.strip() else None,
                "remediation": (
                    python_context_probe.get("remediation")
                    if isinstance(python_context_probe.get("remediation"), str)
                    else _python_probe_remediation(
                        reason_code if isinstance(reason_code, str) else None
                    )
                ),
            }

    if failure is None:
        if prefer_context_selection:
            python_diag = command_diagnostics.get("python")
            if not isinstance(python_diag, dict):
                python_diag = {}
                command_diagnostics["python"] = python_diag
            if isinstance(validated_python_executable, str) and validated_python_executable.strip():
                python_diag["present"] = True
                python_diag["usable"] = True
                python_diag["status"] = "present"
                python_diag["resolved_path"] = validated_python_executable.strip()
                python_diag["reason_code"] = None
                python_diag["reason_type"] = None
                python_diag["reason"] = None
                python_diag["remediation"] = None
            for command in ("python3", "py"):
                diag = command_diagnostics.get(command)
                if isinstance(diag, dict):
                    diag["resolved_path"] = None
        return

    for command in ("python", "python3", "py", "pdm"):
        diag = command_diagnostics.get(command)
        if not isinstance(diag, dict):
            continue
        if diag.get("status") == "missing" and not prefer_context_selection:
            continue
        diag["usable"] = False
        diag["status"] = "unusable"
        if prefer_context_selection:
            diag["present"] = False
            diag["resolved_path"] = None
        diag["reason_code"] = failure.get("reason_code")
        diag["reason_type"] = failure.get("reason_type")
        if isinstance(failure.get("reason"), str):
            diag["reason"] = failure.get("reason")
        if isinstance(failure.get("remediation"), str):
            diag["remediation"] = failure.get("remediation")
        if command == "pdm":
            # pdm is a Python wrapper; mark its diagnostic with this dependency context so
            # consumers can distinguish a pdm-specific failure from a Python-runtime failure.
            diag["python_dependency_blocked"] = True


def _build_python_toolchain_capability_summary(
    *,
    python_validation_required: bool,
    python_validation_enabled: bool,
    python_validation_reason_code: str | None,
    python_validation_reason_type: str | None,
    python_validation_reason: str | None,
    python_context_probe: dict[str, Any] | None,
    validated_python_executable: str | None,
    pdm_required: bool,
) -> dict[str, Any]:
    """
    Build the canonical machine-readable Python toolchain capability summary.

    This is the single authoritative record of whether the Python toolchain
    (interpreter + pdm when applicable) is healthy for the current run.  It is
    stored in preflight artifacts so that consumers never need to reconstruct
    the decision from scattered command-level diagnostics.
    """
    if not python_validation_required:
        toolchain_status = "not_required"
    elif python_validation_enabled:
        toolchain_status = "healthy"
    else:
        toolchain_status = "blocked"

    context_probe_passed: bool | None = None
    if python_validation_required and isinstance(python_context_probe, dict):
        context_probe_passed = bool(python_context_probe.get("passed", False))

    return {
        "toolchain_status": toolchain_status,
        "python_required": python_validation_required,
        "pdm_required": pdm_required,
        "interpreter_usable": python_validation_enabled or not python_validation_required,
        "context_probe_passed": context_probe_passed,
        "reason_code": python_validation_reason_code,
        "reason_type": python_validation_reason_type,
        "reason": python_validation_reason,
        "validated_executable": validated_python_executable,
    }


def _validate_python_capability(
    *,
    workspace_dir: Path,
    verification_commands: tuple[str, ...],
    command_prefix: list[str],
    cwd: Path,
    env_overrides: dict[str, str] | None,
    select_python_runtime_func: SelectPythonRuntimeFunc = select_python_runtime,
    probe_python_context_capability_func: ProbePythonContextCapabilityFunc | None = None,
) -> dict[str, Any]:
    """
    Single Python capability validator used for both preflight gating and execution rewrites.

    It performs ordered candidate evaluation via `select_python_runtime` and, when verification
    needs Python, validates effective execution-path usability with a context probe.
    """

    prefer_context_selection = bool(command_prefix)
    runtime_env_overrides = _sanitize_runtime_env_overrides(
        env_overrides=env_overrides,
        command_prefix=command_prefix,
    )

    python_runtime = select_python_runtime_func(
        workspace_dir=workspace_dir,
        include_where_fallbacks=not prefer_context_selection,
        include_sys_executable=not prefer_context_selection,
        environment=runtime_env_overrides or None,
    )
    python_runtime_summary = python_runtime.to_dict()
    python_validation_required = verification_commands_need_python(verification_commands)

    python_context_probe: dict[str, Any] | None = None
    python_admissibility_probe_required = (
        prefer_context_selection or python_runtime.selected is not None
    )
    if python_admissibility_probe_required:
        probe_func = probe_python_context_capability_func or _probe_python_context_capability
        python_context_probe = probe_func(
            command_prefix=command_prefix,
            cwd=cwd,
            env_overrides=runtime_env_overrides or None,
            python_executable=(
                python_runtime.selected.path
                if (python_runtime.selected is not None and not prefer_context_selection)
                else None
            ),
        )
    python_runtime_summary = _reconcile_python_runtime_summary_with_context(
        python_runtime_summary=python_runtime_summary,
        python_context_probe=python_context_probe,
        prefer_context_selection=prefer_context_selection,
    )
    verified_context_candidate = _context_verified_runtime_candidate(python_context_probe)

    python_validation_enabled = True
    python_validation_reason_code: str | None = None
    python_validation_reason_type: str | None = None
    python_validation_reason: str | None = None
    if isinstance(python_context_probe, dict):
        python_validation_enabled = verified_context_candidate is not None
        if not python_validation_enabled:
            probe_reason_code = python_context_probe.get("reason_code")
            python_validation_reason_code = (
                probe_reason_code if isinstance(probe_reason_code, str) else "runtime_probe_failed"
            )
            python_validation_reason_type = _reason_type_for_code(python_validation_reason_code)
            probe_reason = python_context_probe.get("reason")
            python_validation_reason = probe_reason if isinstance(probe_reason, str) else None
    elif python_validation_required:
        python_validation_enabled = False
        if python_runtime_summary.get("selected") is None:
            primary_rejection = _primary_runtime_rejection(python_runtime_summary)
            python_validation_reason_code = (
                primary_rejection.get("reason_code")
                if isinstance(primary_rejection.get("reason_code"), str)
                else "python_unavailable"
            )
            python_validation_reason_type = (
                primary_rejection.get("reason_type")
                if isinstance(primary_rejection.get("reason_type"), str)
                else _reason_type_for_code(python_validation_reason_code)
            )
            python_validation_reason = (
                primary_rejection.get("reason")
                if isinstance(primary_rejection.get("reason"), str)
                else None
            )
        else:
            python_validation_reason_code = "runtime_probe_failed"
            python_validation_reason_type = _reason_type_for_code(python_validation_reason_code)
            python_validation_reason = (
                "Python validation was required, but context probe metadata was missing."
            )

    validated_python_executable = (
        verified_context_candidate.get("path")
        if isinstance(verified_context_candidate, dict)
        and isinstance(verified_context_candidate.get("path"), str)
        and verified_context_candidate.get("path")
        else None
    )

    return {
        "runtime_selection": python_runtime,
        "runtime_summary": python_runtime_summary,
        "context_probe": python_context_probe,
        "validation": {
            "required": python_validation_required,
            "enabled": python_validation_enabled,
            "reason_code": python_validation_reason_code,
            "reason_type": python_validation_reason_type,
            "reason": python_validation_reason,
            "validated_python_executable": validated_python_executable,
        },
    }


def _probe_python_context_capability(
    *,
    command_prefix: list[str],
    cwd: Path,
    env_overrides: dict[str, str] | None,
    python_executable: str | None,
    rewrite_verification_command_for_python: RewriteVerificationCommandForPythonFunc = (
        _default_rewrite_verification_command_for_python
    ),
    verification_shell_argv: VerificationShellArgvFunc = _default_verification_shell_argv,
    timeout_seconds: float = _PYTHON_CONTEXT_PROBE_BUDGET_SECONDS,
) -> dict[str, Any]:
    is_powershell = (not command_prefix) and _is_windows()
    probe_command = f"python -c {shlex.quote(_PYTHON_CONTEXT_HEALTH_PROBE)}"
    effective_command = probe_command
    direct_argv: list[str] | None = None
    if not command_prefix and isinstance(python_executable, str) and python_executable.strip():
        selected_python = python_executable.strip()
        direct_argv = [selected_python, "-c", _PYTHON_CONTEXT_HEALTH_PROBE]
        effective_command = f"{shlex.quote(selected_python)} -c <health_probe>"
    elif not command_prefix:
        effective_command, _ = rewrite_verification_command_for_python(
            probe_command,
            python_executable=python_executable,
            is_powershell=is_powershell,
        )

    merged_env: dict[str, str] | None = None
    effective_prefix = list(command_prefix)
    if env_overrides:
        safe_overrides = {
            key: value
            for key, value in env_overrides.items()
            if isinstance(key, str) and isinstance(value, str)
        }
        if safe_overrides:
            if effective_prefix and looks_like_docker_exec_prefix(effective_prefix):
                effective_prefix = inject_docker_exec_env(effective_prefix, safe_overrides)
            elif not effective_prefix:
                merged_env = dict(os.environ)
                merged_env.update(safe_overrides)

    argv = (
        direct_argv
        if direct_argv is not None
        else verification_shell_argv(command_prefix=effective_prefix, command=effective_command)
    )

    stdout_text = ""
    stderr_text = ""
    exit_code = 0
    timed_out = False
    exception: str | None = None
    payload: dict[str, Any] | None = None

    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(cwd),
            timeout=max(0.1, float(timeout_seconds)),
            check=False,
            env=merged_env,
        )
        exit_code = int(proc.returncode or 0)
        stdout_text = proc.stdout or ""
        stderr_text = proc.stderr or ""
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        exit_code = 124
        if isinstance(exc.stdout, bytes):
            stdout_text = exc.stdout.decode("utf-8", "replace")
        else:
            stdout_text = exc.stdout or ""
        if isinstance(exc.stderr, bytes):
            stderr_text = exc.stderr.decode("utf-8", "replace")
        else:
            stderr_text = exc.stderr or ""
    except OSError as exc:
        exit_code = 1
        exception = str(exc)

    if exit_code == 0 and not timed_out and exception is None:
        for line in reversed(stdout_text.splitlines()):
            candidate = line.strip()
            if not candidate:
                continue
            try:
                decoded = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            if isinstance(decoded, dict):
                payload = decoded
                break

    merged = "\n".join(value for value in (stderr_text, stdout_text, exception) if value).strip()
    lowered = merged.lower()

    reason_code: str | None = None
    reason: str | None = None
    if timed_out:
        reason_code = "timeout"
        reason = "Python context probe timed out."
    elif exception is not None:
        reason_code = "launch_failed"
        reason = exception
    elif exit_code != 0:
        if "encodings" in lowered and (
            "modulenotfounderror" in lowered or "no module named" in lowered
        ):
            reason_code = "missing_stdlib"
        elif "access is denied" in lowered or "permission denied" in lowered:
            reason_code = "access_denied"
        elif "cannot be accessed by the system" in lowered:
            reason_code = "access_denied"
        elif "windowsapps" in lowered:
            reason_code = "windowsapps_alias"
        elif "not found" in lowered or "not recognized" in lowered:
            reason_code = "not_found"
        else:
            reason_code = "runtime_probe_failed"
        reason = merged or f"Probe command exited with code {exit_code}."
    elif payload is None:
        reason_code = "runtime_probe_failed"
        reason = "Probe command succeeded but did not emit parseable JSON metadata."

    passed = bool(exit_code == 0 and not timed_out and exception is None and payload is not None)
    reason_type = _reason_type_for_code(reason_code)
    remediation = _python_probe_remediation(reason_code)

    return {
        "command": "python -c <health_probe>",
        "effective_command": effective_command,
        "argv": argv,
        "cwd": str(cwd),
        "passed": passed,
        "exit_code": exit_code,
        "timed_out": timed_out,
        "reason_code": reason_code,
        "reason_type": reason_type,
        "reason": reason,
        "remediation": remediation,
        "stdout_tail": _tail_text_for_prompt(stdout_text),
        "stderr_tail": _tail_text_for_prompt(stderr_text),
        "exception": exception,
        "metadata": payload,
    }



__all__ = (
    "_align_python_command_diagnostics",
    "_build_python_toolchain_capability_summary",
    "_probe_python_context_capability",
    "_probe_same_shell_python_command",
    "_probe_same_shell_wrapper_command",
    "_python_probe_remediation",
    "_reason_type_for_code",
    "_resolve_command_in_execution_context",
    "_tool_command_probe_remediation",
    "_validate_python_capability",
)
