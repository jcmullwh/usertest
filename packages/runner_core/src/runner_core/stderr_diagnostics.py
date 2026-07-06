from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from runner_core.artifacts import _read_tail_text

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
        "tool_use_id_collision",
        (
            re.compile(r"`tool_use`\s+ids\s+must\s+be\s+unique", re.IGNORECASE),
            re.compile(r"tool_use\s+ids\s+must\s+be\s+unique", re.IGNORECASE),
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
_CODEX_EMPTY_OVERRIDE_VALUES = frozenset({"", "[]", "{}", "''", '""'})
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
_RAW_EVENTS_PLAINTEXT_EXCERPT_TAIL_BYTES = 24_000
_RAW_EVENTS_PLAINTEXT_EXCERPT_MAX_CHARS = 4_000


def _new_codex_metadata_capture_summary() -> dict[str, Any]:
    return {
        "shell_snapshot": {
            "warning_code": _CODEX_SHELL_SNAPSHOT_WARNING_CODE,
            "warning_occurrences": 0,
            "missing": False,
            "attempts_missing": [],
        },
        "turn_metadata_header": {
            "warning_code": _CODEX_TURN_METADATA_TIMEOUT_CODE,
            "warning_occurrences": 0,
            "missing": False,
            "attempts_missing": [],
        },
    }


def _codex_metadata_capture_from_stderr(stderr_text: str) -> dict[str, Any]:
    shell_snapshot_warning_occurrences = 0
    turn_metadata_header_warning_occurrences = 0
    for line in stderr_text.splitlines():
        lowered = line.lower()
        if _CODEX_SHELL_SNAPSHOT_WARNING.lower() in lowered:
            shell_snapshot_warning_occurrences += 1
        if "turn metadata" in lowered and "timed out" in lowered and "header" in lowered:
            turn_metadata_header_warning_occurrences += 1

    return {
        "shell_snapshot": {
            "warning_code": _CODEX_SHELL_SNAPSHOT_WARNING_CODE,
            "warning_occurrences": shell_snapshot_warning_occurrences,
            "missing": shell_snapshot_warning_occurrences > 0,
        },
        "turn_metadata_header": {
            "warning_code": _CODEX_TURN_METADATA_TIMEOUT_CODE,
            "warning_occurrences": turn_metadata_header_warning_occurrences,
            "missing": turn_metadata_header_warning_occurrences > 0,
        },
    }


def _merge_codex_metadata_capture_summary(
    *,
    summary: dict[str, Any],
    attempt_metadata: dict[str, Any],
    attempt_number: int,
) -> None:
    for key in ("shell_snapshot", "turn_metadata_header"):
        section = summary.get(key)
        attempt_section = attempt_metadata.get(key)
        if not isinstance(section, dict) or not isinstance(attempt_section, dict):
            continue
        raw_occurrences = attempt_section.get("warning_occurrences")
        occurrences = raw_occurrences if isinstance(raw_occurrences, int) else 0
        if occurrences <= 0:
            continue
        section["missing"] = True
        section["warning_occurrences"] = int(section.get("warning_occurrences", 0)) + occurrences
        attempts_missing = section.get("attempts_missing")
        if not isinstance(attempts_missing, list):
            attempts_missing = []
            section["attempts_missing"] = attempts_missing
        if attempt_number not in attempts_missing:
            attempts_missing.append(attempt_number)


def _extract_raw_events_plaintext_excerpt(raw_events_path: Path) -> str:
    tail = _read_tail_text(raw_events_path, max_bytes=_RAW_EVENTS_PLAINTEXT_EXCERPT_TAIL_BYTES)
    if not tail.strip():
        return ""

    kept: list[str] = []
    for line in tail.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            kept.append(stripped)
            continue
        if isinstance(parsed, dict):
            continue
        kept.append(stripped)

    if not kept:
        return ""
    text = "\n".join(kept).strip()
    if len(text) > _RAW_EVENTS_PLAINTEXT_EXCERPT_MAX_CHARS:
        text = text[-_RAW_EVENTS_PLAINTEXT_EXCERPT_MAX_CHARS:]
    return text


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


def _sanitize_agent_stderr_text(
    *,
    agent: str,
    text: str,
    codex_personality_warning_as_error: bool = True,
) -> str:
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
                saw_personality_warning = True
                continue
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
                        "[codex_notice_summary] "
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
        if saw_personality_warning:
            if codex_personality_warning_as_error:
                lines.extend(
                    [
                        (
                            "[codex_error_hint] code=codex_model_messages_missing "
                            "classification=invalid_agent_config"
                        ),
                        (
                            "hint=Codex personality/model_personality was requested but "
                            "model_messages is missing. Fix your Codex config overrides "
                            "(configs/agents.yaml or --agent-config) by providing model_messages "
                            "alongside personality."
                        ),
                    ]
                )
            else:
                lines.extend(
                    [
                        (
                            "[codex_warning_summary] code=codex_model_messages_missing "
                            "classification=runtime_notice"
                        ),
                        (
                            "hint=Codex runtime emitted a personality/model_messages warning, "
                            "but this run did not request a repo-owned personality override."
                        ),
                    ]
                )
        return "\n".join(lines)

    return text


def _sanitize_agent_stderr_file(
    *,
    agent: str,
    path: Path,
    codex_personality_warning_as_error: bool = True,
) -> None:
    if agent not in {"gemini", "codex", "claude"} or not path.exists():
        return
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    sanitized = _sanitize_agent_stderr_text(
        agent=agent,
        text=raw,
        codex_personality_warning_as_error=codex_personality_warning_as_error,
    )
    if sanitized == raw:
        return
    try:
        path.write_text(sanitized, encoding="utf-8")
    except OSError:
        return


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


def _is_retryable_tool_use_id_collision_failure(text: str) -> bool:
    return bool(text.strip())


__all__ = (
    "_classify_failure_subtype",
    "_codex_metadata_capture_from_stderr",
    "_extract_claude_quota_exhaustion",
    "_extract_raw_events_plaintext_excerpt",
    "_format_claude_quota_exhaustion_stderr",
    "_is_retryable_provider_capacity_failure",
    "_is_retryable_tool_use_id_collision_failure",
    "_is_retryable_transient_network_failure",
    "_merge_codex_metadata_capture_summary",
    "_new_codex_metadata_capture_summary",
    "_sanitize_agent_stderr_file",
    "_sanitize_agent_stderr_text",
)
