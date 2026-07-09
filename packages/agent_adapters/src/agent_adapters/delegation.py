from __future__ import annotations

import re
from typing import Any

TOKEN_DIMENSION_KEYS: tuple[str, ...] = (
    "total_tokens",
    "input_tokens",
    "cached_input_tokens",
    "uncached_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
)

_DELEGATION_TOOL_NAMES = {
    "agent",
    "invoke_agent",
    "spawn_agent",
    "delegate",
    "delegation",
    "subagent",
    "sub_agent",
    "task",
}
_DELEGATION_TOOL_SUFFIXES = (
    ".agent",
    ".invoke_agent",
    ".spawn_agent",
    "__agent",
    "__invoke_agent",
    "__spawn_agent",
)
_SUMMARY_WORDS = re.compile(
    r"(?i)\b(summary|findings|recommend(?:ed|ation)|next steps?|risk|paths?)\b"
)
_SOURCE_LINE_RE = re.compile(
    r"""(?x)
    ^\s*(?:
        \d+[:|\t ]+
        |(?:def|class|import|from|function|const|let|var|export|interface|type)\s+
        |[{}()[\];]\s*$
        |(?:\#|//|/\*|\*|<!--)
        |[A-Za-z0-9_./\\-]+\.(?:py|ts|tsx|js|jsx|json|ya?ml|toml|md|sh|ps1):\d+
    )
    """
)
_PATH_RE = re.compile(
    r"(?i)(?:^|\s)(?:[\w./\\-]+\.(?:py|ts|tsx|js|jsx|json|ya?ml|toml|md|sh|ps1))"
    r"(?:[:\s]|$)"
)
_RAW_LEAK_CHARS_MIN = 16_000
_RAW_LEAK_LINES_MIN = 240
_RAW_LEAK_SOURCE_LINES_MIN = 40


def canonical_tool_name(raw: Any) -> str:
    if not isinstance(raw, str):
        return ""
    return raw.strip().lower().replace("-", "_")


def is_delegation_tool(raw: Any) -> bool:
    name = canonical_tool_name(raw)
    if not name:
        return False
    if name in _DELEGATION_TOOL_NAMES:
        return True
    if name.endswith(_DELEGATION_TOOL_SUFFIXES):
        return True
    tail = re.split(r"[.:/]", name)[-1]
    if tail in _DELEGATION_TOOL_NAMES:
        return True
    return "invoke_agent" in name or "spawn_agent" in name or "subagent" in name


def coerce_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        chunks = [coerce_text(item) for item in value]
        return "\n".join(chunk for chunk in chunks if chunk)
    if isinstance(value, dict):
        for key in (
            "text",
            "content",
            "output",
            "result",
            "summary",
            "message",
            "stdout",
            "stderr",
        ):
            text = coerce_text(value.get(key))
            if text:
                return text
    return ""


def token_usage_from_mapping(raw: Any) -> dict[str, int]:
    out = {key: 0 for key in TOKEN_DIMENSION_KEYS}
    if not isinstance(raw, dict):
        return out
    aliases = {
        "input_tokens": ("input_tokens", "prompt_tokens", "promptTokenCount"),
        "output_tokens": ("output_tokens", "completion_tokens", "candidatesTokenCount"),
        "reasoning_output_tokens": ("reasoning_output_tokens", "thoughtsTokenCount"),
        "cached_input_tokens": ("cached_input_tokens", "cachedPromptTokenCount"),
        "total_tokens": ("total_tokens", "totalTokens", "totalTokenCount"),
    }
    for target, keys in aliases.items():
        for key in keys:
            value = raw.get(key)
            if isinstance(value, int):
                out[target] = value
                break
            if isinstance(value, float) and value.is_integer():
                out[target] = int(value)
                break
    if out["total_tokens"] == 0:
        out["total_tokens"] = (
            out["input_tokens"] + out["output_tokens"] + out["reasoning_output_tokens"]
        )
    out["uncached_input_tokens"] = max(0, out["input_tokens"] - out["cached_input_tokens"])
    return out


def extract_token_usage(*values: Any) -> dict[str, int]:
    for value in values:
        usage = _extract_token_usage(value, depth=0)
        if any(usage.values()):
            return usage
    return {key: 0 for key in TOKEN_DIMENSION_KEYS}


def _extract_token_usage(value: Any, *, depth: int) -> dict[str, int]:
    if depth > 4:
        return {key: 0 for key in TOKEN_DIMENSION_KEYS}
    if not isinstance(value, dict):
        return {key: 0 for key in TOKEN_DIMENSION_KEYS}
    direct = token_usage_from_mapping(value)
    if any(direct.values()):
        return direct
    for key in ("usage", "token_usage", "tokens", "usageMetadata", "metadata", "stats"):
        nested = value.get(key)
        if isinstance(nested, dict):
            usage = _extract_token_usage(nested, depth=depth + 1)
            if any(usage.values()):
                return usage
    return {key: 0 for key in TOKEN_DIMENSION_KEYS}


def classify_delegation_result(text: str, *, is_error: bool = False) -> dict[str, Any]:
    if is_error:
        kind = "error"
    else:
        stripped = text.strip()
        if not stripped:
            kind = "empty"
        else:
            stats = delegation_text_stats(stripped)
            if stats["raw_broad_source_leak"]:
                kind = "raw_broad_source_leak"
            elif _SUMMARY_WORDS.search(stripped) or stats["output_chars"] <= _RAW_LEAK_CHARS_MIN:
                kind = "parent_context_summary"
            else:
                kind = "unclassified_result"
    stats = delegation_text_stats(text)
    stats["result_kind"] = kind
    stats["raw_broad_source_leak"] = kind == "raw_broad_source_leak" or bool(
        stats.get("raw_broad_source_leak") and not is_error
    )
    return stats


def delegation_text_stats(text: str) -> dict[str, Any]:
    lines = text.splitlines()
    nonempty = [line for line in lines if line.strip()]
    source_like = sum(1 for line in nonempty if _SOURCE_LINE_RE.search(line))
    path_mentions = len(_PATH_RE.findall(text))
    output_chars = len(text)
    output_lines = len(lines)
    raw_leak = (
        output_chars >= _RAW_LEAK_CHARS_MIN
        or output_lines >= _RAW_LEAK_LINES_MIN
        or (
            source_like >= _RAW_LEAK_SOURCE_LINES_MIN
            and len(nonempty) > 0
            and source_like / len(nonempty) >= 0.30
        )
        or (path_mentions >= 30 and output_chars >= 8_000)
    )
    return {
        "output_chars": output_chars,
        "output_lines": output_lines,
        "source_like_lines": source_like,
        "path_mentions": path_mentions,
        "raw_broad_source_leak": raw_leak,
    }


def delegation_invocation_data(tool_name: str, tool_input: dict[str, Any]) -> dict[str, Any]:
    prompt_text = (
        coerce_text(tool_input.get("prompt"))
        or coerce_text(tool_input.get("task"))
        or coerce_text(tool_input.get("description"))
    )
    requested_agent = (
        tool_input.get("agent")
        or tool_input.get("agent_type")
        or tool_input.get("model")
    )
    data: dict[str, Any] = {
        "tool_name": tool_name,
        "input_keys": sorted(str(key) for key in tool_input.keys()),
        "prompt_chars": len(prompt_text),
    }
    if isinstance(requested_agent, str) and requested_agent.strip():
        data["requested_agent"] = requested_agent.strip()
    return data


def delegation_result_data(
    *,
    tool_name: str,
    tool_input: dict[str, Any],
    result_payload: Any,
    is_error: bool = False,
) -> dict[str, Any]:
    text = coerce_text(result_payload)
    data = delegation_invocation_data(tool_name, tool_input)
    data["is_error"] = bool(is_error)
    data.update(classify_delegation_result(text, is_error=bool(is_error)))
    usage = extract_token_usage(result_payload, tool_input)
    if any(usage.values()):
        data["token_usage"] = usage
    return data
