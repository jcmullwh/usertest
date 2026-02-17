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


def validate_report(report: Any, schema: dict[str, Any]) -> list[str]:
    validator = Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(report), key=lambda e: str(e.path))
    formatted: list[str] = []
    for error in errors:
        path = "$"
        for part in error.path:
            path += f"[{part!r}]" if isinstance(part, int) else f".{part}"
        formatted.append(f"{path}: {error.message}")
    return formatted
