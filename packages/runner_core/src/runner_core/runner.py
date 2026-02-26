from __future__ import annotations

import json
import os
import platform
import random
import re
import shlex
import shutil
import subprocess
import time
import traceback
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from json import JSONDecoder
from pathlib import Path
from typing import Any

from agent_adapters import (
    normalize_claude_events,
    normalize_codex_events,
    normalize_gemini_events,
    run_claude_print,
    run_codex_exec,
    run_gemini,
    validate_codex_personality_config_overrides,
    validate_codex_reasoning_effort_config_overrides,
)
from agent_adapters.codex_config import toml_basic_string
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
from runner_core.catalog import load_catalog_config
from runner_core.execution_backend import prepare_execution_backend
from runner_core.pathing import slugify, utc_timestamp_compact
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
from runner_core.prompt import TemplateSubstitutionError, build_prompt_from_template
from runner_core.python_interpreter_probe import resolve_usable_python_interpreter
from runner_core.python_runtime import (
    probe_pip_module,
    probe_pytest_module,
    select_python_runtime,
    verification_commands_may_provision_pytest,
    verification_commands_need_pytest,
)
from runner_core.run_spec import resolve_effective_run_inputs
from runner_core.target_acquire import acquire_target


def _is_windows() -> bool:
    return os.name == "nt"


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
    agent_system_prompt_file: Path | None = None
    agent_append_system_prompt: str | None = None
    agent_append_system_prompt_file: Path | None = None
    keep_workspace: bool = False
    preflight_commands: tuple[str, ...] = ()
    preflight_required_commands: tuple[str, ...] = ()
    verification_commands: tuple[str, ...] = ()
    verification_timeout_seconds: float | None = None

    exec_backend: str = "local"
    exec_docker_context: Path | None = None
    exec_dockerfile: Path | None = None
    exec_docker_python: str = "auto"
    exec_docker_timeout_seconds: float | None = None
    exec_use_target_sandbox_cli_install: bool = False
    exec_use_host_agent_login: bool = True
    exec_network: str = "open"
    exec_cache: str = "cold"
    exec_cache_dir: Path | None = None
    exec_env: tuple[str, ...] = ()
    exec_keep_container: bool = False
    exec_rebuild_image: bool = False
    agent_rate_limit_retries: int = 2
    agent_rate_limit_backoff_seconds: float = 1.0
    agent_rate_limit_backoff_multiplier: float = 2.0
    agent_followup_attempts: int = 2


@dataclass(frozen=True)
class RunResult:
    run_dir: Path
    exit_code: int
    report_validation_errors: list[str]


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

_FAILURE_SUBTYPE_RULES: tuple[tuple[str, tuple[re.Pattern[str], ...]], ...] = (
    (
        "invalid_agent_config",
        (
            re.compile(r"invalid value.*model_reasoning_effort", re.IGNORECASE),
            re.compile(r"model_reasoning_effort.*\b(enum|expected|invalid)\b", re.IGNORECASE),
        ),
    ),
    (
        "provider_quota_exceeded",
        (
            re.compile(r"out of extra usage", re.IGNORECASE),
            re.compile(r"extra usage.*\bresets?\b", re.IGNORECASE),
        ),
    ),
    (
        "provider_capacity",
        (
            re.compile(r"\b429\b", re.IGNORECASE),
            re.compile(r"resource_exhausted", re.IGNORECASE),
            re.compile(r"model_capacity_exhausted", re.IGNORECASE),
            re.compile(r"no capacity available", re.IGNORECASE),
            re.compile(r"exhausted your capacity", re.IGNORECASE),
            re.compile(r"hit your limit", re.IGNORECASE),
            re.compile(r"rate[_ -]?limit", re.IGNORECASE),
            re.compile(r"too many requests", re.IGNORECASE),
            re.compile(r"\bquota\b", re.IGNORECASE),
        ),
    ),
    (
        "provider_auth",
        (
            re.compile(r"\b401\b", re.IGNORECASE),
            re.compile(r"\bunauthorized\b", re.IGNORECASE),
            re.compile(r"invalid api key", re.IGNORECASE),
            re.compile(r"incorrect api key", re.IGNORECASE),
            re.compile(r"authentication failed", re.IGNORECASE),
        ),
    ),
    (
        "transient_network",
        (
            re.compile(r"\bEAI_AGAIN\b", re.IGNORECASE),
            re.compile(r"temporary failure in name resolution", re.IGNORECASE),
            re.compile(r"\bENOTFOUND\b", re.IGNORECASE),
        ),
    ),
    (
        "disk_full",
        (
            re.compile(r"\bENOSPC\b", re.IGNORECASE),
            re.compile(r"no space left on device", re.IGNORECASE),
            re.compile(r"disk quota exceeded", re.IGNORECASE),
        ),
    ),
    (
        "permission_policy",
        (
            re.compile(r"interactive approval", re.IGNORECASE),
            re.compile(r"apply_patch_approval_request", re.IGNORECASE),
            re.compile(r"denied by policy", re.IGNORECASE),
            re.compile(r"permission mode", re.IGNORECASE),
            re.compile(r"outside the allowed workspace", re.IGNORECASE),
        ),
    ),
    (
        "nested_agent_session",
        (
            re.compile(
                r"claude code cannot be launched inside another claude code session",
                re.IGNORECASE,
            ),
        ),
    ),
    (
        "binary_or_command_missing",
        (
            re.compile(r"command not found", re.IGNORECASE),
            re.compile(r"could not launch .*cli process", re.IGNORECASE),
            re.compile(r"failed to launch .*cli", re.IGNORECASE),
            re.compile(r"no such file or directory", re.IGNORECASE),
        ),
    ),
)
_NON_RETRYABLE_PROVIDER_CAPACITY_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"insufficient[_ -]?quota", re.IGNORECASE),
    re.compile(r"quota exceeded", re.IGNORECASE),
    re.compile(r"hit your limit", re.IGNORECASE),
    re.compile(r"out of extra usage", re.IGNORECASE),
    re.compile(r"billing", re.IGNORECASE),
    re.compile(r"payment required", re.IGNORECASE),
    re.compile(r"upgrade (plan|account)", re.IGNORECASE),
    re.compile(r"trial (has )?ended", re.IGNORECASE),
)
_NON_RETRYABLE_TRANSIENT_NETWORK_PATTERNS: tuple[re.Pattern[str], ...] = ()

_GEMINI_STDERR_STRIP_LINES: frozenset[str] = frozenset(
    {
        "Loaded cached credentials.",
        "Hook registry initialized with 0 hook entries.",
        "Hook registry initialized with 0 hook entries",
    }
)
_CODEX_PERSONALITY_MISSING_MESSAGES_WARNING = (
    "Model personality requested but model_messages is missing"
)
_CODEX_SHELL_SNAPSHOT_WARNING = "Shell snapshot not supported yet for PowerShell"
_CODEX_SHELL_SNAPSHOT_WARNING_CODE = "shell_snapshot_powershell_unsupported"
_CODEX_TURN_METADATA_TIMEOUT_CODE = "turn_metadata_header_timeout"
_CODEX_MODEL_REFRESH_TIMEOUT_CODE = "codex_model_refresh_timeout"
_CODEX_MODEL_REFRESH_TIMEOUT_HINT = "hint=Codex model refresh timed out; model list may be stale."
_MAX_AGENT_RETRY_DELAY_SECONDS = 60.0
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
_GEMINI_METRICS_RECORDING_LINE_RE = re.compile(
    (
        r"^Error recording tool call interactions: .*recordCodeAssistMetrics failed, "
        r"reason:\s*(?P<reason>.+)$"
    ),
    re.IGNORECASE,
)

_CLAUDE_OUT_OF_EXTRA_USAGE_RE = re.compile(r"out of extra usage", re.IGNORECASE)
_CLAUDE_RESET_EXTRACT_RE = re.compile(
    r"\bresets?\b[: ]+(?P<when>.+)",
    re.IGNORECASE,
)
_CLAUDE_IANA_TZ_IN_PARENS_RE = re.compile(r"\((?P<tz>[A-Za-z_]+/[A-Za-z_]+)\)")


def _extract_claude_quota_exhaustion(text: str) -> dict[str, Any] | None:
    if not isinstance(text, str) or not text.strip():
        return None
    if not _CLAUDE_OUT_OF_EXTRA_USAGE_RE.search(text):
        return None

    reset_raw: str | None = None
    m = _CLAUDE_RESET_EXTRACT_RE.search(text)
    if m is not None:
        candidate = m.group("when").strip()
        reset_raw = candidate if candidate else None

    tz: str | None = None
    for source in (reset_raw, text):
        if not source:
            continue
        tz_m = _CLAUDE_IANA_TZ_IN_PARENS_RE.search(source)
        if tz_m is not None:
            tz = tz_m.group("tz")
            break

    return {
        "provider": "claude",
        "reason": "out_of_extra_usage",
        "reset_raw": reset_raw,
        "reset_timezone": tz,
    }


def _format_claude_quota_exhaustion_stderr(
    *,
    provider_message: str,
    reset_raw: str | None,
    reset_timezone: str | None,
) -> str:
    lines: list[str] = [
        "[agent_quota_exceeded] Claude quota/usage exhausted (out of extra usage).",
    ]
    if isinstance(reset_raw, str) and reset_raw.strip():
        lines.append(f"reset_time={reset_raw.strip()}")
    if isinstance(reset_timezone, str) and reset_timezone.strip():
        lines.append(f"reset_timezone={reset_timezone.strip()}")
    lines.append("hint=Retry after the reset time or reduce usage/concurrency.")
    if provider_message.strip():
        lines.extend(["", "[provider_message]", provider_message.strip()])
    return "\n".join(lines).strip()
_GEMINI_PROVIDER_CAPACITY_MODEL_RE = re.compile(
    r"No capacity available for model\s+(?P<model>[A-Za-z0-9_.:-]+)",
    re.IGNORECASE,
)


def _sanitize_agent_stderr_text(*, agent: str, text: str) -> str:
    if not text:
        return text

    if agent == "gemini":
        raw_lines = text.splitlines()
        lines = [line for line in raw_lines if line.strip() not in _GEMINI_STDERR_STRIP_LINES]

        saw_missing_pgrep_output = any(
            line.strip().lower() == "missing pgrep output" for line in lines
        )
        if saw_missing_pgrep_output:
            lines = [line for line in lines if line.strip().lower() != "missing pgrep output"]

        metrics_lines: list[str] = []
        other_lines: list[str] = []
        for line in lines:
            if _GEMINI_METRICS_RECORDING_LINE_RE.match(line.strip()):
                metrics_lines.append(line.strip())
            else:
                other_lines.append(line)

        metrics_occurrences = len(metrics_lines)
        metrics_reason = ""
        if metrics_lines:
            match = _GEMINI_METRICS_RECORDING_LINE_RE.match(metrics_lines[0])
            if match is not None:
                metrics_reason = match.group("reason").strip()

        other_text = "\n".join(other_lines).strip()
        lowered = "\n".join(lines).lower()
        hints: list[str] = []
        prefix_blocks: list[str] = []
        body_lines: list[str] = []

        is_policy_denial = "tool execution denied by policy" in lowered
        is_run_shell_command_denial = "error executing tool run_shell_command" in lowered
        has_heredoc = bool(re.search(r"<<\s*\w+", other_text))

        if _classify_failure_subtype(other_text) == "provider_capacity":
            model = ""
            model_match = _GEMINI_PROVIDER_CAPACITY_MODEL_RE.search(other_text)
            if model_match is not None:
                model = model_match.group("model")
            else:
                json_model_match = re.search(r"\"model\"\s*:\s*\"(?P<model>[^\"]+)\"", other_text)
                if json_model_match is not None:
                    model = json_model_match.group("model")

            retryable = _is_retryable_provider_capacity_failure(other_text)
            model_clause = f" model={model}" if model else ""
            classification = "transient_error" if retryable else "account_or_quota_error"
            prefix_blocks.append(
                "\n".join(
                    [
                        (
                            "[gemini_error_summary] code=provider_capacity "
                            f"classification={classification} retryable={str(retryable).lower()}"
                        ),
                        (
                            "detail=Gemini API reported HTTP 429 RESOURCE_EXHAUSTED "
                            f"(capacity unavailable).{model_clause}"
                        ),
                        (
                            "hint=If this is transient vendor capacity, retry later or pick a "
                            "different model via `--model`. "
                            "If this is quota/billing related, retries will not help."
                        ),
                    ]
                )
            )
            body_lines = [
                line
                for line in other_lines
                if line.lstrip().startswith("Error executing tool") or line.lstrip().startswith("[")
            ]
        elif is_policy_denial:
            prefix_blocks.append(
                "\n".join(
                    [
                        "[gemini_error_summary] code=policy_denial "
                        "classification=policy_denial retryable=false",
                        "detail=Gemini tool execution was denied by policy.",
                        (
                            "hint=Rewrite the operation using sandbox-safe tools "
                            "(read_file/write_file/replace) or simplify the command. "
                            "Check preflight.json -> capabilities for allowed tools."
                        ),
                    ]
                )
            )
            # Keep stderr concise: policy-denial errors sometimes echo huge payloads (for example
            # heredocs). Prefer only tool-level error lines and brief parser diagnostics.
            body_lines = [
                line
                for line in other_lines
                if (
                    line.lstrip().startswith("Error executing tool")
                    or "tool execution denied by policy" in line.lower()
                    or "bash command parsing error" in line.lower()
                    or "syntax errors" in line.lower()
                    or line.lstrip().startswith("[")
                )
            ]
        else:
            body_lines = other_lines

        if metrics_occurrences:
            reason_clause = f" reason={metrics_reason}" if metrics_reason else ""
            prefix_blocks.append(
                "\n".join(
                    [
                        (
                            "[gemini_warning_summary] code=metrics_recording_failed "
                            f"occurrences={metrics_occurrences} classification=transient_warning"
                        ),
                        f"detail=Gemini CLI failed to record metrics.{reason_clause}".strip(),
                        (
                            "hint=This is best-effort telemetry and typically does not affect the "
                            "run output. If it persists, check DNS/proxy/network access and retry."
                        ),
                    ]
                )
            )

        if (
            "error executing tool grep_search" in lowered
            and "invalid regular expression" in lowered
            and "tool=grep_search" not in lowered
        ):
            hints.append(
                "\n".join(
                    [
                        "[gemini_tool_hint] tool=grep_search code=invalid_regex "
                        "classification=user_input_error",
                        "hint=Gemini grep_search patterns are regular expressions. "
                        "Escape regex metacharacters "
                        "(for example `(`, `)`, `[`, `]`) "
                        "or search for a simpler literal substring.",
                    ]
                )
            )

        if (
            "error executing tool replace" in lowered
            and "could not find the string to replace" in lowered
            and "tool=replace" not in lowered
        ):
            hints.append(
                "\n".join(
                    [
                        "[gemini_tool_hint] tool=replace code=string_not_found "
                        "classification=user_input_error",
                        "hint=Gemini replace requires an exact match. "
                        "Re-run grep_search around the intended "
                        "edit location and copy/paste a longer, unique snippet "
                        "(watch whitespace/line endings).",
                    ]
                )
            )

        if (
            "error executing tool read_file" in lowered
            and "file not found" in lowered
            and "tool=read_file" not in lowered
        ):
            if saw_missing_pgrep_output:
                hints.append(
                    "\n".join(
                        [
                            "[gemini_tool_hint] tool=read_file code=missing_pgrep_output "
                            "classification=capability_notice",
                            "hint=Gemini CLI sometimes emits `missing pgrep output` "
                            "alongside read_file `File not found` errors. "
                            "Inspect raw_events.jsonl for the full missing path "
                            "and re-run with a corrected, workspace-relative path.",
                        ]
                    )
                )
            else:
                hints.append(
                    "\n".join(
                        [
                            "[gemini_tool_hint] tool=read_file code=file_not_found "
                            "classification=user_input_error",
                            "hint=Confirm the file path exists in the active workspace. "
                            "If the stderr line omits the missing path, "
                            "check raw_events.jsonl for the full File not found message.",
                        ]
                    )
                )

        if (
            is_policy_denial
            and is_run_shell_command_denial
            and "tool=run_shell_command" not in lowered
            and has_heredoc
        ):
            hints.append(
                "\n".join(
                    [
                        "[gemini_tool_hint] tool=run_shell_command "
                        "code=policy_denied_heredoc classification=policy_denial",
                        (
                            "hint=This sandbox/policy rejects heredoc syntax "
                            "(for example `<<EOF`). "
                            "Use `write_file`/`replace` for multiline content instead of heredocs."
                        ),
                    ]
                )
            )
        elif (
            is_policy_denial
            and is_run_shell_command_denial
            and "tool=run_shell_command" not in lowered
        ):
            hints.append(
                "\n".join(
                    [
                        "[gemini_tool_hint] tool=run_shell_command "
                        "code=policy_denied classification=policy_denial",
                        (
                            "hint=This command was denied by sandbox/policy. "
                            "Check preflight.json -> capabilities and adjust the command "
                            "to use allowed tools."
                        ),
                    ]
                )
            )

        rendered_blocks: list[str] = []
        if prefix_blocks:
            rendered_blocks.append("\n\n".join(prefix_blocks).strip())
        if body_lines:
            rendered_blocks.append("\n".join(body_lines).strip())
        sanitized = "\n\n".join([block for block in rendered_blocks if block]).strip()

        if hints:
            sanitized = (sanitized + "\n\n" if sanitized else "") + "\n\n".join(hints)

        return sanitized

    if agent == "claude":
        blocks: list[list[str]] = []
        current: list[str] = []
        for line in text.splitlines():
            if not line.strip():
                if current:
                    blocks.append(current)
                    current = []
                continue
            current.append(line)
        if current:
            blocks.append(current)

        config_missing_occurrences = 0
        seen_config_blocks: set[str] = set()
        rendered_blocks: list[str] = []

        for block in blocks:
            rendered = "\n".join(block)
            if block and block[0].startswith("Claude configuration file not found at:"):
                config_missing_occurrences += 1
                if rendered in seen_config_blocks:
                    continue
                seen_config_blocks.add(rendered)
            rendered_blocks.append(rendered)

        if config_missing_occurrences > 1:
            rendered_blocks.append(
                "[claude_warning_summary] code=claude_config_missing "
                f"occurrences={config_missing_occurrences} classification=capability_notice"
            )

        if "Claude Code cannot be launched inside another Claude Code session" in text:
            rendered_blocks.append(
                "\n".join(
                    [
                        "[claude_error_hint] code=claude_nested_session classification=env_error",
                        "hint=Claude Code cannot be launched inside another Claude Code session. "
                        "Run usertest outside Claude Code, or use --agent codex/gemini.",
                    ]
                )
            )

        return "\n\n".join(rendered_blocks)

    if agent == "codex":
        # Codex can emit repeated warnings every turn; collapse known noise to one structured note.
        saw_personality_warning = False
        shell_snapshot_count = 0
        turn_metadata_timeout_count = 0
        model_refresh_timeout_count = 0
        lines: list[str] = []
        for line in text.splitlines():
            lowered = line.lower()
            if _CODEX_PERSONALITY_MISSING_MESSAGES_WARNING in line:
                if saw_personality_warning:
                    continue
                saw_personality_warning = True
            if _CODEX_SHELL_SNAPSHOT_WARNING.lower() in lowered:
                shell_snapshot_count += 1
                continue
            if "turn metadata" in lowered and "timed out" in lowered and "header" in lowered:
                turn_metadata_timeout_count += 1
                continue
            if (
                "failed to refresh available models" in lowered
                and "timeout waiting for child process" in lowered
            ):
                model_refresh_timeout_count += 1
                continue
            lines.append(line)

        if shell_snapshot_count > 0:
            lines.extend(
                [
                    (
                        "[codex_warning_summary] "
                        f"code={_CODEX_SHELL_SNAPSHOT_WARNING_CODE} "
                        f"occurrences={shell_snapshot_count} "
                        "classification=capability_notice"
                    ),
                    (
                        "hint=PowerShell shell snapshot unsupported; "
                        "continuing without shell snapshot metadata."
                    ),
                ]
            )
        if turn_metadata_timeout_count > 0:
            lines.extend(
                [
                    (
                        "[codex_warning_summary] "
                        f"code={_CODEX_TURN_METADATA_TIMEOUT_CODE} "
                        f"occurrences={turn_metadata_timeout_count} "
                        "classification=capability_notice"
                    ),
                    "hint=Turn metadata header timed out; continuing without metadata header.",
                ]
            )
        if model_refresh_timeout_count > 0:
            lines.extend(
                [
                    (
                        "[codex_warning_summary] "
                        f"code={_CODEX_MODEL_REFRESH_TIMEOUT_CODE} "
                        f"occurrences={model_refresh_timeout_count} "
                        "classification=capability_notice"
                    ),
                    _CODEX_MODEL_REFRESH_TIMEOUT_HINT,
                ]
            )
        return "\n".join(lines)

    return text


def _sanitize_agent_stderr_file(*, agent: str, path: Path) -> None:
    if agent not in {"gemini", "codex", "claude"} or not path.exists():
        return
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    sanitized = _sanitize_agent_stderr_text(agent=agent, text=raw)
    if sanitized == raw:
        return
    try:
        path.write_text(sanitized, encoding="utf-8")
    except OSError:
        return


def _override_key_matches_suffix(*, key: str, suffix: str) -> bool:
    normalized = key.strip().lower().replace("-", "_")
    return normalized == suffix or normalized.endswith("." + suffix)


def _strip_codex_personality_overrides(overrides: list[str]) -> list[str]:
    """
    Remove personality/model_personality overrides.

    This is used to turn the "personality requested but model_messages missing" situation into a
    warning-only path (by preventing Codex from receiving an invalid personality config override).
    """

    kept: list[str] = []
    for raw in overrides:
        key_raw, sep, _value_raw = raw.partition("=")
        key = key_raw.strip()
        if not sep or not key:
            kept.append(raw)
            continue
        if _override_key_matches_suffix(
            key=key, suffix="personality"
        ) or _override_key_matches_suffix(key=key, suffix="model_personality"):
            continue
        kept.append(raw)
    return kept


def _codex_personality_warning_lines(*, source: str, warning_line: str | None = None) -> list[str]:
    lines = [
        (
            "Codex warned that personality was requested but model_messages is missing "
            "(Codex fell back to base instructions)."
        ),
        f"source={source}",
        "code=codex_model_messages_missing",
    ]
    if isinstance(warning_line, str) and warning_line.strip():
        lines.append(f"warning={warning_line.strip()}")
    lines.append(
        "hint=If you intended to use a personality, provide model_messages alongside "
        "personality/model_personality (configs/agents.yaml or --agent-config). Otherwise, "
        "ignore this warning."
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


def _build_preflight_command_list(request: RunRequest) -> list[str]:
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


def _classify_failure_subtype(text: str) -> str | None:
    if not text.strip():
        return None
    for subtype, patterns in _FAILURE_SUBTYPE_RULES:
        if any(pattern.search(text) for pattern in patterns):
            return subtype
    return None


def _is_retryable_provider_capacity_failure(text: str) -> bool:
    if not text.strip():
        return True
    return not any(pattern.search(text) for pattern in _NON_RETRYABLE_PROVIDER_CAPACITY_PATTERNS)


def _is_retryable_transient_network_failure(text: str) -> bool:
    if not text.strip():
        return True
    return not any(pattern.search(text) for pattern in _NON_RETRYABLE_TRANSIENT_NETWORK_PATTERNS)


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
            timeout_seconds=5.0,
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
                    timeout=2.5,
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
                    timeout=2.0,
                    check=False,
                    env=effective_env,
                )
                usable = int(proc.returncode or 0) == 0
                if not usable:
                    reason_code = "probe_failed"
                    stderr = (proc.stderr or "").strip()
                    reason = (
                        "bash probe exited non-zero"
                        + (f": {stderr}" if stderr else f" (exit_code={proc.returncode})")
                    )
            except subprocess.TimeoutExpired:
                usable = False
                reason_code = "unresponsive"
                reason = "bash probe timed out (2.0s) running `bash -lc \"echo ok\"`."
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
    if python_probe is not None:
        meta["python_interpreter"] = python_probe.to_dict()
    return out, meta


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
        timeout_seconds=5.0,
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


def _format_preflight_summary_md(
    *,
    execution_shell: str,
    shell_status: str,
    python_runtime_summary: dict[str, Any],
    pip_probe: dict[str, Any] | None,
    pytest_probe: dict[str, Any] | None,
    command_diagnostics: dict[str, Any],
    verification_commands: list[str],
    verification_timeout_seconds: float | None,
    agent: str,
    codex_sandbox_mode: str | None,
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
    python_label = "`unavailable`" if not py_path else f"`{py_path}`"
    if py_version:
        python_label += f" ({py_version})"

    pip_label = "unknown"
    if isinstance(pip_probe, dict):
        pip_ok = bool(pip_probe.get("passed") is True)
        reason_code = pip_probe.get("reason_code")
        reason_code_s = reason_code if isinstance(reason_code, str) and reason_code else None
        if pip_ok:
            pip_label = "OK"
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
    if isinstance(pytest_probe, dict):
        pytest_ok = bool(pytest_probe.get("passed") is True)
        reason_code = pytest_probe.get("reason_code")
        reason_code_s = reason_code if isinstance(reason_code, str) and reason_code else None
        if pytest_ok:
            pytest_label = "OK"
        else:
            suffix = f" ({reason_code_s})" if reason_code_s else ""
            pytest_label = "NOT OK" + suffix
        lines.append(f"- pytest: {pytest_label}")

    if verification_commands:
        timeout_label = "none"
        if verification_timeout_seconds is not None:
            timeout_label = f"{float(verification_timeout_seconds):g}"
        lines.append("- Verification gate:")
        lines.append(f"  - timeout_seconds: {timeout_label}")
        lines.append("  - commands:")
        for cmd in verification_commands:
            lines.append(f"    - `{cmd}`")

    if (
        agent == "codex"
        and isinstance(codex_sandbox_mode, str)
        and codex_sandbox_mode.strip().lower().startswith("workspace-")
    ):
        sandbox_label = codex_sandbox_mode.strip()
        lines.append(
            "- Note: Codex workspace sandbox is enabled "
            f"(sandbox={sandbox_label}); commands/files outside the workspace may be unavailable. "
            "If you need a consistent toolchain, consider `--exec-backend docker`."
        )

    return "\n".join(lines)


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
            timeout=3,
            check=False,
        )
    except Exception:  # noqa: BLE001
        return False
    return proc.returncode == 0


def _gemini_include_directories_for_workspace(*, workspace_dir: Path) -> list[str]:
    """
    Gemini CLI may apply gitignore-like "ignore patterns" to file tools (read/search), which can
    hide local-only run artifacts (this repo ignores `runs/`).

    When a workspace contains `runs/usertest/`, explicitly include that directory so agents can
    read generated `report.md` / `report.json` / `metrics.json` during triage flows.
    """

    # Gemini CLI runs inside the runner's Docker sandbox (Linux). Always pass POSIX-style
    # include-directories to avoid `runs\\usertest` being interpreted as a literal path segment.
    include_rel = (Path("runs") / "usertest").as_posix()
    candidate = workspace_dir / "runs" / "usertest"
    if candidate.is_dir():
        return [include_rel]

    # Some missions run this repo's own CLI inside the workspace and then try to inspect the
    # resulting artifacts under `runs/usertest/...`. Gemini CLI's file tools may ignore `runs/`
    # by default, so ensure the directory exists up front for this runner repo so we can pass
    # `--include-directories runs/usertest` at process start.
    marker = workspace_dir / "tools" / "scaffold" / "monorepo.toml"
    if marker.exists():
        try:
            candidate.mkdir(parents=True, exist_ok=True)
        except OSError:
            return []
        return [include_rel]

    return []


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
    hints["config"] = (
        f"Update `configs/agents.yaml` `agents.{agent}.binary` to a valid path/name."
    )
    hints["doctor"] = (
        "Run `python -m agent_adapters.cli doctor` to check which agent CLIs "
        "are on PATH."
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
        hints["install"] = (
            f"Install `{required_binary}` on PATH"
            + (f"; suggested: {install_hint}." if install_hint else ".")
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
            {
                k: v
                for k, v in env_overrides.items()
                if isinstance(k, str) and isinstance(v, str)
            }
        )

    binary_to_run = binary
    if os.name == "nt" and not command_prefix:
        path = (env or os.environ).get("PATH")
        resolved = shutil.which(binary, path=path)
        if resolved:
            binary_to_run = resolved

    argv = [binary_to_run, "--version"]
    full_argv = [*command_prefix, *argv] if command_prefix else argv

    try:
        proc = subprocess.run(
            full_argv,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
            check=False,
            env=env,
        )
    except FileNotFoundError as e:
        return {
            "ok": False,
            "argv": full_argv,
            "error": "FileNotFoundError",
            "details": str(e),
        }
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "argv": full_argv,
            "error": "timeout",
            "timeout_seconds": timeout_seconds,
        }
    except Exception as e:  # noqa: BLE001
        return {
            "ok": False,
            "argv": full_argv,
            "error": type(e).__name__,
            "details": str(e),
        }

    stdout = (proc.stdout or "").strip()
    stderr = (proc.stderr or "").strip()
    return {
        "ok": int(proc.returncode or 0) == 0,
        "argv": full_argv,
        "exit_code": int(proc.returncode or 0),
        "stdout_excerpt": stdout[:300] if stdout else None,
        "stderr_excerpt": stderr[:300] if stderr else None,
    }


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
            {
                k: v
                for k, v in env_overrides.items()
                if isinstance(k, str) and isinstance(v, str)
            }
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


def _resolve_agent_prompt_input_path(*, raw: Path, repo_root: Path, workspace_dir: Path) -> Path:
    if raw.is_absolute():
        candidate = raw
        if candidate.exists() and candidate.is_file():
            return candidate.resolve()
        raise FileNotFoundError(f"Agent prompt file not found: {raw}")

    candidates = [
        workspace_dir / raw,
        repo_root / raw,
    ]
    for candidate in candidates:
        if candidate.exists() and candidate.is_file():
            return candidate.resolve()

    raise FileNotFoundError(
        f"Agent prompt file not found.\ninput={raw}\ntried={', '.join(str(p) for p in candidates)}"
    )


def _stage_agent_prompt_text(*, run_dir: Path, name: str, text: str) -> Path:
    dest_dir = run_dir / "agent_prompts"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_path = dest_dir / name
    dest_path.write_text(text, encoding="utf-8")
    return dest_path


def _stage_agent_prompt_file(*, run_dir: Path, name: str, src_path: Path) -> Path:
    dest_dir = run_dir / "agent_prompts"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_path = dest_dir / name
    shutil.copyfile(src_path, dest_path)
    return dest_path

def _agent_path_for_staged_file(
    staged_path: Path, *, run_dir: Path, run_dir_mount: str | None
) -> str:
    if run_dir_mount is None:
        return str(staged_path.resolve())

    mount = run_dir_mount.strip().replace("\\", "/").rstrip("/")
    if not mount:
        mount = "/run_dir"
    if not mount.startswith("/"):
        mount = f"/{mount}"

    rel = staged_path.resolve().relative_to(run_dir.resolve()).as_posix()
    if not rel:
        return mount
    return f"{mount}/{rel}"


def _extract_json_object(text: str) -> dict[str, Any]:
    cleaned = text.strip()
    if not cleaned:
        raise ValueError("Agent output was empty; expected a JSON object.")

    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        if lines and lines[0].lstrip().startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        cleaned = "\n".join(lines).strip()

    try:
        parsed = json.loads(cleaned)
    except Exception:  # noqa: BLE001
        parsed = None
    if isinstance(parsed, dict):
        return parsed

    decoder = JSONDecoder()
    for idx, char in enumerate(cleaned):
        if char != "{":
            continue
        try:
            parsed_obj, _ = decoder.raw_decode(cleaned[idx:])
        except Exception:  # noqa: BLE001
            continue
        if isinstance(parsed_obj, dict):
            return parsed_obj

    raise ValueError("Could not find a JSON object in agent output.")


def _build_followup_prompt(
    *,
    base_prompt: str,
    report_validation_errors: list[str],
    schema_dict: dict[str, Any],
    prior_last_message_text: str,
    attempt_number: int,
    ) -> str:
    errors = [str(e).strip() for e in report_validation_errors if str(e).strip()]
    error_block = "\n".join(f"- {line}" for line in errors[:20]) or "- (no error details)"

    prior_message = prior_last_message_text.strip()
    if len(prior_message) > 4000:
        prior_message = prior_message[:4000] + "\n...[truncated]"
    if not prior_message:
        prior_message = "(no prior message captured)"

    schema_json = json.dumps(schema_dict, indent=2, ensure_ascii=False)

    return (
        f"{base_prompt}\n\n"
        "Follow-up required.\n"
        f"This is follow-up attempt #{attempt_number} because your previous response did not "
        "validate against the report schema.\n\n"
        "Validation errors:\n"
        f"{error_block}\n\n"
        "Previous assistant output:\n"
        "```\n"
        f"{prior_message}\n"
        "```\n\n"
        "Return ONLY one JSON object that validates against this schema.\n"
        "Do not include markdown fences, prose, or extra keys.\n\n"
        "Schema:\n"
        f"{schema_json}\n"
    )


def _build_verification_followup_prompt(
    *,
    base_prompt: str,
    verification_summary: dict[str, Any],
    schema_dict: dict[str, Any],
    prior_last_message_text: str,
    attempt_number: int,
) -> str:
    commands = verification_summary.get("commands")
    command_lines: list[str] = []
    if isinstance(commands, list):
        for idx, item in enumerate(commands, start=1):
            if not isinstance(item, dict):
                continue
            cmd = item.get("command")
            exit_code = item.get("exit_code")
            wall_seconds = item.get("wall_seconds")
            timed_out = item.get("timed_out")
            stdout_tail = item.get("stdout_tail")
            stderr_tail = item.get("stderr_tail")
            if not isinstance(cmd, str) or not cmd.strip():
                continue
            command_lines.append(f"{idx}) {cmd.strip()}")
            if isinstance(exit_code, int):
                command_lines.append(f"   exit_code={exit_code}")
            if isinstance(wall_seconds, (int, float)):
                command_lines.append(f"   wall_seconds={wall_seconds:.2f}")
            if isinstance(timed_out, bool):
                command_lines.append(f"   timed_out={str(timed_out).lower()}")
            if isinstance(stdout_tail, str) and stdout_tail.strip():
                command_lines.extend(["   stdout_tail:", "```", stdout_tail.strip(), "```"])
            if isinstance(stderr_tail, str) and stderr_tail.strip():
                command_lines.extend(["   stderr_tail:", "```", stderr_tail.strip(), "```"])

    commands_block = "\n".join(command_lines).strip()
    if not commands_block:
        commands_block = "(no verification command details captured)"

    prior_message = prior_last_message_text.strip()
    if len(prior_message) > 20000:
        prior_message = prior_message[:20000] + "\n...[truncated]"
    if not prior_message:
        prior_message = "(no prior message captured)"

    schema_json = json.dumps(schema_dict, indent=2, ensure_ascii=False)

    artifacts_hint = ""
    artifacts_dir = verification_summary.get("artifacts_dir")
    if isinstance(artifacts_dir, str) and artifacts_dir.strip():
        artifacts_hint = (
            "\n\nVerification artifacts:\n"
            f"- Host: {artifacts_dir.strip()}\n"
            f"- Docker: /run_dir/{artifacts_dir.strip()}\n"
        )

    return (
        f"{base_prompt}\n\n"
        "Follow-up required.\n"
        f"This is follow-up attempt #{attempt_number} because the required "
        "verification checks failed.\n\n"
        "Verification results:\n"
        f"{commands_block}"
        f"{artifacts_hint}\n\n"
        "Previous assistant output:\n"
        "```\n"
        f"{prior_message}\n"
        "```\n\n"
        "Fix the issues so the verification checks pass, then return ONLY one JSON object that "
        "validates against this schema.\n"
        "Do not include markdown fences, prose, or extra keys.\n\n"
        "Schema:\n"
        f"{schema_json}\n"
    )


def _verification_shell_argv(*, command_prefix: list[str], command: str) -> list[str]:
    if command_prefix:
        return [*command_prefix, "sh", "-lc", command]
    if _is_windows():
        return ["powershell", "-NoProfile", "-NonInteractive", "-Command", command]
    return ["sh", "-lc", command]


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


def _probe_windows_bash_usable() -> dict[str, Any]:
    resolved = shutil.which("bash")
    if resolved is None:
        return {
            "present": False,
            "usable": False,
            "resolved_path": None,
            "reason_code": "not_found",
            "reason": "`bash` was not found on PATH.",
        }

    try:
        proc = subprocess.run(
            [resolved, "-lc", "echo ok"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=2.0,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {
            "present": True,
            "usable": False,
            "resolved_path": resolved,
            "reason_code": "unresponsive",
            "reason": "bash probe timed out (2.0s) running `bash -lc \"echo ok\"`.",
        }
    except OSError as e:
        return {
            "present": True,
            "usable": False,
            "resolved_path": resolved,
            "reason_code": "blocked",
            "reason": f"bash probe failed: {e}",
        }

    exit_code = int(proc.returncode or 0)
    if exit_code == 0:
        return {
            "present": True,
            "usable": True,
            "resolved_path": resolved,
            "reason_code": None,
            "reason": None,
        }
    stderr = (proc.stderr or "").strip()
    return {
        "present": True,
        "usable": False,
        "resolved_path": resolved,
        "reason_code": "probe_failed",
        "reason": (
            "bash probe exited non-zero"
            + (f": {stderr}" if stderr else f" (exit_code={exit_code})")
        ),
    }


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

        ps_cmd = (
            "powershell -NoProfile -ExecutionPolicy Bypass -File .\\scripts\\smoke.ps1"
            + (" " + " ".join(switches) if switches else "")
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


def _tail_text_for_prompt(text: str, *, max_chars: int = 2000) -> str:
    cleaned = text.strip()
    if len(cleaned) <= max_chars:
        return cleaned
    return cleaned[-max_chars:]


def _run_verification_commands(
    *,
    run_dir: Path,
    attempt_number: int,
    commands: list[str],
    command_prefix: list[str],
    cwd: Path,
    timeout_seconds: float | None,
    python_executable: str | None,
) -> dict[str, Any]:
    attempt_dir_rel = Path("verification") / f"attempt{attempt_number}"
    attempt_dir = run_dir / attempt_dir_rel
    attempt_dir.mkdir(parents=True, exist_ok=True)

    started_utc = _utc_now_z()
    started_monotonic = time.monotonic()
    results: list[dict[str, Any]] = []

    is_powershell = (not command_prefix) and _is_windows()

    windows_bash_probe: dict[str, Any] | None = None
    if _is_windows() and not command_prefix:
        # Only probe when we might need to rewrite/skip bash-based commands.
        if any(
            isinstance(c, str)
            and c.strip()
            and c.strip().replace("\\", "/").lower().startswith("bash ")
            and not _looks_like_verification_rejection_sentinel(c)
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
                    decision.get("rewrite")
                    if isinstance(decision.get("rewrite"), dict)
                    else None
                )
                action = decision.get("action")
                if action == "skip":
                    stdout_text = ""
                    stderr_text = str(decision.get("reason") or "").strip() + "\n"
                    exit_code = 0
                    wall_seconds = 0.0
                    try:
                        stdout_path.write_text(stdout_text, encoding="utf-8", newline="\n")
                    except OSError:
                        pass
                    try:
                        stderr_path.write_text(stderr_text, encoding="utf-8", newline="\n")
                    except OSError:
                        pass

                    result: dict[str, Any] = {
                        "index": idx,
                        "command": cmd_original,
                        "effective_command": None,
                        "rewritten": False,
                        "argv": None,
                        "exit_code": exit_code,
                        "timed_out": False,
                        "skipped": True,
                        "skip_reason": str(decision.get("reason") or "").strip() or None,
                        "command_started_utc": _utc_now_z(),
                        "wall_seconds": wall_seconds,
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

        effective_cmd, python_rewritten = _rewrite_verification_command_for_python(
            cmd_after_bash_rewrite,
            python_executable=python_executable,
            is_powershell=is_powershell,
        )
        rewritten = bool(python_rewritten or bash_rewritten)
        rejected_sentinel = _looks_like_verification_rejection_sentinel(effective_cmd)

        cmd_started_utc = _utc_now_z()
        cmd_started_monotonic = time.monotonic()
        timed_out = False

        stdout_text = ""
        stderr_text = ""
        exit_code: int = 0
        argv: list[str] | None = None
        ripgrep_rewritten = False

        if rejected_sentinel:
            exit_code = 126
            stderr_text = (
                "[runner] Verification command dispatch blocked: received rejection sentinel "
                f"token={effective_cmd!r}.\n"
                "[runner] This indicates a tool/policy rejection was forwarded as a command.\n"
                "[runner] Fix: propagate the rejection as a structured error instead of "
                "executing it.\n"
            )
        else:
            try:
                direct = _maybe_prepare_ripgrep_direct_exec(
                    command_prefix=command_prefix,
                    command=effective_cmd,
                )
                if direct is not None:
                    prefix, inner_argv = direct
                    argv = [*prefix, *inner_argv]
                    proc = subprocess.run(
                        argv,
                        capture_output=True,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        cwd=str(cwd),
                        check=False,
                        timeout=timeout_seconds,
                    )
                    exit_code = int(proc.returncode or 0)
                    stdout_text = proc.stdout or ""
                    stderr_text = proc.stderr or ""

                    if exit_code != 0:
                        retry = _maybe_rewrite_ripgrep_unexpected_argument(
                            argv=inner_argv,
                            stderr_text=stderr_text,
                        )
                        if retry is not None:
                            rewritten_inner, rg_meta = retry
                            argv = [*prefix, *rewritten_inner]
                            proc = subprocess.run(
                                argv,
                                capture_output=True,
                                text=True,
                                encoding="utf-8",
                                errors="replace",
                                cwd=str(cwd),
                                check=False,
                                timeout=timeout_seconds,
                            )
                            exit_code = int(proc.returncode or 0)
                            stdout_text = proc.stdout or ""
                            stderr_text = proc.stderr or ""
                            ripgrep_rewritten = True
                            if rewrite_meta is None:
                                rewrite_meta = rg_meta
                            else:
                                rewrite_meta = {
                                    "kind": "multi",
                                    "rewrites": [rewrite_meta, rg_meta],
                                }
                else:
                    argv = _verification_shell_argv(
                        command_prefix=command_prefix,
                        command=effective_cmd,
                    )
                    proc = subprocess.run(
                        argv,
                        capture_output=True,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        cwd=str(cwd),
                        check=False,
                        timeout=timeout_seconds,
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
                stderr_text = (stderr_text.rstrip() + "\n" if stderr_text else "") + (
                    f"[runner] Verification command timed out after {timeout_seconds} seconds.\n"
                )

        if ripgrep_rewritten:
            rewritten = True

        wall_seconds = max(0.0, time.monotonic() - cmd_started_monotonic)
        try:
            stdout_path.write_text(stdout_text, encoding="utf-8", newline="\n")
        except OSError:
            pass
        try:
            stderr_path.write_text(stderr_text, encoding="utf-8", newline="\n")
        except OSError:
            pass

        result: dict[str, Any] = {
            "index": idx,
            "command": cmd_original,
            "effective_command": effective_cmd,
            "rewritten": rewritten,
            "argv": argv,
            "exit_code": exit_code,
            "timed_out": timed_out,
            "rejected_sentinel": rejected_sentinel,
            "command_started_utc": cmd_started_utc,
            "wall_seconds": wall_seconds,
            "stdout_path": stdout_path.name,
            "stderr_path": stderr_path.name,
            "stdout_tail": _tail_text_for_prompt(stdout_text),
            "stderr_tail": _tail_text_for_prompt(stderr_text),
            "rewrite": rewrite_meta,
        }
        results.append(result)

        if exit_code != 0:
            break

    finished_utc = _utc_now_z()
    wall_seconds_total = max(0.0, time.monotonic() - started_monotonic)
    passed = bool(results) and all(int(r.get("exit_code") or 0) == 0 for r in results)

    summary: dict[str, Any] = {
        "schema_version": 1,
        "attempt": attempt_number,
        "artifacts_dir": attempt_dir_rel.as_posix(),
        "started_utc": started_utc,
        "finished_utc": finished_utc,
        "wall_seconds": wall_seconds_total,
        "timeout_seconds": timeout_seconds,
        "python_executable": python_executable,
        "passed": passed,
        "commands": results,
    }
    _write_json(attempt_dir / "verification.json", summary)
    return summary


def _utc_now_z() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _git_diff(path: Path) -> str:
    proc = subprocess.run(
        ["git", "-C", str(path), "diff"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    return proc.stdout


def _git_numstat(path: Path) -> list[dict[str, Any]]:
    proc = subprocess.run(
        ["git", "-C", str(path), "diff", "--numstat"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    out: list[dict[str, Any]] = []
    for line in proc.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        added_s, removed_s, file_path = parts
        try:
            added = int(added_s) if added_s != "-" else 0
            removed = int(removed_s) if removed_s != "-" else 0
        except ValueError:
            continue
        out.append({"path": file_path, "lines_added": added, "lines_removed": removed})
    return out


def _git_status_porcelain(path: Path) -> str:
    proc = subprocess.run(
        ["git", "-C", str(path), "status", "--porcelain"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    return proc.stdout


def _ensure_git_user_config(path: Path) -> None:
    email = subprocess.run(
        ["git", "-C", str(path), "config", "user.email"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    ).stdout.strip()
    name = subprocess.run(
        ["git", "-C", str(path), "config", "user.name"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    ).stdout.strip()

    if not email:
        subprocess.run(
            ["git", "-C", str(path), "config", "user.email", "usertest@local"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=True,
        )
    if not name:
        subprocess.run(
            ["git", "-C", str(path), "config", "user.name", "usertest"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=True,
        )


def _maybe_commit_preprocess_workspace(path: Path, *, message: str) -> str | None:
    status = _git_status_porcelain(path)
    if not status.strip():
        return None

    _ensure_git_user_config(path)
    subprocess.run(
        ["git", "-C", str(path), "add", "-A"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(path),
            "commit",
            "--no-gpg-sign",
            "-m",
            message,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    )
    return subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    ).stdout.strip()


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

    target_slug = slugify(request.repo)
    timestamp = utc_timestamp_compact()
    run_dir = config.runs_dir / target_slug / timestamp / request.agent / str(request.seed)
    run_dir.mkdir(parents=True, exist_ok=True)

    run_start_monotonic = time.monotonic()
    run_meta: dict[str, Any] = {
        "schema_version": 1,
        "run_started_utc": _utc_now_z(),
        "phases": {},
    }
    agent_phase_start_monotonic: float | None = None
    agent_phase_end_monotonic: float | None = None
    postprocess_phase_start_monotonic: float | None = None

    workspace_id = f"{target_slug}_{timestamp}_{request.agent}_{request.seed}"
    try:
        preferred_workspace_dir = config.runs_dir / "_workspaces" / workspace_id
        acquired = acquire_target(
            repo=request.repo,
            dest_dir=preferred_workspace_dir,
            ref=request.ref,
        )

        _write_json(
            run_dir / "workspace_ref.json",
            {
                "schema_version": 1,
                "workspace_id": workspace_id,
                "workspace_dir": str(acquired.workspace_dir),
                "keep_workspace_requested": bool(request.keep_workspace),
                "will_cleanup_workspace": not (
                    request.keep_workspace or request.exec_keep_container
                ),
            },
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
            **({"model": request.model} if request.model is not None else {}),
        }
        _write_json(run_dir / "target_ref.json", target_ref)

        agent_cfg = config.agents.get(request.agent, {}) if isinstance(config.agents, dict) else {}
        agent_cfg_dict = agent_cfg if isinstance(agent_cfg, dict) else {}
        codex_binary = agent_cfg_dict.get("binary", "codex")
        codex_subcommand = agent_cfg_dict.get("subcommand", "exec")
        default_overrides: list[str] = []
        raw_defaults = agent_cfg_dict.get("config_overrides")
        if isinstance(raw_defaults, list):
            default_overrides = [x for x in raw_defaults if isinstance(x, str)]

        combined_overrides = [*default_overrides, *request.agent_config_overrides]
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
                preflight_warnings.append(
                    {
                        "code": "codex_model_messages_missing",
                        "agent": request.agent,
                        "message": personality_issue.message,
                        "hint": personality_issue.hint,
                        "details": personality_issue.details,
                    },
                )
                combined_overrides = _strip_codex_personality_overrides(list(combined_overrides))

        append_text = request.agent_append_system_prompt
        if isinstance(append_text, str) and not append_text.strip():
            append_text = None

        if request.agent == "gemini" and (
            request.agent_append_system_prompt_file is not None or append_text is not None
        ):
            if request.agent_system_prompt_file is None:
                message = (
                    "Gemini does not support appending to the system prompt; "
                    "use --agent-system-prompt-file with a merged prompt instead."
                )
                hint = (
                    "Create a single file that contains your desired base system prompt plus the "
                    "append content, then pass it via --agent-system-prompt-file (and omit "
                    "--agent-append-system-prompt*)."
                )
                _write_json(
                    run_dir / "preflight.json",
                    {
                        "warnings": preflight_warnings,
                        "agent_config_validation": {
                            "ok": False,
                            "issues": [
                                {
                                    "code": "gemini_system_prompt_append_unsupported",
                                    "message": message,
                                    "hint": hint,
                                    "details": {
                                        "agent_system_prompt_file": None,
                                        "agent_append_system_prompt": bool(append_text),
                                        "agent_append_system_prompt_file": str(
                                            request.agent_append_system_prompt_file
                                        )
                                        if request.agent_append_system_prompt_file is not None
                                        else None,
                                    },
                                }
                            ],
                        },
                    },
                )
                _write_json(
                    run_dir / "error.json",
                    {
                        "type": "AgentPreflightFailed",
                        "subtype": "unsupported_feature",
                        "code": "gemini_system_prompt_append_unsupported",
                        "agent": request.agent,
                        "message": message,
                        "hint": hint,
                    },
                )
                return RunResult(
                    run_dir=run_dir,
                    exit_code=1,
                    report_validation_errors=[
                        message,
                        "code=gemini_system_prompt_append_unsupported",
                        f"hint={hint}",
                    ],
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
                    claude_policy=claude_policy,
                    gemini_policy=gemini_policy,
                    has_outer_sandbox=True,
                )

        if bool(resolved_inputs.mission.requires_shell) and shell_status == "blocked":
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
                    f"policy '{request.policy}' for agent '{request.agent}' blocks shell commands."
                )
                hint = (
                    "Use `--policy write` (allows edits + shell)."
                    if suggested_policy == "write"
                    else "Use `--policy inspect` (read-only + shell)."
                )
                if suggested_exec_backend == "docker" and str(request.exec_backend) == "local":
                    hint = f"{hint} Also add `--exec-backend docker`."

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
                run_dir / "preflight.json",
                {
                    "warnings": preflight_warnings,
                    "capabilities": {
                        "shell_commands": {
                            "status": shell_status,
                            "reason": shell_reason,
                            "allowed_tools": allowed_tools,
                        },
                        "edits": {"allowed": bool(allow_edits)},
                    },
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
                            }
                        }
                    },
                },
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
                run_dir / "preflight.json",
                {
                    "warnings": preflight_warnings,
                    "capabilities": {
                        "shell_commands": {
                            "status": shell_status,
                            "reason": shell_reason,
                            "allowed_tools": allowed_tools,
                        },
                        "edits": {"allowed": bool(allow_edits)},
                    },
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
                        },
                        "edits": {"allowed": bool(allow_edits)},
                    },
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
                    **(
                        {"suggested_command": suggested_command}
                        if suggested_command
                        else {}
                    ),
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
        # breaks agents that interpret `--cd` literally. Keep it as a string when mounted.
        workspace_dir_for_agent: Path | str = (
            workspace_mount if workspace_mount is not None else acquired.workspace_dir
        )
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
                # Emulate append by concatenating the requested append content into the effective
                # system prompt file, then pass that file as the Gemini system prompt.
                assert staged_system_prompt is not None, (
                    "preflight should block append without base"
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

                system_prompt_path_for_agent = _agent_path_for_staged_file(
                    staged_system_prompt,
                    run_dir=run_dir,
                    run_dir_mount=backend.run_dir_mount,
                )
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

            agent_env_overrides = dict(bootstrap.env_overrides) if bootstrap is not None else None
            if os.name == "nt" and sandbox is None and bool(resolved_inputs.mission.requires_shell):
                agent_env_overrides = _ensure_windows_python_on_path(
                    workspace_dir=acquired.workspace_dir,
                    env_overrides=agent_env_overrides,
                )

            codex_sandbox_mode: str | None = None
            codex_ask_for_approval: str | None = None
            if request.agent == "codex":
                sandbox_policy_raw = codex_policy.get("sandbox", "read-only")
                sandbox_policy = (
                    str(sandbox_policy_raw)
                    if isinstance(sandbox_policy_raw, str) and sandbox_policy_raw.strip()
                    else "read-only"
                )
                if sandbox is not None and request.policy == "write":
                    sandbox_policy = "danger-full-access"
                codex_sandbox_mode = sandbox_policy

                ask_for_approval_raw = codex_policy.get("ask_for_approval", "never")
                codex_ask_for_approval = (
                    str(ask_for_approval_raw)
                    if isinstance(ask_for_approval_raw, str) and ask_for_approval_raw.strip()
                    else "never"
                )

            required_agent_binary = _agent_binary_for_preflight_probe(
                agent=request.agent,
                agent_cfg=agent_cfg_dict,
            )

            preflight_required_commands = [
                cmd.strip()
                for cmd in request.preflight_required_commands
                if isinstance(cmd, str) and cmd.strip()
            ]

            probe_commands = _build_preflight_command_list(request)
            if required_agent_binary is not None and required_agent_binary not in probe_commands:
                probe_commands.append(required_agent_binary)
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
                shell_reason = (
                    "Codex CLI command execution depends on Codex sandbox policy/approvals. "
                    "This runner can't reliably precompute allowlist outcome."
                )

            probe_details = preflight_meta.get("command_probe_details")
            probe_details_dict = probe_details if isinstance(probe_details, dict) else {}
            python_interpreter_meta = preflight_meta.get("python_interpreter")
            python_interpreter_summary = (
                python_interpreter_meta if isinstance(python_interpreter_meta, dict) else None
            )
            python_runtime = select_python_runtime(workspace_dir=acquired.workspace_dir)
            python_runtime_summary = python_runtime.to_dict()
            pip_probe: dict[str, Any] | None = None
            if python_runtime.selected is not None:
                pip_probe = probe_pip_module(
                    python_executable=python_runtime.selected.path,
                    cwd=acquired.workspace_dir,
                )
            pytest_probe: dict[str, Any] | None = None
            if (
                verification_commands_need_pytest(request.verification_commands)
                and python_runtime.selected is not None
            ):
                pytest_probe = probe_pytest_module(
                    python_executable=python_runtime.selected.path,
                    cwd=acquired.workspace_dir,
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
                    "reason": reason_s,
                    "remediation": remediation,
                }

            required_agent_binary_present = (
                preflight_commands_present.get(required_agent_binary)
                if required_agent_binary is not None
                else None
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
                    "python_runtime": python_runtime_summary,
                    "pip_probe": pip_probe,
                    "pytest_probe": pytest_probe,
                    "capabilities": {
                        "shell_commands": {
                            "status": shell_status,
                            "reason": shell_reason,
                            "allowed_tools": allowed_tools,
                        },
                        "edits": {"allowed": bool(allow_edits)},
                    },
                    "mission_requirements": {
                        "mission_id": effective_spec.mission_id,
                        "requires_shell": bool(resolved_inputs.mission.requires_shell),
                        "requires_edits": bool(resolved_inputs.mission.requires_edits),
                    },
                    "workspace_root_snapshot": preflight_workspace_snapshot,
                },
            )

            if verification_commands_need_pytest(request.verification_commands):
                if python_runtime.selected is None:
                    _write_json(
                        run_dir / "error.json",
                        {
                            "type": "AgentPreflightFailed",
                            "subtype": "python_unavailable",
                            "agent": request.agent,
                            "message": (
                                "Verification is configured to run pytest, but no usable Python "
                                "interpreter could be selected (WindowsApps/Store aliases are "
                                "rejected)."
                            ),
                            "hint": (
                                "Install a full CPython interpreter and ensure it is executable "
                                "(not a WindowsApps alias), or create a workspace `.venv` and "
                                "install deps into it."
                            ),
                            "preflight": {
                                "python_runtime": python_runtime_summary,
                                "python_interpreter": python_interpreter_summary,
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
                    not verification_commands_may_provision_pytest(request.verification_commands)
                    and not bool(pytest_probe and pytest_probe.get("passed", False))
                ):
                    remediation = (
                        pytest_probe.get("remediation")
                        if isinstance(pytest_probe, dict)
                        else None
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

            if bool(resolved_inputs.mission.requires_shell) and shell_status == "blocked":
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
                                }
                            }
                        },
                    },
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
                        suggested_command_parts.extend(
                            ["--persona-id", effective_spec.persona_id]
                        )
                    if effective_spec.mission_id:
                        suggested_command_parts.extend(
                            ["--mission-id", effective_spec.mission_id]
                        )
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
                        **(
                            {"suggested_command": suggested_command}
                            if suggested_command
                            else {}
                        ),
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
                if (
                    exec_backend == "docker"
                    and not bool(getattr(request, "exec_rebuild_image", False))
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

                version_probe = _probe_agent_cli_version(
                    binary=required_agent_binary,
                    command_prefix=command_prefix,
                    env_overrides=agent_env_overrides,
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

            verification_commands = [
                cmd.strip()
                for cmd in request.verification_commands
                if isinstance(cmd, str) and cmd.strip()
            ]
            verification_timeout_seconds = request.verification_timeout_seconds
            if (
                verification_timeout_seconds is not None
                and float(verification_timeout_seconds) <= 0.0
            ):
                verification_timeout_seconds = None

            policy_json = json.dumps(
                {
                    "agent": request.agent,
                    "policy": request.policy,
                    "allow_edits": allow_edits,
                    "exec_backend": request.exec_backend,
                    "exec_network": request.exec_network,
                    "exec_cache": request.exec_cache,
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
                        "shell": execution_shell,
                        "network": request.exec_network,
                        "cache": request.exec_cache,
                        "container_image": getattr(sandbox, "image_tag", None)
                        if sandbox is not None
                        else None,
                    },
                    "verification_gate": {
                        "configured": bool(verification_commands),
                        "commands": verification_commands,
                        "timeout_seconds": verification_timeout_seconds,
                    },
                    "preflight": {
                        "commands": preflight_commands_present,
                        "command_diagnostics": command_diagnostics,
                        "python_interpreter": python_interpreter_summary,
                        "python_runtime": python_runtime_summary,
                        "pip_probe": pip_probe,
                        "pytest_probe": pytest_probe,
                        "meta": preflight_meta,
                        "probe_commands": effective_probe_commands,
                        "capabilities": {
                            "shell_commands": {
                                "status": shell_status,
                                "reason": shell_reason,
                                "allowed_tools": allowed_tools,
                            },
                            "edits": {"allowed": bool(allow_edits)},
                        },
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
                shell_status=shell_status,
                python_runtime_summary=python_runtime_summary,
                pip_probe=pip_probe,
                pytest_probe=pytest_probe,
                command_diagnostics=command_diagnostics,
                verification_commands=verification_commands,
                verification_timeout_seconds=verification_timeout_seconds,
                agent=request.agent,
                codex_sandbox_mode=codex_sandbox_mode,
            )

            try:
                prompt = build_prompt_from_template(
                    template_text=effective_spec.prompt_template_text,
                    variables={
                        "persona_name": effective_spec.persona_name,
                        "persona_md": effective_spec.persona_md_resolved,
                        "mission_name": effective_spec.mission_name,
                        "mission_md": effective_spec.mission_md_resolved,
                        "users_md": users_md_text,
                        "policy_json": policy_json,
                        "preflight_summary_md": preflight_summary_md,
                        "environment_json": environment_json,
                        "report_schema_json": report_schema_json,
                    },
                )
            except TemplateSubstitutionError as e:
                template_path = effective_spec.prompt_template_path
                raise TemplateSubstitutionError(
                    f"Prompt template substitution failed for {template_path}:\n{e}"
                ) from e
            (run_dir / "prompt.txt").write_text(prompt, encoding="utf-8")

            codex_overrides = list(combined_overrides)
            if system_prompt_path_for_agent is not None:
                codex_overrides.append(
                    "model_instructions_file=" + toml_basic_string(system_prompt_path_for_agent)
                )
            if staged_append_system_prompt is not None:
                try:
                    dev_text = staged_append_system_prompt.read_text(encoding="utf-8")
                except OSError:
                    dev_text = ""
                if dev_text.strip():
                    codex_overrides.append("developer_instructions=" + toml_basic_string(dev_text))

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
                        model=request.model,
                        config_overrides=codex_overrides,
                        command_prefix=command_prefix,
                        env_overrides=agent_env_overrides,
                        agent_last_message_path=codex_last_message_for_attempt,
                    )
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
                        model=request.model,
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
                    model=request.model,
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
                        "commands": verification_commands,
                        "timeout_seconds": verification_timeout_seconds,
                    },
                )

            current_prompt = prompt
            rate_limit_retry_count = 0
            followup_count = 0
            attempts_meta: list[dict[str, Any]] = []
            selected_raw_events_path = raw_events_path
            selected_raw_events_ts_path = raw_events_ts_path
            selected_last_message_path = last_message_path
            selected_stderr_path = stderr_path
            selected_stderr_text = ""
            selected_last_message_text = ""
            selected_verification_summary_path: Path | None = None
            selected_verification_errors: list[str] = []
            verification_seconds_total = 0.0
            report_json = None
            report_validation_errors = []

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
                agent_exec_start_monotonic = time.monotonic()
                agent_exit_code, agent_argv = _run_agent_attempt(
                    prompt_text=current_prompt,
                    raw_events_attempt_path=raw_events_attempt_path,
                    last_message_attempt_path=last_message_attempt_path,
                    stderr_attempt_path=stderr_attempt_path,
                )
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
                codex_personality_warning_detected = bool(
                    request.agent == "codex"
                    and _CODEX_PERSONALITY_MISSING_MESSAGES_WARNING in raw_attempt_stderr_text
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

                _sanitize_agent_stderr_file(agent=request.agent, path=stderr_attempt_path)

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
                attempt_last_text = ""
                if last_message_attempt_path.exists():
                    try:
                        attempt_last_text = last_message_attempt_path.read_text(encoding="utf-8")
                    except OSError:
                        attempt_last_text = ""
                if agent_exit_code == 0:
                    try:
                        attempt_report_json = _extract_json_object(attempt_last_text)
                    except Exception as e:  # noqa: BLE001
                        attempt_report_validation_errors = [
                            f"$: failed to parse JSON from agent output: {e}"
                        ]
                    if attempt_report_json is not None:
                        attempt_report_validation_errors = validate_report(
                            attempt_report_json, effective_spec.report_schema_dict
                        )

                attempt_verification_summary: dict[str, Any] | None = None
                attempt_verification_passed = True
                attempt_verification_errors: list[str] = []
                attempt_verification_summary_path: Path | None = None
                if (
                    agent_exit_code == 0
                    and not attempt_report_validation_errors
                    and verification_commands
                ):
                    python_exec_for_verification: str | None = None
                    if not command_prefix:
                        selection = select_python_runtime(workspace_dir=acquired.workspace_dir)
                        if selection.selected is not None and selection.selected.path.strip():
                            python_exec_for_verification = selection.selected.path

                    attempt_verification_summary = _run_verification_commands(
                        run_dir=run_dir,
                        attempt_number=attempt_number,
                        commands=verification_commands,
                        command_prefix=command_prefix,
                        cwd=acquired.workspace_dir,
                        timeout_seconds=verification_timeout_seconds,
                        python_executable=python_exec_for_verification,
                    )
                    attempt_verification_passed = bool(
                        attempt_verification_summary.get("passed", False)
                    )
                    wall_seconds = attempt_verification_summary.get("wall_seconds")
                    if isinstance(wall_seconds, (int, float)):
                        verification_seconds_total += max(0.0, float(wall_seconds))

                    artifacts_dir = attempt_verification_summary.get("artifacts_dir")
                    if isinstance(artifacts_dir, str) and artifacts_dir.strip():
                        attempt_verification_summary_path = (
                            run_dir / Path(artifacts_dir) / "verification.json"
                        )

                    if not attempt_verification_passed:
                        attempt_verification_errors = [
                            "verification_failed",
                            f"artifacts_dir={artifacts_dir}",
                        ]
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
                        )
                        if value
                    ]
                )
                failure_subtype = _classify_failure_subtype(failure_text)
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
                    "failure_subtype": failure_subtype,
                    "report_validation_errors": attempt_report_validation_errors,
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
                                    else ("passed" if attempt_verification_passed else "failed")
                                )
                            )
                        ),
                        "passed": attempt_verification_passed if verification_commands else None,
                        "summary_path": verification_summary_path,
                    },
                    "raw_events_path": raw_events_attempt_path.name,
                    "last_message_path": last_message_attempt_path.name,
                    "stderr_path": stderr_attempt_path.name,
                }
                attempts_meta.append(attempt_meta)

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
                selected_verification_summary_path = attempt_verification_summary_path
                selected_verification_errors = list(attempt_verification_errors)
                report_json = attempt_report_json
                report_validation_errors = attempt_report_validation_errors
                break

            _write_json(
                run_dir / "agent_attempts.json",
                {
                    "attempts": attempts_meta,
                    "rate_limit_retries_configured": rate_limit_retries,
                    "rate_limit_retries_used": rate_limit_retry_count,
                    "followup_attempts_configured": followup_attempts,
                    "followup_attempts_used": followup_count,
                },
            )

            def _materialize_attempt_artifact(
                src: Path,
                dst: Path,
                *,
                fallback_text: str | None = None,
            ) -> None:
                if src == dst:
                    return
                if src.exists():
                    shutil.copyfile(src, dst)
                    return
                try:
                    dst.write_text(fallback_text or "", encoding="utf-8")
                except OSError:
                    return

            _materialize_attempt_artifact(selected_raw_events_path, raw_events_path)
            _materialize_attempt_artifact(selected_raw_events_ts_path, raw_events_ts_path)
            _materialize_attempt_artifact(
                selected_last_message_path,
                last_message_path,
                fallback_text=selected_last_message_text,
            )
            _materialize_attempt_artifact(
                selected_stderr_path,
                stderr_path,
                fallback_text=selected_stderr_text,
            )

            if selected_verification_summary_path is not None:
                _materialize_attempt_artifact(
                    selected_verification_summary_path,
                    run_dir / "verification.json",
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
                _write_json(
                    run_dir / "verification.json",
                    {
                        "schema_version": 1,
                        "status": status_s,
                        "skipped": True,
                        "skip_reason": skip_reason,
                        "attempt_number": len(attempts_meta),
                        "commands_configured": verification_commands,
                    },
                )
            phases = run_meta.get("phases")
            if isinstance(phases, dict) and verification_commands:
                phases["verification_seconds"] = max(0.0, float(verification_seconds_total))

            if agent_exit_code != 0 and not report_validation_errors:
                if selected_stderr_text:
                    report_validation_errors = selected_stderr_text.splitlines()[:20]
                elif selected_last_message_text.strip():
                    report_validation_errors = selected_last_message_text.strip().splitlines()[:20]
                else:
                    report_validation_errors = [
                        f"{request.agent} exited with code {agent_exit_code}"
                    ]
            if not report_validation_errors and selected_verification_errors:
                report_validation_errors = selected_verification_errors
                _write_json(
                    run_dir / "verification_errors.json",
                    {
                        "schema_version": 1,
                        "errors": selected_verification_errors,
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

            last_message_excerpt = last_message_text
            last_message_truncated = False
            if len(last_message_excerpt) > 4000:
                last_message_excerpt = last_message_excerpt[:4000] + "\n...[truncated]..."
                last_message_truncated = True

            combined_text = "\n".join([x for x in (stderr_text, last_message_text) if x])
            failure_subtype = _classify_failure_subtype(combined_text)
            if (
                stderr_text
                and failure_subtype in {"provider_capacity", "transient_network"}
                and "[runner_retry_summary]" not in stderr_text
            ):
                retryable = True
                if failure_subtype == "provider_capacity":
                    retryable = _is_retryable_provider_capacity_failure(combined_text)
                elif failure_subtype == "transient_network":
                    retryable = _is_retryable_transient_network_failure(combined_text)

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

            if not stderr_text and quota_exhaustion is not None and last_message_text.strip():
                stderr_text = _format_claude_quota_exhaustion_stderr(
                    provider_message=last_message_text,
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
                    "provider_message": last_message_text.strip() or stderr_text.strip(),
                    "reset_time": {
                        "raw": quota_exhaustion.get("reset_raw"),
                        "timezone": quota_exhaustion.get("reset_timezone"),
                    },
                }

            _write_json(run_dir / "error.json", error_payload)

        normalized_events_path = run_dir / "normalized_events.jsonl"
        raw_ts_f = None
        raw_ts_iter = None
        if raw_events_ts_path.exists():
            try:
                raw_ts_f = raw_events_ts_path.open("r", encoding="utf-8")
                raw_ts_iter = (line.strip() for line in raw_ts_f if line.strip())
            except OSError:
                raw_ts_f = None
                raw_ts_iter = None

        try:
            if request.agent == "codex":
                normalize_codex_events(
                    raw_events_path=raw_events_path,
                    normalized_events_path=normalized_events_path,
                    raw_ts_iter=raw_ts_iter,
                    workspace_root=acquired.workspace_dir,
                    workspace_mount=workspace_mount,
                )
            elif request.agent == "claude":
                normalize_claude_events(
                    raw_events_path=raw_events_path,
                    normalized_events_path=normalized_events_path,
                    raw_ts_iter=raw_ts_iter,
                    workspace_root=acquired.workspace_dir,
                    workspace_mount=workspace_mount,
                )
            else:
                normalize_gemini_events(
                    raw_events_path=raw_events_path,
                    normalized_events_path=normalized_events_path,
                    raw_ts_iter=raw_ts_iter,
                    workspace_root=acquired.workspace_dir,
                    workspace_mount=workspace_mount,
                )
        finally:
            if raw_ts_f is not None:
                raw_ts_f.close()

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
            _write_json(run_dir / "report.json", report_json)
        elif agent_exit_code != 0 and not report_validation_errors:
            report_validation_errors = run_errors

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

        return RunResult(
            run_dir=run_dir,
            exit_code=agent_exit_code,
            report_validation_errors=report_validation_errors,
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
                "Common causes on Windows: invalid filename characters (< > : \" / \\\\ | ? *), "
                "overly long paths, or output streams that reject writes. "
                "See error_traceback.txt for the failing operation."
            )
            if "hint" in extra and isinstance(extra["hint"], str) and extra["hint"].strip():
                extra["hint"] = extra["hint"].strip() + "\n" + derived_hint
            else:
                extra["hint"] = derived_hint
            user_errors.append(f"hint={derived_hint}")
        _write_json(
            run_dir / "error.json",
            {
                "type": type(e).__name__,
                "message": message,
                **({"subtype": subtype} if subtype is not None else {}),
                **extra,
            },
        )
        return RunResult(run_dir=run_dir, exit_code=1, report_validation_errors=user_errors)
    finally:
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
            and not (request.keep_workspace or request.exec_keep_container)
            and acquired.workspace_dir.exists()
        ):
            cleanup_wall_start = time.monotonic()
            shutil.rmtree(acquired.workspace_dir, ignore_errors=True)
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
