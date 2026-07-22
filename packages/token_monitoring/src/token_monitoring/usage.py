from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

USAGE_RECEIPT_SCHEMA_VERSION = 1
TOKEN_DIMENSIONS: tuple[str, ...] = (
    "total_tokens",
    "input_tokens",
    "cached_input_tokens",
    "uncached_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
)

UsageSemantics = Literal["per_invocation", "session_cumulative", "unattributable"]


def _integer_token_value(raw: object, *, field_name: str) -> int:
    if isinstance(raw, bool):
        raise ValueError(f"{field_name} must be a non-negative integer")
    if isinstance(raw, int):
        value = raw
    elif isinstance(raw, float) and raw.is_integer():
        value = int(raw)
    else:
        raise ValueError(f"{field_name} must be a non-negative integer")
    if value < 0:
        raise ValueError(f"{field_name} must be a non-negative integer")
    return value


@dataclass(frozen=True)
class TokenUsage:
    """Provider-neutral token dimensions for one attributable unit of work.

    ``reasoning_output_tokens`` is the neutral reasoning bucket.  Providers that
    call it ``reasoning_tokens`` are normalized into that field.
    """

    total_tokens: int
    input_tokens: int
    cached_input_tokens: int
    uncached_input_tokens: int
    output_tokens: int
    reasoning_output_tokens: int

    def __post_init__(self) -> None:
        for name in TOKEN_DIMENSIONS:
            _integer_token_value(getattr(self, name), field_name=name)
        if self.cached_input_tokens > self.input_tokens:
            raise ValueError("cached_input_tokens cannot exceed input_tokens")
        if self.uncached_input_tokens != self.input_tokens - self.cached_input_tokens:
            raise ValueError(
                "uncached_input_tokens must equal input_tokens - cached_input_tokens"
            )

    @classmethod
    def zero(cls) -> TokenUsage:
        return cls(
            total_tokens=0,
            input_tokens=0,
            cached_input_tokens=0,
            uncached_input_tokens=0,
            output_tokens=0,
            reasoning_output_tokens=0,
        )

    @classmethod
    def from_mapping(cls, raw: Mapping[str, object]) -> TokenUsage:
        input_tokens = _integer_token_value(raw.get("input_tokens", 0), field_name="input_tokens")
        cached = _integer_token_value(
            raw.get("cached_input_tokens", 0), field_name="cached_input_tokens"
        )
        explicit_uncached = raw.get("uncached_input_tokens")
        uncached = (
            input_tokens - cached
            if explicit_uncached is None
            else _integer_token_value(explicit_uncached, field_name="uncached_input_tokens")
        )
        output = _integer_token_value(raw.get("output_tokens", 0), field_name="output_tokens")
        reasoning_raw = raw.get("reasoning_output_tokens", raw.get("reasoning_tokens", 0))
        reasoning = _integer_token_value(
            reasoning_raw, field_name="reasoning_output_tokens"
        )
        total_raw = raw.get("total_tokens")
        total = (
            input_tokens + output
            if total_raw is None
            else _integer_token_value(total_raw, field_name="total_tokens")
        )
        return cls(
            total_tokens=total,
            input_tokens=input_tokens,
            cached_input_tokens=cached,
            uncached_input_tokens=uncached,
            output_tokens=output,
            reasoning_output_tokens=reasoning,
        )

    def to_dict(self) -> dict[str, int]:
        return {name: int(getattr(self, name)) for name in TOKEN_DIMENSIONS}

    def decreased_dimensions(self, baseline: TokenUsage) -> tuple[str, ...]:
        return tuple(
            name
            for name in TOKEN_DIMENSIONS
            if int(getattr(self, name)) < int(getattr(baseline, name))
        )

    def delta_from(self, baseline: TokenUsage) -> TokenUsage:
        decreased = self.decreased_dimensions(baseline)
        if decreased:
            raise ValueError(f"usage counters decreased: {', '.join(decreased)}")
        return TokenUsage(
            **{
                name: int(getattr(self, name)) - int(getattr(baseline, name))
                for name in TOKEN_DIMENSIONS
            }
        )


@dataclass(frozen=True)
class UsageResult:
    """An invocation-scoped, provider-neutral usage attribution result.

    ``usage`` is deliberately ``None`` for unattributable input.  Consumers must
    not turn missing or ambiguous provider evidence into a zero-token invocation.
    """

    provider: str
    invocation_id: str
    semantics: UsageSemantics
    usage: TokenUsage | None
    observed_high_water: TokenUsage | None
    baseline_high_water: TokenUsage | None = None
    session_id: str | None = None
    source_kind: str = "provider_jsonl"
    source_sha256: str | None = None
    observation_count: int = 0
    unique_observation_count: int = 0
    duplicate_terminal_count: int = 0
    diagnostics: tuple[Mapping[str, object], ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not self.provider.strip():
            raise ValueError("provider is required")
        if not self.invocation_id.strip():
            raise ValueError("invocation_id is required")
        if self.semantics == "unattributable" and self.usage is not None:
            raise ValueError("unattributable usage must be None")
        if self.semantics != "unattributable" and self.usage is None:
            raise ValueError("attributable usage must include token dimensions")

    @property
    def attributable(self) -> bool:
        return self.semantics != "unattributable" and self.usage is not None

    def receipt_payload(self) -> dict[str, Any]:
        """Return a deterministic, content-addressed artifact payload.

        ``content_sha256`` hashes the canonical JSON object with that field
        omitted.  The receipt contains no collection timestamp, so replaying the
        same evidence and binding produces the same address.
        """

        payload: dict[str, Any] = {
            "schema_version": USAGE_RECEIPT_SCHEMA_VERSION,
            "artifact_type": "model_usage_receipt",
            "provider": self.provider,
            "invocation_id": self.invocation_id,
            "session_id": self.session_id,
            "usage_semantics": self.semantics,
            "attributable": self.attributable,
            "usage": self.usage.to_dict() if self.usage is not None else None,
            "baseline_high_water": (
                self.baseline_high_water.to_dict()
                if self.baseline_high_water is not None
                else None
            ),
            "observed_high_water": (
                self.observed_high_water.to_dict()
                if self.observed_high_water is not None
                else None
            ),
            "source": {
                "kind": self.source_kind,
                "sha256": self.source_sha256,
                "observation_count": self.observation_count,
                "unique_observation_count": self.unique_observation_count,
                "duplicate_terminal_count": self.duplicate_terminal_count,
            },
            "diagnostics": [dict(item) for item in self.diagnostics],
        }
        payload["content_sha256"] = usage_receipt_content_sha256(payload)
        return payload


def usage_receipt_content_sha256(payload: Mapping[str, object]) -> str:
    """Hash a receipt using canonical JSON, excluding its address field."""

    unhashed = {key: value for key, value in payload.items() if key != "content_sha256"}
    encoded = json.dumps(
        unhashed,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def usage_receipt_is_valid(payload: Mapping[str, object]) -> bool:
    """Validate the v1 envelope, token dimensions, and content address."""

    if payload.get("schema_version") != USAGE_RECEIPT_SCHEMA_VERSION:
        return False
    if payload.get("artifact_type") != "model_usage_receipt":
        return False
    if not isinstance(payload.get("provider"), str) or not str(payload["provider"]).strip():
        return False
    if not isinstance(payload.get("invocation_id"), str) or not str(
        payload["invocation_id"]
    ).strip():
        return False
    semantics = payload.get("usage_semantics")
    if semantics not in {"per_invocation", "session_cumulative", "unattributable"}:
        return False
    attributable = payload.get("attributable")
    if not isinstance(attributable, bool):
        return False
    usage = payload.get("usage")
    if (semantics == "unattributable") != (usage is None):
        return False
    if attributable != (semantics != "unattributable"):
        return False
    for field_name in ("usage", "baseline_high_water", "observed_high_water"):
        value = payload.get(field_name)
        if value is None:
            continue
        if not isinstance(value, Mapping):
            return False
        if set(value) != set(TOKEN_DIMENSIONS):
            return False
        try:
            TokenUsage.from_mapping(value)
        except ValueError:
            return False
    source = payload.get("source")
    if not isinstance(source, Mapping):
        return False
    for count_name in (
        "observation_count",
        "unique_observation_count",
        "duplicate_terminal_count",
    ):
        count = source.get(count_name)
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            return False
    diagnostics = payload.get("diagnostics")
    if not isinstance(diagnostics, list) or not all(
        isinstance(item, Mapping) for item in diagnostics
    ):
        return False
    expected = payload.get("content_sha256")
    return isinstance(expected, str) and expected == usage_receipt_content_sha256(payload)
