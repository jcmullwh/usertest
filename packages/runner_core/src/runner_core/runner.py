from __future__ import annotations

import json
import os
import platform
import random
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
import traceback
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agent_adapters import (
    build_codex_subscription_config_overrides,
    normalize_claude_events,
    normalize_codex_events,
    normalize_gemini_events,
    probe_agent_shell_launch,
    probe_codex_login_status,
    resolve_codex_executable,
    run_claude_print,
    run_codex_exec,
    run_gemini,
    validate_codex_personality_config_overrides,
    validate_codex_reasoning_effort_config_overrides,
    validate_codex_subscription_config_overrides,
)
from agent_adapters.codex_config import toml_basic_string
from agent_adapters.docker_exec_env import inject_docker_exec_env, looks_like_docker_exec_prefix
from normalized_events import iter_events_jsonl, make_event
from reporter import (
    compute_metrics,
    render_report_markdown,
    validate_report,
)
from sandbox_runner.diagnostics import (
    capture_container_artifacts,
    capture_dns_snapshot,
    probe_commands_in_container,
)

from runner_core.agent_docs import obfuscate_target_agent_docs
from runner_core.agent_prompt_files import _materialize_agent_prompt_into_workspace
from runner_core.artifacts import (
    _extract_json_object_with_receipt,
    _tail_text_for_prompt,
    _write_json,
)
from runner_core.artifacts import (
    _read_tail_text as _read_tail_text,
)
from runner_core.catalog import load_catalog_config
from runner_core.codex_execpolicy import (
    CONTROLLED_CODEX_AUTH_ENV_VARS,
    CONTROLLED_CODEX_NON_ROUTING_CONFIG_OVERRIDES,
    CONTROLLED_CODEX_WINDOWS_SANDBOX_CONFIG_OVERRIDE,
    ControlledCodexExecpolicyOverlay,
    build_codex_shell_probe_config_overrides,
    capture_probe_workspace_state,
    controlled_codex_execpolicy_receipt_errors,
    install_controlled_codex_execpolicy,
)
from runner_core.execution_backend import prepare_execution_backend
from runner_core.git_helpers import (
    _ensure_git_user_config as _ensure_git_user_config,
)
from runner_core.git_helpers import (
    _git_diff,
    _git_numstat,
    _maybe_commit_preprocess_workspace,
)
from runner_core.git_helpers import (
    _git_status_porcelain as _git_status_porcelain,
)
from runner_core.pathing import (
    LOCAL_BACKEND_RUN_DIR_ALIAS,
    agent_path_join,
    normalize_agent_path,
    slugify,
    utc_timestamp_compact,
)
from runner_core.pip_bootstrap import (
    PipBootstrapResult,
    bootstrap_pip_requirements,
)
from runner_core.pip_target import (
    is_pip_repo_input,
    parse_pip_repo_input,
)
from runner_core.pip_target import (
    requirements_path as pip_requirements_path,
)
from runner_core.preflight import (
    _agent_binary_for_preflight_probe,
    _build_preflight_command_list,
    _ensure_windows_python_on_path,
    _probe_commands_local,
    _run_bounded_command_probe,
)
from runner_core.prompt import (
    CANONICAL_EXECUTION_NOTES_MD,
    TemplateSubstitutionError,
    build_prompt_from_template,
)
from runner_core.prompt_staging import (
    _agent_path_for_staged_file,
    _resolve_agent_prompt_input_path,
    _run_dir_agent_visible_root,
    _stage_agent_prompt_file,
    _stage_agent_prompt_text,
)
from runner_core.provenance import capture_runner_implementation_provenance
from runner_core.python_capability import (
    _PYTHON_COMMAND_PROBE_BUDGET_SECONDS,
    _PYTHON_CONTEXT_PROBE_BUDGET_SECONDS,
    _WRAPPER_COMMAND_PROBE_BUDGET_SECONDS,
    _align_python_command_diagnostics,
    _build_python_toolchain_capability_summary,
    _reason_type_for_code,
)
from runner_core.python_capability import (
    _probe_python_context_capability as _probe_python_context_capability_impl,
)
from runner_core.python_capability import (
    _probe_same_shell_python_command as _probe_same_shell_python_command_impl,
)
from runner_core.python_capability import (
    _probe_same_shell_wrapper_command as _probe_same_shell_wrapper_command_impl,
)
from runner_core.python_capability import (
    _validate_python_capability as _validate_python_capability_impl,
)
from runner_core.python_runtime import (
    probe_pip_module,
    probe_pytest_module,
    select_python_runtime,
    verification_commands_may_provision_pytest,
    verification_commands_need_pdm,
    verification_commands_need_pytest,
    verification_commands_need_python,
)
from runner_core.retained_oracle_assets import (
    RETAINED_ORACLE_AGENT_NOTE,
    retained_oracle_asset_summary,
    stage_retained_oracle_asset,
    validate_retained_oracle_asset_source,
    validate_staged_retained_oracle_asset,
)
from runner_core.run_spec import resolve_effective_run_inputs
from runner_core.shell_capability import (
    _docker_exec_backend_available,
    _effective_gemini_cli_sandbox,
    _gemini_shell_unavailable_reason,
    _resolve_codex_sandbox_mode,
    _resolve_shell_capability,
    _shell_probe_result_from_preflight_meta,
)
from runner_core.shell_capability import (
    _infer_shell_policy_status as _infer_shell_policy_status_impl,
)
from runner_core.shell_command_normalization import (
    normalize_command_for_shell,
    render_shell_command_guidance_md,
)
from runner_core.stderr_diagnostics import (
    _CODEX_EMPTY_OVERRIDE_VALUES,
    _CODEX_PERSONALITY_MISSING_MESSAGES_WARNING,
    _MAX_AGENT_RETRY_DELAY_SECONDS,
    _classify_failure_subtype,
    _codex_metadata_capture_from_stderr,
    _extract_claude_quota_exhaustion,
    _extract_codex_subscription_usage_limit,
    _extract_raw_events_error_messages,
    _extract_raw_events_plaintext_excerpt,
    _format_claude_quota_exhaustion_stderr,
    _format_codex_subscription_usage_limit_stderr,
    _is_retryable_provider_capacity_failure,
    _is_retryable_tool_use_id_collision_failure,
    _is_retryable_transient_network_failure,
    _merge_codex_metadata_capture_summary,
    _new_codex_metadata_capture_summary,
    _sanitize_agent_stderr_file,
)
from runner_core.stderr_diagnostics import (
    _sanitize_agent_stderr_text as _sanitize_agent_stderr_text,
)
from runner_core.target_acquire import (
    acquire_existing_target,
    acquire_target,
    remove_acquired_workspace,
)
from runner_core.verification_broker import (
    VerificationBrokerAttempt,
    VerificationBrokerContract,
    VerificationBrokerRequestResult,
    default_verification_hang_guard_seconds,
    probe_local_verification_launcher,
    probe_local_verification_python,
    render_verification_broker_command,
    resolve_verification_broker_contract,
    resolve_verification_launcher,
    validate_verification_broker_response_payload,
    verification_broker_missing_result_artifacts,
    verification_broker_runtime_prerequisites,
)
from runner_core.verification_broker import (
    probe_windows_bash_usable as _probe_windows_bash_usable_impl,
)
from runner_core.verification_commands import verification_command_safety_errors
from runner_core.verification_prompts import (
    _build_followup_prompt,
    _build_verification_followup_prompt,
)
from runner_core.verification_timing_profile import build_verification_timing_profile
from runner_core.workspace_state_hash import WorkspaceStateHash, compute_workspace_state_hash


def _is_windows() -> bool:
    return os.name == "nt"


def _report_has_live_workspace_output(report: object, workspace_dir: Path) -> bool:
    """Return whether a report names a live regular file in the acquired workspace."""
    if not isinstance(report, dict):
        return False
    outputs = report.get("outputs")
    if not isinstance(outputs, list):
        return False

    try:
        resolved_workspace = workspace_dir.resolve(strict=True)
        if not resolved_workspace.is_dir():
            return False
    except (OSError, RuntimeError):
        return False

    for output in outputs:
        if not isinstance(output, dict):
            continue
        path_value = output.get("path")
        if not isinstance(path_value, str) or not path_value.strip():
            continue

        candidate = Path(path_value)
        if not candidate.is_absolute():
            continue
        try:
            if candidate.is_symlink():
                continue
            resolved_candidate = candidate.resolve(strict=True)
            if not resolved_candidate.is_file():
                continue
            resolved_candidate.relative_to(resolved_workspace)
        except (OSError, RuntimeError, ValueError):
            continue
        return True

    return False


def _codex_subscription_external_wait(text: str) -> dict[str, Any] | None:
    """Project a provider usage-limit message into a resumable, non-API wait state."""
    usage_limit = _extract_codex_subscription_usage_limit(text)
    if usage_limit is None:
        return None
    return {
        "schema_version": 1,
        "state": "parked",
        "reason": "codex_chatgpt_subscription_usage_limit",
        "retryable": True,
        "retry_disposition": "resume_after_provider_reset",
        "retry_mode": "resume_same_session",
        "resume_after": {
            "raw": usage_limit.get("resume_after_raw"),
            "timezone": usage_limit.get("resume_after_timezone"),
        },
        "provider": "codex",
        "route": "chatgpt_subscription",
        "api_fallback_allowed": False,
        "settings_url": usage_limit.get("settings_url"),
    }


@dataclass(frozen=True)
class RunnerConfig:
    repo_root: Path
    runs_dir: Path
    agents: dict[str, Any]
    policies: dict[str, Any]


@dataclass(frozen=True)
class RunRequest:
    repo: str
    ref: str | None = None
    agent: str = "codex"
    policy: str = "write"
    persona_id: str | None = None
    mission_id: str | None = None
    obfuscate_agent_docs: bool = False
    seed: int = 0
    model: str | None = None
    agent_config_overrides: tuple[str, ...] = ()
    codex_execpolicy_allow_prefixes: tuple[tuple[str, ...], ...] = ()
    agent_system_prompt_file: Path | None = None
    agent_append_system_prompt: str | None = None
    agent_append_system_prompt_file: Path | None = None
    supervisor_instruction: str | None = None
    # Explicit user turn for a continued agent conversation. This is distinct from
    # system-instruction composition because resumed sessions retain their original
    # instructions and accept feedback through stdin as the next user message.
    agent_user_prompt: str | None = None
    # Runner-owned backlog lineage. These fields are persisted in target_ref.json
    # and outrank legacy mission-name inference and model-authored extensions.
    evidence_role: str | None = None
    origin_stage: str | None = None
    parent_case_id: str | None = None
    case_lifecycle_id: str | None = None
    # Exact Codex thread.started.thread_id to continue. Never infer with `--last`.
    codex_resume_session_id: str | None = None
    # Retained predecessor run used to prove the cumulative token high-water mark
    # before a Codex session is resumed. Without this evidence, lifecycle
    # telemetry must withhold the continued invocation's token delta.
    codex_resume_usage_source_run_dir: Path | None = None
    keep_workspace: bool = False
    preflight_commands: tuple[str, ...] = ()
    preflight_required_commands: tuple[str, ...] = ()
    verification_commands: tuple[str, ...] = ()
    verification_timeout_seconds: float | None = None
    verification_reuse_mode: str = "off"
    retained_oracle_assets_root: Path | None = None
    retained_oracle_asset_spec: dict[str, Any] | None = None

    exec_backend: str = "local"
    exec_docker_profile: str = "standard"
    exec_docker_context: Path | None = None
    exec_dockerfile: Path | None = None
    exec_docker_python: str = "auto"
    exec_docker_timeout_seconds: float | None = None
    exec_use_target_sandbox_cli_install: bool = False
    exec_use_host_agent_login: bool = True
    exec_network: str = "open"
    exec_cache: str = "cold"
    exec_cache_dir: Path | None = None
    exec_maintenance_venv_cache: bool = False
    exec_maintenance_image_metadata_path: Path | None = None
    exec_env: tuple[str, ...] = ()
    exec_keep_container: bool = False
    exec_rebuild_image: bool = False
    agent_rate_limit_retries: int = 2
    agent_rate_limit_backoff_seconds: float = 1.0
    agent_rate_limit_backoff_multiplier: float = 2.0
    agent_followup_attempts: int = 2
    resume_workspace_dir: Path | None = None


@dataclass(frozen=True)
class RunResult:
    run_dir: Path
    exit_code: int
    report_validation_errors: list[str]
    agent_session_id: str | None = None


@dataclass(frozen=True)
class DelegationCapability:
    """
    Runner-side delegation/subagent capability discovery for the effective agent policy.

    The runner must not guess provider-specific delegation tool names.  Tool names are only treated
    as detected when supplied by a documented adapter/config contract, or by a future local probe
    that can prove them for the installed CLI.  Unknown capability is therefore explicit rather than
    silently equivalent to unavailable.
    """

    state: str
    agent: str
    cli_version: str | None
    configured_allowed_tools: list[str] | None
    delegation_tool_names: list[str]
    available_under_policy: bool | None
    policy_exposes_delegation: bool | None
    cli_supports_delegation: bool | None
    policy_status: str
    cli_support_status: str
    evidence_source: str
    confidence: str
    reason: str
    cli_version_probe: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "agent": self.agent,
            "cli_version": self.cli_version,
            "configured_allowed_tools": self.configured_allowed_tools,
            "delegation_tool_names": list(self.delegation_tool_names),
            "available_under_policy": self.available_under_policy,
            "policy_exposes_delegation": self.policy_exposes_delegation,
            "cli_supports_delegation": self.cli_supports_delegation,
            "policy_status": self.policy_status,
            "cli_support_status": self.cli_support_status,
            "evidence_source": self.evidence_source,
            "confidence": self.confidence,
            "reason": self.reason,
            "cli_version_probe": self.cli_version_probe,
        }


DELEGATION_CAPABILITY_AGENTS: tuple[str, ...] = ("codex", "claude", "gemini")


def _resolve_effective_agent_model(
    *,
    agent: str,
    agent_cfg: dict[str, Any],
    requested_model: str | None,
) -> tuple[str | None, str | None]:
    if requested_model is not None and requested_model.strip():
        return requested_model.strip(), "request"

    if "default_model" not in agent_cfg:
        return None, None

    raw_default = agent_cfg.get("default_model")
    if raw_default is None:
        return None, None
    if not isinstance(raw_default, str):
        raise ValueError(
            f"configs/agents.yaml agents.{agent}.default_model must be a string or null."
        )
    default_model = raw_default.strip()
    if not default_model:
        return None, None
    return default_model, "agent_default"


_SCAFFOLD_SCRIPT_PATTERN = re.compile(r"tools[/\\]scaffold[/\\]scaffold\.py", re.IGNORECASE)
_SCAFFOLD_RUN_PATTERN = re.compile(r"\brun\b", re.IGNORECASE)
_SCAFFOLD_INSTALL_PATTERN = re.compile(r"\binstall\b", re.IGNORECASE)
_SCAFFOLD_LINT_OR_TEST_PATTERN = re.compile(r"\b(?:lint|test)\b", re.IGNORECASE)

_READ_FILE_NOT_FOUND_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"Error executing tool read_file:\s*File not found(?::\s*|\s+)(?P<path>\S+)",
        re.IGNORECASE,
    ),
    re.compile(
        r"read_file.*file not found(?::\s*|\s+)(?P<path>\S+)",
        re.IGNORECASE,
    ),
)
_WINDOWS_POSIX_DRIVE_PATH_RE = re.compile(r"^/([a-zA-Z])/(.*)$")


def _codex_config_key_matches_suffix(*, key: str, suffix: str) -> bool:
    normalized = key.strip().lower().replace("-", "_")
    return normalized == suffix or normalized.endswith("." + suffix)


def _codex_config_value_is_present(value: str) -> bool:
    compact = value.strip().replace(" ", "")
    return compact.lower() not in _CODEX_EMPTY_OVERRIDE_VALUES


def _codex_personality_override_requested(config_overrides: Sequence[str]) -> bool:
    for raw in config_overrides:
        key_raw, sep, value_raw = raw.partition("=")
        if not sep:
            continue
        key = key_raw.strip()
        if not key:
            continue
        if not (
            _codex_config_key_matches_suffix(key=key, suffix="personality")
            or _codex_config_key_matches_suffix(key=key, suffix="model_personality")
        ):
            continue
        if _codex_config_value_is_present(value_raw):
            return True
    return False


def _codex_personality_warning_lines(*, source: str, warning_line: str | None = None) -> list[str]:
    lines = [
        (
            "Codex reported that personality was requested but model_messages is missing "
            "(Codex would fall back to base instructions)."
        ),
        f"source={source}",
        "code=codex_model_messages_missing",
    ]
    lines.append(
        "hint=If you intended to use a personality, provide model_messages alongside "
        "personality/model_personality (configs/agents.yaml or --agent-config)."
    )
    return lines


def _looks_like_windows_drive_path(path_str: str) -> bool:
    return bool(re.match(r"^[a-zA-Z]:[\\/]", path_str))


def _normalize_windowsish_path_token(raw_path: str) -> str:
    token = raw_path.strip().strip("'\"`")
    if not token:
        return token
    posixish = token.replace("\\", "/")
    match = _WINDOWS_POSIX_DRIVE_PATH_RE.match(posixish)
    if match is None:
        return token
    drive = match.group(1).upper()
    remainder = match.group(2)
    return f"{drive}:/{remainder}"


def _augment_tool_file_not_found_diagnostics(
    *,
    stderr_text: str,
    workspace_root: Path | None,
) -> str:
    if not stderr_text.strip():
        return stderr_text

    raw_paths: list[str] = []
    for line in stderr_text.splitlines():
        for pattern in _READ_FILE_NOT_FOUND_PATTERNS:
            match = pattern.search(line)
            if match is None:
                continue
            raw = str(match.group("path")).strip().rstrip(".,;")
            if raw:
                raw_paths.append(raw)
            break

    if not raw_paths:
        return stderr_text

    unique_raw_paths = list(dict.fromkeys(raw_paths))
    diagnostics: list[str] = []
    workspace_text = str(workspace_root.resolve()) if workspace_root is not None else "<unknown>"
    for raw in unique_raw_paths:
        normalized = _normalize_windowsish_path_token(raw)
        candidate = Path(normalized)
        if candidate.is_absolute():
            resolved = candidate.resolve(strict=False)
        elif _looks_like_windows_drive_path(normalized):
            resolved = Path(normalized)
        elif workspace_root is not None:
            resolved = (workspace_root / candidate).resolve(strict=False)
        else:
            resolved = candidate.resolve(strict=False)
        diagnostics.extend(
            [
                "[path_diagnostic]",
                f"raw_path={raw}",
                f"resolved_path={resolved}",
                f"workspace_root={workspace_text}",
                (
                    "hint=On Windows, both /c/... and C:\\... are accepted, but files must exist "
                    "in the active workspace/backend path."
                ),
            ]
        )

    if diagnostics:
        return stderr_text.rstrip() + "\n" + "\n".join(diagnostics)
    return stderr_text


def _requires_scaffold_install_bootstrap(commands: Sequence[str]) -> bool:
    has_scaffold_install = False
    has_scaffold_lint_or_test = False
    for raw in commands:
        if not isinstance(raw, str):
            continue
        command = raw.strip()
        if not command:
            continue
        if _SCAFFOLD_SCRIPT_PATTERN.search(command) is None:
            continue
        if _SCAFFOLD_RUN_PATTERN.search(command) is None:
            continue
        if _SCAFFOLD_INSTALL_PATTERN.search(command) is not None:
            has_scaffold_install = True
        if _SCAFFOLD_LINT_OR_TEST_PATTERN.search(command) is not None:
            has_scaffold_lint_or_test = True
    return has_scaffold_lint_or_test and not has_scaffold_install


def _normalize_verification_commands_for_execution(commands: Sequence[str]) -> tuple[str, ...]:
    normalized: list[str] = []
    for raw in commands:
        if not isinstance(raw, str):
            continue
        stripped = raw.strip()
        if stripped:
            normalized.append(stripped)

    if _requires_scaffold_install_bootstrap(normalized):
        normalized = [
            "python tools/scaffold/scaffold.py run install --all --skip-missing",
            *normalized,
        ]
    return tuple(normalized)


def _verification_commands_need_source_bootstrap(commands: Sequence[str]) -> bool:
    for raw in commands:
        if not isinstance(raw, str):
            continue
        command = raw.strip()
        if not command:
            continue
        if (
            _SCAFFOLD_SCRIPT_PATTERN.search(command) is not None
            and _SCAFFOLD_RUN_PATTERN.search(command) is not None
        ):
            return True
    return verification_commands_need_pytest(tuple(commands))


def _workspace_source_relpaths(workspace_dir: Path) -> tuple[str, ...]:
    relpaths: list[str] = []
    for parent in ("apps", "packages"):
        parent_dir = workspace_dir / parent
        if not parent_dir.is_dir():
            continue
        for project_dir in sorted(parent_dir.iterdir(), key=lambda p: p.name):
            if not project_dir.is_dir():
                continue
            src_dir = project_dir / "src"
            if src_dir.is_dir():
                relpaths.append(src_dir.relative_to(workspace_dir).as_posix())
    return tuple(relpaths)


def _merge_path_entries(*, entries: Sequence[str], existing: str, sep: str) -> str:
    out: list[str] = []
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, str):
            continue
        cleaned = entry.strip()
        if not cleaned:
            continue
        if cleaned not in seen:
            out.append(cleaned)
            seen.add(cleaned)
    if isinstance(existing, str) and existing.strip():
        for item in existing.split(sep):
            cleaned = item.strip()
            if not cleaned:
                continue
            if cleaned in seen:
                continue
            out.append(cleaned)
            seen.add(cleaned)
    return sep.join(out)


def _augment_env_with_workspace_pythonpath(
    *,
    env_overrides: dict[str, str] | None,
    workspace_dir: Path,
    workspace_mount: str | None,
) -> dict[str, str] | None:
    relpaths = _workspace_source_relpaths(workspace_dir)
    if not relpaths:
        return env_overrides

    env = dict(env_overrides or {})
    if workspace_mount:
        mount_root = normalize_agent_path(workspace_mount)
        entries = tuple(agent_path_join(mount_root, rel) for rel in relpaths)
        sep = ":"
    else:
        entries = tuple(str(workspace_dir / rel.replace("/", os.sep)) for rel in relpaths)
        sep = os.pathsep

    merged = _merge_path_entries(
        entries=entries,
        existing=env.get("PYTHONPATH", ""),
        sep=sep,
    )
    if merged:
        env["PYTHONPATH"] = merged
    return env if env else None


def _snapshot_workspace_root(workspace_dir: Path, *, max_entries: int = 200) -> dict[str, Any]:
    if max_entries <= 0:
        return {"entries": [], "total_entries": 0, "truncated": False, "error": None}
    try:
        items = sorted(workspace_dir.iterdir(), key=lambda p: p.name)
    except OSError as exc:
        return {"entries": [], "total_entries": 0, "truncated": False, "error": str(exc)}

    total_entries = len(items)
    truncated = total_entries > max_entries
    entries: list[dict[str, Any]] = []
    for p in items[:max_entries]:
        kind = "other"
        try:
            if p.is_dir():
                kind = "dir"
            elif p.is_file():
                kind = "file"
        except OSError:
            kind = "other"
        entries.append({"name": p.name, "kind": kind})

    return {
        "entries": entries,
        "total_entries": total_entries,
        "truncated": truncated,
        "error": None,
    }


def _runner_host_os() -> str:
    """
    Return a stable host OS label without relying on Windows WMI calls.

    Notes
    -----
    Python's `platform.system()` can hang on some Windows hosts due to WMI queries. The runner
    uses this value only for lightweight environment metadata; avoid the risk by treating the
    Windows case as a constant label.
    """

    if os.name == "nt":
        return "Windows"
    return platform.system()


def _execution_shell_family(*, exec_backend: str, host_os: str) -> str:
    """
    Return the intended shell "family" for commands executed via sandboxed shell tools.

    This is used only for prompt metadata / agent guidance, not for selecting an actual
    interpreter. Keep it coarse and predictable.
    """

    if exec_backend != "local":
        return "bash"
    if host_os.strip().lower().startswith("windows"):
        return "powershell"
    return "bash"


def _format_seconds_for_prompt(value: Any) -> str:
    if not isinstance(value, (int, float)):
        return "unknown"
    seconds = max(0.0, float(value))
    if seconds >= 60.0:
        minutes = seconds / 60.0
        return f"{seconds:.0f}s (~{minutes:.1f} min)"
    return f"{seconds:.0f}s"


def _format_verification_timing_guidance_md(
    *,
    verification_timing_profile: dict[str, Any] | None,
    verification_timing_profile_path: str | None,
    verification_result_path: str | None,
) -> list[str]:
    if not isinstance(verification_timing_profile, dict):
        return []
    recommendations = verification_timing_profile.get("recommendations")
    if not isinstance(recommendations, dict):
        return []

    expected = recommendations.get("expected_duration_range_seconds")
    expected_dict = expected if isinstance(expected, dict) else {}
    recommended_wait = recommendations.get("recommended_initial_wait_seconds")
    check_after = recommendations.get("reasonable_check_after_seconds")
    hang_guard = recommendations.get("high_hang_guard_seconds")
    history_state = str(recommendations.get("history_state") or "unknown")
    insufficient_reason = recommendations.get("insufficient_history_reason")

    lines = ["  - timing guidance:"]
    lines.append(
        "    - expected duration range: "
        f"p05={_format_seconds_for_prompt(expected_dict.get('low'))}, "
        f"median={_format_seconds_for_prompt(expected_dict.get('typical'))}, "
        f"p95={_format_seconds_for_prompt(expected_dict.get('high'))} "
        f"(history={history_state})"
    )
    if isinstance(insufficient_reason, str) and insufficient_reason.strip():
        lines.append(f"    - fallback reason: {insufficient_reason.strip()}")
    lines.append(
        "    - runner expected blocking wait: "
        f"{_format_seconds_for_prompt(recommended_wait)} before a status check would normally help"
    )
    lines.append(
        "    - model guidance: do not issue repeated wait/poll actions for normal "
        "completion; the runner owns the wait and will only re-enter you if a fix is needed"
    )
    lines.append(
        "    - hang guard: do not call verification hung until it exceeds "
        f"{_format_seconds_for_prompt(hang_guard)} or shows concrete failure evidence"
    )
    lines.append(
        "    - internal check cadence: if an operator inspects a long verification manually, "
        f"wait near {_format_seconds_for_prompt(check_after)} before checking again"
    )
    if isinstance(verification_result_path, str) and verification_result_path.strip():
        lines.append(
            "    - artifact paths to inspect after the result returns: final result "
            f"`{verification_result_path.strip()}` and any summary_path/artifacts_dir "
            "printed by the verifier"
        )
    if (
        isinstance(verification_timing_profile_path, str)
        and verification_timing_profile_path.strip()
    ):
        lines.append(f"    - timing profile artifact: `{verification_timing_profile_path.strip()}`")
    return lines


def _format_preflight_summary_md(
    *,
    execution_shell: str,
    shell_status: str,
    python_runtime_summary: dict[str, Any],
    python_toolchain_capability: dict[str, Any],
    pip_probe: dict[str, Any] | None,
    pytest_probe: dict[str, Any] | None,
    command_diagnostics: dict[str, Any],
    verification_commands: list[str],
    verification_timeout_seconds: float | None,
    verification_reuse_mode: str,
    verification_broker_command: str | None,
    agent: str,
    codex_sandbox_mode: str | None,
    delegation_capability: dict[str, Any] | None = None,
    verification_timing_profile: dict[str, Any] | None = None,
    verification_timing_profile_path: str | None = None,
    verification_result_path: str | None = None,
) -> str:
    shell_label = execution_shell.strip() or "unknown"
    if shell_label.lower() == "powershell":
        shell_label = "PowerShell (Windows; no `&&` / `||`)"
    elif shell_label.lower() == "bash":
        shell_label = "bash"

    selected = python_runtime_summary.get("selected")
    selected_dict = selected if isinstance(selected, dict) else {}
    py_path = selected_dict.get("path") if isinstance(selected_dict.get("path"), str) else None
    py_version = (
        selected_dict.get("version") if isinstance(selected_dict.get("version"), str) else None
    )

    toolchain_status = python_toolchain_capability.get("toolchain_status", "unknown")
    interpreter_usable = bool(python_toolchain_capability.get("interpreter_usable", False))

    if not py_path:
        python_label = "`unavailable`"
    elif not interpreter_usable:
        python_label = f"`{py_path}` (UNUSABLE)"
    else:
        python_label = f"`{py_path}`"

    if py_version:
        python_label += f" ({py_version})"

    if toolchain_status == "blocked":
        reason_code = python_toolchain_capability.get("reason_code")
        reason = python_toolchain_capability.get("reason")
        if reason_code:
            python_label += f" - BLOCKED: {reason_code}"
            if reason:
                python_label += f" ({reason})"

    pip_label = "unknown"
    if isinstance(pip_probe, dict):
        pip_ok = bool(pip_probe.get("passed") is True)
        reason_code = pip_probe.get("reason_code")
        reason_code_s = reason_code if isinstance(reason_code, str) and reason_code else None
        if pip_ok and interpreter_usable:
            pip_label = "OK"
        elif not interpreter_usable:
            pip_label = "BLOCKED (Python unusable)"
        else:
            suffix = f" ({reason_code_s})" if reason_code_s else ""
            pip_label = "NOT OK" + suffix

    tool_order = ("git", "rg", "pdm", "bash")
    tool_parts: list[str] = []
    for tool in tool_order:
        diag = command_diagnostics.get(tool)
        diag_dict = diag if isinstance(diag, dict) else {}
        status = diag_dict.get("status")
        status_s = status if isinstance(status, str) and status else "unknown"
        label = {
            "present": "OK",
            "missing": "MISSING",
            "unusable": "UNUSABLE",
            "blocked_by_policy": "BLOCKED",
        }.get(status_s, status_s.upper())
        tool_parts.append(f"{tool}={label}")

    lines = [
        f"- Shell: {shell_label} (shell_commands: {shell_status})",
        f"- Python: {python_label}; pip: {pip_label}",
        f"- Tools: {', '.join(tool_parts)}",
    ]
    if isinstance(delegation_capability, dict):
        delegation_state = delegation_capability.get("state")
        delegation_state_s = (
            delegation_state
            if isinstance(delegation_state, str) and delegation_state.strip()
            else "unknown"
        )
        policy_status = delegation_capability.get("policy_status")
        policy_status_s = (
            policy_status if isinstance(policy_status, str) and policy_status.strip() else "unknown"
        )
        cli_status = delegation_capability.get("cli_support_status")
        cli_status_s = (
            cli_status if isinstance(cli_status, str) and cli_status.strip() else "unknown"
        )
        tools_raw = delegation_capability.get("delegation_tool_names")
        tools = [x for x in tools_raw if isinstance(x, str)] if isinstance(tools_raw, list) else []
        tools_label = ", ".join(tools) if tools else "none"
        lines.append(
            "- Delegation: "
            f"{delegation_state_s} (policy={policy_status_s}; cli={cli_status_s}; "
            f"tools={tools_label})"
        )
    if isinstance(pytest_probe, dict):
        pytest_ok = bool(pytest_probe.get("passed") is True)
        reason_code = pytest_probe.get("reason_code")
        reason_code_s = reason_code if isinstance(reason_code, str) and reason_code else None
        if pytest_ok and interpreter_usable:
            pytest_label = "OK"
        elif not interpreter_usable:
            pytest_label = "BLOCKED (Python unusable)"
        else:
            suffix = f" ({reason_code_s})" if reason_code_s else ""
            pytest_label = "NOT OK" + suffix
        lines.append(f"- pytest: {pytest_label}")

    if verification_commands:
        timeout_label = "none"
        if verification_timeout_seconds is not None:
            timeout_label = f"{float(verification_timeout_seconds):g}"
        if (
            verification_reuse_mode == "auto"
            and isinstance(verification_broker_command, str)
            and verification_broker_command.strip()
        ):
            lines.append("- Final handoff verification:")
            lines.append(f"  - timeout_seconds: {timeout_label}")
            lines.append(
                "  - mode: runner-owned blocking wait; do not launch or poll a "
                "verification command yourself during normal completion."
            )
            lines.append(
                "  - note: when you believe the work is complete, return the required "
                "final JSON report. The runner will request verification once, wait for "
                "the broker/client result, and finalize automatically if it passes."
            )
            lines.append(
                "  - failure handling: if verification fails, the runner will re-enter "
                "the agent with one compact fix prompt containing the failing command "
                "tails and artifact paths."
            )
            lines.extend(
                _format_verification_timing_guidance_md(
                    verification_timing_profile=verification_timing_profile,
                    verification_timing_profile_path=verification_timing_profile_path,
                    verification_result_path=verification_result_path,
                )
            )
        else:
            lines.append("- Verification gate:")
            lines.append(f"  - timeout_seconds: {timeout_label}")
            lines.append("  - commands:")
            for cmd in verification_commands:
                lines.append(f"    - `{cmd}`")

    if agent == "codex" and isinstance(codex_sandbox_mode, str):
        sandbox_label = codex_sandbox_mode.strip()
        if sandbox_label.lower().startswith("workspace-"):
            lines.append(
                "- Note: Codex workspace sandbox is enabled "
                f"(sandbox={sandbox_label}); commands/files outside the workspace may be "
                "unavailable. If you need a consistent toolchain, consider "
                "`--exec-backend docker`."
            )
            lines.append(
                "- Do not treat a blocked shell command as proof that the workspace is read-only. "
                "When `allow_edits=true` and the sandbox is workspace-write, retry with simpler "
                "sandbox-compatible commands or file-edit tools before reporting an edit blocker."
            )
        elif sandbox_label.lower() == "danger-full-access":
            lines.append(
                "- Note: Codex unrestricted local sandbox mode is enabled "
                f"(sandbox={sandbox_label}) because native Windows workspace-write cannot perform "
                "write missions reliably. Keep all mission changes within the acquired target "
                "workspace; runner-owned branch, diff, verification, review, and PR gates still "
                "apply."
            )

    return "\n".join(lines)


def _gemini_include_directories_for_workspace(*, workspace_dir: Path) -> list[str]:
    """
    Gemini CLI may apply gitignore-like "ignore patterns" to file tools (read/search), which can
    hide local-only run artifacts (this repo ignores `runs/`) as well as the dot-prefixed
    `LOCAL_BACKEND_RUN_DIR_ALIAS` staging directory used to surface run_dir-scoped content
    (verification broker client/artifacts) to a workspace-confined agent on local backend.

    When a workspace contains `runs/usertest/`, explicitly include that directory so agents can
    read generated `report.md` / `report.json` / `metrics.json` during triage flows. Likewise,
    always include the run_dir alias directory when present so Gemini's file tools can read
    verification artifacts staged there.
    """

    includes: list[str] = []

    # Gemini CLI runs inside the runner's Docker sandbox (Linux). Always pass POSIX-style
    # include-directories to avoid `runs\\usertest` being interpreted as a literal path segment.
    include_rel = agent_path_join("runs", "usertest")
    candidate = workspace_dir / "runs" / "usertest"
    if candidate.is_dir():
        includes.append(include_rel)
    else:
        # Some missions run this repo's own CLI inside the workspace and then try to inspect the
        # resulting artifacts under `runs/usertest/...`. Gemini CLI's file tools may ignore
        # `runs/` by default, so ensure the directory exists up front for this runner repo so we
        # can pass `--include-directories runs/usertest` at process start.
        marker = workspace_dir / "tools" / "scaffold" / "monorepo.toml"
        if marker.exists():
            try:
                candidate.mkdir(parents=True, exist_ok=True)
            except OSError:
                pass
            else:
                includes.append(include_rel)

    if (workspace_dir / LOCAL_BACKEND_RUN_DIR_ALIAS).is_dir():
        includes.append(LOCAL_BACKEND_RUN_DIR_ALIAS)

    return includes


_RUNS_USERTEST_GITIGNORE_MARKER = (
    "# usertest: allow reading run artifacts under runs/usertest for agent file tools."
)


def _gitignore_ignores_runs(text: str) -> bool:
    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("!"):
            continue
        if stripped == "runs" or stripped == "runs/":
            return True
        if stripped.startswith("runs/"):
            return True
    return False


def _maybe_patch_workspace_gitignore_for_runs_usertest(*, workspace_dir: Path) -> None:
    """
    Some agent file tools respect gitignore-style ignore patterns and will refuse to read files
    under ignored directories. Many repos ignore `runs/` by default, but usertest itself writes
    run artifacts under `runs/usertest/**` which are important for triage/rerender workflows.

    This helper patches the acquired (ephemeral) workspace `.gitignore` to re-include
    `runs/usertest/**` while keeping other `runs/*` children ignored.
    """

    gitignore_path = workspace_dir / ".gitignore"
    try:
        existing = gitignore_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return

    if _RUNS_USERTEST_GITIGNORE_MARKER in existing:
        return
    if not _gitignore_ignores_runs(existing):
        return

    # Standard gitignore-compatible pattern sequence to unignore only runs/usertest.
    patch_lines = [
        _RUNS_USERTEST_GITIGNORE_MARKER,
        "!runs/",
        "runs/*",
        "!runs/usertest/",
        "!runs/usertest/**",
        "",
    ]
    prefix = "" if (not existing or existing.endswith("\n")) else "\n"
    patched = existing + prefix + "\n".join(patch_lines)
    try:
        gitignore_path.write_text(patched, encoding="utf-8", newline="\n")
    except OSError:
        return


def _infer_docker_container_name(command_prefix: list[str]) -> str | None:
    if (
        len(command_prefix) >= 3
        and command_prefix[0] == "docker"
        and command_prefix[1] == "exec"
        and isinstance(command_prefix[-1], str)
        and command_prefix[-1].strip()
    ):
        return command_prefix[-1].strip()
    return None


def _render_sandbox_cli_install_hint(agent_cfg: dict[str, Any]) -> str | None:
    install_cfg = agent_cfg.get("sandbox_cli_install")
    if not isinstance(install_cfg, dict):
        return None

    npm_global = install_cfg.get("npm_global")
    npm_pkgs = (
        [x.strip() for x in npm_global if isinstance(x, str) and x.strip()]
        if isinstance(npm_global, list)
        else []
    )
    if npm_pkgs:
        pkgs = " ".join(npm_pkgs)
        return f"`npm install -g {pkgs}` (requires Node.js + npm)"

    pip_items = install_cfg.get("pip")
    pip_pkgs = (
        [x.strip() for x in pip_items if isinstance(x, str) and x.strip()]
        if isinstance(pip_items, list)
        else []
    )
    if pip_pkgs:
        pkgs = " ".join(pip_pkgs)
        return f"`python -m pip install {pkgs}`"

    return None


def _default_agent_install_hint(agent: str) -> str | None:
    agent_norm = (agent or "").strip().lower()
    npm_pkg = {
        "codex": "@openai/codex",
        "claude": "@anthropic-ai/claude-code",
        "gemini": "@google/gemini-cli",
    }.get(agent_norm)
    if npm_pkg:
        return f"`npm install -g {npm_pkg}` (requires Node.js + npm)"
    return None


def _build_binary_missing_hints(
    *,
    agent: str,
    required_binary: str,
    exec_backend: str,
    agent_cfg: dict[str, Any],
    command_prefix: list[str],
) -> dict[str, str]:
    hints: dict[str, str] = {}

    hints["verify"] = f"`{required_binary} --version`"
    hints["config"] = f"Update `configs/agents.yaml` `agents.{agent}.binary` to a valid path/name."
    hints["doctor"] = (
        "Run `python -m agent_adapters.cli doctor` to check which agent CLIs are on PATH."
    )
    hints["offline_validation"] = (
        "To validate the pipeline without executing agent CLIs, use "
        "`usertest batch --validate-only` and/or render the checked-in fixtures under "
        "`examples/golden_runs/`."
    )

    install_hint = _render_sandbox_cli_install_hint(agent_cfg)
    if install_hint is None:
        install_hint = _default_agent_install_hint(agent)
    if exec_backend == "docker":
        details = f" (expected install: {install_hint})" if install_hint else ""
        hints["install"] = (
            "Rebuild the Docker sandbox image so it can install the agent CLI"
            f"{details}; rerun with `--exec-rebuild-image`."
        )
        hints["debug"] = (
            "See `sandbox/docker_build.log` and "
            "`sandbox/sandbox_cli_install.json` in the run directory."
        )
        container_name = _infer_docker_container_name(command_prefix)
        if container_name is not None:
            hints["container"] = (
                "For interactive debugging, rerun with `--exec-keep-container` and inspect "
                f"`sandbox/sandbox.json` (container_name={container_name!r})."
            )
    else:
        hints["install"] = f"Install `{required_binary}` on PATH" + (
            f"; suggested: {install_hint}." if install_hint else "."
        )

    return hints


def _probe_agent_cli_version(
    *,
    binary: str,
    command_prefix: list[str],
    env_overrides: dict[str, str] | None,
    timeout_seconds: float = 2.5,
) -> dict[str, Any]:
    env: dict[str, str] | None = None
    if env_overrides is not None and not command_prefix:
        env = os.environ.copy()
        env.update(
            {k: v for k, v in env_overrides.items() if isinstance(k, str) and isinstance(v, str)}
        )

    binary_to_run = binary
    if os.name == "nt" and not command_prefix:
        binary_to_run = resolve_codex_executable(binary, env=env or os.environ)

    argv = [binary_to_run, "--version"]
    full_argv = [*command_prefix, *argv] if command_prefix else argv

    try:
        probe = _run_bounded_command_probe(
            full_argv,
            timeout_seconds=timeout_seconds,
            env=env,
        )
    except FileNotFoundError as e:
        return {
            "ok": False,
            "argv": full_argv,
            "error": "FileNotFoundError",
            "details": str(e),
        }
    except Exception as e:  # noqa: BLE001
        return {
            "ok": False,
            "argv": full_argv,
            "error": type(e).__name__,
            "details": str(e),
        }

    stdout = (probe.stdout or "").strip()
    stderr = (probe.stderr or "").strip()
    common = {
        "argv": full_argv,
        "exit_code": int(probe.returncode),
        "stdout_excerpt": stdout[:300] if stdout else None,
        "stderr_excerpt": stderr[:300] if stderr else None,
        "probe_timed_out": bool(probe.timed_out),
        "probe_tree_cleanup_succeeded": bool(probe.cleanup_succeeded),
        "probe_tree_cleanup_diagnostic": probe.cleanup_diagnostic,
    }
    if probe.timed_out:
        return {
            "ok": False,
            **common,
            "error": "timeout",
            "timeout_seconds": timeout_seconds,
        }
    if not probe.cleanup_succeeded:
        return {
            "ok": False,
            **common,
            "error": "probe_cleanup_failed",
        }
    return {
        "ok": int(probe.returncode) == 0,
        **common,
    }


def _agent_cli_version_from_probe(version_probe: dict[str, Any] | None) -> str | None:
    if not isinstance(version_probe, dict) or not bool(version_probe.get("ok")):
        return None
    for key in ("stdout_excerpt", "stderr_excerpt"):
        value = version_probe.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip().splitlines()[0][:120]
    return None


def _agent_policy_for_delegation(
    *,
    agent: str,
    policy_cfg: dict[str, Any],
) -> dict[str, Any]:
    raw = policy_cfg.get(agent)
    return raw if isinstance(raw, dict) else {}


def _configured_allowed_tools_for_agent(
    *,
    agent: str,
    policy_cfg: dict[str, Any],
) -> list[str] | None:
    agent_policy = _agent_policy_for_delegation(agent=agent, policy_cfg=policy_cfg)
    raw_allowed = agent_policy.get("allowed_tools")
    if isinstance(raw_allowed, list):
        return [x for x in raw_allowed if isinstance(x, str) and x.strip()]
    return None


def _delegation_tools_from_adapter_contract(
    *,
    agent_cfg: dict[str, Any],
) -> tuple[list[str], str | None]:
    """
    Return delegation tool names declared by adapter configuration.

    Supported contract keys:
    - ``delegation_tools: [tool, ...]``
    - ``delegation: {tools: [tool, ...]}``

    These keys are intentionally opt-in.  The runner does not embed provider-specific names here;
    defaults should be populated only after a local CLI probe or adapter documentation confirms the
    installed agent version's contract.
    """

    tools, source, _confirmed_cli_versions = _delegation_contract_from_adapter_config(
        agent_cfg=agent_cfg
    )
    return tools, source


def _coerce_unique_str_list(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []

    values: list[str] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, str):
            continue
        value = item.strip()
        if not value or value in seen:
            continue
        values.append(value)
        seen.add(value)
    return values


def _delegation_contract_from_adapter_config(
    *,
    agent_cfg: dict[str, Any],
) -> tuple[list[str], str | None, list[str]]:
    """
    Return delegation tool contract declared by adapter configuration.

    ``delegation.confirmed_cli_versions`` is optional for legacy/test contracts, but production
    policy should set it when exposing a confirmed tool through ``allowed_tools``.  When present,
    the capability resolver treats non-matching CLI versions as unsupported rather than silently
    assuming the tool name still exists.
    """

    raw_tools = agent_cfg.get("delegation_tools")
    source = "agent_config.delegation_tools"
    confirmed_cli_versions: list[str] = []
    if isinstance(raw_tools, list):
        raw_versions = agent_cfg.get("delegation_confirmed_cli_versions")
        confirmed_cli_versions = _coerce_unique_str_list(raw_versions)
    else:
        raw_delegation = agent_cfg.get("delegation")
        delegation = raw_delegation if isinstance(raw_delegation, dict) else {}
        raw_tools = delegation.get("tools")
        source = "agent_config.delegation.tools"
        confirmed_cli_versions = _coerce_unique_str_list(delegation.get("confirmed_cli_versions"))

    tools = _coerce_unique_str_list(raw_tools)
    if not tools:
        return [], None, []
    return tools, source, confirmed_cli_versions


def _cli_version_matches_confirmed(
    *,
    cli_version: str | None,
    confirmed_cli_versions: list[str],
) -> bool | None:
    if not confirmed_cli_versions:
        return True
    if not isinstance(cli_version, str) or not cli_version.strip():
        return None

    observed = cli_version.strip()
    for raw_expected in confirmed_cli_versions:
        expected = raw_expected.strip()
        if not expected:
            continue
        if expected.startswith("regex:"):
            pattern = expected.removeprefix("regex:")
            try:
                if re.search(pattern, observed):
                    return True
            except re.error:
                continue
            continue
        if expected.endswith("*") and observed.startswith(expected[:-1]):
            return True
        if observed == expected:
            return True
    return False


def _resolve_delegation_capability(
    *,
    agent: str,
    agent_cfg: dict[str, Any],
    policy_cfg: dict[str, Any],
    cli_version_probe: dict[str, Any] | None = None,
) -> DelegationCapability:
    agent_norm = agent.strip().lower()
    allowed_tools = _configured_allowed_tools_for_agent(agent=agent_norm, policy_cfg=policy_cfg)
    delegation_tool_names, evidence_source, confirmed_cli_versions = (
        _delegation_contract_from_adapter_config(agent_cfg=agent_cfg)
    )
    cli_version = _agent_cli_version_from_probe(cli_version_probe)
    version_probe_copy = dict(cli_version_probe) if isinstance(cli_version_probe, dict) else None
    cli_version_matches = _cli_version_matches_confirmed(
        cli_version=cli_version,
        confirmed_cli_versions=confirmed_cli_versions,
    )

    if not delegation_tool_names:
        return DelegationCapability(
            state="unknown",
            agent=agent_norm or agent,
            cli_version=cli_version,
            configured_allowed_tools=allowed_tools,
            delegation_tool_names=[],
            available_under_policy=None,
            policy_exposes_delegation=None,
            cli_supports_delegation=None,
            policy_status="unknown_no_contract",
            cli_support_status="unknown_no_contract",
            evidence_source="none",
            confidence="low",
            reason=(
                "No delegation tool names were detected from a local CLI probe or documented "
                "adapter contract; capability is unknown and not guessed."
            ),
            cli_version_probe=version_probe_copy,
        )

    if cli_version_matches is None:
        return DelegationCapability(
            state="unknown",
            agent=agent_norm or agent,
            cli_version=cli_version,
            configured_allowed_tools=allowed_tools,
            delegation_tool_names=delegation_tool_names,
            available_under_policy=None,
            policy_exposes_delegation=(
                None
                if allowed_tools is None
                else bool([tool for tool in delegation_tool_names if tool in allowed_tools])
            ),
            cli_supports_delegation=None,
            policy_status=(
                "no_allowlist"
                if allowed_tools is None
                else (
                    "exposed"
                    if [tool for tool in delegation_tool_names if tool in allowed_tools]
                    else "not_exposed"
                )
            ),
            cli_support_status="unknown_cli_version",
            evidence_source=str(evidence_source or "agent_config"),
            confidence="low",
            reason=(
                "Delegation tools are declared by adapter contract, but the CLI version was "
                "not available to verify against confirmed versions. Delegation is marked "
                "unknown rather than guessed."
            ),
            cli_version_probe=version_probe_copy,
        )

    policy_exposes = (
        True
        if allowed_tools is None
        else bool([tool for tool in delegation_tool_names if tool in allowed_tools])
    )
    policy_status = (
        "no_allowlist"
        if allowed_tools is None
        else ("exposed" if policy_exposes else "not_exposed")
    )

    if cli_version_matches is False:
        expected = ", ".join(confirmed_cli_versions)
        observed = cli_version or "unknown"
        return DelegationCapability(
            state="unavailable",
            agent=agent_norm or agent,
            cli_version=cli_version,
            configured_allowed_tools=allowed_tools,
            delegation_tool_names=delegation_tool_names,
            available_under_policy=False,
            policy_exposes_delegation=policy_exposes,
            cli_supports_delegation=False,
            policy_status=policy_status,
            cli_support_status="unsupported_cli_version",
            evidence_source=str(evidence_source or "agent_config"),
            confidence="high",
            reason=(
                "Delegation tools are declared by adapter contract, but the installed CLI "
                f"version ({observed}) does not match the confirmed delegation versions "
                f"({expected}). Treating this as CLI delegation unsupported until the "
                "adapter contract is updated."
            ),
            cli_version_probe=version_probe_copy,
        )

    if allowed_tools is None:
        return DelegationCapability(
            state="available",
            agent=agent_norm or agent,
            cli_version=cli_version,
            configured_allowed_tools=None,
            delegation_tool_names=delegation_tool_names,
            available_under_policy=True,
            policy_exposes_delegation=True,
            cli_supports_delegation=True,
            policy_status=policy_status,
            cli_support_status="supported",
            evidence_source=str(evidence_source or "agent_config"),
            confidence="high",
            reason=(
                "Delegation tools are declared by adapter contract, and the selected policy does "
                "not define an allowed_tools allowlist for this agent."
            ),
            cli_version_probe=version_probe_copy,
        )

    exposed = [tool for tool in delegation_tool_names if tool in allowed_tools]
    if exposed:
        return DelegationCapability(
            state="available",
            agent=agent_norm or agent,
            cli_version=cli_version,
            configured_allowed_tools=allowed_tools,
            delegation_tool_names=delegation_tool_names,
            available_under_policy=True,
            policy_exposes_delegation=True,
            cli_supports_delegation=True,
            policy_status=policy_status,
            cli_support_status="supported",
            evidence_source=str(evidence_source or "agent_config"),
            confidence="high",
            reason=(
                "Delegation tools are declared by adapter contract and exposed by the selected "
                "policy allowed_tools."
            ),
            cli_version_probe=version_probe_copy,
        )

    return DelegationCapability(
        state="unavailable",
        agent=agent_norm or agent,
        cli_version=cli_version,
        configured_allowed_tools=allowed_tools,
        delegation_tool_names=delegation_tool_names,
        available_under_policy=False,
        policy_exposes_delegation=False,
        cli_supports_delegation=True,
        policy_status="not_exposed",
        cli_support_status="supported",
        evidence_source=str(evidence_source or "agent_config"),
        confidence="high",
        reason=(
            "Delegation tools are declared by adapter contract and supported by the CLI, "
            "but the selected policy allowed_tools does not expose any of them."
        ),
        cli_version_probe=version_probe_copy,
    )


def _agent_config_for_capability_probe(
    *,
    agents_cfg: dict[str, Any],
    agent: str,
) -> dict[str, Any]:
    raw = agents_cfg.get(agent)
    return raw if isinstance(raw, dict) else {}


def _agent_version_probe_timeout_seconds(agent: str) -> float:
    if str(agent).strip().lower() == "gemini":
        # Gemini CLI is Node-based and can exceed the normal quick version-probe budget,
        # especially through docker exec.
        return 8.0
    return 2.5


def _resolve_delegation_capabilities(
    *,
    agents_cfg: dict[str, Any],
    policy_cfg: dict[str, Any],
    cli_version_probes: dict[str, dict[str, Any]] | None = None,
) -> dict[str, dict[str, Any]]:
    probes = cli_version_probes if isinstance(cli_version_probes, dict) else {}
    out: dict[str, dict[str, Any]] = {}
    for agent in DELEGATION_CAPABILITY_AGENTS:
        agent_cfg = _agent_config_for_capability_probe(
            agents_cfg=agents_cfg,
            agent=agent,
        )
        probe = probes.get(agent)
        out[agent] = _resolve_delegation_capability(
            agent=agent,
            agent_cfg=agent_cfg,
            policy_cfg=policy_cfg,
            cli_version_probe=probe if isinstance(probe, dict) else None,
        ).to_dict()
    return out


def _selected_delegation_capability(
    *,
    agent: str,
    delegation_capabilities: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    agent_norm = str(agent).strip().lower()
    selected = delegation_capabilities.get(agent_norm)
    if isinstance(selected, dict):
        return selected
    return _resolve_delegation_capability(
        agent=agent_norm or agent,
        agent_cfg={},
        policy_cfg={},
        cli_version_probe=None,
    ).to_dict()


def _agent_auth_env_var_candidates(agent: str) -> tuple[str, ...]:
    agent_norm = (agent or "").strip().lower()
    if agent_norm == "codex":
        return ("OPENAI_API_KEY",)
    if agent_norm == "claude":
        return ("ANTHROPIC_API_KEY", "CLAUDE_API_KEY")
    if agent_norm == "gemini":
        return ("GOOGLE_API_KEY", "GEMINI_API_KEY")
    return ()


def _agent_login_state_paths(agent: str) -> tuple[Path, ...]:
    home = Path.home()
    agent_norm = (agent or "").strip().lower()
    if agent_norm == "codex":
        return (home / ".codex",)
    if agent_norm == "claude":
        return (home / ".claude", home / ".claude.json")
    if agent_norm == "gemini":
        return (home / ".gemini",)
    return ()


def _agent_auth_present_local(
    *,
    agent: str,
    env_overrides: dict[str, str] | None,
) -> tuple[bool, str]:
    env = os.environ
    if env_overrides:
        merged = dict(os.environ)
        merged.update(
            {k: v for k, v in env_overrides.items() if isinstance(k, str) and isinstance(v, str)}
        )
        env = merged  # type: ignore[assignment]

    for key in _agent_auth_env_var_candidates(agent):
        if str(env.get(key, "")).strip():
            return True, f"env:{key}"

    for path in _agent_login_state_paths(agent):
        try:
            if path.exists():
                return True, f"path:{path}"
        except OSError:
            continue

    return False, "missing"


def _agent_auth_present_docker(
    *,
    agent: str,
    exec_use_host_agent_login: bool,
    exec_env_allowlist: list[str],
) -> tuple[bool, str]:
    if exec_use_host_agent_login:
        # Docker backend validates the host login dir exists before starting the sandbox.
        return True, "host_login_mount"

    candidates = set(_agent_auth_env_var_candidates(agent))
    allowlisted = [name for name in exec_env_allowlist if name in candidates]
    if not allowlisted:
        # Best-effort: if no known auth vars are allowlisted, assume auth is missing.
        return False, "missing:env_allowlist"

    for key in allowlisted:
        if str(os.environ.get(key, "")).strip():
            return True, f"env:{key}"

    return False, "missing:env_unset"


def _controlled_codex_overlay_required(
    request: RunRequest,
    *,
    has_sandbox_backend: bool,
) -> bool:
    """Keep the research exec-policy overlay separate from ordinary session resume.

    A local Stage-3 continuation without explicit prefixes still needs the controlled
    host-subscription overlay.  A Docker implementation continuation already receives
    the host ``.codex`` mount and must not be rejected as though it requested the
    local-only research exec policy.
    """

    if request.agent != "codex":
        return False
    if request.codex_execpolicy_allow_prefixes:
        return True
    return bool(request.codex_resume_session_id and not has_sandbox_backend)


def _build_auth_missing_hints(
    *,
    agent: str,
    exec_backend: str,
    exec_use_host_agent_login: bool,
    required_binary: str,
) -> dict[str, str]:
    hints: dict[str, str] = {}
    env_vars = list(_agent_auth_env_var_candidates(agent))
    if env_vars:
        hints["env"] = "Set one of: " + ", ".join(f"`{name}`" for name in env_vars)

    agent_norm = (agent or "").strip().lower()
    if agent_norm == "codex":
        hints["login"] = (
            "`codex login` (subscription) or `$env:OPENAI_API_KEY | codex login --with-api-key`"
        )
    elif agent_norm == "claude":
        hints["login"] = "`claude login` (if supported) or set `ANTHROPIC_API_KEY`"
    elif agent_norm == "gemini":
        hints["login"] = (
            "Set `GOOGLE_API_KEY` (AI Studio key) or configure the Gemini CLI login state"
        )

    if exec_backend == "docker" and not exec_use_host_agent_login:
        hints["docker"] = (
            "For Docker runs, allowlist the auth env var into the container (e.g. "
            f"`--exec-env {env_vars[0]}`) and set it on the host."
            if env_vars
            else "For Docker runs, allowlist the required auth env var via `--exec-env`."
        )
    elif exec_backend == "docker" and exec_use_host_agent_login:
        hints["docker"] = (
            "If you intended API-key auth for Docker, pass `--exec-use-api-key-auth` and "
            "allowlist the key via `--exec-env`."
        )

    hints["verify"] = f"`{required_binary} --version`"
    hints["offline_validation"] = (
        "To validate the pipeline without executing agent CLIs, use "
        "`usertest batch --validate-only` and/or render the checked-in fixtures under "
        "`examples/golden_runs/`."
    )
    return hints


def _infer_shell_policy_status(
    *,
    agent: str,
    codex_policy: dict[str, Any] | None = None,
    claude_policy: dict[str, Any],
    gemini_policy: dict[str, Any],
    has_outer_sandbox: bool,
) -> tuple[str, str, list[str] | None]:
    # Compatibility wrapper: keep the historical runner import surface and ensure tests or
    # callers that monkeypatch runner._effective_gemini_cli_sandbox affect run_once decisions.
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
                _gemini_shell_unavailable_reason(
                    policy_value=gemini_policy.get("sandbox", True),
                    has_outer_sandbox=has_outer_sandbox,
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

    return _infer_shell_policy_status_impl(
        agent=agent,
        codex_policy=codex_policy,
        claude_policy=claude_policy,
        gemini_policy=gemini_policy,
        has_outer_sandbox=has_outer_sandbox,
    )


def _policy_allows_edits(*, agent: str, policy_cfg: dict[str, Any]) -> bool:
    agent_cfg = policy_cfg.get(agent, {})
    agent_cfg = agent_cfg if isinstance(agent_cfg, dict) else {}
    return bool(agent_cfg.get("allow_edits", False))


def _policy_allows_shell(
    *,
    agent: str,
    policy_cfg: dict[str, Any],
    exec_backend: str,
) -> bool:
    claude_policy = policy_cfg.get("claude", {})
    claude_policy = claude_policy if isinstance(claude_policy, dict) else {}

    gemini_policy = policy_cfg.get("gemini", {})
    gemini_policy = gemini_policy if isinstance(gemini_policy, dict) else {}

    status, _reason, _allowed_tools = _infer_shell_policy_status(
        agent=agent,
        codex_policy=policy_cfg.get("codex", {}),
        claude_policy=claude_policy,
        gemini_policy=gemini_policy,
        has_outer_sandbox=(str(exec_backend) == "docker"),
    )
    sandbox_mode: str | None = None
    if agent == "codex":
        codex_policy = policy_cfg.get("codex", {})
        codex_policy = codex_policy if isinstance(codex_policy, dict) else {}
        sandbox_raw = codex_policy.get("sandbox")
        sandbox_mode = sandbox_raw if isinstance(sandbox_raw, str) and sandbox_raw.strip() else None
    capability = _resolve_shell_capability(
        agent=agent,
        operating_system=_runner_host_os(),
        backend=str(exec_backend or "local"),
        sandbox_mode=sandbox_mode,
        policy_status=status,
        policy_reason=_reason,
        allowed_tools=_allowed_tools,
        probe_result=(
            {"kind": "static_policy_recommendation", "ok": True} if status == "allowed" else None
        ),
    )
    return capability.state == "available"


def _recommended_shell_policy_and_exec_backend(
    *,
    policies: dict[str, Any],
    agent: str,
    requires_edits: bool,
    current_policy: str,
    current_exec_backend: str,
) -> tuple[str, str] | None:
    """
    Suggest a single policy/exec-backend combination expected to satisfy:
    - shell commands available (for the selected agent)
    - edits allowed if the mission requires edits
    """

    policies_dict: dict[str, dict[str, Any]] = {}
    for name, cfg in (policies or {}).items():
        if isinstance(name, str) and name.strip() and isinstance(cfg, dict):
            policies_dict[name] = cfg

    if not policies_dict:
        return None

    exec_backend_norm = str(current_exec_backend or "local").strip() or "local"
    exec_backend_candidates: list[str] = [exec_backend_norm]
    if exec_backend_norm != "docker" and _docker_exec_backend_available():
        exec_backend_candidates.append("docker")

    baseline_policy = "write" if requires_edits else "inspect"

    def policy_satisfies(*, policy_name: str, exec_backend: str) -> bool:
        policy_cfg = policies_dict.get(policy_name)
        if not isinstance(policy_cfg, dict):
            return False
        if requires_edits and not _policy_allows_edits(agent=agent, policy_cfg=policy_cfg):
            return False
        if not _policy_allows_shell(agent=agent, policy_cfg=policy_cfg, exec_backend=exec_backend):
            return False
        return True

    policy_name_order: list[str] = []
    if isinstance(current_policy, str) and current_policy.strip():
        policy_name_order.append(current_policy.strip())
    if baseline_policy not in policy_name_order:
        policy_name_order.append(baseline_policy)
    if not requires_edits and "write" not in policy_name_order:
        policy_name_order.append("write")
    for name in sorted(policies_dict):
        if name not in policy_name_order:
            policy_name_order.append(name)

    for exec_backend in exec_backend_candidates:
        if not requires_edits:
            for policy_name in policy_name_order:
                if not policy_satisfies(policy_name=policy_name, exec_backend=exec_backend):
                    continue
                policy_cfg = policies_dict.get(policy_name, {})
                if not _policy_allows_edits(agent=agent, policy_cfg=policy_cfg):
                    return policy_name, exec_backend
            for policy_name in policy_name_order:
                if policy_satisfies(policy_name=policy_name, exec_backend=exec_backend):
                    return policy_name, exec_backend
            continue

        for policy_name in policy_name_order:
            if policy_satisfies(policy_name=policy_name, exec_backend=exec_backend):
                return policy_name, exec_backend

    return None


def _format_usertest_rerun_command(argv: Sequence[str]) -> str:
    return shlex.join([str(x) for x in argv])


def _verification_broker_client_command(
    *,
    run_dir: Path,
    run_dir_mount: str | None,
    workspace_dir: Path,
    contract: VerificationBrokerContract,
) -> str:
    physical_root = _run_dir_agent_visible_root(
        run_dir=run_dir, run_dir_mount=run_dir_mount, workspace_dir=workspace_dir
    )
    client_root = physical_root / "verification_broker" / "client"
    client_root_for_agent = _agent_path_for_staged_file(
        client_root,
        run_dir=physical_root,
        run_dir_mount=run_dir_mount,
    )
    return render_verification_broker_command(
        client_root_for_agent=client_root_for_agent,
        launcher=contract.launcher,
    )


def _probe_verification_broker_launcher(
    *,
    command_prefix: list[str],
    sandbox: Any,
    contract: VerificationBrokerContract,
) -> tuple[Any, dict[str, Any]]:
    launcher = contract.launcher
    required_runtime_commands = verification_broker_runtime_prerequisites(contract)
    if command_prefix and sandbox is not None:
        try:
            present_map, meta = probe_commands_in_container(
                command_prefix=command_prefix,
                commands=list(required_runtime_commands),
            )
        except Exception as exc:  # noqa: BLE001
            return launcher, {
                "present": False,
                "usable": False,
                "resolved_path": None,
                "reason_code": "probe_failed",
                "reason": f"launcher preflight probe failed: {exc}",
                "runtime_dependencies": {},
            }

        dependency_details: dict[str, dict[str, Any]] = {}
        failing_dependency: str | None = None
        failing_detail: dict[str, Any] | None = None
        for dependency in required_runtime_commands:
            detail = meta.get(dependency) if isinstance(meta, dict) else None
            detail_dict = detail if isinstance(detail, dict) else {}
            present = bool(detail_dict.get("present", present_map.get(dependency)))
            usable = bool(detail_dict.get("usable", present))
            normalized_detail = {
                "present": present,
                "usable": usable,
                "resolved_path": (
                    detail_dict.get("resolved_path")
                    if isinstance(detail_dict.get("resolved_path"), str)
                    else None
                ),
                "reason_code": (
                    detail_dict.get("reason_code")
                    if isinstance(detail_dict.get("reason_code"), str)
                    else ("not_found" if not present else None)
                ),
                "reason": (
                    detail_dict.get("reason")
                    if isinstance(detail_dict.get("reason"), str)
                    else (
                        f"`{dependency}` was not found in the verification runtime."
                        if not present
                        else None
                    )
                ),
            }
            dependency_details[dependency] = normalized_detail
            if failing_dependency is None and not usable:
                failing_dependency = dependency
                failing_detail = normalized_detail
        launcher_detail = dependency_details.get(launcher.executable, {})
        if failing_dependency is None:
            return launcher, {
                "present": True,
                "usable": True,
                "resolved_path": launcher_detail.get("resolved_path"),
                "reason_code": None,
                "reason": None,
                "runtime_dependencies": dependency_details,
            }
        assert failing_detail is not None
        reason = failing_detail.get("reason")
        dependency_label = (
            "launcher"
            if failing_dependency == launcher.executable
            else f"required dependency `{failing_dependency}`"
        )
        return launcher, {
            "present": False,
            "usable": False,
            "resolved_path": failing_detail.get("resolved_path"),
            "reason_code": failing_detail.get("reason_code"),
            "reason": (
                f"{dependency_label} unavailable in the verification runtime: {reason}"
                if isinstance(reason, str) and reason.strip()
                else f"{dependency_label} unavailable in the verification runtime."
            ),
            "failed_dependency": failing_dependency,
            "runtime_dependencies": dependency_details,
        }
    launcher_probe = probe_local_verification_launcher(launcher=launcher)
    dependency_details = {launcher.executable: dict(launcher_probe)}
    python_dependency = contract.python_probe_command
    if isinstance(python_dependency, str) and python_dependency.strip():
        python_probe = probe_local_verification_python(python_command=python_dependency)
        dependency_details[python_dependency] = dict(python_probe)
        if not bool(python_probe.get("usable", False)):
            reason = python_probe.get("reason")
            return launcher, {
                "present": False,
                "usable": False,
                "resolved_path": python_probe.get("resolved_path"),
                "reason_code": python_probe.get("reason_code"),
                "reason": (
                    "required dependency `"
                    + python_dependency.strip()
                    + "` unavailable in the verification runtime: "
                    + str(reason).strip()
                    if isinstance(reason, str) and reason.strip()
                    else (
                        f"required dependency `{python_dependency.strip()}` "
                        "unavailable in the verification runtime."
                    )
                ),
                "failed_dependency": python_dependency.strip(),
                "runtime_dependencies": dependency_details,
            }
    if bool(launcher_probe.get("usable", False)):
        return launcher, {
            **launcher_probe,
            "runtime_dependencies": dependency_details,
        }
    return launcher, {
        **launcher_probe,
        "failed_dependency": launcher.executable,
        "runtime_dependencies": dependency_details,
    }


def _verification_terminal_reason(summary: dict[str, Any]) -> str:
    terminal_reason = summary.get("terminal_reason")
    if isinstance(terminal_reason, str) and terminal_reason.strip():
        return terminal_reason.strip()
    if bool(summary.get("cancelled")):
        return "cancelled"
    if bool(summary.get("timed_out")):
        return "timed_out"
    if bool(summary.get("passed")):
        return "passed"
    return "failed"


def _normalize_verification_summary(summary: dict[str, Any]) -> dict[str, Any]:
    payload = dict(summary)
    terminal_reason = _verification_terminal_reason(payload)
    payload["terminal_reason"] = terminal_reason
    payload["status"] = str(payload.get("status") or terminal_reason).strip() or terminal_reason
    payload["passed"] = terminal_reason == "passed"
    payload["timed_out"] = bool(payload.get("timed_out")) or terminal_reason == "timed_out"
    payload["cancelled"] = bool(payload.get("cancelled")) or terminal_reason == "cancelled"
    if terminal_reason == "failed" and not payload.get("failure_reason"):
        payload["failure_reason"] = "verification_failed"
    if terminal_reason == "timed_out" and not payload.get("failure_reason"):
        payload["failure_reason"] = "timed_out"
    if terminal_reason == "cancelled" and not payload.get("failure_reason"):
        payload["failure_reason"] = "cancelled"
    return payload


def _decorate_verification_summary(
    summary: dict[str, Any],
    *,
    source: str,
    reused: bool,
    workspace_hash: WorkspaceStateHash | None,
    broker_request_id: str | None,
    broker_artifacts_dir: str | None,
) -> dict[str, Any]:
    payload = _normalize_verification_summary(summary)
    payload["source"] = source
    payload["reused"] = reused
    payload["workspace_hash"] = workspace_hash.to_dict() if workspace_hash is not None else None
    payload["broker_request_id"] = broker_request_id
    payload["broker_artifacts_dir"] = broker_artifacts_dir
    return payload


def _coerce_verification_summary_from_broker_result(
    broker_result: VerificationBrokerRequestResult,
    *,
    commands_configured: list[str],
) -> dict[str, Any]:
    summary = broker_result.verification_summary
    if isinstance(summary, dict):
        return dict(summary)
    return {
        "schema_version": 1,
        "attempt_number": broker_result.attempt,
        "commands_configured": list(commands_configured),
        "passed": broker_result.status == "passed",
        "started_utc": broker_result.started_utc,
        "finished_utc": broker_result.finished_utc,
        "wall_seconds": 0.0,
        "artifacts_dir": broker_result.artifacts_dir,
        "commands": [],
        "status": broker_result.status,
        "terminal_reason": broker_result.terminal_reason or broker_result.status,
        "timed_out": broker_result.timed_out,
        "cancelled": broker_result.cancelled,
        "failure_reason": broker_result.failure_reason,
        "broker_failure_reason": broker_result.failure_reason,
    }


def _verification_shell_argv(*, command_prefix: list[str], command: str) -> list[str]:
    launcher = resolve_verification_launcher(
        command_prefix=command_prefix,
        is_windows=_is_windows(),
    )
    return [*command_prefix, *launcher.shell_argv_prefix, command]


def _merge_command_rewrite_meta(
    existing: dict[str, Any] | None, new_meta: dict[str, Any] | None
) -> dict[str, Any] | None:
    if new_meta is None:
        return existing
    if existing is None:
        return new_meta

    rewrites: list[dict[str, Any]] = []
    if existing.get("kind") == "multi" and isinstance(existing.get("rewrites"), list):
        rewrites.extend(item for item in existing["rewrites"] if isinstance(item, dict))
    else:
        rewrites.append(existing)

    if new_meta.get("kind") == "multi" and isinstance(new_meta.get("rewrites"), list):
        rewrites.extend(item for item in new_meta["rewrites"] if isinstance(item, dict))
    else:
        rewrites.append(new_meta)

    return {"kind": "multi", "rewrites": rewrites}


_VERIFICATION_SHELL_CONTROL_TOKENS: frozenset[str] = frozenset(
    {
        "|",
        "||",
        "&&",
        ";",
        "<",
        ">",
        ">>",
        "2>",
        "2>>",
        "1>",
        "1>>",
        "&>",
    }
)

_RIPGREP_UNEXPECTED_ARGUMENT_RE = re.compile(
    r"Found argument '([^']+)' which wasn't expected",
    re.IGNORECASE,
)


def _split_verification_command(command: str, *, prefer_posix: bool) -> list[str]:
    posix_order = (True, False) if prefer_posix else (False, True)
    for posix in posix_order:
        try:
            return shlex.split(command, posix=posix)
        except ValueError:
            continue
    return command.split()


def _looks_like_ripgrep_argv(argv: list[str]) -> bool:
    if not argv:
        return False
    exe = str(argv[0] or "").replace("\\", "/").strip()
    if not exe:
        return False
    base = exe.rsplit("/", 1)[-1].lower()
    return base in {"rg", "rg.exe"}


def _maybe_prepare_ripgrep_direct_exec(
    *,
    command_prefix: list[str],
    command: str,
) -> tuple[list[str], list[str]] | None:
    prefer_posix = bool(command_prefix) or (not _is_windows())
    parsed = _split_verification_command(command, prefer_posix=prefer_posix)
    if not _looks_like_ripgrep_argv(parsed):
        return None
    if any(token in _VERIFICATION_SHELL_CONTROL_TOKENS for token in parsed[1:]):
        # This looks like it relies on a shell (pipes, redirects, chaining).
        return None
    return command_prefix, parsed


def _maybe_rewrite_ripgrep_unexpected_argument(
    *,
    argv: list[str],
    stderr_text: str,
) -> tuple[list[str], dict[str, Any]] | None:
    """
    If ripgrep treated a leading-dash pattern as an option and errored, retry by inserting
    `-e` immediately before the unexpected token.

    This enables patterns like `--skip-install` and `--skip-install|--use-pythonpath` to be
    treated as patterns, not flags.
    """

    if not argv:
        return None
    if "-e" in argv or "--regexp" in argv or "--" in argv:
        return None

    match = _RIPGREP_UNEXPECTED_ARGUMENT_RE.search(stderr_text or "")
    if match is None:
        return None
    token = match.group(1)
    if not token or not token.startswith("-"):
        return None

    try:
        idx = argv.index(token)
    except ValueError:
        return None
    if idx == 0:
        return None

    rewritten = [*argv[:idx], "-e", token, *argv[idx + 1 :]]
    meta: dict[str, Any] = {
        "kind": "ripgrep_unexpected_argument_to_regexp",
        "token": token,
        "original_argv": list(argv),
        "rewritten_argv": list(rewritten),
    }
    return rewritten, meta


_VERIFICATION_REJECTION_SENTINELS: frozenset[str] = frozenset({"rejected"})
_VERIFICATION_PATCH_TOOL_RE = re.compile(
    r"^\s*(?:&\s*)?(?:apply_patch|applypatch|apply-patch)(?:\s|$)",
    re.IGNORECASE,
)
_VERIFICATION_PATCH_PAYLOAD_RE = re.compile(
    r"^\s*\*{3}\s+(?:Begin|Update|Add|Delete|End)\s+Patch\b",
    re.IGNORECASE,
)
_VERIFICATION_PYTHON_HEREDOC_RE = re.compile(
    r"^\s*(?:python3?|py)(?=\s|$).*<<",
    re.IGNORECASE,
)


def _looks_like_verification_rejection_sentinel(command: str) -> bool:
    """
    Detect tool/policy rejection tokens that should never be executed as a shell command.

    Some environments wrap shell execution through common launchers (cmd/sh/powershell). If a
    policy layer mistakenly forwards a status token like `rejected` into the execution path, it
    may appear as an inner command (e.g., `cmd /c rejected`). Treat these as structured failures
    and block dispatch rather than letting the shell emit confusing "not recognized" errors.
    """

    def _normalize_token(raw: str) -> str:
        token = (raw or "").strip()
        if not token:
            return ""
        # Common renderings include quotes/backticks and PowerShell's leading `&`.
        while token.startswith("&"):
            token = token[1:].lstrip()
        if len(token) >= 2 and token[0] == token[-1] and token[0] in {'"', "'", "`"}:
            token = token[1:-1].strip()
        return token.strip().lower()

    def _is_rejection_token(raw: str) -> bool:
        normalized = _normalize_token(raw)
        return bool(normalized and normalized in _VERIFICATION_REJECTION_SENTINELS)

    def _unwrap_once(raw: str) -> str | None:
        argv = _split_verification_command(raw, prefer_posix=True)
        if not argv:
            return None
        if argv[0] == "&" and len(argv) >= 2:
            return " ".join(argv[1:])

        exe = str(argv[0] or "").replace("\\", "/").strip()
        if not exe:
            return None
        base = exe.rsplit("/", 1)[-1].lower()

        if base in {"bash", "sh"}:
            for flag in ("-lc", "-c"):
                if len(argv) >= 3 and argv[1] == flag:
                    inner = argv[2]
                    return inner if isinstance(inner, str) and inner.strip() else None
            return None

        if base in {"cmd", "cmd.exe"}:
            if len(argv) >= 3 and argv[1].lower() == "/c":
                inner = argv[2]
                return inner if isinstance(inner, str) and inner.strip() else None
            return None

        if base in {"powershell", "powershell.exe", "pwsh", "pwsh.exe"}:
            lowered = [str(t).lower() if isinstance(t, str) else "" for t in argv]
            for flag in ("-command", "-c"):
                try:
                    idx = lowered.index(flag)
                except ValueError:
                    continue
                if idx + 1 < len(argv):
                    inner = argv[idx + 1]
                    return inner if isinstance(inner, str) and inner.strip() else None
            return None

        return None

    raw = (command or "").strip()
    if not raw:
        return False
    if _is_rejection_token(raw):
        return True

    # Unwrap common shell wrappers a few times so `cmd /c rejected` is treated as a sentinel.
    current: str | None = raw
    for _ in range(3):
        if current is None:
            break
        inner = _unwrap_once(current)
        if inner is None:
            break
        if _is_rejection_token(inner):
            return True
        current = inner
    return False


def _validate_verification_command_dispatch(
    *,
    command: str,
    is_powershell: bool,
) -> dict[str, Any] | None:
    """
    Block malformed command shapes before they reach the shell/process launcher.

    These checks intentionally focus on repeated high-signal failure patterns observed in runs:
    shell-dispatched patch payloads/tool names, POSIX chaining forwarded into PowerShell 5.1, and
    Python heredoc invocations that are incompatible with PowerShell dispatch.
    """

    raw = (command or "").strip()
    if not raw:
        return None

    if _VERIFICATION_PATCH_TOOL_RE.match(raw) or _VERIFICATION_PATCH_PAYLOAD_RE.match(raw):
        return {
            "kind": "shell_dispatched_patch_tool",
            "reason": (
                "Received an apply_patch command or patch payload on the shell verification path."
            ),
            "hint": (
                "Route patch payloads through the apply_patch tool instead of dispatching them "
                "through the shell."
            ),
        }

    if not is_powershell:
        return None

    argv = _split_verification_command(raw, prefer_posix=False)
    if any(token in {"&&", "||"} for token in argv):
        return {
            "kind": "powershell_unsupported_chain_operator",
            "reason": (
                "The command uses POSIX-style `&&`/`||` chaining, which is incompatible with "
                "the PowerShell 5.1 shell contract used for Windows local runs."
            ),
            "hint": (
                "Run the commands separately, or use PowerShell-native sequencing and "
                "`$LASTEXITCODE` checks."
            ),
        }

    if _VERIFICATION_PYTHON_HEREDOC_RE.search(raw):
        return {
            "kind": "powershell_unsupported_python_heredoc",
            "reason": (
                "Python heredoc shell syntax (`python - <<...`) is incompatible with the "
                "PowerShell verification shell."
            ),
            "hint": (
                "Use `python -c`, write the script to a file, or run the command in a bash "
                "execution path instead."
            ),
        }

    return None


def _probe_windows_bash_usable() -> dict[str, Any]:
    return _probe_windows_bash_usable_impl()


def _maybe_rewrite_windows_bash_smoke_verification_command(
    *,
    command: str,
    bash_probe: dict[str, Any],
) -> dict[str, Any] | None:
    """
    If bash is not runnable on Windows local backend, rewrite known smoke invocations to the
    PowerShell equivalent (or skip bash-only checks).

    Returns a dict describing the action, or None to run the command as-is.
    """

    raw = command.strip()
    if not raw:
        return None

    normalized = raw.replace("\\", "/")
    lower = normalized.lower()
    if not lower.startswith("bash "):
        return None

    usable = bool(bash_probe.get("usable", False))
    if usable:
        return None

    # Skip bash-only syntax checks if bash can't execute.
    if lower.startswith("bash -n ") and "scripts/smoke.sh" in lower:
        return {
            "action": "skip",
            "reason": (
                "Skipping `bash -n scripts/smoke.sh` because bash is not runnable on this Windows "
                "host. Run this check on macOS/Linux, or in a Linux Docker backend."
            ),
            "rewrite": {
                "kind": "skip_bash_syntax_check",
                "bash_reason": str(bash_probe.get("reason") or "").strip() or None,
            },
        }

    # Rewrite smoke.sh execution to smoke.ps1.
    if "scripts/smoke.sh" in lower:
        switches: list[str] = []
        if "--skip-install" in lower:
            switches.append("-SkipInstall")
        if "--use-pythonpath" in lower:
            switches.append("-UsePythonPath")
        if "--require-doctor" in lower:
            switches.append("-RequireDoctor")

        ps_cmd = "powershell -NoProfile -ExecutionPolicy Bypass -File .\\scripts\\smoke.ps1" + (
            " " + " ".join(switches) if switches else ""
        )
        return {
            "action": "rewrite",
            "command": ps_cmd,
            "rewrite": {
                "kind": "bash_smoke_to_powershell_smoke",
                "original_command": raw,
                "bash_reason": str(bash_probe.get("reason") or "").strip() or None,
            },
        }

    return None


def _powershell_quote_literal(text: str) -> str:
    # PowerShell: single-quote string literals escape a literal quote by doubling it.
    return "'" + text.replace("'", "''") + "'"


_VERIFICATION_PYTHON_CMD_PATTERN = re.compile(r"^(python3?|py)(?=\s|$)", re.IGNORECASE)
_VERIFICATION_PYTEST_CMD_PATTERN = re.compile(r"^pytest(?=\s|$)", re.IGNORECASE)


def _rewrite_verification_command_for_python(
    command: str,
    *,
    python_executable: str | None,
    is_powershell: bool,
) -> tuple[str, bool]:
    """
    Rewrite `python ...` / `py ...` / `pytest ...` to a fully-qualified, verified interpreter.

    This avoids PATH resolution hitting WindowsApps/Store aliases on restricted Windows runners.
    """

    if not isinstance(python_executable, str) or not python_executable.strip():
        return command, False

    raw = command
    stripped = raw.lstrip()
    indent = raw[: len(raw) - len(stripped)]

    def _python_invocation() -> str:
        if is_powershell:
            return f"& {_powershell_quote_literal(python_executable)}"
        return shlex.quote(python_executable)

    match = _VERIFICATION_PYTHON_CMD_PATTERN.match(stripped)
    if match is not None:
        rest = stripped[match.end() :]
        return indent + _python_invocation() + rest, True

    match = _VERIFICATION_PYTEST_CMD_PATTERN.match(stripped)
    if match is not None:
        rest = stripped[match.end() :]
        return indent + _python_invocation() + " -m pytest" + rest, True

    return command, False


def _probe_same_shell_python_command(
    *,
    command_name: str,
    command_prefix: list[str],
    cwd: Path,
    env_overrides: dict[str, str] | None,
    timeout_seconds: float = _PYTHON_COMMAND_PROBE_BUDGET_SECONDS,
) -> dict[str, Any]:
    return _probe_same_shell_python_command_impl(
        command_name=command_name,
        command_prefix=command_prefix,
        cwd=cwd,
        env_overrides=env_overrides,
        verification_shell_argv=_verification_shell_argv,
        timeout_seconds=timeout_seconds,
    )


def _probe_same_shell_wrapper_command(
    *,
    command_name: str,
    argv_suffix: list[str],
    command_prefix: list[str],
    cwd: Path,
    env_overrides: dict[str, str] | None,
    timeout_seconds: float = _WRAPPER_COMMAND_PROBE_BUDGET_SECONDS,
) -> dict[str, Any]:
    return _probe_same_shell_wrapper_command_impl(
        command_name=command_name,
        argv_suffix=argv_suffix,
        command_prefix=command_prefix,
        cwd=cwd,
        env_overrides=env_overrides,
        verification_shell_argv=_verification_shell_argv,
        timeout_seconds=timeout_seconds,
    )


def _probe_python_context_capability(
    *,
    command_prefix: list[str],
    cwd: Path,
    env_overrides: dict[str, str] | None,
    python_executable: str | None,
    timeout_seconds: float = _PYTHON_CONTEXT_PROBE_BUDGET_SECONDS,
) -> dict[str, Any]:
    return _probe_python_context_capability_impl(
        command_prefix=command_prefix,
        cwd=cwd,
        env_overrides=env_overrides,
        python_executable=python_executable,
        rewrite_verification_command_for_python=_rewrite_verification_command_for_python,
        verification_shell_argv=_verification_shell_argv,
        timeout_seconds=timeout_seconds,
    )


def _validate_python_capability(
    *,
    workspace_dir: Path,
    verification_commands: tuple[str, ...],
    command_prefix: list[str],
    cwd: Path,
    env_overrides: dict[str, str] | None,
) -> dict[str, Any]:
    # Compatibility wrapper: preserve the runner-level monkeypatch/import surface while
    # delegating the capability implementation to runner_core.python_capability.
    return _validate_python_capability_impl(
        workspace_dir=workspace_dir,
        verification_commands=verification_commands,
        command_prefix=command_prefix,
        cwd=cwd,
        env_overrides=env_overrides,
        select_python_runtime_func=select_python_runtime,
        probe_python_context_capability_func=_probe_python_context_capability,
    )


def _terminate_verification_process(proc: subprocess.Popen[str]) -> None:
    try:
        proc.terminate()
    except OSError:
        return
    try:
        proc.wait(timeout=2.0)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        proc.kill()
    except OSError:
        return
    try:
        proc.wait(timeout=5.0)
    except subprocess.TimeoutExpired:
        return


def _run_verification_subprocess(
    *,
    argv: list[str],
    cwd: Path,
    env: dict[str, str] | None,
    stdout_path: Path,
    stderr_path: Path,
    timeout_seconds: float | None,
    deadline_monotonic: float | None,
    cancel_event: threading.Event | None,
    progress_callback: Callable[[dict[str, Any]], None] | None,
    progress_base: dict[str, Any],
) -> tuple[int, bool, bool]:
    cancelled = False
    timed_out = False
    started_monotonic = time.monotonic()
    last_progress_monotonic = started_monotonic - 1.0

    # Preserve the legacy `subprocess.run(...)` execution contract for simple one-shot
    # verification commands. Several bootstrap/rewrite paths and tests rely on that surface,
    # while the broker lifecycle features still use the polling `Popen` path below.
    if (
        timeout_seconds is None
        and deadline_monotonic is None
        and cancel_event is None
        and progress_callback is None
    ):
        stdout_text = ""
        stderr_text = ""
        exit_code = 0
        try:
            proc = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                cwd=str(cwd),
                env=env,
                check=False,
            )
            exit_code = int(proc.returncode or 0)
            stdout_text = proc.stdout or ""
            stderr_text = proc.stderr or ""
        except OSError as exc:
            exit_code = 1
            stderr_text = f"[runner] Failed to start verification command: {exc}\n"

        stdout_path.write_text(stdout_text, encoding="utf-8", newline="\n")
        stderr_path.write_text(stderr_text, encoding="utf-8", newline="\n")
        return exit_code, False, False

    with (
        stdout_path.open("w", encoding="utf-8", newline="\n") as stdout_handle,
        stderr_path.open("w", encoding="utf-8", newline="\n") as stderr_handle,
    ):
        proc = subprocess.Popen(
            argv,
            stdout=stdout_handle,
            stderr=stderr_handle,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(cwd),
            env=env,
        )
        exit_code: int | None = None
        while exit_code is None:
            exit_code = proc.poll()
            if exit_code is not None:
                break
            now = time.monotonic()
            elapsed = max(0.0, now - started_monotonic)
            if progress_callback is not None and (now - last_progress_monotonic) >= 1.0:
                progress_payload = dict(progress_base)
                progress_payload["phase"] = "running_command"
                progress_payload["elapsed_seconds"] = elapsed
                progress_payload["updated_utc"] = _utc_now_z()
                progress_callback(progress_payload)
                last_progress_monotonic = now
            if cancel_event is not None and cancel_event.is_set():
                cancelled = True
                exit_code = 130
                _terminate_verification_process(proc)
                break
            if timeout_seconds is not None and elapsed >= max(0.1, float(timeout_seconds)):
                timed_out = True
                exit_code = 124
                _terminate_verification_process(proc)
                break
            if deadline_monotonic is not None and now >= deadline_monotonic:
                timed_out = True
                exit_code = 124
                _terminate_verification_process(proc)
                break
            wait_seconds = 0.2
            if timeout_seconds is not None:
                wait_seconds = min(wait_seconds, max(0.01, float(timeout_seconds) - elapsed))
            if deadline_monotonic is not None:
                wait_seconds = min(wait_seconds, max(0.01, deadline_monotonic - now))
            if cancel_event is not None:
                cancel_event.wait(wait_seconds)
            else:
                time.sleep(wait_seconds)

        if exit_code is None:
            exit_code = int(proc.wait())

    if timed_out:
        with stderr_path.open("a", encoding="utf-8", newline="\n") as stderr_handle:
            stderr_handle.write(
                f"[runner] Verification command timed out after {timeout_seconds} seconds.\n"
                if timeout_seconds is not None
                else "[runner] Verification command timed out waiting for broker deadline.\n"
            )
    elif cancelled:
        with stderr_path.open("a", encoding="utf-8", newline="\n") as stderr_handle:
            stderr_handle.write(
                "[runner] Verification command cancelled because broker shutdown was requested.\n"
            )

    return int(exit_code or 0), timed_out, cancelled


def _run_verification_commands(
    *,
    run_dir: Path,
    attempt_number: int,
    commands: list[str],
    command_prefix: list[str],
    cwd: Path,
    timeout_seconds: float | None,
    python_executable: str | None,
    python_toolchain_capability: dict[str, Any] | None = None,
    env_overrides: dict[str, str] | None = None,
    artifacts_dir_rel: Path | None = None,
    cancel_event: threading.Event | None = None,
    deadline_monotonic: float | None = None,
    deadline_seconds: float | None = None,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
    run_dir_mount: str | None = None,
    workspace_dir: Path | None = None,
) -> dict[str, Any]:
    attempt_dir_rel = (
        artifacts_dir_rel
        if artifacts_dir_rel is not None
        else Path("verification") / f"attempt{attempt_number}"
    )
    attempt_dir = run_dir / attempt_dir_rel
    attempt_dir.mkdir(parents=True, exist_ok=True)

    started_utc = _utc_now_z()
    started_monotonic = time.monotonic()
    results: list[dict[str, Any]] = []

    toolchain_status = (
        python_toolchain_capability.get("toolchain_status", "unknown")
        if isinstance(python_toolchain_capability, dict)
        else "unknown"
    )
    toolchain_reason_code = (
        python_toolchain_capability.get("reason_code")
        if isinstance(python_toolchain_capability, dict)
        else None
    )
    toolchain_reason = (
        python_toolchain_capability.get("reason")
        if isinstance(python_toolchain_capability, dict)
        else None
    )

    is_powershell = (not command_prefix) and _is_windows()
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

    windows_bash_probe: dict[str, Any] | None = None
    if _is_windows() and not command_prefix:
        if any(
            isinstance(c, str)
            and c.strip()
            and c.strip().replace("\\", "/").lower().startswith("bash ")
            and not _looks_like_verification_rejection_sentinel(c)
            and normalize_command_for_shell(c, shell_family="powershell").action == "passthrough"
            for c in commands
        ):
            windows_bash_probe = _probe_windows_bash_usable()

    for idx, raw in enumerate(commands, start=1):
        cmd_original = raw.strip()
        if not cmd_original:
            continue

        stdout_path = attempt_dir / f"cmd_{idx:02d}.stdout.txt"
        stderr_path = attempt_dir / f"cmd_{idx:02d}.stderr.txt"

        rewrite_meta: dict[str, Any] | None = None
        cmd_after_bash_rewrite = cmd_original
        bash_rewritten = False
        if windows_bash_probe is not None:
            decision = _maybe_rewrite_windows_bash_smoke_verification_command(
                command=cmd_after_bash_rewrite,
                bash_probe=windows_bash_probe,
            )
            if decision is not None:
                rewrite_meta = (
                    decision.get("rewrite") if isinstance(decision.get("rewrite"), dict) else None
                )
                action = decision.get("action")
                if action == "skip":
                    stdout_text = ""
                    stderr_text = str(decision.get("reason") or "").strip() + "\n"
                    try:
                        stdout_path.write_text(stdout_text, encoding="utf-8", newline="\n")
                    except OSError:
                        pass
                    try:
                        stderr_path.write_text(stderr_text, encoding="utf-8", newline="\n")
                    except OSError:
                        pass
                    result = {
                        "index": idx,
                        "command": cmd_original,
                        "effective_command": None,
                        "rewritten": False,
                        "argv": None,
                        "exit_code": 0,
                        "timed_out": False,
                        "cancelled": False,
                        "skipped": True,
                        "skip_reason": str(decision.get("reason") or "").strip() or None,
                        "command_started_utc": _utc_now_z(),
                        "wall_seconds": 0.0,
                        "stdout_path": stdout_path.name,
                        "stderr_path": stderr_path.name,
                        "stdout_tail": _tail_text_for_prompt(stdout_text),
                        "stderr_tail": _tail_text_for_prompt(stderr_text),
                        "rewrite": rewrite_meta,
                    }
                    results.append(result)
                    continue
                if action == "rewrite":
                    new_cmd = decision.get("command")
                    if isinstance(new_cmd, str) and new_cmd.strip():
                        cmd_after_bash_rewrite = new_cmd.strip()
                        bash_rewritten = True

        shell_family = "powershell" if is_powershell else "bash"
        host_normalization = normalize_command_for_shell(
            cmd_after_bash_rewrite,
            shell_family=shell_family,
        )
        cmd_after_host_normalization = cmd_after_bash_rewrite
        host_dispatch_validation: dict[str, Any] | None = None
        host_rewritten = False
        if host_normalization.action == "rewrite":
            normalized_command = host_normalization.command.strip()
            if normalized_command and normalized_command != cmd_after_bash_rewrite:
                cmd_after_host_normalization = normalized_command
                host_rewritten = True
                host_rewrite_meta = {
                    "kind": host_normalization.kind or "host_shell_normalization",
                    "original_command": cmd_after_bash_rewrite,
                    "rewritten_command": normalized_command,
                }
                if host_normalization.reason:
                    host_rewrite_meta["reason"] = host_normalization.reason
                if host_normalization.hint:
                    host_rewrite_meta["hint"] = host_normalization.hint
                rewrite_meta = _merge_command_rewrite_meta(rewrite_meta, host_rewrite_meta)
        elif host_normalization.action == "blocked":
            host_dispatch_validation = {
                "kind": host_normalization.kind or "host_shell_portability_blocked",
                "reason": (
                    host_normalization.reason or "Command is not portable to the active shell."
                ),
                "hint": host_normalization.hint
                or "Rewrite the command for the active shell, or report the portability issue.",
            }

        if (
            host_dispatch_validation is None
            and toolchain_status == "blocked"
            and verification_commands_need_python((cmd_after_host_normalization,))
        ):
            stdout_text = ""
            reason_s = f": {toolchain_reason}" if toolchain_reason else ""
            stderr_text = (
                f"[runner] Verification command skipped: Python toolchain is blocked "
                f"({toolchain_reason_code}){reason_s}\n"
            )
            try:
                stdout_path.write_text(stdout_text, encoding="utf-8", newline="\n")
            except OSError:
                pass
            try:
                stderr_path.write_text(stderr_text, encoding="utf-8", newline="\n")
            except OSError:
                pass
            result = {
                "index": idx,
                "command": cmd_original,
                "effective_command": None,
                "rewritten": False,
                "argv": None,
                "exit_code": 1,  # Marking as failed since it's a blocked required tool
                "timed_out": False,
                "cancelled": False,
                "skipped": True,
                "skip_reason": f"toolchain_blocked: {toolchain_reason_code}",
                "command_started_utc": _utc_now_z(),
                "wall_seconds": 0.0,
                "stdout_path": stdout_path.name,
                "stderr_path": stderr_path.name,
                "stdout_tail": _tail_text_for_prompt(stdout_text),
                "stderr_tail": _tail_text_for_prompt(stderr_text),
            }
            results.append(result)
            continue

        effective_cmd, python_rewritten = _rewrite_verification_command_for_python(
            cmd_after_host_normalization,
            python_executable=python_executable,
            is_powershell=is_powershell,
        )
        rewritten = bool(python_rewritten or bash_rewritten or host_rewritten)
        rejected_sentinel = _looks_like_verification_rejection_sentinel(effective_cmd)
        dispatch_validation = (
            host_dispatch_validation
            if host_dispatch_validation is not None
            else None
            if rejected_sentinel
            else _validate_verification_command_dispatch(
                command=effective_cmd,
                is_powershell=is_powershell,
            )
        )
        cmd_started_utc = _utc_now_z()
        cmd_started_monotonic = time.monotonic()
        timed_out = False
        cancelled = False
        stdout_text = ""
        stderr_text = ""
        exit_code = 0
        argv: list[str] | None = None
        ripgrep_rewritten = False
        progress_base = {
            "command_index": idx,
            "command_count": len(commands),
            "command": cmd_original,
            "updated_utc": cmd_started_utc,
        }

        if progress_callback is not None:
            starting_progress = dict(progress_base)
            starting_progress["phase"] = "starting_command"
            starting_progress["message"] = f"starting verification command {idx}/{len(commands)}"
            progress_callback(starting_progress)

        if rejected_sentinel:
            exit_code = 126
            stderr_text = (
                "[runner] Verification command dispatch blocked: received rejection sentinel "
                f"token={effective_cmd!r}.\n"
                "[runner] This indicates a tool/policy rejection was forwarded as a command.\n"
                "[runner] Fix: propagate the rejection as a structured error instead of "
                "executing it.\n"
            )
            try:
                stdout_path.write_text("", encoding="utf-8", newline="\n")
            except OSError:
                pass
            try:
                stderr_path.write_text(stderr_text, encoding="utf-8", newline="\n")
            except OSError:
                pass
        elif dispatch_validation is not None:
            exit_code = 126
            kind = str(dispatch_validation.get("kind") or "invalid_dispatch").strip()
            reason = str(dispatch_validation.get("reason") or "").strip()
            hint = str(dispatch_validation.get("hint") or "").strip()
            stderr_text = (
                "[runner] Verification command dispatch blocked: "
                f"kind={kind} command={effective_cmd!r}.\n"
            )
            if reason:
                stderr_text += f"[runner] Reason: {reason}\n"
            if hint:
                stderr_text += f"[runner] Fix: {hint}\n"
            try:
                stdout_path.write_text("", encoding="utf-8", newline="\n")
            except OSError:
                pass
            try:
                stderr_path.write_text(stderr_text, encoding="utf-8", newline="\n")
            except OSError:
                pass
        else:
            direct = _maybe_prepare_ripgrep_direct_exec(
                command_prefix=effective_prefix,
                command=effective_cmd,
            )
            if direct is not None:
                prefix, inner_argv = direct
                argv = [*prefix, *inner_argv]
                exit_code, timed_out, cancelled = _run_verification_subprocess(
                    argv=argv,
                    cwd=cwd,
                    env=merged_env,
                    stdout_path=stdout_path,
                    stderr_path=stderr_path,
                    timeout_seconds=timeout_seconds,
                    deadline_monotonic=deadline_monotonic,
                    cancel_event=cancel_event,
                    progress_callback=progress_callback,
                    progress_base=progress_base,
                )
                try:
                    stdout_text = stdout_path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    stdout_text = ""
                try:
                    stderr_text = stderr_path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    stderr_text = ""
                if exit_code != 0 and not timed_out and not cancelled:
                    retry = _maybe_rewrite_ripgrep_unexpected_argument(
                        argv=inner_argv,
                        stderr_text=stderr_text,
                    )
                    if retry is not None:
                        rewritten_inner, rg_meta = retry
                        argv = [*prefix, *rewritten_inner]
                        exit_code, timed_out, cancelled = _run_verification_subprocess(
                            argv=argv,
                            cwd=cwd,
                            env=merged_env,
                            stdout_path=stdout_path,
                            stderr_path=stderr_path,
                            timeout_seconds=timeout_seconds,
                            deadline_monotonic=deadline_monotonic,
                            cancel_event=cancel_event,
                            progress_callback=progress_callback,
                            progress_base=progress_base,
                        )
                        try:
                            stdout_text = stdout_path.read_text(encoding="utf-8", errors="replace")
                        except OSError:
                            stdout_text = ""
                        try:
                            stderr_text = stderr_path.read_text(encoding="utf-8", errors="replace")
                        except OSError:
                            stderr_text = ""
                        ripgrep_rewritten = True
                        rewrite_meta = _merge_command_rewrite_meta(rewrite_meta, rg_meta)
            else:
                argv = _verification_shell_argv(
                    command_prefix=effective_prefix,
                    command=effective_cmd,
                )
                exit_code, timed_out, cancelled = _run_verification_subprocess(
                    argv=argv,
                    cwd=cwd,
                    env=merged_env,
                    stdout_path=stdout_path,
                    stderr_path=stderr_path,
                    timeout_seconds=timeout_seconds,
                    deadline_monotonic=deadline_monotonic,
                    cancel_event=cancel_event,
                    progress_callback=progress_callback,
                    progress_base=progress_base,
                )
                try:
                    stdout_text = stdout_path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    stdout_text = ""
                try:
                    stderr_text = stderr_path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    stderr_text = ""

        if ripgrep_rewritten:
            rewritten = True

        wall_seconds = max(0.0, time.monotonic() - cmd_started_monotonic)
        result = {
            "index": idx,
            "command": cmd_original,
            "effective_command": effective_cmd,
            "rewritten": rewritten,
            "argv": argv,
            "exit_code": exit_code,
            "timed_out": timed_out,
            "cancelled": cancelled,
            "rejected_sentinel": rejected_sentinel,
            "dispatch_blocked": dispatch_validation is not None,
            "dispatch_validation": dispatch_validation,
            "command_started_utc": cmd_started_utc,
            "wall_seconds": wall_seconds,
            "stdout_path": stdout_path.name,
            "stderr_path": stderr_path.name,
            "stdout_tail": _tail_text_for_prompt(stdout_text),
            "stderr_tail": _tail_text_for_prompt(stderr_text),
            "rewrite": rewrite_meta,
        }
        results.append(result)

        if progress_callback is not None:
            finished_progress = dict(progress_base)
            finished_progress["phase"] = (
                "cancelled" if cancelled else ("timed_out" if timed_out else "finished_command")
            )
            finished_progress["elapsed_seconds"] = wall_seconds
            finished_progress["updated_utc"] = _utc_now_z()
            if cancelled:
                finished_progress["message"] = (
                    f"verification command {idx}/{len(commands)} cancelled"
                )
            elif timed_out:
                finished_progress["message"] = (
                    f"verification command {idx}/{len(commands)} timed out"
                )
            else:
                finished_progress["message"] = (
                    "verification command "
                    f"{idx}/{len(commands)} finished with exit_code={exit_code}"
                )
            progress_callback(finished_progress)

        if exit_code != 0:
            break

    finished_utc = _utc_now_z()
    wall_seconds_total = max(0.0, time.monotonic() - started_monotonic)
    cancelled_any = any(bool(r.get("cancelled")) for r in results)
    timed_out_any = any(bool(r.get("timed_out")) for r in results)
    passed = (
        bool(results)
        and not cancelled_any
        and not timed_out_any
        and all(int(r.get("exit_code") or 0) == 0 for r in results)
    )
    terminal_reason = (
        "cancelled"
        if cancelled_any
        else ("timed_out" if timed_out_any else ("passed" if passed else "failed"))
    )
    failure_reason = (
        None
        if terminal_reason == "passed"
        else (
            "cancelled"
            if terminal_reason == "cancelled"
            else ("timed_out" if terminal_reason == "timed_out" else "verification_failed")
        )
    )

    summary = _normalize_verification_summary(
        {
            "schema_version": 1,
            "attempt": attempt_number,
            "artifacts_dir": normalize_agent_path(attempt_dir_rel),
            "started_utc": started_utc,
            "finished_utc": finished_utc,
            "wall_seconds": wall_seconds_total,
            "timeout_seconds": timeout_seconds,
            "deadline_seconds": deadline_seconds,
            "python_executable": python_executable,
            "passed": passed,
            "status": terminal_reason,
            "terminal_reason": terminal_reason,
            "timed_out": timed_out_any,
            "cancelled": cancelled_any,
            "failure_reason": failure_reason,
            "progress": {
                "phase": terminal_reason,
                "message": f"verification finished with terminal_reason={terminal_reason}",
                "updated_utc": finished_utc,
            },
            "commands_configured": list(commands),
            "commands": results,
        }
    )

    # `artifacts_dir` above is a run_dir-relative label kept for host-side bookkeeping
    # (reports, error records). It is not, by itself, resolvable by an agent: on docker
    # backend it is missing the mount prefix, and on local backend run_dir is not reachable
    # from the agent's own workspace at all. `artifacts_dir_for_agent` is the companion path
    # actually safe to surface to the agent, always derived from the same canonical
    # mount-aware resolution used for the verification broker client command.
    artifacts_dir_for_agent: str | None
    mirror_dir: Path | None = None
    if run_dir_mount is not None:
        artifacts_dir_for_agent = _agent_path_for_staged_file(
            attempt_dir, run_dir=run_dir, run_dir_mount=run_dir_mount
        )
    elif workspace_dir is not None:
        mirror_dir = workspace_dir / LOCAL_BACKEND_RUN_DIR_ALIAS / attempt_dir_rel
        artifacts_dir_for_agent = str(mirror_dir.resolve())
    else:
        artifacts_dir_for_agent = None
    summary["artifacts_dir_for_agent"] = artifacts_dir_for_agent

    _write_json(attempt_dir / "verification.json", summary)

    if mirror_dir is not None:
        # Local backend has no run_dir mount, so an agent confined to its own workspace
        # cannot read `attempt_dir` at its real (run_dir) location. Mirror the finished
        # attempt directory into the workspace so the reported `artifacts_dir_for_agent`
        # path actually resolves to a readable file. `run_dir` remains the durable,
        # canonical copy of these artifacts regardless of backend.
        try:
            mirror_dir.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(attempt_dir, mirror_dir, dirs_exist_ok=True)
        except OSError:
            summary["artifacts_dir_for_agent"] = None

    return summary


def capture_local_verification(
    *,
    run_dir: Path,
    cwd: Path,
    commands: Sequence[str],
    timeout_seconds: float | None,
    python_executable: str | None = None,
) -> dict[str, Any]:
    """Execute an immutable command list without starting an agent turn.

    This is the deliberately small, host-local counterpart to the normal
    post-agent verification path.  It is used when an already committed clean
    implementation head needs a fresh receipt for the exact stage-6 command
    contract.  The command text is retained byte-for-byte, no workspace mirror
    is produced, and the canonical summary is written at ``verification.json``
    in ``run_dir``.
    """

    resolved_run_dir = run_dir.expanduser().resolve()
    resolved_cwd = cwd.expanduser().resolve()
    if not resolved_cwd.is_dir():
        raise ValueError(f"Verification cwd does not exist: {resolved_cwd}")
    normalized_commands: list[str] = []
    for command in commands:
        if not isinstance(command, str) or not command:
            raise ValueError("Verification commands must be non-empty strings")
        if command != command.strip():
            raise ValueError(
                "Verification commands must not contain leading or trailing whitespace"
            )
        safety_errors = verification_command_safety_errors(command)
        if safety_errors:
            raise ValueError(
                f"Unsafe verification command {command!r}: " + "; ".join(safety_errors)
            )
        normalized_commands.append(command)
    if not normalized_commands:
        raise ValueError("At least one verification command is required")
    if len(normalized_commands) != len(set(normalized_commands)):
        raise ValueError("Verification commands must not contain duplicates")
    if timeout_seconds is not None:
        if isinstance(timeout_seconds, bool) or float(timeout_seconds) <= 0:
            raise ValueError("Verification timeout must be positive when provided")
        timeout_seconds = float(timeout_seconds)

    resolved_run_dir.mkdir(parents=True, exist_ok=True)
    if (resolved_run_dir / "verification.json").exists() or (
        resolved_run_dir / "verification" / "capture"
    ).exists():
        raise ValueError(
            f"Refusing to overwrite an existing verification capture: {resolved_run_dir}"
        )
    summary = _run_verification_commands(
        run_dir=resolved_run_dir,
        attempt_number=1,
        commands=normalized_commands,
        command_prefix=[],
        cwd=resolved_cwd,
        timeout_seconds=timeout_seconds,
        python_executable=python_executable,
        env_overrides=_augment_env_with_workspace_pythonpath(
            env_overrides=None,
            workspace_dir=resolved_cwd,
            workspace_mount=None,
        ),
        artifacts_dir_rel=Path("verification") / "capture",
        run_dir_mount=None,
        workspace_dir=None,
    )
    summary["capture_mode"] = "local_exact_commands"
    summary["model_invoked"] = False
    summary["workspace_mirror_written"] = False
    _write_json(resolved_run_dir / "verification.json", summary)
    return summary


def _first_verification_rejection_sentinel(
    verification_summary: dict[str, Any],
) -> dict[str, Any] | None:
    commands = verification_summary.get("commands")
    if not isinstance(commands, list):
        return None
    for item in commands:
        if not isinstance(item, dict):
            continue
        if item.get("rejected_sentinel") is True:
            return item
    return None


def _utc_now_z() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _maybe_write_token_monitoring_artifacts(run_dir: Path) -> None:
    try:
        from token_monitoring import write_run_monitoring

        write_run_monitoring(run_dir)
    except Exception as exc:  # noqa: BLE001
        _write_json(
            run_dir / "token_monitoring_error.json",
            {
                "schema_version": 1,
                "type": type(exc).__name__,
                "message": str(exc),
                "generated_at_utc": _utc_now_z(),
                "non_fatal": True,
            },
        )


def _maybe_write_lifecycle_telemetry(
    *,
    run_dir: Path,
    request: RunRequest,
    model: str | None,
) -> None:
    try:
        from runner_core.lifecycle_telemetry import write_run_lifecycle_telemetry

        write_run_lifecycle_telemetry(
            run_dir=run_dir,
            agent=request.agent,
            model=model,
            policy=request.policy,
            parent_case_id=request.parent_case_id,
            case_lifecycle_id=request.case_lifecycle_id,
            origin_stage=request.origin_stage,
            supervisor_instruction=request.supervisor_instruction,
            codex_resume_session_id=request.codex_resume_session_id,
            codex_resume_usage_source_run_dir=request.codex_resume_usage_source_run_dir,
        )
    except Exception as exc:  # noqa: BLE001
        _write_json(
            run_dir / "lifecycle_telemetry_error.json",
            {
                "schema_version": 1,
                "type": type(exc).__name__,
                "message": str(exc),
                "generated_at_utc": _utc_now_z(),
                "non_fatal": True,
            },
        )


def _schema_is_task_run_v1(schema_dict: dict[str, Any]) -> bool:
    properties = schema_dict.get("properties")
    properties_dict = properties if isinstance(properties, dict) else {}
    kind = properties_dict.get("kind")
    kind_dict = kind if isinstance(kind, dict) else {}
    return kind_dict.get("const") == "task_run_v1"


def _maybe_write_shell_capability_block_report_artifacts(
    *,
    run_dir: Path,
    target_ref: dict[str, Any],
    schema_dict: dict[str, Any],
    mission_id: str | None,
    message: str,
    hint: str,
    shell_capability: dict[str, Any],
) -> None:
    """
    Write normal report artifacts for shell-capability preflight blocks when the mission's
    existing schema is the task-run report schema.

    This keeps blocked shell-required runs auditable through the same report.json/report.md
    surface used by completed task missions, without introducing a new command, mode, or schema.
    """

    if not _schema_is_task_run_v1(schema_dict):
        _append_shell_capability_normalized_event(
            run_dir=run_dir,
            shell_capability=shell_capability,
            blocked=True,
        )
        return

    state = shell_capability.get("state")
    state_s = state if isinstance(state, str) and state.strip() else "unknown"
    reason = shell_capability.get("reason")
    reason_s = reason if isinstance(reason, str) and reason.strip() else message
    reason_code = shell_capability.get("reason_code")
    reason_code_s = reason_code if isinstance(reason_code, str) and reason_code.strip() else None
    mission_label = mission_id or "selected mission"
    report = {
        "schema_version": 1,
        "kind": "task_run_v1",
        "status": "failure",
        "confidence": 1.0,
        "goal": f"Run mission '{mission_label}'.",
        "summary": (
            f"Runner blocked dispatch before starting the agent because canonical shell "
            f"capability is {state_s}."
        ),
        "steps": [
            {
                "name": "Resolve shell capability",
                "attempts": [
                    {
                        "action": (
                            "Resolve effective agent, OS, backend, sandbox, policy, "
                            "and probe state."
                        ),
                        "result": f"canonical shell capability state={state_s}",
                        "evidence": "preflight.json -> shell_capability",
                    }
                ],
                "outcome": message,
            }
        ],
        "outputs": [
            {
                "label": "Preflight artifact",
                "path": "preflight.json",
                "description": "Contains canonical shell capability details.",
            },
            {
                "label": "Preflight error",
                "path": "error.json",
                "description": "Contains the loud blocked dispatch result.",
            },
        ],
        "issues": [
            {
                "severity": "error",
                "title": f"Shell capability {state_s}",
                "details": reason_s,
                "evidence": (
                    f"state={state_s}" + (f"; reason_code={reason_code_s}" if reason_code_s else "")
                ),
                "suggested_fix": hint,
            }
        ],
        "next_actions": [hint],
        "extensions": {"shell_capability": shell_capability},
    }
    validation_errors = validate_report(report, schema_dict, require_shell_capability=True)
    if validation_errors:
        _write_json(run_dir / "report_validation_errors.json", validation_errors)
        return

    _write_json(run_dir / "report.json", report)
    md = render_report_markdown(report=report, metrics=None, target_ref=target_ref)
    (run_dir / "report.md").write_text(md, encoding="utf-8", newline="\n")
    _append_shell_capability_normalized_event(
        run_dir=run_dir,
        shell_capability=shell_capability,
        blocked=True,
    )


def _append_shell_capability_normalized_event(
    *,
    run_dir: Path,
    shell_capability: dict[str, Any],
    blocked: bool,
) -> None:
    if not isinstance(shell_capability, dict):
        return
    event = make_event(
        "preflight_shell_capability",
        {
            "capability": "shell_commands",
            "blocked": bool(blocked),
            "shell_capability": shell_capability,
        },
    )
    path = run_dir / "normalized_events.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")


def _maybe_codex_login_in_sandbox(
    *,
    command_prefix: list[str],
    run_dir: Path,
) -> None:
    setup_log = run_dir / "sandbox_setup.txt"

    # Avoid `printenv ... | codex login ...`:
    # - It appends a newline to the key.
    # - In POSIX `sh`, pipeline exit codes do not reflect earlier failures (no pipefail),
    #   so a missing OPENAI_API_KEY can be silently ignored.
    login_cmd = (
        'if [ -z "${OPENAI_API_KEY:-}" ]; then '
        'echo "OPENAI_API_KEY is not set in the sandbox environment" >&2; exit 1; '
        "fi; "
        'echo "OPENAI_API_KEY length=${#OPENAI_API_KEY}" >&2; '
        'printf "%s" "$OPENAI_API_KEY" | codex login --with-api-key'
    )
    proc = subprocess.run(
        [*command_prefix, "sh", "-lc", login_cmd],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )

    setup_log.parent.mkdir(parents=True, exist_ok=True)
    setup_log.write_text(
        "\n".join(
            [
                f"$ docker exec ... sh -lc {login_cmd!r}",
                f"exit_code={proc.returncode}",
                "",
                "stdout:",
                proc.stdout.strip(),
                "",
                "stderr:",
                proc.stderr.strip(),
                "",
            ]
        ),
        encoding="utf-8",
    )

    if proc.returncode != 0:
        raise RuntimeError(
            "Failed to log into Codex inside the Docker sandbox. "
            "Prefer --exec-use-host-agent-login to reuse your local Codex subscription login "
            "state (~/.codex) inside Docker without API keys. "
            "If you must use an API key, opt into API-key mode with --exec-use-api-key-auth, "
            "ensure OPENAI_API_KEY is set on the host, and allowlist it via "
            "--exec-env OPENAI_API_KEY. "
            f"See {setup_log}."
        )


def _finalize_controlled_codex_execpolicy(
    *,
    overlay: ControlledCodexExecpolicyOverlay,
    binary: str | None,
    env_overrides: dict[str, str] | None,
    config_overrides: Sequence[str] | None,
    run_dir: Path,
) -> list[str]:
    """Run the post-agent host-login proof exactly once, then restore target rules."""

    if "post_login_status" not in overlay.receipt:
        if binary is None or config_overrides is None:
            overlay.record_post_login_status(
                {
                    "ok": False,
                    "exit_code": 1,
                    "expected_status": "Logged in using ChatGPT",
                    "status_kind": "missing",
                    "chatgpt_status_exact": False,
                    "auth_env_vars_blank": {name: False for name in CONTROLLED_CODEX_AUTH_ENV_VARS},
                    "error_kind": "CodexBinaryUnavailable",
                }
            )
        else:
            try:
                status = probe_codex_login_status(
                    binary=binary,
                    codex_home=overlay.host_codex_home,
                    cwd=run_dir,
                    config_overrides=config_overrides,
                    env_overrides=env_overrides,
                )
                overlay.record_post_login_status(status.to_redacted_dict())
            except Exception as exc:  # noqa: BLE001
                overlay.record_post_login_status(
                    {
                        "ok": False,
                        "exit_code": 1,
                        "expected_status": "Logged in using ChatGPT",
                        "status_kind": "missing",
                        "chatgpt_status_exact": False,
                        "auth_env_vars_blank": {
                            name: False for name in CONTROLLED_CODEX_AUTH_ENV_VARS
                        },
                        "error_kind": type(exc).__name__,
                    }
                )
    errors = overlay.restore()
    errors.extend(controlled_codex_execpolicy_receipt_errors(overlay.receipt))
    return list(dict.fromkeys(errors))


def run_once(config: RunnerConfig, request: RunRequest) -> RunResult:
    policy_cfg = config.policies.get(request.policy, {})
    if not isinstance(policy_cfg, dict):
        policy_cfg = {}

    codex_policy = policy_cfg.get("codex", {})
    codex_policy = codex_policy if isinstance(codex_policy, dict) else {}

    claude_policy = policy_cfg.get("claude", {})
    claude_policy = claude_policy if isinstance(claude_policy, dict) else {}

    gemini_policy = policy_cfg.get("gemini", {})
    gemini_policy = gemini_policy if isinstance(gemini_policy, dict) else {}

    if request.agent == "codex":
        allow_edits = bool(codex_policy.get("allow_edits", False))
    elif request.agent == "claude":
        allow_edits = bool(claude_policy.get("allow_edits", False))
    elif request.agent == "gemini":
        allow_edits = bool(gemini_policy.get("allow_edits", False))
    else:
        raise NotImplementedError(
            f"Unsupported agent={request.agent!r}. "
            "MVP implements `codex`, `claude`, and `gemini`; other agents are placeholders."
        )

    acquired = None
    workspace_ref_payload: dict[str, Any] | None = None
    retain_workspace_for_reported_output = False
    codex_execpolicy_overlay: ControlledCodexExecpolicyOverlay | None = None
    controlled_codex_binary: str | None = None
    controlled_codex_env_overrides: dict[str, str] | None = None
    controlled_codex_config_overrides: list[str] | None = None
    codex_session_id: str | None = request.codex_resume_session_id
    codex_last_invocation_resumed = False
    effective_model = (
        request.model.strip()
        if isinstance(request.model, str) and request.model.strip()
        else None
    )

    if request.codex_resume_session_id is not None and request.agent != "codex":
        raise ValueError("codex_resume_session_id is only valid for the codex agent")

    target_slug = slugify(request.repo)
    timestamp = utc_timestamp_compact()
    run_dir = config.runs_dir / target_slug / timestamp / request.agent / str(request.seed)
    run_dir.mkdir(parents=True, exist_ok=True)

    has_retained_asset_root = request.retained_oracle_assets_root is not None
    has_retained_asset_spec = request.retained_oracle_asset_spec is not None
    if has_retained_asset_root != has_retained_asset_spec:
        raise ValueError("retained_oracle_asset_transport_incomplete")
    if has_retained_asset_spec:
        if str(request.verification_reuse_mode or "").strip().lower() != "off":
            raise ValueError("retained_oracle_asset_requires_verification_reuse_off")
        assert request.retained_oracle_assets_root is not None
        assert request.retained_oracle_asset_spec is not None
        validate_retained_oracle_asset_source(
            spec=request.retained_oracle_asset_spec,
            trusted_runs_root=request.retained_oracle_assets_root,
        )

    run_start_monotonic = time.monotonic()
    run_meta: dict[str, Any] = {
        "schema_version": 1,
        "run_started_utc": _utc_now_z(),
        "phases": {},
    }
    runner_implementation = capture_runner_implementation_provenance(config.repo_root)
    runner_implementation_path = run_dir / "runner_implementation.json"
    _write_json(runner_implementation_path, runner_implementation)
    run_meta["runner_implementation"] = {
        "artifact_path": str(runner_implementation_path),
        "available": runner_implementation.get("available") is True,
        "head_commit": runner_implementation.get("head_commit"),
        "dirty": runner_implementation.get("dirty"),
        "implementation_identity_sha256": runner_implementation.get(
            "implementation_identity_sha256"
        ),
    }
    shell_capability_summary: dict[str, Any] | None = None
    codex_metadata_capture_summary: dict[str, Any] | None = None
    if request.agent == "codex":
        codex_metadata_capture_summary = _new_codex_metadata_capture_summary()
        run_meta["codex_metadata_capture"] = codex_metadata_capture_summary
    agent_phase_start_monotonic: float | None = None
    agent_phase_end_monotonic: float | None = None
    postprocess_phase_start_monotonic: float | None = None

    workspace_id = f"{target_slug}_{timestamp}_{request.agent}_{request.seed}"
    try:
        preferred_workspace_dir = config.runs_dir / "_workspaces" / workspace_id
        resume_workspace_dir = request.resume_workspace_dir
        if resume_workspace_dir is not None:
            acquired = acquire_existing_target(
                repo=request.repo,
                workspace_dir=resume_workspace_dir,
                ref=request.ref,
            )
        else:
            acquired = acquire_target(
                repo=request.repo,
                dest_dir=preferred_workspace_dir,
                ref=request.ref,
            )
        using_existing_workspace = acquired.mode == "existing"

        workspace_ref_payload = {
            "schema_version": 1,
            "workspace_id": workspace_id,
            "workspace_dir": str(acquired.workspace_dir),
            "keep_workspace_requested": bool(request.keep_workspace),
            "will_cleanup_workspace": (
                not using_existing_workspace
                and not (request.keep_workspace or request.exec_keep_container)
            ),
            "resume_workspace_requested": (
                str(resume_workspace_dir) if resume_workspace_dir is not None else None
            ),
        }
        _write_json(run_dir / "workspace_ref.json", workspace_ref_payload)

        agent_cfg = config.agents.get(request.agent, {}) if isinstance(config.agents, dict) else {}
        agent_cfg_dict = agent_cfg if isinstance(agent_cfg, dict) else {}
        effective_model, model_source = _resolve_effective_agent_model(
            agent=request.agent,
            agent_cfg=agent_cfg_dict,
            requested_model=request.model,
        )

        target_ref: dict[str, Any] = {
            "repo_input": acquired.repo_input,
            "ref": acquired.ref,
            "commit_sha": acquired.commit_sha,
            "acquire_mode": acquired.mode,
            "agent": request.agent,
            "policy": request.policy,
            "seed": request.seed,
            "obfuscate_agent_docs": bool(request.obfuscate_agent_docs),
            "requested_persona_id": request.persona_id,
            "requested_mission_id": request.mission_id,
            "requested_codex_resume_session_id": request.codex_resume_session_id,
            **(
                {"case_lifecycle_id": request.case_lifecycle_id.strip()}
                if isinstance(request.case_lifecycle_id, str)
                and request.case_lifecycle_id.strip()
                else {}
            ),
            "codex_resume_usage_source_run_dir": (
                str(request.codex_resume_usage_source_run_dir.resolve())
                if request.codex_resume_usage_source_run_dir is not None
                else None
            ),
            **({"model": effective_model} if effective_model is not None else {}),
            **({"model_source": model_source} if model_source is not None else {}),
        }
        if request.retained_oracle_asset_spec is not None:
            assert request.retained_oracle_assets_root is not None
            target_ref["retained_oracle_asset_transport"] = {
                "trusted_runs_root": str(request.retained_oracle_assets_root.resolve()),
                "spec": request.retained_oracle_asset_spec,
            }
        if (
            isinstance(request.supervisor_instruction, str)
            and request.supervisor_instruction.strip()
        ):
            target_ref["supervisor_instruction"] = request.supervisor_instruction.strip()
        lineage_values = {
            "evidence_role": request.evidence_role,
            "origin_stage": request.origin_stage,
            "parent_case_id": request.parent_case_id,
        }
        if any(value is not None for value in lineage_values.values()):
            evidence_role = (
                request.evidence_role.strip() if isinstance(request.evidence_role, str) else ""
            )
            origin_stage = (
                request.origin_stage.strip() if isinstance(request.origin_stage, str) else ""
            )
            parent_case_id = (
                request.parent_case_id.strip() if isinstance(request.parent_case_id, str) else ""
            )
            if evidence_role not in {
                "observation",
                "research",
                "implementation",
                "verification",
            }:
                raise ValueError("runner_backlog_lineage_evidence_role_invalid")
            if not origin_stage:
                raise ValueError("runner_backlog_lineage_origin_stage_invalid")
            if evidence_role in {"research", "implementation", "verification"} and not (
                parent_case_id
            ):
                raise ValueError("runner_backlog_lineage_parent_case_id_required")
            target_ref["backlog_lineage"] = {
                "evidence_role": evidence_role,
                "origin_stage": origin_stage,
                "parent_case_id": parent_case_id or None,
            }
        if request.model is not None and request.model.strip():
            target_ref["requested_model"] = request.model.strip()
        _write_json(run_dir / "target_ref.json", target_ref)

        codex_binary = agent_cfg_dict.get("binary", "codex")
        codex_subcommand = agent_cfg_dict.get("subcommand", "exec")
        default_overrides: list[str] = []
        raw_defaults = agent_cfg_dict.get("config_overrides")
        if isinstance(raw_defaults, list):
            default_overrides = [x for x in raw_defaults if isinstance(x, str)]

        combined_overrides = [*default_overrides, *request.agent_config_overrides]
        codex_personality_override_requested = (
            _codex_personality_override_requested(combined_overrides)
            if request.agent == "codex"
            else False
        )
        preflight_warnings: list[dict[str, Any]] = []
        if request.agent == "codex":
            reasoning_issue = validate_codex_reasoning_effort_config_overrides(combined_overrides)
            if reasoning_issue is not None:
                message = reasoning_issue.message
                hint = reasoning_issue.hint
                _write_json(
                    run_dir / "preflight.json",
                    {
                        "warnings": preflight_warnings,
                        "agent_config_validation": {
                            "ok": False,
                            "issues": [
                                {
                                    "code": "codex_model_reasoning_effort_invalid",
                                    "message": message,
                                    "hint": hint,
                                    "details": reasoning_issue.details,
                                }
                            ],
                        },
                    },
                )
                _write_json(
                    run_dir / "error.json",
                    {
                        "type": "AgentPreflightFailed",
                        "subtype": "invalid_agent_config",
                        "code": "codex_model_reasoning_effort_invalid",
                        "agent": request.agent,
                        "message": message,
                        "hint": hint,
                        "details": reasoning_issue.details,
                    },
                )
                return RunResult(
                    run_dir=run_dir,
                    exit_code=1,
                    report_validation_errors=[
                        message,
                        "code=codex_model_reasoning_effort_invalid",
                        f"hint={hint}",
                    ],
                )

            personality_issue = validate_codex_personality_config_overrides(combined_overrides)
            if personality_issue is not None:
                message = personality_issue.message
                hint = personality_issue.hint
                _write_json(
                    run_dir / "preflight.json",
                    {
                        "warnings": preflight_warnings,
                        "agent_config_validation": {
                            "ok": False,
                            "issues": [
                                {
                                    "code": "codex_model_messages_missing",
                                    "message": message,
                                    "hint": hint,
                                    "details": personality_issue.details,
                                }
                            ],
                        },
                    },
                )
                _write_json(
                    run_dir / "error.json",
                    {
                        "type": "AgentPreflightFailed",
                        "subtype": "invalid_agent_config",
                        "code": "codex_model_messages_missing",
                        "agent": request.agent,
                        "message": message,
                        "hint": hint,
                        "details": personality_issue.details,
                    },
                )
                return RunResult(
                    run_dir=run_dir,
                    exit_code=1,
                    report_validation_errors=[
                        message,
                        "code=codex_model_messages_missing",
                        f"hint={hint}",
                    ],
                )

        append_text = request.agent_append_system_prompt
        if isinstance(append_text, str) and not append_text.strip():
            append_text = None
        if request.retained_oracle_asset_spec is not None:
            append_text = "\n\n".join(
                part.rstrip()
                for part in (append_text, RETAINED_ORACLE_AGENT_NOTE)
                if isinstance(part, str) and part.strip()
            )
        if (
            isinstance(request.supervisor_instruction, str)
            and request.supervisor_instruction.strip()
        ):
            supervisor_section = (
                "# Runner-owned supervisor execution constraints\n\n"
                + request.supervisor_instruction.strip()
            )
            append_text = "\n\n".join(
                part.rstrip()
                for part in (append_text, supervisor_section)
                if isinstance(part, str) and part.strip()
            )

        if (
            request.agent == "gemini"
            and (request.agent_append_system_prompt_file is not None or append_text is not None)
            and request.agent_system_prompt_file is None
        ):
            preflight_warnings.append(
                {
                    "code": "gemini_system_prompt_append_autocomposed",
                    "agent": request.agent,
                    "message": (
                        "Gemini CLI does not support appending to the system prompt; "
                        "composing a replacement system prompt file from append content."
                    ),
                }
            )

        catalog_config = load_catalog_config(config.repo_root, acquired.workspace_dir)

        resolved_inputs = resolve_effective_run_inputs(
            runner_repo_root=config.repo_root,
            target_repo_root=acquired.workspace_dir,
            catalog_config=catalog_config,
            persona_id=request.persona_id,
            mission_id=request.mission_id,
        )
        effective_spec = resolved_inputs.effective

        # Fail fast: permission requirements are validated before any expensive backend setup.
        shell_status, shell_reason, allowed_tools = _infer_shell_policy_status(
            agent=request.agent,
            codex_policy=codex_policy,
            claude_policy=claude_policy,
            gemini_policy=gemini_policy,
            has_outer_sandbox=(request.exec_backend == "docker"),
        )

        if (
            request.agent == "gemini"
            and bool(resolved_inputs.mission.requires_shell)
            and str(request.exec_backend) == "local"
            and shell_status == "blocked"
            and isinstance(allowed_tools, list)
            and "run_shell_command" in allowed_tools
        ):
            effective_gemini_sandbox = _effective_gemini_cli_sandbox(
                policy_value=gemini_policy.get("sandbox", True),
                has_outer_sandbox=False,
            )
            if not effective_gemini_sandbox and _docker_exec_backend_available():
                preflight_warnings.append(
                    {
                        "code": "gemini_exec_backend_autoselected",
                        "agent": request.agent,
                        "message": (
                            "Mission requires shell commands, but Gemini sandbox is unavailable "
                            "under `--exec-backend local`; auto-selecting `--exec-backend docker`."
                        ),
                        "details": {"from": "local", "to": "docker"},
                    }
                )
                request = replace(request, exec_backend="docker")
                shell_status, shell_reason, allowed_tools = _infer_shell_policy_status(
                    agent=request.agent,
                    codex_policy=codex_policy,
                    claude_policy=claude_policy,
                    gemini_policy=gemini_policy,
                    has_outer_sandbox=True,
                )

        host_os_for_shell_capability = _runner_host_os()
        early_codex_sandbox_mode: str | None = None
        if request.agent == "codex":
            early_codex_sandbox_mode = _resolve_codex_sandbox_mode(
                request=request,
                codex_policy=codex_policy,
                has_sandbox_backend=(str(request.exec_backend) == "docker"),
            )
        shell_capability = _resolve_shell_capability(
            agent=request.agent,
            operating_system=host_os_for_shell_capability,
            backend=str(request.exec_backend or "local"),
            sandbox_mode=early_codex_sandbox_mode,
            policy_status=shell_status,
            policy_reason=shell_reason,
            allowed_tools=allowed_tools,
        )
        shell_capability_summary = shell_capability.to_dict()
        agents_cfg_for_capabilities = config.agents if isinstance(config.agents, dict) else {}
        delegation_capabilities_summary = _resolve_delegation_capabilities(
            agents_cfg=agents_cfg_for_capabilities,
            policy_cfg=policy_cfg,
        )
        delegation_capability_summary = _selected_delegation_capability(
            agent=request.agent,
            delegation_capabilities=delegation_capabilities_summary,
        )

        early_shell_capability_state = shell_capability_summary.get("state")
        defer_shell_launch_probe = (
            early_shell_capability_state == "unprobed"
            and str(shell_status).strip().lower() != "blocked"
        )
        if (
            bool(resolved_inputs.mission.requires_shell)
            and early_shell_capability_state != "available"
            and not defer_shell_launch_probe
        ):
            requires_edits = bool(resolved_inputs.mission.requires_edits)

            suggested_policy = "write" if requires_edits else "inspect"
            suggested_exec_backend = str(request.exec_backend or "local").strip() or "local"
            suggested = _recommended_shell_policy_and_exec_backend(
                policies=config.policies,
                agent=request.agent,
                requires_edits=requires_edits,
                current_policy=request.policy,
                current_exec_backend=str(request.exec_backend or "local"),
            )
            if suggested is not None:
                suggested_policy, suggested_exec_backend = suggested

            gemini_local_sandbox_available = True
            if request.agent == "gemini" and str(request.exec_backend) == "local":
                gemini_local_sandbox_available = _effective_gemini_cli_sandbox(
                    policy_value=gemini_policy.get("sandbox", True),
                    has_outer_sandbox=False,
                )

            blocked_by_backend = (
                request.agent == "gemini"
                and str(request.exec_backend) == "local"
                and isinstance(allowed_tools, list)
                and "run_shell_command" in allowed_tools
                and not gemini_local_sandbox_available
            )
            if blocked_by_backend and suggested_exec_backend == "local":
                suggested_exec_backend = "docker"

            if blocked_by_backend:
                message = (
                    f"Mission '{effective_spec.mission_id}' requires shell commands, but "
                    "Gemini shell execution is unavailable under `--exec-backend local` "
                    "(Gemini sandbox disabled/unavailable)."
                )
                hint = "Rerun with `--exec-backend docker` (recommended)."
                if requires_edits and not allow_edits:
                    suggested_policy = "write"
                else:
                    suggested_policy = request.policy
            else:
                message = (
                    f"Mission '{effective_spec.mission_id}' requires shell commands, but "
                    f"policy '{request.policy}' for agent '{request.agent}' blocks shell commands."
                )
                hint = (
                    "Use `--policy write` (allows edits + shell)."
                    if suggested_policy == "write"
                    else "Use `--policy inspect` (read-only + shell)."
                )
                if suggested_exec_backend == "docker" and str(request.exec_backend) == "local":
                    hint = f"{hint} Also add `--exec-backend docker`."
                if shell_capability_summary.get("state") == "unprobed":
                    message = (
                        f"Mission '{effective_spec.mission_id}' requires shell commands, but "
                        f"canonical shell capability for agent '{request.agent}' is unprobed "
                        "in the effective execution path."
                    )
                    hint = (
                        "Use a backend/policy combination with canonical shell capability "
                        "available (recommended: `--exec-backend docker` for local Windows "
                        "Codex runs)."
                    )
                    if suggested_exec_backend == "local":
                        suggested_exec_backend = "docker"

            suggested_command_argv: list[str] = [
                "python",
                "-m",
                "usertest.cli",
                "run",
                "--repo-root",
                ".",
                "--repo",
                str(request.repo),
                "--agent",
                str(request.agent),
                "--policy",
                str(suggested_policy),
            ]
            if request.ref:
                suggested_command_argv.extend(["--ref", str(request.ref)])
            if effective_spec.persona_id:
                suggested_command_argv.extend(["--persona-id", str(effective_spec.persona_id)])
            if effective_spec.mission_id:
                suggested_command_argv.extend(["--mission-id", str(effective_spec.mission_id)])
            if suggested_exec_backend != "local":
                suggested_command_argv.extend(["--exec-backend", str(suggested_exec_backend)])
            suggested_command = _format_usertest_rerun_command(suggested_command_argv)
            _write_json(
                run_dir / "preflight.json",
                {
                    "warnings": preflight_warnings,
                    "capabilities": {
                        "shell_commands": {
                            "status": shell_status,
                            "reason": shell_reason,
                            "allowed_tools": allowed_tools,
                            "canonical": shell_capability_summary,
                        },
                        "delegation": delegation_capability_summary,
                        "delegation_by_agent": delegation_capabilities_summary,
                        "edits": {"allowed": bool(allow_edits)},
                    },
                    "shell_capability": shell_capability_summary,
                    "delegation_capability": delegation_capability_summary,
                    "delegation_capabilities": delegation_capabilities_summary,
                    "mission_requirements": {
                        "mission_id": effective_spec.mission_id,
                        "requires_shell": bool(resolved_inputs.mission.requires_shell),
                        "requires_edits": bool(resolved_inputs.mission.requires_edits),
                    },
                },
            )
            _write_json(
                run_dir / "error.json",
                {
                    "type": "AgentPreflightFailed",
                    "subtype": "mission_requires_shell",
                    "code": "mission_requires_shell",
                    "agent": request.agent,
                    "policy": request.policy,
                    "mission_id": effective_spec.mission_id,
                    "capability": "shell_commands",
                    "message": message,
                    "hint": hint,
                    "suggested_policy": suggested_policy,
                    "suggested_command": suggested_command,
                    "preflight": {
                        "capabilities": {
                            "shell_commands": {
                                "status": shell_status,
                                "reason": shell_reason,
                                "allowed_tools": allowed_tools,
                                "canonical": shell_capability_summary,
                            },
                            "delegation": delegation_capability_summary,
                            "delegation_by_agent": delegation_capabilities_summary,
                        },
                        "shell_capability": shell_capability_summary,
                        "delegation_capability": delegation_capability_summary,
                        "delegation_capabilities": delegation_capabilities_summary,
                    },
                },
            )
            _maybe_write_shell_capability_block_report_artifacts(
                run_dir=run_dir,
                target_ref=target_ref,
                schema_dict=effective_spec.report_schema_dict,
                mission_id=effective_spec.mission_id,
                message=message,
                hint=hint,
                shell_capability=shell_capability_summary,
            )
            return RunResult(
                run_dir=run_dir,
                exit_code=1,
                report_validation_errors=[
                    message,
                    "code=mission_requires_shell",
                    "Recommended next command:",
                    suggested_command,
                ],
            )

        if bool(resolved_inputs.mission.requires_edits) and not allow_edits:
            message = (
                f"Mission '{effective_spec.mission_id}' requires edits, but policy "
                f"'{request.policy}' for agent '{request.agent}' has allow_edits=false."
            )
            hint = "Use --policy write (or update configs/policies.yaml to allow edits)."
            _write_json(
                run_dir / "preflight.json",
                {
                    "warnings": preflight_warnings,
                    "capabilities": {
                        "shell_commands": {
                            "status": shell_status,
                            "reason": shell_reason,
                            "allowed_tools": allowed_tools,
                            "canonical": shell_capability_summary,
                        },
                        "delegation": delegation_capability_summary,
                        "delegation_by_agent": delegation_capabilities_summary,
                        "edits": {"allowed": bool(allow_edits)},
                    },
                    "shell_capability": shell_capability_summary,
                    "delegation_capability": delegation_capability_summary,
                    "delegation_capabilities": delegation_capabilities_summary,
                    "mission_requirements": {
                        "mission_id": effective_spec.mission_id,
                        "requires_shell": bool(resolved_inputs.mission.requires_shell),
                        "requires_edits": bool(resolved_inputs.mission.requires_edits),
                    },
                },
            )
            _write_json(
                run_dir / "error.json",
                {
                    "type": "AgentPreflightFailed",
                    "subtype": "mission_requires_edits",
                    "code": "mission_requires_edits",
                    "agent": request.agent,
                    "policy": request.policy,
                    "mission_id": effective_spec.mission_id,
                    "capability": "edits",
                    "message": message,
                    "hint": hint,
                    "preflight": {"capabilities": {"edits": {"allowed": bool(allow_edits)}}},
                },
            )
            return RunResult(
                run_dir=run_dir,
                exit_code=1,
                report_validation_errors=[message, "code=mission_requires_edits", f"hint={hint}"],
            )

        if request.policy in {"inspect", "write"} and shell_status == "blocked":
            hint: str | None = None
            suggested_command: str | None = None
            if (
                request.agent == "gemini"
                and str(request.exec_backend) == "local"
                and isinstance(allowed_tools, list)
                and "run_shell_command" in allowed_tools
            ):
                message = (
                    f"Policy '{request.policy}' enables Gemini shell commands, but shell "
                    "execution is unavailable under `--exec-backend local` "
                    "(Gemini sandbox disabled/unavailable)."
                )
                hint = "Rerun with `--exec-backend docker` (recommended)."
                suggested_command_parts: list[str] = [
                    "python",
                    "-m",
                    "usertest.cli",
                    "run",
                    "--repo-root",
                    ".",
                    "--repo",
                    json.dumps(request.repo, ensure_ascii=False),
                    "--agent",
                    request.agent,
                    "--policy",
                    request.policy,
                    "--exec-backend",
                    "docker",
                ]
                if request.ref:
                    suggested_command_parts.extend(
                        ["--ref", json.dumps(request.ref, ensure_ascii=False)]
                    )
                if effective_spec.persona_id:
                    suggested_command_parts.extend(["--persona-id", effective_spec.persona_id])
                if effective_spec.mission_id:
                    suggested_command_parts.extend(["--mission-id", effective_spec.mission_id])
                suggested_command = " ".join(suggested_command_parts)
            else:
                message = (
                    f"Policy '{request.policy}' for agent '{request.agent}' blocks shell "
                    "commands. Fix configs/policies.yaml or pick a policy that enables shell "
                    "command execution."
                )
            _write_json(
                run_dir / "preflight.json",
                {
                    "warnings": preflight_warnings,
                    "capabilities": {
                        "shell_commands": {
                            "status": shell_status,
                            "reason": shell_reason,
                            "allowed_tools": allowed_tools,
                            "canonical": shell_capability_summary,
                        },
                        "delegation": delegation_capability_summary,
                        "delegation_by_agent": delegation_capabilities_summary,
                        "edits": {"allowed": bool(allow_edits)},
                    },
                    "shell_capability": shell_capability_summary,
                    "delegation_capability": delegation_capability_summary,
                    "delegation_capabilities": delegation_capabilities_summary,
                    "mission_requirements": {
                        "mission_id": effective_spec.mission_id,
                        "requires_shell": bool(resolved_inputs.mission.requires_shell),
                        "requires_edits": bool(resolved_inputs.mission.requires_edits),
                    },
                },
            )
            _write_json(
                run_dir / "error.json",
                {
                    "type": "AgentPreflightFailed",
                    "subtype": "policy_block",
                    "agent": request.agent,
                    "capability": "shell_commands",
                    "message": message,
                    **({"hint": hint} if hint else {}),
                    **({"suggested_command": suggested_command} if suggested_command else {}),
                    "preflight": {
                        "capabilities": {
                            "shell_commands": {
                                "status": shell_status,
                                "reason": shell_reason,
                                "allowed_tools": allowed_tools,
                            },
                            "delegation": delegation_capability_summary,
                            "delegation_by_agent": delegation_capabilities_summary,
                        },
                        "delegation_capability": delegation_capability_summary,
                        "delegation_capabilities": delegation_capabilities_summary,
                    },
                },
            )
            return RunResult(run_dir=run_dir, exit_code=1, report_validation_errors=[message])

        if request.obfuscate_agent_docs:
            obfuscate_target_agent_docs(workspace_dir=acquired.workspace_dir, run_dir=run_dir)
            if allow_edits:
                preprocess_commit_sha = _maybe_commit_preprocess_workspace(
                    acquired.workspace_dir,
                    message="usertest: preprocess workspace (obfuscate agent docs)",
                )
                if preprocess_commit_sha:
                    (run_dir / "preprocess_commit.txt").write_text(
                        preprocess_commit_sha + "\n", encoding="utf-8"
                    )
                    target_ref["preprocess_commit_sha"] = preprocess_commit_sha
                    _write_json(run_dir / "target_ref.json", target_ref)

        users_md_path = acquired.workspace_dir / "USERS.md"
        users_md_present = users_md_path.exists()
        users_md_text = users_md_path.read_text(encoding="utf-8") if users_md_present else ""
        if users_md_present:
            (run_dir / "users.md").write_text(users_md_text, encoding="utf-8")

        persona_source_text = resolved_inputs.persona.source_path.read_text(encoding="utf-8")
        mission_source_text = resolved_inputs.mission.source_path.read_text(encoding="utf-8")

        (run_dir / "persona.source.md").write_text(persona_source_text, encoding="utf-8")
        (run_dir / "persona.resolved.md").write_text(
            effective_spec.persona_md_resolved.rstrip() + "\n", encoding="utf-8"
        )
        (run_dir / "mission.source.md").write_text(mission_source_text, encoding="utf-8")
        (run_dir / "mission.resolved.md").write_text(
            effective_spec.mission_md_resolved.rstrip() + "\n", encoding="utf-8"
        )
        (run_dir / "prompt.template.md").write_text(
            effective_spec.prompt_template_text, encoding="utf-8"
        )

        _write_json(run_dir / "report.schema.json", effective_spec.report_schema_dict)

        _write_json(
            run_dir / "effective_run_spec.json",
            {
                "persona_id": effective_spec.persona_id,
                "persona_name": effective_spec.persona_name,
                "persona_md_resolved": effective_spec.persona_md_resolved,
                "persona_source_path": str(resolved_inputs.persona.source_path),
                "mission_id": effective_spec.mission_id,
                "mission_name": effective_spec.mission_name,
                "mission_md_resolved": effective_spec.mission_md_resolved,
                "mission_source_path": str(resolved_inputs.mission.source_path),
                "execution_mode": effective_spec.execution_mode,
                "prompt_template_path": str(effective_spec.prompt_template_path),
                "prompt_template_text": effective_spec.prompt_template_text,
                "report_schema_path": str(effective_spec.report_schema_path),
                "report_schema_dict": effective_spec.report_schema_dict,
            },
        )

        target_ref.update(
            {
                "users_md_present": users_md_present,
                "persona_id": effective_spec.persona_id,
                "mission_id": effective_spec.mission_id,
                "prompt_template_path": str(effective_spec.prompt_template_path),
                "report_schema_path": str(effective_spec.report_schema_path),
            }
        )
        _write_json(run_dir / "target_ref.json", target_ref)

        raw_events_path = run_dir / "raw_events.jsonl"
        raw_events_ts_path = raw_events_path.with_suffix(".ts.jsonl")
        last_message_path = run_dir / "agent_last_message.txt"
        stderr_path = run_dir / "agent_stderr.txt"
        attempts_meta: list[dict[str, Any]] = []

        backend = prepare_execution_backend(
            repo_root=config.repo_root,
            run_dir=run_dir,
            workspace_dir=acquired.workspace_dir,
            request=request,
            workspace_id=workspace_id,
            agent_cfg=agent_cfg_dict,
        )
        sandbox = backend.sandbox_instance
        command_prefix = backend.command_prefix
        workspace_mount = backend.workspace_mount
        # When executing inside a docker sandbox, `workspace_mount` is a POSIX path like
        # `/workspace`. On Windows hosts, `Path("/workspace")` becomes `\\workspace`, which
        # break agents that interpret `--cd` literally. Keep it as a string when mounted.
        workspace_dir_for_agent: Path | str
        if workspace_mount is not None:
            workspace_dir_for_agent = normalize_agent_path(workspace_mount)
        else:
            workspace_dir_for_agent = acquired.workspace_dir
        staged_system_prompt: Path | None = None
        system_prompt_path_for_agent: str | None = None
        if request.agent_system_prompt_file is not None:
            src_path = _resolve_agent_prompt_input_path(
                raw=request.agent_system_prompt_file,
                repo_root=config.repo_root,
                workspace_dir=acquired.workspace_dir,
            )
            staged_system_prompt = _stage_agent_prompt_file(
                run_dir=run_dir,
                name="system_prompt.md",
                src_path=src_path,
            )
            if request.agent == "gemini":
                # Gemini CLI has no system-prompt file flag; agent adapters inject the content
                # into stdin on the host. Therefore use the host path, not a container mount path.
                system_prompt_path_for_agent = str(staged_system_prompt)
            else:
                system_prompt_path_for_agent = _agent_path_for_staged_file(
                    staged_system_prompt,
                    run_dir=run_dir,
                    run_dir_mount=backend.run_dir_mount,
                )

        append_src_path: Path | None = None
        if request.agent_append_system_prompt_file is not None:
            append_src_path = _resolve_agent_prompt_input_path(
                raw=request.agent_append_system_prompt_file,
                repo_root=config.repo_root,
                workspace_dir=acquired.workspace_dir,
            )

        if append_src_path is not None or append_text is not None:
            _materialize_agent_prompt_into_workspace(
                workspace_dir=acquired.workspace_dir,
                name="append_system_prompt.md",
                src_path=append_src_path,
                text=append_text,
            )

        staged_append_system_prompt: Path | None = None
        append_system_prompt_path_for_agent: str | None = None
        if append_src_path is not None or append_text is not None:
            if request.agent == "gemini":
                # Gemini CLI doesn't support an explicit "append to system prompt" mechanism.
                # Emulate append by composing a replacement system prompt file and passing that
                # file as the Gemini system prompt.
                if staged_system_prompt is None:
                    staged_system_prompt = _stage_agent_prompt_text(
                        run_dir=run_dir,
                        name="system_prompt.md",
                        text="",
                    )
                if append_src_path is not None:
                    append_payload = append_src_path.read_text(encoding="utf-8")
                else:
                    assert append_text is not None
                    append_payload = append_text

                base_payload = staged_system_prompt.read_text(encoding="utf-8")

                merged_parts: list[str] = []
                if base_payload.strip():
                    merged_parts.append(base_payload.rstrip())
                if append_payload.strip():
                    merged_parts.append(append_payload.strip())
                merged_payload = "\n\n".join(merged_parts).rstrip() + "\n"

                staged_system_prompt.write_text(merged_payload, encoding="utf-8")

                system_prompt_path_for_agent = str(staged_system_prompt)
            else:
                if append_src_path is not None:
                    staged_append_system_prompt = _stage_agent_prompt_file(
                        run_dir=run_dir,
                        name="append_system_prompt.md",
                        src_path=append_src_path,
                    )
                else:
                    assert append_text is not None
                    staged_append_system_prompt = _stage_agent_prompt_text(
                        run_dir=run_dir,
                        name="append_system_prompt.md",
                        text=append_text,
                    )

                append_system_prompt_path_for_agent = _agent_path_for_staged_file(
                    staged_append_system_prompt,
                    run_dir=run_dir,
                    run_dir_mount=backend.run_dir_mount,
                )

        if staged_system_prompt is not None:
            _materialize_agent_prompt_into_workspace(
                workspace_dir=acquired.workspace_dir,
                name="system_prompt.md",
                src_path=staged_system_prompt,
                text=None,
            )

        if staged_system_prompt is not None:
            try:
                target_ref["agent_system_prompt_file"] = (
                    staged_system_prompt.resolve().relative_to(run_dir.resolve()).as_posix()
                )
            except Exception:
                target_ref["agent_system_prompt_file"] = staged_system_prompt.as_posix()
        if staged_append_system_prompt is not None:
            try:
                target_ref["agent_append_system_prompt_file"] = (
                    staged_append_system_prompt.resolve().relative_to(run_dir.resolve()).as_posix()
                )
            except Exception:
                target_ref["agent_append_system_prompt_file"] = (
                    staged_append_system_prompt.as_posix()
                )
        if staged_system_prompt is not None or staged_append_system_prompt is not None:
            _write_json(run_dir / "target_ref.json", target_ref)

        try:
            effective_verification_commands = _normalize_verification_commands_for_execution(
                request.verification_commands
            )
            bootstrap: PipBootstrapResult | None = None
            if is_pip_repo_input(request.repo):
                pip_spec = parse_pip_repo_input(request.repo)
                req_path = pip_requirements_path(acquired.workspace_dir)
                requirements_rel = req_path.relative_to(acquired.workspace_dir).as_posix()
                bootstrap = bootstrap_pip_requirements(
                    workspace_dir=acquired.workspace_dir,
                    requirements_relpath=requirements_rel,
                    run_dir=run_dir,
                    command_prefix=command_prefix,
                    workspace_mount=workspace_mount,
                    installer=pip_spec.installer,
                )
            else:
                requirements_dev = acquired.workspace_dir / "requirements-dev.txt"
                if requirements_dev.is_file() and _verification_commands_need_source_bootstrap(
                    effective_verification_commands
                ):
                    bootstrap = bootstrap_pip_requirements(
                        workspace_dir=acquired.workspace_dir,
                        requirements_relpath="requirements-dev.txt",
                        run_dir=run_dir,
                        command_prefix=command_prefix,
                        workspace_mount=workspace_mount,
                        installer="pip",
                    )
                    bootstrap.meta["source_repo_bootstrap"] = True

            agent_env_overrides = dict(bootstrap.env_overrides) if bootstrap is not None else None
            if _verification_commands_need_source_bootstrap(effective_verification_commands):
                agent_env_overrides = _augment_env_with_workspace_pythonpath(
                    env_overrides=agent_env_overrides,
                    workspace_dir=acquired.workspace_dir,
                    workspace_mount=workspace_mount,
                )
            if os.name == "nt" and sandbox is None and bool(resolved_inputs.mission.requires_shell):
                agent_env_overrides = _ensure_windows_python_on_path(
                    workspace_dir=acquired.workspace_dir,
                    env_overrides=agent_env_overrides,
                )
            signed_in_codex_resume = bool(
                request.agent == "codex" and request.codex_resume_session_id is not None
            )
            if signed_in_codex_resume:
                if not bool(request.exec_use_host_agent_login):
                    raise ValueError("codex_resume_requires_host_subscription_login")
                validate_codex_subscription_config_overrides(
                    combined_overrides,
                    source="signed_in_codex_resume",
                )
                agent_env_overrides = dict(agent_env_overrides or {})
                for auth_env_var in CONTROLLED_CODEX_AUTH_ENV_VARS:
                    agent_env_overrides[auth_env_var] = ""
                target_ref["codex_resume_auth"] = {
                    "auth_mode": "host_chatgpt_subscription_login",
                    "host_agent_login_required": True,
                    "api_billing_environment_disabled": True,
                    "blocked_child_env_vars": list(CONTROLLED_CODEX_AUTH_ENV_VARS),
                }
                _write_json(run_dir / "target_ref.json", target_ref)

            controlled_codex_probe_commands: list[str] = []
            controlled_codex_requested = _controlled_codex_overlay_required(
                request,
                has_sandbox_backend=sandbox is not None,
            )
            if controlled_codex_requested:
                if sandbox is not None:
                    raise ValueError("codex_execpolicy_requires_local_execution_backend")
                validate_codex_subscription_config_overrides(
                    combined_overrides,
                    source="controlled_stage3",
                )
                controlled_allow_prefixes = list(request.codex_execpolicy_allow_prefixes)
                controlled_prefixes = set(request.codex_execpolicy_allow_prefixes)
                if ("git", "rev-parse") in controlled_prefixes:
                    controlled_codex_probe_commands.append("git rev-parse --is-inside-work-tree")
                if ("python",) in controlled_prefixes:
                    controlled_codex_probe_commands.append("python --version")
                if ("python",) in set(request.codex_execpolicy_allow_prefixes):
                    python_candidates = [
                        (agent_env_overrides or {}).get("USERTEST_PYTHON"),
                        shutil.which(
                            "python",
                            path=(agent_env_overrides or {}).get("PATH"),
                        ),
                        sys.executable,
                        str(
                            acquired.workspace_dir
                            / ".venv"
                            / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
                        ),
                    ]
                    for candidate in python_candidates:
                        if not isinstance(candidate, str) or not candidate.strip():
                            continue
                        candidate_path = Path(candidate).resolve()
                        if candidate_path.is_file():
                            controlled_allow_prefixes.append((str(candidate_path),))
                if os.name == "nt":
                    controlled_allow_prefixes.append(("Write-Output",))
                else:
                    controlled_allow_prefixes.append(("printf",))
                codex_execpolicy_overlay = install_controlled_codex_execpolicy(
                    workspace_dir=acquired.workspace_dir,
                    run_dir=run_dir,
                    allow_prefixes=controlled_allow_prefixes,
                    agent_workspace_path=workspace_dir_for_agent,
                    activation_probe_required=bool(resolved_inputs.mission.requires_shell),
                    expected_activation_sandbox_mode=_resolve_codex_sandbox_mode(
                        request=request,
                        codex_policy=codex_policy,
                        has_sandbox_backend=False,
                    ),
                )
            if codex_execpolicy_overlay is not None:
                agent_env_overrides = dict(agent_env_overrides or {})
                agent_env_overrides["CODEX_HOME"] = str(codex_execpolicy_overlay.host_codex_home)
                # The controlled backlog-research path intentionally reuses the host's
                # ChatGPT subscription login and must never fall back to per-token API billing.
                for auth_env_var in CONTROLLED_CODEX_AUTH_ENV_VARS:
                    agent_env_overrides[auth_env_var] = ""
                controlled_codex_binary = str(codex_binary)
                controlled_codex_env_overrides = dict(agent_env_overrides)

            codex_sandbox_mode: str | None = None
            codex_ask_for_approval: str | None = None
            if request.agent == "codex":
                codex_sandbox_mode = _resolve_codex_sandbox_mode(
                    request=request,
                    codex_policy=codex_policy,
                    has_sandbox_backend=sandbox is not None,
                )

                ask_for_approval_raw = codex_policy.get("ask_for_approval", "never")
                codex_ask_for_approval = (
                    str(ask_for_approval_raw)
                    if isinstance(ask_for_approval_raw, str) and ask_for_approval_raw.strip()
                    else "never"
                )

            codex_overrides = list(combined_overrides)
            controlled_codex_activation_overrides: list[str] | None = None
            codex_instructions_path_for_agent: str | None = system_prompt_path_for_agent
            if staged_append_system_prompt is not None:
                if staged_system_prompt is not None:
                    try:
                        base_payload = staged_system_prompt.read_text(encoding="utf-8")
                    except OSError:
                        base_payload = ""
                    try:
                        append_payload = staged_append_system_prompt.read_text(encoding="utf-8")
                    except OSError:
                        append_payload = ""
                    merged_parts: list[str] = []
                    if base_payload.strip():
                        merged_parts.append(base_payload.rstrip())
                    if append_payload.strip():
                        merged_parts.append(append_payload.strip())
                    if merged_parts:
                        staged_codex_instructions = _stage_agent_prompt_text(
                            run_dir=run_dir,
                            name="codex_model_instructions.md",
                            text="\n\n".join(merged_parts).rstrip() + "\n",
                        )
                        codex_instructions_path_for_agent = _agent_path_for_staged_file(
                            staged_codex_instructions,
                            run_dir=run_dir,
                            run_dir_mount=backend.run_dir_mount,
                        )
                else:
                    codex_instructions_path_for_agent = _agent_path_for_staged_file(
                        staged_append_system_prompt,
                        run_dir=run_dir,
                        run_dir_mount=backend.run_dir_mount,
                    )
            if codex_instructions_path_for_agent is not None:
                codex_overrides.append(
                    "model_instructions_file="
                    + toml_basic_string(codex_instructions_path_for_agent)
                )
            if signed_in_codex_resume and codex_execpolicy_overlay is None:
                codex_overrides = build_codex_subscription_config_overrides(
                    codex_overrides,
                    source="signed_in_codex_resume_effective",
                )
            if codex_execpolicy_overlay is not None:
                codex_overrides = build_codex_subscription_config_overrides(
                    codex_overrides,
                    source="controlled_stage3_mission",
                    internal_safe_overrides=[
                        *CONTROLLED_CODEX_NON_ROUTING_CONFIG_OVERRIDES,
                        *(
                            [CONTROLLED_CODEX_WINDOWS_SANDBOX_CONFIG_OVERRIDE]
                            if os.name == "nt"
                            else []
                        ),
                        codex_execpolicy_overlay.project_trust_override,
                    ],
                )
                (
                    controlled_codex_config_overrides,
                    controlled_codex_activation_overrides,
                ) = codex_execpolicy_overlay.bind_effective_config(codex_overrides)
                codex_overrides = list(controlled_codex_config_overrides)
                login_status = probe_codex_login_status(
                    binary=str(codex_binary),
                    codex_home=codex_execpolicy_overlay.host_codex_home,
                    cwd=run_dir,
                    config_overrides=controlled_codex_config_overrides,
                    env_overrides=agent_env_overrides,
                )
                login_status_receipt = login_status.to_redacted_dict()
                codex_execpolicy_overlay.record_login_status(login_status_receipt)
                if not login_status.ok:
                    raise ValueError(
                        "codex_execpolicy_chatgpt_login_status_failed:"
                        + str(login_status_receipt.get("status_kind") or "unknown")
                    )

            claude_cfg = config.agents.get("claude", {}) if isinstance(config.agents, dict) else {}
            claude_binary = (
                claude_cfg.get("binary", "claude") if isinstance(claude_cfg, dict) else "claude"
            )
            claude_output_format = (
                claude_cfg.get("output_format", "stream-json")
                if isinstance(claude_cfg, dict)
                else "stream-json"
            )
            claude_allowed_tools: list[str] = []
            raw_claude_allowed = claude_policy.get("allowed_tools")
            if isinstance(raw_claude_allowed, list):
                claude_allowed_tools = [x for x in raw_claude_allowed if isinstance(x, str)]
            claude_permission_mode = claude_policy.get("permission_mode")
            claude_permission_mode = (
                claude_permission_mode if isinstance(claude_permission_mode, str) else None
            )

            gemini_cfg = config.agents.get("gemini", {}) if isinstance(config.agents, dict) else {}
            gemini_binary = (
                gemini_cfg.get("binary", "gemini") if isinstance(gemini_cfg, dict) else "gemini"
            )
            gemini_output_format = (
                gemini_cfg.get("output_format", "stream-json")
                if isinstance(gemini_cfg, dict)
                else "stream-json"
            )
            gemini_sandbox_enabled = _effective_gemini_cli_sandbox(
                policy_value=gemini_policy.get("sandbox", True),
                has_outer_sandbox=sandbox is not None,
            )
            gemini_approval_mode = gemini_policy.get("approval_mode", "default")
            gemini_approval_mode = (
                gemini_approval_mode if isinstance(gemini_approval_mode, str) else "default"
            )
            gemini_allowed_tools: list[str] = []
            raw_gemini_allowed = gemini_policy.get("allowed_tools")
            if isinstance(raw_gemini_allowed, list):
                gemini_allowed_tools = [x for x in raw_gemini_allowed if isinstance(x, str)]
            gemini_env_overrides: dict[str, str] | None = None
            if agent_env_overrides is not None:
                gemini_env_overrides = dict(agent_env_overrides)

            required_agent_binary = _agent_binary_for_preflight_probe(
                agent=request.agent,
                agent_cfg=agent_cfg_dict,
            )
            delegation_agent_binaries: dict[str, str] = {}
            for delegation_agent in DELEGATION_CAPABILITY_AGENTS:
                delegation_agent_cfg = _agent_config_for_capability_probe(
                    agents_cfg=agents_cfg_for_capabilities,
                    agent=delegation_agent,
                )
                delegation_binary = _agent_binary_for_preflight_probe(
                    agent=delegation_agent,
                    agent_cfg=delegation_agent_cfg,
                )
                if delegation_binary is not None:
                    delegation_agent_binaries[delegation_agent] = delegation_binary

            preflight_required_commands = [
                cmd.strip()
                for cmd in request.preflight_required_commands
                if isinstance(cmd, str) and cmd.strip()
            ]

            probe_commands = _build_preflight_command_list(request)
            if required_agent_binary is not None and required_agent_binary not in probe_commands:
                probe_commands.append(required_agent_binary)
            for delegation_binary in delegation_agent_binaries.values():
                if delegation_binary not in probe_commands:
                    probe_commands.append(delegation_binary)
            preflight_commands_present: dict[str, bool] = {}
            preflight_meta: dict[str, Any] = {}
            effective_probe_commands = list(probe_commands)
            try:
                if sandbox is not None:
                    effective_probe_commands = [
                        c for c in probe_commands if isinstance(c, str) and c.strip()
                    ]
                    preflight_commands_present, preflight_meta = probe_commands_in_container(
                        command_prefix=command_prefix,
                        commands=effective_probe_commands,
                    )
                else:
                    preflight_commands_present, preflight_meta = _probe_commands_local(
                        probe_commands,
                        workspace_dir=acquired.workspace_dir,
                        env_overrides=agent_env_overrides,
                    )
            except Exception as e:  # noqa: BLE001
                preflight_commands_present = {}
                preflight_meta = {"error": str(e)}

            preflight_workspace_snapshot = _snapshot_workspace_root(acquired.workspace_dir)

            shell_status = "unknown"
            shell_reason = ""
            allowed_tools: list[str] | None = None
            preflight_external_wait: dict[str, Any] | None = None
            preflight_external_wait_message = ""
            if request.agent == "claude":
                raw_allowed = claude_policy.get("allowed_tools")
                if isinstance(raw_allowed, list):
                    allowed_tools = [x for x in raw_allowed if isinstance(x, str) and x.strip()]
                else:
                    allowed_tools = []
                shell_enabled = "Bash" in allowed_tools
                shell_status = "allowed" if shell_enabled else "blocked"
                shell_reason = (
                    "claude.allowed_tools includes Bash" if shell_enabled else "Bash not enabled"
                )
            elif request.agent == "gemini":
                raw_allowed = gemini_policy.get("allowed_tools")
                if isinstance(raw_allowed, list):
                    allowed_tools = [x for x in raw_allowed if isinstance(x, str) and x.strip()]
                else:
                    allowed_tools = []
                shell_enabled = "run_shell_command" in allowed_tools
                effective_gemini_sandbox = _effective_gemini_cli_sandbox(
                    policy_value=gemini_policy.get("sandbox", True),
                    has_outer_sandbox=sandbox is not None,
                )
                shell_available = (sandbox is not None) or effective_gemini_sandbox
                if shell_enabled and not shell_available:
                    shell_status = "blocked"
                    shell_reason = _gemini_shell_unavailable_reason(
                        policy_value=gemini_policy.get("sandbox", True),
                        has_outer_sandbox=(sandbox is not None),
                    )
                else:
                    shell_status = "allowed" if shell_enabled else "blocked"
                    shell_reason = (
                        "gemini.allowed_tools includes run_shell_command"
                        if shell_enabled
                        else "run_shell_command not enabled"
                    )
            else:
                shell_status, shell_reason, allowed_tools = _infer_shell_policy_status(
                    agent=request.agent,
                    codex_policy=codex_policy,
                    claude_policy=claude_policy,
                    gemini_policy=gemini_policy,
                    has_outer_sandbox=(sandbox is not None),
                )

            if (
                bool(resolved_inputs.mission.requires_shell)
                and str(shell_status).strip().lower() != "blocked"
                and not (
                    isinstance(preflight_meta.get("error"), str)
                    and preflight_meta.get("error", "").strip()
                )
            ):
                probe_dir = run_dir / "agent_shell_probe"
                codex_probe_last_message_for_agent = (
                    _agent_path_for_staged_file(
                        probe_dir / "agent_last_message.txt",
                        run_dir=run_dir,
                        run_dir_mount=backend.run_dir_mount,
                    )
                    if request.agent == "codex" and backend.run_dir_mount
                    else None
                )
                codex_probe_overrides = (
                    controlled_codex_activation_overrides
                    if request.agent == "codex"
                    and codex_execpolicy_overlay is not None
                    and controlled_codex_activation_overrides is not None
                    else (
                        build_codex_shell_probe_config_overrides(codex_overrides)
                        if request.agent == "codex"
                        else codex_overrides
                    )
                )
                codex_probe_commands = (
                    list(controlled_codex_probe_commands)
                    if request.agent == "codex" and codex_execpolicy_overlay is not None
                    else []
                )
                probe_workspace_before = (
                    capture_probe_workspace_state(acquired.workspace_dir)
                    if request.agent == "codex" and codex_execpolicy_overlay is not None
                    else None
                )
                agent_shell_probe = probe_agent_shell_launch(
                    agent=request.agent,
                    workspace_dir=workspace_dir_for_agent,
                    artifacts_dir=probe_dir,
                    binary=str(
                        {
                            "codex": codex_binary,
                            "claude": claude_binary,
                            "gemini": gemini_binary,
                        }.get(request.agent, gemini_binary)
                    ),
                    model=effective_model,
                    command_prefix=command_prefix,
                    env_overrides=(
                        gemini_env_overrides if request.agent == "gemini" else agent_env_overrides
                    ),
                    codex_sandbox=codex_sandbox_mode,
                    codex_ask_for_approval=codex_ask_for_approval,
                    codex_subcommand=str(codex_subcommand),
                    codex_config_overrides=codex_probe_overrides,
                    codex_ignore_user_config=True,
                    codex_ignore_rules=(codex_execpolicy_overlay is None or os.name == "nt"),
                    codex_agent_last_message_path=codex_probe_last_message_for_agent,
                    codex_required_commands=codex_probe_commands,
                    codex_required_command_outputs={
                        "git rev-parse --is-inside-work-tree": "true",
                        "python --version": "Python ",
                    },
                    claude_output_format=str(claude_output_format),
                    claude_allowed_tools=claude_allowed_tools,
                    claude_permission_mode=claude_permission_mode,
                    gemini_output_format=str(gemini_output_format),
                    gemini_sandbox=gemini_sandbox_enabled,
                    gemini_approval_mode=gemini_approval_mode,
                    gemini_allowed_tools=gemini_allowed_tools,
                    gemini_include_directories=(
                        _gemini_include_directories_for_workspace(
                            workspace_dir=acquired.workspace_dir
                        )
                        if request.agent == "gemini"
                        else []
                    ),
                )
                agent_shell_probe_payload = agent_shell_probe.to_dict()
                if request.agent == "codex":
                    preflight_external_wait_message = "\n".join(
                        str(agent_shell_probe_payload.get(key) or "").strip()
                        for key in (
                            "stdout_excerpt",
                            "stderr_excerpt",
                            "last_message_excerpt",
                            "reason",
                        )
                        if str(agent_shell_probe_payload.get(key) or "").strip()
                    )
                    preflight_external_wait = _codex_subscription_external_wait(
                        preflight_external_wait_message
                    )
                    if preflight_external_wait is not None:
                        agent_shell_probe_payload["external_wait"] = dict(preflight_external_wait)
                        preflight_meta["external_wait"] = dict(preflight_external_wait)
                if probe_workspace_before is not None:
                    probe_workspace_after = capture_probe_workspace_state(acquired.workspace_dir)
                    workspace_unchanged = probe_workspace_after == probe_workspace_before
                    agent_shell_probe_payload["workspace_state_before"] = probe_workspace_before
                    agent_shell_probe_payload["workspace_state_after"] = probe_workspace_after
                    agent_shell_probe_payload["workspace_unchanged"] = workspace_unchanged
                    if not workspace_unchanged:
                        agent_shell_probe_payload["ok"] = False
                        agent_shell_probe_payload["reason"] = (
                            "Agent shell probe changed the acquired workspace before "
                            "mission execution."
                        )
                preflight_meta["agent_shell_probe"] = agent_shell_probe_payload
                if codex_execpolicy_overlay is not None:
                    codex_execpolicy_overlay.record_activation_probe(agent_shell_probe_payload)

            host_os = _runner_host_os()
            shell_probe_result = _shell_probe_result_from_preflight_meta(preflight_meta)
            shell_capability = _resolve_shell_capability(
                agent=request.agent,
                operating_system=host_os,
                backend=str(request.exec_backend or "local"),
                sandbox_mode=codex_sandbox_mode if request.agent == "codex" else None,
                policy_status=shell_status,
                policy_reason=shell_reason,
                allowed_tools=allowed_tools,
                probe_result=shell_probe_result,
            )
            shell_capability_summary = shell_capability.to_dict()

            probe_details = preflight_meta.get("command_probe_details")
            probe_details_dict = probe_details if isinstance(probe_details, dict) else {}
            python_interpreter_meta = preflight_meta.get("python_interpreter")
            python_interpreter_summary = (
                python_interpreter_meta if isinstance(python_interpreter_meta, dict) else None
            )
            python_capability = _validate_python_capability(
                workspace_dir=acquired.workspace_dir,
                verification_commands=effective_verification_commands,
                command_prefix=command_prefix,
                cwd=acquired.workspace_dir,
                env_overrides=agent_env_overrides,
            )
            python_runtime = python_capability["runtime_selection"]
            python_runtime_summary = python_capability["runtime_summary"]
            python_context_probe = python_capability["context_probe"]
            python_validation = python_capability["validation"]
            python_validation_required = bool(python_validation.get("required", False))
            python_validation_enabled = bool(python_validation.get("enabled", False))
            python_validation_reason_code = (
                python_validation.get("reason_code")
                if isinstance(python_validation.get("reason_code"), str)
                else None
            )
            python_validation_reason_type = (
                python_validation.get("reason_type")
                if isinstance(python_validation.get("reason_type"), str)
                else None
            )
            python_validation_reason = (
                python_validation.get("reason")
                if isinstance(python_validation.get("reason"), str)
                else None
            )
            validated_python_executable_for_execution = (
                python_validation.get("validated_python_executable")
                if isinstance(python_validation.get("validated_python_executable"), str)
                else None
            )
            pdm_validation_required = verification_commands_need_pdm(
                effective_verification_commands
            )
            pytest_validation_required = verification_commands_need_pytest(
                effective_verification_commands
            )
            pip_probe: dict[str, Any] | None = None
            if (
                python_validation_required
                and python_validation_enabled
                and not command_prefix
                and python_runtime.selected is not None
            ):
                pip_probe = probe_pip_module(
                    python_executable=python_runtime.selected.path,
                    cwd=acquired.workspace_dir,
                )
                if isinstance(pip_probe, dict):
                    reason_code = pip_probe.get("reason_code")
                    pip_probe["reason_type"] = _reason_type_for_code(
                        reason_code if isinstance(reason_code, str) else None
                    )
            pytest_probe: dict[str, Any] | None = None
            if (
                pytest_validation_required
                and python_validation_enabled
                and not command_prefix
                and python_runtime.selected is not None
            ):
                pytest_probe = probe_pytest_module(
                    python_executable=python_runtime.selected.path,
                    cwd=acquired.workspace_dir,
                )
                if isinstance(pytest_probe, dict):
                    reason_code = pytest_probe.get("reason_code")
                    pytest_probe["reason_type"] = _reason_type_for_code(
                        reason_code if isinstance(reason_code, str) else None
                    )

            command_diagnostics: dict[str, Any] = {}
            for cmd in effective_probe_commands:
                detail = probe_details_dict.get(cmd)
                detail_dict = detail if isinstance(detail, dict) else {}
                detail_present = detail_dict.get("present")
                detail_usable = detail_dict.get("usable")

                usable = preflight_commands_present.get(cmd)
                present: bool | None = (
                    detail_present
                    if isinstance(detail_present, bool)
                    else (usable if isinstance(usable, bool) else None)
                )
                usable_effective: bool | None = (
                    detail_usable
                    if isinstance(detail_usable, bool)
                    else (usable if isinstance(usable, bool) else None)
                )

                reason_code = detail_dict.get("reason_code")
                reason_code_s = reason_code if isinstance(reason_code, str) else None
                reason = detail_dict.get("reason")
                reason_s = reason if isinstance(reason, str) else None
                resolved_path = detail_dict.get("resolved_path")
                resolved_path_s = resolved_path if isinstance(resolved_path, str) else None

                status = "unknown"
                if present is False:
                    status = "missing"
                elif usable_effective is True:
                    status = "present"
                elif present is True and usable_effective is False:
                    status = "unusable"

                if shell_status == "blocked" and status == "present":
                    status = "blocked_by_policy"
                remediation: str | None = None
                if status in {"missing", "unusable"}:
                    if cmd in {"python", "python3", "py"} and reason_code_s in {
                        "access_denied",
                        "launch_failed",
                        "timeout",
                    }:
                        remediation = (
                            "Python execution appears blocked or broken. Install a full CPython "
                            "interpreter (python.org or winget), disable Windows App Execution "
                            "Alias shims (python.exe/python3.exe), or switch to --exec-backend "
                            "docker."
                        )
                    elif reason_code_s == "windowsapps_alias":
                        remediation = (
                            "Install and expose a full CPython interpreter (not WindowsApps "
                            "alias), then retry."
                        )
                    elif reason_code_s == "missing_stdlib":
                        remediation = (
                            "Selected Python runtime is incomplete (missing stdlib). "
                            "Install a full interpreter and retry."
                        )
                    elif cmd == "pdm" and status == "unusable":
                        remediation = (
                            "PDM is present but not usable. Try reinstalling it into your Python "
                            "(python -m pip install -U pdm), or switch to --exec-backend docker."
                        )
                    else:
                        remediation = (
                            f"Install `{cmd}` in the selected execution backend, "
                            "or switch --exec-backend."
                        )
                elif status == "blocked_by_policy":
                    remediation = (
                        "Enable shell commands in policy (recommended: --policy inspect), "
                        "or switch agent/policy."
                    )
                command_diagnostics[cmd] = {
                    "present": present,
                    "usable": usable_effective,
                    "status": status,
                    "resolved_path": resolved_path_s,
                    "reason_code": reason_code_s,
                    "reason_type": _reason_type_for_code(reason_code_s),
                    "reason": reason_s,
                    "remediation": remediation,
                }

            _align_python_command_diagnostics(
                command_diagnostics=command_diagnostics,
                python_runtime_summary=python_runtime_summary,
                python_context_probe=python_context_probe,
                python_validation_required=python_validation_required,
                prefer_context_selection=bool(command_prefix),
                validated_python_executable=validated_python_executable_for_execution,
            )

            python_validation_summary = {
                "required": python_validation_required,
                "enabled": python_validation_enabled,
                "reason_code": python_validation_reason_code,
                "reason_type": python_validation_reason_type,
                "reason": python_validation_reason,
                "validated_python_executable": validated_python_executable_for_execution,
            }
            python_toolchain_summary = {
                "commands": {
                    key: dict(value)
                    for key, value in command_diagnostics.items()
                    if isinstance(key, str) and isinstance(value, dict)
                },
                "runtime": python_runtime_summary,
                "validation": dict(python_validation_summary),
                "context_probe": (
                    dict(python_context_probe) if isinstance(python_context_probe, dict) else None
                ),
                "modules": {
                    "pip": dict(pip_probe) if isinstance(pip_probe, dict) else None,
                    "pytest": dict(pytest_probe) if isinstance(pytest_probe, dict) else None,
                },
            }

            required_agent_binary_present = (
                preflight_commands_present.get(required_agent_binary)
                if required_agent_binary is not None
                else None
            )
            agent_cli_version_probe: dict[str, Any] | None = None
            agent_cli_version_probes: dict[str, dict[str, Any]] = {}
            if required_agent_binary is not None and required_agent_binary_present is True:
                version_timeout_seconds = _agent_version_probe_timeout_seconds(request.agent)
                agent_cli_version_probe = _probe_agent_cli_version(
                    binary=required_agent_binary,
                    command_prefix=command_prefix,
                    env_overrides=agent_env_overrides,
                    timeout_seconds=version_timeout_seconds,
                )
                preflight_meta["agent_cli_version_probe"] = dict(agent_cli_version_probe)
                selected_agent_norm = str(request.agent).strip().lower()
                if selected_agent_norm:
                    agent_cli_version_probes[selected_agent_norm] = dict(agent_cli_version_probe)

            for delegation_agent, delegation_binary in delegation_agent_binaries.items():
                if delegation_agent in agent_cli_version_probes:
                    continue
                if preflight_commands_present.get(delegation_binary) is not True:
                    continue
                delegation_probe = _probe_agent_cli_version(
                    binary=delegation_binary,
                    command_prefix=command_prefix,
                    env_overrides=agent_env_overrides,
                    timeout_seconds=_agent_version_probe_timeout_seconds(delegation_agent),
                )
                agent_cli_version_probes[delegation_agent] = delegation_probe
            if agent_cli_version_probes:
                preflight_meta["agent_cli_version_probes"] = {
                    agent: dict(probe) for agent, probe in agent_cli_version_probes.items()
                }

            delegation_capabilities_summary = _resolve_delegation_capabilities(
                agents_cfg=agents_cfg_for_capabilities,
                policy_cfg=policy_cfg,
                cli_version_probes=agent_cli_version_probes,
            )
            delegation_capability_summary = _selected_delegation_capability(
                agent=request.agent,
                delegation_capabilities=delegation_capabilities_summary,
            )

            python_toolchain_capability_summary = _build_python_toolchain_capability_summary(
                python_validation_required=python_validation_required,
                python_validation_enabled=python_validation_enabled,
                python_validation_reason_code=python_validation_reason_code,
                python_validation_reason_type=python_validation_reason_type,
                python_validation_reason=python_validation_reason,
                python_context_probe=python_context_probe,
                validated_python_executable=validated_python_executable_for_execution,
                pdm_required=pdm_validation_required,
            )
            _write_json(
                run_dir / "preflight.json",
                {
                    "commands": preflight_commands_present,
                    "command_diagnostics": command_diagnostics,
                    "required_commands": preflight_required_commands,
                    "meta": preflight_meta,
                    "warnings": preflight_warnings,
                    "probe_commands": effective_probe_commands,
                    "required_agent_binary": required_agent_binary,
                    "required_agent_binary_present": required_agent_binary_present,
                    "python_interpreter": python_interpreter_summary,
                    "python_toolchain": python_toolchain_summary,
                    "python_runtime": python_runtime_summary,
                    "python_context_probe": python_context_probe,
                    "python_validation": python_validation_summary,
                    "python_toolchain_capability": python_toolchain_capability_summary,
                    "pip_probe": pip_probe,
                    "pytest_probe": pytest_probe,
                    "capabilities": {
                        "shell_commands": {
                            "status": shell_status,
                            "reason": shell_reason,
                            "allowed_tools": allowed_tools,
                            "canonical": shell_capability_summary,
                        },
                        "delegation": delegation_capability_summary,
                        "delegation_by_agent": delegation_capabilities_summary,
                        "edits": {"allowed": bool(allow_edits)},
                    },
                    "shell_capability": shell_capability_summary,
                    "delegation_capability": delegation_capability_summary,
                    "delegation_capabilities": delegation_capabilities_summary,
                    "mission_requirements": {
                        "mission_id": effective_spec.mission_id,
                        "requires_shell": bool(resolved_inputs.mission.requires_shell),
                        "requires_edits": bool(resolved_inputs.mission.requires_edits),
                    },
                    "workspace_root_snapshot": preflight_workspace_snapshot,
                },
            )

            if preflight_external_wait is not None:
                message = (
                    "Codex ChatGPT subscription usage is exhausted; mission dispatch is parked "
                    "until the provider reset. The signed-in subscription route remains required."
                )
                hint = (
                    "Resume this retained workflow after the recorded reset using the same "
                    "ChatGPT login; do not switch to API billing."
                )
                _write_json(
                    run_dir / "error.json",
                    {
                        "type": "AgentExternalWait",
                        "subtype": "provider_subscription_usage_limit",
                        "code": "codex_chatgpt_subscription_usage_limit",
                        "agent": request.agent,
                        "provider": "codex",
                        "phase": "agent_shell_probe",
                        "message": message,
                        "hint": hint,
                        "provider_message": preflight_external_wait_message,
                        "route": "chatgpt_subscription",
                        "api_fallback_allowed": False,
                        "external_wait": preflight_external_wait,
                    },
                )
                return RunResult(
                    run_dir=run_dir,
                    exit_code=1,
                    report_validation_errors=[
                        message,
                        "code=codex_chatgpt_subscription_usage_limit",
                        f"hint={hint}",
                    ],
                )

            if python_validation_required and not python_validation_enabled:
                probe_remediation = (
                    python_context_probe.get("remediation")
                    if isinstance(python_context_probe, dict)
                    and isinstance(python_context_probe.get("remediation"), str)
                    else None
                )
                hint = probe_remediation or (
                    "Install a full CPython interpreter and ensure it is executable "
                    "in the agent execution context (not a WindowsApps alias), then retry."
                )
                blocked_tool_hint = (
                    " (includes pdm commands which depend on a usable Python runtime)"
                    if pdm_validation_required
                    else ""
                )
                _write_json(
                    run_dir / "error.json",
                    {
                        "type": "AgentPreflightFailed",
                        "subtype": "python_unavailable",
                        "agent": request.agent,
                        "message": (
                            "Verification includes Python-dependent commands"
                            + blocked_tool_hint
                            + ", but Python context preflight failed in the effective agent "
                            "execution path."
                        ),
                        "hint": hint,
                        "preflight": {
                            "python_runtime": python_runtime_summary,
                            "python_interpreter": python_interpreter_summary,
                            "python_context_probe": python_context_probe,
                            "python_validation": {
                                "required": python_validation_required,
                                "enabled": python_validation_enabled,
                                "reason_code": python_validation_reason_code,
                                "reason_type": python_validation_reason_type,
                                "reason": python_validation_reason,
                                "validated_python_executable": (
                                    validated_python_executable_for_execution
                                ),
                            },
                            "python_toolchain_capability": python_toolchain_capability_summary,
                            "command_diagnostics": command_diagnostics,
                        },
                    },
                )
                return RunResult(
                    run_dir=run_dir,
                    exit_code=1,
                    report_validation_errors=[],
                )

            if pytest_validation_required and (
                not verification_commands_may_provision_pytest(effective_verification_commands)
                and not bool(pytest_probe and pytest_probe.get("passed", False))
            ):
                remediation = (
                    pytest_probe.get("remediation") if isinstance(pytest_probe, dict) else None
                )
                _write_json(
                    run_dir / "error.json",
                    {
                        "type": "AgentPreflightFailed",
                        "subtype": "pytest_unavailable",
                        "agent": request.agent,
                        "message": (
                            "Verification is configured to run pytest, but "
                            "`python -m pytest --version` failed."
                        ),
                        "hint": remediation
                        or (
                            "Install pytest into the selected interpreter, or ensure the "
                            "workspace `.venv` exists and contains pytest."
                        ),
                        "preflight": {
                            "python_runtime": python_runtime_summary,
                            "python_context_probe": python_context_probe,
                            "python_validation": {
                                "required": python_validation_required,
                                "enabled": python_validation_enabled,
                                "reason_code": python_validation_reason_code,
                                "reason_type": python_validation_reason_type,
                                "reason": python_validation_reason,
                                "validated_python_executable": (
                                    validated_python_executable_for_execution
                                ),
                            },
                            "python_toolchain_capability": python_toolchain_capability_summary,
                            "pytest_probe": pytest_probe,
                            "command_diagnostics": command_diagnostics,
                        },
                    },
                )
                return RunResult(
                    run_dir=run_dir,
                    exit_code=1,
                    report_validation_errors=[],
                )

            if (
                bool(resolved_inputs.mission.requires_shell)
                and shell_capability_summary.get("state") != "available"
            ):
                suggested_policy = (
                    "write" if bool(resolved_inputs.mission.requires_edits) else "inspect"
                )
                suggested_exec_backend = str(request.exec_backend or "local").strip() or "local"

                gemini_local_sandbox_available = True
                if request.agent == "gemini" and suggested_exec_backend == "local":
                    gemini_local_sandbox_available = _effective_gemini_cli_sandbox(
                        policy_value=gemini_policy.get("sandbox", True),
                        has_outer_sandbox=False,
                    )
                    if not gemini_local_sandbox_available:
                        suggested_exec_backend = "docker"

                blocked_by_backend = (
                    request.agent == "gemini"
                    and isinstance(allowed_tools, list)
                    and "run_shell_command" in allowed_tools
                    and not gemini_local_sandbox_available
                )

                if blocked_by_backend:
                    message = (
                        f"Mission '{effective_spec.mission_id}' requires shell commands, but "
                        "Gemini shell execution is unavailable under `--exec-backend local` "
                        "(Gemini sandbox disabled/unavailable)."
                    )
                    hint = "Rerun with `--exec-backend docker` (recommended)."
                    if bool(resolved_inputs.mission.requires_edits) and not allow_edits:
                        suggested_policy = "write"
                    else:
                        suggested_policy = request.policy
                else:
                    message = (
                        f"Mission '{effective_spec.mission_id}' requires shell commands, but "
                        f"policy '{request.policy}' for agent '{request.agent}' "
                        "blocks shell commands."
                    )
                    hint = (
                        "Use `--policy write` (allows edits + shell)."
                        if suggested_policy == "write"
                        else "Use `--policy inspect` (read-only + shell)."
                    )
                    if suggested_exec_backend == "docker" and str(request.exec_backend) == "local":
                        hint = f"{hint} Also add `--exec-backend docker`."
                    if shell_capability_summary.get("state") == "unprobed":
                        message = (
                            f"Mission '{effective_spec.mission_id}' requires shell commands, but "
                            f"canonical shell capability for agent '{request.agent}' is unprobed "
                            "in the effective execution path."
                        )
                        hint = (
                            "Use a backend/policy combination with canonical shell capability "
                            "available (recommended: `--exec-backend docker` for local Windows "
                            "Codex runs)."
                        )
                        if suggested_exec_backend == "local":
                            suggested_exec_backend = "docker"
                suggested_command_parts: list[str] = [
                    "python",
                    "-m",
                    "usertest.cli",
                    "run",
                    "--repo-root",
                    ".",
                    "--repo",
                    json.dumps(request.repo, ensure_ascii=False),
                    "--agent",
                    request.agent,
                    "--policy",
                    suggested_policy,
                ]
                if request.ref:
                    ref_json = json.dumps(request.ref, ensure_ascii=False)
                    suggested_command_parts.extend(["--ref", ref_json])
                if effective_spec.persona_id:
                    suggested_command_parts.extend(["--persona-id", effective_spec.persona_id])
                if effective_spec.mission_id:
                    suggested_command_parts.extend(["--mission-id", effective_spec.mission_id])
                if suggested_exec_backend != "local":
                    suggested_command_parts.extend(["--exec-backend", suggested_exec_backend])
                suggested_command = " ".join(suggested_command_parts)
                _write_json(
                    run_dir / "error.json",
                    {
                        "type": "AgentPreflightFailed",
                        "subtype": "mission_requires_shell",
                        "code": "mission_requires_shell",
                        "agent": request.agent,
                        "policy": request.policy,
                        "mission_id": effective_spec.mission_id,
                        "capability": "shell_commands",
                        "message": message,
                        "hint": hint,
                        "suggested_policy": suggested_policy,
                        "suggested_command": suggested_command,
                        "preflight": {
                            "capabilities": {
                                "shell_commands": {
                                    "status": shell_status,
                                    "reason": shell_reason,
                                    "allowed_tools": allowed_tools,
                                    "canonical": shell_capability_summary,
                                }
                            },
                            "shell_capability": shell_capability_summary,
                        },
                    },
                )
                _maybe_write_shell_capability_block_report_artifacts(
                    run_dir=run_dir,
                    target_ref=target_ref,
                    schema_dict=effective_spec.report_schema_dict,
                    mission_id=effective_spec.mission_id,
                    message=message,
                    hint=hint,
                    shell_capability=shell_capability_summary,
                )
                return RunResult(
                    run_dir=run_dir,
                    exit_code=1,
                    report_validation_errors=[
                        message,
                        "code=mission_requires_shell",
                        f"hint={hint}",
                        f"suggested_command={suggested_command}",
                    ],
                )

            if bool(resolved_inputs.mission.requires_edits) and not allow_edits:
                message = (
                    f"Mission '{effective_spec.mission_id}' requires edits, but policy "
                    f"'{request.policy}' for agent '{request.agent}' has allow_edits=false."
                )
                hint = "Use --policy write (or update configs/policies.yaml to allow edits)."
                _write_json(
                    run_dir / "error.json",
                    {
                        "type": "AgentPreflightFailed",
                        "subtype": "mission_requires_edits",
                        "code": "mission_requires_edits",
                        "agent": request.agent,
                        "policy": request.policy,
                        "mission_id": effective_spec.mission_id,
                        "capability": "edits",
                        "message": message,
                        "hint": hint,
                        "preflight": {"capabilities": {"edits": {"allowed": bool(allow_edits)}}},
                    },
                )
                return RunResult(
                    run_dir=run_dir,
                    exit_code=1,
                    report_validation_errors=[
                        message,
                        "code=mission_requires_edits",
                        f"hint={hint}",
                    ],
                )

            if request.policy in {"inspect", "write"} and shell_status == "blocked":
                hint: str | None = None
                suggested_command: str | None = None
                if (
                    request.agent == "gemini"
                    and str(request.exec_backend) == "local"
                    and isinstance(allowed_tools, list)
                    and "run_shell_command" in allowed_tools
                ):
                    message = (
                        f"Policy '{request.policy}' enables Gemini shell commands, but shell "
                        "execution is unavailable under `--exec-backend local` "
                        "(Gemini sandbox disabled/unavailable)."
                    )
                    hint = "Rerun with `--exec-backend docker` (recommended)."
                    suggested_command_parts: list[str] = [
                        "python",
                        "-m",
                        "usertest.cli",
                        "run",
                        "--repo-root",
                        ".",
                        "--repo",
                        json.dumps(request.repo, ensure_ascii=False),
                        "--agent",
                        request.agent,
                        "--policy",
                        request.policy,
                        "--exec-backend",
                        "docker",
                    ]
                    if request.ref:
                        suggested_command_parts.extend(
                            ["--ref", json.dumps(request.ref, ensure_ascii=False)]
                        )
                    if effective_spec.persona_id:
                        suggested_command_parts.extend(["--persona-id", effective_spec.persona_id])
                    if effective_spec.mission_id:
                        suggested_command_parts.extend(["--mission-id", effective_spec.mission_id])
                    suggested_command = " ".join(suggested_command_parts)
                else:
                    message = (
                        f"Policy '{request.policy}' for agent '{request.agent}' blocks shell "
                        "commands. Fix configs/policies.yaml or pick a policy that enables shell "
                        "command execution."
                    )
                _write_json(
                    run_dir / "error.json",
                    {
                        "type": "AgentPreflightFailed",
                        "subtype": "policy_block",
                        "agent": request.agent,
                        "capability": "shell_commands",
                        "message": message,
                        **({"hint": hint} if hint else {}),
                        **({"suggested_command": suggested_command} if suggested_command else {}),
                        "preflight": {
                            "capabilities": {
                                "shell_commands": {
                                    "status": shell_status,
                                    "reason": shell_reason,
                                    "allowed_tools": allowed_tools,
                                }
                            }
                        },
                    },
                )
                return RunResult(
                    run_dir=run_dir,
                    exit_code=1,
                    report_validation_errors=[message],
                )

            if (
                required_agent_binary is not None
                and preflight_commands_present
                and preflight_commands_present.get(required_agent_binary) is False
            ):
                exec_backend = str(getattr(request, "exec_backend", "local") or "local").strip()
                hints = _build_binary_missing_hints(
                    agent=request.agent,
                    required_binary=required_agent_binary,
                    exec_backend=exec_backend,
                    agent_cfg=agent_cfg_dict,
                    command_prefix=command_prefix,
                )
                message = (
                    f"Required agent binary '{required_agent_binary}' is missing for agent "
                    f"'{request.agent}' (exec_backend={exec_backend})."
                )

                suggested_command: str | None = None
                if exec_backend == "docker" and not bool(
                    getattr(request, "exec_rebuild_image", False)
                ):
                    suggested_command_parts: list[str] = [
                        "python",
                        "-m",
                        "usertest.cli",
                        "run",
                        "--repo-root",
                        ".",
                        "--repo",
                        json.dumps(request.repo, ensure_ascii=False),
                        "--agent",
                        request.agent,
                        "--policy",
                        request.policy,
                        "--exec-backend",
                        "docker",
                        "--exec-rebuild-image",
                    ]
                    if request.ref:
                        suggested_command_parts.extend(
                            ["--ref", json.dumps(request.ref, ensure_ascii=False)]
                        )
                    if effective_spec.persona_id:
                        suggested_command_parts.extend(["--persona-id", effective_spec.persona_id])
                    if effective_spec.mission_id:
                        suggested_command_parts.extend(["--mission-id", effective_spec.mission_id])
                    suggested_command = " ".join(suggested_command_parts)
                _write_json(
                    run_dir / "error.json",
                    {
                        "type": "AgentPreflightFailed",
                        "subtype": "binary_missing",
                        "code": "binary_missing",
                        "agent": request.agent,
                        "required_binary": required_agent_binary,
                        "exec_backend": exec_backend,
                        "message": message,
                        "hints": hints,
                        "suggested_command": suggested_command,
                        "preflight": {
                            "commands": preflight_commands_present,
                            "meta": preflight_meta,
                            "probe_commands": effective_probe_commands,
                        },
                    },
                )
                return RunResult(
                    run_dir=run_dir,
                    exit_code=1,
                    report_validation_errors=[
                        message,
                        "code=binary_missing",
                        *[f"{key}={value}" for key, value in hints.items() if value],
                        *(["suggested_command=" + suggested_command] if suggested_command else []),
                    ],
                )

            if (
                required_agent_binary is not None
                and preflight_commands_present
                and preflight_commands_present.get(required_agent_binary) is True
            ):
                exec_backend = str(getattr(request, "exec_backend", "local") or "local").strip()

                version_probe = agent_cli_version_probe
                if version_probe is None:
                    version_timeout_seconds = 2.5
                    # Gemini CLI is a Node-based binary and can exceed the default 2.5s budget,
                    # especially under `docker exec` where process startup overhead is higher.
                    if str(request.agent).strip().lower() == "gemini":
                        version_timeout_seconds = 8.0
                    version_probe = _probe_agent_cli_version(
                        binary=required_agent_binary,
                        command_prefix=command_prefix,
                        env_overrides=agent_env_overrides,
                        timeout_seconds=version_timeout_seconds,
                    )
                if not bool(version_probe.get("ok")):
                    message = (
                        f"Required agent binary '{required_agent_binary}' is present but failed "
                        "`--version` preflight probe."
                    )
                    hints = _build_binary_missing_hints(
                        agent=request.agent,
                        required_binary=required_agent_binary,
                        exec_backend=exec_backend,
                        agent_cfg=agent_cfg_dict,
                        command_prefix=command_prefix,
                    )
                    _write_json(
                        run_dir / "error.json",
                        {
                            "type": "AgentPreflightFailed",
                            "subtype": "binary_unusable",
                            "code": "binary_unusable",
                            "agent": request.agent,
                            "required_binary": required_agent_binary,
                            "exec_backend": exec_backend,
                            "message": message,
                            "hints": hints,
                            "probe": {"version": version_probe},
                        },
                    )
                    return RunResult(
                        run_dir=run_dir,
                        exit_code=1,
                        report_validation_errors=[
                            message,
                            "code=binary_unusable",
                            *[f"{key}={value}" for key, value in hints.items() if value],
                        ],
                    )

                exec_use_host_agent_login = bool(
                    getattr(request, "exec_use_host_agent_login", False)
                )
                exec_env_allowlist_raw = getattr(request, "exec_env", ())
                exec_env_allowlist = [
                    str(x) for x in exec_env_allowlist_raw if isinstance(x, str) and x.strip()
                ]
                if exec_backend == "docker":
                    auth_ok, auth_evidence = _agent_auth_present_docker(
                        agent=request.agent,
                        exec_use_host_agent_login=exec_use_host_agent_login,
                        exec_env_allowlist=exec_env_allowlist,
                    )
                else:
                    auth_ok, auth_evidence = _agent_auth_present_local(
                        agent=request.agent,
                        env_overrides=agent_env_overrides,
                    )

                if not auth_ok:
                    message = (
                        f"Agent authentication appears missing for agent '{request.agent}' "
                        f"(exec_backend={exec_backend})."
                    )
                    hints = _build_auth_missing_hints(
                        agent=request.agent,
                        exec_backend=exec_backend,
                        exec_use_host_agent_login=exec_use_host_agent_login,
                        required_binary=required_agent_binary,
                    )
                    _write_json(
                        run_dir / "error.json",
                        {
                            "type": "AgentPreflightFailed",
                            "subtype": "auth_missing",
                            "code": "auth_missing",
                            "agent": request.agent,
                            "required_binary": required_agent_binary,
                            "exec_backend": exec_backend,
                            "message": message,
                            "hints": hints,
                            "evidence": auth_evidence,
                        },
                    )
                    return RunResult(
                        run_dir=run_dir,
                        exit_code=1,
                        report_validation_errors=[
                            message,
                            "code=auth_missing",
                            *[f"{key}={value}" for key, value in hints.items() if value],
                            f"evidence={auth_evidence}",
                        ],
                    )

            if preflight_required_commands:
                failures: dict[str, Any] = {}
                for cmd in preflight_required_commands:
                    diag = command_diagnostics.get(cmd)
                    status = diag.get("status") if isinstance(diag, dict) else None
                    if status != "present":
                        failures[cmd] = diag
                if failures:
                    failing_list = ", ".join(sorted(failures))
                    message = (
                        "Preflight failed: required command(s) unavailable: "
                        f"{failing_list}. See preflight.json for details."
                    )
                    _write_json(
                        run_dir / "error.json",
                        {
                            "type": "AgentPreflightFailed",
                            "subtype": "required_command_unavailable",
                            "agent": request.agent,
                            "message": message,
                            "required_commands": preflight_required_commands,
                            "failures": failures,
                        },
                    )
                    return RunResult(
                        run_dir=run_dir,
                        exit_code=1,
                        report_validation_errors=[message],
                    )

            if sandbox is not None:
                capture_dns_snapshot(
                    command_prefix=command_prefix,
                    artifacts_dir=run_dir / "sandbox",
                )

            host_os = _runner_host_os()
            execution_shell = _execution_shell_family(
                exec_backend=request.exec_backend, host_os=host_os
            )

            verification_commands = list(effective_verification_commands)
            verification_timeout_seconds = request.verification_timeout_seconds
            if (
                verification_timeout_seconds is not None
                and float(verification_timeout_seconds) <= 0.0
            ):
                verification_timeout_seconds = None
            verification_reuse_mode = (
                str(getattr(request, "verification_reuse_mode", "auto") or "auto").strip().lower()
            )
            if verification_reuse_mode not in {"auto", "off"}:
                raise ValueError(
                    "verification_reuse_mode must be one of {'auto', 'off'}; "
                    f"got {request.verification_reuse_mode!r}"
                )
            verification_broker_command: str | None = None
            verification_broker_contract: VerificationBrokerContract | None = None
            verification_timing_profile: dict[str, Any] | None = None
            verification_timing_profile_path_for_agent: str | None = None
            verification_result_path_for_agent: str | None = None
            effective_verification_timeout_seconds = verification_timeout_seconds
            if verification_commands and verification_reuse_mode == "auto":
                verification_broker_contract = resolve_verification_broker_contract(
                    command_prefix=command_prefix,
                    exec_backend=request.exec_backend,
                    validated_python_executable=validated_python_executable_for_execution,
                    verification_timeout_seconds=verification_timeout_seconds,
                    verification_command_count=len(verification_commands),
                    is_windows=_is_windows(),
                )
                effective_verification_timeout_seconds = (
                    verification_broker_contract.effective_timeout_seconds
                )
                broker_launcher, broker_launcher_probe = _probe_verification_broker_launcher(
                    command_prefix=command_prefix,
                    sandbox=sandbox,
                    contract=verification_broker_contract,
                )
                if not bool(broker_launcher_probe.get("usable", False)):
                    launcher_name = broker_launcher.executable
                    resolved_path = broker_launcher_probe.get("resolved_path")
                    resolved_path_s = (
                        resolved_path.strip()
                        if isinstance(resolved_path, str) and resolved_path.strip()
                        else None
                    )
                    reason = broker_launcher_probe.get("reason")
                    reason_s = (
                        reason.strip()
                        if isinstance(reason, str) and reason.strip()
                        else "launcher did not pass the runtime availability probe."
                    )
                    reason_code = broker_launcher_probe.get("reason_code")
                    reason_code_s = (
                        reason_code.strip()
                        if isinstance(reason_code, str) and reason_code.strip()
                        else None
                    )
                    failed_dependency = broker_launcher_probe.get("failed_dependency")
                    failed_dependency_s = (
                        failed_dependency.strip()
                        if isinstance(failed_dependency, str) and failed_dependency.strip()
                        else None
                    )
                    message = (
                        "Final verification broker launcher is unavailable in the current "
                        f"runtime: launcher=`{launcher_name}`; {reason_s}"
                    )
                    if resolved_path_s is not None:
                        message += f" Resolved path: `{resolved_path_s}`."
                    _write_json(
                        run_dir / "error.json",
                        {
                            "type": "VerificationBrokerLauncherUnavailable",
                            "subtype": "verification_broker_launcher_unavailable",
                            "message": message,
                            "launcher": launcher_name,
                            "failed_dependency": failed_dependency_s,
                            "resolved_path": resolved_path_s,
                            "reason_code": reason_code_s,
                            "reason": reason_s,
                            "runtime_dependencies": broker_launcher_probe.get(
                                "runtime_dependencies"
                            ),
                            "exec_backend": request.exec_backend,
                            "verification_reuse_mode": verification_reuse_mode,
                        },
                    )
                    return RunResult(
                        run_dir=run_dir,
                        exit_code=1,
                        report_validation_errors=[message],
                    )
                verification_broker_command = _verification_broker_client_command(
                    run_dir=run_dir,
                    run_dir_mount=backend.run_dir_mount,
                    workspace_dir=acquired.workspace_dir,
                    contract=verification_broker_contract,
                )
                verification_timing_profile = build_verification_timing_profile(
                    runs_dir=config.runs_dir,
                    current_run_dir=run_dir,
                    broker_timeout_guard_seconds=max(
                        float(verification_broker_contract.effective_timeout_seconds),
                        default_verification_hang_guard_seconds(),
                    ),
                    generated_utc=_utc_now_z(),
                )
                verification_timing_profile_path = run_dir / "verification_timing_profile.json"
                _write_json(verification_timing_profile_path, verification_timing_profile)
                verification_timing_profile_path_for_agent = _agent_path_for_staged_file(
                    verification_timing_profile_path,
                    run_dir=run_dir,
                    run_dir_mount=backend.run_dir_mount,
                )
                verification_result_path_for_agent = _agent_path_for_staged_file(
                    run_dir / "verification.json",
                    run_dir=run_dir,
                    run_dir_mount=backend.run_dir_mount,
                )

            policy_json = json.dumps(
                {
                    "agent": request.agent,
                    "policy": request.policy,
                    "allow_edits": allow_edits,
                    "exec_backend": request.exec_backend,
                    "exec_docker_profile": request.exec_docker_profile,
                    "exec_network": request.exec_network,
                    "exec_cache": request.exec_cache,
                    "exec_maintenance_venv_cache": request.exec_maintenance_venv_cache,
                    "exec_maintenance_image_metadata_path": (
                        str(request.exec_maintenance_image_metadata_path)
                        if request.exec_maintenance_image_metadata_path is not None
                        else None
                    ),
                    "codex": {
                        "sandbox": codex_sandbox_mode,
                        "ask_for_approval": codex_ask_for_approval,
                    }
                    if request.agent == "codex"
                    else {},
                },
                indent=2,
                ensure_ascii=False,
            )

            environment_json = json.dumps(
                {
                    "runner_host_os": host_os,
                    "runner_host_python": platform.python_version(),
                    "workspace": {
                        "path": str(workspace_dir_for_agent),
                        "mount": workspace_mount,
                        "provenance": acquired.mode,
                    },
                    "execution_backend": {
                        "backend": request.exec_backend,
                        "docker_profile": request.exec_docker_profile,
                        "shell": execution_shell,
                        "network": request.exec_network,
                        "cache": request.exec_cache,
                        "maintenance_venv_cache": request.exec_maintenance_venv_cache,
                        "maintenance_image_metadata_path": (
                            str(request.exec_maintenance_image_metadata_path)
                            if request.exec_maintenance_image_metadata_path is not None
                            else None
                        ),
                        "container_image": getattr(sandbox, "image_tag", None)
                        if sandbox is not None
                        else None,
                    },
                    "verification_gate": {
                        "configured": bool(verification_commands),
                        "mode": verification_reuse_mode,
                        "runner_owned_blocking_wait": bool(
                            verification_commands and verification_reuse_mode == "auto"
                        ),
                        "commands": (
                            [] if verification_reuse_mode == "auto" else verification_commands
                        ),
                        "final_handoff_command": (
                            None
                            if verification_reuse_mode == "auto"
                            else verification_broker_command
                        ),
                        "timeout_seconds": effective_verification_timeout_seconds,
                        "timing_profile_path": verification_timing_profile_path_for_agent,
                        "timing_profile": {
                            "run_count": verification_timing_profile.get("run_count"),
                            "command_count": verification_timing_profile.get("command_count"),
                            "recommendations": verification_timing_profile.get("recommendations"),
                        }
                        if isinstance(verification_timing_profile, dict)
                        else None,
                    },
                    "preflight": {
                        "commands": preflight_commands_present,
                        "command_diagnostics": command_diagnostics,
                        "python_interpreter": python_interpreter_summary,
                        "python_runtime": python_runtime_summary,
                        "python_context_probe": python_context_probe,
                        "python_validation": {
                            "required": python_validation_required,
                            "enabled": python_validation_enabled,
                            "reason_code": python_validation_reason_code,
                            "reason_type": python_validation_reason_type,
                            "reason": python_validation_reason,
                            "validated_python_executable": (
                                validated_python_executable_for_execution
                            ),
                        },
                        "python_toolchain_capability": python_toolchain_capability_summary,
                        "pip_probe": pip_probe,
                        "pytest_probe": pytest_probe,
                        "meta": preflight_meta,
                        "probe_commands": effective_probe_commands,
                        "capabilities": {
                            "shell_commands": {
                                "status": shell_status,
                                "reason": shell_reason,
                                "allowed_tools": allowed_tools,
                                "canonical": shell_capability_summary,
                            },
                            "delegation": delegation_capability_summary,
                            "delegation_by_agent": delegation_capabilities_summary,
                            "edits": {"allowed": bool(allow_edits)},
                        },
                        "shell_capability": shell_capability_summary,
                        "delegation_capability": delegation_capability_summary,
                        "delegation_capabilities": delegation_capabilities_summary,
                        "workspace_root_snapshot": preflight_workspace_snapshot,
                    },
                    "bootstrap": bootstrap.meta if bootstrap is not None else None,
                },
                indent=2,
                ensure_ascii=False,
            )

            report_schema_json = json.dumps(
                effective_spec.report_schema_dict, indent=2, ensure_ascii=False
            )

            preflight_summary_md = _format_preflight_summary_md(
                execution_shell=execution_shell,
                shell_status=str(shell_capability_summary.get("state") or shell_status),
                python_runtime_summary=python_runtime_summary,
                python_toolchain_capability=python_toolchain_capability_summary,
                pip_probe=pip_probe,
                pytest_probe=pytest_probe,
                command_diagnostics=command_diagnostics,
                verification_commands=verification_commands,
                verification_timeout_seconds=effective_verification_timeout_seconds,
                verification_reuse_mode=verification_reuse_mode,
                verification_broker_command=verification_broker_command,
                agent=request.agent,
                codex_sandbox_mode=codex_sandbox_mode,
                delegation_capability=delegation_capability_summary,
                verification_timing_profile=verification_timing_profile,
                verification_timing_profile_path=verification_timing_profile_path_for_agent,
                verification_result_path=verification_result_path_for_agent,
            )

            try:
                execution_notes_md = CANONICAL_EXECUTION_NOTES_MD
                shell_command_guidance_md = render_shell_command_guidance_md(
                    shell_family=execution_shell
                )
                if shell_command_guidance_md.strip():
                    execution_notes_md = (
                        execution_notes_md.rstrip() + "\n" + shell_command_guidance_md.strip()
                    )
                base_prompt = build_prompt_from_template(
                    template_text=effective_spec.prompt_template_text,
                    variables={
                        "persona_name": effective_spec.persona_name,
                        "persona_md": effective_spec.persona_md_resolved,
                        "mission_name": effective_spec.mission_name,
                        "mission_md": effective_spec.mission_md_resolved,
                        "users_md": users_md_text,
                        "policy_json": policy_json,
                        "preflight_summary_md": preflight_summary_md,
                        "execution_notes_md": execution_notes_md,
                        "environment_json": environment_json,
                        "report_schema_json": report_schema_json,
                    },
                )
            except TemplateSubstitutionError as e:
                template_path = effective_spec.prompt_template_path
                raise TemplateSubstitutionError(
                    f"Prompt template substitution failed for {template_path}:\n{e}"
                ) from e
            prompt = base_prompt
            if request.agent_user_prompt is not None:
                if not request.agent_user_prompt.strip():
                    raise ValueError("agent_user_prompt must not be blank")
                prompt = request.agent_user_prompt
                (run_dir / "prompt.base.txt").write_text(base_prompt, encoding="utf-8")
            (run_dir / "prompt.txt").write_text(prompt, encoding="utf-8")

            if (
                request.agent == "codex"
                and sandbox is not None
                and not bool(getattr(request, "exec_use_host_agent_login", False))
            ):
                if "OPENAI_API_KEY" not in request.exec_env:
                    raise RuntimeError(
                        "Running Codex inside the Docker execution backend requires credentials. "
                        "Prefer --exec-use-host-agent-login to reuse your local Codex subscription "
                        "login state (~/.codex) inside Docker without API keys (default). "
                        "To opt into API-key login mode, pass --exec-use-api-key-auth, "
                        "--exec-env OPENAI_API_KEY, and set OPENAI_API_KEY on the host."
                    )
                if not os.environ.get("OPENAI_API_KEY"):
                    raise RuntimeError(
                        "OPENAI_API_KEY is allowlisted for the Docker sandbox but is not set on "
                        "the host. Set OPENAI_API_KEY on the host, or remove "
                        "--exec-use-api-key-auth to use host-agent-login mode."
                    )

                _maybe_codex_login_in_sandbox(command_prefix=command_prefix, run_dir=run_dir)

            agent_phase_start_monotonic = time.monotonic()
            phases = run_meta.get("phases")
            if isinstance(phases, dict):
                phases["setup_seconds"] = max(
                    0.0, agent_phase_start_monotonic - run_start_monotonic
                )

            def _attempt_paths(attempt: int) -> tuple[Path, Path, Path]:
                suffix = f"attempt{attempt}"
                return (
                    run_dir / f"raw_events.{suffix}.jsonl",
                    run_dir / f"agent_last_message.{suffix}.txt",
                    run_dir / f"agent_stderr.{suffix}.txt",
                )

            def _run_agent_attempt(
                *,
                prompt_text: str,
                raw_events_attempt_path: Path,
                last_message_attempt_path: Path,
                stderr_attempt_path: Path,
            ) -> tuple[int, list[str]]:
                nonlocal codex_last_invocation_resumed
                nonlocal codex_session_id
                if request.agent == "codex":
                    codex_last_message_for_attempt = (
                        _agent_path_for_staged_file(
                            last_message_attempt_path,
                            run_dir=run_dir,
                            run_dir_mount=backend.run_dir_mount,
                        )
                        if backend.run_dir_mount
                        else None
                    )
                    require_continuation = (
                        bool(request.codex_resume_session_id) or followup_count > 0
                    )
                    resume_session_id = codex_session_id if require_continuation else None
                    if require_continuation and resume_session_id is None:
                        raise RuntimeError("codex_continuation_thread_id_unavailable")
                    codex_last_invocation_resumed = resume_session_id is not None
                    codex_result = run_codex_exec(
                        workspace_dir=workspace_dir_for_agent,
                        prompt=prompt_text,
                        raw_events_path=raw_events_attempt_path,
                        last_message_path=last_message_attempt_path,
                        stderr_path=stderr_attempt_path,
                        sandbox=str(codex_sandbox_mode or "read-only"),
                        ask_for_approval=str(codex_ask_for_approval or "never"),
                        binary=str(codex_binary),
                        subcommand=str(codex_subcommand),
                        model=effective_model,
                        config_overrides=codex_overrides,
                        ignore_user_config=True,
                        ignore_rules=(codex_execpolicy_overlay is None or os.name == "nt"),
                        command_prefix=command_prefix,
                        env_overrides=agent_env_overrides,
                        agent_last_message_path=codex_last_message_for_attempt,
                        resume_session_id=resume_session_id,
                    )
                    result_thread_id = getattr(codex_result, "thread_id", None)
                    if isinstance(result_thread_id, str) and result_thread_id.strip():
                        if resume_session_id is not None and result_thread_id != resume_session_id:
                            raise RuntimeError("codex_continuation_thread_id_changed")
                        codex_session_id = result_thread_id
                    return codex_result.exit_code, codex_result.argv

                if request.agent == "claude":
                    claude_result = run_claude_print(
                        workspace_dir=workspace_dir_for_agent,
                        prompt=prompt_text,
                        raw_events_path=raw_events_attempt_path,
                        last_message_path=last_message_attempt_path,
                        stderr_path=stderr_attempt_path,
                        binary=str(claude_binary),
                        output_format=str(claude_output_format),
                        model=effective_model,
                        allowed_tools=claude_allowed_tools,
                        permission_mode=claude_permission_mode,
                        system_prompt_file=system_prompt_path_for_agent,
                        append_system_prompt_file=append_system_prompt_path_for_agent,
                        command_prefix=command_prefix,
                        env_overrides=agent_env_overrides,
                    )
                    return claude_result.exit_code, claude_result.argv

                _maybe_patch_workspace_gitignore_for_runs_usertest(
                    workspace_dir=acquired.workspace_dir
                )
                gemini_result = run_gemini(
                    workspace_dir=workspace_dir_for_agent,
                    prompt=prompt_text,
                    raw_events_path=raw_events_attempt_path,
                    last_message_path=last_message_attempt_path,
                    stderr_path=stderr_attempt_path,
                    binary=str(gemini_binary),
                    output_format=str(gemini_output_format),
                    sandbox=gemini_sandbox_enabled,
                    model=effective_model,
                    system_prompt_file=system_prompt_path_for_agent,
                    approval_mode=gemini_approval_mode,
                    allowed_tools=gemini_allowed_tools,
                    include_directories=_gemini_include_directories_for_workspace(
                        workspace_dir=acquired.workspace_dir
                    ),
                    command_prefix=command_prefix,
                    env_overrides=gemini_env_overrides,
                )
                return gemini_result.exit_code, gemini_result.argv

            rate_limit_retries = max(0, int(request.agent_rate_limit_retries))
            rate_limit_backoff_seconds = max(0.0, float(request.agent_rate_limit_backoff_seconds))
            rate_limit_backoff_multiplier = max(
                1.0, float(request.agent_rate_limit_backoff_multiplier)
            )
            followup_attempts = max(0, int(request.agent_followup_attempts))

            if verification_commands:
                _write_json(
                    run_dir / "verification_config.json",
                    {
                        "schema_version": 1,
                        "reuse_mode": verification_reuse_mode,
                        "final_handoff_command": verification_broker_command,
                        "commands": verification_commands,
                        "timeout_seconds": effective_verification_timeout_seconds,
                    },
                )

            current_prompt = prompt
            rate_limit_retry_count = 0
            followup_count = 0
            selected_raw_events_path = raw_events_path
            selected_raw_events_ts_path = raw_events_ts_path
            selected_last_message_path = last_message_path
            selected_stderr_path = stderr_path
            selected_stderr_text = ""
            selected_last_message_text = ""
            selected_verification_summary: dict[str, Any] | None = None
            selected_verification_errors: list[str] = []
            verification_seconds_total = 0.0
            verification_broker_seconds_total = 0.0
            verification_reuse_requests: list[dict[str, Any]] = []
            verification_reuse_selected_source = (
                "disabled" if not verification_commands else "post_agent_rerun"
            )
            verification_reuse_fallback_reason: str | None = (
                "verification_commands_not_configured" if not verification_commands else None
            )
            verification_reuse_selected_request_id: str | None = None
            verification_reuse_selected_attempt: int | None = None
            verification_reuse_selected_artifacts_dir: str | None = None
            verification_reuse_workspace_hash_final: dict[str, Any] | None = None
            report_json = None
            report_validation_errors = []
            forced_exit_code: int | None = None
            parked_external_wait: dict[str, Any] | None = None
            retained_oracle_asset_staged = False
            retained_oracle_asset_receipt: dict[str, Any] | None = None

            while True:
                attempt_number = len(attempts_meta) + 1
                (
                    raw_events_attempt_path,
                    last_message_attempt_path,
                    stderr_attempt_path,
                ) = _attempt_paths(attempt_number)
                raw_events_attempt_ts_path = raw_events_attempt_path.with_suffix(".ts.jsonl")

                attempt_started_utc = _utc_now_z()
                attempt_start_monotonic = time.monotonic()
                python_exec_for_verification: str | None = (
                    validated_python_executable_for_execution if not command_prefix else None
                )
                broker_session: VerificationBrokerAttempt | None = None
                broker_latest_result: VerificationBrokerRequestResult | None = None
                broker_results: list[VerificationBrokerRequestResult] = []
                broker_attempt_rows: list[dict[str, Any]] = []
                broker_request_ids: list[str] = []
                if verification_commands and verification_reuse_mode == "auto":
                    assert verification_broker_contract is not None
                    broker_physical_root = _run_dir_agent_visible_root(
                        run_dir=run_dir,
                        run_dir_mount=backend.run_dir_mount,
                        workspace_dir=acquired.workspace_dir,
                    )
                    client_root = broker_physical_root / "verification_broker" / "client"
                    attempt_broker_root = (
                        broker_physical_root / "verification_broker" / f"attempt{attempt_number}"
                    )
                    client_root_for_agent = _agent_path_for_staged_file(
                        client_root,
                        run_dir=broker_physical_root,
                        run_dir_mount=backend.run_dir_mount,
                    )
                    attempt_root_for_agent = _agent_path_for_staged_file(
                        attempt_broker_root,
                        run_dir=broker_physical_root,
                        run_dir_mount=backend.run_dir_mount,
                    )

                    def _run_broker_verification(
                        request_index: int,
                        *,
                        cancel_event: threading.Event | None = None,
                        deadline_monotonic: float | None = None,
                        deadline_utc: str | None = None,  # noqa: ARG001
                        deadline_seconds: float | None = None,
                        progress_callback: Callable[[dict[str, Any]], None] | None = None,
                        _attempt_number: int = attempt_number,
                        _python_exec_for_verification: str | None = python_exec_for_verification,
                    ) -> dict[str, Any]:
                        broker_progress_callback = (
                            (lambda payload: progress_callback(payload, status="running"))
                            if progress_callback is not None
                            else None
                        )
                        return _run_verification_commands(
                            run_dir=run_dir,
                            attempt_number=_attempt_number,
                            commands=verification_commands,
                            command_prefix=command_prefix,
                            cwd=acquired.workspace_dir,
                            timeout_seconds=effective_verification_timeout_seconds,
                            python_executable=_python_exec_for_verification,
                            python_toolchain_capability=python_toolchain_capability_summary,
                            env_overrides=agent_env_overrides,
                            cancel_event=cancel_event,
                            deadline_monotonic=deadline_monotonic,
                            deadline_seconds=deadline_seconds,
                            progress_callback=broker_progress_callback,
                            artifacts_dir_rel=Path("verification")
                            / f"attempt{_attempt_number}"
                            / f"broker_request_{request_index:02d}",
                            run_dir_mount=backend.run_dir_mount,
                            workspace_dir=acquired.workspace_dir,
                        )

                    broker_session = VerificationBrokerAttempt(
                        run_dir=broker_physical_root,
                        attempt_number=attempt_number,
                        client_root=client_root,
                        client_root_for_agent=client_root_for_agent,
                        attempt_root_for_agent=attempt_root_for_agent,
                        contract=verification_broker_contract,
                        verifier=_run_broker_verification,
                        workspace_hash_fn=lambda: compute_workspace_state_hash(
                            acquired.workspace_dir
                        ),
                        utc_now_fn=_utc_now_z,
                        run_async_verifier=True,
                    )
                    broker_session.start()
                agent_exec_start_monotonic = time.monotonic()
                try:
                    agent_exit_code, agent_argv = _run_agent_attempt(
                        prompt_text=current_prompt,
                        raw_events_attempt_path=raw_events_attempt_path,
                        last_message_attempt_path=last_message_attempt_path,
                        stderr_attempt_path=stderr_attempt_path,
                    )
                finally:
                    if broker_session is not None:
                        broker_session.stop(
                            cancel_pending=False,
                        )
                        broker_request_ids = broker_session.request_ids()
                        broker_results = broker_session.results()
                        broker_attempt_rows = broker_session.artifact_rows()
                        broker_latest_result = broker_session.latest_result()
                        if backend.run_dir_mount is None:
                            # The broker's client/request/response files were staged inside
                            # the workspace (see `broker_physical_root` above) so a
                            # workspace-confined agent -- and the client subprocess it
                            # spawns -- could reach them on local backend. Mirror them back
                            # into run_dir once the attempt is done so run_dir remains the
                            # durable, complete audit trail regardless of backend (tooling
                            # such as batch failure classification inspects
                            # `run_dir/verification_broker/...` directly).
                            try:
                                shutil.copytree(
                                    broker_physical_root / "verification_broker",
                                    run_dir / "verification_broker",
                                    dirs_exist_ok=True,
                                )
                            except OSError:
                                pass
                agent_exec_wall_seconds = time.monotonic() - agent_exec_start_monotonic

                raw_attempt_stderr_text = ""
                if stderr_attempt_path.exists():
                    try:
                        raw_attempt_stderr_text = stderr_attempt_path.read_text(
                            encoding="utf-8", errors="replace"
                        ).strip()
                    except OSError:
                        raw_attempt_stderr_text = ""

                codex_personality_warning_line = ""
                codex_personality_warning_seen = bool(
                    request.agent == "codex"
                    and _CODEX_PERSONALITY_MISSING_MESSAGES_WARNING in raw_attempt_stderr_text
                )
                codex_personality_warning_detected = bool(
                    codex_personality_warning_seen and codex_personality_override_requested
                )
                codex_metadata_capture = (
                    _codex_metadata_capture_from_stderr(raw_attempt_stderr_text)
                    if request.agent == "codex"
                    else None
                )
                attempt_warnings: list[str] = []
                if codex_personality_warning_detected:
                    for line in raw_attempt_stderr_text.splitlines():
                        if _CODEX_PERSONALITY_MISSING_MESSAGES_WARNING in line:
                            codex_personality_warning_line = line.strip()
                            break
                    attempt_warnings = _codex_personality_warning_lines(
                        source="agent_stderr",
                        warning_line=codex_personality_warning_line,
                    )

                _sanitize_agent_stderr_file(
                    agent=request.agent,
                    path=stderr_attempt_path,
                    codex_personality_warning_as_error=codex_personality_warning_detected,
                )

                attempt_stderr_text = ""
                if stderr_attempt_path.exists():
                    try:
                        attempt_stderr_text = stderr_attempt_path.read_text(
                            encoding="utf-8", errors="replace"
                        ).strip()
                    except OSError:
                        attempt_stderr_text = ""

                attempt_report_validation_errors: list[str] = []
                attempt_report_json: dict[str, Any] | None = None
                attempt_json_repair: dict[str, Any] | None = None
                attempt_last_text = ""
                if last_message_attempt_path.exists():
                    try:
                        attempt_last_text = last_message_attempt_path.read_text(encoding="utf-8")
                    except OSError:
                        attempt_last_text = ""
                attempt_raw_error_text = (
                    _extract_raw_events_error_messages(raw_events_attempt_path)
                    if agent_exit_code != 0 and raw_events_attempt_path.exists()
                    else ""
                )
                if agent_exit_code == 0:
                    try:
                        (
                            attempt_report_json,
                            attempt_json_repair,
                        ) = _extract_json_object_with_receipt(attempt_last_text)
                    except Exception as e:  # noqa: BLE001
                        attempt_report_validation_errors = [
                            f"$: failed to parse JSON from agent output: {e}"
                        ]
                    if attempt_report_json is not None:
                        attempt_report_validation_errors = validate_report(
                            attempt_report_json, effective_spec.report_schema_dict
                        )

                for broker_result, broker_row in zip(
                    broker_results,
                    broker_attempt_rows,
                    strict=False,
                ):
                    summary = getattr(broker_result, "verification_summary", None)
                    if isinstance(summary, dict):
                        wall_seconds = summary.get("wall_seconds")
                        if isinstance(wall_seconds, (int, float)):
                            verification_broker_seconds_total += max(0.0, float(wall_seconds))
                    verification_reuse_requests.append(dict(broker_row))

                attempt_verification_summary: dict[str, Any] | None = None
                attempt_verification_passed = True
                attempt_verification_rejected_sentinel = False
                attempt_verification_rejected_sentinel_command: str | None = None
                attempt_verification_errors: list[str] = []
                attempt_verification_source = (
                    "disabled" if not verification_commands else "post_agent_rerun"
                )
                attempt_verification_workspace_hash: WorkspaceStateHash | None = None
                attempt_broker_requested = bool(broker_attempt_rows or broker_request_ids)
                attempt_broker_request_id = None
                attempt_broker_response_status: str | None = None
                attempt_broker_response_failure_reason: str | None = None
                attempt_broker_missing_required_artifacts: list[str] = []
                attempt_broker_response_contract_error: str | None = None
                attempt_broker_reuse_candidate = False
                if broker_latest_result is not None:
                    latest_request_id = getattr(broker_latest_result, "request_id", None)
                    if isinstance(latest_request_id, str) and latest_request_id.strip():
                        attempt_broker_request_id = latest_request_id.strip()
                    latest_status = getattr(broker_latest_result, "status", None)
                    if isinstance(latest_status, str) and latest_status.strip():
                        attempt_broker_response_status = latest_status.strip()
                    latest_failure_reason = getattr(broker_latest_result, "failure_reason", None)
                    if isinstance(latest_failure_reason, str) and latest_failure_reason.strip():
                        attempt_broker_response_failure_reason = latest_failure_reason.strip()
                    attempt_broker_missing_required_artifacts = list(
                        verification_broker_missing_result_artifacts(broker_latest_result)
                    )
                    _broker_payload, broker_payload_error = (
                        validate_verification_broker_response_payload(
                            broker_latest_result.to_response_dict(),
                            request_id=broker_latest_result.request_id,
                        )
                    )
                    if broker_payload_error is not None:
                        attempt_broker_response_contract_error = broker_payload_error
                        attempt_broker_response_failure_reason = "incomplete_broker_response"
                elif broker_request_ids:
                    attempt_broker_request_id = broker_request_ids[-1]
                if (
                    agent_exit_code == 0
                    and not attempt_report_validation_errors
                    and verification_commands
                ):
                    broker_reuse_fallback_reason: str | None = None
                    if (
                        verification_reuse_mode == "auto"
                        and broker_latest_result is None
                        and not attempt_broker_requested
                    ):
                        assert broker_session is not None
                        runner_owned_broker = VerificationBrokerAttempt(
                            run_dir=broker_physical_root,
                            attempt_number=attempt_number,
                            client_root=client_root,
                            client_root_for_agent=client_root_for_agent,
                            attempt_root_for_agent=attempt_root_for_agent,
                            contract=verification_broker_contract,
                            verifier=_run_broker_verification,
                            workspace_hash_fn=lambda: compute_workspace_state_hash(
                                acquired.workspace_dir
                            ),
                            utc_now_fn=_utc_now_z,
                            run_async_verifier=True,
                            # The agent may already have launched the advertised client
                            # while its process is returning. Reuse that client's immutable
                            # token and scripts: rewriting them here can race a late
                            # PowerShell/Python process and can turn a valid request into an
                            # invalid-token result.
                            request_token=broker_session.request_token,
                            existing_client=broker_session.client,
                        )
                        runner_owned_broker.start()
                        runner_fallback_result: VerificationBrokerRequestResult | None = None
                        try:
                            runner_fallback_result = runner_owned_broker.request_and_wait(
                                request_origin="runner_after_agent_ready",
                            )
                        finally:
                            # A client launched by the agent can finish writing its request
                            # after the agent process itself returns.  The runner-owned
                            # fallback shares the same request directory, so stopping it by
                            # cancelling pending work can race that late client: the fallback
                            # succeeds while the agent's equivalent request is killed.  Drain
                            # both requests to terminal results instead.  The settle window is
                            # for request discovery only; accepted verification still uses its
                            # normal command deadline.
                            runner_owned_broker.stop(cancel_pending=False)
                            if backend.run_dir_mount is None:
                                try:
                                    shutil.copytree(
                                        broker_physical_root / "verification_broker",
                                        run_dir / "verification_broker",
                                        dirs_exist_ok=True,
                                    )
                                except OSError:
                                    pass
                        broker_request_ids = runner_owned_broker.request_ids()
                        broker_results = runner_owned_broker.results()
                        broker_attempt_rows = runner_owned_broker.artifact_rows()
                        valid_request_ids = set(broker_request_ids)
                        runner_fallback_request_id = (
                            runner_fallback_result.request_id
                            if runner_fallback_result is not None
                            else None
                        )
                        # Prefer a late request actually launched by the agent over the
                        # redundant runner fallback.  Both are drained above, but only the
                        # agent request proves the agent used the advertised broker path.
                        late_agent_results = [
                            result
                            for result in broker_results
                            if result.request_id in valid_request_ids
                            and result.request_id != runner_fallback_request_id
                        ]
                        broker_latest_result = (
                            late_agent_results[-1] if late_agent_results else runner_fallback_result
                        )
                        for broker_result, broker_row in zip(
                            broker_results,
                            broker_attempt_rows,
                            strict=False,
                        ):
                            summary = getattr(broker_result, "verification_summary", None)
                            if isinstance(summary, dict):
                                wall_seconds = summary.get("wall_seconds")
                                if isinstance(wall_seconds, (int, float)):
                                    verification_broker_seconds_total += max(
                                        0.0, float(wall_seconds)
                                    )
                            row = dict(broker_row)
                            if broker_result.request_id == runner_fallback_request_id:
                                row["request_origin"] = "runner_after_agent_ready"
                            verification_reuse_requests.append(row)
                        attempt_broker_requested = bool(broker_attempt_rows or broker_request_ids)
                        if broker_latest_result is None and broker_results:
                            broker_latest_result = broker_results[-1]
                        if broker_latest_result is not None:
                            latest_request_id = getattr(broker_latest_result, "request_id", None)
                            if isinstance(latest_request_id, str) and latest_request_id.strip():
                                attempt_broker_request_id = latest_request_id.strip()
                            latest_status = getattr(broker_latest_result, "status", None)
                            if isinstance(latest_status, str) and latest_status.strip():
                                attempt_broker_response_status = latest_status.strip()
                            latest_failure_reason = getattr(
                                broker_latest_result, "failure_reason", None
                            )
                            if (
                                isinstance(latest_failure_reason, str)
                                and latest_failure_reason.strip()
                            ):
                                attempt_broker_response_failure_reason = (
                                    latest_failure_reason.strip()
                                )
                            attempt_broker_missing_required_artifacts = list(
                                verification_broker_missing_result_artifacts(broker_latest_result)
                            )
                            _broker_payload, broker_payload_error = (
                                validate_verification_broker_response_payload(
                                    broker_latest_result.to_response_dict(),
                                    request_id=broker_latest_result.request_id,
                                )
                            )
                            if broker_payload_error is not None:
                                attempt_broker_response_contract_error = broker_payload_error
                                attempt_broker_response_failure_reason = (
                                    "incomplete_broker_response"
                                )
                    if (
                        verification_reuse_mode == "auto"
                        and broker_latest_result is not None
                        and attempt_broker_response_contract_error is None
                    ):
                        attempt_broker_request_id = broker_latest_result.request_id
                        if attempt_broker_missing_required_artifacts:
                            broker_reuse_fallback_reason = "broker_response_incomplete"
                        else:
                            broker_summary = _coerce_verification_summary_from_broker_result(
                                broker_latest_result,
                                commands_configured=verification_commands,
                            )
                            if broker_latest_result.status == "passed":
                                attempt_verification_workspace_hash = compute_workspace_state_hash(
                                    acquired.workspace_dir
                                )
                                expected_hash = (
                                    broker_latest_result.workspace_hash_after_verification
                                )
                                expected_hash_s = (
                                    expected_hash.strip()
                                    if isinstance(expected_hash, str) and expected_hash.strip()
                                    else None
                                )
                                if (
                                    expected_hash_s is not None
                                    and expected_hash_s
                                    == attempt_verification_workspace_hash.sha256
                                ):
                                    attempt_broker_reuse_candidate = True
                                    attempt_verification_source = "broker_reuse"
                                    attempt_verification_summary = _decorate_verification_summary(
                                        broker_summary,
                                        source="broker_reuse",
                                        reused=True,
                                        workspace_hash=attempt_verification_workspace_hash,
                                        broker_request_id=broker_latest_result.request_id,
                                        broker_artifacts_dir=broker_latest_result.artifacts_dir,
                                    )
                                    verification_reuse_selected_source = "broker_reuse"
                                    verification_reuse_fallback_reason = None
                                    verification_reuse_selected_request_id = (
                                        broker_latest_result.request_id
                                    )
                                    verification_reuse_selected_attempt = attempt_number
                                    verification_reuse_selected_artifacts_dir = (
                                        broker_latest_result.artifacts_dir
                                    )
                                    verification_reuse_workspace_hash_final = (
                                        attempt_verification_workspace_hash.to_dict()
                                    )
                                else:
                                    broker_reuse_fallback_reason = "workspace_hash_mismatch"
                            else:
                                attempt_verification_source = "broker_reuse"
                                attempt_verification_summary = _decorate_verification_summary(
                                    broker_summary,
                                    source="broker_reuse",
                                    reused=True,
                                    workspace_hash=None,
                                    broker_request_id=broker_latest_result.request_id,
                                    broker_artifacts_dir=broker_latest_result.artifacts_dir,
                                )
                                verification_reuse_selected_source = "broker_reuse"
                                verification_reuse_fallback_reason = None
                                verification_reuse_selected_request_id = (
                                    broker_latest_result.request_id
                                )
                                verification_reuse_selected_attempt = attempt_number
                                verification_reuse_selected_artifacts_dir = (
                                    broker_latest_result.artifacts_dir
                                )
                    elif verification_reuse_mode == "auto":
                        broker_reuse_fallback_reason = (
                            "broker_response_incomplete"
                            if attempt_broker_response_contract_error is not None
                            else (
                                "broker_response_missing"
                                if attempt_broker_requested
                                else "broker_not_requested"
                            )
                        )

                    if attempt_verification_summary is None:
                        attempt_verification_source = "post_agent_rerun"
                        if request.retained_oracle_asset_spec is not None:
                            assert request.retained_oracle_assets_root is not None
                            if not retained_oracle_asset_staged:
                                retained_oracle_asset_receipt = stage_retained_oracle_asset(
                                    workspace=acquired.workspace_dir,
                                    trusted_runs_root=request.retained_oracle_assets_root,
                                    spec=request.retained_oracle_asset_spec,
                                )
                                retained_oracle_asset_staged = True
                            destination_manifest = validate_staged_retained_oracle_asset(
                                workspace=acquired.workspace_dir,
                                spec=request.retained_oracle_asset_spec,
                            )
                            assert retained_oracle_asset_receipt is not None
                            retained_oracle_asset_receipt.update(
                                {
                                    "validated_immediately_before_dispatch": True,
                                    "verification_attempt": attempt_number,
                                    "verification_dispatch_utc": _utc_now_z(),
                                    "destination_manifest_sha256": retained_oracle_asset_summary(
                                        trusted_runs_root=request.retained_oracle_assets_root,
                                        spec=request.retained_oracle_asset_spec,
                                    )["manifest_sha256"],
                                    "destination_manifest_entry_count": len(
                                        destination_manifest
                                    ),
                                }
                            )
                            _write_json(
                                run_dir / "retained_oracle_asset_staging.json",
                                retained_oracle_asset_receipt,
                            )
                        verification_kwargs: dict[str, Any] = {
                            "run_dir": run_dir,
                            "attempt_number": attempt_number,
                            "commands": verification_commands,
                            "command_prefix": command_prefix,
                            "cwd": acquired.workspace_dir,
                            "timeout_seconds": effective_verification_timeout_seconds,
                            "python_executable": python_exec_for_verification,
                            "python_toolchain_capability": python_toolchain_capability_summary,
                            "env_overrides": agent_env_overrides,
                            "run_dir_mount": backend.run_dir_mount,
                            "workspace_dir": acquired.workspace_dir,
                        }
                        if verification_reuse_mode == "auto":
                            verification_kwargs["artifacts_dir_rel"] = (
                                Path("verification")
                                / f"attempt{attempt_number}"
                                / "post_agent_rerun"
                            )
                        attempt_verification_summary = _run_verification_commands(
                            **verification_kwargs,
                        )
                        attempt_verification_summary = _decorate_verification_summary(
                            attempt_verification_summary,
                            source="post_agent_rerun",
                            reused=False,
                            workspace_hash=None,
                            broker_request_id=attempt_broker_request_id,
                            broker_artifacts_dir=(
                                getattr(broker_latest_result, "artifacts_dir", None)
                                if broker_latest_result is not None
                                else None
                            ),
                        )
                        verification_reuse_selected_source = "post_agent_rerun"
                        verification_reuse_fallback_reason = broker_reuse_fallback_reason
                        verification_reuse_selected_request_id = None
                        verification_reuse_selected_attempt = attempt_number
                        verification_reuse_selected_artifacts_dir = (
                            str(attempt_verification_summary.get("artifacts_dir") or "").strip()
                            or None
                        )
                    attempt_verification_passed = bool(
                        attempt_verification_summary.get("passed", False)
                    )
                    attempt_verification_terminal_reason = _verification_terminal_reason(
                        attempt_verification_summary
                    )
                    wall_seconds = attempt_verification_summary.get("wall_seconds")
                    if isinstance(wall_seconds, (int, float)):
                        verification_seconds_total += max(0.0, float(wall_seconds))

                    artifacts_dir = attempt_verification_summary.get("artifacts_dir")

                    rejected_sentinel = _first_verification_rejection_sentinel(
                        attempt_verification_summary
                    )
                    if rejected_sentinel is not None:
                        attempt_verification_passed = False
                        attempt_verification_rejected_sentinel = True
                        forced_exit_code = 1
                        cmd = rejected_sentinel.get("command")
                        if isinstance(cmd, str) and cmd.strip():
                            attempt_verification_rejected_sentinel_command = cmd.strip()
                        effective_cmd = rejected_sentinel.get("effective_command")
                        effective_cmd_s = (
                            effective_cmd.strip()
                            if isinstance(effective_cmd, str) and effective_cmd.strip()
                            else None
                        )
                        exit_code = rejected_sentinel.get("exit_code")
                        exit_code_i = exit_code if isinstance(exit_code, int) else None
                        stderr_tail = rejected_sentinel.get("stderr_tail")
                        stderr_tail_s = (
                            stderr_tail.strip()
                            if isinstance(stderr_tail, str) and stderr_tail.strip()
                            else None
                        )

                        attempt_verification_errors = [
                            "verification_rejected_sentinel",
                            "code=verification_rejected_sentinel",
                        ]
                        if isinstance(artifacts_dir, str) and artifacts_dir.strip():
                            attempt_verification_errors.append(
                                f"artifacts_dir={artifacts_dir.strip()}"
                            )
                        if attempt_verification_rejected_sentinel_command is not None:
                            attempt_verification_errors.append(
                                f"command={attempt_verification_rejected_sentinel_command}"
                            )
                        if effective_cmd_s is not None:
                            attempt_verification_errors.append(
                                f"effective_command={effective_cmd_s}"
                            )
                        if exit_code_i is not None:
                            attempt_verification_errors.append(f"exit_code={exit_code_i}")

                        _write_json(
                            run_dir / "error.json",
                            {
                                "type": "VerificationRejectedSentinel",
                                "subtype": "rejection_sentinel",
                                "code": "verification_rejected_sentinel",
                                "message": (
                                    "Verification command dispatch blocked: received rejection "
                                    "sentinel forwarded as a command. This indicates a tool/policy "
                                    "rejection was incorrectly serialized into the shell command "
                                    "stream; the command was not executed."
                                ),
                                "attempt": attempt_number,
                                "verification": {
                                    "summary_path": (
                                        str(Path(artifacts_dir.strip()) / "verification.json")
                                        if isinstance(artifacts_dir, str) and artifacts_dir.strip()
                                        else None
                                    ),
                                    "command": attempt_verification_rejected_sentinel_command,
                                    "effective_command": effective_cmd_s,
                                    "exit_code": exit_code_i,
                                    "stderr_tail": stderr_tail_s,
                                    "command_prefix": list(command_prefix),
                                },
                            },
                        )
                    elif not attempt_verification_passed:
                        attempt_verification_errors = [
                            f"verification_{attempt_verification_terminal_reason}",
                            f"artifacts_dir={artifacts_dir}",
                        ]
                        attempt_verification_errors.append(
                            f"terminal_reason={attempt_verification_terminal_reason}"
                        )
                        if attempt_broker_response_failure_reason is not None:
                            attempt_verification_errors.append(
                                f"broker_failure_reason={attempt_broker_response_failure_reason}"
                            )
                        commands = attempt_verification_summary.get("commands")
                        if isinstance(commands, list) and commands:
                            last = commands[-1] if isinstance(commands[-1], dict) else None
                            if last is not None:
                                cmd = last.get("command")
                                exit_code = last.get("exit_code")
                                if isinstance(cmd, str) and cmd.strip():
                                    attempt_verification_errors.append(f"command={cmd.strip()}")
                                if isinstance(exit_code, int):
                                    attempt_verification_errors.append(f"exit_code={exit_code}")

                failure_text = "\n".join(
                    [
                        value
                        for value in (
                            attempt_stderr_text,
                            attempt_last_text.strip() if attempt_last_text else "",
                            attempt_raw_error_text,
                        )
                        if value
                    ]
                )
                failure_subtype = _classify_failure_subtype(failure_text)
                attempt_external_wait = (
                    _codex_subscription_external_wait(failure_text)
                    if request.agent == "codex" and agent_exit_code != 0
                    else None
                )
                if attempt_external_wait is not None:
                    parked_external_wait = dict(attempt_external_wait)
                if codex_personality_warning_detected:
                    failure_subtype = "invalid_agent_config"
                attempt_finished_utc = _utc_now_z()
                attempt_wall_seconds = time.monotonic() - attempt_start_monotonic
                verification_summary_path: str | None = None
                if attempt_verification_summary is not None:
                    artifacts_dir = Path(
                        str(attempt_verification_summary.get("artifacts_dir", "")).strip()
                    )
                    verification_summary_path = str(artifacts_dir / "verification.json")
                attempt_meta: dict[str, Any] = {
                    "attempt": attempt_number,
                    "attempt_started_utc": attempt_started_utc,
                    "attempt_finished_utc": attempt_finished_utc,
                    "attempt_wall_seconds": attempt_wall_seconds,
                    "agent_exec_wall_seconds": agent_exec_wall_seconds,
                    "exit_code": agent_exit_code,
                    "argv": agent_argv,
                    "agent_session_id": codex_session_id if request.agent == "codex" else None,
                    "continued_session": bool(
                        request.agent == "codex" and codex_last_invocation_resumed
                    ),
                    "failure_subtype": failure_subtype,
                    "report_validation_errors": attempt_report_validation_errors,
                    "json_syntax_repair": attempt_json_repair,
                    "warnings": attempt_warnings,
                    "verification": {
                        "status": (
                            "disabled"
                            if not verification_commands
                            else (
                                "skipped_agent_failed"
                                if agent_exit_code != 0
                                else (
                                    "skipped_report_invalid"
                                    if attempt_report_validation_errors
                                    else (
                                        "rejected_sentinel"
                                        if attempt_verification_rejected_sentinel
                                        else _verification_terminal_reason(
                                            attempt_verification_summary
                                            if isinstance(attempt_verification_summary, dict)
                                            else {}
                                        )
                                    )
                                )
                            )
                        ),
                        "terminal_reason": (
                            _verification_terminal_reason(attempt_verification_summary)
                            if verification_commands
                            and isinstance(attempt_verification_summary, dict)
                            else None
                        ),
                        "source": attempt_verification_source,
                        "passed": attempt_verification_passed if verification_commands else None,
                        "failure_reason": (
                            attempt_verification_summary.get("failure_reason")
                            if verification_commands
                            and isinstance(attempt_verification_summary, dict)
                            else None
                        ),
                        "rejected_sentinel": attempt_verification_rejected_sentinel
                        if verification_commands
                        else None,
                        "rejected_command": attempt_verification_rejected_sentinel_command
                        if attempt_verification_rejected_sentinel
                        else None,
                        "broker_requested": (
                            attempt_broker_requested if verification_commands else False
                        ),
                        "broker_request_id": attempt_broker_request_id,
                        "broker_response_status": attempt_broker_response_status,
                        "broker_response_failure_reason": (attempt_broker_response_failure_reason),
                        "broker_missing_required_artifacts": (
                            attempt_broker_missing_required_artifacts
                        ),
                        "broker_response_contract_error": (attempt_broker_response_contract_error),
                        "reuse_candidate": (
                            attempt_broker_reuse_candidate if verification_commands else False
                        ),
                        "reuse_selected": False,
                        "summary_path": verification_summary_path,
                    },
                    "raw_events_path": raw_events_attempt_path.name,
                    "last_message_path": last_message_attempt_path.name,
                    "stderr_path": stderr_attempt_path.name,
                }
                if codex_metadata_capture is not None:
                    attempt_meta["codex_metadata_capture"] = codex_metadata_capture
                    if codex_metadata_capture_summary is not None:
                        _merge_codex_metadata_capture_summary(
                            summary=codex_metadata_capture_summary,
                            attempt_metadata=codex_metadata_capture,
                            attempt_number=attempt_number,
                        )
                attempts_meta.append(attempt_meta)
                if attempt_external_wait is not None:
                    attempt_meta["external_wait"] = dict(attempt_external_wait)
                    attempt_meta["retry_reason"] = "provider_subscription_usage_limit"
                    attempt_meta["retry_scheduled"] = False
                    attempt_meta["retry_disposition"] = "parked_until_resume_after"

                if codex_personality_warning_detected:
                    message = (
                        "Codex reported that personality was requested but model_messages is "
                        "missing. Aborting to avoid silently running with base instructions."
                    )
                    hint = (
                        "Provide model_messages alongside personality/model_personality in your "
                        "Codex config (configs/agents.yaml agents.codex.config_overrides or "
                        "--agent-config)."
                    )
                    forced_exit_code = 1
                    if not (run_dir / "error.json").exists():
                        _write_json(
                            run_dir / "error.json",
                            {
                                "type": "AgentConfigInvalid",
                                "subtype": "invalid_agent_config",
                                "code": "codex_model_messages_missing",
                                "agent": request.agent,
                                "message": message,
                                "hint": hint,
                                "details": {
                                    "source": "agent_stderr",
                                },
                            },
                        )

                    selected_raw_events_path = raw_events_attempt_path
                    selected_raw_events_ts_path = raw_events_attempt_ts_path
                    selected_last_message_path = last_message_attempt_path
                    selected_stderr_path = stderr_attempt_path
                    selected_stderr_text = attempt_stderr_text
                    selected_last_message_text = attempt_last_text
                    selected_verification_summary = attempt_verification_summary
                    selected_verification_errors = list(attempt_verification_errors)
                    report_json = attempt_report_json
                    report_validation_errors = [
                        message,
                        "code=codex_model_messages_missing",
                        f"hint={hint}",
                    ]
                    break

                retry_reason: str | None = None
                if agent_exit_code != 0 and rate_limit_retry_count < rate_limit_retries:
                    if (
                        failure_subtype == "provider_capacity"
                        and _is_retryable_provider_capacity_failure(failure_text)
                    ):
                        retry_reason = "provider_capacity"
                    elif (
                        failure_subtype == "transient_network"
                        and _is_retryable_transient_network_failure(failure_text)
                    ):
                        retry_reason = "transient_network"
                    elif (
                        request.agent == "claude"
                        and failure_subtype == "tool_use_id_collision"
                        and _is_retryable_tool_use_id_collision_failure(failure_text)
                    ):
                        retry_reason = "tool_use_id_collision"

                if retry_reason is not None:
                    raw_delay_seconds = rate_limit_backoff_seconds * (
                        rate_limit_backoff_multiplier**rate_limit_retry_count
                    )
                    capped_delay_seconds = min(_MAX_AGENT_RETRY_DELAY_SECONDS, raw_delay_seconds)
                    delay_seconds = (
                        random.uniform(0.0, capped_delay_seconds)
                        if capped_delay_seconds > 0
                        else 0.0
                    )
                    attempt_meta["retry_reason"] = retry_reason
                    attempt_meta["retry_delay_seconds_raw"] = raw_delay_seconds
                    attempt_meta["retry_delay_seconds"] = delay_seconds
                    rate_limit_retry_count += 1
                    if delay_seconds > 0:
                        time.sleep(delay_seconds)
                    continue

                if (
                    agent_exit_code == 0
                    and not attempt_report_validation_errors
                    and attempt_verification_summary is not None
                    and not attempt_verification_passed
                    and not attempt_verification_rejected_sentinel
                    and followup_count < followup_attempts
                    and attempt_last_text.strip()
                ):
                    followup_count += 1
                    attempt_meta["followup_scheduled"] = True
                    attempt_meta["followup_reason"] = "verification_failed"
                    attempt_meta["followup_index"] = followup_count
                    current_prompt = _build_verification_followup_prompt(
                        base_prompt=prompt,
                        verification_summary=attempt_verification_summary,
                        schema_dict=effective_spec.report_schema_dict,
                        prior_last_message_text=attempt_last_text,
                        attempt_number=followup_count,
                    )
                    continue

                if (
                    agent_exit_code == 0
                    and attempt_report_validation_errors
                    and followup_count < followup_attempts
                    and attempt_last_text.strip()
                ):
                    followup_count += 1
                    attempt_meta["followup_scheduled"] = True
                    attempt_meta["followup_index"] = followup_count
                    current_prompt = _build_followup_prompt(
                        base_prompt=prompt,
                        report_validation_errors=attempt_report_validation_errors,
                        schema_dict=effective_spec.report_schema_dict,
                        prior_last_message_text=attempt_last_text,
                        attempt_number=followup_count,
                    )
                    continue

                selected_raw_events_path = raw_events_attempt_path
                selected_raw_events_ts_path = raw_events_attempt_ts_path
                selected_last_message_path = last_message_attempt_path
                selected_stderr_path = stderr_attempt_path
                selected_stderr_text = attempt_stderr_text
                selected_last_message_text = attempt_last_text
                selected_verification_summary = attempt_verification_summary
                selected_verification_errors = list(attempt_verification_errors)
                report_json = attempt_report_json
                report_validation_errors = attempt_report_validation_errors
                break

            selected_attempt_index = len(attempts_meta) - 1 if attempts_meta else None
            selected_verification_source = (
                str(selected_verification_summary.get("source") or "").strip()
                if isinstance(selected_verification_summary, dict)
                else ""
            )
            selected_verification_broker_request_id = (
                str(selected_verification_summary.get("broker_request_id") or "").strip()
                if isinstance(selected_verification_summary, dict)
                else ""
            )
            if (
                selected_attempt_index is not None
                and 0 <= selected_attempt_index < len(attempts_meta)
                and selected_verification_source == "broker_reuse"
            ):
                selected_attempt_verification = attempts_meta[selected_attempt_index].get(
                    "verification"
                )
                if isinstance(selected_attempt_verification, dict):
                    selected_attempt_verification["reuse_selected"] = True
                verification_reuse_selected_source = "broker_reuse"
                verification_reuse_fallback_reason = None
                verification_reuse_selected_request_id = (
                    selected_verification_broker_request_id
                    or verification_reuse_selected_request_id
                )
                verification_reuse_selected_attempt = selected_attempt_index + 1
                verification_reuse_selected_artifacts_dir = (
                    str(selected_verification_summary.get("artifacts_dir") or "").strip() or None
                )
                workspace_hash_dict = selected_verification_summary.get("workspace_hash")
                if isinstance(workspace_hash_dict, dict):
                    verification_reuse_workspace_hash_final = dict(workspace_hash_dict)
            elif not verification_commands:
                verification_reuse_selected_source = "disabled"
                verification_reuse_fallback_reason = "verification_commands_not_configured"
            elif (
                selected_verification_source != "post_agent_rerun"
                and verification_reuse_mode == "off"
            ):
                verification_reuse_selected_source = "post_agent_rerun"
                verification_reuse_fallback_reason = "verification_reuse_disabled"

            _write_json(
                run_dir / "agent_attempts.json",
                {
                    "attempts": attempts_meta,
                    "rate_limit_retries_configured": rate_limit_retries,
                    "rate_limit_retries_used": rate_limit_retry_count,
                    "followup_attempts_configured": followup_attempts,
                    "followup_attempts_used": followup_count,
                    **(
                        {"external_wait": parked_external_wait}
                        if parked_external_wait is not None
                        else {}
                    ),
                },
            )

            materialization_errors: list[str] = []

            def _materialize_attempt_artifact(
                src: Path,
                dst: Path,
                *,
                label: str,
                fallback_text: str | None = None,
            ) -> None:
                if src == dst:
                    return
                if src.exists():
                    shutil.copyfile(src, dst)
                    return
                if fallback_text is None:
                    materialization_errors.append(
                        f"missing_selected_attempt_artifact={label}:{src}"
                    )
                    try:
                        dst.write_text("", encoding="utf-8")
                    except OSError as exc:
                        materialization_errors.append(
                            f"failed_selected_attempt_artifact_placeholder={label}:{dst}:{exc}"
                        )
                    return
                try:
                    dst.write_text(fallback_text, encoding="utf-8")
                except OSError as exc:
                    materialization_errors.append(
                        f"failed_selected_attempt_artifact_materialization={label}:{dst}:{exc}"
                    )

            _materialize_attempt_artifact(
                selected_raw_events_path,
                raw_events_path,
                label="raw_events",
            )
            _materialize_attempt_artifact(
                selected_raw_events_ts_path,
                raw_events_ts_path,
                label="raw_events_ts",
                fallback_text="",
            )
            _materialize_attempt_artifact(
                selected_last_message_path,
                last_message_path,
                label="last_message",
                fallback_text=selected_last_message_text,
            )
            _materialize_attempt_artifact(
                selected_stderr_path,
                stderr_path,
                label="stderr",
                fallback_text=selected_stderr_text,
            )
            if materialization_errors:
                forced_exit_code = 1
                message = (
                    "Selected attempt artifacts were incomplete during final materialization; "
                    "the runner refused to silently synthesize missing files."
                )
                if not (run_dir / "error.json").exists():
                    _write_json(
                        run_dir / "error.json",
                        {
                            "type": "SelectedAttemptArtifactsIncomplete",
                            "subtype": "selected_attempt_artifacts_incomplete",
                            "code": "selected_attempt_artifacts_incomplete",
                            "message": message,
                            "details": {
                                "errors": materialization_errors,
                                "selected_verification_source": (
                                    str(selected_verification_summary.get("source") or "").strip()
                                    if isinstance(selected_verification_summary, dict)
                                    else None
                                ),
                                "selected_attempt": selected_attempt_index + 1
                                if selected_attempt_index is not None
                                else None,
                            },
                        },
                    )
                if not report_validation_errors:
                    report_validation_errors = [message, *materialization_errors]

            verification_output_payload: dict[str, Any]
            if selected_verification_summary is not None:
                verification_output_payload = _normalize_verification_summary(
                    selected_verification_summary
                )
            else:
                selected_attempt = attempts_meta[-1] if attempts_meta else {}
                selected_verification = (
                    selected_attempt.get("verification")
                    if isinstance(selected_attempt, dict)
                    else None
                )
                selected_verification_dict = (
                    selected_verification if isinstance(selected_verification, dict) else {}
                )
                status = selected_verification_dict.get("status")
                status_s = status if isinstance(status, str) and status.strip() else "disabled"
                skip_reason = {
                    "disabled": "verification_commands_not_configured",
                    "skipped_agent_failed": "agent_exit_code_nonzero",
                    "skipped_report_invalid": "report_validation_failed",
                }.get(status_s, "verification_not_run")
                verification_output_payload = {
                    "schema_version": 1,
                    "status": status_s,
                    "terminal_reason": None,
                    "skipped": True,
                    "skip_reason": skip_reason,
                    "attempt_number": len(attempts_meta),
                    "commands_configured": verification_commands,
                    "source": "disabled" if status_s == "disabled" else "post_agent_rerun",
                    "reused": False,
                    "workspace_hash": None,
                    "broker_request_id": None,
                    "broker_artifacts_dir": None,
                    "failure_reason": None,
                    "timed_out": False,
                    "cancelled": False,
                }
                if verification_reuse_mode == "auto" and status_s != "disabled":
                    verification_reuse_selected_source = "post_agent_rerun"
                    verification_reuse_fallback_reason = skip_reason
                elif status_s == "disabled":
                    verification_reuse_selected_source = "disabled"
                    verification_reuse_fallback_reason = skip_reason
            if verification_reuse_selected_request_id is not None:
                verification_reuse_requests.sort(
                    key=lambda row: row.get("request_id") != verification_reuse_selected_request_id
                )
            _write_json(run_dir / "verification.json", verification_output_payload)
            _write_json(
                run_dir / "verification_reuse.json",
                {
                    "schema_version": 1,
                    "mode": verification_reuse_mode,
                    "selected_source": verification_reuse_selected_source,
                    "fallback_reason": verification_reuse_fallback_reason,
                    "workspace_hash_final": verification_reuse_workspace_hash_final,
                    "requests": verification_reuse_requests,
                    "selected_request_id": verification_reuse_selected_request_id,
                    "selected_attempt": verification_reuse_selected_attempt,
                    "selected_artifacts_dir": verification_reuse_selected_artifacts_dir,
                },
            )
            phases = run_meta.get("phases")
            if isinstance(phases, dict):
                if verification_commands:
                    phases["verification_seconds"] = max(0.0, float(verification_seconds_total))
                phases["verification_source"] = verification_reuse_selected_source
                phases["verification_reused"] = verification_reuse_selected_source == "broker_reuse"
                phases["verification_broker_seconds"] = max(
                    0.0, float(verification_broker_seconds_total)
                )

            if agent_exit_code != 0 and not report_validation_errors:
                if selected_stderr_text:
                    report_validation_errors = selected_stderr_text.splitlines()[:20]
                elif selected_last_message_text.strip():
                    report_validation_errors = selected_last_message_text.strip().splitlines()[:20]
                else:
                    report_validation_errors = [
                        f"{request.agent} exited with code {agent_exit_code}"
                    ]
            if selected_verification_errors:
                _write_json(
                    run_dir / "verification_errors.json",
                    {
                        "schema_version": 1,
                        "errors": selected_verification_errors,
                    },
                )
                if isinstance(selected_verification_summary, dict):
                    verification_summary = _normalize_verification_summary(
                        selected_verification_summary
                    )
                    terminal_reason = _verification_terminal_reason(verification_summary)
                    if terminal_reason != "passed":
                        forced_exit_code = 1
                        if not (run_dir / "error.json").exists():
                            commands = verification_summary.get("commands")
                            failed_command = (
                                commands[-1]
                                if isinstance(commands, list)
                                and commands
                                and isinstance(commands[-1], dict)
                                else {}
                            )
                            artifacts_dir = verification_summary.get("artifacts_dir")
                            artifacts_dir_s = (
                                artifacts_dir.strip()
                                if isinstance(artifacts_dir, str) and artifacts_dir.strip()
                                else None
                            )
                            verification_details = {
                                "summary_path": (
                                    str(Path(artifacts_dir_s) / "verification.json")
                                    if artifacts_dir_s is not None
                                    else None
                                ),
                                "artifacts_dir": artifacts_dir_s,
                                "terminal_reason": terminal_reason,
                                "failure_reason": verification_summary.get("failure_reason"),
                                "command": failed_command.get("command"),
                                "effective_command": failed_command.get("effective_command"),
                                "exit_code": failed_command.get("exit_code"),
                                "stdout_path": failed_command.get("stdout_path"),
                                "stderr_path": failed_command.get("stderr_path"),
                                "command_prefix": list(command_prefix),
                            }
                            _write_json(
                                run_dir / "error.json",
                                {
                                    "type": "VerificationFailed",
                                    "subtype": terminal_reason,
                                    "code": selected_verification_errors[0],
                                    "message": (
                                        "Verification did not pass; see verification artifacts "
                                        "for command output and diagnostics."
                                    ),
                                    "failure_phase": "verification",
                                    "exit_code": 1,
                                    "attempt": (
                                        selected_attempt_index + 1
                                        if selected_attempt_index is not None
                                        else None
                                    ),
                                    "details": {
                                        "errors": selected_verification_errors,
                                        "verification_summary": verification_details,
                                    },
                                    "verification": verification_details,
                                },
                            )
        finally:
            if sandbox is not None:
                capture_container_artifacts(
                    container_name=getattr(sandbox, "container_name", ""),
                    artifacts_dir=run_dir / "sandbox",
                )
                sandbox.close()

        agent_phase_end_monotonic = time.monotonic()
        if codex_execpolicy_overlay is not None:
            overlay_errors = _finalize_controlled_codex_execpolicy(
                overlay=codex_execpolicy_overlay,
                binary=controlled_codex_binary,
                env_overrides=controlled_codex_env_overrides,
                config_overrides=controlled_codex_config_overrides,
                run_dir=run_dir,
            )
            if overlay_errors:
                report_validation_errors = list(
                    dict.fromkeys([*report_validation_errors, *overlay_errors])
                )
                if agent_exit_code == 0:
                    agent_exit_code = 1
        if agent_phase_start_monotonic is not None:
            phases = run_meta.get("phases")
            if isinstance(phases, dict):
                phases["agent_seconds"] = max(
                    0.0, agent_phase_end_monotonic - agent_phase_start_monotonic
                )
        postprocess_phase_start_monotonic = agent_phase_end_monotonic

        run_errors: list[str] = []
        if agent_exit_code != 0:
            _sanitize_agent_stderr_file(agent=request.agent, path=stderr_path)

            stderr_text = ""
            if stderr_path.exists():
                try:
                    stderr_text = stderr_path.read_text(encoding="utf-8", errors="replace").strip()
                except OSError:
                    stderr_text = ""
            stderr_text = _augment_tool_file_not_found_diagnostics(
                stderr_text=stderr_text,
                workspace_root=acquired.workspace_dir if acquired is not None else None,
            )
            if stderr_text and stderr_path.exists():
                try:
                    stderr_path.write_text(stderr_text.rstrip() + "\n", encoding="utf-8")
                except OSError:
                    pass

            last_message_text = ""
            if last_message_path.exists():
                try:
                    last_message_text = last_message_path.read_text(
                        encoding="utf-8", errors="replace"
                    ).strip()
                except OSError:
                    last_message_text = ""

            raw_events_plaintext_excerpt = ""
            if (
                request.agent == "claude"
                and not last_message_text.strip()
                and raw_events_path.exists()
            ):
                raw_events_plaintext_excerpt = _extract_raw_events_plaintext_excerpt(
                    raw_events_path
                )
            raw_events_error_text = (
                _extract_raw_events_error_messages(raw_events_path)
                if raw_events_path.exists()
                else ""
            )

            last_message_excerpt = last_message_text
            last_message_truncated = False
            if len(last_message_excerpt) > 4000:
                last_message_excerpt = last_message_excerpt[:4000] + "\n...[truncated]..."
                last_message_truncated = True

            provider_message_text = last_message_text
            if not provider_message_text.strip() and raw_events_error_text.strip():
                provider_message_text = raw_events_error_text.strip()
            if not provider_message_text.strip() and raw_events_plaintext_excerpt.strip():
                provider_message_text = raw_events_plaintext_excerpt.strip()

            combined_text = "\n".join([x for x in (stderr_text, provider_message_text) if x])
            failure_subtype = _classify_failure_subtype(combined_text)
            codex_subscription_limit = (
                _extract_codex_subscription_usage_limit(combined_text)
                if request.agent == "codex"
                else None
            )
            if (
                stderr_text
                and failure_subtype
                in {"provider_capacity", "transient_network", "tool_use_id_collision"}
                and "[runner_retry_summary]" not in stderr_text
            ):
                retryable = True
                if failure_subtype == "provider_capacity":
                    retryable = _is_retryable_provider_capacity_failure(combined_text)
                elif failure_subtype == "transient_network":
                    retryable = _is_retryable_transient_network_failure(combined_text)
                elif failure_subtype == "tool_use_id_collision":
                    retryable = (
                        request.agent == "claude"
                        and _is_retryable_tool_use_id_collision_failure(combined_text)
                    )

                retry_summary_lines = [
                    (
                        "[runner_retry_summary] "
                        f"code={failure_subtype} "
                        f"retryable={str(retryable).lower()} "
                        f"retries_configured={rate_limit_retries} "
                        f"retries_used={rate_limit_retry_count} "
                        f"backoff_seconds={rate_limit_backoff_seconds} "
                        f"backoff_multiplier={rate_limit_backoff_multiplier} "
                        f"max_delay_seconds={_MAX_AGENT_RETRY_DELAY_SECONDS}"
                    )
                ]
                if not retryable:
                    retry_summary_lines.append(
                        "hint=This failure looks non-retryable (quota/billing/account). "
                        "Fix the account issue and re-run; retries will not help."
                    )
                elif rate_limit_retries <= 0:
                    retry_summary_lines.append(
                        "hint=Retries are disabled (agent_rate_limit_retries=0). "
                        "Re-run later or increase agent_rate_limit_retries for transient failures."
                    )
                elif rate_limit_retry_count >= rate_limit_retries:
                    retry_summary_lines.append(
                        "hint=Runner retries were exhausted. Retry later, reduce concurrency, "
                        "or switch models."
                    )
                else:
                    retry_summary_lines.append(
                        "hint=Transient error detected. The runner may retry automatically; "
                        "see agent_attempts.json."
                    )

                stderr_text = "\n".join(retry_summary_lines).strip() + "\n\n" + stderr_text
                if stderr_path.exists():
                    try:
                        stderr_path.write_text(stderr_text.rstrip() + "\n", encoding="utf-8")
                    except OSError:
                        pass
            stderr_was_empty = not bool(stderr_text)
            raw_events_size_bytes = (
                raw_events_path.stat().st_size if raw_events_path.exists() else 0
            )
            last_message_size_chars = len(last_message_text)

            quota_exhaustion: dict[str, Any] | None = None
            if request.agent == "claude":
                quota_exhaustion = _extract_claude_quota_exhaustion(combined_text)

            if codex_subscription_limit is not None:
                external_wait_stderr = _format_codex_subscription_usage_limit_stderr(
                    provider_message=provider_message_text,
                    resume_after_raw=codex_subscription_limit.get("resume_after_raw"),
                )
                stderr_text = external_wait_stderr
                try:
                    stderr_path.write_text(stderr_text.rstrip() + "\n", encoding="utf-8")
                except OSError:
                    pass

            if not stderr_text and quota_exhaustion is not None and provider_message_text.strip():
                stderr_text = _format_claude_quota_exhaustion_stderr(
                    provider_message=provider_message_text,
                    reset_raw=quota_exhaustion.get("reset_raw"),
                    reset_timezone=quota_exhaustion.get("reset_timezone"),
                )
                try:
                    stderr_path.write_text(stderr_text.rstrip() + "\n", encoding="utf-8")
                except OSError:
                    pass
            elif not stderr_text:
                synthetic_lines = [
                    "[synthetic_stderr] No stderr captured from agent process.",
                    f"agent={request.agent}",
                    f"exit_code={agent_exit_code}",
                    f"failure_subtype={failure_subtype or 'unknown'}",
                    f"raw_events={raw_events_path.name}",
                    f"last_message={last_message_path.name}",
                    f"raw_events_size_bytes={raw_events_size_bytes}",
                    f"last_message_size_chars={last_message_size_chars}",
                ]
                if request.agent == "claude":
                    synthetic_lines.append(
                        "hint=Claude produced no stderr; inspect raw_events.jsonl and "
                        "agent_attempts.json for additional context."
                    )
                if last_message_excerpt:
                    synthetic_lines.extend(["", "[agent_last_message]", last_message_excerpt])
                elif raw_events_plaintext_excerpt.strip():
                    synthetic_lines.extend(
                        ["", "[raw_events_plaintext_excerpt]", raw_events_plaintext_excerpt.strip()]
                    )
                stderr_text = "\n".join(synthetic_lines).strip()
                try:
                    stderr_path.write_text(stderr_text + "\n", encoding="utf-8")
                except OSError:
                    pass

            if stderr_text:
                run_errors = stderr_text.splitlines()[:20]
            elif last_message_text:
                run_errors = last_message_text.splitlines()[:20]
            else:
                run_errors = [f"{request.agent} exited with code {agent_exit_code}"]

            error_payload: dict[str, Any] = {
                "type": "AgentExecFailed",
                "exit_code": agent_exit_code,
                "stderr": "\n".join(run_errors).strip(),
                "stderr_synthesized": stderr_was_empty,
                "artifacts": {
                    "raw_events": raw_events_path.name,
                    "last_message": last_message_path.name,
                    "stderr": stderr_path.name,
                },
                **({"subtype": failure_subtype} if failure_subtype is not None else {}),
                **(
                    {
                        "last_message": last_message_excerpt,
                        "last_message_truncated": last_message_truncated,
                    }
                    if last_message_excerpt
                    else {}
                ),
            }
            if quota_exhaustion is not None:
                error_payload = {
                    **error_payload,
                    "type": "AgentQuotaExceeded",
                    "code": "claude_out_of_extra_usage",
                    "provider": "claude",
                    "provider_message": provider_message_text.strip() or stderr_text.strip(),
                    "reset_time": {
                        "raw": quota_exhaustion.get("reset_raw"),
                        "timezone": quota_exhaustion.get("reset_timezone"),
                    },
                }
            if codex_subscription_limit is not None:
                external_wait = _codex_subscription_external_wait(combined_text)
                assert external_wait is not None
                error_payload = {
                    **error_payload,
                    "type": "AgentExternalWait",
                    "subtype": "provider_subscription_usage_limit",
                    "code": "codex_chatgpt_subscription_usage_limit",
                    "provider": "codex",
                    "provider_message": provider_message_text.strip(),
                    "route": "chatgpt_subscription",
                    "api_fallback_allowed": False,
                    "external_wait": external_wait,
                }

            if not (run_dir / "error.json").exists():
                _write_json(run_dir / "error.json", error_payload)

        normalized_events_path = run_dir / "normalized_events.jsonl"

        def _normalize_raw_events(
            *,
            source_path: Path,
            destination_path: Path,
        ) -> None:
            source_ts_path = source_path.with_suffix(".ts.jsonl")
            raw_ts_f = None
            raw_ts_iter = None
            if source_ts_path.exists():
                try:
                    raw_ts_f = source_ts_path.open("r", encoding="utf-8")
                    raw_ts_iter = (line.strip() for line in raw_ts_f if line.strip())
                except OSError:
                    raw_ts_f = None
                    raw_ts_iter = None
            try:
                if request.agent == "codex":
                    normalize_codex_events(
                        raw_events_path=source_path,
                        normalized_events_path=destination_path,
                        raw_ts_iter=raw_ts_iter,
                        workspace_root=acquired.workspace_dir,
                        workspace_mount=workspace_mount,
                    )
                elif request.agent == "claude":
                    normalize_claude_events(
                        raw_events_path=source_path,
                        normalized_events_path=destination_path,
                        raw_ts_iter=raw_ts_iter,
                        workspace_root=acquired.workspace_dir,
                        workspace_mount=workspace_mount,
                    )
                else:
                    normalize_gemini_events(
                        raw_events_path=source_path,
                        normalized_events_path=destination_path,
                        raw_ts_iter=raw_ts_iter,
                        workspace_root=acquired.workspace_dir,
                        workspace_mount=workspace_mount,
                    )
            finally:
                if raw_ts_f is not None:
                    raw_ts_f.close()

        attempt_event_sources = [
            run_dir / str(attempt["raw_events_path"])
            for attempt in attempts_meta
            if isinstance(attempt.get("raw_events_path"), str)
        ]
        normalization_source = raw_events_path
        # A missing selected-attempt artifact has already failed the run closed and
        # produced a canonical empty placeholder above. Do not reopen the absent source
        # here and replace that precise terminal cause with a generic FileNotFoundError.
        if not materialization_errors:
            if len(attempt_event_sources) == 1:
                normalization_source = attempt_event_sources[0]
            elif len(attempt_event_sources) > 1:
                normalization_source = run_dir / "raw_events.all_attempts.jsonl"
                with normalization_source.open("wb") as cumulative_f:
                    for source_path in attempt_event_sources:
                        content = source_path.read_bytes()
                        cumulative_f.write(content)
                        if content and not content.endswith(b"\n"):
                            cumulative_f.write(b"\n")

                attempt_ts_sources = [
                    source_path.with_suffix(".ts.jsonl")
                    for source_path in attempt_event_sources
                ]
                if all(source_path.is_file() for source_path in attempt_ts_sources):
                    cumulative_ts_path = normalization_source.with_suffix(".ts.jsonl")
                    with cumulative_ts_path.open("wb") as cumulative_ts_f:
                        for source_path in attempt_ts_sources:
                            content = source_path.read_bytes()
                            cumulative_ts_f.write(content)
                            if content and not content.endswith(b"\n"):
                                cumulative_ts_f.write(b"\n")

        _normalize_raw_events(
            source_path=normalization_source,
            destination_path=normalized_events_path,
        )

        if isinstance(shell_capability_summary, dict):
            _append_shell_capability_normalized_event(
                run_dir=run_dir,
                shell_capability=shell_capability_summary,
                blocked=False,
            )

        diff_numstat: list[dict[str, Any]] = []
        if allow_edits:
            diff_numstat = _git_numstat(acquired.workspace_dir)
            _write_json(run_dir / "diff_numstat.json", diff_numstat)
            if diff_numstat:
                with normalized_events_path.open("a", encoding="utf-8", newline="\n") as out_f:
                    for item in diff_numstat:
                        path = item.get("path")
                        lines_added = item.get("lines_added")
                        lines_removed = item.get("lines_removed")
                        if not isinstance(path, str):
                            continue
                        if not isinstance(lines_added, int) or not isinstance(lines_removed, int):
                            continue
                        event = make_event(
                            "write_file",
                            {
                                "path": path,
                                "lines_added": lines_added,
                                "lines_removed": lines_removed,
                            },
                        )
                        out_f.write(json.dumps(event, ensure_ascii=False) + "\n")

        try:
            metrics = compute_metrics(iter_events_jsonl(normalized_events_path))
        except Exception as metrics_exc:  # noqa: BLE001
            metrics = {
                "event_counts": {},
                "distinct_files_read": [],
                "distinct_docs_read": [],
                "distinct_files_written": [],
                "commands_executed": 0,
                "commands_failed": 0,
                "lines_added_total": 0,
                "lines_removed_total": 0,
                "step_count": 0,
                "metrics_error": str(metrics_exc),
            }
        if allow_edits:
            metrics["diff_numstat"] = diff_numstat
        _write_json(run_dir / "metrics.json", metrics)

        if report_json is not None:
            extensions = report_json.get("extensions")
            if not isinstance(extensions, dict):
                extensions = {}
                report_json["extensions"] = extensions
            verification_extension = {
                "status": verification_output_payload.get("status"),
                "terminal_reason": verification_output_payload.get("terminal_reason"),
                "failure_reason": verification_output_payload.get("failure_reason"),
                "source": verification_output_payload.get("source"),
                "reused": bool(verification_output_payload.get("reused", False)),
                "timed_out": bool(verification_output_payload.get("timed_out", False)),
                "cancelled": bool(verification_output_payload.get("cancelled", False)),
            }
            extensions["verification"] = verification_extension
            extensions["python_toolchain_capability"] = python_toolchain_capability_summary
            if isinstance(shell_capability_summary, dict):
                extensions["shell_capability"] = shell_capability_summary
            if bool(resolved_inputs.mission.requires_shell):
                final_report_validation_errors = validate_report(
                    report_json,
                    effective_spec.report_schema_dict,
                    require_shell_capability=True,
                )
                if final_report_validation_errors:
                    report_validation_errors = final_report_validation_errors
            _write_json(run_dir / "report.json", report_json)
        elif agent_exit_code != 0 and not report_validation_errors:
            report_validation_errors = run_errors

        if (
            report_json is not None
            and not report_validation_errors
            and workspace_ref_payload is not None
            and workspace_ref_payload["will_cleanup_workspace"] is True
            and _report_has_live_workspace_output(report_json, acquired.workspace_dir)
        ):
            retained_workspace_ref = {
                **workspace_ref_payload,
                "will_cleanup_workspace": False,
                "cleanup_suppressed_reason": "reported_output_retention",
            }
            _write_json(run_dir / "workspace_ref.json", retained_workspace_ref)
            workspace_ref_payload = retained_workspace_ref
            retain_workspace_for_reported_output = True

        if report_validation_errors:
            _write_json(run_dir / "report_validation_errors.json", report_validation_errors)

        if allow_edits:
            patch = _git_diff(acquired.workspace_dir)
            if patch.strip():
                (run_dir / "patch.diff").write_text(patch, encoding="utf-8", newline="\n")

        md = render_report_markdown(
            report=report_json or {}, metrics=metrics, target_ref=target_ref
        )
        (run_dir / "report.md").write_text(md, encoding="utf-8", newline="\n")

        final_exit_code = agent_exit_code
        if final_exit_code == 0 and forced_exit_code is not None:
            final_exit_code = int(forced_exit_code)

        return RunResult(
            run_dir=run_dir,
            exit_code=final_exit_code,
            report_validation_errors=report_validation_errors,
            agent_session_id=codex_session_id,
        )
    except Exception as e:  # noqa: BLE001
        message = str(e)
        subtype = _classify_failure_subtype(message)
        extra: dict[str, Any] = {}
        user_errors: list[str] = [message]
        code = getattr(e, "code", None)
        details = getattr(e, "details", None)
        hint = getattr(e, "hint", None)
        if isinstance(code, str) and code.strip():
            code_s = code.strip()
            extra["code"] = code_s
            user_errors.append(f"code={code_s}")
        if isinstance(details, dict) and details:
            extra["details"] = details
            user_errors.append(f"details={json.dumps(details, ensure_ascii=False)}")
        if isinstance(hint, str) and hint.strip():
            hint_s = hint.strip()
            extra["hint"] = hint_s
            user_errors.append(f"hint={hint_s}")
        if isinstance(e, OSError):
            if e.errno is not None:
                extra["errno"] = e.errno
                user_errors.append(f"errno={e.errno}")
            winerror = getattr(e, "winerror", None)
            if winerror is not None:
                extra["winerror"] = winerror
                user_errors.append(f"winerror={winerror}")
            if e.strerror is not None:
                extra["strerror"] = e.strerror
                user_errors.append(f"strerror={e.strerror}")
            if e.filename is not None:
                extra["filename"] = e.filename
                user_errors.append(f"filename={e.filename}")
            filename2 = getattr(e, "filename2", None)
            if filename2 is not None:
                extra["filename2"] = filename2
                user_errors.append(f"filename2={filename2}")

            traceback_path = run_dir / "error_traceback.txt"
            try:
                traceback_path.write_text(traceback.format_exc(), encoding="utf-8")
            except OSError:
                traceback_path = None
            if traceback_path is not None:
                extra["traceback_artifact"] = traceback_path.name
                user_errors.append(f"traceback={traceback_path.name}")

            derived_hint = (
                'Common causes on Windows: invalid filename characters (< > : " / \\\\ | ? *), '
                "overly long paths, or output streams that reject writes. "
                "See error_traceback.txt for the failing operation."
            )
            if "hint" in extra and isinstance(extra["hint"], str) and extra["hint"].strip():
                extra["hint"] = extra["hint"].strip() + "\n" + derived_hint
            else:
                extra["hint"] = derived_hint
            user_errors.append(f"hint={derived_hint}")
        error_payload = {
            "type": type(e).__name__,
            "message": message,
            **({"subtype": subtype} if subtype is not None else {}),
            **extra,
        }
        error_path = run_dir / "error.json"
        if error_path.exists():
            _write_json(
                run_dir / "postprocess_error.json",
                {
                    **error_payload,
                    "preserved_terminal_error": error_path.name,
                },
            )
        else:
            _write_json(error_path, error_payload)
        return RunResult(
            run_dir=run_dir,
            exit_code=1,
            report_validation_errors=user_errors,
            agent_session_id=codex_session_id,
        )
    finally:
        if codex_execpolicy_overlay is not None and not codex_execpolicy_overlay.restored:
            try:
                _finalize_controlled_codex_execpolicy(
                    overlay=codex_execpolicy_overlay,
                    binary=controlled_codex_binary,
                    env_overrides=controlled_codex_env_overrides,
                    config_overrides=controlled_codex_config_overrides,
                    run_dir=run_dir,
                )
            except Exception:
                pass
        cleanup_start_monotonic = time.monotonic()
        try:
            phases = run_meta.get("phases")
            if not isinstance(phases, dict):
                phases = {}
                run_meta["phases"] = phases

            if "setup_seconds" not in phases:
                if agent_phase_start_monotonic is not None:
                    phases["setup_seconds"] = max(
                        0.0, agent_phase_start_monotonic - run_start_monotonic
                    )
                else:
                    phases["setup_seconds"] = max(
                        0.0, cleanup_start_monotonic - run_start_monotonic
                    )

            if agent_phase_start_monotonic is not None and "agent_seconds" not in phases:
                end = agent_phase_end_monotonic or cleanup_start_monotonic
                phases["agent_seconds"] = max(0.0, end - agent_phase_start_monotonic)

            if (
                postprocess_phase_start_monotonic is not None
                and "postprocess_seconds" not in phases
            ):
                phases["postprocess_seconds"] = max(
                    0.0, cleanup_start_monotonic - postprocess_phase_start_monotonic
                )
        except Exception:  # noqa: BLE001
            pass

        cleanup_seconds: float | None = None
        if (
            acquired is not None
            and acquired.mode != "existing"
            and not (request.keep_workspace or request.exec_keep_container)
            and not retain_workspace_for_reported_output
            and acquired.workspace_dir.exists()
        ):
            cleanup_wall_start = time.monotonic()
            try:
                remove_acquired_workspace(acquired.workspace_dir)
            except OSError:
                pass
            cleanup_seconds = time.monotonic() - cleanup_wall_start

        try:
            phases = run_meta.get("phases")
            if isinstance(phases, dict) and cleanup_seconds is not None:
                phases["cleanup_seconds"] = max(0.0, cleanup_seconds)
            run_meta["run_finished_utc"] = _utc_now_z()
            run_meta["run_wall_seconds"] = max(0.0, time.monotonic() - run_start_monotonic)
            _write_json(run_dir / "run_meta.json", run_meta)
        except Exception:  # noqa: BLE001
            pass

        _maybe_write_token_monitoring_artifacts(run_dir)
        _maybe_write_lifecycle_telemetry(
            run_dir=run_dir,
            request=request,
            model=effective_model,
        )
