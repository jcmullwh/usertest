from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_DOCKER_VERSION_PROBE_BUDGET_SECONDS = 3.0

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


def _is_windows() -> bool:
    return os.name == "nt"


@dataclass(frozen=True)
class ShellCapability:
    """
    Canonical runner-side shell capability decision for an effective agent run.

    The legacy policy inference reports agent-specific allowlist terms such as
    ``allowed``/``blocked``/``unknown``.  Shell-required missions need a stricter shared contract:
    they may dispatch only when this canonical state is ``available``.  ``blocked`` and
    ``unprobed`` are terminal preflight states for shell-required missions and carry structured
    reason details for artifacts.
    """

    state: str
    agent: str
    operating_system: str
    backend: str
    sandbox_mode: str | None
    probe_status: str
    reason_code: str | None
    reason_type: str | None
    reason: str
    policy_status: str
    policy_reason: str
    allowed_tools: list[str] | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "agent": self.agent,
            "operating_system": self.operating_system,
            "backend": self.backend,
            "sandbox_mode": self.sandbox_mode,
            "probe_status": self.probe_status,
            "reason_code": self.reason_code,
            "reason_type": self.reason_type,
            "reason": self.reason,
            "policy_status": self.policy_status,
            "policy_reason": self.policy_reason,
            "allowed_tools": self.allowed_tools,
        }

def _effective_gemini_cli_sandbox(*, policy_value: Any, has_outer_sandbox: bool) -> bool:
    enabled = bool(policy_value) if isinstance(policy_value, bool) else True
    if not enabled:
        return False
    if has_outer_sandbox:
        # Gemini CLI's `--sandbox` uses docker/podman; when the runner itself is already
        # executing inside a Docker sandbox, rely on the outer sandbox and disable Gemini's
        # nested sandbox.
        return False
    try:
        if Path("/.dockerenv").exists():
            # Some environments run the runner inside a container even when the runner's
            # execution backend is "local". Avoid asking Gemini CLI to create a nested container.
            return False
    except OSError:
        pass
    if os.name == "nt":
        # Gemini CLI's `--sandbox` relies on docker/podman and can hang on Windows hosts in
        # headless/non-interactive runs. For runner use-cases, prefer the runner's own Docker
        # sandbox backend instead.
        return False
    return True


def _gemini_shell_unavailable_reason(*, policy_value: Any, has_outer_sandbox: bool) -> str:
    """
    Render a user-facing reason when Gemini `run_shell_command` is enabled but shell execution
    cannot be provided by either an outer sandbox (runner docker backend) or Gemini's own sandbox.
    """

    if has_outer_sandbox:
        return "Gemini shell commands are unavailable: outer sandbox is expected but missing."

    enabled = bool(policy_value) if isinstance(policy_value, bool) else True
    if not enabled:
        if _is_windows():
            return (
                "run_shell_command requested, but Gemini shell is unavailable under "
                "`--exec-backend local` on Windows (Gemini sandbox is disabled). "
                "Use `--exec-backend docker`."
            )
        return (
            "run_shell_command requested, but Gemini sandbox is disabled (gemini.sandbox=false). "
            "Use `--exec-backend docker` (recommended) or enable gemini.sandbox."
        )

    try:
        if Path("/.dockerenv").exists():
            return (
                "run_shell_command requested, but Gemini sandbox is unavailable because the "
                "runner is already inside a container (nested sandbox is disabled). "
                "Use `--exec-backend docker`."
            )
    except OSError:
        pass

    if _is_windows():
        return (
            "run_shell_command requested, but Gemini sandbox is unavailable on Windows for "
            "headless runs. Use `--exec-backend docker`."
        )

    return (
        "run_shell_command requested, but Gemini sandbox is disabled/unavailable. "
        "Use `--exec-backend docker` (recommended) or enable gemini.sandbox."
    )


def _docker_exec_backend_available() -> bool:
    docker = shutil.which("docker")
    if docker is None:
        return False
    try:
        proc = subprocess.run(
            [docker, "version"],
            capture_output=True,
            text=True,
            timeout=_DOCKER_VERSION_PROBE_BUDGET_SECONDS,
            check=False,
        )
    except Exception:  # noqa: BLE001
        return False
    return proc.returncode == 0

def _infer_shell_policy_status(
    *,
    agent: str,
    codex_policy: dict[str, Any] | None = None,
    claude_policy: dict[str, Any],
    gemini_policy: dict[str, Any],
    has_outer_sandbox: bool,
) -> tuple[str, str, list[str] | None]:
    """
    Infer whether shell commands should be treated as allowed/blocked for the selected agent.

    Returns `(status, reason, allowed_tools)` where status is one of: allowed, blocked, unknown.
    """

    if agent == "claude":
        raw_allowed = claude_policy.get("allowed_tools")
        allowed_tools = (
            [x for x in raw_allowed if isinstance(x, str) and x.strip()]
            if isinstance(raw_allowed, list)
            else []
        )
        shell_enabled = "Bash" in allowed_tools
        return (
            ("allowed" if shell_enabled else "blocked"),
            ("claude.allowed_tools includes Bash" if shell_enabled else "Bash not enabled"),
            allowed_tools,
        )

    if agent == "gemini":
        raw_allowed = gemini_policy.get("allowed_tools")
        allowed_tools = (
            [x for x in raw_allowed if isinstance(x, str) and x.strip()]
            if isinstance(raw_allowed, list)
            else []
        )
        shell_enabled = "run_shell_command" in allowed_tools
        effective_gemini_sandbox = _effective_gemini_cli_sandbox(
            policy_value=gemini_policy.get("sandbox", True),
            has_outer_sandbox=has_outer_sandbox,
        )
        shell_available = has_outer_sandbox or effective_gemini_sandbox
        if shell_enabled and not shell_available:
            return (
                "blocked",
                (
                    _gemini_shell_unavailable_reason(
                        policy_value=gemini_policy.get("sandbox", True),
                        has_outer_sandbox=has_outer_sandbox,
                    )
                ),
                allowed_tools,
            )
        return (
            ("allowed" if shell_enabled else "blocked"),
            (
                "gemini.allowed_tools includes run_shell_command"
                if shell_enabled
                else "run_shell_command not enabled"
            ),
            allowed_tools,
        )

    if agent == "codex":
        codex_policy = codex_policy if isinstance(codex_policy, dict) else {}
        sandbox_raw = codex_policy.get("sandbox")
        sandbox = (
            str(sandbox_raw).strip()
            if isinstance(sandbox_raw, str) and str(sandbox_raw).strip()
            else ""
        )
        if sandbox in {"read-only", "workspace-write", "danger-full-access"}:
            return (
                "allowed",
                f"Codex sandbox policy is explicitly configured as {sandbox}.",
                None,
            )
        return (
            "unknown",
            (
                "Codex CLI command execution depends on Codex sandbox policy/approvals. "
                "This runner can't reliably precompute allowlist outcome."
            ),
            None,
        )

    return (
        "unknown",
        f"Unknown agent={agent!r}; cannot infer shell allowlist status.",
        None,
    )


def _codex_shell_probe_failure_reason(
    *,
    operating_system: str,
    probe_result: dict[str, Any] | None,
) -> tuple[str, str] | None:
    if not isinstance(probe_result, dict):
        return None

    passed_raw = probe_result.get("passed")
    ok_raw = probe_result.get("ok")
    exit_code = probe_result.get("exit_code")
    probe_failed = False
    if isinstance(passed_raw, bool):
        probe_failed = not passed_raw
    elif isinstance(ok_raw, bool):
        probe_failed = not ok_raw
    elif isinstance(exit_code, int):
        probe_failed = exit_code != 0
    if not probe_failed:
        return None

    text_parts: list[str] = []
    for key in (
        "stderr",
        "stderr_tail",
        "stderr_excerpt",
        "stdout",
        "stdout_tail",
        "stdout_excerpt",
        "details",
        "error",
        "reason",
    ):
        value = probe_result.get(key)
        if isinstance(value, str) and value.strip():
            text_parts.append(value.strip())
    text = "\n".join(text_parts)
    lowered = text.lower()

    os_is_windows = operating_system.strip().lower().startswith("windows")
    if os_is_windows and "windows-sandbox-rs" in lowered:
        return (
            "codex_windows_sandbox_panic",
            "Codex Windows sandbox probe failed before shell payload execution.",
        )
    if os_is_windows and (
        "blocked by policy" in lowered
        or "denied by policy" in lowered
        or "policy-blocked" in lowered
        or "policy blocked" in lowered
        or (
            "policy" in lowered
            and (
                "process launch" in lowered
                or "process creation" in lowered
                or "failed to launch" in lowered
                or "failed to spawn" in lowered
            )
        )
    ):
        return (
            "codex_windows_process_launch_blocked_by_policy",
            "Codex Windows shell probe process launch was blocked by policy.",
        )
    if (
        os_is_windows
        and "powershell" in lowered
        and (
            "pre-payload" in lowered
            or "before payload" in lowered
            or "before shell payload" in lowered
        )
    ):
        return (
            "codex_windows_powershell_prepayload_failed",
            "Codex PowerShell probe failed before shell payload execution.",
        )
    if os_is_windows and (
        "access is denied" in lowered
        or "failed to launch" in lowered
        or "failed to spawn" in lowered
        or "could not launch" in lowered
    ):
        return (
            "codex_windows_shell_launch_failed",
            "Codex Windows shell probe could not launch the shell payload.",
        )
    return (
        "shell_probe_failed",
        "Shell probe failed before the runner could mark shell capability available.",
    )


def _resolve_codex_sandbox_mode(
    *,
    request: Any,
    codex_policy: dict[str, Any],
    has_sandbox_backend: bool,
) -> str:
    sandbox_policy_raw = codex_policy.get("sandbox", "read-only")
    sandbox_policy = (
        str(sandbox_policy_raw)
        if isinstance(sandbox_policy_raw, str) and sandbox_policy_raw.strip()
        else "read-only"
    )
    if has_sandbox_backend and request.policy == "write":
        return "danger-full-access"
    return sandbox_policy


def _resolve_shell_capability(
    *,
    agent: str,
    operating_system: str,
    backend: str,
    sandbox_mode: str | None,
    policy_status: str,
    policy_reason: str,
    allowed_tools: list[str] | None,
    probe_result: dict[str, Any] | None = None,
) -> ShellCapability:
    """
    Resolve the canonical shell capability for the effective agent execution path.

    ``available`` is the only state that may dispatch shell-required missions.  ``blocked`` means
    the runner has a concrete policy/backend/probe reason.  ``unprobed`` means the runner cannot
    prove shell availability from the effective agent/OS/backend/sandbox tuple and must not treat
    unknown capability as available.
    """

    agent_norm = agent.strip().lower()
    backend_norm = backend.strip().lower() if backend.strip() else "local"
    policy_status_norm = policy_status.strip().lower() if policy_status.strip() else "unknown"
    probe_status = "not_run"
    probe_kind: str | None = None

    if isinstance(probe_result, dict):
        kind_raw = probe_result.get("kind")
        probe_kind = kind_raw.strip() if isinstance(kind_raw, str) and kind_raw.strip() else None
        passed_raw = probe_result.get("passed")
        ok_raw = probe_result.get("ok")
        exit_code = probe_result.get("exit_code")
        if passed_raw is False or ok_raw is False:
            probe_status = "failed"
        elif passed_raw is True or ok_raw is True:
            probe_status = "passed"
        elif isinstance(exit_code, int):
            probe_status = "passed" if exit_code == 0 else "failed"
        else:
            probe_status = "unknown"

    if probe_status == "failed":
        if agent_norm == "codex":
            reason_code, reason = _codex_shell_probe_failure_reason(
                operating_system=operating_system,
                probe_result=probe_result,
            ) or (
                "shell_probe_failed",
                "Codex shell probe failed before shell capability could be marked available.",
            )
        else:
            reason_code = "shell_probe_failed"
            reason = "Shell probe failed before shell capability could be marked available."
        return ShellCapability(
            state="blocked",
            agent=agent,
            operating_system=operating_system,
            backend=backend_norm,
            sandbox_mode=sandbox_mode,
            probe_status=probe_status,
            reason_code=reason_code,
            reason_type=_reason_type_for_code(reason_code),
            reason=reason,
            policy_status=policy_status_norm,
            policy_reason=policy_reason,
            allowed_tools=allowed_tools,
        )

    if policy_status_norm == "blocked":
        return ShellCapability(
            state="blocked",
            agent=agent,
            operating_system=operating_system,
            backend=backend_norm,
            sandbox_mode=sandbox_mode,
            probe_status=probe_status,
            reason_code="shell_policy_blocked",
            reason_type="configuration",
            reason=policy_reason or "Shell commands are blocked by agent policy.",
            policy_status=policy_status_norm,
            policy_reason=policy_reason,
            allowed_tools=allowed_tools,
        )

    probe_proves_agent_shell_launch = True
    if (
        probe_status == "passed"
        and probe_kind == "backend_shell_payload"
        and agent_norm == "codex"
        and backend_norm == "local"
        and operating_system.strip().lower().startswith("windows")
    ):
        # A plain PowerShell/bash no-op can succeed on Windows while Codex's own sandboxed shell
        # backend still fails before the payload command is invoked.  Do not upgrade local Windows
        # Codex shell capability from a generic backend probe alone.
        probe_proves_agent_shell_launch = False

    if probe_status == "passed" and probe_proves_agent_shell_launch:
        return ShellCapability(
            state="available",
            agent=agent,
            operating_system=operating_system,
            backend=backend_norm,
            sandbox_mode=sandbox_mode,
            probe_status=probe_status,
            reason_code=None,
            reason_type=None,
            reason=("Shell probe passed for the effective agent execution path."),
            policy_status=policy_status_norm,
            policy_reason=policy_reason,
            allowed_tools=allowed_tools,
        )

    reason_code = "shell_capability_unprobed"
    reason = (
        "Shell capability is not proven for the effective agent execution path; "
        "unknown shell capability is not treated as available for shell-required missions."
    )
    if policy_status_norm == "allowed":
        reason_code = "shell_command_discovered_without_launchability"
        reason = (
            "Shell policy/command discovery allows shell use, but no payload-equivalent "
            "launch probe proved shell backend launchability."
        )
    if agent_norm == "codex" and operating_system.strip().lower().startswith("windows"):
        reason_code = "codex_windows_shell_unprobed"
        if probe_status == "passed" and not probe_proves_agent_shell_launch:
            reason = (
                "A generic local shell payload probe passed, but local Windows Codex shell "
                "backend launchability was not proven under the Codex sandbox policy."
            )
    return ShellCapability(
        state="unprobed",
        agent=agent,
        operating_system=operating_system,
        backend=backend_norm,
        sandbox_mode=sandbox_mode,
        probe_status=probe_status,
        reason_code=reason_code,
        reason_type="runtime",
        reason=reason,
        policy_status=policy_status_norm,
        policy_reason=policy_reason,
        allowed_tools=allowed_tools,
    )


def _shell_probe_result_from_preflight_meta(
    preflight_meta: dict[str, Any],
) -> dict[str, Any] | None:
    if not isinstance(preflight_meta, dict):
        return None

    agent_shell_probe = preflight_meta.get("agent_shell_probe")
    if isinstance(agent_shell_probe, dict):
        exit_code_raw = agent_shell_probe.get("exit_code")
        exit_code = exit_code_raw if isinstance(exit_code_raw, int) else 1
        ok = agent_shell_probe.get("ok")
        return {
            "kind": "agent_shell_payload",
            "ok": bool(ok) if isinstance(ok, bool) else exit_code == 0,
            "exit_code": exit_code,
            "stderr_excerpt": (
                agent_shell_probe.get("stderr_excerpt")
                if isinstance(agent_shell_probe.get("stderr_excerpt"), str)
                else ""
            ),
            "stdout_excerpt": (
                agent_shell_probe.get("stdout_excerpt")
                if isinstance(agent_shell_probe.get("stdout_excerpt"), str)
                else ""
            ),
            "details": (
                agent_shell_probe.get("last_message_excerpt")
                if isinstance(agent_shell_probe.get("last_message_excerpt"), str)
                else ""
            ),
            "reason": (
                agent_shell_probe.get("reason")
                if isinstance(agent_shell_probe.get("reason"), str)
                else ""
            ),
        }

    error = preflight_meta.get("error")
    if isinstance(error, str) and error.strip():
        return {
            "ok": False,
            "exit_code": 1,
            "error": error.strip(),
            "reason": error.strip(),
        }

    shell_probe = preflight_meta.get("shell_probe")
    if isinstance(shell_probe, dict):
        exit_code_raw = shell_probe.get("exit_code")
        exit_code = exit_code_raw if isinstance(exit_code_raw, int) else 1
        stderr = shell_probe.get("stderr")
        stdout = shell_probe.get("stdout")
        return {
            "kind": shell_probe.get("kind")
            if isinstance(shell_probe.get("kind"), str)
            else "backend_shell_payload",
            "ok": exit_code == 0,
            "exit_code": exit_code,
            "stderr_excerpt": stderr if isinstance(stderr, str) else "",
            "stdout_excerpt": stdout if isinstance(stdout, str) else "",
        }

    return None

__all__ = (
    "ShellCapability",
    "_codex_shell_probe_failure_reason",
    "_docker_exec_backend_available",
    "_effective_gemini_cli_sandbox",
    "_gemini_shell_unavailable_reason",
    "_infer_shell_policy_status",
    "_resolve_shell_capability",
    "_shell_probe_result_from_preflight_meta",
)
