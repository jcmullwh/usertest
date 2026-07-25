from __future__ import annotations

import json
from hashlib import sha256
from json import JSONDecoder
from pathlib import Path
from typing import Any


def _read_tail_text(path: Path, *, max_bytes: int) -> str:
    try:
        size = path.stat().st_size
    except OSError:
        return ""
    if size <= 0:
        return ""
    offset = max(0, size - max(1, int(max_bytes)))
    try:
        with path.open("rb") as f:
            f.seek(offset)
            data = f.read()
    except OSError:
        return ""
    if not data:
        return ""
    return data.decode("utf-8", errors="replace")


def _eof_delimiter_completion(text: str) -> str | None:
    """Return the uniquely implied closing delimiters for a truncated JSON container."""

    stack: list[str] = []
    in_string = False
    escaped = False
    for char in text:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in "{[":
            stack.append(char)
        elif char in "}]":
            expected = "{" if char == "}" else "["
            if not stack or stack.pop() != expected:
                return None
    if in_string or not stack:
        return None
    return "".join("}" if opener == "{" else "]" for opener in reversed(stack))


def _extract_json_object_with_receipt(
    text: str,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
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
        return parsed, None

    if cleaned.startswith("{"):
        appended = _eof_delimiter_completion(cleaned)
        if appended is not None:
            repaired_text = cleaned + appended
            try:
                repaired = json.loads(repaired_text)
            except (TypeError, json.JSONDecodeError):
                repaired = None
            if isinstance(repaired, dict):
                return repaired, {
                    "repair_kind": "append_missing_eof_delimiters",
                    "appended_delimiters": appended,
                    "appended_delimiter_count": len(appended),
                    "raw_sha256": sha256(cleaned.encode("utf-8")).hexdigest(),
                    "repaired_sha256": sha256(repaired_text.encode("utf-8")).hexdigest(),
                }
        # A malformed top-level object must not be replaced by the first valid
        # nested object. That would turn a one-character syntax defect into
        # misleading root-schema errors and an unnecessary model correction.
        raise ValueError("Top-level JSON object is malformed or truncated.")

    decoder = JSONDecoder()
    for idx, char in enumerate(cleaned):
        if char != "{":
            continue
        try:
            parsed_obj, _ = decoder.raw_decode(cleaned[idx:])
        except Exception:  # noqa: BLE001
            continue
        if isinstance(parsed_obj, dict):
            return parsed_obj, None

    raise ValueError("Could not find a JSON object in agent output.")


def _extract_json_object(text: str) -> dict[str, Any]:
    parsed, _receipt = _extract_json_object_with_receipt(text)
    return parsed


def _tail_text_for_prompt(text: str, *, max_chars: int = 2000) -> str:
    cleaned = text.strip()
    if len(cleaned) <= max_chars:
        return cleaned
    return cleaned[-max_chars:]


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
        newline="\n",
    )


__all__ = (
    "_extract_json_object",
    "_extract_json_object_with_receipt",
    "_read_tail_text",
    "_tail_text_for_prompt",
    "_write_json",
)
