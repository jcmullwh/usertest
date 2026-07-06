from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator


def load_schema(path: Path) -> dict[str, Any]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"Schema must be a JSON object: {path}")
    return raw


def validate_report(
    report: Any,
    schema: dict[str, Any],
    *,
    require_shell_capability: bool = False,
) -> list[str]:
    validator = Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(report), key=lambda e: str(e.path))
    formatted: list[str] = []
    for error in errors:
        path = "$"
        for part in error.path:
            path += f"[{part!r}]" if isinstance(part, int) else f".{part}"
        formatted.append(f"{path}: {error.message}")
    if require_shell_capability:
        formatted.extend(_validate_shell_capability_extension(report))
    return formatted


def _validate_shell_capability_extension(report: Any) -> list[str]:
    if not isinstance(report, dict):
        return ["$.extensions.shell_capability: required for shell-backend preflight reports"]
    extensions = report.get("extensions")
    if not isinstance(extensions, dict):
        return ["$.extensions.shell_capability: required for shell-backend preflight reports"]
    capability = extensions.get("shell_capability")
    if not isinstance(capability, dict):
        return ["$.extensions.shell_capability: required for shell-backend preflight reports"]

    errors: list[str] = []
    state = capability.get("state")
    if state not in {"available", "blocked", "unprobed"}:
        errors.append(
            "$.extensions.shell_capability.state: must be one of "
            "'available', 'blocked', or 'unprobed'"
        )
    for key in ("agent", "operating_system", "backend", "probe_status", "reason"):
        value = capability.get(key)
        if not isinstance(value, str) or not value.strip():
            errors.append(f"$.extensions.shell_capability.{key}: non-empty string required")
    if state in {"blocked", "unprobed"}:
        reason_code = capability.get("reason_code")
        if not isinstance(reason_code, str) or not reason_code.strip():
            errors.append(
                "$.extensions.shell_capability.reason_code: non-empty string required "
                "when shell capability is not available"
            )
    return errors
