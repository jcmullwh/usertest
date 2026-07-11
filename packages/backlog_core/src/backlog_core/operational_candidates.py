"""Runner-owned operational failure evidence for automated problem mining.

Derived research, implementation, and verification output normally updates its
parent case.  This module handles the narrow exception: a typed failure in the
automation boundary itself prevented that stage from running.  It projects the
runner-owned signal into one synthetic observation atom, grouped by an exact
failure signature.  Free-form agent prose and ordinary failed experiments never
become candidates here; the existing problem-mining and relation-review stages
still decide whether the synthetic observation describes a real problem.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from hashlib import sha256
from typing import Any

from backlog_core.case_lineage import normalize_atom_lineage

OPERATIONAL_CANDIDATE_SCHEMA_VERSION = 1
OPERATIONAL_CANDIDATE_PRODUCER = "backlog_core.operational_candidates"

_DERIVED_ROLES = frozenset({"research", "implementation", "verification"})
_BINDING_STATUSES = frozenset({"verified", "reconstructed", "unavailable", "conflict"})
_TYPED_SIGNAL_KINDS = frozenset(
    {
        "agent_config",
        "agent_execution",
        "agent_preflight",
        "backend",
        "evidence_harness",
        "implementation_harness",
        "infrastructure",
        "policy",
        "report_contract",
        "research_harness",
        "runner_exception",
        "sandbox",
        "transport",
        "verification_harness",
    }
)
_ERROR_TYPE_CLASSIFICATION: Mapping[str, tuple[str, str]] = {
    "agentconfiginvalid": ("agent_config", "agent_start"),
    "agentpreflightfailed": ("agent_preflight", "preflight"),
    "executionbackenderror": ("backend", "preflight"),
    "evidenceverificationerror": ("evidence_harness", "evidence_verification"),
    "policydenied": ("policy", "agent_execution"),
    "reportcontracterror": ("report_contract", "report_contract"),
    "reportvalidationerror": ("report_contract", "report_contract"),
    "researchharnesserror": ("research_harness", "research"),
    "runnererror": ("runner_exception", "setup"),
    "runnersetuperror": ("runner_exception", "setup"),
    "sandboxerror": ("sandbox", "preflight"),
    "transporterror": ("transport", "agent_execution"),
    "verificationharnesserror": ("verification_harness", "verification"),
}
_AGENT_EXEC_SIGNAL_CLASSIFICATION: Mapping[str, tuple[str, str]] = {
    # These values are emitted by runner_core's structured failure classifier.  An
    # outer AgentExecFailed without one of these exact machine fields remains evidence
    # on its parent case; stderr prose is never interpreted here.
    "binary_or_command_missing": ("agent_preflight", "agent_start"),
    "disk_full": ("infrastructure", "storage"),
    "invalid_agent_config": ("agent_config", "agent_start"),
    "nested_agent_session": ("agent_config", "agent_start"),
    "permission_policy": ("policy", "agent_execution"),
    "provider_auth": ("backend", "agent_execution"),
    "provider_capacity": ("backend", "agent_execution"),
    "provider_quota_exceeded": ("backend", "agent_execution"),
    "tool_use_id_collision": ("transport", "agent_execution"),
    "transient_network": ("transport", "agent_execution"),
}
_CODE_PREFIX_CLASSIFICATION: tuple[tuple[str, str, str], ...] = (
    ("agent_config_", "agent_config", "agent_start"),
    ("backend_", "backend", "preflight"),
    ("docker_", "backend", "preflight"),
    ("evidence_", "evidence_harness", "evidence_verification"),
    ("execution_backend_", "backend", "preflight"),
    ("policy_", "policy", "agent_execution"),
    ("report_", "report_contract", "report_contract"),
    ("research_harness_", "research_harness", "research"),
    ("runner_", "runner_exception", "setup"),
    ("sandbox_", "sandbox", "preflight"),
    ("shell_probe_", "sandbox", "preflight"),
    ("transport_", "transport", "agent_execution"),
    ("verification_harness_", "verification_harness", "verification"),
)
_MISSION_ROLES: Mapping[str, tuple[str, str]] = {
    "backlog_repro_research": ("research", "repro_research"),
    "implement_backlog_ticket_v1": ("implementation", "implementation"),
    "implement_maintenance_backlog_ticket_v1": ("implementation", "implementation"),
    "review_backlog_implementation_pr_v1": ("verification", "verification"),
}
_TYPED_SOURCE_ATOM_KINDS = frozenset({"run_failure_event", "report_validation_error"})
_SAFE_EVIDENCE_TOKEN = re.compile(r"^[A-Za-z0-9_.:/-]{1,160}$")
_SAFE_SIGNAL_PROJECTION_FIELDS = frozenset(
    {
        "artifact_sha256",
        "backend",
        "error_code",
        "error_subtype",
        "error_type",
        "failure_class",
        "phase",
        "report_schema",
        "validation_issue_set_sha256",
        "validation_issue_count",
        "validation_issues",
        "validation_errors_unscanned_count",
        "validation_source_error_count",
        "validation_issues_omitted_count",
    }
)
_SAFE_VALIDATION_ISSUE_FIELDS = frozenset({"code", "constraint", "field", "path"})
_SAFE_ERROR_PROJECTION_FIELDS = frozenset(
    {
        "agent",
        "capability",
        "code",
        "failure_phase",
        "phase",
        "provider",
        "subtype",
        "type",
    }
)
_SAFE_PREFLIGHT_PROJECTION_FIELDS = frozenset(
    {
        "backend",
        "policy_status",
        "reason_code",
        "state",
    }
)
_STABLE_SIGNATURE_FIELDS = frozenset(
    {
        "backend",
        "error_code",
        "error_subtype",
        "error_type",
        "failure_class",
        "phase",
        "producer",
        "report_schema",
        "validation_issue_set_sha256",
    }
)
_MAX_PROMPT_EVIDENCE_SHAPES = 8
_MAX_PROMPT_EVIDENCE_SHAPES_BYTES = 12_000
_MAX_REPORT_VALIDATION_ISSUES = 16
_MAX_REPORT_VALIDATION_ERRORS_SCANNED = 256


def _clean_string(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned if cleaned else None


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return list(
        dict.fromkeys(item.strip() for item in value if isinstance(item, str) and item.strip())
    )


def _canonical_sha256(value: Any) -> str:
    return sha256(
        json.dumps(
            value,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _valid_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value.casefold())
    )


def _identifier(value: str | None) -> str | None:
    if value is None:
        return None
    return "".join(character for character in value.casefold() if character.isalnum())


def _safe_evidence_token(value: Any) -> str | None:
    """Return a bounded machine token, never free-form runner or agent prose."""

    cleaned = _clean_string(value)
    if cleaned is None or _SAFE_EVIDENCE_TOKEN.fullmatch(cleaned) is None:
        return None
    return cleaned


def _identity_token(value: Any, *, identifier: bool = False) -> str | None:
    """Normalize stable mechanism tokens without retaining path/casing spelling drift."""

    cleaned = _safe_evidence_token(value)
    if cleaned is None:
        return None
    if identifier:
        return _identifier(cleaned)
    return cleaned.replace("\\", "/").casefold()


def _identity_report_schema(value: Any) -> str | None:
    cleaned = _clean_string(value)
    if cleaned is None:
        return None
    basename = cleaned.replace("\\", "/").rsplit("/", 1)[-1]
    return _identity_token(basename)


def _safe_token_projection(
    value: Mapping[str, Any] | None,
    allowed_fields: frozenset[str],
) -> dict[str, str]:
    if not isinstance(value, Mapping):
        return {}
    return {
        field: token
        for field in sorted(allowed_fields)
        for token in [_safe_evidence_token(value.get(field))]
        if token is not None
    }


def _validation_path(value: str | None) -> str | None:
    """Normalize a JSON-schema path without retaining report values."""

    if value is None:
        return None
    path = value.strip()
    if not path.startswith("$"):
        return None
    parts = ["root"]
    for match in re.finditer(r"\.([A-Za-z_][A-Za-z0-9_-]*)|\[(\d+)\]", path):
        field, index = match.groups()
        parts.append(field if field is not None else f"index_{index}")
    normalized = ".".join(parts)
    return _safe_evidence_token(normalized)


def _validation_field(value: Any) -> str | None:
    token = _safe_evidence_token(value)
    return token.casefold() if token is not None else None


def _report_validation_issue(value: Any) -> dict[str, str] | None:
    """Classify one runner-owned validation line into non-secret causal fields.

    Validation messages can contain report values, paths, or provider text.  Only
    schema locations, constraint kinds, and explicit machine codes are retained;
    expected/actual values and arbitrary prose never enter mining context.
    """

    message = _clean_string(value)
    if message is None:
        return None
    lowered = message.casefold()
    path_match = re.match(r"^\s*(\$[^:]{0,240})\s*:\s*(.*)$", message, flags=re.DOTALL)
    path = _validation_path(path_match.group(1) if path_match is not None else None)
    detail = (path_match.group(2) if path_match is not None else message).strip()
    auxiliary_match = re.match(r"^(details|hint)\s*=\s*(.*)$", detail, flags=re.IGNORECASE)
    auxiliary_kind = auxiliary_match.group(1).casefold() if auxiliary_match is not None else None
    if auxiliary_match is not None:
        detail = auxiliary_match.group(2).strip()
    detail_lower = detail.casefold()

    code_match = re.fullmatch(r"\s*code\s*=\s*([A-Za-z0-9_.:/-]{1,160})\s*", message)
    if code_match is not None:
        return {"code": code_match.group(1).casefold(), "constraint": "machine_code"}

    extension_match = re.search(
        r"\bmissing\s+required\s+extension(?:\s+(?:field|key|named))?\s*[:=]?\s*"
        r"['\"]?([A-Za-z_][A-Za-z0-9_.:/-]{0,159})",
        message,
        flags=re.IGNORECASE,
    )
    if extension_match is not None:
        issue = {
            "code": "required_extension_missing",
            "constraint": "required",
        }
        field = _validation_field(extension_match.group(1))
        if field is not None:
            issue["field"] = field
        return issue

    normalized_identity = re.sub(r"[^a-z0-9]+", "_", lowered).strip("_")
    if "case_id" in normalized_identity and (
        "does_not_match_assignment" in normalized_identity
        or "case_id_mismatch" in normalized_identity
        or "case_id_attestation_mismatch" in normalized_identity
    ):
        return {
            "code": "case_assignment_mismatch",
            "constraint": "identity_binding",
            "path": "case_id",
        }
    if "problem_id" in normalized_identity and (
        "does_not_match_assignment" in normalized_identity
        or "problem_id_mismatch" in normalized_identity
        or "problem_id_attestation_mismatch" in normalized_identity
    ):
        return {
            "code": "problem_assignment_mismatch",
            "constraint": "identity_binding",
            "path": "problem_id",
        }

    issue: dict[str, str] = {}
    required_property = re.search(
        r"['\"]([A-Za-z_][A-Za-z0-9_.-]{0,159})['\"]\s+is\s+a\s+required\s+property",
        detail,
        flags=re.IGNORECASE,
    )
    if required_property is not None:
        issue = {"code": "required_field_missing", "constraint": "required"}
        field = _validation_field(required_property.group(1))
        if field is not None:
            issue["field"] = field
    elif "required for" in detail_lower or "required extension" in detail_lower:
        issue = {
            "code": (
                "required_extension_missing"
                if path is not None and path.startswith("root.extensions.")
                else "required_field_missing"
            ),
            "constraint": "required",
        }
    elif "non-empty string required" in detail_lower:
        issue = {"code": "non_empty_string_required", "constraint": "min_length"}
    elif "must be one of" in detail_lower or "is not one of" in detail_lower:
        issue = {"code": "enum_constraint_failed", "constraint": "enum"}
    elif "is not of type" in detail_lower or "must be a" in detail_lower:
        issue = {"code": "type_constraint_failed", "constraint": "type"}
    elif "additional properties are not allowed" in detail_lower:
        issue = {
            "code": "additional_property_forbidden",
            "constraint": "additional_properties",
        }
    elif "failed to parse json" in lowered or "invalid json" in lowered:
        issue = {"code": "report_json_parse_failed", "constraint": "json_syntax"}
    elif auxiliary_kind is not None:
        return None
    else:
        issue = {"code": "schema_constraint_failed", "constraint": "unclassified"}
    if path is not None:
        issue["path"] = path
    return issue


def _report_validation_projection(value: Any) -> dict[str, Any]:
    raw_errors = value if isinstance(value, list) else []
    scanned_errors = (
        raw_errors
        if len(raw_errors) <= _MAX_REPORT_VALIDATION_ERRORS_SCANNED
        else [
            *raw_errors[: _MAX_REPORT_VALIDATION_ERRORS_SCANNED // 2],
            *raw_errors[-(_MAX_REPORT_VALIDATION_ERRORS_SCANNED // 2) :],
        ]
    )
    issues_by_json = {
        json.dumps(issue, sort_keys=True, ensure_ascii=True, separators=(",", ":")): issue
        for raw in scanned_errors
        for issue in [_report_validation_issue(raw)]
        if issue is not None
    }
    if not issues_by_json:
        fallback = {
            "code": "schema_validation_failed_unclassified",
            "constraint": "unclassified",
        }
        issues_by_json[
            json.dumps(fallback, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
        ] = fallback
    all_issues = [issues_by_json[key] for key in sorted(issues_by_json)]
    visible = all_issues[:_MAX_REPORT_VALIDATION_ISSUES]
    return {
        "validation_source_error_count": len(raw_errors),
        "validation_errors_unscanned_count": len(raw_errors) - len(scanned_errors),
        "validation_issue_count": len(all_issues),
        "validation_issues": visible,
        "validation_issues_omitted_count": len(all_issues) - len(visible),
        "validation_issue_set_sha256": _canonical_sha256(all_issues),
    }


def _safe_validation_issues(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    issues: list[dict[str, str]] = []
    for raw in value[:_MAX_REPORT_VALIDATION_ISSUES]:
        if not isinstance(raw, Mapping) or set(raw) - _SAFE_VALIDATION_ISSUE_FIELDS:
            continue
        issue = {
            str(field): token
            for field, raw_value in raw.items()
            for token in [_safe_evidence_token(raw_value)]
            if token is not None
        }
        if issue.get("code") is not None and len(issue) == len(raw):
            issues.append(issue)
    return issues


def _failure_evidence_projection(
    *,
    signal: Mapping[str, Any],
    record: Mapping[str, Any],
) -> dict[str, Any]:
    """Project only structured machine fields that are safe to expose and hash.

    Paths, stderr, messages, last-message text, request IDs, and arbitrary nested
    payloads are intentionally absent.  The projection is both human-inspectable in
    the candidate and content-addressed in its immutable occurrence receipt.
    """

    signal_projection = _safe_token_projection(signal, _SAFE_SIGNAL_PROJECTION_FIELDS)
    artifact_sha256 = signal.get("artifact_sha256")
    if _valid_sha256(artifact_sha256):
        signal_projection["artifact_sha256"] = str(artifact_sha256).casefold()
    validation_issues = _safe_validation_issues(signal.get("validation_issues"))
    validation_issue_count = signal.get("validation_issue_count")
    source_error_count = signal.get("validation_source_error_count")
    unscanned_count = signal.get("validation_errors_unscanned_count")
    omitted_count = signal.get("validation_issues_omitted_count")
    if validation_issues:
        signal_projection["validation_issues"] = validation_issues
    if (
        isinstance(validation_issue_count, int)
        and not isinstance(validation_issue_count, bool)
        and validation_issue_count >= len(validation_issues)
    ):
        signal_projection["validation_issue_count"] = validation_issue_count
    if (
        isinstance(source_error_count, int)
        and not isinstance(source_error_count, bool)
        and source_error_count >= 0
    ):
        signal_projection["validation_source_error_count"] = source_error_count
    if (
        isinstance(unscanned_count, int)
        and not isinstance(unscanned_count, bool)
        and unscanned_count >= 0
    ):
        signal_projection["validation_errors_unscanned_count"] = unscanned_count
    if (
        isinstance(omitted_count, int)
        and not isinstance(omitted_count, bool)
        and omitted_count >= 0
    ):
        signal_projection["validation_issues_omitted_count"] = omitted_count

    error_raw = record.get("error")
    error = error_raw if isinstance(error_raw, Mapping) else {}
    error_projection = _safe_token_projection(error, _SAFE_ERROR_PROJECTION_FIELDS)

    preflight_raw = error.get("preflight")
    preflight = preflight_raw if isinstance(preflight_raw, Mapping) else {}
    shell_raw = preflight.get("shell_capability")
    shell = shell_raw if isinstance(shell_raw, Mapping) else {}
    shell_projection = _safe_token_projection(shell, _SAFE_PREFLIGHT_PROJECTION_FIELDS)

    projection: dict[str, Any] = {"signal": signal_projection}
    if error_projection:
        projection["error"] = error_projection
    if shell_projection:
        projection["preflight_shell_capability"] = shell_projection
    return projection


def _failure_evidence_projection_errors(value: Any) -> list[str]:
    if not isinstance(value, Mapping):
        return ["projection_not_object"]
    errors: list[str] = []
    allowed_sections = {"signal", "error", "preflight_shell_capability"}
    unknown_sections = sorted(str(key) for key in value if key not in allowed_sections)
    if unknown_sections:
        errors.append("projection_unknown_sections:" + ",".join(unknown_sections))

    section_specs = (
        ("signal", _SAFE_SIGNAL_PROJECTION_FIELDS, True),
        ("error", _SAFE_ERROR_PROJECTION_FIELDS, False),
        ("preflight_shell_capability", _SAFE_PREFLIGHT_PROJECTION_FIELDS, False),
    )
    for section, allowed_fields, required in section_specs:
        raw = value.get(section)
        if raw is None and not required:
            continue
        if not isinstance(raw, Mapping):
            errors.append(f"projection_{section}_not_object")
            continue
        unknown_fields = sorted(str(key) for key in raw if key not in allowed_fields)
        if unknown_fields:
            errors.append(f"projection_{section}_unknown_fields:" + ",".join(unknown_fields))
        for field, raw_value in raw.items():
            if section == "signal" and field == "validation_issues":
                if _safe_validation_issues(raw_value) != raw_value:
                    errors.append("projection_signal_validation_issues_invalid")
                continue
            if section == "signal" and field in {
                "validation_issue_count",
                "validation_errors_unscanned_count",
                "validation_source_error_count",
                "validation_issues_omitted_count",
            }:
                if isinstance(raw_value, bool) or not isinstance(raw_value, int) or raw_value < 0:
                    errors.append(f"projection_signal_{field}_invalid")
                continue
            if field == "artifact_sha256":
                if not _valid_sha256(raw_value):
                    errors.append(f"projection_{section}_{field}_invalid")
            elif _safe_evidence_token(raw_value) != raw_value:
                errors.append(f"projection_{section}_{field}_invalid")
    signal_raw = value.get("signal")
    signal = signal_raw if isinstance(signal_raw, Mapping) else {}
    issues = signal.get("validation_issues")
    if isinstance(issues, list):
        issue_count = signal.get("validation_issue_count")
        omitted_count = signal.get("validation_issues_omitted_count")
        if (
            isinstance(issue_count, bool)
            or not isinstance(issue_count, int)
            or isinstance(omitted_count, bool)
            or not isinstance(omitted_count, int)
            or issue_count != len(issues) + omitted_count
        ):
            errors.append("projection_signal_validation_issue_counts_mismatch")
    source_error_count = signal.get("validation_source_error_count")
    unscanned_count = signal.get("validation_errors_unscanned_count")
    if source_error_count is not None or unscanned_count is not None:
        if (
            isinstance(source_error_count, bool)
            or not isinstance(source_error_count, int)
            or isinstance(unscanned_count, bool)
            or not isinstance(unscanned_count, int)
            or source_error_count < unscanned_count
        ):
            errors.append("projection_signal_validation_source_counts_mismatch")
    return errors


def _prompt_evidence_shape(value: Any) -> dict[str, Any]:
    """Remove per-occurrence content hashes while retaining causal machine fields."""

    if not isinstance(value, Mapping):
        return {}
    shape: dict[str, Any] = {}
    for section in ("signal", "error", "preflight_shell_capability"):
        raw = value.get(section)
        if not isinstance(raw, Mapping):
            continue
        fields = {str(key): item for key, item in raw.items() if not str(key).endswith("sha256")}
        if fields or section == "signal":
            shape[section] = fields
    return shape


def _operational_prompt_projection(
    *,
    signature_fields: Mapping[str, Any],
    signals: Sequence[Mapping[str, Any]],
    bindings: Sequence[Mapping[str, Any]],
    source_ids: Sequence[str],
    parent_case_ids: Sequence[str],
    occurrence_set_sha256: str,
    receipt_sha256: str,
) -> dict[str, Any]:
    shape_by_json = {
        json.dumps(shape, sort_keys=True, ensure_ascii=True, separators=(",", ":")): shape
        for signal in signals
        if isinstance(signal, Mapping)
        for shape in [_prompt_evidence_shape(signal.get("failure_evidence_projection"))]
    }
    shapes = [shape_by_json[key] for key in sorted(shape_by_json)]
    included_shapes: list[dict[str, Any]] = []
    for shape in shapes[:_MAX_PROMPT_EVIDENCE_SHAPES]:
        candidate_shapes = [*included_shapes, shape]
        if (
            len(
                json.dumps(
                    candidate_shapes,
                    sort_keys=True,
                    ensure_ascii=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            )
            > _MAX_PROMPT_EVIDENCE_SHAPES_BYTES
        ):
            break
        included_shapes.append(shape)
    status_counts: dict[str, int] = {}
    for binding in bindings:
        if not isinstance(binding, Mapping):
            continue
        status = _clean_string(binding.get("status")) or "unknown"
        status_counts[status] = status_counts.get(status, 0) + 1
    projection: dict[str, Any] = {
        "schema_version": 1,
        "candidate_signature": _canonical_sha256(signature_fields),
        "mechanism": dict(signature_fields),
        "occurrence_count": len(signals),
        "source_derived_atom_count": len(source_ids),
        "related_parent_case_count": len(parent_case_ids),
        "parent_binding_status_counts": dict(sorted(status_counts.items())),
        "evidence_shape_count": len(shapes),
        "evidence_shapes_included_count": len(included_shapes),
        "evidence_shapes": included_shapes,
        "evidence_shapes_omitted_count": len(shapes) - len(included_shapes),
        "evidence_shapes_sha256": _canonical_sha256(shapes),
        "occurrence_set_sha256": occurrence_set_sha256,
        "full_receipt_sha256": receipt_sha256,
        "full_occurrence_ledger": (
            "retained_in_operational_candidate_receipt_excluded_from_stage1_prompt"
        ),
    }
    projection["projection_sha256"] = _canonical_sha256(projection)
    return projection


def _candidate_text(prompt_projection: Mapping[str, Any]) -> str:
    return "Automated stage blocker: " + json.dumps(
        prompt_projection,
        sort_keys=True,
        ensure_ascii=True,
        separators=(",", ":"),
    )


def _run_key(record: Mapping[str, Any]) -> str | None:
    for field in ("run_rel", "origin_run_id", "run_id", "run_dir"):
        value = _clean_string(record.get(field))
        if value is not None:
            return value
    return None


def _atom_run_key(atom: Mapping[str, Any]) -> str | None:
    for field in ("origin_run_id", "run_rel", "run_id", "run_dir"):
        value = _clean_string(atom.get(field))
        if value is not None:
            return value
    return None


def _mission_role(record: Mapping[str, Any]) -> tuple[str, str] | None:
    target_ref_raw = record.get("target_ref")
    target_ref = target_ref_raw if isinstance(target_ref_raw, Mapping) else {}
    candidates = [
        _clean_string(target_ref.get("mission_id")),
        _clean_string(target_ref.get("requested_mission_id")),
    ]
    roles = [_MISSION_ROLES[value] for value in candidates if value in _MISSION_ROLES]
    if not roles:
        return None
    if len(set(roles)) != 1:
        return None
    return roles[0]


def _derived_role(
    record: Mapping[str, Any],
    run_atoms: Sequence[Mapping[str, Any]],
) -> tuple[str, str] | None:
    roles = {
        value
        for atom in run_atoms
        for value in [_clean_string(atom.get("evidence_role"))]
        if value in _DERIVED_ROLES
    }
    if len(roles) == 1:
        role = next(iter(roles))
        origin_stages = {
            value
            for atom in run_atoms
            for value in [_clean_string(atom.get("origin_stage"))]
            if value is not None
        }
        return role, (next(iter(origin_stages)) if len(origin_stages) == 1 else role)
    if len(roles) > 1:
        return None
    return _mission_role(record)


def _backend(record: Mapping[str, Any]) -> str | None:
    target_ref_raw = record.get("target_ref")
    target_ref = target_ref_raw if isinstance(target_ref_raw, Mapping) else {}
    effective_raw = record.get("effective_run_spec")
    effective = effective_raw if isinstance(effective_raw, Mapping) else {}
    preflight_raw = record.get("preflight")
    preflight = preflight_raw if isinstance(preflight_raw, Mapping) else {}
    error_raw = record.get("error")
    error = error_raw if isinstance(error_raw, Mapping) else {}
    error_preflight_raw = error.get("preflight")
    error_preflight = error_preflight_raw if isinstance(error_preflight_raw, Mapping) else {}
    shell_capability_raw = error_preflight.get("shell_capability")
    shell_capability = shell_capability_raw if isinstance(shell_capability_raw, Mapping) else {}
    for value in (
        record.get("execution_backend"),
        target_ref.get("execution_backend"),
        effective.get("execution_backend"),
        preflight.get("execution_backend"),
        shell_capability.get("backend"),
    ):
        direct = _clean_string(value)
        if direct is not None:
            return direct.casefold()
        if isinstance(value, Mapping):
            for field in ("name", "backend", "kind", "type"):
                nested = _clean_string(value.get(field))
                if nested is not None:
                    return nested.casefold()
    return None


def _report_schema(record: Mapping[str, Any]) -> str | None:
    target_ref_raw = record.get("target_ref")
    target_ref = target_ref_raw if isinstance(target_ref_raw, Mapping) else {}
    value = _clean_string(target_ref.get("report_schema_path"))
    if value is None:
        return None
    return value.replace("\\", "/").rsplit("/", 1)[-1].casefold()


def _original_scenario_record(
    record: Mapping[str, Any],
    run_atoms: Sequence[Mapping[str, Any]],
) -> bool:
    containers: list[Mapping[str, Any]] = [record]
    for field in ("ticket_ref", "verification_binding", "outcome_record"):
        raw = record.get(field)
        if isinstance(raw, Mapping):
            containers.append(raw)
            binding = raw.get("verification_binding")
            if isinstance(binding, Mapping):
                containers.append(binding)
    for container in containers:
        for field in ("outcome_role", "verification_role", "role", "evidence_kind"):
            if _clean_string(container.get(field)) == "original_scenario":
                return True
        evidence_raw = container.get("original_scenario_evidence")
        evidence = evidence_raw if isinstance(evidence_raw, list) else []
        if any(
            isinstance(item, Mapping)
            and (_clean_string(item.get("result")) or "").casefold() in {"failed", "failure"}
            for item in evidence
        ):
            return True
    return any(
        _clean_string(atom.get("outcome_role")) == "original_scenario"
        or _clean_string(atom.get("verification_role")) == "original_scenario"
        for atom in run_atoms
    )


def _explicit_signal(record: Mapping[str, Any]) -> dict[str, Any] | None:
    raw_signals = record.get("operational_failure_signals")
    signals = raw_signals if isinstance(raw_signals, list) else []
    accepted: list[dict[str, Any]] = []
    for raw in signals:
        if not isinstance(raw, Mapping) or raw.get("prevented_stage") is not True:
            continue
        kind = _clean_string(raw.get("kind"))
        if kind not in _TYPED_SIGNAL_KINDS:
            continue
        if _clean_string(raw.get("outcome_role")) == "original_scenario":
            continue
        accepted.append(
            {
                "failure_class": kind,
                "phase": _clean_string(raw.get("phase")) or "unknown",
                "error_type": _clean_string(raw.get("error_type")),
                "error_subtype": _clean_string(raw.get("error_subtype")),
                "error_code": _clean_string(raw.get("error_code")),
                "backend": _clean_string(raw.get("backend")),
                "report_schema": _clean_string(raw.get("report_schema")),
                "artifact_sha256": (
                    raw.get("artifact_sha256")
                    if _valid_sha256(raw.get("artifact_sha256"))
                    else None
                ),
            }
        )
    if not accepted:
        return None
    return sorted(
        accepted,
        key=lambda item: json.dumps(item, sort_keys=True, ensure_ascii=False),
    )[0]


def _typed_error_signal(record: Mapping[str, Any]) -> dict[str, Any] | None:
    status = (_clean_string(record.get("status")) or "").casefold()
    if status not in {"error", "report_validation_error"}:
        return None
    error_raw = record.get("error")
    error = error_raw if isinstance(error_raw, Mapping) else {}
    error_type = _clean_string(error.get("type"))
    error_subtype = _clean_string(error.get("subtype"))
    error_code = _clean_string(error.get("code"))
    normalized_type = _identifier(error_type) or ""
    classification: tuple[str, str] | None = None
    if normalized_type == "agentexecfailed":
        # AgentExecFailed is only a process-envelope error.  Ordinary implementation
        # mistakes, warning-only stderr, timeouts, and plugin notices all use it.  A
        # candidate requires an exact runner-owned subtype/code; never classify its
        # message or stderr prose.
        for value in (error_subtype, error_code):
            normalized = (value or "").casefold()
            classification = _AGENT_EXEC_SIGNAL_CLASSIFICATION.get(normalized)
            if classification is not None:
                break
            for prefix, failure_class, phase in _CODE_PREFIX_CLASSIFICATION:
                if normalized.startswith(prefix):
                    classification = (failure_class, phase)
                    break
            if classification is not None:
                break
    elif normalized_type == "runtimeerror":
        # runner_core can persist setup failures as RuntimeError before target_ref is
        # available.  Only the exact machine-owned disk-full subtype/code is causal
        # enough to promote; RuntimeError prose is never inspected or classified.
        for value in (error_subtype, error_code):
            if (value or "").casefold() == "disk_full":
                classification = _AGENT_EXEC_SIGNAL_CLASSIFICATION["disk_full"]
                break
    else:
        classification = _ERROR_TYPE_CLASSIFICATION.get(normalized_type)
        if classification is None:
            for value in (error_subtype, error_code):
                normalized = (value or "").casefold()
                for prefix, failure_class, phase in _CODE_PREFIX_CLASSIFICATION:
                    if normalized.startswith(prefix):
                        classification = (failure_class, phase)
                        break
                if classification is not None:
                    break
    if classification is None:
        return None
    failure_class, phase = classification
    details_raw = error.get("details")
    details = details_raw if isinstance(details_raw, Mapping) else {}
    return {
        "failure_class": failure_class,
        "phase": (
            _clean_string(error.get("failure_phase"))
            or _clean_string(error.get("phase"))
            or _clean_string(details.get("phase"))
            or phase
        ),
        "error_type": error_type,
        "error_subtype": error_subtype,
        "error_code": error_code,
        "backend": None,
        "report_schema": None,
        "artifact_sha256": None,
    }


def _policy_signal(record: Mapping[str, Any]) -> dict[str, Any] | None:
    metrics_raw = record.get("metrics")
    metrics = metrics_raw if isinstance(metrics_raw, Mapping) else {}
    blocked = metrics.get("commands_blocked_by_policy")
    if isinstance(blocked, bool) or not isinstance(blocked, int) or blocked <= 0:
        return None
    status = (_clean_string(record.get("status")) or "").casefold()
    report_raw = record.get("report")
    report = report_raw if isinstance(report_raw, Mapping) else {}
    report_status = (_clean_string(report.get("status")) or "").casefold()
    if status not in {"error", "report_validation_error"} and report_status != "failure":
        return None
    return {
        "failure_class": "policy",
        "phase": "agent_execution",
        "error_type": "PolicyBlocked",
        "error_subtype": None,
        "error_code": "commands_blocked_by_policy",
        "backend": None,
        "report_schema": None,
        "artifact_sha256": None,
    }


def _report_contract_signal(record: Mapping[str, Any]) -> dict[str, Any] | None:
    status = (_clean_string(record.get("status")) or "").casefold()
    terminal_raw = record.get("terminal_artifact_reads")
    terminal = terminal_raw if isinstance(terminal_raw, Mapping) else {}
    report_read_raw = terminal.get("report.json")
    report_read = report_read_raw if isinstance(report_read_raw, Mapping) else {}
    unreadable = report_read.get("decode_ok") is False or report_read.get("parse_ok") is False
    if status != "report_validation_error" and not (status == "error" and unreadable):
        return None
    validation_errors = record.get("report_validation_errors")
    validation_projection = validation_errors if isinstance(validation_errors, list) else []
    issue_projection = _report_validation_projection(validation_projection)
    if unreadable:
        issue_projection = {
            "validation_source_error_count": len(validation_projection),
            "validation_errors_unscanned_count": 0,
            "validation_issue_count": 1,
            "validation_issues": [
                {"code": "report_artifact_unreadable", "constraint": "artifact_read"}
            ],
            "validation_issues_omitted_count": 0,
            "validation_issue_set_sha256": _canonical_sha256(
                [{"code": "report_artifact_unreadable", "constraint": "artifact_read"}]
            ),
        }
    return {
        "failure_class": "report_contract",
        "phase": "report_contract",
        "error_type": "ReportValidationError",
        "error_subtype": _clean_string(report_read.get("error_type")),
        "error_code": "report_artifact_unreadable" if unreadable else "schema_validation_failed",
        "backend": None,
        "report_schema": None,
        "artifact_sha256": _canonical_sha256(validation_projection),
        **issue_projection,
    }


def _typed_failure_signal(record: Mapping[str, Any]) -> dict[str, Any] | None:
    status = (_clean_string(record.get("status")) or "").casefold()
    if status in {"nonterminal", "running", "in_progress", "pending"}:
        return None
    return (
        _explicit_signal(record)
        or _report_contract_signal(record)
        or _typed_error_signal(record)
        or _policy_signal(record)
    )


def _normalize_parent_binding(
    value: Mapping[str, Any] | None,
    run_atoms: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    if isinstance(value, Mapping):
        status = _clean_string(value.get("status"))
        case_ids = _string_list(value.get("case_ids"))
        singular = _clean_string(value.get("case_id"))
        if singular is not None:
            case_ids = list(dict.fromkeys([*case_ids, singular]))
        case_ids = sorted(case_ids)
        authority = _clean_string(value.get("authority")) or "caller_binding"
        external_receipt_sha256 = (
            value.get("receipt_sha256") if _valid_sha256(value.get("receipt_sha256")) else None
        )
        if status not in _BINDING_STATUSES:
            status = "conflict" if case_ids else "unavailable"
        if status in {"verified", "reconstructed"} and len(case_ids) != 1:
            status = "conflict" if case_ids else "unavailable"
        if status == "unavailable" and case_ids:
            status = "conflict"
    else:
        case_ids = sorted(
            {
                case_id
                for atom in run_atoms
                for case_id in [_clean_string(atom.get("parent_case_id"))]
                if case_id is not None
            }
        )
        authorities = {
            authority
            for atom in run_atoms
            for raw in [atom.get("lineage_authorities")]
            for authority in (raw if isinstance(raw, list) else [])
            if isinstance(authority, str) and authority.strip()
        }
        if len(case_ids) == 1:
            verified = bool(
                authorities
                & {"runner_evidence_assignment", "runner_ticket_ref", "runner_target_ref"}
            )
            status = "verified" if verified else "reconstructed"
        else:
            status = "conflict" if case_ids else "unavailable"
        authority = ",".join(sorted(authorities)) or "atom_lineage"
        external_receipt_sha256 = None
    binding: dict[str, Any] = {
        "status": status,
        "case_ids": case_ids,
        "authority": authority,
        "external_receipt_sha256": external_receipt_sha256,
    }
    binding["binding_sha256"] = _canonical_sha256(binding)
    return binding


def _signal_receipt(
    *,
    run_id: str,
    signal: Mapping[str, Any],
    record: Mapping[str, Any],
    role: str,
    origin_stage: str,
    run_atoms: Sequence[Mapping[str, Any]],
    parent_binding: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    backend = _clean_string(signal.get("backend")) or _backend(record)
    report_schema = _clean_string(signal.get("report_schema")) or _report_schema(record)
    evidence_projection = _failure_evidence_projection(signal=signal, record=record)
    failure_evidence_sha256 = _canonical_sha256(evidence_projection)
    signature_fields = {
        "producer": OPERATIONAL_CANDIDATE_PRODUCER,
        "failure_class": _identity_token(signal["failure_class"]),
        "phase": _identity_token(signal["phase"]),
        "error_type": _identity_token(signal.get("error_type"), identifier=True),
        "error_subtype": _identity_token(signal.get("error_subtype")),
        "error_code": _identity_token(signal.get("error_code")),
        "backend": _identity_token(backend),
        "report_schema": _identity_report_schema(report_schema),
        "validation_issue_set_sha256": _identity_token(signal.get("validation_issue_set_sha256")),
    }
    signature = _canonical_sha256(signature_fields)
    target_ref_raw = record.get("target_ref")
    structured_projection = {
        "run_id": run_id,
        "signature": signature,
        "origin_role": _identity_token(role),
        "origin_stage": _identity_token(origin_stage),
        "status": _clean_string(record.get("status")),
        "agent_exit_code": record.get("agent_exit_code"),
        "target_ref_sha256": (
            _canonical_sha256(target_ref_raw) if isinstance(target_ref_raw, Mapping) else None
        ),
        "artifact_sha256": signal.get("artifact_sha256"),
        "failure_evidence_projection": evidence_projection,
        "failure_evidence_sha256": failure_evidence_sha256,
        "parent_binding_sha256": parent_binding["binding_sha256"],
    }
    structured_projection["signal_sha256"] = _canonical_sha256(structured_projection)
    structured_projection["signal_id"] = (
        "operational_signal:" + structured_projection["signal_sha256"]
    )

    proposal_ids: list[str] = []
    source_ids: list[str] = []
    context_ids: list[str] = []
    for atom in run_atoms:
        atom_id = _clean_string(atom.get("atom_id"))
        if atom_id is None:
            continue
        source = _clean_string(atom.get("source"))
        evidence_class = _clean_string(atom.get("evidence_class"))
        if source == "suggested_change" or evidence_class == "proposal":
            proposal_ids.append(atom_id)
        elif source in _TYPED_SOURCE_ATOM_KINDS:
            source_ids.append(atom_id)
        else:
            context_ids.append(atom_id)
    occurrence = {
        "run_id": run_id,
        "signal": structured_projection,
        "parent_binding": dict(parent_binding),
        "source_derived_atom_ids": sorted(set(source_ids)),
        "excluded_proposal_atom_ids": sorted(set(proposal_ids)),
        "excluded_context_atom_ids": sorted(set(context_ids)),
    }
    return signature_fields, occurrence


def _candidate_atom(
    signature_fields: Mapping[str, Any],
    occurrences: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    signature = _canonical_sha256(signature_fields)
    signals = sorted(
        [dict(item["signal"]) for item in occurrences],
        key=lambda item: str(item["signal_id"]),
    )
    bindings = sorted(
        [
            {
                "run_id": item["run_id"],
                **dict(item["parent_binding"]),
            }
            for item in occurrences
        ],
        key=lambda item: str(item["run_id"]),
    )
    source_ids = sorted(
        {atom_id for item in occurrences for atom_id in item["source_derived_atom_ids"]}
    )
    proposal_ids = sorted(
        {atom_id for item in occurrences for atom_id in item["excluded_proposal_atom_ids"]}
    )
    context_ids = sorted(
        {atom_id for item in occurrences for atom_id in item["excluded_context_atom_ids"]}
    )
    parent_case_ids = sorted({case_id for binding in bindings for case_id in binding["case_ids"]})
    occurrence_set_projection = {
        "candidate_signature": signature,
        "typed_signal_receipts": signals,
        "parent_bindings": bindings,
        "source_derived_atom_ids": source_ids,
        "excluded_proposal_atom_ids": proposal_ids,
        "excluded_context_atom_ids": context_ids,
    }
    occurrence_set_sha256 = _canonical_sha256(occurrence_set_projection)
    candidate_id = f"operational_failure:{signature}:{occurrence_set_sha256}"
    receipt: dict[str, Any] = {
        "schema_version": OPERATIONAL_CANDIDATE_SCHEMA_VERSION,
        "producer": OPERATIONAL_CANDIDATE_PRODUCER,
        "candidate_id": candidate_id,
        "candidate_signature": signature,
        "occurrence_set_sha256": occurrence_set_sha256,
        "signature_fields": dict(signature_fields),
        "typed_signal_receipts": signals,
        "occurrence_count": len(signals),
        "parent_bindings": bindings,
        "parent_binding_statuses": sorted({str(item["status"]) for item in bindings}),
        "related_parent_case_ids": parent_case_ids,
        "source_derived_atom_ids": source_ids,
        "excluded_proposal_atom_ids": proposal_ids,
        "excluded_context_atom_ids": context_ids,
    }
    receipt["receipt_sha256"] = _canonical_sha256(receipt)
    prompt_projection = _operational_prompt_projection(
        signature_fields=signature_fields,
        signals=signals,
        bindings=bindings,
        source_ids=source_ids,
        parent_case_ids=parent_case_ids,
        occurrence_set_sha256=occurrence_set_sha256,
        receipt_sha256=receipt["receipt_sha256"],
    )
    text = _candidate_text(prompt_projection)
    raw_atom = {
        "atom_id": candidate_id,
        "run_id": candidate_id,
        "run_rel": candidate_id,
        "run_dir": "__operational_failure_candidates__",
        "agent": "runner",
        "status": "observed",
        "timestamp_utc": None,
        "source": "operational_failure_candidate",
        "text": text,
        "evidence_class": "observed",
        "severity_hint": "high",
        "severity_score_hint": 2,
        "origin_run_id": candidate_id,
        "origin_stage": "operational_failure_classification",
        "parent_case_id": None,
        "derived_from_atom_ids": source_ids,
        "evidence_role": "observation",
        "case_id": None,
        "supporting_case_ids": [],
        "disposition": "unresolved",
        "disposition_status": "pending",
        "disposition_receipt": None,
        "path_anchors": [],
        "operational_failure_class": signature_fields["failure_class"],
        "operational_failure_phase": signature_fields["phase"],
        "operational_candidate_signature": signature,
        "operational_candidate_receipt": receipt,
        "operational_candidate_receipt_sha256": receipt["receipt_sha256"],
        "operational_candidate_prompt_projection": prompt_projection,
        "related_parent_case_ids": parent_case_ids,
        "source_derived_atom_ids": source_ids,
    }
    return normalize_atom_lineage([raw_atom], strict_new_output=True)[0]


def build_operational_failure_candidates(
    records: Sequence[Mapping[str, Any]],
    atoms: Sequence[Mapping[str, Any]],
    *,
    parent_bindings_by_run: Mapping[str, Mapping[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Project typed derived-stage blockers into content-addressed observations.

    The function is deliberately unable to classify free-form text.  Every candidate
    originates in a structured runner record or explicit runner-owned signal.  The
    returned atoms are complete stage-1 observation records; input derived atoms are
    neither mutated nor returned as mining candidates.
    """

    atoms_by_run: dict[str, list[Mapping[str, Any]]] = {}
    for atom in atoms:
        run_id = _atom_run_key(atom)
        if run_id is not None:
            atoms_by_run.setdefault(run_id, []).append(atom)
    bindings = parent_bindings_by_run or {}
    grouped: dict[str, tuple[dict[str, Any], list[dict[str, Any]]]] = {}
    observed_signal_ids: set[str] = set()

    for record in records:
        run_id = _run_key(record)
        if run_id is None:
            continue
        run_atoms = atoms_by_run.get(run_id, [])
        role = _derived_role(record, run_atoms)
        if role is None or _original_scenario_record(record, run_atoms):
            continue
        signal = _typed_failure_signal(record)
        if signal is None:
            continue
        parent_binding = _normalize_parent_binding(bindings.get(run_id), run_atoms)
        signature_fields, occurrence = _signal_receipt(
            run_id=run_id,
            signal=signal,
            record=record,
            role=role[0],
            origin_stage=role[1],
            run_atoms=run_atoms,
            parent_binding=parent_binding,
        )
        signal_id = str(occurrence["signal"]["signal_id"])
        if signal_id in observed_signal_ids:
            continue
        observed_signal_ids.add(signal_id)
        signature = _canonical_sha256(signature_fields)
        if signature not in grouped:
            grouped[signature] = (signature_fields, [])
        grouped[signature][1].append(occurrence)

    return [
        _candidate_atom(signature_fields, occurrences)
        for signature_fields, occurrences in (grouped[signature] for signature in sorted(grouped))
    ]


def operational_candidate_receipt_errors(atom: Mapping[str, Any]) -> list[str]:
    """Return integrity errors for one synthetic operational candidate atom."""

    errors: list[str] = []
    receipt_raw = atom.get("operational_candidate_receipt")
    if not isinstance(receipt_raw, Mapping):
        return ["operational_candidate_receipt_missing"]
    receipt = dict(receipt_raw)
    if receipt.get("schema_version") != OPERATIONAL_CANDIDATE_SCHEMA_VERSION:
        errors.append("operational_candidate_schema_version_invalid")
    if receipt.get("producer") != OPERATIONAL_CANDIDATE_PRODUCER:
        errors.append("operational_candidate_producer_invalid")
    if (
        atom.get("source") != "operational_failure_candidate"
        or atom.get("evidence_class") != "observed"
        or atom.get("evidence_role") != "observation"
        or atom.get("origin_stage") != "operational_failure_classification"
    ):
        errors.append("operational_candidate_atom_contract_invalid")
    signature_fields = receipt.get("signature_fields")
    if not isinstance(signature_fields, Mapping):
        errors.append("operational_candidate_signature_fields_invalid")
        signature = None
    else:
        signature = _canonical_sha256(signature_fields)
        if receipt.get("candidate_signature") != signature:
            errors.append("operational_candidate_signature_hash_mismatch")
        if set(signature_fields) != set(_STABLE_SIGNATURE_FIELDS):
            errors.append("operational_candidate_signature_fields_contract_invalid")
        if signature_fields.get("producer") != OPERATIONAL_CANDIDATE_PRODUCER:
            errors.append("operational_candidate_signature_producer_invalid")
        for field in _STABLE_SIGNATURE_FIELDS - {
            "producer",
            "error_type",
            "report_schema",
        }:
            value = signature_fields.get(field)
            if value is not None and _identity_token(value) != value:
                errors.append(f"operational_candidate_signature_token_invalid:{field}")
        error_type = signature_fields.get("error_type")
        if error_type is not None and _identity_token(error_type, identifier=True) != error_type:
            errors.append("operational_candidate_signature_token_invalid:error_type")
        report_schema = signature_fields.get("report_schema")
        if report_schema is not None and _identity_report_schema(report_schema) != report_schema:
            errors.append("operational_candidate_signature_token_invalid:report_schema")
    if atom.get("operational_candidate_signature") != signature:
        errors.append("operational_candidate_identity_mismatch")

    signals_raw = receipt.get("typed_signal_receipts")
    signals = signals_raw if isinstance(signals_raw, list) else []
    if not signals:
        errors.append("operational_candidate_typed_signals_missing")
    signal_ids: list[str] = []
    for index, raw in enumerate(signals):
        if not isinstance(raw, Mapping):
            errors.append(f"operational_candidate_signal_invalid:{index}")
            continue
        signal = dict(raw)
        supplied_sha = signal.pop("signal_sha256", None)
        supplied_id = signal.pop("signal_id", None)
        expected_sha = _canonical_sha256(signal)
        evidence_projection = signal.get("failure_evidence_projection")
        for projection_error in _failure_evidence_projection_errors(evidence_projection):
            errors.append(f"operational_candidate_signal_evidence_{projection_error}:{index}")
        if signal.get("failure_evidence_sha256") != _canonical_sha256(evidence_projection):
            errors.append(f"operational_candidate_signal_evidence_hash_mismatch:{index}")
        if supplied_sha != expected_sha or supplied_id != f"operational_signal:{expected_sha}":
            errors.append(f"operational_candidate_signal_hash_mismatch:{index}")
        elif signal.get("signature") != signature:
            errors.append(f"operational_candidate_signal_signature_mismatch:{index}")
        elif isinstance(supplied_id, str):
            signal_ids.append(supplied_id)
    if signal_ids != sorted(set(signal_ids)):
        errors.append("operational_candidate_signals_not_sorted_unique")
    if receipt.get("occurrence_count") != len(signals):
        errors.append("operational_candidate_occurrence_count_mismatch")

    bindings_raw = receipt.get("parent_bindings")
    bindings = bindings_raw if isinstance(bindings_raw, list) else []
    binding_run_ids: list[str] = []
    binding_case_ids: set[str] = set()
    binding_statuses: set[str] = set()
    for index, raw in enumerate(bindings):
        if not isinstance(raw, Mapping):
            errors.append(f"operational_candidate_parent_binding_invalid:{index}")
            continue
        binding = dict(raw)
        run_id = binding.pop("run_id", None)
        supplied_sha = binding.pop("binding_sha256", None)
        case_ids = _string_list(binding.get("case_ids"))
        status = _clean_string(binding.get("status"))
        if isinstance(run_id, str):
            binding_run_ids.append(run_id)
        binding_case_ids.update(case_ids)
        if status is not None:
            binding_statuses.add(status)
        if run_id is None or supplied_sha != _canonical_sha256(binding):
            errors.append(f"operational_candidate_parent_binding_hash_mismatch:{index}")
        if status not in _BINDING_STATUSES:
            errors.append(f"operational_candidate_parent_binding_status_invalid:{index}")
        elif status in {"verified", "reconstructed"} and len(case_ids) != 1:
            errors.append(f"operational_candidate_parent_binding_case_invalid:{index}")
        elif status == "unavailable" and case_ids:
            errors.append(f"operational_candidate_parent_binding_unavailable_has_case:{index}")
    if binding_run_ids != sorted(set(binding_run_ids)):
        errors.append("operational_candidate_parent_bindings_not_sorted_unique")
    if len(bindings) != len(signals):
        errors.append("operational_candidate_parent_binding_coverage_mismatch")
    if receipt.get("parent_binding_statuses") != sorted(binding_statuses):
        errors.append("operational_candidate_parent_binding_statuses_mismatch")
    if receipt.get("related_parent_case_ids") != sorted(binding_case_ids):
        errors.append("operational_candidate_parent_case_ids_mismatch")

    source_ids = _string_list(receipt.get("source_derived_atom_ids"))
    proposal_ids = _string_list(receipt.get("excluded_proposal_atom_ids"))
    context_ids = _string_list(receipt.get("excluded_context_atom_ids"))
    if source_ids != receipt.get("source_derived_atom_ids") or source_ids != sorted(source_ids):
        errors.append("operational_candidate_source_atom_ids_invalid")
    if proposal_ids != receipt.get("excluded_proposal_atom_ids") or proposal_ids != sorted(
        proposal_ids
    ):
        errors.append("operational_candidate_proposal_atom_ids_invalid")
    if context_ids != receipt.get("excluded_context_atom_ids") or context_ids != sorted(
        context_ids
    ):
        errors.append("operational_candidate_context_atom_ids_invalid")
    if set(source_ids) & set(proposal_ids):
        errors.append("operational_candidate_source_proposal_overlap")
    if atom.get("source_derived_atom_ids") != source_ids:
        errors.append("operational_candidate_atom_source_ids_mismatch")
    if atom.get("related_parent_case_ids") != receipt.get("related_parent_case_ids"):
        errors.append("operational_candidate_atom_parent_cases_mismatch")
    if isinstance(signature_fields, Mapping):
        if atom.get("operational_failure_class") != signature_fields.get(
            "failure_class"
        ) or atom.get("operational_failure_phase") != signature_fields.get("phase"):
            errors.append("operational_candidate_atom_failure_identity_mismatch")

    occurrence_set_projection = {
        "candidate_signature": signature,
        "typed_signal_receipts": signals,
        "parent_bindings": bindings,
        "source_derived_atom_ids": source_ids,
        "excluded_proposal_atom_ids": proposal_ids,
        "excluded_context_atom_ids": context_ids,
    }
    occurrence_set_sha256 = _canonical_sha256(occurrence_set_projection)
    candidate_id = (
        f"operational_failure:{signature}:{occurrence_set_sha256}"
        if signature is not None
        else None
    )
    if receipt.get("occurrence_set_sha256") != occurrence_set_sha256:
        errors.append("operational_candidate_occurrence_set_hash_mismatch")
    if (
        candidate_id is None
        or receipt.get("candidate_id") != candidate_id
        or atom.get("atom_id") != candidate_id
    ):
        errors.append("operational_candidate_identity_mismatch")

    supplied_receipt_sha = receipt.get("receipt_sha256")
    if isinstance(signature_fields, Mapping) and isinstance(supplied_receipt_sha, str):
        expected_prompt_projection = _operational_prompt_projection(
            signature_fields=signature_fields,
            signals=signals,
            bindings=bindings,
            source_ids=source_ids,
            parent_case_ids=sorted(binding_case_ids),
            occurrence_set_sha256=occurrence_set_sha256,
            receipt_sha256=supplied_receipt_sha,
        )
        if atom.get("operational_candidate_prompt_projection") != expected_prompt_projection:
            errors.append("operational_candidate_prompt_projection_mismatch")
        if atom.get("text") != _candidate_text(expected_prompt_projection):
            errors.append("operational_candidate_text_projection_mismatch")
    else:
        errors.append("operational_candidate_prompt_projection_unverifiable")

    supplied_receipt_sha = receipt.pop("receipt_sha256", None)
    expected_receipt_sha = _canonical_sha256(receipt)
    if supplied_receipt_sha != expected_receipt_sha:
        errors.append("operational_candidate_receipt_hash_mismatch")
    if atom.get("operational_candidate_receipt_sha256") != supplied_receipt_sha:
        errors.append("operational_candidate_atom_receipt_hash_mismatch")
    return errors


__all__ = [
    "OPERATIONAL_CANDIDATE_PRODUCER",
    "OPERATIONAL_CANDIDATE_SCHEMA_VERSION",
    "build_operational_failure_candidates",
    "operational_candidate_receipt_errors",
]
