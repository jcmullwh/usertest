from __future__ import annotations

import json
import os
import re
import shlex
import time
import uuid
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path, PurePosixPath
from typing import Any, Literal, TypeAlias, cast

LIFECYCLE_TELEMETRY_SCHEMA_VERSION = 1
LIFECYCLE_CONTEXT_ENV = "USERTEST_LIFECYCLE_CONTEXT"
LIFECYCLE_CONTEXT_FILE_ENV = "USERTEST_LIFECYCLE_CONTEXT_FILE"
MODEL_USAGE_RECEIPT_FILENAME = "model_usage_receipt.json"

ActorType: TypeAlias = Literal[
    "system",
    "controller",
    "model",
    "human",
    "supervising_agent",
    "external_service",
    "unknown",
]
OriginType: TypeAlias = Literal[
    "automatic",
    "manual",
    "supervising_agent",
    "external_service",
    "unknown_external",
]
ProvenanceQuality: TypeAlias = Literal[
    "authoritative",
    "artifact_derived",
    "operator_attested",
    "inferred",
    "unknown",
]
UsageSemantics: TypeAlias = Literal[
    "per_invocation",
    "session_cumulative",
    "unattributable",
]
ResolutionMode: TypeAlias = Literal[
    "self_healed_same_author",
    "self_healed_controller",
    "resolved_supervisor",
    "resolved_human",
    "resolved_external",
    "tolerated_nonblocking",
    "unresolved_terminal",
    "open",
]

ACTOR_TYPES = frozenset(
    {
        "system",
        "controller",
        "model",
        "human",
        "supervising_agent",
        "external_service",
        "unknown",
    }
)
ORIGIN_TYPES = frozenset(
    {"automatic", "manual", "supervising_agent", "external_service", "unknown_external"}
)
PROVENANCE_QUALITIES = frozenset(
    {"authoritative", "artifact_derived", "operator_attested", "inferred", "unknown"}
)
USAGE_SEMANTICS = frozenset({"per_invocation", "session_cumulative", "unattributable"})
RESOLUTION_MODES = frozenset(
    {
        "self_healed_same_author",
        "self_healed_controller",
        "resolved_supervisor",
        "resolved_human",
        "resolved_external",
        "tolerated_nonblocking",
        "unresolved_terminal",
        "open",
    }
)
ACTION_FAMILIES = frozenset(
    {
        "launch",
        "control",
        "diagnosis",
        "observation",
        "adjudication",
        "environment",
        "configuration",
        "credential",
        "permission",
        "authored_output_repair",
        "controller_repair",
        "code_repair",
        "commit",
        "push",
        "pull_request",
        "ci",
        "review",
        "merge",
        "ticket",
        "data_custody",
        "provenance_custody",
        "external_system",
        "other",
    }
)
MANIFEST_STATUSES = frozenset({"active", "terminal", "incomplete", "unreconciled"})
TOKEN_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "uncached_input_tokens",
    "output_tokens",
    "reasoning_tokens",
    "total_tokens",
)

_ID_RE = re.compile(r"^\S{1,512}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SECRET_NAME = (
    r"(?:api[-_]?key|access[-_]?token|refresh[-_]?token|auth(?:orization)?|bearer|"
    r"password|passwd|secret|client[-_]?secret|credential|private[-_]?key)"
)
_SECRET_OPTION_RE = re.compile(
    rf"(?i)(?P<option>--?{_SECRET_NAME})(?P<separator>\s+|=)"
    r"(?P<value>\"[^\"]*\"|'[^']*'|[^\s\"']+)(?P<trailing_quote>[\"']?)"
)
_SECRET_ASSIGNMENT_RE = re.compile(
    rf"(?i)(?P<name>[A-Za-z_][A-Za-z0-9_]*(?:{_SECRET_NAME})[A-Za-z0-9_]*)"
    r"(?P<separator>\s*=\s*)(?P<value>\"[^\"]*\"|'[^']*'|[^\s;\"']+)"
    r"(?P<trailing_quote>[\"']?)"
)
_AUTH_HEADER_RE = re.compile(
    r"(?i)(?P<prefix>authorization\s*:\s*(?:bearer|basic)?\s*)"
    r"(?P<value>[^\s\"']+)(?P<trailing_quote>[\"']?)"
)
_URL_CREDENTIAL_RE = re.compile(
    r"(?P<scheme>[A-Za-z][A-Za-z0-9+.-]*://)(?P<user>[^/@:\s]+):(?P<password>[^/@\s]+)@"
)
_KNOWN_SECRET_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:sk-[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9_]{12,}|"
    r"github_pat_[A-Za-z0-9_]{12,})(?![A-Za-z0-9])"
)


class TelemetryValidationError(ValueError):
    """Raised when a lifecycle telemetry artifact violates its versioned contract."""


class TelemetryArtifactError(RuntimeError):
    """Raised when a telemetry artifact cannot be read or persisted safely."""


class IdempotencyConflictError(TelemetryArtifactError):
    """Raised when an event id is reused for different content."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _json_ready(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return _json_ready(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _json_ready(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_ready(item) for item in value]
    return value


def canonical_json(value: Any) -> str:
    return json.dumps(
        _json_ready(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def canonical_sha256(value: Any) -> str:
    return sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _require_object(raw: Any, field_name: str) -> Mapping[str, Any]:
    if not isinstance(raw, Mapping):
        raise TelemetryValidationError(f"{field_name} must be an object")
    return cast(Mapping[str, Any], raw)


def _reject_unknown_keys(raw: Mapping[str, Any], *, allowed: set[str], field_name: str) -> None:
    unknown = sorted(str(key) for key in raw if key not in allowed)
    if unknown:
        raise TelemetryValidationError(f"{field_name} has unknown fields: {', '.join(unknown)}")


def _require_schema_version(value: Any, field_name: str = "schema_version") -> int:
    if value != LIFECYCLE_TELEMETRY_SCHEMA_VERSION:
        raise TelemetryValidationError(
            f"{field_name} must be {LIFECYCLE_TELEMETRY_SCHEMA_VERSION}, got {value!r}"
        )
    return LIFECYCLE_TELEMETRY_SCHEMA_VERSION


def _require_string(value: Any, field_name: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise TelemetryValidationError(f"{field_name} must be a string")
    normalized = value.strip()
    if not allow_empty and not normalized:
        raise TelemetryValidationError(f"{field_name} must not be empty")
    return normalized


def _optional_string(value: Any, field_name: str) -> str | None:
    if value is None:
        return None
    return _require_string(value, field_name)


def _require_id(value: Any, field_name: str) -> str:
    candidate = _require_string(value, field_name)
    if _ID_RE.fullmatch(candidate) is None:
        raise TelemetryValidationError(
            f"{field_name} must be 1-512 non-whitespace characters"
        )
    return candidate


def _optional_id(value: Any, field_name: str) -> str | None:
    if value is None:
        return None
    return _require_id(value, field_name)


def _require_timestamp(value: Any, field_name: str) -> str:
    timestamp = _require_string(value, field_name)
    parse_value = f"{timestamp[:-1]}+00:00" if timestamp.endswith("Z") else timestamp
    try:
        parsed = datetime.fromisoformat(parse_value)
    except ValueError as exc:
        raise TelemetryValidationError(f"{field_name} must be an RFC 3339 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise TelemetryValidationError(f"{field_name} must include a timezone")
    return timestamp


def _optional_timestamp(value: Any, field_name: str) -> str | None:
    if value is None:
        return None
    return _require_timestamp(value, field_name)


def _as_datetime(value: str) -> datetime:
    return datetime.fromisoformat(f"{value[:-1]}+00:00" if value.endswith("Z") else value)


def _validate_interval(start: str | None, end: str | None, field_name: str) -> None:
    if end is not None and start is None:
        raise TelemetryValidationError(f"{field_name}.end requires a start timestamp")
    if start is not None and end is not None and _as_datetime(end) < _as_datetime(start):
        raise TelemetryValidationError(f"{field_name}.end must not precede start")


def _optional_nonnegative_float(value: Any, field_name: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TelemetryValidationError(f"{field_name} must be a number")
    result = float(value)
    if result < 0 or result != result or result in (float("inf"), float("-inf")):
        raise TelemetryValidationError(f"{field_name} must be finite and non-negative")
    return result


def _optional_nonnegative_int(value: Any, field_name: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise TelemetryValidationError(f"{field_name} must be a non-negative integer")
    return cast(int, value)


def _require_positive_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise TelemetryValidationError(f"{field_name} must be a positive integer")
    return cast(int, value)


def _string_tuple(value: Any, field_name: str, *, ids: bool = False) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise TelemetryValidationError(f"{field_name} must be an array of strings")
    values = tuple(
        _require_id(item, f"{field_name}[{index}]")
        if ids
        else _require_string(item, f"{field_name}[{index}]")
        for index, item in enumerate(value)
    )
    if len(set(values)) != len(values):
        raise TelemetryValidationError(f"{field_name} must not contain duplicates")
    return values


def _string_map(value: Any, field_name: str) -> dict[str, str]:
    if value is None:
        return {}
    raw = _require_object(value, field_name)
    result: dict[str, str] = {}
    for key, item in raw.items():
        normalized_key = _require_string(key, f"{field_name} key")
        result[normalized_key] = _require_string(item, f"{field_name}.{normalized_key}")
    return result


def _json_map(value: Any, field_name: str) -> dict[str, Any]:
    if value is None:
        return {}
    raw = _require_object(value, field_name)
    result = {str(key): _json_ready(item) for key, item in raw.items()}
    try:
        canonical_json(result)
    except (TypeError, ValueError) as exc:
        raise TelemetryValidationError(f"{field_name} must contain JSON values") from exc
    return result


def _validate_enum(value: Any, field_name: str, choices: frozenset[str]) -> str:
    result = _require_string(value, field_name)
    if result not in choices:
        raise TelemetryValidationError(
            f"{field_name} must be one of: {', '.join(sorted(choices))}"
        )
    return result


def _validate_sha256(value: Any, field_name: str) -> str:
    result = _require_string(value, field_name)
    if _SHA256_RE.fullmatch(result) is None:
        raise TelemetryValidationError(f"{field_name} must be a lowercase SHA-256 digest")
    return result


def _validate_relative_artifact_path(value: str, field_name: str) -> str:
    normalized = value.replace("\\", "/")
    path = PurePosixPath(normalized)
    if path.is_absolute() or not path.parts or ".." in path.parts:
        raise TelemetryValidationError(f"{field_name} must be a non-escaping relative path")
    return normalized


@dataclass(frozen=True)
class LifecycleContext:
    case_lifecycle_id: str | None = None
    case_id: str | None = None
    cycle_id: str | None = None
    stage: str | None = None
    milestone_id: str | None = None
    work_unit_id: str | None = None
    invocation_id: str | None = None
    session_id: str | None = None
    shared_work_id: str | None = None
    parent_action_id: str | None = None
    system_fingerprint: dict[str, str] = field(default_factory=dict)
    schema_version: int = LIFECYCLE_TELEMETRY_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return cast(dict[str, Any], _json_ready(self))

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> LifecycleContext:
        allowed = {
            "schema_version",
            "case_lifecycle_id",
            "case_id",
            "cycle_id",
            "stage",
            "milestone_id",
            "work_unit_id",
            "invocation_id",
            "session_id",
            "shared_work_id",
            "parent_action_id",
            "system_fingerprint",
        }
        _reject_unknown_keys(raw, allowed=allowed, field_name="context")
        context = cls(
            schema_version=_require_schema_version(raw.get("schema_version")),
            case_lifecycle_id=_optional_id(
                raw.get("case_lifecycle_id"), "context.case_lifecycle_id"
            ),
            case_id=_optional_id(raw.get("case_id"), "context.case_id"),
            cycle_id=_optional_id(raw.get("cycle_id"), "context.cycle_id"),
            stage=_optional_string(raw.get("stage"), "context.stage"),
            milestone_id=_optional_id(raw.get("milestone_id"), "context.milestone_id"),
            work_unit_id=_optional_id(raw.get("work_unit_id"), "context.work_unit_id"),
            invocation_id=_optional_id(raw.get("invocation_id"), "context.invocation_id"),
            session_id=_optional_id(raw.get("session_id"), "context.session_id"),
            shared_work_id=_optional_id(raw.get("shared_work_id"), "context.shared_work_id"),
            parent_action_id=_optional_id(
                raw.get("parent_action_id"), "context.parent_action_id"
            ),
            system_fingerprint=_string_map(
                raw.get("system_fingerprint"), "context.system_fingerprint"
            ),
        )
        validate_lifecycle_context(context)
        return context


def validate_lifecycle_context(
    context: LifecycleContext | Mapping[str, Any],
) -> LifecycleContext:
    if isinstance(context, Mapping):
        return LifecycleContext.from_dict(context)
    if not isinstance(context, LifecycleContext):
        raise TelemetryValidationError("context must be a LifecycleContext or object")
    _require_schema_version(context.schema_version, "context.schema_version")
    values = context.to_dict()
    if not any(
        values.get(key)
        for key in ("case_lifecycle_id", "cycle_id", "work_unit_id", "shared_work_id")
    ):
        raise TelemetryValidationError(
            "context requires case_lifecycle_id, cycle_id, work_unit_id, or shared_work_id"
        )
    for key in (
        "case_lifecycle_id",
        "case_id",
        "cycle_id",
        "milestone_id",
        "work_unit_id",
        "invocation_id",
        "session_id",
        "shared_work_id",
        "parent_action_id",
    ):
        _optional_id(values.get(key), f"context.{key}")
    _optional_string(context.stage, "context.stage")
    _string_map(context.system_fingerprint, "context.system_fingerprint")
    return context


@dataclass(frozen=True)
class LifecycleEvent:
    event_id: str
    event_type: str
    occurred_at: str
    context: LifecycleContext
    idempotency_key: str
    actor_type: ActorType = "unknown"
    initiator_type: ActorType = "unknown"
    root_initiator_type: ActorType = "unknown"
    origin: OriginType = "unknown_external"
    recorded_at: str = field(default_factory=utc_now)
    started_at: str | None = None
    ended_at: str | None = None
    active_seconds: float | None = None
    machine_wait_seconds: float | None = None
    external_wait_seconds: float | None = None
    parent_event_id: str | None = None
    error_cluster_id: str | None = None
    intervention_id: str | None = None
    beneficiary_case_lifecycle_ids: tuple[str, ...] = ()
    evidence_paths: tuple[str, ...] = ()
    artifact_hashes: dict[str, str] = field(default_factory=dict)
    provenance_quality: ProvenanceQuality = "authoritative"
    attributes: dict[str, Any] = field(default_factory=dict)
    schema_version: int = LIFECYCLE_TELEMETRY_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return cast(dict[str, Any], _json_ready(self))

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> LifecycleEvent:
        allowed = {
            "schema_version",
            "event_id",
            "event_type",
            "occurred_at",
            "recorded_at",
            "context",
            "idempotency_key",
            "actor_type",
            "initiator_type",
            "root_initiator_type",
            "origin",
            "started_at",
            "ended_at",
            "active_seconds",
            "machine_wait_seconds",
            "external_wait_seconds",
            "parent_event_id",
            "error_cluster_id",
            "intervention_id",
            "beneficiary_case_lifecycle_ids",
            "evidence_paths",
            "artifact_hashes",
            "provenance_quality",
            "attributes",
        }
        _reject_unknown_keys(raw, allowed=allowed, field_name="event")
        context_raw = _require_object(raw.get("context"), "event.context")
        event = cls(
            schema_version=_require_schema_version(raw.get("schema_version")),
            event_id=_require_id(raw.get("event_id"), "event.event_id"),
            event_type=_require_string(raw.get("event_type"), "event.event_type"),
            occurred_at=_require_timestamp(raw.get("occurred_at"), "event.occurred_at"),
            recorded_at=_require_timestamp(raw.get("recorded_at"), "event.recorded_at"),
            context=LifecycleContext.from_dict(context_raw),
            idempotency_key=_require_id(
                raw.get("idempotency_key"), "event.idempotency_key"
            ),
            actor_type=cast(
                ActorType, _validate_enum(raw.get("actor_type"), "event.actor_type", ACTOR_TYPES)
            ),
            initiator_type=cast(
                ActorType,
                _validate_enum(raw.get("initiator_type"), "event.initiator_type", ACTOR_TYPES),
            ),
            root_initiator_type=cast(
                ActorType,
                _validate_enum(
                    raw.get("root_initiator_type"),
                    "event.root_initiator_type",
                    ACTOR_TYPES,
                ),
            ),
            origin=cast(
                OriginType, _validate_enum(raw.get("origin"), "event.origin", ORIGIN_TYPES)
            ),
            started_at=_optional_timestamp(raw.get("started_at"), "event.started_at"),
            ended_at=_optional_timestamp(raw.get("ended_at"), "event.ended_at"),
            active_seconds=_optional_nonnegative_float(
                raw.get("active_seconds"), "event.active_seconds"
            ),
            machine_wait_seconds=_optional_nonnegative_float(
                raw.get("machine_wait_seconds"), "event.machine_wait_seconds"
            ),
            external_wait_seconds=_optional_nonnegative_float(
                raw.get("external_wait_seconds"), "event.external_wait_seconds"
            ),
            parent_event_id=_optional_id(raw.get("parent_event_id"), "event.parent_event_id"),
            error_cluster_id=_optional_id(
                raw.get("error_cluster_id"), "event.error_cluster_id"
            ),
            intervention_id=_optional_id(
                raw.get("intervention_id"), "event.intervention_id"
            ),
            beneficiary_case_lifecycle_ids=_string_tuple(
                raw.get("beneficiary_case_lifecycle_ids"),
                "event.beneficiary_case_lifecycle_ids",
                ids=True,
            ),
            evidence_paths=_string_tuple(raw.get("evidence_paths"), "event.evidence_paths"),
            artifact_hashes=_string_map(raw.get("artifact_hashes"), "event.artifact_hashes"),
            provenance_quality=cast(
                ProvenanceQuality,
                _validate_enum(
                    raw.get("provenance_quality"),
                    "event.provenance_quality",
                    PROVENANCE_QUALITIES,
                ),
            ),
            attributes=_json_map(raw.get("attributes"), "event.attributes"),
        )
        validate_lifecycle_event(event)
        return event


def make_lifecycle_event(
    event_type: str,
    context: LifecycleContext,
    *,
    idempotency_key: str | None = None,
    occurred_at: str | None = None,
    **kwargs: Any,
) -> LifecycleEvent:
    event_id = str(uuid.uuid4())
    event = LifecycleEvent(
        event_id=event_id,
        event_type=event_type,
        occurred_at=occurred_at or utc_now(),
        context=context,
        idempotency_key=idempotency_key or event_id,
        **kwargs,
    )
    return validate_lifecycle_event(event)


def validate_lifecycle_event(event: LifecycleEvent | Mapping[str, Any]) -> LifecycleEvent:
    if isinstance(event, Mapping):
        return LifecycleEvent.from_dict(event)
    if not isinstance(event, LifecycleEvent):
        raise TelemetryValidationError("event must be a LifecycleEvent or object")
    _require_schema_version(event.schema_version, "event.schema_version")
    _require_id(event.event_id, "event.event_id")
    _require_string(event.event_type, "event.event_type")
    _require_timestamp(event.occurred_at, "event.occurred_at")
    _require_timestamp(event.recorded_at, "event.recorded_at")
    validate_lifecycle_context(event.context)
    _require_id(event.idempotency_key, "event.idempotency_key")
    _validate_enum(event.actor_type, "event.actor_type", ACTOR_TYPES)
    _validate_enum(event.initiator_type, "event.initiator_type", ACTOR_TYPES)
    _validate_enum(event.root_initiator_type, "event.root_initiator_type", ACTOR_TYPES)
    _validate_enum(event.origin, "event.origin", ORIGIN_TYPES)
    start = _optional_timestamp(event.started_at, "event.started_at")
    end = _optional_timestamp(event.ended_at, "event.ended_at")
    _validate_interval(start, end, "event")
    _optional_nonnegative_float(event.active_seconds, "event.active_seconds")
    _optional_nonnegative_float(event.machine_wait_seconds, "event.machine_wait_seconds")
    _optional_nonnegative_float(event.external_wait_seconds, "event.external_wait_seconds")
    _optional_id(event.parent_event_id, "event.parent_event_id")
    _optional_id(event.error_cluster_id, "event.error_cluster_id")
    _optional_id(event.intervention_id, "event.intervention_id")
    _string_tuple(
        event.beneficiary_case_lifecycle_ids,
        "event.beneficiary_case_lifecycle_ids",
        ids=True,
    )
    _string_tuple(event.evidence_paths, "event.evidence_paths")
    artifact_hashes = _string_map(event.artifact_hashes, "event.artifact_hashes")
    for name, digest in artifact_hashes.items():
        _validate_sha256(digest, f"event.artifact_hashes.{name}")
    _validate_enum(
        event.provenance_quality, "event.provenance_quality", PROVENANCE_QUALITIES
    )
    _json_map(event.attributes, "event.attributes")
    return event


@dataclass(frozen=True)
class ModelUsageReceipt:
    receipt_id: str
    context: LifecycleContext
    provider: str
    model: str
    usage_semantics: UsageSemantics
    recorded_at: str = field(default_factory=utc_now)
    invocation_started_at: str | None = None
    invocation_ended_at: str | None = None
    input_tokens: int | None = None
    cached_input_tokens: int | None = None
    uncached_input_tokens: int | None = None
    output_tokens: int | None = None
    reasoning_tokens: int | None = None
    total_tokens: int | None = None
    baseline_usage: dict[str, int] = field(default_factory=dict)
    observed_usage: dict[str, int] = field(default_factory=dict)
    source_artifact_path: str | None = None
    source_artifact_sha256: str | None = None
    provenance_quality: ProvenanceQuality = "authoritative"
    schema_version: int = LIFECYCLE_TELEMETRY_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return cast(dict[str, Any], _json_ready(self))

    def attributed_usage(self) -> dict[str, int | None]:
        return {name: cast(int | None, getattr(self, name)) for name in TOKEN_FIELDS}

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> ModelUsageReceipt:
        allowed = {
            "schema_version",
            "receipt_id",
            "context",
            "provider",
            "model",
            "usage_semantics",
            "recorded_at",
            "invocation_started_at",
            "invocation_ended_at",
            *TOKEN_FIELDS,
            "baseline_usage",
            "observed_usage",
            "source_artifact_path",
            "source_artifact_sha256",
            "provenance_quality",
        }
        _reject_unknown_keys(raw, allowed=allowed, field_name="usage_receipt")
        context_raw = _require_object(raw.get("context"), "usage_receipt.context")
        receipt = cls(
            schema_version=_require_schema_version(raw.get("schema_version")),
            receipt_id=_require_id(raw.get("receipt_id"), "usage_receipt.receipt_id"),
            context=LifecycleContext.from_dict(context_raw),
            provider=_require_string(raw.get("provider"), "usage_receipt.provider"),
            model=_require_string(raw.get("model"), "usage_receipt.model"),
            usage_semantics=cast(
                UsageSemantics,
                _validate_enum(
                    raw.get("usage_semantics"),
                    "usage_receipt.usage_semantics",
                    USAGE_SEMANTICS,
                ),
            ),
            recorded_at=_require_timestamp(raw.get("recorded_at"), "usage_receipt.recorded_at"),
            invocation_started_at=_optional_timestamp(
                raw.get("invocation_started_at"), "usage_receipt.invocation_started_at"
            ),
            invocation_ended_at=_optional_timestamp(
                raw.get("invocation_ended_at"), "usage_receipt.invocation_ended_at"
            ),
            input_tokens=_optional_nonnegative_int(
                raw.get("input_tokens"), "usage_receipt.input_tokens"
            ),
            cached_input_tokens=_optional_nonnegative_int(
                raw.get("cached_input_tokens"), "usage_receipt.cached_input_tokens"
            ),
            uncached_input_tokens=_optional_nonnegative_int(
                raw.get("uncached_input_tokens"), "usage_receipt.uncached_input_tokens"
            ),
            output_tokens=_optional_nonnegative_int(
                raw.get("output_tokens"), "usage_receipt.output_tokens"
            ),
            reasoning_tokens=_optional_nonnegative_int(
                raw.get("reasoning_tokens"), "usage_receipt.reasoning_tokens"
            ),
            total_tokens=_optional_nonnegative_int(
                raw.get("total_tokens"), "usage_receipt.total_tokens"
            ),
            baseline_usage=_token_map(raw.get("baseline_usage"), "usage_receipt.baseline_usage"),
            observed_usage=_token_map(raw.get("observed_usage"), "usage_receipt.observed_usage"),
            source_artifact_path=_optional_string(
                raw.get("source_artifact_path"), "usage_receipt.source_artifact_path"
            ),
            source_artifact_sha256=_optional_sha256(
                raw.get("source_artifact_sha256"), "usage_receipt.source_artifact_sha256"
            ),
            provenance_quality=cast(
                ProvenanceQuality,
                _validate_enum(
                    raw.get("provenance_quality"),
                    "usage_receipt.provenance_quality",
                    PROVENANCE_QUALITIES,
                ),
            ),
        )
        validate_model_usage_receipt(receipt)
        return receipt


def _optional_sha256(value: Any, field_name: str) -> str | None:
    if value is None:
        return None
    return _validate_sha256(value, field_name)


def _token_map(value: Any, field_name: str) -> dict[str, int]:
    if value is None:
        return {}
    raw = _require_object(value, field_name)
    result: dict[str, int] = {}
    for key, item in raw.items():
        name = _require_string(key, f"{field_name} key")
        if name not in TOKEN_FIELDS:
            raise TelemetryValidationError(
                f"{field_name}.{name} is not a recognized token dimension"
            )
        parsed = _optional_nonnegative_int(item, f"{field_name}.{name}")
        if parsed is None:
            raise TelemetryValidationError(f"{field_name}.{name} must not be null")
        result[name] = parsed
    return result


def validate_model_usage_receipt(
    receipt: ModelUsageReceipt | Mapping[str, Any],
) -> ModelUsageReceipt:
    if isinstance(receipt, Mapping):
        return ModelUsageReceipt.from_dict(receipt)
    if not isinstance(receipt, ModelUsageReceipt):
        raise TelemetryValidationError("receipt must be a ModelUsageReceipt or object")
    _require_schema_version(receipt.schema_version, "usage_receipt.schema_version")
    _require_id(receipt.receipt_id, "usage_receipt.receipt_id")
    validate_lifecycle_context(receipt.context)
    _require_string(receipt.provider, "usage_receipt.provider")
    _require_string(receipt.model, "usage_receipt.model")
    _validate_enum(receipt.usage_semantics, "usage_receipt.usage_semantics", USAGE_SEMANTICS)
    _require_timestamp(receipt.recorded_at, "usage_receipt.recorded_at")
    start = _optional_timestamp(
        receipt.invocation_started_at, "usage_receipt.invocation_started_at"
    )
    end = _optional_timestamp(receipt.invocation_ended_at, "usage_receipt.invocation_ended_at")
    _validate_interval(start, end, "usage_receipt.invocation")
    attributed = receipt.attributed_usage()
    for field_name, value in attributed.items():
        _optional_nonnegative_int(value, f"usage_receipt.{field_name}")
    if (
        receipt.input_tokens is not None
        and receipt.cached_input_tokens is not None
        and receipt.cached_input_tokens > receipt.input_tokens
    ):
        raise TelemetryValidationError("cached_input_tokens must not exceed input_tokens")
    if receipt.input_tokens is not None and receipt.cached_input_tokens is not None:
        expected_uncached = receipt.input_tokens - receipt.cached_input_tokens
        if (
            receipt.uncached_input_tokens is not None
            and receipt.uncached_input_tokens != expected_uncached
        ):
            raise TelemetryValidationError(
                "uncached_input_tokens must equal input_tokens - cached_input_tokens"
            )
    baseline = _token_map(receipt.baseline_usage, "usage_receipt.baseline_usage")
    observed = _token_map(receipt.observed_usage, "usage_receipt.observed_usage")
    if receipt.usage_semantics == "session_cumulative":
        if not baseline or not observed:
            raise TelemetryValidationError(
                "session_cumulative receipts require baseline_usage and observed_usage"
            )
        if set(baseline) != set(observed):
            raise TelemetryValidationError(
                "session_cumulative baseline_usage and observed_usage must have identical fields"
            )
        for name, observed_value in observed.items():
            delta = observed_value - baseline[name]
            if delta < 0:
                raise TelemetryValidationError(
                    f"usage_receipt.observed_usage.{name} must not be below its baseline"
                )
            if attributed[name] != delta:
                raise TelemetryValidationError(
                    f"usage_receipt.{name} must equal cumulative observed-baseline delta"
                )
    elif receipt.usage_semantics == "unattributable":
        if any(value is not None for value in attributed.values()):
            raise TelemetryValidationError(
                "unattributable receipts must not publish attributed token counts"
            )
    else:
        if baseline:
            raise TelemetryValidationError("per_invocation receipts must not have baseline_usage")
        for name, observed_value in observed.items():
            if attributed[name] != observed_value:
                raise TelemetryValidationError(
                    f"usage_receipt.{name} must equal per-invocation observed usage"
                )
    _optional_string(receipt.source_artifact_path, "usage_receipt.source_artifact_path")
    _optional_sha256(receipt.source_artifact_sha256, "usage_receipt.source_artifact_sha256")
    if (receipt.source_artifact_path is None) != (receipt.source_artifact_sha256 is None):
        raise TelemetryValidationError(
            "source_artifact_path and source_artifact_sha256 must be supplied together"
        )
    _validate_enum(
        receipt.provenance_quality,
        "usage_receipt.provenance_quality",
        PROVENANCE_QUALITIES,
    )
    return receipt


@dataclass(frozen=True)
class ErrorCluster:
    error_cluster_id: str
    context: LifecycleContext
    error_kind: str
    first_occurred_at: str
    last_occurred_at: str
    occurrence_count: int
    resolution_mode: ResolutionMode = "open"
    resolved_at: str | None = None
    resolution_event_id: str | None = None
    occurrence_event_ids: tuple[str, ...] = ()
    token_usage_receipt_ids: tuple[str, ...] = ()
    evidence_paths: tuple[str, ...] = ()
    provenance_quality: ProvenanceQuality = "authoritative"
    schema_version: int = LIFECYCLE_TELEMETRY_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return cast(dict[str, Any], _json_ready(self))

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> ErrorCluster:
        allowed = {
            "schema_version",
            "error_cluster_id",
            "context",
            "error_kind",
            "first_occurred_at",
            "last_occurred_at",
            "occurrence_count",
            "resolution_mode",
            "resolved_at",
            "resolution_event_id",
            "occurrence_event_ids",
            "token_usage_receipt_ids",
            "evidence_paths",
            "provenance_quality",
        }
        _reject_unknown_keys(raw, allowed=allowed, field_name="error_cluster")
        context_raw = _require_object(raw.get("context"), "error_cluster.context")
        cluster = cls(
            schema_version=_require_schema_version(raw.get("schema_version")),
            error_cluster_id=_require_id(
                raw.get("error_cluster_id"), "error_cluster.error_cluster_id"
            ),
            context=LifecycleContext.from_dict(context_raw),
            error_kind=_require_string(raw.get("error_kind"), "error_cluster.error_kind"),
            first_occurred_at=_require_timestamp(
                raw.get("first_occurred_at"), "error_cluster.first_occurred_at"
            ),
            last_occurred_at=_require_timestamp(
                raw.get("last_occurred_at"), "error_cluster.last_occurred_at"
            ),
            occurrence_count=_require_positive_int(
                raw.get("occurrence_count"), "error_cluster.occurrence_count"
            ),
            resolution_mode=cast(
                ResolutionMode,
                _validate_enum(
                    raw.get("resolution_mode"),
                    "error_cluster.resolution_mode",
                    RESOLUTION_MODES,
                ),
            ),
            resolved_at=_optional_timestamp(raw.get("resolved_at"), "error_cluster.resolved_at"),
            resolution_event_id=_optional_id(
                raw.get("resolution_event_id"), "error_cluster.resolution_event_id"
            ),
            occurrence_event_ids=_string_tuple(
                raw.get("occurrence_event_ids"), "error_cluster.occurrence_event_ids", ids=True
            ),
            token_usage_receipt_ids=_string_tuple(
                raw.get("token_usage_receipt_ids"),
                "error_cluster.token_usage_receipt_ids",
                ids=True,
            ),
            evidence_paths=_string_tuple(
                raw.get("evidence_paths"), "error_cluster.evidence_paths"
            ),
            provenance_quality=cast(
                ProvenanceQuality,
                _validate_enum(
                    raw.get("provenance_quality"),
                    "error_cluster.provenance_quality",
                    PROVENANCE_QUALITIES,
                ),
            ),
        )
        validate_error_cluster(cluster)
        return cluster


def validate_error_cluster(cluster: ErrorCluster | Mapping[str, Any]) -> ErrorCluster:
    if isinstance(cluster, Mapping):
        return ErrorCluster.from_dict(cluster)
    if not isinstance(cluster, ErrorCluster):
        raise TelemetryValidationError("cluster must be an ErrorCluster or object")
    _require_schema_version(cluster.schema_version, "error_cluster.schema_version")
    _require_id(cluster.error_cluster_id, "error_cluster.error_cluster_id")
    validate_lifecycle_context(cluster.context)
    _require_string(cluster.error_kind, "error_cluster.error_kind")
    first = _require_timestamp(cluster.first_occurred_at, "error_cluster.first_occurred_at")
    last = _require_timestamp(cluster.last_occurred_at, "error_cluster.last_occurred_at")
    if _as_datetime(last) < _as_datetime(first):
        raise TelemetryValidationError("error_cluster.last_occurred_at must not precede first")
    _require_positive_int(cluster.occurrence_count, "error_cluster.occurrence_count")
    _validate_enum(cluster.resolution_mode, "error_cluster.resolution_mode", RESOLUTION_MODES)
    resolved = _optional_timestamp(cluster.resolved_at, "error_cluster.resolved_at")
    if cluster.resolution_mode == "open" and resolved is not None:
        raise TelemetryValidationError("open error clusters must not have resolved_at")
    if cluster.resolution_mode not in {"open", "unresolved_terminal"} and resolved is None:
        raise TelemetryValidationError(
            "resolved error clusters require resolved_at (except unresolved_terminal)"
        )
    if resolved is not None and _as_datetime(resolved) < _as_datetime(last):
        raise TelemetryValidationError("error_cluster.resolved_at must not precede last occurrence")
    _optional_id(cluster.resolution_event_id, "error_cluster.resolution_event_id")
    occurrence_ids = _string_tuple(
        cluster.occurrence_event_ids, "error_cluster.occurrence_event_ids", ids=True
    )
    if occurrence_ids and len(occurrence_ids) != cluster.occurrence_count:
        raise TelemetryValidationError(
            "error_cluster.occurrence_count must match occurrence_event_ids when supplied"
        )
    _string_tuple(
        cluster.token_usage_receipt_ids, "error_cluster.token_usage_receipt_ids", ids=True
    )
    _string_tuple(cluster.evidence_paths, "error_cluster.evidence_paths")
    _validate_enum(
        cluster.provenance_quality,
        "error_cluster.provenance_quality",
        PROVENANCE_QUALITIES,
    )
    return cluster


@dataclass(frozen=True)
class Intervention:
    intervention_id: str
    context: LifecycleContext
    intervention_kind: str
    started_at: str
    actor_type: ActorType
    ended_at: str | None = None
    active_seconds: float | None = None
    required_for_progress: bool = True
    related_error_cluster_ids: tuple[str, ...] = ()
    action_ids: tuple[str, ...] = ()
    result: str | None = None
    evidence_paths: tuple[str, ...] = ()
    provenance_quality: ProvenanceQuality = "authoritative"
    schema_version: int = LIFECYCLE_TELEMETRY_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return cast(dict[str, Any], _json_ready(self))

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> Intervention:
        allowed = {
            "schema_version",
            "intervention_id",
            "context",
            "intervention_kind",
            "started_at",
            "actor_type",
            "ended_at",
            "active_seconds",
            "required_for_progress",
            "related_error_cluster_ids",
            "action_ids",
            "result",
            "evidence_paths",
            "provenance_quality",
        }
        _reject_unknown_keys(raw, allowed=allowed, field_name="intervention")
        context_raw = _require_object(raw.get("context"), "intervention.context")
        intervention = cls(
            schema_version=_require_schema_version(raw.get("schema_version")),
            intervention_id=_require_id(
                raw.get("intervention_id"), "intervention.intervention_id"
            ),
            context=LifecycleContext.from_dict(context_raw),
            intervention_kind=_require_string(
                raw.get("intervention_kind"), "intervention.intervention_kind"
            ),
            started_at=_require_timestamp(raw.get("started_at"), "intervention.started_at"),
            actor_type=cast(
                ActorType,
                _validate_enum(raw.get("actor_type"), "intervention.actor_type", ACTOR_TYPES),
            ),
            ended_at=_optional_timestamp(raw.get("ended_at"), "intervention.ended_at"),
            active_seconds=_optional_nonnegative_float(
                raw.get("active_seconds"), "intervention.active_seconds"
            ),
            required_for_progress=_require_bool(
                raw.get("required_for_progress"), "intervention.required_for_progress"
            ),
            related_error_cluster_ids=_string_tuple(
                raw.get("related_error_cluster_ids"),
                "intervention.related_error_cluster_ids",
                ids=True,
            ),
            action_ids=_string_tuple(raw.get("action_ids"), "intervention.action_ids", ids=True),
            result=_optional_string(raw.get("result"), "intervention.result"),
            evidence_paths=_string_tuple(
                raw.get("evidence_paths"), "intervention.evidence_paths"
            ),
            provenance_quality=cast(
                ProvenanceQuality,
                _validate_enum(
                    raw.get("provenance_quality"),
                    "intervention.provenance_quality",
                    PROVENANCE_QUALITIES,
                ),
            ),
        )
        validate_intervention(intervention)
        return intervention


def _require_bool(value: Any, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise TelemetryValidationError(f"{field_name} must be a boolean")
    return value


def validate_intervention(
    intervention: Intervention | Mapping[str, Any],
) -> Intervention:
    if isinstance(intervention, Mapping):
        return Intervention.from_dict(intervention)
    if not isinstance(intervention, Intervention):
        raise TelemetryValidationError("intervention must be an Intervention or object")
    _require_schema_version(intervention.schema_version, "intervention.schema_version")
    _require_id(intervention.intervention_id, "intervention.intervention_id")
    validate_lifecycle_context(intervention.context)
    _require_string(intervention.intervention_kind, "intervention.intervention_kind")
    start = _require_timestamp(intervention.started_at, "intervention.started_at")
    end = _optional_timestamp(intervention.ended_at, "intervention.ended_at")
    _validate_interval(start, end, "intervention")
    _validate_enum(intervention.actor_type, "intervention.actor_type", ACTOR_TYPES)
    if intervention.actor_type not in {"human", "supervising_agent"}:
        raise TelemetryValidationError(
            "intervention.actor_type must be human or supervising_agent"
        )
    _optional_nonnegative_float(intervention.active_seconds, "intervention.active_seconds")
    _require_bool(intervention.required_for_progress, "intervention.required_for_progress")
    _string_tuple(
        intervention.related_error_cluster_ids,
        "intervention.related_error_cluster_ids",
        ids=True,
    )
    _string_tuple(intervention.action_ids, "intervention.action_ids", ids=True)
    _optional_string(intervention.result, "intervention.result")
    _string_tuple(intervention.evidence_paths, "intervention.evidence_paths")
    _validate_enum(
        intervention.provenance_quality,
        "intervention.provenance_quality",
        PROVENANCE_QUALITIES,
    )
    return intervention


@dataclass(frozen=True)
class ManualAction:
    action_id: str
    context: LifecycleContext
    action_family: str
    operation: str
    interface: str
    actor_type: ActorType
    started_at: str
    ended_at: str | None = None
    active_seconds: float | None = None
    required_for_progress: bool = True
    passive_observation: bool = False
    policy_mandated: bool = False
    measurement_administration: bool = False
    root_initiator_type: ActorType = "human"
    command_family: str | None = None
    redacted_command: str | None = None
    command_fingerprint: str | None = None
    result: str | None = None
    related_error_cluster_ids: tuple[str, ...] = ()
    intervention_id: str | None = None
    evidence_paths: tuple[str, ...] = ()
    provenance_quality: ProvenanceQuality = "authoritative"
    schema_version: int = LIFECYCLE_TELEMETRY_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return cast(dict[str, Any], _json_ready(self))

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> ManualAction:
        allowed = {
            "schema_version",
            "action_id",
            "context",
            "action_family",
            "operation",
            "interface",
            "actor_type",
            "started_at",
            "ended_at",
            "active_seconds",
            "required_for_progress",
            "passive_observation",
            "policy_mandated",
            "measurement_administration",
            "root_initiator_type",
            "command_family",
            "redacted_command",
            "command_fingerprint",
            "result",
            "related_error_cluster_ids",
            "intervention_id",
            "evidence_paths",
            "provenance_quality",
        }
        _reject_unknown_keys(raw, allowed=allowed, field_name="manual_action")
        context_raw = _require_object(raw.get("context"), "manual_action.context")
        action = cls(
            schema_version=_require_schema_version(raw.get("schema_version")),
            action_id=_require_id(raw.get("action_id"), "manual_action.action_id"),
            context=LifecycleContext.from_dict(context_raw),
            action_family=_require_string(
                raw.get("action_family"), "manual_action.action_family"
            ),
            operation=_require_string(raw.get("operation"), "manual_action.operation"),
            interface=_require_string(raw.get("interface"), "manual_action.interface"),
            actor_type=cast(
                ActorType,
                _validate_enum(raw.get("actor_type"), "manual_action.actor_type", ACTOR_TYPES),
            ),
            started_at=_require_timestamp(raw.get("started_at"), "manual_action.started_at"),
            ended_at=_optional_timestamp(raw.get("ended_at"), "manual_action.ended_at"),
            active_seconds=_optional_nonnegative_float(
                raw.get("active_seconds"), "manual_action.active_seconds"
            ),
            required_for_progress=_require_bool(
                raw.get("required_for_progress"), "manual_action.required_for_progress"
            ),
            passive_observation=_require_bool(
                raw.get("passive_observation"), "manual_action.passive_observation"
            ),
            policy_mandated=_require_bool(
                raw.get("policy_mandated"), "manual_action.policy_mandated"
            ),
            measurement_administration=_require_bool(
                raw.get("measurement_administration"),
                "manual_action.measurement_administration",
            ),
            root_initiator_type=cast(
                ActorType,
                _validate_enum(
                    raw.get("root_initiator_type"),
                    "manual_action.root_initiator_type",
                    ACTOR_TYPES,
                ),
            ),
            command_family=_optional_string(
                raw.get("command_family"), "manual_action.command_family"
            ),
            redacted_command=_optional_string(
                raw.get("redacted_command"), "manual_action.redacted_command"
            ),
            command_fingerprint=_optional_sha256(
                raw.get("command_fingerprint"), "manual_action.command_fingerprint"
            ),
            result=_optional_string(raw.get("result"), "manual_action.result"),
            related_error_cluster_ids=_string_tuple(
                raw.get("related_error_cluster_ids"),
                "manual_action.related_error_cluster_ids",
                ids=True,
            ),
            intervention_id=_optional_id(
                raw.get("intervention_id"), "manual_action.intervention_id"
            ),
            evidence_paths=_string_tuple(
                raw.get("evidence_paths"), "manual_action.evidence_paths"
            ),
            provenance_quality=cast(
                ProvenanceQuality,
                _validate_enum(
                    raw.get("provenance_quality"),
                    "manual_action.provenance_quality",
                    PROVENANCE_QUALITIES,
                ),
            ),
        )
        validate_manual_action(action)
        return action


def validate_manual_action(action: ManualAction | Mapping[str, Any]) -> ManualAction:
    if isinstance(action, Mapping):
        return ManualAction.from_dict(action)
    if not isinstance(action, ManualAction):
        raise TelemetryValidationError("action must be a ManualAction or object")
    _require_schema_version(action.schema_version, "manual_action.schema_version")
    _require_id(action.action_id, "manual_action.action_id")
    validate_lifecycle_context(action.context)
    _validate_enum(action.action_family, "manual_action.action_family", ACTION_FAMILIES)
    _require_string(action.operation, "manual_action.operation")
    _require_string(action.interface, "manual_action.interface")
    _validate_enum(action.actor_type, "manual_action.actor_type", ACTOR_TYPES)
    if action.actor_type not in {"human", "supervising_agent"}:
        raise TelemetryValidationError(
            "manual_action.actor_type must be human or supervising_agent"
        )
    start = _require_timestamp(action.started_at, "manual_action.started_at")
    end = _optional_timestamp(action.ended_at, "manual_action.ended_at")
    _validate_interval(start, end, "manual_action")
    _optional_nonnegative_float(action.active_seconds, "manual_action.active_seconds")
    _require_bool(action.required_for_progress, "manual_action.required_for_progress")
    _require_bool(action.passive_observation, "manual_action.passive_observation")
    _require_bool(action.policy_mandated, "manual_action.policy_mandated")
    _require_bool(
        action.measurement_administration, "manual_action.measurement_administration"
    )
    _validate_enum(
        action.root_initiator_type, "manual_action.root_initiator_type", ACTOR_TYPES
    )
    if action.root_initiator_type not in {"human", "supervising_agent"}:
        raise TelemetryValidationError(
            "manual_action.root_initiator_type must be human or supervising_agent"
        )
    _optional_string(action.command_family, "manual_action.command_family")
    redacted = _optional_string(action.redacted_command, "manual_action.redacted_command")
    fingerprint = _optional_sha256(
        action.command_fingerprint, "manual_action.command_fingerprint"
    )
    if (redacted is None) != (fingerprint is None):
        raise TelemetryValidationError(
            "redacted_command and command_fingerprint must be supplied together"
        )
    if redacted is not None and fingerprint != fingerprint_command(redacted):
        raise TelemetryValidationError(
            "manual_action.command_fingerprint does not match redacted_command"
        )
    _optional_string(action.result, "manual_action.result")
    _string_tuple(
        action.related_error_cluster_ids,
        "manual_action.related_error_cluster_ids",
        ids=True,
    )
    _optional_id(action.intervention_id, "manual_action.intervention_id")
    _string_tuple(action.evidence_paths, "manual_action.evidence_paths")
    _validate_enum(
        action.provenance_quality,
        "manual_action.provenance_quality",
        PROVENANCE_QUALITIES,
    )
    return action


@dataclass(frozen=True)
class LifecycleManifest:
    case_lifecycle_id: str
    case_id: str
    created_at: str
    updated_at: str
    status: str = "active"
    event_log_path: str = "lifecycle_events.jsonl"
    dependency_lifecycle_ids: tuple[str, ...] = ()
    shared_work_ids: tuple[str, ...] = ()
    usage_receipt_paths: tuple[str, ...] = ()
    system_fingerprint: dict[str, str] = field(default_factory=dict)
    provenance_quality: ProvenanceQuality = "authoritative"
    metadata: dict[str, Any] = field(default_factory=dict)
    schema_version: int = LIFECYCLE_TELEMETRY_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return cast(dict[str, Any], _json_ready(self))

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> LifecycleManifest:
        allowed = {
            "schema_version",
            "case_lifecycle_id",
            "case_id",
            "created_at",
            "updated_at",
            "status",
            "event_log_path",
            "dependency_lifecycle_ids",
            "shared_work_ids",
            "usage_receipt_paths",
            "system_fingerprint",
            "provenance_quality",
            "metadata",
        }
        _reject_unknown_keys(raw, allowed=allowed, field_name="lifecycle_manifest")
        manifest = cls(
            schema_version=_require_schema_version(raw.get("schema_version")),
            case_lifecycle_id=_require_id(
                raw.get("case_lifecycle_id"), "lifecycle_manifest.case_lifecycle_id"
            ),
            case_id=_require_id(raw.get("case_id"), "lifecycle_manifest.case_id"),
            created_at=_require_timestamp(raw.get("created_at"), "lifecycle_manifest.created_at"),
            updated_at=_require_timestamp(raw.get("updated_at"), "lifecycle_manifest.updated_at"),
            status=_validate_enum(
                raw.get("status"), "lifecycle_manifest.status", MANIFEST_STATUSES
            ),
            event_log_path=_require_string(
                raw.get("event_log_path"), "lifecycle_manifest.event_log_path"
            ),
            dependency_lifecycle_ids=_string_tuple(
                raw.get("dependency_lifecycle_ids"),
                "lifecycle_manifest.dependency_lifecycle_ids",
                ids=True,
            ),
            shared_work_ids=_string_tuple(
                raw.get("shared_work_ids"), "lifecycle_manifest.shared_work_ids", ids=True
            ),
            usage_receipt_paths=_string_tuple(
                raw.get("usage_receipt_paths"), "lifecycle_manifest.usage_receipt_paths"
            ),
            system_fingerprint=_string_map(
                raw.get("system_fingerprint"), "lifecycle_manifest.system_fingerprint"
            ),
            provenance_quality=cast(
                ProvenanceQuality,
                _validate_enum(
                    raw.get("provenance_quality"),
                    "lifecycle_manifest.provenance_quality",
                    PROVENANCE_QUALITIES,
                ),
            ),
            metadata=_json_map(raw.get("metadata"), "lifecycle_manifest.metadata"),
        )
        validate_lifecycle_manifest(manifest)
        return manifest


def validate_lifecycle_manifest(
    manifest: LifecycleManifest | Mapping[str, Any],
) -> LifecycleManifest:
    if isinstance(manifest, Mapping):
        return LifecycleManifest.from_dict(manifest)
    if not isinstance(manifest, LifecycleManifest):
        raise TelemetryValidationError("manifest must be a LifecycleManifest or object")
    _require_schema_version(manifest.schema_version, "lifecycle_manifest.schema_version")
    _require_id(manifest.case_lifecycle_id, "lifecycle_manifest.case_lifecycle_id")
    _require_id(manifest.case_id, "lifecycle_manifest.case_id")
    created = _require_timestamp(manifest.created_at, "lifecycle_manifest.created_at")
    updated = _require_timestamp(manifest.updated_at, "lifecycle_manifest.updated_at")
    if _as_datetime(updated) < _as_datetime(created):
        raise TelemetryValidationError("lifecycle_manifest.updated_at must not precede created_at")
    _validate_enum(manifest.status, "lifecycle_manifest.status", MANIFEST_STATUSES)
    _validate_relative_artifact_path(
        _require_string(manifest.event_log_path, "lifecycle_manifest.event_log_path"),
        "lifecycle_manifest.event_log_path",
    )
    dependencies = _string_tuple(
        manifest.dependency_lifecycle_ids,
        "lifecycle_manifest.dependency_lifecycle_ids",
        ids=True,
    )
    if manifest.case_lifecycle_id in dependencies:
        raise TelemetryValidationError("a lifecycle manifest cannot depend on itself")
    _string_tuple(manifest.shared_work_ids, "lifecycle_manifest.shared_work_ids", ids=True)
    receipt_paths = _string_tuple(
        manifest.usage_receipt_paths, "lifecycle_manifest.usage_receipt_paths"
    )
    for index, path in enumerate(receipt_paths):
        _validate_relative_artifact_path(path, f"lifecycle_manifest.usage_receipt_paths[{index}]")
    _string_map(manifest.system_fingerprint, "lifecycle_manifest.system_fingerprint")
    _validate_enum(
        manifest.provenance_quality,
        "lifecycle_manifest.provenance_quality",
        PROVENANCE_QUALITIES,
    )
    _json_map(manifest.metadata, "lifecycle_manifest.metadata")
    return manifest


def serialize_lifecycle_context(context: LifecycleContext) -> str:
    return canonical_json(validate_lifecycle_context(context).to_dict())


def deserialize_lifecycle_context(raw: str) -> LifecycleContext:
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise TelemetryValidationError("serialized lifecycle context is not valid JSON") from exc
    return LifecycleContext.from_dict(_require_object(decoded, "context"))


def lifecycle_context_env(context: LifecycleContext) -> dict[str, str]:
    return {LIFECYCLE_CONTEXT_ENV: serialize_lifecycle_context(context)}


def write_lifecycle_context(path: Path, context: LifecycleContext) -> str:
    payload = validate_lifecycle_context(context).to_dict()
    digest = canonical_sha256(payload)
    _atomic_write_json(path, payload)
    return digest


def read_lifecycle_context(path: Path) -> LifecycleContext:
    return LifecycleContext.from_dict(_read_json_object(path, "lifecycle context"))


def load_context_from_env(
    environ: Mapping[str, str] | None = None,
    *,
    required: bool = False,
) -> LifecycleContext | None:
    values = os.environ if environ is None else environ
    inline = values.get(LIFECYCLE_CONTEXT_ENV)
    file_value = values.get(LIFECYCLE_CONTEXT_FILE_ENV)
    inline_context = deserialize_lifecycle_context(inline) if inline else None
    file_context = read_lifecycle_context(Path(file_value)) if file_value else None
    if inline_context is not None and file_context is not None:
        if inline_context.to_dict() != file_context.to_dict():
            raise TelemetryValidationError(
                f"{LIFECYCLE_CONTEXT_ENV} and {LIFECYCLE_CONTEXT_FILE_ENV} disagree"
            )
        return inline_context
    result = inline_context or file_context
    if result is None and required:
        raise TelemetryValidationError(
            f"missing {LIFECYCLE_CONTEXT_ENV} or {LIFECYCLE_CONTEXT_FILE_ENV}"
        )
    return result


def redact_command(command: str | Sequence[str]) -> str:
    """Return a persistable command representation with likely credentials removed."""

    if isinstance(command, str):
        rendered = command
    elif isinstance(command, Sequence):
        redacted_args: list[str] = []
        redact_next = False
        for raw_arg in command:
            arg = str(raw_arg)
            if redact_next:
                redacted_args.append("<redacted>")
                redact_next = False
                continue
            option = arg.split("=", 1)[0]
            if re.fullmatch(rf"(?i)--?{_SECRET_NAME}", option):
                if "=" in arg:
                    redacted_args.append(f"{option}=<redacted>")
                else:
                    redacted_args.append(option)
                    redact_next = True
                continue
            redacted_args.append(arg)
        rendered = shlex.join(redacted_args)
    else:
        raise TypeError("command must be a string or sequence of strings")

    def replace_secret(match: re.Match[str], prefix: str) -> str:
        original = match.group("value")
        if len(original) >= 2 and original[0] == original[-1] and original[0] in "\"'":
            replacement = f"{original[0]}<redacted>{original[0]}"
        else:
            replacement = "<redacted>"
        trailing_quote = match.groupdict().get("trailing_quote") or ""
        return f"{prefix}{replacement}{trailing_quote}"

    rendered = _URL_CREDENTIAL_RE.sub(r"\g<scheme>\g<user>:<redacted>@", rendered)
    rendered = _SECRET_OPTION_RE.sub(
        lambda match: replace_secret(
            match, f"{match.group('option')}{match.group('separator')}"
        ),
        rendered,
    )
    rendered = _SECRET_ASSIGNMENT_RE.sub(
        lambda match: replace_secret(
            match, f"{match.group('name')}{match.group('separator')}"
        ),
        rendered,
    )
    rendered = _AUTH_HEADER_RE.sub(
        lambda match: replace_secret(match, match.group("prefix")), rendered
    )
    rendered = _KNOWN_SECRET_RE.sub("<redacted>", rendered)
    return rendered.strip()


def fingerprint_command(command: str | Sequence[str]) -> str:
    redacted = redact_command(command)
    return sha256(redacted.encode("utf-8")).hexdigest()


def command_family(command: str | Sequence[str]) -> str | None:
    if isinstance(command, str):
        try:
            parts = shlex.split(redact_command(command), posix=os.name != "nt")
        except ValueError:
            parts = redact_command(command).split()
    else:
        parts = [str(item) for item in command]
    for part in parts:
        if not part or "=" in part and not part.startswith((".", "/", "\\")):
            continue
        return Path(part).name.lower()
    return None


@contextmanager
def _artifact_lock(
    target: Path,
    *,
    timeout_seconds: float = 10.0,
    stale_after_seconds: float = 300.0,
) -> Iterator[None]:
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise TelemetryArtifactError(
            f"cannot create telemetry artifact directory {target.parent}: {exc}"
        ) from exc
    lock_path = target.with_name(f"{target.name}.lock")
    started = time.monotonic()
    while True:
        try:
            descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except OSError as exc:
            is_contention = isinstance(exc, FileExistsError) or (
                os.name == "nt" and isinstance(exc, PermissionError)
            )
            if not is_contention:
                raise TelemetryArtifactError(
                    f"cannot create telemetry lock {lock_path}: {exc}"
                ) from exc
            # Windows can surface an existing exclusively-created lock as a
            # sharing-violation PermissionError rather than FileExistsError.
            # Reconcile both as lock contention, while retaining the same
            # bounded timeout and stale-lock recovery contract.
            try:
                age = time.time() - lock_path.stat().st_mtime
            except FileNotFoundError:
                continue
            except PermissionError:
                age = 0.0
            if age > stale_after_seconds:
                try:
                    lock_path.unlink()
                except (FileNotFoundError, PermissionError):
                    pass
                else:
                    continue
            if time.monotonic() - started >= timeout_seconds:
                raise TelemetryArtifactError(
                    f"timed out waiting for telemetry lock: {lock_path}"
                ) from None
            time.sleep(0.01)
            continue
        try:
            os.write(descriptor, f"pid={os.getpid()} created={utc_now()}\n".encode("ascii"))
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        break
    try:
        yield
    finally:
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass


def _fsync_directory(path: Path) -> None:
    try:
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        descriptor = os.open(path, flags)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _atomic_write_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        descriptor = os.open(temp_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            view = memoryview(payload)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise TelemetryArtifactError(f"short write while persisting {path}")
                view = view[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(temp_path, path)
        _fsync_directory(path.parent)
    except OSError as exc:
        raise TelemetryArtifactError(f"cannot atomically write {path}: {exc}") from exc
    finally:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    _atomic_write_bytes(path, (canonical_json(payload) + "\n").encode("utf-8"))


def _read_json_object(path: Path, label: str) -> Mapping[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise TelemetryArtifactError(f"cannot read {label} {path}: {exc}") from exc
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise TelemetryArtifactError(f"invalid JSON in {label} {path}: {exc}") from exc
    return _require_object(decoded, label)


def _read_jsonl_objects(
    path: Path,
    *,
    repair_truncated_tail: bool = False,
) -> tuple[list[Mapping[str, Any]], bool]:
    try:
        payload = path.read_bytes()
    except FileNotFoundError:
        return [], False
    except OSError as exc:
        raise TelemetryArtifactError(f"cannot read lifecycle event log {path}: {exc}") from exc
    if not payload:
        return [], False
    objects: list[Mapping[str, Any]] = []
    offset = 0
    repaired = False
    lines = payload.splitlines(keepends=True)
    for line_number, line in enumerate(lines, start=1):
        terminated = line.endswith((b"\n", b"\r"))
        stripped = line.strip()
        next_offset = offset + len(line)
        if not stripped:
            offset = next_offset
            continue
        try:
            decoded = json.loads(stripped.decode("utf-8"))
            obj = _require_object(decoded, f"lifecycle event line {line_number}")
        except (UnicodeDecodeError, json.JSONDecodeError, TelemetryValidationError) as exc:
            is_last = line_number == len(lines)
            if repair_truncated_tail and is_last and not terminated:
                try:
                    with path.open("r+b") as handle:
                        handle.truncate(offset)
                        handle.flush()
                        os.fsync(handle.fileno())
                except OSError as write_exc:
                    raise TelemetryArtifactError(
                        f"cannot repair truncated lifecycle event log {path}: {write_exc}"
                    ) from write_exc
                repaired = True
                break
            raise TelemetryArtifactError(
                f"invalid lifecycle event JSON at {path}:{line_number}: {exc}"
            ) from exc
        objects.append(obj)
        offset = next_offset
    return objects, repaired


def read_lifecycle_events(path: Path) -> list[LifecycleEvent]:
    objects, _ = _read_jsonl_objects(path)
    events: list[LifecycleEvent] = []
    event_ids: set[str] = set()
    idempotency_keys: set[str] = set()
    for line_number, raw in enumerate(objects, start=1):
        try:
            event = LifecycleEvent.from_dict(raw)
        except TelemetryValidationError as exc:
            raise TelemetryArtifactError(
                f"invalid lifecycle event at {path}:{line_number}: {exc}"
            ) from exc
        if event.event_id in event_ids:
            raise TelemetryArtifactError(f"duplicate event_id in {path}: {event.event_id}")
        if event.idempotency_key in idempotency_keys:
            raise TelemetryArtifactError(
                f"duplicate idempotency_key in {path}: {event.idempotency_key}"
            )
        event_ids.add(event.event_id)
        idempotency_keys.add(event.idempotency_key)
        events.append(event)
    return events


def append_lifecycle_event(
    path: Path,
    event: LifecycleEvent | Mapping[str, Any],
    *,
    lock_timeout_seconds: float = 10.0,
) -> bool:
    """Append one durable event; return ``False`` when its idempotency key already exists."""

    validated = validate_lifecycle_event(event)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _artifact_lock(path, timeout_seconds=lock_timeout_seconds):
        objects, repaired = _read_jsonl_objects(path, repair_truncated_tail=True)
        validated_action_id = (
            str(validated.attributes.get("action_id") or "").strip()
            if validated.event_type.startswith("action.")
            else ""
        )
        for raw in objects:
            existing = LifecycleEvent.from_dict(raw)
            if existing.event_id == validated.event_id:
                if existing.to_dict() != validated.to_dict():
                    raise IdempotencyConflictError(
                        f"event_id {validated.event_id!r} already has different content"
                    )
                return False
            if existing.idempotency_key == validated.idempotency_key:
                return False
            existing_action_id = (
                str(existing.attributes.get("action_id") or "").strip()
                if existing.event_type.startswith("action.")
                else ""
            )
            if (
                validated_action_id
                and existing_action_id
                and validated.context.work_unit_id is not None
                and existing.context.work_unit_id == validated.context.work_unit_id
                and existing_action_id != validated_action_id
            ):
                raise IdempotencyConflictError(
                    "work_unit_id "
                    f"{validated.context.work_unit_id!r} is already bound to action "
                    f"{existing_action_id!r}; cannot bind it to {validated_action_id!r}"
                )
            if (
                validated_action_id
                and existing_action_id == validated_action_id
                and validated.context.work_unit_id is not None
                and existing.context.work_unit_id is not None
                and existing.context.work_unit_id != validated.context.work_unit_id
            ):
                raise IdempotencyConflictError(
                    "action_id "
                    f"{validated_action_id!r} is already bound to work unit "
                    f"{existing.context.work_unit_id!r}; cannot bind it to "
                    f"{validated.context.work_unit_id!r}"
                )

        line = (canonical_json(validated.to_dict()) + "\n").encode("utf-8")
        prefix = b""
        if path.exists() and path.stat().st_size > 0 and not repaired:
            with path.open("rb") as handle:
                handle.seek(-1, os.SEEK_END)
                if handle.read(1) not in {b"\n", b"\r"}:
                    prefix = b"\n"
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
            try:
                payload = memoryview(prefix + line)
                while payload:
                    written = os.write(descriptor, payload)
                    if written <= 0:
                        raise TelemetryArtifactError(f"short write while appending {path}")
                    payload = payload[written:]
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        except OSError as exc:
            raise TelemetryArtifactError(f"cannot append lifecycle event to {path}: {exc}") from exc
        _fsync_directory(path.parent)
    return True


def write_lifecycle_manifest(path: Path, manifest: LifecycleManifest | Mapping[str, Any]) -> str:
    validated = validate_lifecycle_manifest(manifest)
    payload = validated.to_dict()
    digest = canonical_sha256(payload)
    with _artifact_lock(path):
        _atomic_write_json(path, payload)
    return digest


def read_lifecycle_manifest(path: Path) -> LifecycleManifest:
    return LifecycleManifest.from_dict(_read_json_object(path, "lifecycle manifest"))


def write_model_usage_receipt(
    path: Path,
    receipt: ModelUsageReceipt | Mapping[str, Any],
) -> str:
    """Write one immutable usage receipt and return its canonical SHA-256 digest."""

    validated = validate_model_usage_receipt(receipt)
    payload = validated.to_dict()
    digest = canonical_sha256(payload)
    with _artifact_lock(path):
        if path.exists():
            existing = _read_json_object(path, "model usage receipt")
            existing_receipt = ModelUsageReceipt.from_dict(existing)
            if existing_receipt.to_dict() != payload:
                raise IdempotencyConflictError(
                    f"model usage receipt already exists with different content: {path}"
                )
            return digest
        _atomic_write_json(path, payload)
    return digest


def write_content_addressed_model_usage_receipt(
    receipts_root: Path,
    receipt: ModelUsageReceipt | Mapping[str, Any],
) -> Path:
    validated = validate_model_usage_receipt(receipt)
    digest = canonical_sha256(validated.to_dict())
    path = receipts_root / digest / MODEL_USAGE_RECEIPT_FILENAME
    write_model_usage_receipt(path, validated)
    return path


def read_model_usage_receipt(path: Path) -> ModelUsageReceipt:
    return ModelUsageReceipt.from_dict(_read_json_object(path, "model usage receipt"))


__all__ = [
    "ACTION_FAMILIES",
    "ACTOR_TYPES",
    "LIFECYCLE_CONTEXT_ENV",
    "LIFECYCLE_CONTEXT_FILE_ENV",
    "LIFECYCLE_TELEMETRY_SCHEMA_VERSION",
    "MANIFEST_STATUSES",
    "MODEL_USAGE_RECEIPT_FILENAME",
    "ORIGIN_TYPES",
    "PROVENANCE_QUALITIES",
    "RESOLUTION_MODES",
    "TOKEN_FIELDS",
    "USAGE_SEMANTICS",
    "ActorType",
    "ErrorCluster",
    "IdempotencyConflictError",
    "Intervention",
    "LifecycleContext",
    "LifecycleEvent",
    "LifecycleManifest",
    "ManualAction",
    "ModelUsageReceipt",
    "OriginType",
    "ProvenanceQuality",
    "ResolutionMode",
    "TelemetryArtifactError",
    "TelemetryValidationError",
    "UsageSemantics",
    "append_lifecycle_event",
    "canonical_json",
    "canonical_sha256",
    "command_family",
    "deserialize_lifecycle_context",
    "fingerprint_command",
    "lifecycle_context_env",
    "load_context_from_env",
    "make_lifecycle_event",
    "read_lifecycle_context",
    "read_lifecycle_events",
    "read_lifecycle_manifest",
    "read_model_usage_receipt",
    "redact_command",
    "serialize_lifecycle_context",
    "utc_now",
    "validate_error_cluster",
    "validate_intervention",
    "validate_lifecycle_context",
    "validate_lifecycle_event",
    "validate_lifecycle_manifest",
    "validate_manual_action",
    "validate_model_usage_receipt",
    "write_content_addressed_model_usage_receipt",
    "write_lifecycle_context",
    "write_lifecycle_manifest",
    "write_model_usage_receipt",
]
