"""Shared normalization for command-observation outcome predicates."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

COMMAND_STREAMS = frozenset({"stdout", "stderr", "combined"})
COMMAND_STREAM_OPERATORS = frozenset({"contains", "not_contains", "equals"})
COMMAND_STREAM_PREDICATE_TYPES = frozenset(
    f"command_{source}_{operator}"
    for source in COMMAND_STREAMS
    for operator in COMMAND_STREAM_OPERATORS
)


def normalize_command_stream_predicate(
    raw: Mapping[str, Any],
    *,
    command_count: int,
) -> dict[str, Any]:
    """Validate and canonicalize one command stream predicate.

    Both planning readiness and exported verification contracts use this function,
    so a predicate accepted as implementation-ready cannot later fail serialization.
    """

    predicate_type = raw.get("type")
    if predicate_type not in COMMAND_STREAM_PREDICATE_TYPES:
        raise ValueError("command_stream_predicate_type_invalid")
    if isinstance(command_count, bool) or not isinstance(command_count, int):
        raise ValueError("command_stream_predicate_command_count_invalid")
    command_index = raw.get("command_index")
    value = raw.get("value")
    if (
        isinstance(command_index, bool)
        or not isinstance(command_index, int)
        or command_index < 0
        or command_index >= command_count
        or not isinstance(value, str)
        or not value.strip()
    ):
        raise ValueError("command_stream_predicate_invalid")
    unknown = sorted(set(raw) - {"type", "command_index", "value"})
    if unknown:
        raise ValueError(f"command_stream_predicate_unknown_fields:{unknown!r}")
    return {
        "type": predicate_type,
        "command_index": command_index,
        "value": value.strip(),
    }
