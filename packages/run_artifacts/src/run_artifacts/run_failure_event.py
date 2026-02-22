from __future__ import annotations

import json
from typing import Any

MAX_ATTACHMENT_EXCERPT_CHARS = 1_200
MAX_ERROR_FALLBACK_CHARS = 2_000

_SHELL_SNAPSHOT_WARNING_CODE = "shell_snapshot_powershell_unsupported"
_TURN_METADATA_TIMEOUT_WARNING_CODE = "turn_metadata_header_timeout"
_CODEX_MODEL_REFRESH_TIMEOUT_WARNING_CODE = "codex_model_refresh_timeout"
_BASH_TOOL_PREFLIGHT_SLOW_WARNING_CODE = "bash_tool_preflight_slow"

_SHELL_SNAPSHOT_WARNING_HINT = (
    "hint=PowerShell shell snapshot unsupported; continuing without shell snapshot metadata."
)
_TURN_METADATA_TIMEOUT_HINT = (
    "hint=Turn metadata header timed out; continuing without metadata header."
)
_CODEX_MODEL_REFRESH_TIMEOUT_HINT = (
    "hint=Codex model refresh timed out; model list may be stale."
)


def _truncate_text(text: str, *, max_chars: int, marker: str) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + marker


def coerce_validation_errors(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            continue
        cleaned = item.strip()
        if not cleaned:
            continue
        out.append(cleaned)
    return out


def sanitize_error(error_obj: Any) -> dict[str, Any] | None:
    if not isinstance(error_obj, dict):
        return None
    # Preserve the parsed error object without truncation. Prompt/display layers
    # should apply excerpting if needed, but the atom should not drop data.
    return dict(error_obj)


def extract_error_artifacts(error: dict[str, Any] | None) -> dict[str, Any] | None:
    if error is None:
        return None
    artifacts = error.get("artifacts")
    if not isinstance(artifacts, dict):
        return None
    return dict(artifacts)


def extract_artifact_filenames(artifacts: dict[str, Any] | None) -> list[str]:
    if artifacts is None:
        return []
    out: list[str] = []
    for value in artifacts.values():
        if isinstance(value, str) and value.strip():
            out.append(value.strip())
    return sorted(set(out))


def classify_failure_kind(
    *,
    status: str,
    error: dict[str, Any] | None,
    validation_errors: list[str],
) -> tuple[bool, str]:
    status_lower = status.strip().lower() if isinstance(status, str) else ""
    if error is not None:
        return True, "error"
    if validation_errors:
        return True, "report_validation_error"
    if status_lower in {"error", "report_validation_error", "missing_report"}:
        return True, status_lower
    return False, "unknown"


def classify_known_stderr_warnings(text: str) -> dict[str, Any]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    counts: dict[str, int] = {
        _SHELL_SNAPSHOT_WARNING_CODE: 0,
        _TURN_METADATA_TIMEOUT_WARNING_CODE: 0,
        _CODEX_MODEL_REFRESH_TIMEOUT_WARNING_CODE: 0,
        _BASH_TOOL_PREFLIGHT_SLOW_WARNING_CODE: 0,
    }
    unknown_lines: list[str] = []

    for line in lines:
        lowered = line.lower()
        if (
            "shell snapshot not supported yet for powershell" in lowered
            or f"code={_SHELL_SNAPSHOT_WARNING_CODE}" in lowered
            or lowered == _SHELL_SNAPSHOT_WARNING_HINT.lower()
        ):
            counts[_SHELL_SNAPSHOT_WARNING_CODE] += 1
            continue

        if (
            ("turn metadata" in lowered and "timed out" in lowered and "header" in lowered)
            or f"code={_TURN_METADATA_TIMEOUT_WARNING_CODE}" in lowered
            or lowered == _TURN_METADATA_TIMEOUT_HINT.lower()
        ):
            counts[_TURN_METADATA_TIMEOUT_WARNING_CODE] += 1
            continue

        if (
            (
                "failed to refresh available models" in lowered
                and "timeout waiting for child process" in lowered
            )
            or f"code={_CODEX_MODEL_REFRESH_TIMEOUT_WARNING_CODE}" in lowered
            or lowered == _CODEX_MODEL_REFRESH_TIMEOUT_HINT.lower()
        ):
            counts[_CODEX_MODEL_REFRESH_TIMEOUT_WARNING_CODE] += 1
            continue

        if "[bashtool]" in lowered and "pre-flight check is taking longer than expected" in lowered:
            counts[_BASH_TOOL_PREFLIGHT_SLOW_WARNING_CODE] += 1
            continue

        unknown_lines.append(line)

    active_counts = {code: count for code, count in counts.items() if count > 0}
    codes = sorted(active_counts)
    return {
        "codes": codes,
        "counts": active_counts,
        "unknown_lines": unknown_lines,
        "warning_only": bool(codes) and not unknown_lines,
    }


def render_failure_text(
    *,
    failure_kind: str,
    agent: str,
    status: str,
    error: dict[str, Any] | None,
    report_validation_errors: list[str],
    artifacts: dict[str, Any] | None,
    attachments: list[dict[str, Any]] | None = None,
) -> str:
    lines: list[str] = [
        f"Run failure: {failure_kind} (agent={agent} status={status})",
    ]

    if error is not None:
        added_error_details = False
        parts: list[str] = []
        err_type = error.get("type")
        if isinstance(err_type, str) and err_type.strip():
            parts.append(f"type={err_type.strip()}")
        exit_code = error.get("exit_code")
        if isinstance(exit_code, int):
            parts.append(f"exit_code={exit_code}")
        stderr_synth = error.get("stderr_synthesized")
        if isinstance(stderr_synth, bool):
            parts.append(f"stderr_synthesized={stderr_synth}")
        if parts:
            lines.append(" ".join(parts))
            added_error_details = True

        stderr = error.get("stderr")
        if isinstance(stderr, str) and stderr.strip():
            lines.append("stderr:")
            lines.append(stderr.strip())
            added_error_details = True
        else:
            message = error.get("message")
            if isinstance(message, str) and message.strip():
                lines.append("message:")
                lines.append(message.strip())
                added_error_details = True

        if not added_error_details:
            lines.append("error:")
            try:
                blob = json.dumps(error, ensure_ascii=False, sort_keys=True)
            except Exception:  # noqa: BLE001
                blob = str(error)
            lines.append(
                _truncate_text(
                    blob,
                    max_chars=MAX_ERROR_FALLBACK_CHARS,
                    marker="\n...[truncated_error_json]...",
                )
            )

    if report_validation_errors:
        lines.append("report_validation_errors:")
        for value in report_validation_errors:
            lines.append(f"- {value}")

    filenames = extract_artifact_filenames(artifacts)
    if filenames:
        lines.append(f"artifacts: {', '.join(filenames)}")

    if attachments:
        by_path: dict[str, dict[str, Any]] = {}
        for item in attachments:
            path = item.get("path")
            if isinstance(path, str) and path.strip():
                by_path[path.strip()] = item
        for path in ("agent_stderr.txt", "agent_last_message.txt"):
            entry = by_path.get(path)
            if entry is None:
                continue
            excerpt_head = entry.get("excerpt_head")
            if isinstance(excerpt_head, str) and excerpt_head.strip():
                lines.append(f"{path} excerpt:")
                lines.append(
                    _truncate_text(
                        excerpt_head.strip(),
                        max_chars=MAX_ATTACHMENT_EXCERPT_CHARS,
                        marker="\n...[truncated_attachment_excerpt]...",
                    )
                )
                continue
            capture_error = entry.get("capture_error")
            if isinstance(capture_error, str) and capture_error.strip():
                lines.append(f"{path} capture_error: {capture_error.strip()}")

    return "\n".join(lines).strip()
