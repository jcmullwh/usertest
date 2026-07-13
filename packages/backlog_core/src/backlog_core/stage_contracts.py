"""Stage artifact parsers, normalizers, and document builders for the six-stage pipeline.

Each ``parse_*`` function accepts a raw text string (typically from a prompt response),
extracts a JSON list or object, validates the stage-specific schema, and returns a
``(items, warnings)`` tuple.  Warnings are non-fatal observations; errors raise
``ValueError``.

No item is silently dropped.  When an item is malformed the parser includes it in the
output with a ``_parse_warning`` key so callers can surface the issue rather than hide
it.

Design contract
---------------
- ``parse_problem_record_list`` rejects any item that contains solution fields
  (``proposed_fix``, ``selected_solution``, ``family_id``, ``option_id``,
  ``implementation_steps``).
- ``parse_research_dossier_list`` treats the current research-proof contract as
  strict: malformed or incomplete proof records raise ``ValueError`` and
  ``implementation_performed`` must be exactly ``false``. Historical dossiers can
  only be read through the explicit ``legacy=True`` compatibility path.
- ``parse_solution_option_sets`` rejects any item with ``selected_solution``.
- ``build_stage_document`` is the single place that knows how to wrap stage items into
  the standard artifact envelope.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Mapping
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import PurePosixPath
from typing import Any
from uuid import UUID

from backlog_core.causal_proof import (
    command_authorization_errors,
    command_authorization_identity,
    material_unknowns_block_advancement,
    proof_predicate_contract_errors,
    validate_causal_proof_receipt,
)

_LOG = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Required and forbidden field contracts
# ---------------------------------------------------------------------------

_PROBLEM_RECORD_REQUIRED: tuple[str, ...] = (
    "problem_id",
    "title",
    "problem",
    "user_impact",
    "severity",
    "confidence",
    "evidence_atom_ids",
    "evidence_summary",
)
_PROBLEM_RECORD_FORBIDDEN: tuple[str, ...] = (
    "proposed_fix",
    "selected_solution",
    "family_id",
    "option_id",
    "implementation_steps",
)
_VALID_SEVERITIES: frozenset[str] = frozenset({"low", "medium", "high", "blocker"})
_VALID_PROBLEM_STATUSES: frozenset[str] = frozenset({"identified"})

_PRIORITY_DECISION_REQUIRED: tuple[str, ...] = (
    "problem_id",
    "priority_bucket",
    "selected_for_research",
    "priority_rationale",
)
_VALID_PRIORITY_BUCKETS: frozenset[str] = frozenset({"p0", "p1", "p2", "p3", "watch"})

_LEGACY_RESEARCH_DOSSIER_REQUIRED: tuple[str, ...] = (
    "problem_id",
    "reproduction_status",
    "writes_used",
    "writes_purpose",
    "implementation_performed",
    "root_cause_hypotheses",
    "broader_class_assessment",
    "unknowns",
)
_RESEARCH_DOSSIER_REQUIRED: tuple[str, ...] = (
    "research_schema_version",
    "case_id",
    "problem_id",
    "repo_revision",
    "research_method",
    "reproduction_status",
    "research_status",
    "writes_used",
    "writes_purpose",
    "implementation_performed",
    "diff_classification",
    "artifact_refs",
    "experiments",
    "inspected_files",
    "inspected_symbols",
    "root_cause_hypotheses",
    "root_cause_confidence",
    "broader_class_assessment",
    "material_unknowns",
    "blocking_reasons",
    "evidence_boundaries",
    "evidence_assignment",
    "evidence_verification",
)
_RESEARCH_DOSSIER_OUTPUT_REQUIRED: tuple[str, ...] = tuple(
    field
    for field in _RESEARCH_DOSSIER_REQUIRED
    if field
    not in {
        "research_schema_version",
        "repo_revision",
        "diff_classification",
        "evidence_assignment",
        "evidence_verification",
    }
)
_RESEARCH_DOSSIER_RUNNER_FIELDS: tuple[str, ...] = (
    "repo_workspace",
    "run_dir",
    "runner_exit_code",
    "runner_report_validation_errors",
    "diff_suspicious_reasons",
    "artifacts",
    "post_research_same_mechanism_bundle",
    "research_attempts",
)
_RESEARCH_DOSSIER_ALLOWED: frozenset[str] = frozenset(
    (*_RESEARCH_DOSSIER_REQUIRED, *_RESEARCH_DOSSIER_RUNNER_FIELDS)
)
_VALID_REPRODUCTION_STATUSES: frozenset[str] = frozenset(
    {"reproduced", "reproduction_failed", "partial", "blocked"}
)
_VALID_RESEARCH_STATUSES: frozenset[str] = frozenset(
    {"evidence_sufficient", "insufficient_evidence", "blocked"}
)
_VALID_EXPERIMENT_OUTCOMES: frozenset[str] = frozenset({"supports", "refutes", "inconclusive"})
_VALID_FALSIFICATION_ATTEMPT_OUTCOMES: frozenset[str] = frozenset(
    {"survived", "disproved", "inconclusive"}
)
_PLATFORM_REQUIREMENT_RE = re.compile(r"[a-z0-9][a-z0-9_.:/+@-]*")
_REPLAY_ENVIRONMENT_KEY_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_VALID_MECHANISM_EVIDENCE_TYPES: frozenset[str] = frozenset(
    {
        "exception_trace",
        "observed_output",
        "controlled_scenario",
        "temporary_harness",
        "static_trace",
        "live_runtime",
        "adapter_proof",
    }
)
_VALID_HYPOTHESIS_DISPOSITIONS: frozenset[str] = frozenset(
    {"primary", "refuted", "plausible", "unresolved"}
)
_VALID_ASSERTION_SOURCES: frozenset[str] = frozenset({"exit_code", "stdout", "stderr", "combined"})
_VALID_ASSERTION_OPERATORS: frozenset[str] = frozenset({"equals", "contains", "not_contains"})
RESEARCH_PROOF_SCHEMA_VERSION = 3
# Compatibility export only. Confidence is typed telemetry, not an advancement threshold.
MIN_RESEARCH_CONFIDENCE_FOR_READY = 0.0
_VALID_BROADER_CLASS: frozenset[str] = frozenset(
    {"isolated_instance", "repeated_variant", "unknown"}
)
_VALID_DIFF_CLASSIFICATIONS: frozenset[str] = frozenset(
    {"allowed_research_edits", "suspicious_implementation", "no_changes"}
)

_RESEARCH_CLAIM_FIELDS: tuple[str, ...] = (
    "research_schema_version",
    "case_id",
    "problem_id",
    "repo_revision",
    "research_method",
    "reproduction_status",
    "research_status",
    "writes_used",
    "writes_purpose",
    "implementation_performed",
    "diff_classification",
    "artifact_refs",
    "experiments",
    "inspected_files",
    "inspected_symbols",
    "root_cause_hypotheses",
    "root_cause_confidence",
    "broader_class_assessment",
    "material_unknowns",
    "blocking_reasons",
    "evidence_boundaries",
    "evidence_assignment",
)


def _canonical_sha256(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return sha256(encoded.encode()).hexdigest()


def research_claims_sha256(item: dict[str, Any]) -> str:
    """Hash every decision-relevant research claim, excluding runner receipts."""
    claims = {field: item.get(field) for field in _RESEARCH_CLAIM_FIELDS}
    # Attempt history is optional for compatibility with proofs created before
    # bounded output-contract retries existed.  When present, bind the complete
    # raw-attempt history into the proof so it cannot be silently rewritten.
    if "research_attempts" in item:
        claims["research_attempts"] = item.get("research_attempts")
    return _canonical_sha256(claims)


def research_attempt_sha256(attempt: dict[str, Any]) -> str:
    """Hash one retained research attempt without its self hash."""
    return _canonical_sha256(
        {key: value for key, value in attempt.items() if key != "attempt_sha256"}
    )


def evidence_assignment_sha256(assignment: dict[str, Any]) -> str:
    """Hash a runner-owned origin-evidence assignment without its self hash."""
    return _canonical_sha256(
        {key: value for key, value in assignment.items() if key != "assignment_sha256"}
    )


def evidence_verification_sha256(receipt: dict[str, Any]) -> str:
    """Hash a runner-owned verification receipt without its self hash."""
    return _canonical_sha256(
        {key: value for key, value in receipt.items() if key != "receipt_sha256"}
    )


def research_prompt_projection(item: dict[str, Any]) -> dict[str, Any]:
    """Return only claims and runner receipts covered by stage-3 hashes.

    Runner convenience metadata (workspace shortcuts, rendered artifact maps,
    parse diagnostics) is intentionally excluded.  Callers must not interpolate
    the original dossier into a downstream model prompt.
    """

    errors = _validate_research_dossier(item)
    if errors:
        raise ValueError("research_prompt_projection_invalid: " + "; ".join(errors))
    projection = {
        **{field: item[field] for field in _RESEARCH_CLAIM_FIELDS},
        "evidence_verification": item["evidence_verification"],
    }
    if "post_research_same_mechanism_bundle" in item:
        projection["post_research_same_mechanism_bundle"] = item[
            "post_research_same_mechanism_bundle"
        ]
    return projection


_SOLUTION_OPTION_REQUIRED: tuple[str, ...] = (
    "option_id",
    "problem_id",
    "summary",
    "tradeoffs",
    "recurrence_prevention",
    "change_surface_hypothesis",
    "test_implications",
    "rationale",
)
_SOLUTION_OPTION_FORBIDDEN: tuple[str, ...] = ("selected_solution",)

_SELECTION_DECISION_REQUIRED: tuple[str, ...] = (
    "problem_id",
    "selected_option_id",
    "selection_rationale",
    "repo_intent_alignment",
    "why_other_options_were_not_selected",
    "needs_ux_review",
)

_CHANGE_PLAN_REQUIRED: tuple[str, ...] = (
    "change_plan_id",
    "case_id",
    "problem_id",
    "selected_option_id",
    "title",
    "problem",
    "user_impact",
    "proposed_fix",
    "implementation_steps",
    "verification_steps",
    "success_criteria",
    "rollback_notes",
    "suggested_owner",
    "related_change_plan_ids",
    "repo_revision",
    "change_targets",
    "target_contract",
    "verification_commands",
    "outcome_verification_roles",
    "before_after_reproduction",
    "compatibility_and_failure_modes",
    "causal_coverage",
    "scope_evidence",
    "requires_live_verification",
)

# ---------------------------------------------------------------------------
# JSON extraction helpers
# ---------------------------------------------------------------------------

_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)


def _extract_json(text: str) -> Any:
    """Extract the first valid JSON value from *text*.

    Tries the full text first, then fenced blocks, then the first JSON array or
    object found by bracket scanning.

    Parameters
    ----------
    text:
        Raw text to parse.

    Returns
    -------
    Any
        Parsed JSON value.

    Raises
    ------
    ValueError
        When no valid JSON could be extracted.
    """
    text = text.strip()
    if not text:
        raise ValueError("empty_response: no content to parse")

    # 1. Try full text.
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # 2. Try code-fenced JSON.
    for match in _JSON_FENCE_RE.finditer(text):
        try:
            return json.loads(match.group(1).strip())
        except json.JSONDecodeError:
            continue

    # 3. Bracket scan for the first JSON array or object.
    for start_char, end_char in [("[", "]"), ("{", "}")]:
        idx = text.find(start_char)
        if idx == -1:
            continue
        depth = 0
        in_string = False
        escape_next = False
        for end_idx in range(idx, len(text)):
            ch = text[end_idx]
            if escape_next:
                escape_next = False
                continue
            if ch == "\\" and in_string:
                escape_next = True
                continue
            if ch == '"':
                in_string = not in_string
                continue
            if in_string:
                continue
            if ch == start_char:
                depth += 1
            elif ch == end_char:
                depth -= 1
                if depth == 0:
                    candidate = text[idx : end_idx + 1]
                    try:
                        return json.loads(candidate)
                    except json.JSONDecodeError:
                        break

    raise ValueError(
        f"no_valid_json: could not extract JSON from response text ({len(text)} chars)"
    )


def _as_list(value: Any) -> list[Any]:
    """Coerce *value* to a list.

    If the value is already a list, return it.

    Some models "wrap" a list inside an object (for example ``{"items": [...]}``).
    We want to accept these wrappers, but we must avoid accidentally unwrapping
    legitimate stage items that contain list-valued fields (for example a single
    priority-decision dict with ``evidence_atom_ids_used=[...]``).

    Therefore we only unwrap when the dict either:
    - has exactly one key and its value is a list, OR
    - contains a known wrapper key whose value is a list.

    Otherwise, wrap the dict in a list.

    Parameters
    ----------
    value:
        Parsed JSON value.

    Returns
    -------
    list[Any]
        Coerced list.
    """
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        if len(value) == 1:
            only_value = next(iter(value.values()))
            return only_value if isinstance(only_value, list) else [value]

        wrapper_keys = (
            "items",
            "problem_records",
            "priority_decisions",
            "research_dossiers",
            "solution_options",
            "selection_decisions",
            "change_plans",
        )
        for key in wrapper_keys:
            candidate = value.get(key)
            if isinstance(candidate, list):
                return candidate
    return [value]


# ---------------------------------------------------------------------------
# Per-item validators
# ---------------------------------------------------------------------------


def _validate_problem_record(item: dict[str, Any]) -> list[str]:
    """Return warning strings for a single problem record.

    Parameters
    ----------
    item:
        Candidate problem record dict.

    Returns
    -------
    list[str]
        Validation warnings.  An empty list means the record is valid.
    """
    warnings: list[str] = []
    pid = item.get("problem_id") or "(no problem_id)"

    for field in _PROBLEM_RECORD_REQUIRED:
        if field not in item:
            warnings.append(f"problem_record_missing_required_field: {pid}: {field}")

    # These fields are the actual observed-problem claim.  Merely including the
    # keys (with empty strings) produces a structurally complete artifact that
    # carries no problem statement into research.  New stage-1 output must be
    # substantive enough for an independent evidence review to evaluate it.
    for field in ("title", "problem", "user_impact", "evidence_summary"):
        value = item.get(field)
        if not isinstance(value, str) or not value.strip():
            warnings.append(f"problem_record_empty_required_text: {pid}: {field}")

    for field in _PROBLEM_RECORD_FORBIDDEN:
        if field in item:
            warnings.append(
                f"problem_record_contains_forbidden_field: {pid}: {field} "
                "(solution fields are not allowed in stage-1 problem records)"
            )

    sev = item.get("severity")
    if sev is not None and (not isinstance(sev, str) or sev not in _VALID_SEVERITIES):
        warnings.append(f"problem_record_invalid_severity: {pid}: {sev!r}")

    conf = item.get("confidence")
    if isinstance(conf, bool) or (conf is not None and not isinstance(conf, (int, float))):
        warnings.append(f"problem_record_invalid_confidence_type: {pid}: {type(conf).__name__}")
    elif conf is not None and not (0.0 <= float(conf) <= 1.0):
        warnings.append(f"problem_record_confidence_out_of_range: {pid}: {conf}")

    eids = item.get("evidence_atom_ids")
    if not isinstance(eids, list) or len(eids) == 0:
        warnings.append(f"problem_record_empty_evidence_atom_ids: {pid}")

    status = item.get("problem_status")
    if status is not None and (
        not isinstance(status, str) or status not in _VALID_PROBLEM_STATUSES
    ):
        warnings.append(f"problem_record_invalid_status: {pid}: {status!r}")

    return warnings


def _validate_priority_decision(item: dict[str, Any]) -> list[str]:
    """Return warning strings for a single priority decision.

    Parameters
    ----------
    item:
        Candidate priority decision dict.

    Returns
    -------
    list[str]
        Validation warnings.
    """
    warnings: list[str] = []
    pid = item.get("problem_id") or "(no problem_id)"

    for field in _PRIORITY_DECISION_REQUIRED:
        if field not in item:
            warnings.append(f"priority_decision_missing_required_field: {pid}: {field}")

    bucket = item.get("priority_bucket")
    if bucket is not None and (
        not isinstance(bucket, str) or bucket not in _VALID_PRIORITY_BUCKETS
    ):
        warnings.append(f"priority_decision_invalid_bucket: {pid}: {bucket!r}")

    sfr = item.get("selected_for_research")
    if sfr is not None and not isinstance(sfr, bool):
        warnings.append(
            f"priority_decision_invalid_selected_for_research_type: {pid}: "
            f"{type(sfr).__name__} (must be bool)"
        )

    return warnings


def _is_nonempty_string(value: Any) -> bool:
    """Return whether *value* is a string containing non-whitespace text."""
    return isinstance(value, str) and bool(value.strip())


def _canonical_uuid_string(value: Any) -> str | None:
    """Return the canonical UUID spelling or ``None`` for non-UUID input."""
    if not isinstance(value, str):
        return None
    try:
        return str(UUID(value))
    except (AttributeError, ValueError):
        return None


def _expected_semantic_field_path(value: Any) -> bool:
    """Accept only origin fields objectively named as desired behavior."""

    if not isinstance(value, str):
        return False
    names = re.findall(r"\.([A-Za-z_][A-Za-z0-9_:-]*)", value)
    if not names:
        return False
    terminal = names[-1].casefold()
    exact = terminal in {
        "expected",
        "desired",
        "correct_value",
        "expected_behavior",
        "desired_behavior",
        "intended_behavior",
        "required_behavior",
        "success_criteria",
    }
    prefixed = terminal.startswith(("expected_", "desired_", "correct_", "intended_", "required_"))
    proposal_tokens = {
        "actual",
        "impact",
        "effect",
        "benefit",
        "context",
        "diagnostic",
        "error",
        "failure",
        "observed",
        "risk",
        "symptom",
        "change",
        "fix",
        "solution",
        "implementation",
        "plan",
        "estimate",
        "proposal",
        "recommendation",
    }
    return (exact or prefixed) and not any(
        token in proposal_tokens for token in terminal.split("_")
    )


def _source_observation_atom(snapshot: Any) -> bool:
    if not isinstance(snapshot, dict) or snapshot.get("evidence_role") != "observation":
        return False
    if str(snapshot.get("origin_stage") or "").casefold() in {
        "repro_research",
        "research",
        "implementation",
        "verification",
    }:
        return False
    proposal_kinds = {
        "idea",
        "proposal",
        "recommendation",
        "suggested_change",
        "suggestion",
    }
    return not any(
        str(snapshot.get(field) or "").casefold() in proposal_kinds
        for field in ("category", "kind", "source", "surface_kind")
    )


def _semantic_quote_field_path(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    names = re.findall(r"\.([A-Za-z_][A-Za-z0-9_:-]*)", value)
    if not names:
        return False
    terminal = names[-1].casefold()
    forbidden = {
        "actual",
        "context",
        "error",
        "impact",
        "observed",
        "proposal",
        "recommendation",
        "suggested_change",
        "symptom",
    }
    return not any(token in forbidden for token in terminal.split("_"))


def _expectation_quote(value: Any, *, expected_value: Any) -> bool:
    del expected_value
    return isinstance(value, str) and bool(value.strip())


def _validate_string_list(
    value: Any,
    *,
    field: str,
    pid: str,
    require_nonempty: bool = False,
) -> list[str]:
    """Validate a list whose members must all be non-empty strings."""
    errors: list[str] = []
    if not isinstance(value, list):
        return [f"research_dossier_invalid_{field}_type: {pid}: {type(value).__name__}"]
    if require_nonempty and not value:
        errors.append(f"research_dossier_empty_{field}: {pid}")
    for idx, entry in enumerate(value):
        if not _is_nonempty_string(entry):
            errors.append(
                f"research_dossier_invalid_{field}_entry: {pid}: index={idx} "
                f"type={type(entry).__name__}"
            )
    return errors


def _validate_artifact_refs(value: Any, *, pid: str) -> list[str]:
    """Validate structured evidence-artifact references."""
    if not isinstance(value, list):
        return [f"research_dossier_invalid_artifact_refs_type: {pid}: {type(value).__name__}"]
    errors: list[str] = []
    seen_ids: set[str] = set()
    for idx, ref in enumerate(value):
        if not isinstance(ref, dict):
            errors.append(
                f"research_dossier_invalid_artifact_ref: {pid}: index={idx} "
                f"type={type(ref).__name__}"
            )
            continue
        for field in ("artifact_id", "kind", "path"):
            if not _is_nonempty_string(ref.get(field)):
                errors.append(f"research_dossier_invalid_artifact_ref_{field}: {pid}: index={idx}")
        artifact_id = ref.get("artifact_id")
        if _is_nonempty_string(artifact_id):
            if artifact_id in seen_ids:
                errors.append(f"research_dossier_duplicate_artifact_id: {pid}: {artifact_id}")
            seen_ids.add(str(artifact_id))
        description = ref.get("description")
        if description is not None and not _is_nonempty_string(description):
            errors.append(f"research_dossier_invalid_artifact_ref_description: {pid}: index={idx}")
    return errors


def _validate_research_attempts(value: Any, *, pid: str) -> list[str]:
    """Validate runner-owned, non-advancing research-attempt provenance."""
    if value is None:
        return []
    if not isinstance(value, list) or not value:
        return [f"research_dossier_invalid_research_attempts_type: {pid}: {type(value).__name__}"]

    allowed_fields = {
        "attempt_number",
        "attempt_kind",
        "outcome",
        "run_dir",
        "report_path",
        "validation_errors",
        "validation_errors_before",
        "validation_errors_after",
        "attempted_dossier",
        "attempted_dossier_sha256",
        "source_attempt_sha256",
        "authorized_paths",
        "baseline_dossier_sha256",
        "baseline_projection_sha256",
        "repair_contract_sha256",
        "agent_session_id",
        "observed_agent_session_id",
        "resumed_from_session_id",
        "attempt_wall_seconds",
        "repair_progress",
        "attempt_artifacts",
        "attempt_sha256",
    }
    valid_outcomes = {
        "output_contract_valid",
        "output_contract_invalid",
        "runner_contract_invalid",
        "invocation_failed",
        "repair_contract_valid",
        "repair_contract_invalid",
        "repair_scope_rejected",
        "evidence_verification_invalid",
        "external_wait",
    }
    valid_attempt_kinds = {
        "full_research",
        "model_output_repair",
        "fresh_research_retry",
        "evidence_verification_feedback",
        "evidence_verification_dossier_repair",
        "evidence_verification_research_continuation",
    }
    errors: list[str] = []
    seen_numbers: set[int] = set()
    current_contract_flags: list[bool] = []
    attempts_by_hash: dict[str, dict[str, Any]] = {}
    for index, attempt in enumerate(value):
        if not isinstance(attempt, dict):
            errors.append(
                f"research_dossier_invalid_research_attempt: {pid}: index={index} "
                f"type={type(attempt).__name__}"
            )
            continue
        unknown = sorted(set(attempt) - allowed_fields)
        if unknown:
            errors.append(
                f"research_dossier_unknown_research_attempt_fields: {pid}: "
                f"index={index} fields={unknown!r}"
            )
        attempt_kind = attempt.get("attempt_kind")
        current_contract = attempt_kind is not None
        current_contract_flags.append(current_contract)
        if current_contract and attempt_kind not in valid_attempt_kinds:
            errors.append(f"research_dossier_invalid_research_attempt_kind: {pid}: index={index}")
        attempt_number = attempt.get("attempt_number")
        if (
            isinstance(attempt_number, bool)
            or not isinstance(attempt_number, int)
            or attempt_number < 1
        ):
            errors.append(f"research_dossier_invalid_research_attempt_number: {pid}: index={index}")
        elif attempt_number in seen_numbers:
            errors.append(
                f"research_dossier_duplicate_research_attempt_number: {pid}: {attempt_number}"
            )
        else:
            seen_numbers.add(attempt_number)
        if attempt.get("outcome") not in valid_outcomes:
            errors.append(
                f"research_dossier_invalid_research_attempt_outcome: {pid}: index={index}"
            )
        invocation_failed = attempt.get("outcome") == "invocation_failed"
        for field in ("run_dir", "report_path"):
            valid_path = (
                attempt.get(field) is None
                if invocation_failed
                else _is_nonempty_string(attempt.get(field))
            )
            if not valid_path:
                errors.append(
                    f"research_dossier_invalid_research_attempt_{field}: {pid}: index={index}"
                )
        errors.extend(
            _validate_string_list(
                attempt.get("validation_errors"),
                field=f"research_attempts_{index}_validation_errors",
                pid=pid,
            )
        )
        if current_contract:
            agent_session_id = attempt.get("agent_session_id")
            if agent_session_id is not None and (
                _canonical_uuid_string(agent_session_id) != agent_session_id
            ):
                errors.append(
                    f"research_dossier_research_attempt_session_id_invalid: {pid}: index={index}"
                )
            observed_session_id = attempt.get("observed_agent_session_id")
            if observed_session_id is not None and (
                _canonical_uuid_string(observed_session_id) != observed_session_id
            ):
                errors.append(
                    f"research_dossier_research_attempt_observed_session_id_invalid: "
                    f"{pid}: index={index}"
                )
            errors.extend(
                _validate_string_list(
                    attempt.get("validation_errors_before"),
                    field=f"research_attempts_{index}_validation_errors_before",
                    pid=pid,
                )
            )
            errors.extend(
                _validate_string_list(
                    attempt.get("validation_errors_after"),
                    field=f"research_attempts_{index}_validation_errors_after",
                    pid=pid,
                )
            )
            if attempt.get("validation_errors_after") != attempt.get("validation_errors"):
                errors.append(
                    f"research_dossier_research_attempt_error_projection_mismatch: "
                    f"{pid}: index={index}"
                )
            authorized_paths = attempt.get("authorized_paths")
            errors.extend(
                _validate_string_list(
                    authorized_paths,
                    field=f"research_attempts_{index}_authorized_paths",
                    pid=pid,
                )
            )
            source_attempt_sha256 = attempt.get("source_attempt_sha256")
            baseline_dossier_sha256 = attempt.get("baseline_dossier_sha256")
            baseline_projection_sha256 = attempt.get("baseline_projection_sha256")
            repair_contract_sha256 = attempt.get("repair_contract_sha256")
            if attempt_kind == "full_research":
                if (
                    any(
                        field is not None
                        for field in (
                            source_attempt_sha256,
                            baseline_dossier_sha256,
                            baseline_projection_sha256,
                            repair_contract_sha256,
                        )
                    )
                    or authorized_paths != []
                    or attempt.get("validation_errors_before") != []
                ):
                    errors.append(
                        f"research_dossier_initial_research_attempt_has_retry_provenance: "
                        f"{pid}: index={index}"
                    )
            elif attempt_kind == "evidence_verification_feedback":
                if (
                    not _valid_sha256(source_attempt_sha256)
                    or baseline_dossier_sha256 is not None
                    or baseline_projection_sha256 is not None
                    or repair_contract_sha256 is not None
                    or authorized_paths != []
                    or attempt.get("validation_errors_before") != []
                    or attempt.get("outcome") != "evidence_verification_invalid"
                ):
                    errors.append(
                        f"research_dossier_evidence_verification_feedback_invalid: "
                        f"{pid}: index={index}"
                    )
            else:
                for field_name, field_value in (
                    ("source_attempt_sha256", source_attempt_sha256),
                    ("baseline_dossier_sha256", baseline_dossier_sha256),
                    ("baseline_projection_sha256", baseline_projection_sha256),
                ):
                    if not _valid_sha256(field_value):
                        errors.append(
                            f"research_dossier_invalid_research_attempt_{field_name}: "
                            f"{pid}: index={index}"
                        )
                if not attempt.get("validation_errors_before"):
                    errors.append(
                        f"research_dossier_retry_attempt_without_prior_errors: {pid}: index={index}"
                    )
                if attempt_kind in {
                    "model_output_repair",
                    "evidence_verification_dossier_repair",
                    "evidence_verification_research_continuation",
                }:
                    if not authorized_paths:
                        errors.append(
                            f"research_dossier_repair_attempt_without_authorized_paths: "
                            f"{pid}: index={index}"
                        )
                    if not _valid_sha256(repair_contract_sha256):
                        errors.append(
                            f"research_dossier_invalid_research_attempt_repair_contract_sha256: "
                            f"{pid}: index={index}"
                        )
                    if attempt.get("outcome") not in {
                        "repair_contract_valid",
                        "repair_contract_invalid",
                        "repair_scope_rejected",
                        "invocation_failed",
                        "external_wait",
                    }:
                        errors.append(
                            f"research_dossier_invalid_repair_attempt_outcome: {pid}: index={index}"
                        )
                    after = attempt.get("validation_errors_after")
                    if attempt.get("outcome") == "repair_contract_valid" and after != []:
                        errors.append(
                            f"research_dossier_valid_repair_attempt_has_errors: "
                            f"{pid}: index={index}"
                        )
                    progress = attempt.get("repair_progress")
                    if not isinstance(progress, dict):
                        errors.append(
                            f"research_dossier_repair_attempt_progress_missing: "
                            f"{pid}: index={index}"
                        )
                    else:
                        if progress.get("decision") not in {
                            "continue",
                            "accepted",
                            "restart",
                            "parked",
                        }:
                            errors.append(
                                f"research_dossier_repair_attempt_progress_decision_invalid: "
                                f"{pid}: index={index}"
                            )
                        if not _is_nonempty_string(progress.get("reason")):
                            errors.append(
                                f"research_dossier_repair_attempt_progress_reason_missing: "
                                f"{pid}: index={index}"
                            )
                    session_id = attempt.get("agent_session_id")
                    resumed_id = attempt.get("resumed_from_session_id")
                    if _canonical_uuid_string(session_id) != session_id or session_id != resumed_id:
                        errors.append(
                            f"research_dossier_repair_attempt_session_continuity_invalid: "
                            f"{pid}: index={index}"
                        )
                    if observed_session_id is not None and observed_session_id != session_id:
                        continuity_failure_recorded = (
                            isinstance(progress, dict)
                            and progress.get("decision") == "restart"
                            and progress.get("reason") == "same_session_continuity_failed"
                            and attempt.get("outcome") == "repair_scope_rejected"
                        )
                        if not continuity_failure_recorded:
                            errors.append(
                                f"research_dossier_repair_attempt_observed_session_mismatch: "
                                f"{pid}: index={index}"
                            )
                elif attempt_kind == "fresh_research_retry":
                    if authorized_paths != [] or repair_contract_sha256 is not None:
                        errors.append(
                            f"research_dossier_fresh_retry_has_repair_authorization: "
                            f"{pid}: index={index}"
                        )
                    if attempt.get("outcome") not in {
                        "output_contract_valid",
                        "output_contract_invalid",
                        "runner_contract_invalid",
                        "invocation_failed",
                        "external_wait",
                    }:
                        errors.append(
                            f"research_dossier_invalid_fresh_retry_outcome: {pid}: index={index}"
                        )
                    provenance = attempt.get("repair_progress")
                    provenance_without_hash = (
                        {
                            key: item
                            for key, item in provenance.items()
                            if key != "provenance_sha256"
                        }
                        if isinstance(provenance, dict)
                        else {}
                    )
                    if (
                        not isinstance(provenance, dict)
                        or provenance.get("schema_version") != 1
                        or provenance.get("decision") != "fresh_investigation"
                        or not _is_nonempty_string(provenance.get("reason"))
                        or provenance.get("source_attempt_sha256") != source_attempt_sha256
                        or not _valid_sha256(provenance.get("source_projection_sha256"))
                        or not _valid_sha256(provenance.get("correction_frontiers_sha256"))
                        or provenance.get("provenance_sha256")
                        != _canonical_sha256(provenance_without_hash)
                    ):
                        errors.append(
                            f"research_dossier_fresh_retry_provenance_invalid: {pid}: index={index}"
                        )
            wall_seconds = attempt.get("attempt_wall_seconds")
            if wall_seconds is not None and (
                isinstance(wall_seconds, bool)
                or not isinstance(wall_seconds, (int, float))
                or float(wall_seconds) < 0.0
            ):
                errors.append(
                    f"research_dossier_invalid_research_attempt_wall_seconds: {pid}: index={index}"
                )
        if invocation_failed and not attempt.get("validation_errors"):
            errors.append(
                f"research_dossier_invocation_failure_without_error: {pid}: index={index}"
            )
        if not isinstance(attempt.get("attempted_dossier"), dict):
            errors.append(
                f"research_dossier_invalid_research_attempt_dossier: {pid}: index={index}"
            )
        elif attempt.get("attempted_dossier_sha256") != _canonical_sha256(
            attempt.get("attempted_dossier")
        ):
            errors.append(
                f"research_dossier_research_attempt_dossier_hash_mismatch: {pid}: index={index}"
            )

        artifacts = attempt.get("attempt_artifacts")
        required_artifact_kinds = {
            "report",
            "workspace_ref",
            "target_ref",
            "normalized_events",
            "codex_subscription_auth",
        }
        observed_artifact_kinds: set[str] = set()
        if invocation_failed and artifacts == []:
            pass
        elif not isinstance(artifacts, list):
            errors.append(
                f"research_dossier_invalid_research_attempt_artifacts: {pid}: index={index}"
            )
        else:
            for artifact_index, artifact in enumerate(artifacts):
                if not isinstance(artifact, dict):
                    errors.append(
                        f"research_dossier_invalid_research_attempt_artifact: {pid}: "
                        f"index={index}:{artifact_index}"
                    )
                    continue
                unknown_artifact_fields = sorted(
                    set(artifact) - {"kind", "path", "exists", "sha256", "size_bytes"}
                )
                if unknown_artifact_fields:
                    errors.append(
                        f"research_dossier_unknown_research_attempt_artifact_fields: "
                        f"{pid}: index={index}:{artifact_index} "
                        f"fields={unknown_artifact_fields!r}"
                    )
                kind = artifact.get("kind")
                if kind not in required_artifact_kinds or kind in observed_artifact_kinds:
                    errors.append(
                        f"research_dossier_invalid_research_attempt_artifact_kind: "
                        f"{pid}: index={index}:{artifact_index} value={kind!r}"
                    )
                elif isinstance(kind, str):
                    observed_artifact_kinds.add(kind)
                if not _is_nonempty_string(artifact.get("path")):
                    errors.append(
                        f"research_dossier_invalid_research_attempt_artifact_path: "
                        f"{pid}: index={index}:{artifact_index}"
                    )
                exists = artifact.get("exists")
                if not isinstance(exists, bool):
                    errors.append(
                        f"research_dossier_invalid_research_attempt_artifact_exists: "
                        f"{pid}: index={index}:{artifact_index}"
                    )
                elif exists:
                    size_bytes = artifact.get("size_bytes")
                    if not _valid_sha256(artifact.get("sha256")):
                        errors.append(
                            f"research_dossier_invalid_research_attempt_artifact_sha256: "
                            f"{pid}: index={index}:{artifact_index}"
                        )
                    if (
                        isinstance(size_bytes, bool)
                        or not isinstance(size_bytes, int)
                        or size_bytes < 0
                    ):
                        errors.append(
                            f"research_dossier_invalid_research_attempt_artifact_size: "
                            f"{pid}: index={index}:{artifact_index}"
                        )
                elif artifact.get("sha256") is not None or artifact.get("size_bytes") is not None:
                    errors.append(
                        f"research_dossier_absent_research_attempt_artifact_has_digest: "
                        f"{pid}: index={index}:{artifact_index}"
                    )
            if observed_artifact_kinds != required_artifact_kinds:
                errors.append(
                    f"research_dossier_research_attempt_artifact_coverage_mismatch: "
                    f"{pid}: index={index}"
                )
        if attempt.get("attempt_sha256") != research_attempt_sha256(attempt):
            errors.append(f"research_dossier_research_attempt_hash_mismatch: {pid}: index={index}")
        attempt_hash = attempt.get("attempt_sha256")
        if isinstance(attempt_hash, str):
            attempts_by_hash[attempt_hash] = attempt

    uses_current_contract = any(current_contract_flags)
    if uses_current_contract and not all(current_contract_flags):
        errors.append(f"research_dossier_mixed_research_attempt_contract_versions: {pid}")
    max_attempts = 2 if not uses_current_contract else None
    if max_attempts is not None and len(value) > max_attempts:
        errors.append(f"research_dossier_too_many_research_attempts: {pid}: {len(value)}")
    if seen_numbers != set(range(1, len(value) + 1)):
        errors.append(f"research_dossier_nonsequential_research_attempts: {pid}")
    if uses_current_contract:
        kinds = [attempt.get("attempt_kind") for attempt in value if isinstance(attempt, dict)]
        if not kinds or kinds[0] != "full_research":
            errors.append(f"research_dossier_research_attempt_sequence_missing_initial: {pid}")
        if any(
            kind
            not in {
                "model_output_repair",
                "fresh_research_retry",
                "evidence_verification_feedback",
                "evidence_verification_dossier_repair",
                "evidence_verification_research_continuation",
            }
            for kind in kinds[1:]
        ):
            errors.append(f"research_dossier_invalid_targeted_repair_attempt_sequence: {pid}")
        for index, kind in enumerate(kinds[1:], start=1):
            if kind == "fresh_research_retry" and kinds[index - 1] != "model_output_repair":
                attempt = value[index] if isinstance(value[index], dict) else {}
                provenance = attempt.get("repair_progress")
                unavailable_trigger = (
                    provenance.get("trigger_status")
                    in {"same_session_continuation_unavailable", "workspace_unavailable"}
                    if isinstance(provenance, dict)
                    else False
                )
                if not unavailable_trigger:
                    errors.append(
                        f"research_dossier_fresh_retry_without_completed_repair_cycle: "
                        f"{pid}: index={index}"
                    )

        prior_hashes: set[str] = set()
        for index, attempt in enumerate(value):
            if not isinstance(attempt, dict):
                continue
            source_hash = attempt.get("source_attempt_sha256")
            if index > 0:
                source_attempt = attempts_by_hash.get(str(source_hash))
                if source_hash not in prior_hashes or not isinstance(source_attempt, dict):
                    errors.append(
                        f"research_dossier_research_attempt_source_not_prior: {pid}: index={index}"
                    )
                else:
                    # A verification-feedback record is the runner's immutable
                    # transition from an output-valid dossier into verifier errors.
                    # It points at the model attempt it verified but deliberately has
                    # no repair baseline; the following same-author repair/continuation
                    # uses the feedback record itself as its hashed baseline.
                    if attempt.get(
                        "attempt_kind"
                    ) != "evidence_verification_feedback" and attempt.get(
                        "baseline_dossier_sha256"
                    ) != source_attempt.get("attempted_dossier_sha256"):
                        errors.append(
                            f"research_dossier_research_attempt_baseline_hash_mismatch: "
                            f"{pid}: index={index}"
                        )
                    if attempt.get("validation_errors_before") != source_attempt.get(
                        "validation_errors_after"
                    ):
                        errors.append(
                            f"research_dossier_research_attempt_prior_errors_mismatch: "
                            f"{pid}: index={index}"
                        )
                    if attempt.get("attempt_kind") in {
                        "model_output_repair",
                        "evidence_verification_dossier_repair",
                        "evidence_verification_research_continuation",
                    } and (
                        attempt.get("agent_session_id") != source_attempt.get("agent_session_id")
                    ):
                        errors.append(
                            f"research_dossier_repair_attempt_source_session_mismatch: "
                            f"{pid}: index={index}"
                        )
            attempt_hash = attempt.get("attempt_sha256")
            if isinstance(attempt_hash, str):
                prior_hashes.add(attempt_hash)
    return errors


def _validate_proof_adapter_claim(value: Any, *, pid: str, index: int) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, dict):
        return [f"research_dossier_proof_adapter_invalid: {pid}: index={index}"]
    errors: list[str] = []
    for field in (
        "adapter_id",
        "hypothesis_id",
        "baseline_experiment_id",
        "challenge_experiment_id",
    ):
        if not _is_nonempty_string(value.get(field)):
            errors.append(f"research_dossier_proof_adapter_{field}_invalid: {pid}: index={index}")
    if value.get("baseline_experiment_id") == value.get("challenge_experiment_id"):
        errors.append(f"research_dossier_proof_adapter_pair_not_distinct: {pid}: index={index}")
    intervention = value.get("intervention")
    if not isinstance(intervention, dict) or any(
        not _is_nonempty_string(intervention.get(field))
        for field in ("kind", "target", "predicted_polarity")
    ):
        errors.append(f"research_dossier_proof_adapter_intervention_invalid: {pid}: index={index}")
    observations = value.get("observations")
    if observations is not None and (
        not isinstance(observations, dict)
        or any(
            not isinstance(observations.get(label), dict)
            or not _is_nonempty_string(observations[label].get("source"))
            for label in ("baseline", "challenge")
        )
    ):
        errors.append(f"research_dossier_proof_adapter_observations_invalid: {pid}: index={index}")
    positive = value.get("positive_outcome")
    if not isinstance(positive, dict):
        errors.append(
            f"research_dossier_proof_adapter_positive_outcome_invalid: {pid}: index={index}"
        )
    else:
        errors.extend(
            f"research_dossier_proof_adapter_{error}: {pid}: index={index}"
            for error in proof_predicate_contract_errors(positive.get("predicate"))
        )
        if not _is_nonempty_string(positive.get("basis_kind", "expected_behavior")):
            errors.append(
                f"research_dossier_proof_adapter_positive_basis_invalid: {pid}: index={index}"
            )
        semantic_basis = positive.get("semantic_basis")
        if not isinstance(semantic_basis, dict) or not _is_nonempty_string(
            semantic_basis.get("kind")
        ):
            errors.append(
                f"research_dossier_proof_adapter_semantic_basis_invalid: {pid}: index={index}"
            )
    return errors


def _validate_replay_setup(value: Any, *, pid: str, index: int) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, dict):
        return [f"research_dossier_replay_setup_invalid: {pid}: index={index}"]
    errors: list[str] = []
    environment = value.get("environment")
    if environment is not None:
        if (
            not isinstance(environment, dict)
            or len(environment) > 32
            or any(
                not isinstance(key, str)
                or _REPLAY_ENVIRONMENT_KEY_RE.fullmatch(key) is None
                or (item is not None and not isinstance(item, str))
                or (isinstance(item, str) and (len(item) > 4096 or "\x00" in item))
                for key, item in (environment.items() if isinstance(environment, dict) else [])
            )
        ):
            errors.append(f"research_dossier_replay_environment_invalid: {pid}: index={index}")
    paths = value.get("disposable_state_paths")
    if paths is not None:
        if (
            not isinstance(paths, list)
            or not paths
            or len(paths) > 32
            or any(
                not _is_nonempty_string(path)
                or PurePosixPath(str(path).replace("\\", "/")).is_absolute()
                or ".." in PurePosixPath(str(path).replace("\\", "/")).parts
                for path in (paths if isinstance(paths, list) else [])
            )
        ):
            errors.append(f"research_dossier_replay_state_paths_invalid: {pid}: index={index}")
    return errors


def _validate_experiments(value: Any, *, pid: str) -> list[str]:
    """Validate command/result evidence collected during research."""
    if not isinstance(value, list):
        return [f"research_dossier_invalid_experiments_type: {pid}: {type(value).__name__}"]
    errors: list[str] = []
    seen_ids: set[str] = set()
    for idx, experiment in enumerate(value):
        if not isinstance(experiment, dict):
            errors.append(
                f"research_dossier_invalid_experiment: {pid}: index={idx} "
                f"type={type(experiment).__name__}"
            )
            continue
        for field in ("experiment_id", "command", "result"):
            if not _is_nonempty_string(experiment.get(field)):
                errors.append(f"research_dossier_invalid_experiment_{field}: {pid}: index={idx}")
        experiment_id = experiment.get("experiment_id")
        if _is_nonempty_string(experiment_id):
            if experiment_id in seen_ids:
                errors.append(f"research_dossier_duplicate_experiment_id: {pid}: {experiment_id}")
            seen_ids.add(str(experiment_id))
        outcome = experiment.get("outcome")
        if not isinstance(outcome, str) or outcome not in _VALID_EXPERIMENT_OUTCOMES:
            errors.append(
                f"research_dossier_invalid_experiment_outcome: {pid}: index={idx} value={outcome!r}"
            )
        scenario_kind = experiment.get("scenario_kind")
        if not _is_nonempty_string(scenario_kind):
            errors.append(
                f"research_dossier_invalid_experiment_scenario_kind: {pid}: "
                f"index={idx} value={scenario_kind!r}"
            )
        fidelity_mapping = experiment.get("fidelity_mapping")
        if isinstance(scenario_kind, str) and scenario_kind in {
            "faithful_replay",
            "live_runtime",
        }:
            if not isinstance(fidelity_mapping, dict) or any(
                not _is_nonempty_string(fidelity_mapping.get(field))
                for field in (
                    "original_condition",
                    "retained_differences",
                    "why_mechanism_equivalent",
                )
            ):
                errors.append(f"research_dossier_fidelity_mapping_missing: {pid}: index={idx}")
        elif fidelity_mapping is not None:
            if not isinstance(fidelity_mapping, dict) or any(
                not _is_nonempty_string(fidelity_mapping.get(field))
                for field in (
                    "original_condition",
                    "retained_differences",
                    "why_mechanism_equivalent",
                )
            ):
                errors.append(f"research_dossier_fidelity_mapping_invalid: {pid}: index={idx}")
        mechanism_link = experiment.get("mechanism_link")
        if mechanism_link is not None:
            code_path = (
                mechanism_link.get("code_path") if isinstance(mechanism_link, dict) else None
            )
            if (
                not isinstance(mechanism_link, dict)
                or not isinstance(mechanism_link.get("kind"), str)
                or mechanism_link.get("kind")
                not in {"entrypoint_dataflow", "verified_symbol_trace"}
                or not _is_nonempty_string(mechanism_link.get("entrypoint"))
                or not isinstance(code_path, list)
                or not code_path
                or any(
                    not isinstance(step, dict)
                    or not _is_nonempty_string(step.get("path"))
                    or not _is_nonempty_string(step.get("symbol"))
                    or not _is_nonempty_string(step.get("observation"))
                    for step in (code_path if isinstance(code_path, list) else [])
                )
            ):
                errors.append(
                    f"research_dossier_experiment_mechanism_link_invalid: {pid}: index={idx}"
                )
        platform_requirement = experiment.get("platform_requirement", "any")
        if (
            not isinstance(platform_requirement, str)
            or _PLATFORM_REQUIREMENT_RE.fullmatch(platform_requirement) is None
        ):
            errors.append(
                f"research_dossier_invalid_experiment_platform_requirement: {pid}: "
                f"index={idx} value={platform_requirement!r}"
            )
        if scenario_kind == "live_runtime" and platform_requirement == "any":
            errors.append(f"research_dossier_live_runtime_platform_required: {pid}: index={idx}")
        static_trace = experiment.get("static_trace")
        if scenario_kind == "static_trace":
            if not isinstance(static_trace, dict):
                errors.append(f"research_dossier_static_trace_contract_missing: {pid}: index={idx}")
            else:
                if not isinstance(static_trace.get("deterministic"), bool):
                    errors.append(
                        f"research_dossier_static_trace_determinism_missing: {pid}: index={idx}"
                    )
                errors.extend(
                    _validate_string_list(
                        static_trace.get("environment_dependencies"),
                        field=f"experiments_{idx}_static_environment_dependencies",
                        pid=pid,
                    )
                )
                code_path = static_trace.get("code_path")
                if not isinstance(code_path, list) or not code_path:
                    errors.append(
                        f"research_dossier_static_trace_code_path_missing: {pid}: index={idx}"
                    )
                else:
                    for step_index, step in enumerate(code_path):
                        if (
                            not isinstance(step, dict)
                            or not _is_nonempty_string(step.get("path"))
                            or not _is_nonempty_string(step.get("symbol"))
                            or not _is_nonempty_string(step.get("observation"))
                        ):
                            errors.append(
                                f"research_dossier_static_trace_step_invalid: {pid}: "
                                f"index={idx}:{step_index}"
                            )
        elif static_trace is not None:
            errors.append(
                f"research_dossier_non_static_has_static_trace_contract: {pid}: index={idx}"
            )
        control_relationship = experiment.get("control_relationship")
        if scenario_kind == "control":
            if not isinstance(control_relationship, dict):
                errors.append(f"research_dossier_control_relationship_missing: {pid}: index={idx}")
            else:
                for field in (
                    "supports_experiment_id",
                    "controlled_variable",
                    "expected_difference",
                ):
                    if not _is_nonempty_string(control_relationship.get(field)):
                        errors.append(
                            f"research_dossier_control_relationship_{field}_invalid: "
                            f"{pid}: index={idx}"
                        )
                errors.extend(
                    _validate_string_list(
                        control_relationship.get("mechanism_symbols"),
                        field=f"experiments_{idx}_control_mechanism_symbols",
                        pid=pid,
                        require_nonempty=True,
                    )
                )
        elif control_relationship is not None:
            errors.append(
                f"research_dossier_non_control_has_control_relationship: {pid}: index={idx}"
            )
        positive_contract = experiment.get("positive_outcome_contract")
        if positive_contract is not None:
            contract_kind = (
                positive_contract.get("contract_kind")
                if isinstance(positive_contract, dict)
                else None
            )
            valid_contract = False
            if contract_kind == "origin_atom_exact_value":
                origin_bindings = experiment.get("origin_evidence_bindings")
                expected_binding = next(
                    (
                        binding
                        for binding in (
                            origin_bindings if isinstance(origin_bindings, list) else []
                        )
                        if isinstance(binding, dict)
                        and binding.get("role") == "expected_behavior"
                        and binding.get("atom_id") == positive_contract.get("atom_id")
                        and binding.get("field_path") == positive_contract.get("field_path")
                    ),
                    None,
                )
                postcondition = positive_contract.get("postcondition")
                predicate_type = (
                    postcondition.get("type") if isinstance(postcondition, dict) else None
                )
                valid_predicate = False
                if isinstance(predicate_type, str) and predicate_type in {
                    "command_stdout_equals",
                    "command_stdout_contains",
                    "command_stderr_equals",
                    "command_stderr_contains",
                    "command_combined_equals",
                    "command_combined_contains",
                }:
                    valid_predicate = _is_nonempty_string(postcondition.get("value"))
                elif predicate_type == "artifact_json_value":
                    artifact_path = postcondition.get("path")
                    pointer = postcondition.get("json_pointer")
                    valid_predicate = (
                        _is_nonempty_string(artifact_path)
                        and not str(artifact_path).startswith(("/", "\\"))
                        and ".." not in str(artifact_path).replace("\\", "/").split("/")
                        and isinstance(pointer, str)
                        and (not pointer or pointer.startswith("/"))
                        and "equals" in postcondition
                    )
                elif predicate_type == "config_state_equals":
                    valid_predicate = (
                        _is_nonempty_string(postcondition.get("mechanism_symbol"))
                        and str(postcondition.get("mechanism_symbol")).startswith("config:/")
                        and "equals" in postcondition
                        and isinstance(postcondition.get("exists", True), bool)
                    )
                valid_contract = (
                    _is_nonempty_string(positive_contract.get("atom_id"))
                    and _is_nonempty_string(positive_contract.get("field_path"))
                    and str(positive_contract.get("field_path")).startswith("$")
                    and _expected_semantic_field_path(positive_contract.get("field_path"))
                    and isinstance(expected_binding, dict)
                    and valid_predicate
                )
            elif contract_kind == "retained_harness_semantic_assertion":
                basis = positive_contract.get("semantic_basis")
                basis_kind = basis.get("kind") if isinstance(basis, dict) else None
                common_valid = (
                    isinstance(scenario_kind, str)
                    and scenario_kind in {"original_replay", "faithful_replay"}
                    and "expected_value" in positive_contract
                    and _is_nonempty_string(positive_contract.get("semantic_rationale"))
                    and len(str(positive_contract.get("semantic_rationale") or "").strip()) >= 20
                    and isinstance(positive_contract.get("semantic_relation"), str)
                    and positive_contract.get("semantic_relation")
                    in {
                        "exact_expected_value",
                        "logical_correction_of_source_failure",
                        "required_operational_property",
                        "repository_contract_requirement",
                    }
                    and isinstance(basis, dict)
                    and _is_nonempty_string(basis.get("exact_quote"))
                    and _expectation_quote(
                        basis.get("exact_quote"),
                        expected_value=positive_contract.get("expected_value"),
                    )
                )
                if basis_kind == "source_atom_quote":
                    valid_basis = (
                        _is_nonempty_string(basis.get("atom_id"))
                        and basis.get("atom_id") in experiment.get("addresses_atom_ids", [])
                        and _semantic_quote_field_path(basis.get("field_path"))
                    )
                elif basis_kind == "repository_contract_quote":
                    valid_basis = (
                        isinstance(basis.get("contract_type"), str)
                        and basis.get("contract_type")
                        in {"api_contract", "documentation", "schema"}
                        and _is_nonempty_string(basis.get("path"))
                        and not str(basis.get("path")).startswith(("/", "\\"))
                        and ".." not in str(basis.get("path")).replace("\\", "/").split("/")
                    )
                else:
                    valid_basis = False
                review_ref = positive_contract.get("adversarial_review_reference")
                valid_contract = (
                    common_valid
                    and valid_basis
                    and (review_ref is None or _is_nonempty_string(review_ref))
                )
            if not isinstance(positive_contract, dict) or not valid_contract:
                errors.append(
                    f"research_dossier_positive_outcome_contract_invalid: {pid}: index={idx}"
                )
        errors.extend(
            _validate_proof_adapter_claim(experiment.get("proof_adapter"), pid=pid, index=idx)
        )
        errors.extend(_validate_replay_setup(experiment.get("replay_setup"), pid=pid, index=idx))
        exit_code = experiment.get("exit_code")
        if isinstance(exit_code, bool) or not isinstance(exit_code, int):
            errors.append(
                f"research_dossier_invalid_experiment_exit_code: {pid}: index={idx} "
                f"type={type(exit_code).__name__}"
            )
        errors.extend(
            _validate_string_list(
                experiment.get("artifact_refs"),
                field=f"experiments_{idx}_artifact_refs",
                pid=pid,
                require_nonempty=True,
            )
        )
        errors.extend(
            _validate_string_list(
                experiment.get("addresses_atom_ids"),
                field=f"experiments_{idx}_addresses_atom_ids",
                pid=pid,
                require_nonempty=True,
            )
        )
        assertion = experiment.get("observable_assertion")
        if not isinstance(assertion, dict):
            errors.append(
                f"research_dossier_invalid_experiment_observable_assertion: {pid}: index={idx}"
            )
            continue
        source = assertion.get("source")
        operator = assertion.get("operator")
        expected = assertion.get("expected")
        if not isinstance(source, str) or source not in _VALID_ASSERTION_SOURCES:
            errors.append(
                f"research_dossier_invalid_assertion_source: {pid}: index={idx} value={source!r}"
            )
        if not isinstance(operator, str) or operator not in _VALID_ASSERTION_OPERATORS:
            errors.append(
                f"research_dossier_invalid_assertion_operator: {pid}: "
                f"index={idx} value={operator!r}"
            )
        if source == "exit_code":
            if operator != "equals" or isinstance(expected, bool) or not isinstance(expected, int):
                errors.append(f"research_dossier_invalid_exit_code_assertion: {pid}: index={idx}")
        elif not _is_nonempty_string(expected):
            errors.append(f"research_dossier_invalid_text_assertion_expected: {pid}: index={idx}")
    return errors


def _validate_hypotheses(value: Any, *, pid: str) -> list[str]:
    """Validate root-cause hypotheses and explicit causal challenges."""
    if not isinstance(value, list):
        return [
            f"research_dossier_invalid_root_cause_hypotheses_type: {pid}: {type(value).__name__}"
        ]
    errors: list[str] = []
    seen_ids: set[str] = set()
    for idx, hypothesis in enumerate(value):
        if not isinstance(hypothesis, dict):
            errors.append(
                f"research_dossier_invalid_root_cause_hypothesis: {pid}: index={idx} "
                f"type={type(hypothesis).__name__}"
            )
            continue
        hypothesis_id = hypothesis.get("hypothesis_id")
        if not _is_nonempty_string(hypothesis_id):
            errors.append(f"research_dossier_invalid_hypothesis_id: {pid}: index={idx}")
        elif hypothesis_id in seen_ids:
            errors.append(f"research_dossier_duplicate_hypothesis_id: {pid}: {hypothesis_id}")
        else:
            seen_ids.add(str(hypothesis_id))
        if not _is_nonempty_string(hypothesis.get("statement")):
            errors.append(f"research_dossier_invalid_hypothesis_statement: {pid}: index={idx}")
        errors.extend(
            _validate_string_list(
                hypothesis.get("supporting_evidence"),
                field=f"hypotheses_{idx}_supporting_evidence",
                pid=pid,
                require_nonempty=True,
            )
        )
        errors.extend(
            _validate_string_list(
                hypothesis.get("counterevidence"),
                field=f"hypotheses_{idx}_counterevidence",
                pid=pid,
                require_nonempty=False,
            )
        )
        errors.extend(
            _validate_string_list(
                hypothesis.get("mechanism_symbols"),
                field=f"hypotheses_{idx}_mechanism_symbols",
                pid=pid,
                require_nonempty=True,
            )
        )
        disposition = hypothesis.get("disposition")
        expected_disposition = "primary" if idx == 0 else None
        if not isinstance(disposition, str) or disposition not in _VALID_HYPOTHESIS_DISPOSITIONS:
            errors.append(
                f"research_dossier_invalid_hypothesis_disposition: {pid}: "
                f"index={idx} value={disposition!r}"
            )
        elif expected_disposition is not None and disposition != expected_disposition:
            errors.append(
                f"research_dossier_primary_hypothesis_disposition_invalid: {pid}: "
                f"index={idx} value={disposition!r}"
            )
        elif idx > 0 and disposition == "primary":
            errors.append(
                f"research_dossier_alternative_hypothesis_disposition_invalid: {pid}: "
                f"index={idx} value={disposition!r}"
            )
        errors.extend(
            _validate_string_list(
                hypothesis.get("disposition_evidence"),
                field=f"hypotheses_{idx}_disposition_evidence",
                pid=pid,
                require_nonempty=(
                    isinstance(disposition, str) and disposition in {"primary", "refuted"}
                ),
            )
        )
        attempts = hypothesis.get("falsification_attempts")
        if not isinstance(attempts, list):
            errors.append(
                f"research_dossier_invalid_hypotheses_{idx}_falsification_attempts_type: "
                f"{pid}: {type(attempts).__name__}"
            )
            continue
        seen_attempt_ids: set[str] = set()
        for attempt_index, attempt in enumerate(attempts):
            if not isinstance(attempt, dict):
                errors.append(
                    f"research_dossier_invalid_falsification_attempt: {pid}: "
                    f"hypothesis={hypothesis_id} index={attempt_index}"
                )
                continue
            attempt_id = attempt.get("attempt_id")
            if not _is_nonempty_string(attempt_id) or attempt_id in seen_attempt_ids:
                errors.append(
                    f"research_dossier_invalid_falsification_attempt_id: {pid}: "
                    f"hypothesis={hypothesis_id} index={attempt_index}"
                )
            else:
                seen_attempt_ids.add(str(attempt_id))
            if attempt.get("hypothesis_id") != hypothesis_id:
                errors.append(
                    f"research_dossier_falsification_attempt_hypothesis_mismatch: {pid}: "
                    f"hypothesis={hypothesis_id} index={attempt_index}"
                )
            if attempt.get("claim") != hypothesis.get("statement"):
                errors.append(
                    f"research_dossier_falsification_attempt_claim_mismatch: {pid}: "
                    f"hypothesis={hypothesis_id} index={attempt_index}"
                )
            for field in ("baseline_experiment_id", "challenge_experiment_id"):
                if not _is_nonempty_string(attempt.get(field)):
                    errors.append(
                        f"research_dossier_falsification_attempt_{field}_invalid: {pid}: "
                        f"hypothesis={hypothesis_id} index={attempt_index}"
                    )
            if attempt.get("baseline_experiment_id") == attempt.get("challenge_experiment_id"):
                errors.append(
                    f"research_dossier_falsification_attempt_reuses_baseline: {pid}: "
                    f"hypothesis={hypothesis_id} index={attempt_index}"
                )
            condition = attempt.get("disproof_condition")
            if not isinstance(condition, dict):
                errors.append(
                    f"research_dossier_falsification_attempt_disproof_condition_invalid: "
                    f"{pid}: hypothesis={hypothesis_id} index={attempt_index}"
                )
            else:
                source = condition.get("source")
                operator = condition.get("operator")
                expected = condition.get("expected")
                if not isinstance(source, str) or source not in _VALID_ASSERTION_SOURCES:
                    errors.append(
                        f"research_dossier_falsification_attempt_disproof_source_invalid: "
                        f"{pid}: hypothesis={hypothesis_id} index={attempt_index}"
                    )
                if not isinstance(operator, str) or operator not in _VALID_ASSERTION_OPERATORS:
                    errors.append(
                        f"research_dossier_falsification_attempt_disproof_operator_invalid: "
                        f"{pid}: hypothesis={hypothesis_id} index={attempt_index}"
                    )
                if source == "exit_code":
                    if (
                        operator != "equals"
                        or isinstance(expected, bool)
                        or not isinstance(expected, int)
                    ):
                        errors.append(
                            f"research_dossier_falsification_attempt_disproof_expected_invalid: "
                            f"{pid}: hypothesis={hypothesis_id} index={attempt_index}"
                        )
                elif not _is_nonempty_string(expected):
                    errors.append(
                        f"research_dossier_falsification_attempt_disproof_expected_invalid: "
                        f"{pid}: hypothesis={hypothesis_id} index={attempt_index}"
                    )
            attempt_outcome = attempt.get("outcome")
            if (
                not isinstance(attempt_outcome, str)
                or attempt_outcome not in _VALID_FALSIFICATION_ATTEMPT_OUTCOMES
            ):
                errors.append(
                    f"research_dossier_falsification_attempt_outcome_invalid: {pid}: "
                    f"hypothesis={hypothesis_id} index={attempt_index}"
                )
    return errors


def _declared_adapter_touchpoint_locators(
    item: Mapping[str, Any],
    *,
    hypothesis_id: str,
) -> set[str]:
    """Return causal locators with a declared repository touchpoint.

    This is only a model-output shape check.  It lets a non-code mechanism such as
    a configuration value survive output parsing long enough for the runner to
    verify it.  Readiness still requires the content-addressed file, causal-proof,
    and implementation-touchpoint receipts validated below.
    """

    inspected_paths = {
        str(path).replace("\\", "/").removeprefix("./")
        for path in item.get("inspected_files", [])
        if isinstance(path, str) and path.strip()
    }
    locators: set[str] = set()
    experiments = item.get("experiments")
    for experiment in experiments if isinstance(experiments, list) else []:
        if not isinstance(experiment, Mapping):
            continue
        claim = experiment.get("proof_adapter")
        if not isinstance(claim, Mapping) or claim.get("hypothesis_id") != hypothesis_id:
            continue
        intervention = claim.get("intervention")
        target = (
            str(intervention.get("target")).strip()
            if isinstance(intervention, Mapping) and _is_nonempty_string(intervention.get("target"))
            else None
        )
        touchpoints = claim.get("implementation_touchpoints")
        for touchpoint in touchpoints if isinstance(touchpoints, list) else []:
            if not isinstance(touchpoint, Mapping):
                continue
            path = (
                str(touchpoint.get("path")).replace("\\", "/").removeprefix("./")
                if _is_nonempty_string(touchpoint.get("path"))
                else None
            )
            symbols = touchpoint.get("symbols")
            if (
                target is not None
                and touchpoint.get("causal_locator") == target
                and path in inspected_paths
                and isinstance(symbols, list)
                and all(_is_nonempty_string(symbol) for symbol in symbols)
                and _is_nonempty_string(touchpoint.get("relationship"))
            ):
                locators.add(target)
    return locators


def _declared_proof_adapter_for_pair(
    item: Mapping[str, Any],
    *,
    hypothesis_id: str,
    baseline_experiment_id: str,
    challenge_experiment_id: str,
) -> Mapping[str, Any] | None:
    experiments = item.get("experiments")
    for experiment in experiments if isinstance(experiments, list) else []:
        claim = experiment.get("proof_adapter") if isinstance(experiment, Mapping) else None
        if (
            isinstance(claim, Mapping)
            and claim.get("hypothesis_id") == hypothesis_id
            and claim.get("baseline_experiment_id") == baseline_experiment_id
            and claim.get("challenge_experiment_id") == challenge_experiment_id
            and isinstance(claim.get("intervention"), Mapping)
            and isinstance(claim.get("observations"), Mapping)
            and isinstance(claim.get("positive_outcome"), Mapping)
        ):
            return claim
    return None


def _validate_research_evidence_links(item: dict[str, Any], *, pid: str) -> list[str]:
    """Validate cross-record evidence IDs and directional hypothesis evidence."""
    errors: list[str] = []
    artifact_refs = item.get("artifact_refs")
    artifact_ids = {
        str(ref.get("artifact_id"))
        for ref in (artifact_refs if isinstance(artifact_refs, list) else [])
        if isinstance(ref, dict) and _is_nonempty_string(ref.get("artifact_id"))
    }
    artifact_paths_by_id = {
        str(ref.get("artifact_id")): str(ref.get("path")).replace("\\", "/").removeprefix("./")
        for ref in (artifact_refs if isinstance(artifact_refs, list) else [])
        if isinstance(ref, dict)
        and _is_nonempty_string(ref.get("artifact_id"))
        and _is_nonempty_string(ref.get("path"))
    }
    experiments = item.get("experiments")
    experiment_outcomes = {
        str(experiment.get("experiment_id")): str(experiment.get("outcome"))
        for experiment in (experiments if isinstance(experiments, list) else [])
        if isinstance(experiment, dict) and _is_nonempty_string(experiment.get("experiment_id"))
    }
    experiment_scenarios = {
        str(experiment.get("experiment_id")): str(experiment.get("scenario_kind"))
        for experiment in (experiments if isinstance(experiments, list) else [])
        if isinstance(experiment, dict) and _is_nonempty_string(experiment.get("experiment_id"))
    }
    experiments_by_id = {
        str(experiment.get("experiment_id")): experiment
        for experiment in (experiments if isinstance(experiments, list) else [])
        if isinstance(experiment, dict) and _is_nonempty_string(experiment.get("experiment_id"))
    }
    experiment_artifact_refs = {
        str(experiment.get("experiment_id")): {
            ref
            for ref in experiment.get("artifact_refs", [])
            if isinstance(ref, str) and ref.strip()
        }
        for experiment in (experiments if isinstance(experiments, list) else [])
        if isinstance(experiment, dict) and _is_nonempty_string(experiment.get("experiment_id"))
    }
    inspected_files = {
        value.replace("\\", "/").removeprefix("./")
        for value in item.get("inspected_files", [])
        if isinstance(value, str) and value.strip()
    }
    inspected_symbols = {
        value.strip()
        for value in item.get("inspected_symbols", [])
        if isinstance(value, str) and value.strip()
    }
    assignment_raw = item.get("evidence_assignment")
    assignment = assignment_raw if isinstance(assignment_raw, dict) else {}
    expected_atom_ids_raw = assignment.get("expected_atom_ids")
    expected_atom_ids = {
        atom_id
        for atom_id in (expected_atom_ids_raw if isinstance(expected_atom_ids_raw, list) else [])
        if isinstance(atom_id, str) and atom_id.strip()
    }
    addressed_atom_ids: set[str] = set()
    for index, experiment in enumerate(experiments if isinstance(experiments, list) else []):
        if not isinstance(experiment, dict):
            continue
        for ref in experiment.get("artifact_refs", []):
            if isinstance(ref, str) and ref not in artifact_ids:
                errors.append(
                    f"research_dossier_unresolved_experiment_artifact_ref: {pid}: "
                    f"index={index} ref={ref}"
                )
        addresses_raw = experiment.get("addresses_atom_ids")
        addresses = addresses_raw if isinstance(addresses_raw, list) else []
        addressed_atom_ids.update(
            atom_id for atom_id in addresses if isinstance(atom_id, str) and atom_id.strip()
        )
        unknown_atom_ids = {
            atom_id
            for atom_id in addresses
            if isinstance(atom_id, str) and atom_id not in expected_atom_ids
        }
        if unknown_atom_ids:
            errors.append(
                f"research_dossier_experiment_addresses_unknown_atoms: {pid}: "
                f"index={index} ids={sorted(unknown_atom_ids)}"
            )

    # Only an advancing proof claims that its experiments establish the complete
    # assigned problem.  An honest insufficient/blocked investigation may retain
    # verified work on a strict subset while naming the remaining atoms as material
    # unknowns.  Requiring full coverage in that state incentivizes the model to
    # overstate what a partial experiment addressed and turns useful investigation
    # into a malformed-output retry.  Individual experiment IDs, artifact refs, and
    # atom IDs remain strictly validated below and by the runner receipt.
    if (
        item.get("research_status") == "evidence_sufficient"
        and assignment.get("status") == "complete"
        and expected_atom_ids
        and addressed_atom_ids != expected_atom_ids
    ):
        errors.append(f"research_dossier_experiment_atom_coverage_mismatch: {pid}")

    known_refs = artifact_ids | set(experiment_outcomes)
    hypotheses = item.get("root_cause_hypotheses")
    for index, hypothesis in enumerate(hypotheses if isinstance(hypotheses, list) else []):
        if not isinstance(hypothesis, dict):
            continue
        hypothesis_id = str(hypothesis.get("hypothesis_id") or index)
        supporting = hypothesis.get("supporting_evidence")
        support_refs = [
            ref
            for ref in (supporting if isinstance(supporting, list) else [])
            if isinstance(ref, str) and ref.strip()
        ]
        counter = hypothesis.get("counterevidence")
        counter_refs = [
            ref
            for ref in (counter if isinstance(counter, list) else [])
            if isinstance(ref, str) and ref.strip()
        ]
        mechanism_symbols_raw = hypothesis.get("mechanism_symbols")
        mechanism_symbols = [
            symbol
            for symbol in (mechanism_symbols_raw if isinstance(mechanism_symbols_raw, list) else [])
            if isinstance(symbol, str) and symbol.strip()
        ]
        disposition = hypothesis.get("disposition")
        disposition_raw = hypothesis.get("disposition_evidence")
        disposition_refs = [
            ref
            for ref in (disposition_raw if isinstance(disposition_raw, list) else [])
            if isinstance(ref, str) and ref.strip()
        ]
        # Code symbols must still be observed exactly.  A causal locator that is
        # not a code symbol may instead name a declared repository touchpoint;
        # the runner-owned connection is a readiness requirement, never inferred
        # from a path extension or naming convention.
        declared_touchpoint_locators = (
            _declared_adapter_touchpoint_locators(
                item,
                hypothesis_id=hypothesis_id,
            )
            if item.get("research_status") == "evidence_sufficient"
            else set()
        )
        if any(
            symbol not in inspected_symbols and symbol not in declared_touchpoint_locators
            for symbol in mechanism_symbols
        ):
            errors.append(f"research_dossier_hypothesis_symbol_uninspected: {pid}: {hypothesis_id}")
        for ref in [*support_refs, *counter_refs, *disposition_refs]:
            if isinstance(ref, str) and ref not in known_refs:
                errors.append(
                    f"research_dossier_unresolved_hypothesis_evidence_ref: {pid}: "
                    f"hypothesis={hypothesis_id} ref={ref}"
                )
        attempts_raw = hypothesis.get("falsification_attempts")
        attempts = attempts_raw if isinstance(attempts_raw, list) else []
        mechanism_symbol_set = {
            symbol for symbol in mechanism_symbols if isinstance(symbol, str) and symbol.strip()
        }
        expected_challenge_outcome = {
            "survived": "supports",
            "disproved": "refutes",
            "inconclusive": "inconclusive",
        }
        for attempt_index, attempt in enumerate(attempts):
            if not isinstance(attempt, dict):
                continue
            baseline_id = str(attempt.get("baseline_experiment_id") or "")
            challenge_id = str(attempt.get("challenge_experiment_id") or "")
            baseline = experiments_by_id.get(baseline_id)
            challenge = experiments_by_id.get(challenge_id)
            adapter_pair = _declared_proof_adapter_for_pair(
                item,
                hypothesis_id=hypothesis_id,
                baseline_experiment_id=baseline_id,
                challenge_experiment_id=challenge_id,
            )
            attempt_outcome = str(attempt.get("outcome") or "")
            label = str(attempt.get("attempt_id") or attempt_index)
            if not isinstance(baseline, dict) or not isinstance(challenge, dict):
                errors.append(
                    f"research_dossier_falsification_experiment_unresolved: {pid}: "
                    f"hypothesis={hypothesis_id} attempt={label}"
                )
                continue
            if baseline_id not in support_refs or baseline.get("outcome") != "supports":
                errors.append(
                    f"research_dossier_falsification_baseline_not_supporting: {pid}: "
                    f"hypothesis={hypothesis_id} attempt={label}"
                )
            required_challenge_outcome = expected_challenge_outcome.get(attempt_outcome)
            if challenge.get("outcome") != required_challenge_outcome:
                errors.append(
                    f"research_dossier_falsification_challenge_outcome_mismatch: {pid}: "
                    f"hypothesis={hypothesis_id} attempt={label}"
                )
            if attempt_outcome == "survived" and challenge_id not in support_refs:
                errors.append(
                    f"research_dossier_falsification_survived_challenge_not_supporting: {pid}: "
                    f"hypothesis={hypothesis_id} attempt={label}"
                )
            if attempt_outcome == "disproved" and challenge_id not in counter_refs:
                errors.append(
                    f"research_dossier_falsification_disproved_challenge_not_counterevidence: "
                    f"{pid}: hypothesis={hypothesis_id} attempt={label}"
                )
            if not _is_nonempty_string(challenge.get("scenario_kind")):
                errors.append(
                    f"research_dossier_falsification_challenge_scenario_invalid: {pid}: "
                    f"hypothesis={hypothesis_id} attempt={label}"
                )
            baseline_atoms = baseline.get("addresses_atom_ids")
            challenge_atoms = challenge.get("addresses_atom_ids")
            if (
                not isinstance(baseline_atoms, list)
                or not baseline_atoms
                or baseline_atoms != challenge_atoms
            ):
                errors.append(
                    f"research_dossier_falsification_source_atoms_mismatch: {pid}: "
                    f"hypothesis={hypothesis_id} attempt={label}"
                )
            if baseline.get("command") == challenge.get("command") and adapter_pair is None:
                errors.append(
                    f"research_dossier_falsification_challenge_reuses_baseline_command: {pid}: "
                    f"hypothesis={hypothesis_id} attempt={label}"
                )
            shared_code_refs = experiment_artifact_refs.get(
                baseline_id, set()
            ) & experiment_artifact_refs.get(challenge_id, set())
            if adapter_pair is None and not any(
                artifact_paths_by_id.get(artifact_ref) in inspected_files
                for artifact_ref in shared_code_refs
            ):
                errors.append(
                    f"research_dossier_falsification_shared_mechanism_artifact_missing: {pid}: "
                    f"hypothesis={hypothesis_id} attempt={label}"
                )
            if challenge.get("scenario_kind") == "control":
                relationship_raw = challenge.get("control_relationship")
                relationship = relationship_raw if isinstance(relationship_raw, dict) else {}
                relationship_symbols_raw = relationship.get("mechanism_symbols")
                relationship_symbols = {
                    symbol
                    for symbol in (
                        relationship_symbols_raw
                        if isinstance(relationship_symbols_raw, list)
                        else []
                    )
                    if isinstance(symbol, str) and symbol.strip()
                }
                if (
                    relationship.get("supports_experiment_id") != baseline_id
                    or not relationship_symbols
                    or not relationship_symbols.issubset(mechanism_symbol_set)
                ):
                    errors.append(
                        f"research_dossier_falsification_control_relationship_unbound: {pid}: "
                        f"hypothesis={hypothesis_id} attempt={label}"
                    )
            disproof_condition = attempt.get("disproof_condition")
            observed_assertion = challenge.get("observable_assertion")
            if (
                not isinstance(disproof_condition, dict)
                or not isinstance(observed_assertion, dict)
                or not _falsification_assertion_relation(
                    disproof_condition,
                    observed_assertion,
                    outcome=attempt_outcome,
                )
            ):
                errors.append(
                    f"research_dossier_falsification_result_mismatch: {pid}: "
                    f"hypothesis={hypothesis_id} attempt={label}"
                )
        has_refuting_experiment = any(
            experiment_outcomes.get(ref) == "refutes"
            for ref in disposition_refs
            if isinstance(ref, str)
        )
        has_disproved_falsification = any(
            isinstance(attempt, dict)
            and attempt.get("outcome") == "disproved"
            and attempt.get("challenge_experiment_id") in disposition_refs
            for attempt in attempts
        )
        if (
            disposition == "refuted"
            and not has_refuting_experiment
            and not has_disproved_falsification
        ):
            errors.append(
                f"research_dossier_refuted_hypothesis_missing_falsification: {pid}: {hypothesis_id}"
            )
        if disposition == "refuted" and not any(
            isinstance(attempt, dict) and attempt.get("outcome") == "disproved"
            for attempt in attempts
        ):
            errors.append(
                f"research_dossier_refuted_hypothesis_missing_disproved_attempt: "
                f"{pid}: {hypothesis_id}"
            )
        advancing_scenarios = {
            "original_replay",
            "faithful_replay",
            "static_trace",
            "live_runtime",
        }
        supporting_experiment_ids = [
            ref
            for ref in support_refs
            if experiment_outcomes.get(ref) == "supports"
            and (
                experiment_scenarios.get(ref) in advancing_scenarios
                or any(
                    _declared_proof_adapter_for_pair(
                        item,
                        hypothesis_id=hypothesis_id,
                        baseline_experiment_id=str(attempt.get("baseline_experiment_id") or ""),
                        challenge_experiment_id=str(attempt.get("challenge_experiment_id") or ""),
                    )
                    is not None
                    and ref
                    in {
                        attempt.get("baseline_experiment_id"),
                        attempt.get("challenge_experiment_id"),
                    }
                    for attempt in attempts
                    if isinstance(attempt, dict)
                )
            )
        ]
        # The first hypothesis is the implementation-driving mechanism.  Later
        # hypotheses are explicit alternatives and may be supported/refuted by
        # retained artifacts rather than an independent full reproduction.
        if (
            item.get("research_status") == "evidence_sufficient"
            and index == 0
            and not supporting_experiment_ids
        ):
            errors.append(
                f"research_dossier_primary_hypothesis_missing_supporting_experiment: "
                f"{pid}: {hypothesis_id}"
            )
        adapter_touchpoint_linked = bool(
            set(mechanism_symbols)
            & _declared_adapter_touchpoint_locators(item, hypothesis_id=hypothesis_id)
        )
        if (
            supporting_experiment_ids
            and not adapter_touchpoint_linked
            and not any(
                artifact_paths_by_id.get(artifact_ref) in inspected_files
                for experiment_id in supporting_experiment_ids
                for artifact_ref in experiment_artifact_refs.get(experiment_id, set())
            )
        ):
            errors.append(
                f"research_dossier_hypothesis_support_not_linked_to_inspected_code: "
                f"{pid}: {hypothesis_id}"
            )
        for counter_ref in counter_refs:
            control_experiment = next(
                (
                    experiment
                    for experiment in (experiments if isinstance(experiments, list) else [])
                    if isinstance(experiment, dict)
                    and experiment.get("experiment_id") == counter_ref
                ),
                None,
            )
            if (
                not isinstance(control_experiment, dict)
                or control_experiment.get("scenario_kind") != "control"
                or control_experiment.get("outcome") != "refutes"
            ):
                continue
            relationship_raw = control_experiment.get("control_relationship")
            relationship = relationship_raw if isinstance(relationship_raw, dict) else {}
            relationship_symbols_raw = relationship.get("mechanism_symbols")
            relationship_symbols = {
                symbol
                for symbol in (
                    relationship_symbols_raw if isinstance(relationship_symbols_raw, list) else []
                )
                if isinstance(symbol, str) and symbol.strip()
            }
            support_id = str(relationship.get("supports_experiment_id") or "")
            support_experiment = next(
                (
                    experiment
                    for experiment in (experiments if isinstance(experiments, list) else [])
                    if isinstance(experiment, dict)
                    and experiment.get("experiment_id") == support_id
                ),
                None,
            )
            shared_code_refs = experiment_artifact_refs.get(
                str(counter_ref), set()
            ) & experiment_artifact_refs.get(support_id, set())
            if (
                not isinstance(support_experiment, dict)
                or support_id not in supporting_experiment_ids
                or not relationship_symbols
                or not relationship_symbols.issubset(mechanism_symbol_set)
                or control_experiment.get("addresses_atom_ids")
                != support_experiment.get("addresses_atom_ids")
                or control_experiment.get("command") == support_experiment.get("command")
                or not any(
                    artifact_paths_by_id.get(artifact_ref) in inspected_files
                    for artifact_ref in shared_code_refs
                )
            ):
                errors.append(
                    f"research_dossier_hypothesis_control_unbound: {pid}: "
                    f"{hypothesis_id}:{counter_ref}"
                )
        verification = item.get("evidence_verification")
        if (
            item.get("research_status") == "evidence_sufficient"
            and isinstance(verification, dict)
            and verification.get("status") == "verified"
            and index == 0
        ):
            declared_attempt_ids = {
                str(attempt.get("attempt_id"))
                for attempt in attempts
                if isinstance(attempt, dict) and _is_nonempty_string(attempt.get("attempt_id"))
            }
            verified_attempt_ids = {
                str(attempt.get("attempt_id"))
                for attempt in verified_hypothesis_falsification_attempts(
                    item,
                    hypothesis_id=hypothesis_id,
                )
            }
            if declared_attempt_ids != verified_attempt_ids:
                errors.append(
                    f"research_dossier_falsification_attempt_unverified: {pid}: "
                    f"hypothesis={hypothesis_id} "
                    f"missing={sorted(declared_attempt_ids - verified_attempt_ids)}"
                )
    return errors


def _valid_sha256(value: Any) -> bool:
    return isinstance(value, str) and bool(re.fullmatch(r"[0-9a-f]{64}", value))


def _falsification_assertion_relation(
    disproof_condition: dict[str, Any],
    observed_assertion: dict[str, Any],
    *,
    outcome: str,
) -> bool:
    """Bind a challenge result to the condition that would disprove its claim."""

    if outcome == "disproved":
        return observed_assertion == disproof_condition
    if outcome == "inconclusive":
        return True
    if outcome != "survived":
        return False
    source = disproof_condition.get("source")
    operator = disproof_condition.get("operator")
    expected = disproof_condition.get("expected")
    if observed_assertion.get("source") != source:
        return False
    observed_operator = observed_assertion.get("operator")
    observed_expected = observed_assertion.get("expected")
    if operator == "contains":
        return observed_operator == "not_contains" and observed_expected == expected
    if operator == "not_contains":
        return observed_operator == "contains" and observed_expected == expected
    if operator != "equals" or observed_operator != "equals":
        return False
    if source == "exit_code":
        return (
            isinstance(expected, int)
            and not isinstance(expected, bool)
            and isinstance(observed_expected, int)
            and not isinstance(observed_expected, bool)
            and observed_expected != expected
        )
    return (
        isinstance(expected, str)
        and isinstance(observed_expected, str)
        and observed_expected != expected
    )


def verified_hypothesis_falsification_attempts(
    item: dict[str, Any] | None,
    *,
    hypothesis_id: str,
) -> list[dict[str, Any]]:
    """Project runner-replayed, causally bound attempts to disprove one hypothesis.

    A model-authored ``refutes`` label is not enough.  The challenge must be distinct
    from its supporting baseline, address the same source evidence, exercise typed
    mechanism evidence for the selected hypothesis, and have an exact runner receipt.
    """

    if not isinstance(item, dict):
        return []
    hypotheses_raw = item.get("root_cause_hypotheses")
    hypothesis = next(
        (
            value
            for value in (hypotheses_raw if isinstance(hypotheses_raw, list) else [])
            if isinstance(value, dict) and value.get("hypothesis_id") == hypothesis_id
        ),
        None,
    )
    verification = item.get("evidence_verification")
    if not isinstance(hypothesis, dict) or not isinstance(verification, dict):
        return []
    if verification.get("status") != "verified":
        return []
    interventions_raw = verification.get("falsification_interventions")
    interventions = {
        (str(value.get("hypothesis_id")), str(value.get("attempt_id"))): value
        for value in (interventions_raw if isinstance(interventions_raw, list) else [])
        if isinstance(value, dict)
        and _is_nonempty_string(value.get("hypothesis_id"))
        and _is_nonempty_string(value.get("attempt_id"))
        and value.get("intervention_receipt_id")
        == "falsification_intervention:"
        + _canonical_sha256(
            {
                field: item_value
                for field, item_value in value.items()
                if field != "intervention_receipt_id"
            }
        )
    }
    proof_by_pair = {
        (
            str(proof.get("hypothesis_id")),
            str(proof.get("intervention", {}).get("baseline_experiment_id")),
            str(proof.get("intervention", {}).get("challenge_experiment_id")),
        ): proof
        for proof in (
            verification.get("proof_adapter_receipts")
            if isinstance(verification.get("proof_adapter_receipts"), list)
            else []
        )
        if isinstance(proof, Mapping)
        and isinstance(proof.get("intervention"), Mapping)
        and not validate_causal_proof_receipt(proof)
    }
    experiments_raw = item.get("experiments")
    experiments = {
        str(experiment.get("experiment_id")): experiment
        for experiment in (experiments_raw if isinstance(experiments_raw, list) else [])
        if isinstance(experiment, dict) and _is_nonempty_string(experiment.get("experiment_id"))
    }
    receipt_experiments_raw = verification.get("experiments")
    receipt_experiments = {
        str(experiment.get("experiment_id")): experiment
        for experiment in (
            receipt_experiments_raw if isinstance(receipt_experiments_raw, list) else []
        )
        if isinstance(experiment, dict) and _is_nonempty_string(experiment.get("experiment_id"))
    }
    expected_symbols = hypothesis.get("mechanism_symbols")
    mechanism_evidence_raw = verification.get("mechanism_evidence")
    mechanism_evidence_ids: dict[str, set[str]] = {}
    for evidence in mechanism_evidence_raw if isinstance(mechanism_evidence_raw, list) else []:
        if not isinstance(evidence, dict):
            continue
        evidence_id = str(evidence.get("mechanism_evidence_id") or "")
        expected_id = "mechanism_evidence:" + _canonical_sha256(
            {field: value for field, value in evidence.items() if field != "mechanism_evidence_id"}
        )
        experiment_ids = evidence.get("experiment_ids")
        if (
            evidence_id == expected_id
            and evidence.get("hypothesis_id") == hypothesis_id
            and evidence.get("mechanism_symbols") == expected_symbols
            and isinstance(experiment_ids, list)
        ):
            for experiment_id in experiment_ids:
                if isinstance(experiment_id, str):
                    mechanism_evidence_ids.setdefault(experiment_id, set()).add(evidence_id)

    support_refs = {
        ref for ref in hypothesis.get("supporting_evidence", []) if isinstance(ref, str)
    }
    counter_refs = {ref for ref in hypothesis.get("counterevidence", []) if isinstance(ref, str)}
    attempts_raw = hypothesis.get("falsification_attempts")
    projected: list[dict[str, Any]] = []
    expected_experiment_outcomes = {
        "survived": "supports",
        "disproved": "refutes",
        "inconclusive": "inconclusive",
    }
    for attempt in attempts_raw if isinstance(attempts_raw, list) else []:
        if not isinstance(attempt, dict):
            continue
        attempt_id = attempt.get("attempt_id")
        baseline_id = attempt.get("baseline_experiment_id")
        challenge_id = attempt.get("challenge_experiment_id")
        outcome = attempt.get("outcome")
        disproof_condition = attempt.get("disproof_condition")
        baseline = experiments.get(str(baseline_id))
        challenge = experiments.get(str(challenge_id))
        baseline_receipt = receipt_experiments.get(str(baseline_id))
        challenge_receipt = receipt_experiments.get(str(challenge_id))
        intervention_receipt = interventions.get((hypothesis_id, str(attempt_id)))
        proof_receipt = proof_by_pair.get((hypothesis_id, str(baseline_id), str(challenge_id)))
        baseline_artifacts_raw = (
            baseline.get("artifact_refs") if isinstance(baseline, dict) else None
        )
        challenge_artifacts_raw = (
            challenge.get("artifact_refs") if isinstance(challenge, dict) else None
        )
        baseline_artifacts = {
            value
            for value in (
                baseline_artifacts_raw if isinstance(baseline_artifacts_raw, list) else []
            )
            if isinstance(value, str)
        }
        challenge_artifacts = {
            value
            for value in (
                challenge_artifacts_raw if isinstance(challenge_artifacts_raw, list) else []
            )
            if isinstance(value, str)
        }
        if (
            not _is_nonempty_string(attempt_id)
            or attempt.get("hypothesis_id") != hypothesis_id
            or attempt.get("claim") != hypothesis.get("statement")
            or not isinstance(disproof_condition, dict)
            or not isinstance(outcome, str)
            or outcome not in _VALID_FALSIFICATION_ATTEMPT_OUTCOMES
            or not isinstance(baseline, dict)
            or not isinstance(challenge, dict)
            or not isinstance(baseline_receipt, dict)
            or not isinstance(challenge_receipt, dict)
            or baseline_id == challenge_id
            or baseline_id not in support_refs
            or baseline.get("outcome") != "supports"
            or challenge.get("outcome") != expected_experiment_outcomes.get(str(outcome))
            or (
                not isinstance(proof_receipt, Mapping)
                and (
                    not isinstance(challenge.get("scenario_kind"), str)
                    or challenge.get("scenario_kind")
                    not in {
                        "original_replay",
                        "faithful_replay",
                        "control",
                        "static_trace",
                        "live_runtime",
                    }
                )
            )
            or (
                not isinstance(proof_receipt, Mapping)
                and baseline.get("command") == challenge.get("command")
            )
            or not baseline.get("addresses_atom_ids")
            or baseline.get("addresses_atom_ids") != challenge.get("addresses_atom_ids")
            or (
                not isinstance(proof_receipt, Mapping)
                and not baseline_artifacts.intersection(challenge_artifacts)
            )
            or str(challenge_id) not in mechanism_evidence_ids
            or str(baseline_id) not in mechanism_evidence_ids
            or challenge_receipt.get("assertion_passed") is not True
            or baseline_receipt.get("assertion_passed") is not True
            or (
                outcome in {"survived", "disproved"}
                and not isinstance(intervention_receipt, dict)
                and not isinstance(proof_receipt, Mapping)
            )
        ):
            continue
        if outcome == "survived" and challenge_id not in support_refs:
            continue
        if outcome == "disproved" and challenge_id not in counter_refs:
            continue
        if challenge.get("scenario_kind") == "control":
            relationship = challenge.get("control_relationship")
            if (
                not isinstance(relationship, dict)
                or relationship.get("supports_experiment_id") != baseline_id
                or relationship.get("mechanism_symbols") != expected_symbols
            ):
                continue
        observed_assertion = challenge.get("observable_assertion")
        if not isinstance(observed_assertion, dict) or not _falsification_assertion_relation(
            disproof_condition,
            observed_assertion,
            outcome=str(outcome),
        ):
            continue
        receipt_matches = all(
            (challenge_receipt.get(receipt_field) == challenge.get(declared_field))
            for receipt_field, declared_field in (
                ("command", "command"),
                ("declared_result", "result"),
                ("exit_code", "exit_code"),
                ("outcome", "outcome"),
                ("scenario_kind", "scenario_kind"),
                ("observable_assertion", "observable_assertion"),
            )
        )
        if (
            not receipt_matches
            or not _valid_sha256(challenge_receipt.get("stdout_sha256"))
            or not _valid_sha256(challenge_receipt.get("stderr_sha256"))
        ):
            continue
        projected.append(
            {
                "attempt_id": str(attempt_id),
                "hypothesis_id": hypothesis_id,
                "claim": hypothesis.get("statement"),
                "baseline_experiment_id": str(baseline_id),
                "challenge_experiment_id": str(challenge_id),
                "disproof_condition": disproof_condition,
                "outcome": str(outcome),
                "scenario_kind": challenge.get("scenario_kind"),
                "command": challenge.get("command"),
                "declared_result": challenge.get("result"),
                "observable_assertion": observed_assertion,
                "exit_code": challenge_receipt.get("exit_code"),
                "stdout_sha256": challenge_receipt.get("stdout_sha256"),
                "stderr_sha256": challenge_receipt.get("stderr_sha256"),
                "mechanism_evidence_ids": sorted(mechanism_evidence_ids[str(challenge_id)]),
                "mechanism_symbols": (
                    list(expected_symbols) if isinstance(expected_symbols, list) else []
                ),
                "intervention_receipt_id": (
                    intervention_receipt.get("intervention_receipt_id")
                    if isinstance(intervention_receipt, dict)
                    else proof_receipt.get("intervention_id")
                    if isinstance(proof_receipt, Mapping)
                    else None
                ),
                "proof_receipt_id": (
                    proof_receipt.get("proof_receipt_id")
                    if isinstance(proof_receipt, Mapping)
                    else None
                ),
            }
        )
    return sorted(projected, key=lambda value: str(value.get("attempt_id")))


def verified_deterministic_mechanism_closures(
    item: dict[str, Any] | None,
    *,
    hypothesis_id: str,
) -> list[dict[str, Any]]:
    """Project runner-owned closure for mechanisms with no honest counterfactual."""

    if not isinstance(item, dict):
        return []
    hypotheses_raw = item.get("root_cause_hypotheses")
    hypotheses = (
        [value for value in hypotheses_raw if isinstance(value, dict)]
        if isinstance(hypotheses_raw, list)
        else []
    )
    hypothesis = next(
        (value for value in hypotheses if value.get("hypothesis_id") == hypothesis_id),
        None,
    )
    verification = item.get("evidence_verification")
    if (
        not isinstance(hypothesis, dict)
        or not isinstance(verification, dict)
        or verification.get("status") != "verified"
        or hypothesis.get("falsification_attempts")
    ):
        return []
    if any(value.get("disposition") != "refuted" for value in hypotheses[1:]):
        return []
    if any(
        isinstance(unknown, dict)
        and "root_cause"
        in (unknown.get("affects") if isinstance(unknown.get("affects"), list) else [])
        for unknown in (
            item.get("material_unknowns") if isinstance(item.get("material_unknowns"), list) else []
        )
    ):
        return []
    experiments_raw = item.get("experiments")
    experiments = {
        str(value.get("experiment_id")): value
        for value in (experiments_raw if isinstance(experiments_raw, list) else [])
        if isinstance(value, dict) and _is_nonempty_string(value.get("experiment_id"))
    }
    replay_raw = verification.get("experiments")
    replays = {
        str(value.get("experiment_id")): value
        for value in (replay_raw if isinstance(replay_raw, list) else [])
        if isinstance(value, dict) and _is_nonempty_string(value.get("experiment_id"))
    }
    symbol_paths = {
        str(value.get("symbol")): str(value.get("path"))
        for value in (
            verification.get("inspected_symbols")
            if isinstance(verification.get("inspected_symbols"), list)
            else []
        )
        if isinstance(value, dict)
        and _is_nonempty_string(value.get("symbol"))
        and _is_nonempty_string(value.get("path"))
    }
    expected_symbols = hypothesis.get("mechanism_symbols")
    expected_alternatives = [str(value.get("hypothesis_id")) for value in hypotheses[1:]]
    normalized_symbols = (
        [value.strip() for value in expected_symbols if isinstance(value, str) and value.strip()]
        if isinstance(expected_symbols, list)
        else []
    )
    mechanism_raw = verification.get("mechanism_evidence")
    mechanism_evidence = [
        value
        for value in (mechanism_raw if isinstance(mechanism_raw, list) else [])
        if isinstance(value, dict)
        and value.get("hypothesis_id") == hypothesis_id
        and value.get("adversarial_effect") == "supports_selection"
    ]
    connected, covered_symbols, support_connectivity, disconnected = _rooted_support_connectivity(
        mechanism_evidence,
        hypothesis_symbols=normalized_symbols,
    )
    if (
        disconnected
        or not connected
        or covered_symbols != set(normalized_symbols)
        or any(symbol not in symbol_paths for symbol in normalized_symbols)
    ):
        return []
    support_ids = sorted(
        {
            experiment_id
            for evidence in connected
            for experiment_id in evidence.get("experiment_ids", [])
            if isinstance(experiment_id, str)
        }
    )
    if not set(support_ids).issubset(
        {value for value in hypothesis.get("supporting_evidence", []) if isinstance(value, str)}
    ):
        return []
    origin_atom_ids: set[str] = set()
    observed_results: list[dict[str, Any]] = []
    for support_id in support_ids:
        experiment = experiments.get(support_id)
        replay = replays.get(support_id)
        if (
            not isinstance(experiment, dict)
            or not isinstance(replay, dict)
            or experiment.get("outcome") != "supports"
            or replay.get("assertion_passed") is not True
        ):
            return []
        origin_atom_ids.update(
            value for value in experiment.get("addresses_atom_ids", []) if isinstance(value, str)
        )
        observed_results.append(
            {
                "experiment_id": support_id,
                "scenario_kind": experiment.get("scenario_kind"),
                "exit_code": replay.get("exit_code"),
                "stdout_sha256": replay.get("stdout_sha256"),
                "stderr_sha256": replay.get("stderr_sha256"),
                "assertion": experiment.get("observable_assertion"),
            }
        )
    expected = {
        "verification_method": "runner_deterministic_mechanism_closure_v2",
        "hypothesis_id": hypothesis_id,
        "support_experiment_ids": support_ids,
        "mechanism_evidence_ids": sorted(
            str(value["mechanism_evidence_id"]) for value in connected
        ),
        "causal_root_evidence_ids": sorted(
            str(value["mechanism_evidence_id"])
            for value in connected
            if value.get("causal_root_bindings")
        ),
        "mechanism_symbols": normalized_symbols,
        "code_path": [
            {"symbol": symbol, "path": symbol_paths[symbol]} for symbol in normalized_symbols
        ],
        "closure_basis": "rooted_connected_support_component",
        "support_connectivity": support_connectivity,
        "alternatives_disposed": expected_alternatives,
        "origin_atom_ids": sorted(origin_atom_ids),
        "observed_results": observed_results,
    }
    expected["closure_receipt_id"] = "deterministic_mechanism_closure:" + _canonical_sha256(
        expected
    )
    raw_closures = verification.get("deterministic_mechanism_closures")
    observed = raw_closures if isinstance(raw_closures, list) else []
    return [expected] if observed == [expected] else []


def replay_invocation_references_model_overlay(
    command: Any,
    executed_argv: Any,
) -> bool:
    """Return whether a replay can execute model-authored overlay content.

    Research workers may write isolated probes below ``.usertest_research``.
    Those probes are useful for exploration and controls, but their own output
    cannot independently prove that a production mechanism executed: a probe
    could simply print a plausible traceback. Treat malformed invocations as
    overlay-dependent too so this trust boundary fails closed.
    """

    if not isinstance(command, str) or not command.strip():
        return True
    if (
        not isinstance(executed_argv, list)
        or not executed_argv
        or any(not isinstance(argument, str) or not argument for argument in executed_argv)
    ):
        return True
    return any(
        ".usertest_research" in value.replace("\\", "/").casefold()
        for value in [command, *executed_argv]
    )


def _validate_evidence_assignment(item: dict[str, Any], *, pid: str) -> list[str]:
    assignment = item.get("evidence_assignment")
    if not isinstance(assignment, dict):
        return [f"research_dossier_invalid_evidence_assignment: {pid}"]
    errors: list[str] = []
    for field in (
        "assignment_sha256",
        "status",
        "errors",
        "case_id",
        "problem_id",
        "expected_atom_ids",
        "atom_receipts",
    ):
        if field not in assignment:
            errors.append(f"research_evidence_assignment_missing_field: {pid}: {field}")
    if assignment.get("case_id") != item.get("case_id"):
        errors.append(f"research_evidence_assignment_case_mismatch: {pid}")
    if assignment.get("problem_id") != item.get("problem_id"):
        errors.append(f"research_evidence_assignment_problem_mismatch: {pid}")
    if assignment.get("assignment_sha256") != evidence_assignment_sha256(assignment):
        errors.append(f"research_evidence_assignment_hash_mismatch: {pid}")
    assignment_status = assignment.get("status")
    if assignment_status not in {"complete", "incomplete"}:
        errors.append(f"research_evidence_assignment_status_invalid: {pid}")
    assignment_errors = assignment.get("errors")
    errors.extend(
        _validate_string_list(
            assignment_errors,
            field="evidence_assignment_errors",
            pid=pid,
        )
    )
    if (
        assignment_status == "incomplete"
        and isinstance(assignment_errors, list)
        and not assignment_errors
    ):
        errors.append(f"research_evidence_assignment_incomplete_without_error: {pid}")
    if (
        assignment_status == "complete"
        and isinstance(assignment_errors, list)
        and assignment_errors
    ):
        errors.append(f"research_evidence_assignment_complete_with_errors: {pid}")
    expected_raw = assignment.get("expected_atom_ids")
    expected = expected_raw if isinstance(expected_raw, list) else []
    errors.extend(
        _validate_string_list(
            expected_raw,
            field="evidence_assignment_expected_atom_ids",
            pid=pid,
            require_nonempty=assignment_status == "complete",
        )
    )
    receipts_raw = assignment.get("atom_receipts")
    receipts = receipts_raw if isinstance(receipts_raw, list) else []
    if not isinstance(receipts_raw, list) or (assignment_status == "complete" and not receipts):
        errors.append(f"research_evidence_assignment_atom_receipts_missing: {pid}")
    receipt_ids: list[str] = []
    for index, receipt in enumerate(receipts):
        if not isinstance(receipt, dict):
            errors.append(f"research_evidence_assignment_atom_receipt_invalid: {pid}: {index}")
            continue
        atom_id = receipt.get("atom_id")
        if not _is_nonempty_string(atom_id):
            errors.append(f"research_evidence_assignment_atom_id_invalid: {pid}: {index}")
        else:
            receipt_ids.append(str(atom_id))
        if not _valid_sha256(receipt.get("atom_sha256")):
            errors.append(f"research_evidence_assignment_atom_hash_invalid: {pid}: {index}")
        snapshot = receipt.get("atom_snapshot")
        if not isinstance(snapshot, dict) or receipt.get("atom_sha256") != _canonical_sha256(
            snapshot
        ):
            errors.append(f"research_evidence_assignment_atom_snapshot_invalid: {pid}: {index}")
        elif snapshot.get("atom_id") != atom_id:
            errors.append(f"research_evidence_assignment_atom_snapshot_id_mismatch: {pid}: {index}")
        artifact_receipts_raw = receipt.get("artifact_receipts")
        artifact_receipts = artifact_receipts_raw if isinstance(artifact_receipts_raw, list) else []
        origin_evidence_mode = receipt.get("origin_evidence_mode")
        if origin_evidence_mode is None and artifact_receipts:
            # Compatibility for retained v1 receipts written before the mode was
            # explicit. Their artifact list proves the stronger legacy shape.
            origin_evidence_mode = "snapshot_and_artifacts"
        if origin_evidence_mode not in {"signed_snapshot", "snapshot_and_artifacts"}:
            errors.append(f"research_evidence_assignment_origin_mode_invalid: {pid}: {atom_id}")
        if (
            assignment_status == "complete"
            and origin_evidence_mode == "snapshot_and_artifacts"
            and not artifact_receipts
        ):
            errors.append(
                f"research_evidence_assignment_origin_artifacts_missing: {pid}: {atom_id}"
            )
        if origin_evidence_mode == "signed_snapshot" and artifact_receipts:
            errors.append(
                f"research_evidence_assignment_signed_snapshot_has_artifacts: {pid}: {atom_id}"
            )
        for artifact_index, artifact in enumerate(artifact_receipts):
            if (
                not isinstance(artifact, dict)
                or not _is_nonempty_string(artifact.get("path"))
                or not _valid_sha256(artifact.get("sha256"))
                or isinstance(artifact.get("size_bytes"), bool)
                or not isinstance(artifact.get("size_bytes"), int)
            ):
                errors.append(
                    f"research_evidence_assignment_artifact_invalid: {pid}: "
                    f"{index}:{artifact_index}"
                )
    if assignment_status == "complete" and sorted(receipt_ids) != sorted(
        str(atom_id) for atom_id in expected
    ):
        errors.append(f"research_evidence_assignment_atom_coverage_mismatch: {pid}")
    return errors


def _control_call_arguments(
    selection: dict[str, Any],
    symbol: str,
) -> list[dict[str, Any]] | None:
    touches = selection.get("mechanism_touches")
    if not isinstance(touches, list):
        return None
    matching_calls: list[dict[str, Any]] = []
    for touch in touches:
        if not isinstance(touch, dict) or touch.get("symbol") != symbol:
            continue
        calls = touch.get("calls")
        if not isinstance(calls, list):
            return None
        matching_calls.extend(call for call in calls if isinstance(call, dict))
    if len(matching_calls) != 1 or matching_calls[0].get("arguments_complete") is not True:
        return None
    arguments = matching_calls[0].get("arguments")
    if not isinstance(arguments, list):
        return None
    return [argument for argument in arguments if isinstance(argument, dict)]


def _expected_structural_control_difference(
    support_selection: dict[str, Any],
    control_selection: dict[str, Any],
    mechanism_symbols: list[str],
) -> dict[str, Any] | None:
    differences: list[dict[str, Any]] = []
    for symbol in mechanism_symbols:
        support_arguments_raw = _control_call_arguments(support_selection, symbol)
        control_arguments_raw = _control_call_arguments(control_selection, symbol)
        if support_arguments_raw is None or control_arguments_raw is None:
            return None
        support_arguments = {
            str(argument.get("slot")): argument for argument in support_arguments_raw
        }
        control_arguments = {
            str(argument.get("slot")): argument for argument in control_arguments_raw
        }
        if len(support_arguments) != len(support_arguments_raw) or len(control_arguments) != len(
            control_arguments_raw
        ):
            return None
        for slot in sorted(set(support_arguments) | set(control_arguments)):
            support_argument = support_arguments.get(slot)
            control_argument = control_arguments.get(slot)
            if (
                support_argument is not None
                and control_argument is not None
                and support_argument.get("ast_sha256") == control_argument.get("ast_sha256")
            ):
                continue
            differences.append(
                {
                    "mechanism_symbol": symbol,
                    "slot": slot,
                    "difference_kind": (
                        "added_in_control"
                        if support_argument is None
                        else "removed_in_control"
                        if control_argument is None
                        else "changed"
                    ),
                    "support_argument": support_argument,
                    "control_argument": control_argument,
                }
            )
    if len(differences) != 1:
        return None
    return {
        "verification_method": "python_ast_explicit_argument_delta_v1",
        "difference_count": 1,
        "difference": differences[0],
    }


def _normalized_mechanism_subset(
    value: Any,
    *,
    hypothesis_symbols: Any,
) -> list[str] | None:
    """Return one non-empty declared subset in hypothesis-path order."""

    if not isinstance(hypothesis_symbols, list) or not isinstance(value, list):
        return None
    normalized_hypothesis = [
        symbol.strip()
        for symbol in hypothesis_symbols
        if isinstance(symbol, str) and symbol.strip()
    ]
    declared = [symbol.strip() for symbol in value if isinstance(symbol, str) and symbol.strip()]
    if (
        not normalized_hypothesis
        or len(normalized_hypothesis) != len(hypothesis_symbols)
        or len(set(normalized_hypothesis)) != len(normalized_hypothesis)
        or not declared
        or len(declared) != len(value)
        or len(set(declared)) != len(declared)
        or not set(declared).issubset(set(normalized_hypothesis))
    ):
        return None
    return [symbol for symbol in normalized_hypothesis if symbol in set(declared)]


def _receipt_text(value: Any) -> str | None:
    """Normalize one runner-receipt string exactly as the evidence producer does."""

    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def _runner_observed_root_mechanism_symbol(
    support: dict[str, Any],
    mechanism_link: dict[str, Any] | None,
) -> str | None:
    """Re-derive the one runner-observed symbol allowed to seed connectivity."""

    if not isinstance(mechanism_link, dict):
        return None
    verification_method = _receipt_text(mechanism_link.get("verification_method"))
    if verification_method is None or not verification_method.startswith("runner_"):
        return None
    symbols_raw = support.get("mechanism_symbols")
    declared = {
        value.strip()
        for value in (symbols_raw if isinstance(symbols_raw, list) else [])
        if isinstance(value, str) and value.strip()
    }
    entrypoint = _receipt_text(mechanism_link.get("entrypoint"))
    if entrypoint in declared:
        return entrypoint
    if verification_method == "runner_python_call_chain_v1":
        code_path_raw = mechanism_link.get("code_path")
        code_path = code_path_raw if isinstance(code_path_raw, list) else []
        path_symbols = [
            str(item.get("symbol"))
            for item in code_path
            if isinstance(item, dict) and _receipt_text(item.get("symbol")) in declared
        ]
        if path_symbols:
            return path_symbols[0]
    sinks_raw = mechanism_link.get("symbol_sinks")
    sink_symbols = sorted(
        {
            str(item.get("symbol"))
            for item in (sinks_raw if isinstance(sinks_raw, list) else [])
            if isinstance(item, dict) and _receipt_text(item.get("symbol")) in declared
        }
    )
    return sink_symbols[0] if len(sink_symbols) == 1 else None


def _derived_causal_root_bindings(
    support: dict[str, Any],
) -> list[dict[str, Any]]:
    """Re-derive immutable causal roots instead of trusting persisted labels."""

    experiment_ids_raw = support.get("experiment_ids")
    experiment_ids = [
        value
        for value in (experiment_ids_raw if isinstance(experiment_ids_raw, list) else [])
        if isinstance(value, str)
    ]
    origin_atom_ids_raw = support.get("origin_atom_ids")
    origin_atom_ids = [
        value
        for value in (origin_atom_ids_raw if isinstance(origin_atom_ids_raw, list) else [])
        if isinstance(value, str)
    ]
    symptom_bindings_raw = support.get("origin_symptom_bindings")
    symptom_bindings = [
        dict(value)
        for value in (symptom_bindings_raw if isinstance(symptom_bindings_raw, list) else [])
        if isinstance(value, dict)
    ]
    mechanism_link_raw = support.get("mechanism_link")
    mechanism_link = mechanism_link_raw if isinstance(mechanism_link_raw, dict) else None
    executed_argv_raw = support.get("executed_argv")
    executed_argv = (
        [value for value in executed_argv_raw if isinstance(value, str)]
        if isinstance(executed_argv_raw, list)
        else None
    )
    authorization_raw = support.get("command_authorization")
    command_authorization = authorization_raw if isinstance(authorization_raw, dict) else None

    roots: list[dict[str, Any]] = []
    experiment_set = set(experiment_ids)
    origin_atom_set = set(origin_atom_ids)
    link_method = (
        _receipt_text(mechanism_link.get("verification_method"))
        if isinstance(mechanism_link, dict)
        else None
    )
    link_entrypoint = (
        _receipt_text(mechanism_link.get("entrypoint"))
        if isinstance(mechanism_link, dict)
        else None
    )
    root_mechanism_symbol = _runner_observed_root_mechanism_symbol(
        support,
        mechanism_link,
    )
    symptom_match_kinds = {
        "command_and_exit_code",
        "command_and_atom_evidence_symptom",
        "faithful_atom_evidence_symptom",
        "command_and_artifact_symptom_text",
        "faithful_artifact_symptom_text",
        "explicit_symptom_field_binding",
    }
    valid_symptom_bindings: list[dict[str, Any]] = []
    for binding in symptom_bindings:
        experiment_id = _receipt_text(binding.get("experiment_id")) or _receipt_text(
            binding.get("baseline_experiment_id")
        )
        atom_id = _receipt_text(binding.get("atom_id"))
        match_kind = _receipt_text(binding.get("match_kind"))
        atom_sha256 = _receipt_text(binding.get("origin_atom_sha256"))
        predicate_binding = bool(
            binding.get("runner_attested") is True
            and binding.get("atom_field_binding_sha256")
            == _canonical_sha256(
                {key: value for key, value in binding.items() if key != "atom_field_binding_sha256"}
            )
            and isinstance(binding.get("observation_predicate"), Mapping)
            and not proof_predicate_contract_errors(binding.get("observation_predicate"))
        )
        if (
            experiment_id not in experiment_set
            or atom_id not in origin_atom_set
            or (match_kind not in symptom_match_kinds and not predicate_binding)
            or atom_sha256 is None
            or re.fullmatch(r"[0-9a-f]{64}", atom_sha256) is None
        ):
            continue
        valid_symptom_bindings.append(dict(binding))
    valid_symptom_bindings.sort(
        key=lambda value: json.dumps(value, sort_keys=True, separators=(",", ":"))
    )
    if (
        valid_symptom_bindings
        and link_method is not None
        and link_method.startswith("runner_")
        and link_entrypoint is not None
        and root_mechanism_symbol is not None
        and isinstance(mechanism_link, dict)
    ):
        roots.append(
            {
                "kind": "origin_symptom_observation",
                "experiment_ids": sorted(experiment_set),
                "origin_atom_ids": sorted(
                    {
                        str(binding["atom_id"])
                        for binding in valid_symptom_bindings
                        if isinstance(binding.get("atom_id"), str)
                    }
                ),
                "origin_bindings_sha256": _canonical_sha256(valid_symptom_bindings),
                "mechanism_link_sha256": _canonical_sha256(mechanism_link),
                "root_mechanism_symbol": root_mechanism_symbol,
            }
        )

    authorization = command_authorization if isinstance(command_authorization, dict) else {}
    argv = executed_argv if isinstance(executed_argv, list) else []
    origin_atom_id = _receipt_text(authorization.get("origin_atom_id"))
    origin_atom_sha256 = _receipt_text(authorization.get("origin_atom_sha256"))
    origin_command_sha256 = _receipt_text(authorization.get("origin_command_value_sha256"))
    authorization_projection = {
        key: value for key, value in authorization.items() if key != "authorization_sha256"
    }
    authorization_attested = bool(
        _receipt_text(authorization.get("authorization_kind")) is not None
        and authorization.get("runner_attested") is True
        and authorization.get("authorization_sha256") == _canonical_sha256(authorization_projection)
        and authorization.get("executed_argv_sha256") == _canonical_sha256(argv)
        and authorization.get("shell") is False
        and authorization.get("workspace_confined") is True
    )
    if (
        argv
        and all(isinstance(token, str) and token for token in argv)
        and authorization_attested
        and origin_atom_id in origin_atom_set
        and authorization.get("origin_atom_field_path") == "$.command"
        and origin_atom_sha256 is not None
        and re.fullmatch(r"[0-9a-f]{64}", origin_atom_sha256) is not None
        and origin_command_sha256 is not None
        and re.fullmatch(r"[0-9a-f]{64}", origin_command_sha256) is not None
        and root_mechanism_symbol is not None
    ):
        roots.append(
            {
                "kind": "immutable_source_command",
                "experiment_ids": sorted(experiment_set),
                "origin_atom_ids": [origin_atom_id],
                "origin_atom_sha256": origin_atom_sha256,
                "origin_atom_field_path": "$.command",
                "origin_command_value_sha256": origin_command_sha256,
                "executed_argv_sha256": authorization["executed_argv_sha256"],
                "root_mechanism_symbol": root_mechanism_symbol,
            }
        )
    return sorted(
        roots,
        key=lambda value: json.dumps(value, sort_keys=True, separators=(",", ":")),
    )


def _runner_verified_support_edges(
    supports: list[dict[str, Any]],
    *,
    hypothesis_symbols: list[str],
) -> list[dict[str, Any]]:
    """Reconstruct only content-addressed, source/AST-attested causal edges."""

    allowed_symbols = set(hypothesis_symbols)
    edges: dict[str, dict[str, Any]] = {}
    for support in supports:
        source_evidence_id = _receipt_text(support.get("mechanism_evidence_id"))
        if source_evidence_id is None:
            continue
        link_raw = support.get("mechanism_link")
        link = link_raw if isinstance(link_raw, dict) else {}
        supplied_link_hash = _receipt_text(link.get("mechanism_link_sha256"))
        link_projection = {
            key: value for key, value in link.items() if key != "mechanism_link_sha256"
        }
        if supplied_link_hash != _canonical_sha256(link_projection):
            continue
        code_path_raw = link.get("code_path")
        code_path = code_path_raw if isinstance(code_path_raw, list) else []
        verified_points = {
            (str(point.get("symbol")), str(point.get("path")))
            for point in code_path
            if isinstance(point, dict)
            and _receipt_text(point.get("symbol")) is not None
            and _receipt_text(point.get("path")) is not None
        }
        directed_edges = link.get("verified_directed_edges")
        for raw_edge in directed_edges if isinstance(directed_edges, list) else []:
            if not isinstance(raw_edge, dict):
                continue
            caller_symbol = _receipt_text(raw_edge.get("from_locator"))
            callee_symbol = _receipt_text(raw_edge.get("to_locator"))
            evidence_sha256 = _receipt_text(raw_edge.get("evidence_sha256"))
            if (
                caller_symbol not in allowed_symbols
                or callee_symbol not in allowed_symbols
                or not _valid_sha256(evidence_sha256)
                or raw_edge.get("runner_attested") is not True
            ):
                continue
            edge = {
                "caller_symbol": caller_symbol,
                "caller_path": caller_symbol,
                "callee_symbol": callee_symbol,
                "callee_path": callee_symbol,
                "edge_kind": raw_edge.get("kind"),
                "edge_evidence_sha256": evidence_sha256,
                "mechanism_link_sha256": supplied_link_hash,
                "source_mechanism_evidence_id": source_evidence_id,
            }
            edge["causal_edge_sha256"] = _canonical_sha256(edge)
            edges[str(edge["causal_edge_sha256"])] = edge
        raw_edges = link.get("verified_call_edges")
        for raw_edge in raw_edges if isinstance(raw_edges, list) else []:
            if not isinstance(raw_edge, dict):
                continue
            caller_symbol = _receipt_text(raw_edge.get("caller_symbol"))
            caller_path = _receipt_text(raw_edge.get("caller_path"))
            callee_symbol = _receipt_text(raw_edge.get("callee_symbol"))
            callee_path = _receipt_text(raw_edge.get("callee_path"))
            resolved_call = _receipt_text(raw_edge.get("resolved_call"))
            call_ast_sha256 = _receipt_text(raw_edge.get("call_ast_sha256"))
            line = raw_edge.get("line")
            if (
                caller_symbol not in allowed_symbols
                or callee_symbol not in allowed_symbols
                or caller_path is None
                or callee_path is None
                or (caller_symbol, caller_path) not in verified_points
                or (callee_symbol, callee_path) not in verified_points
                or resolved_call is None
                or not isinstance(line, int)
                or isinstance(line, bool)
                or line < 1
                or call_ast_sha256 is None
                or re.fullmatch(r"[0-9a-f]{64}", call_ast_sha256) is None
            ):
                continue
            edge = {
                "caller_symbol": caller_symbol,
                "caller_path": caller_path,
                "callee_symbol": callee_symbol,
                "callee_path": callee_path,
                "line": line,
                "resolved_call": resolved_call,
                "call_ast_sha256": call_ast_sha256,
                "mechanism_link_sha256": supplied_link_hash,
                "source_mechanism_evidence_id": source_evidence_id,
            }
            edge["causal_edge_sha256"] = _canonical_sha256(edge)
            edges[str(edge["causal_edge_sha256"])] = edge
    return [edges[key] for key in sorted(edges)]


def _rooted_support_connectivity(
    supports: list[dict[str, Any]],
    *,
    hypothesis_symbols: list[str],
) -> tuple[list[dict[str, Any]], set[str], list[dict[str, Any]], list[str]]:
    """Rebuild the producer's best single-root connected support component."""

    records: dict[str, dict[str, Any]] = {}
    record_symbols: dict[str, set[str]] = {}
    for support in supports:
        evidence_id = _receipt_text(support.get("mechanism_evidence_id"))
        item_symbols = _normalized_mechanism_subset(
            support.get("mechanism_symbols"),
            hypothesis_symbols=hypothesis_symbols,
        )
        if evidence_id is None or item_symbols is None or evidence_id in records:
            continue
        records[evidence_id] = support
        record_symbols[evidence_id] = set(item_symbols)
    edges = _runner_verified_support_edges(
        list(records.values()),
        hypothesis_symbols=hypothesis_symbols,
    )
    edges_by_source: dict[str, list[dict[str, Any]]] = {}
    for edge in edges:
        edges_by_source.setdefault(str(edge["source_mechanism_evidence_id"]), []).append(edge)

    def receipt_forward_closure(
        evidence_id: str,
        *,
        starting_symbols: set[str],
        externally_reachable: set[str],
    ) -> tuple[set[str], list[dict[str, Any]]]:
        support_symbols = record_symbols[evidence_id]
        closure = support_symbols & starting_symbols
        used_edges: list[dict[str, Any]] = []
        remaining_edges = list(edges_by_source.get(evidence_id, []))
        while remaining_edges:
            progressed = False
            for edge in list(remaining_edges):
                caller = str(edge["caller_symbol"])
                callee = str(edge["callee_symbol"])
                if callee not in support_symbols or callee in closure:
                    remaining_edges.remove(edge)
                    continue
                if caller not in externally_reachable and caller not in closure:
                    continue
                closure.add(callee)
                used_edges.append(edge)
                remaining_edges.remove(edge)
                progressed = True
            if not progressed:
                break
        return closure, used_edges

    root_ids = [
        evidence_id
        for evidence_id in sorted(records)
        if isinstance(records[evidence_id].get("causal_root_bindings"), list)
        and records[evidence_id]["causal_root_bindings"]
    ]

    def expand_from_root(
        root_id: str,
    ) -> tuple[set[str], set[str], dict[str, dict[str, Any]]]:
        root = records[root_id]
        roots = root["causal_root_bindings"]
        root_symbols = {
            str(binding.get("root_mechanism_symbol"))
            for binding in roots
            if isinstance(binding, dict)
            and _receipt_text(binding.get("root_mechanism_symbol")) in record_symbols[root_id]
        }
        if len(root_symbols) != 1:
            return set(), set(), {}
        root_symbol = next(iter(root_symbols))
        root_reachable, root_edges = receipt_forward_closure(
            root_id,
            starting_symbols={root_symbol},
            externally_reachable=set(),
        )
        if root_reachable != record_symbols[root_id]:
            return set(), set(), {}
        connected = {root_id}
        reachable_symbols = set(root_reachable)
        trace_by_id: dict[str, dict[str, Any]] = {
            root_id: {
                "mechanism_evidence_id": root_id,
                "experiment_ids": sorted(
                    value for value in root.get("experiment_ids", []) if isinstance(value, str)
                ),
                "connection_kind": "causal_root",
                "connected_from_mechanism_evidence_id": None,
                "shared_verified_symbols": [],
                "verified_causal_edge": None,
                "verified_causal_edges": root_edges,
                "causal_root_kinds": sorted(
                    {
                        str(binding.get("kind"))
                        for binding in roots
                        if isinstance(binding, dict)
                        and _receipt_text(binding.get("kind")) is not None
                    }
                ),
            }
        }
        remaining = set(records) - connected
        while remaining:
            progressed = False
            for evidence_id in sorted(remaining):
                support = records[evidence_id]
                support_symbols = record_symbols[evidence_id]
                shared_candidates: list[tuple[str, list[str]]] = []
                for connected_id in sorted(connected):
                    shared = sorted(support_symbols & record_symbols[connected_id])
                    if shared:
                        shared_candidates.append((connected_id, shared))
                predecessor_id: str | None = None
                shared_symbols: list[str] = []
                causal_edge: dict[str, Any] | None = None
                causal_edges: list[dict[str, Any]] = []
                connection_kind: str | None = None
                if support_symbols.issubset(reachable_symbols) and shared_candidates:
                    predecessor_id, shared_symbols = shared_candidates[0]
                    connection_kind = "shared_verified_symbol"
                else:
                    receipt_reachable, causal_edges = receipt_forward_closure(
                        evidence_id,
                        starting_symbols=support_symbols & reachable_symbols,
                        externally_reachable=reachable_symbols,
                    )
                    if receipt_reachable == support_symbols and causal_edges:
                        boundary_edge = next(
                            (
                                edge
                                for edge in causal_edges
                                if str(edge["caller_symbol"]) in reachable_symbols
                            ),
                            None,
                        )
                        caller = (
                            str(boundary_edge["caller_symbol"])
                            if isinstance(boundary_edge, dict)
                            else None
                        )
                        predecessor_ids = [
                            candidate_id
                            for candidate_id in sorted(connected)
                            if caller in record_symbols[candidate_id]
                        ]
                        if predecessor_ids and boundary_edge is not None:
                            predecessor_id = predecessor_ids[0]
                            causal_edge = boundary_edge
                        connection_kind = "runner_verified_causal_edge"
                if predecessor_id is None or connection_kind is None:
                    continue
                connected.add(evidence_id)
                remaining.remove(evidence_id)
                reachable_symbols.update(support_symbols)
                trace_by_id[evidence_id] = {
                    "mechanism_evidence_id": evidence_id,
                    "experiment_ids": sorted(
                        value
                        for value in support.get("experiment_ids", [])
                        if isinstance(value, str)
                    ),
                    "connection_kind": connection_kind,
                    "connected_from_mechanism_evidence_id": predecessor_id,
                    "shared_verified_symbols": shared_symbols,
                    "verified_causal_edge": causal_edge,
                    "verified_causal_edges": causal_edges,
                    "causal_root_kinds": [],
                }
                progressed = True
            if not progressed:
                break
        return connected, reachable_symbols, trace_by_id

    best_connected: set[str] = set()
    best_symbols: set[str] = set()
    best_trace: dict[str, dict[str, Any]] = {}
    best_score = (-1, -1)
    for root_id in root_ids:
        connected, reachable_symbols, trace = expand_from_root(root_id)
        score = (len(reachable_symbols), len(connected))
        if score > best_score:
            best_connected = connected
            best_symbols = reachable_symbols
            best_trace = trace
            best_score = score

    connected_supports = [records[evidence_id] for evidence_id in sorted(best_connected)]
    support_connectivity = [best_trace[evidence_id] for evidence_id in sorted(best_trace)]
    disconnected = sorted(set(records) - best_connected)
    return connected_supports, best_symbols, support_connectivity, disconnected


def _verified_proof_adapter_for_pair(
    item: Mapping[str, Any],
    receipt: Mapping[str, Any],
    *,
    hypothesis_id: str,
    baseline_experiment_id: str,
    challenge_experiment_id: str,
) -> Mapping[str, Any] | None:
    claim = _declared_proof_adapter_for_pair(
        item,
        hypothesis_id=hypothesis_id,
        baseline_experiment_id=baseline_experiment_id,
        challenge_experiment_id=challenge_experiment_id,
    )
    if not isinstance(claim, Mapping):
        return None
    proofs = receipt.get("proof_adapter_receipts")
    for proof in proofs if isinstance(proofs, list) else []:
        if not isinstance(proof, Mapping) or validate_causal_proof_receipt(proof):
            continue
        observations = proof.get("observations")
        baseline = observations.get("baseline") if isinstance(observations, Mapping) else None
        challenge = observations.get("challenge") if isinstance(observations, Mapping) else None
        intervention = proof.get("intervention")
        claimed_intervention = claim.get("intervention")
        if (
            proof.get("hypothesis_id") == hypothesis_id
            and isinstance(baseline, Mapping)
            and baseline.get("experiment_id") == baseline_experiment_id
            and isinstance(challenge, Mapping)
            and challenge.get("experiment_id") == challenge_experiment_id
            and isinstance(intervention, Mapping)
            and isinstance(claimed_intervention, Mapping)
            and intervention.get("kind") == claimed_intervention.get("kind")
            and intervention.get("target") == claimed_intervention.get("target")
        ):
            return proof
    return None


def _validate_falsification_interventions(
    item: dict[str, Any],
    receipt: dict[str, Any],
    *,
    pid: str,
) -> list[str]:
    """Validate content-addressed runner proof of a real challenge intervention."""

    if receipt.get("status") != "verified":
        return []
    experiments_raw = item.get("experiments")
    experiments = {
        str(experiment.get("experiment_id")): experiment
        for experiment in (experiments_raw if isinstance(experiments_raw, list) else [])
        if isinstance(experiment, dict) and _is_nonempty_string(experiment.get("experiment_id"))
    }
    receipt_experiments_raw = receipt.get("experiments")
    receipt_experiments = {
        str(experiment.get("experiment_id")): experiment
        for experiment in (
            receipt_experiments_raw if isinstance(receipt_experiments_raw, list) else []
        )
        if isinstance(experiment, dict) and _is_nonempty_string(experiment.get("experiment_id"))
    }
    expected: dict[tuple[str, str], dict[str, Any]] = {}
    hypotheses_raw = item.get("root_cause_hypotheses")
    for hypothesis in hypotheses_raw if isinstance(hypotheses_raw, list) else []:
        if not isinstance(hypothesis, dict):
            continue
        hypothesis_id = str(hypothesis.get("hypothesis_id") or "")
        mechanisms = hypothesis.get("mechanism_symbols")
        attempts_raw = hypothesis.get("falsification_attempts")
        for attempt in attempts_raw if isinstance(attempts_raw, list) else []:
            if (
                not isinstance(attempt, dict)
                or not isinstance(attempt.get("outcome"), str)
                or attempt.get("outcome") not in {"survived", "disproved"}
            ):
                continue
            attempt_id = str(attempt.get("attempt_id") or "")
            if (
                _verified_proof_adapter_for_pair(
                    item,
                    receipt,
                    hypothesis_id=hypothesis_id,
                    baseline_experiment_id=str(attempt.get("baseline_experiment_id") or ""),
                    challenge_experiment_id=str(attempt.get("challenge_experiment_id") or ""),
                )
                is not None
            ):
                continue
            if hypothesis_id and attempt_id:
                expected[(hypothesis_id, attempt_id)] = {
                    "attempt": attempt,
                    "mechanism_symbols": mechanisms,
                }

    errors: list[str] = []
    observed: set[tuple[str, str]] = set()
    raw_interventions = receipt.get("falsification_interventions")
    interventions = raw_interventions if isinstance(raw_interventions, list) else []
    for index, intervention in enumerate(interventions):
        if not isinstance(intervention, dict):
            errors.append(f"research_falsification_intervention_invalid: {pid}: {index}")
            continue
        key = (
            str(intervention.get("hypothesis_id") or ""),
            str(intervention.get("attempt_id") or ""),
        )
        expected_entry = expected.get(key)
        attempt = expected_entry.get("attempt", {}) if expected_entry else {}
        baseline_id = str(attempt.get("baseline_experiment_id") or "")
        challenge_id = str(attempt.get("challenge_experiment_id") or "")
        challenge = experiments.get(challenge_id, {})
        relationship_raw = challenge.get("control_relationship")
        relationship = relationship_raw if isinstance(relationship_raw, dict) else {}
        hypothesis_symbols = expected_entry.get("mechanism_symbols") if expected_entry else None
        relationship_symbols = _normalized_mechanism_subset(
            relationship.get("mechanism_symbols"),
            hypothesis_symbols=hypothesis_symbols,
        )
        baseline_replay = receipt_experiments.get(baseline_id, {})
        challenge_replay = receipt_experiments.get(challenge_id, {})
        structural = intervention.get("controlled_input_difference")
        structural = structural if isinstance(structural, dict) else {}
        difference = structural.get("difference")
        difference = difference if isinstance(difference, dict) else {}
        observation = intervention.get("observed_polarity")
        observation = observation if isinstance(observation, dict) else {}
        baseline_observed = observation.get("baseline")
        baseline_observed = baseline_observed if isinstance(baseline_observed, dict) else {}
        challenge_observed = observation.get("challenge")
        challenge_observed = challenge_observed if isinstance(challenge_observed, dict) else {}
        expected_id = "falsification_intervention:" + _canonical_sha256(
            {
                field: value
                for field, value in intervention.items()
                if field != "intervention_receipt_id"
            }
        )
        expected_polarity = (
            "failure_persists_after_intervention"
            if attempt.get("outcome") == "survived"
            else "disproof_observed_after_intervention"
        )
        intervention_method = intervention.get("verification_method")
        modern_subset_receipt = (
            intervention_method == "runner_argv_falsification_intervention_v2"
            or "shared_verified_mechanism_symbols" in intervention
        )
        expected_symbols = (
            (
                relationship_symbols
                if modern_subset_receipt
                else expected_entry.get("mechanism_symbols")
            )
            if expected_entry
            else None
        )
        if intervention_method == "pytest_ast_falsification_intervention_v1":
            selections_valid = (
                intervention.get("baseline_selection_id") == f"{key[0]}:{baseline_id}"
                and intervention.get("challenge_selection_id") == f"{key[0]}:{challenge_id}"
            )
            structural_valid = (
                structural.get("verification_method") == "python_ast_explicit_argument_delta_v1"
                and structural.get("difference_count") == 1
                and difference.get("mechanism_symbol") in (expected_symbols or [])
                and _is_nonempty_string(difference.get("slot"))
                and difference.get("difference_kind")
                in {"added_in_control", "removed_in_control", "changed"}
            )
        elif intervention_method in {
            "runner_argv_falsification_intervention_v1",
            "runner_argv_falsification_intervention_v2",
        }:
            baseline_argument = difference.get("baseline_argument")
            challenge_argument = difference.get("challenge_argument")
            baseline_hash = difference.get("baseline_file_sha256")
            challenge_hash = difference.get("challenge_file_sha256")
            selections_valid = intervention.get(
                "baseline_selection_id"
            ) == "argv_selection:" + _canonical_sha256(
                baseline_replay.get("executed_argv")
            ) and intervention.get(
                "challenge_selection_id"
            ) == "argv_selection:" + _canonical_sha256(challenge_replay.get("executed_argv"))
            if structural.get("verification_method") == ("executed_argv_repository_file_delta_v1"):
                structural_valid = (
                    structural.get("difference_count") == 1
                    and _is_nonempty_string(difference.get("slot"))
                    and str(difference.get("slot")).startswith("argv:")
                    and difference.get("difference_kind") == "repository_file_input_changed"
                    and _is_nonempty_string(baseline_argument)
                    and _is_nonempty_string(challenge_argument)
                    and baseline_argument != challenge_argument
                    and _valid_sha256(baseline_hash)
                    and _valid_sha256(challenge_hash)
                    and difference.get("content_relation")
                    == (
                        "same_content_different_identity"
                        if baseline_hash == challenge_hash
                        else "different_content"
                    )
                )
            else:
                bindings_raw = difference.get("mechanism_argument_bindings")
                bindings = bindings_raw if isinstance(bindings_raw, list) else []
                slot = str(difference.get("slot") or "")
                try:
                    argv_index = int(slot.removeprefix("argv:"))
                except ValueError:
                    argv_index = -1
                baseline_argv = baseline_replay.get("executed_argv")
                challenge_argv = challenge_replay.get("executed_argv")
                harness_path = str(difference.get("harness_path") or "")
                harness_indices = [
                    index
                    for index, value in enumerate(
                        baseline_argv if isinstance(baseline_argv, list) else []
                    )
                    if isinstance(value, str) and value.replace("\\", "/") == harness_path
                ]
                scalar_values_valid = (
                    isinstance(baseline_argv, list)
                    and isinstance(challenge_argv, list)
                    and len(baseline_argv) == len(challenge_argv)
                    and 0 <= argv_index < len(baseline_argv)
                    and baseline_argv[argv_index] != challenge_argv[argv_index]
                    and difference.get("baseline_value_sha256")
                    == _canonical_sha256(baseline_argv[argv_index])
                    and difference.get("challenge_value_sha256")
                    == _canonical_sha256(challenge_argv[argv_index])
                    and len(harness_indices) == 1
                    and difference.get("runtime_argv_index") == argv_index - harness_indices[0]
                )
                bound_symbols = {
                    str(binding.get("symbol"))
                    for binding in bindings
                    if isinstance(binding, dict)
                    and _is_nonempty_string(binding.get("symbol"))
                    and isinstance(binding.get("line"), int)
                    and not isinstance(binding.get("line"), bool)
                    and binding.get("line", 0) > 0
                    and isinstance(binding.get("argument_index"), int)
                    and not isinstance(binding.get("argument_index"), bool)
                    and binding.get("argument_index", -1) >= 0
                    and _valid_sha256(binding.get("argument_ast_sha256"))
                }
                structural_valid = (
                    structural.get("verification_method") == "retained_harness_scalar_argv_delta_v1"
                    and structural.get("difference_count") == 1
                    and _is_nonempty_string(difference.get("slot"))
                    and str(difference.get("slot")).startswith("argv:")
                    and difference.get("difference_kind") == "scalar_argument_changed"
                    and isinstance(difference.get("runtime_argv_index"), int)
                    and not isinstance(difference.get("runtime_argv_index"), bool)
                    and difference.get("runtime_argv_index", 0) > 0
                    and _valid_sha256(difference.get("baseline_value_sha256"))
                    and _valid_sha256(difference.get("challenge_value_sha256"))
                    and difference.get("baseline_value_sha256")
                    != difference.get("challenge_value_sha256")
                    and _is_nonempty_string(difference.get("harness_path"))
                    and str(difference.get("harness_path")).startswith(".usertest_research/")
                    and str(difference.get("harness_path")).endswith(".py")
                    and _valid_sha256(difference.get("harness_sha256"))
                    and scalar_values_valid
                    and bool(bindings)
                    and len(bindings)
                    == len([binding for binding in bindings if isinstance(binding, dict)])
                    and bound_symbols == set(expected_symbols or [])
                )
        else:
            selections_valid = False
            structural_valid = False
        if modern_subset_receipt:
            shared_mechanism_valid = (
                relationship_symbols is not None
                and intervention.get("baseline_verified_mechanism_symbols") == relationship_symbols
                and intervention.get("challenge_verified_mechanism_symbols") == relationship_symbols
                and intervention.get("shared_verified_mechanism_symbols") == relationship_symbols
                and (
                    intervention.get("mechanism_verification_mode") == "pytest_ast_selection"
                    if intervention_method == "pytest_ast_falsification_intervention_v1"
                    else intervention.get("mechanism_verification_mode")
                    in {
                        "retained_harness_observable_dataflow",
                        "declared_python_call_chain",
                        "runner_exception_symbol_trace",
                    }
                )
            )
        else:
            shared_mechanism_valid = relationship.get("mechanism_symbols") == expected_symbols
        valid = (
            expected_entry is not None
            and key not in observed
            and intervention_method
            in {
                "pytest_ast_falsification_intervention_v1",
                "runner_argv_falsification_intervention_v1",
                "runner_argv_falsification_intervention_v2",
            }
            and intervention.get("baseline_experiment_id") == baseline_id
            and intervention.get("challenge_experiment_id") == challenge_id
            and expected_symbols is not None
            and intervention.get("mechanism_symbols") == expected_symbols
            and shared_mechanism_valid
            and selections_valid
            and challenge.get("scenario_kind") == "control"
            and relationship.get("supports_experiment_id") == baseline_id
            and relationship_symbols == expected_symbols
            and intervention.get("relationship_sha256")
            == _canonical_sha256(
                {
                    "controlled_variable": relationship.get("controlled_variable"),
                    "expected_difference": relationship.get("expected_difference"),
                    "mechanism_symbols": expected_symbols,
                }
            )
            and structural_valid
            and observation.get("verification_method") == "runner_replay_falsification_polarity_v1"
            and observation.get("polarity") == expected_polarity
            and baseline_replay.get("assertion_passed") is True
            and challenge_replay.get("assertion_passed") is True
            and baseline_observed
            == {
                "exit_code": baseline_replay.get("exit_code"),
                "stdout_sha256": baseline_replay.get("stdout_sha256"),
                "stderr_sha256": baseline_replay.get("stderr_sha256"),
            }
            and challenge_observed
            == {
                "exit_code": challenge_replay.get("exit_code"),
                "stdout_sha256": challenge_replay.get("stdout_sha256"),
                "stderr_sha256": challenge_replay.get("stderr_sha256"),
            }
            and _valid_sha256(baseline_observed.get("stdout_sha256"))
            and _valid_sha256(baseline_observed.get("stderr_sha256"))
            and _valid_sha256(challenge_observed.get("stdout_sha256"))
            and _valid_sha256(challenge_observed.get("stderr_sha256"))
            and intervention.get("intervention_receipt_id") == expected_id
        )
        if valid:
            observed.add(key)
        else:
            errors.append(f"research_falsification_intervention_invalid: {pid}: {index}")
    if observed != set(expected):
        errors.append(f"research_falsification_intervention_coverage_mismatch: {pid}")
    return errors


def _validate_deterministic_mechanism_closures(
    item: dict[str, Any],
    receipt: dict[str, Any],
    *,
    pid: str,
) -> list[str]:
    if receipt.get("status") != "verified":
        return []
    hypotheses_raw = item.get("root_cause_hypotheses")
    hypothesis_ids = [
        str(value.get("hypothesis_id"))
        for value in (hypotheses_raw if isinstance(hypotheses_raw, list) else [])
        if isinstance(value, dict) and _is_nonempty_string(value.get("hypothesis_id"))
    ]
    verified = [
        closure
        for hypothesis_id in hypothesis_ids
        for closure in verified_deterministic_mechanism_closures(
            item,
            hypothesis_id=hypothesis_id,
        )
    ]
    raw = receipt.get("deterministic_mechanism_closures")
    observed = raw if isinstance(raw, list) else []
    if observed != sorted(
        verified,
        key=lambda value: str(value.get("closure_receipt_id")),
    ):
        return [f"research_deterministic_mechanism_closure_invalid: {pid}"]
    return []


def _validate_verified_mechanism_projection(
    item: dict[str, Any],
    receipt: dict[str, Any],
    *,
    pid: str,
) -> list[str]:
    projection = receipt.get("verified_mechanism")
    digest = receipt.get("verified_mechanism_sha256")
    provenance = receipt.get("verified_mechanism_provenance")
    provenance_digest = receipt.get("verified_mechanism_provenance_sha256")
    if receipt.get("status") != "verified":
        return (
            []
            if projection is None
            and digest is None
            and provenance is None
            and provenance_digest is None
            else [f"research_verified_mechanism_present_on_failed_receipt: {pid}"]
        )
    if item.get("research_status") != "evidence_sufficient" and all(
        value is None
        for value in (
            projection,
            digest,
            provenance,
            provenance_digest,
        )
    ):
        return []
    current_schema = item.get("research_schema_version") == RESEARCH_PROOF_SCHEMA_VERSION
    projection_present = any(
        value is not None
        for value in (
            projection,
            digest,
            provenance,
            provenance_digest,
        )
    )
    current_projection_required = current_schema and (
        item.get("research_status") == "evidence_sufficient" or projection_present
    )
    if current_projection_required:
        schema_errors: list[str] = []
        if not isinstance(projection, dict) or projection.get("schema_version") != 3:
            schema_errors.append(f"research_verified_mechanism_current_schema_required: {pid}")
        if not isinstance(provenance, dict) or provenance.get("schema_version") != 2:
            schema_errors.append(f"research_verified_mechanism_current_provenance_required: {pid}")
        if schema_errors:
            return schema_errors
    hypotheses_raw = item.get("root_cause_hypotheses")
    hypotheses = hypotheses_raw if isinstance(hypotheses_raw, list) else []
    primary = hypotheses[0] if hypotheses and isinstance(hypotheses[0], dict) else {}
    hypothesis_id = primary.get("hypothesis_id")
    symbols = primary.get("mechanism_symbols")
    # The dossier schema selects the verification contract. A current dossier
    # cannot opt itself into the historical projection rules by changing the
    # schema number inside a runner-owned receipt and recomputing its hashes.
    aggregate_projection = current_schema
    evidence_raw = receipt.get("mechanism_evidence")
    candidate_evidence: list[dict[str, Any]] = []
    evidence: list[dict[str, Any]] = []
    covered_symbols: set[str] = set()
    support_connectivity: list[dict[str, Any]] = []
    for value in evidence_raw if isinstance(evidence_raw, list) else []:
        if not isinstance(value, dict) or value.get("hypothesis_id") != hypothesis_id:
            continue
        if aggregate_projection:
            value_symbols = _normalized_mechanism_subset(
                value.get("mechanism_symbols"),
                hypothesis_symbols=symbols,
            )
            points_raw = value.get("code_paths")
            points = points_raw if isinstance(points_raw, list) else []
            point_symbols = {
                str(point.get("symbol"))
                for point in points
                if isinstance(point, dict)
                and _is_nonempty_string(point.get("symbol"))
                and _is_nonempty_string(point.get("path"))
            }
            if (
                value_symbols is None
                or value.get("adversarial_effect") != "supports_selection"
                or point_symbols != set(value_symbols)
            ):
                continue
            declared_roots = value.get("causal_root_bindings")
            derived_roots = (
                [dict(root) for root in declared_roots if isinstance(root, dict)]
                if value.get("evidence_type") == "adapter_proof"
                and isinstance(declared_roots, list)
                else _derived_causal_root_bindings(value)
            )
            if declared_roots is not None and declared_roots != derived_roots:
                continue
            projected_value = dict(value)
            projected_value["causal_root_bindings"] = derived_roots
            candidate_evidence.append(projected_value)
        elif value.get("mechanism_symbols") != symbols:
            continue
        else:
            evidence.append(value)
    if aggregate_projection:
        hypothesis_symbols = (
            [value for value in symbols if isinstance(value, str)]
            if isinstance(symbols, list)
            else []
        )
        evidence, covered_symbols, support_connectivity, _ = _rooted_support_connectivity(
            candidate_evidence,
            hypothesis_symbols=hypothesis_symbols,
        )
    code_paths = sorted(
        {
            (
                str(point.get("symbol")).strip(),
                PurePosixPath(
                    str(point.get("path")).replace("\\", "/").removeprefix("./")
                ).as_posix(),
            )
            for value in evidence
            for point in (
                value.get("code_paths") if isinstance(value.get("code_paths"), list) else []
            )
            if isinstance(point, dict)
            and _is_nonempty_string(point.get("symbol"))
            and _is_nonempty_string(point.get("path"))
        }
    )
    normalized_symbols = sorted(
        {
            symbol.strip()
            for symbol in (symbols if isinstance(symbols, list) else [])
            if isinstance(symbol, str) and symbol.strip()
        }
    )
    control_verifications = (
        receipt.get("control_verifications")
        if isinstance(receipt.get("control_verifications"), list)
        else []
    )
    falsification_interventions = (
        receipt.get("falsification_interventions")
        if isinstance(receipt.get("falsification_interventions"), list)
        else []
    )
    deterministic_closures = (
        receipt.get("deterministic_mechanism_closures")
        if isinstance(receipt.get("deterministic_mechanism_closures"), list)
        else []
    )
    control_points: list[dict[str, Any]] = []
    for causal_receipt in (*control_verifications, *falsification_interventions):
        if (
            not isinstance(causal_receipt, dict)
            or causal_receipt.get("hypothesis_id") != hypothesis_id
        ):
            continue
        receipt_symbols_raw = causal_receipt.get("mechanism_symbols")
        if aggregate_projection:
            receipt_symbols = _normalized_mechanism_subset(
                receipt_symbols_raw,
                hypothesis_symbols=symbols,
            )
            if receipt_symbols is None:
                continue
        else:
            receipt_symbols = sorted(
                {
                    symbol.strip()
                    for symbol in (
                        receipt_symbols_raw if isinstance(receipt_symbols_raw, list) else []
                    )
                    if isinstance(symbol, str) and symbol.strip()
                }
            )
            if receipt_symbols != normalized_symbols:
                continue
        controlled = causal_receipt.get("controlled_input_difference")
        difference = controlled.get("difference") if isinstance(controlled, dict) else None
        method = controlled.get("verification_method") if isinstance(controlled, dict) else None
        slot = difference.get("slot") if isinstance(difference, dict) else None
        if not _is_nonempty_string(method) or not _is_nonempty_string(slot):
            continue
        descriptor: dict[str, Any] = {
            "verification_method": method,
            "mechanism_symbols": receipt_symbols,
            "slot": slot,
        }
        mechanism_symbol = difference.get("mechanism_symbol")
        if _is_nonempty_string(mechanism_symbol):
            descriptor["mechanism_symbol"] = mechanism_symbol
        control_points.append(descriptor)
    unique_control_points = sorted(
        {
            json.dumps(value, sort_keys=True, separators=(",", ":")): value
            for value in control_points
        }.values(),
        key=lambda value: json.dumps(value, sort_keys=True, separators=(",", ":")),
    )
    expected = {
        "schema_version": 3 if aggregate_projection else 2,
        "mechanism_symbols": normalized_symbols,
        "code_paths": [{"symbol": symbol, "path": path} for symbol, path in code_paths],
    }
    mechanism_targets = sorted(
        {
            json.dumps(target, sort_keys=True, separators=(",", ":")): target
            for value in evidence
            for target in (
                value.get("mechanism_targets")
                if isinstance(value.get("mechanism_targets"), list)
                else []
            )
            if isinstance(target, dict)
        }.values(),
        key=lambda value: json.dumps(value, sort_keys=True, separators=(",", ":")),
    )
    if mechanism_targets:
        expected["mechanism_targets"] = mechanism_targets
    expected_provenance = {
        "schema_version": 2 if aggregate_projection else 1,
        "primary_hypothesis_id": hypothesis_id,
        "mechanism_evidence_ids": sorted(
            str(value.get("mechanism_evidence_id")) for value in evidence
        ),
        "causal_control_ids": sorted(
            str(value.get("control_verification_id"))
            for value in control_verifications
            if isinstance(value, dict)
            and value.get("hypothesis_id") == hypothesis_id
            and _is_nonempty_string(value.get("control_verification_id"))
        ),
        "falsification_intervention_ids": sorted(
            str(value.get("intervention_receipt_id"))
            for value in falsification_interventions
            if isinstance(value, dict)
            and value.get("hypothesis_id") == hypothesis_id
            and _is_nonempty_string(value.get("intervention_receipt_id"))
        ),
        "deterministic_closure_ids": sorted(
            str(value.get("closure_receipt_id"))
            for value in deterministic_closures
            if isinstance(value, dict)
            and value.get("hypothesis_id") == hypothesis_id
            and _is_nonempty_string(value.get("closure_receipt_id"))
        ),
        "research_probe_control_points": unique_control_points,
    }
    intervention_targets = sorted(
        {
            json.dumps(target, sort_keys=True, separators=(",", ":")): target
            for value in evidence
            for target in (
                value.get("intervention_targets")
                if isinstance(value.get("intervention_targets"), list)
                else []
            )
            if isinstance(target, dict)
        }.values(),
        key=lambda value: json.dumps(value, sort_keys=True, separators=(",", ":")),
    )
    if intervention_targets:
        expected_provenance["intervention_targets"] = intervention_targets
    if aggregate_projection:
        expected_provenance["causal_root_evidence_ids"] = sorted(
            str(value.get("mechanism_evidence_id"))
            for value in evidence
            if value.get("causal_root_bindings")
        )
        expected_provenance["support_connectivity"] = support_connectivity
        expected_provenance["support_symbol_coverage"] = sorted(
            (
                {
                    "experiment_ids": sorted(
                        experiment_id
                        for experiment_id in value.get("experiment_ids", [])
                        if isinstance(experiment_id, str)
                    ),
                    "mechanism_symbols": value.get("mechanism_symbols"),
                }
                for value in evidence
            ),
            key=lambda value: json.dumps(value, sort_keys=True, separators=(",", ":")),
        )
    if (
        not isinstance(projection, dict)
        or (
            aggregate_projection
            and (
                covered_symbols != set(normalized_symbols)
                or {symbol for symbol, _ in code_paths} != set(normalized_symbols)
                or not any(
                    value.get("connection_kind") == "causal_root" for value in support_connectivity
                )
            )
        )
        or projection != expected
        or digest != _canonical_sha256(expected)
        or not isinstance(provenance, dict)
        or provenance != expected_provenance
        or provenance_digest != _canonical_sha256(expected_provenance)
    ):
        return [f"research_verified_mechanism_projection_invalid: {pid}"]
    return []


def _validate_causal_control_verification(
    item: dict[str, Any],
    receipt: dict[str, Any],
    *,
    pid: str,
) -> list[str]:
    """Validate runner-owned exact-test, input-delta, and replay-control receipts."""
    if receipt.get("status") != "verified":
        return []
    errors: list[str] = []
    experiments_raw = item.get("experiments")
    experiments = {
        str(experiment.get("experiment_id")): experiment
        for experiment in (experiments_raw if isinstance(experiments_raw, list) else [])
        if isinstance(experiment, dict) and _is_nonempty_string(experiment.get("experiment_id"))
    }
    receipt_experiments_raw = receipt.get("experiments")
    receipt_experiments = {
        str(experiment.get("experiment_id")): experiment
        for experiment in (
            receipt_experiments_raw if isinstance(receipt_experiments_raw, list) else []
        )
        if isinstance(experiment, dict) and _is_nonempty_string(experiment.get("experiment_id"))
    }
    symbol_receipts_raw = receipt.get("inspected_symbols")
    symbol_paths = {
        str(symbol.get("symbol")): str(symbol.get("path"))
        for symbol in (symbol_receipts_raw if isinstance(symbol_receipts_raw, list) else [])
        if isinstance(symbol, dict)
        and _is_nonempty_string(symbol.get("symbol"))
        and _is_nonempty_string(symbol.get("path"))
    }
    expected_selections: dict[str, tuple[str, str, list[str]]] = {}
    expected_controls: dict[tuple[str, str], dict[str, Any]] = {}
    hypotheses_raw = item.get("root_cause_hypotheses")
    hypotheses = hypotheses_raw if isinstance(hypotheses_raw, list) else []
    for hypothesis in hypotheses:
        if not isinstance(hypothesis, dict):
            continue
        hypothesis_id = str(hypothesis.get("hypothesis_id") or "")
        mechanism_raw = hypothesis.get("mechanism_symbols")
        mechanism_symbols = (
            [symbol for symbol in mechanism_raw if isinstance(symbol, str)]
            if isinstance(mechanism_raw, list)
            else []
        )
        counter_raw = hypothesis.get("counterevidence")
        counter_ids = counter_raw if isinstance(counter_raw, list) else []
        for raw_control_id in counter_ids:
            control_id = str(raw_control_id)
            control = experiments.get(control_id, {})
            if control.get("scenario_kind") != "control" or control.get("outcome") != "refutes":
                continue
            relationship_raw = control.get("control_relationship")
            relationship = relationship_raw if isinstance(relationship_raw, dict) else {}
            relationship_symbols = _normalized_mechanism_subset(
                relationship.get("mechanism_symbols"),
                hypothesis_symbols=mechanism_symbols,
            )
            if relationship_symbols is None:
                continue
            support_id = str(relationship.get("supports_experiment_id") or "")
            support_selection_id = f"{hypothesis_id}:{support_id}"
            control_selection_id = f"{hypothesis_id}:{control_id}"
            expected_selections[support_selection_id] = (
                hypothesis_id,
                support_id,
                relationship_symbols,
            )
            expected_selections[control_selection_id] = (
                hypothesis_id,
                control_id,
                relationship_symbols,
            )
            expected_controls[(hypothesis_id, control_id)] = {
                "support_id": support_id,
                "support_selection_id": support_selection_id,
                "control_selection_id": control_selection_id,
                "mechanism_symbols": relationship_symbols,
                "relationship_sha256": _canonical_sha256(
                    {
                        "controlled_variable": relationship.get("controlled_variable"),
                        "expected_difference": relationship.get("expected_difference"),
                        "mechanism_symbols": relationship_symbols,
                    }
                ),
            }

    selections_raw = receipt.get("test_selections")
    selections = selections_raw if isinstance(selections_raw, list) else []
    selections_by_id: dict[str, dict[str, Any]] = {}
    for index, selection in enumerate(selections):
        if not isinstance(selection, dict):
            errors.append(f"research_evidence_verification_test_selection_invalid: {pid}: {index}")
            continue
        selection_id = str(selection.get("selection_id") or "")
        expected = expected_selections.get(selection_id)
        if not selection_id or selection_id in selections_by_id or expected is None:
            errors.append(f"research_evidence_verification_test_selection_invalid: {pid}: {index}")
            continue
        selections_by_id[selection_id] = selection
        hypothesis_id, experiment_id, mechanism_symbols = expected
        experiment = experiments.get(experiment_id, {})
        replay = receipt_experiments.get(experiment_id, {})
        selector_parts = selection.get("selector_parts")
        reachable_functions = selection.get("reachable_functions")
        test_path = str(selection.get("test_path") or "").replace("\\", "/")
        path_parts = [part for part in test_path.split("/") if part]
        selection_valid = (
            selection.get("hypothesis_id") == hypothesis_id
            and selection.get("experiment_id") == experiment_id
            and selection.get("runner") == "pytest"
            and selection.get("command_sha256")
            == sha256(str(experiment.get("command") or "").encode()).hexdigest()
            and selection.get("executed_argv_sha256")
            == _canonical_sha256(replay.get("executed_argv"))
            and _is_nonempty_string(selection.get("test_path"))
            and test_path.endswith(".py")
            and not test_path.startswith("/")
            and not re.match(r"^[A-Za-z]:/", test_path)
            and ".." not in path_parts
            and ".usertest_research" not in test_path.casefold()
            and _valid_sha256(selection.get("test_file_sha256"))
            and isinstance(selection.get("test_file_git_blob_sha"), str)
            and bool(
                re.fullmatch(
                    r"[0-9a-f]{40}|[0-9a-f]{64}",
                    str(selection.get("test_file_git_blob_sha")),
                )
            )
            and isinstance(selector_parts, list)
            and bool(selector_parts)
            and all(
                isinstance(part, str) and part.isidentifier()
                for part in (selector_parts if isinstance(selector_parts, list) else [])
            )
            and str(selector_parts[-1]).startswith("test")
            and selection.get("selector") == "::".join(selector_parts)
            and selection.get("test_function") == ".".join(selector_parts)
            and isinstance(selection.get("test_function_line"), int)
            and not isinstance(selection.get("test_function_line"), bool)
            and selection.get("test_function_line", 0) > 0
            and _valid_sha256(selection.get("test_function_source_sha256"))
            and isinstance(reachable_functions, list)
            and selection.get("test_function") in reachable_functions
            and all(_is_nonempty_string(name) for name in reachable_functions)
        )
        touches_raw = selection.get("mechanism_touches")
        touches = touches_raw if isinstance(touches_raw, list) else []
        touched_symbols: set[str] = set()
        for touch in touches:
            if not isinstance(touch, dict):
                selection_valid = False
                continue
            symbol = str(touch.get("symbol") or "")
            calls_raw = touch.get("calls")
            calls = calls_raw if isinstance(calls_raw, list) else []
            valid_calls = bool(calls)
            for call in calls:
                if not isinstance(call, dict):
                    valid_calls = False
                    continue
                arguments = call.get("arguments")
                if not isinstance(arguments, list):
                    valid_calls = False
                    arguments = []
                argument_slots: set[str] = set()
                valid_arguments = True
                for argument in arguments:
                    if not isinstance(argument, dict):
                        valid_arguments = False
                        continue
                    slot = argument.get("slot")
                    if (
                        not _is_nonempty_string(slot)
                        or str(slot) in argument_slots
                        or not _is_nonempty_string(argument.get("expression"))
                        or not _valid_sha256(argument.get("ast_sha256"))
                    ):
                        valid_arguments = False
                    else:
                        argument_slots.add(str(slot))
                valid_calls = valid_calls and (
                    call.get("function") in reachable_functions
                    and isinstance(call.get("line"), int)
                    and not isinstance(call.get("line"), bool)
                    and call.get("line", 0) > 0
                    and _is_nonempty_string(call.get("expression"))
                    and _is_nonempty_string(call.get("resolved_target"))
                    and isinstance(call.get("arguments_complete"), bool)
                    and valid_arguments
                )
            if (
                symbol not in mechanism_symbols
                or touch.get("source_path") != symbol_paths.get(symbol)
                or not valid_calls
            ):
                selection_valid = False
            else:
                touched_symbols.add(symbol)
        if touched_symbols != set(mechanism_symbols):
            selection_valid = False
        if not selection_valid:
            errors.append(f"research_evidence_verification_test_selection_invalid: {pid}: {index}")
    if set(selections_by_id) != set(expected_selections):
        errors.append(f"research_evidence_verification_test_selection_coverage_mismatch: {pid}")

    controls_raw = receipt.get("control_verifications")
    controls = controls_raw if isinstance(controls_raw, list) else []
    observed_controls: set[tuple[str, str]] = set()
    verified_controls_by_id: dict[str, dict[str, Any]] = {}
    for index, control in enumerate(controls):
        if not isinstance(control, dict):
            errors.append(f"research_evidence_verification_control_invalid: {pid}: {index}")
            continue
        key = (
            str(control.get("hypothesis_id") or ""),
            str(control.get("control_experiment_id") or ""),
        )
        expected = expected_controls.get(key)
        support_selection = selections_by_id.get(
            str(control.get("support_selection_id") or ""),
            {},
        )
        control_selection = selections_by_id.get(
            str(control.get("control_selection_id") or ""),
            {},
        )
        expected_structural = (
            _expected_structural_control_difference(
                support_selection,
                control_selection,
                expected["mechanism_symbols"],
            )
            if expected is not None
            else None
        )
        support_experiment = experiments.get(expected["support_id"], {}) if expected else {}
        control_experiment = experiments.get(key[1], {})
        support_replay = receipt_experiments.get(expected["support_id"], {}) if expected else {}
        control_replay = receipt_experiments.get(key[1], {})
        support_assertion = support_experiment.get("observable_assertion")
        control_assertion = control_experiment.get("observable_assertion")
        support_assertion = support_assertion if isinstance(support_assertion, dict) else {}
        control_assertion = control_assertion if isinstance(control_assertion, dict) else {}
        observable = control.get("observable_difference")
        observable = observable if isinstance(observable, dict) else {}
        observable_support = observable.get("support")
        observable_support = observable_support if isinstance(observable_support, dict) else {}
        observable_control = observable.get("control")
        observable_control = observable_control if isinstance(observable_control, dict) else {}
        source = support_assertion.get("source")
        common_observable_valid = (
            observable.get("verification_method") == "runner_replay_complement_v1"
            and isinstance(source, str)
            and source in {"exit_code", "stdout", "stderr", "combined"}
            and control_assertion.get("source") == source
            and support_replay.get("assertion_passed") is True
            and control_replay.get("assertion_passed") is True
            and observable_support.get("exit_code") == support_replay.get("exit_code")
            and observable_control.get("exit_code") == control_replay.get("exit_code")
            and observable_support.get("stdout_sha256") == support_replay.get("stdout_sha256")
            and observable_support.get("stderr_sha256") == support_replay.get("stderr_sha256")
            and observable_control.get("stdout_sha256") == control_replay.get("stdout_sha256")
            and observable_control.get("stderr_sha256") == control_replay.get("stderr_sha256")
            and _valid_sha256(observable_support.get("observed_sha256"))
            and _valid_sha256(observable_control.get("observed_sha256"))
            and observable_support.get("observed_sha256")
            != observable_control.get("observed_sha256")
        )
        if source == "exit_code":
            observable_valid = common_observable_valid and (
                observable.get("difference_kind") == "failing_exit_to_zero"
                and observable.get("expected_sha256") is None
                and support_assertion.get("operator") == "equals"
                and control_assertion.get("operator") == "equals"
                and support_assertion.get("expected") == support_replay.get("exit_code")
                and control_assertion.get("expected") == control_replay.get("exit_code")
                and isinstance(support_replay.get("exit_code"), int)
                and not isinstance(support_replay.get("exit_code"), bool)
                and support_replay.get("exit_code") != 0
                and control_replay.get("exit_code") == 0
                and observable_support.get("observed_sha256")
                == _canonical_sha256(support_replay.get("exit_code"))
                and observable_control.get("observed_sha256")
                == _canonical_sha256(control_replay.get("exit_code"))
            )
        elif observable.get("difference_kind") == "wrong_value_corrected":
            support_expected = support_assertion.get("expected")
            control_expected = control_assertion.get("expected")
            observable_valid = common_observable_valid and (
                source in {"stdout", "stderr", "combined"}
                and observable.get("expected_sha256") is None
                and support_assertion.get("operator") == "equals"
                and control_assertion.get("operator") == "equals"
                and isinstance(support_expected, str)
                and isinstance(control_expected, str)
                and support_expected != control_expected
                and (
                    support_replay.get("stdout_sha256") != control_replay.get("stdout_sha256")
                    or support_replay.get("stderr_sha256") != control_replay.get("stderr_sha256")
                )
                and observable.get("support_expected_sha256") == _canonical_sha256(support_expected)
                and observable.get("control_expected_sha256") == _canonical_sha256(control_expected)
                and (
                    source == "combined"
                    or observable_support.get("observed_sha256")
                    == support_replay.get(f"{source}_sha256")
                )
                and (
                    source == "combined"
                    or observable_control.get("observed_sha256")
                    == control_replay.get(f"{source}_sha256")
                )
            )
        else:
            expected_marker = support_assertion.get("expected")
            observable_valid = common_observable_valid and (
                observable.get("difference_kind") == "failure_marker_removed"
                and support_assertion.get("operator") == "contains"
                and control_assertion.get("operator") == "not_contains"
                and isinstance(expected_marker, str)
                and bool(expected_marker)
                and control_assertion.get("expected") == expected_marker
                and observable.get("expected_sha256")
                == sha256(expected_marker.encode()).hexdigest()
                and (
                    source == "combined"
                    or observable_support.get("observed_sha256")
                    == support_replay.get(f"{source}_sha256")
                )
                and (
                    source == "combined"
                    or observable_control.get("observed_sha256")
                    == control_replay.get(f"{source}_sha256")
                )
            )
        control_verification_id = str(control.get("control_verification_id") or "")
        expected_control_verification_id = "control_verification:" + _canonical_sha256(
            {field: value for field, value in control.items() if field != "control_verification_id"}
        )
        modern_subset_receipt = "support_verified_mechanism_symbols" in control
        shared_mechanism_valid = (
            (
                control.get("support_verified_mechanism_symbols") == expected["mechanism_symbols"]
                and control.get("control_verified_mechanism_symbols")
                == expected["mechanism_symbols"]
                and control.get("shared_verified_mechanism_symbols")
                == expected["mechanism_symbols"]
                and control.get("mechanism_verification_mode") == "pytest_ast_selection"
            )
            if modern_subset_receipt and expected is not None
            else (
                control.get("shared_verified_mechanism_symbols") == expected["mechanism_symbols"]
                if expected is not None
                else False
            )
        )
        valid = (
            expected is not None
            and key not in observed_controls
            and control.get("verification_method") == "pytest_ast_controlled_difference_v2"
            and control.get("support_experiment_id") == expected["support_id"]
            and control.get("support_selection_id") == expected["support_selection_id"]
            and control.get("control_selection_id") == expected["control_selection_id"]
            and control.get("mechanism_symbols") == expected["mechanism_symbols"]
            and shared_mechanism_valid
            and isinstance(control.get("same_test_file"), bool)
            and control.get("same_test_file")
            is (support_selection.get("test_path") == control_selection.get("test_path"))
            and control.get("relationship_sha256") == expected["relationship_sha256"]
            and control.get("controlled_input_difference") == expected_structural
            and expected_structural is not None
            and observable_valid
            and control.get("adversarial_effect") == "limits_scope"
            and control_verification_id == expected_control_verification_id
        )
        if valid:
            observed_controls.add(key)
            verified_controls_by_id[control_verification_id] = control
        else:
            errors.append(f"research_evidence_verification_control_invalid: {pid}: {index}")
    if observed_controls != set(expected_controls):
        errors.append(f"research_evidence_verification_control_coverage_mismatch: {pid}")
    failure_paths_raw = receipt.get("failure_paths")
    failure_paths = failure_paths_raw if isinstance(failure_paths_raw, list) else []
    observed_failure_controls: set[str] = set()
    observed_failure_ids: set[str] = set()
    for index, path in enumerate(failure_paths):
        if not isinstance(path, dict):
            errors.append(f"research_evidence_verification_failure_path_invalid: {pid}: {index}")
            continue
        control_verification_id = str(path.get("control_verification_id") or "")
        control = verified_controls_by_id.get(control_verification_id)
        support_selection = (
            selections_by_id.get(str(control.get("support_selection_id") or ""), {})
            if isinstance(control, dict)
            else {}
        )
        support_id = str(control.get("support_experiment_id") or "") if control else ""
        support_experiment = experiments.get(support_id, {})
        origin_ids_raw = support_experiment.get("addresses_atom_ids")
        origin_ids = sorted(
            {
                str(atom_id).strip()
                for atom_id in (origin_ids_raw if isinstance(origin_ids_raw, list) else [])
                if _is_nonempty_string(atom_id)
            }
        )
        path_name = (
            f"{support_selection.get('test_path', '')}::{support_selection.get('selector', '')}"
        )
        consumer_identity = {
            "kind": "evidence_selector",
            "entrypoint": path_name,
        }
        independence_key = _canonical_sha256(consumer_identity)
        observable = control.get("observable_difference") if control else None
        observable = observable if isinstance(observable, dict) else {}
        support_observation = observable.get("support")
        support_observation = support_observation if isinstance(support_observation, dict) else {}
        expected_observed_failure = {
            "source": observable.get("source"),
            "difference_kind": observable.get("difference_kind"),
            **support_observation,
        }
        failure_path_id = str(path.get("failure_path_id") or "")
        expected_failure_path_id = "failure_path:" + _canonical_sha256(
            {field: value for field, value in path.items() if field != "failure_path_id"}
        )
        valid_path = (
            control is not None
            and control_verification_id not in observed_failure_controls
            and failure_path_id not in observed_failure_ids
            and path.get("verification_method") == "runner_controlled_failure_path_v1"
            and path.get("path_name") == path_name
            and path.get("consumer_identity") == consumer_identity
            and path.get("independence_key") == independence_key
            and path.get("hypothesis_id") == control.get("hypothesis_id")
            and path.get("support_experiment_id") == support_id
            and path.get("support_selection_id") == control.get("support_selection_id")
            and path.get("mechanism_symbols") == control.get("mechanism_symbols")
            and path.get("origin_atom_ids") == origin_ids
            and bool(origin_ids)
            and path.get("observed_failure") == expected_observed_failure
            and failure_path_id == expected_failure_path_id
        )
        if valid_path:
            observed_failure_controls.add(control_verification_id)
            observed_failure_ids.add(failure_path_id)
        else:
            errors.append(f"research_evidence_verification_failure_path_invalid: {pid}: {index}")
    if observed_failure_controls != set(verified_controls_by_id):
        errors.append(f"research_evidence_verification_failure_path_coverage_mismatch: {pid}")
    return errors


def _content_bound_consumer_identity_valid(value: Any) -> bool:
    if not isinstance(value, Mapping):
        return False
    supplied = value.get("consumer_identity_sha256")
    return (
        value.get("runner_attested") is True
        and _is_nonempty_string(value.get("kind"))
        and _is_nonempty_string(value.get("entrypoint"))
        and _valid_sha256(supplied)
        and supplied
        == _canonical_sha256(
            {key: item for key, item in value.items() if key != "consumer_identity_sha256"}
        )
    )


def _expected_adapter_executed_consumer(
    proof: Mapping[str, Any],
    *,
    experiments: Mapping[str, Mapping[str, Any]],
    implementation_touchpoints: list[Mapping[str, Any]],
) -> dict[str, Any] | None:
    observations = proof.get("observations")
    experiment_ids = [
        _receipt_text(observation.get("experiment_id"))
        for observation in (
            observations.get("baseline"),
            observations.get("challenge"),
        )
        if isinstance(observations, Mapping) and isinstance(observation, Mapping)
    ]
    if len(experiment_ids) != 2 or any(experiment_id is None for experiment_id in experiment_ids):
        return None
    authorization_identity: dict[str, Any] | None = None
    invocations: list[dict[str, Any]] = []
    for experiment_id in experiment_ids:
        replay = experiments.get(str(experiment_id))
        argv = replay.get("executed_argv") if isinstance(replay, Mapping) else None
        authorization = replay.get("command_authorization") if isinstance(replay, Mapping) else None
        if (
            not isinstance(argv, list)
            or not argv
            or any(not isinstance(argument, str) or not argument for argument in argv)
            or not isinstance(authorization, Mapping)
            or command_authorization_errors(authorization, argv=argv)
            or authorization.get("authorization_kind") == "attested_research_harness"
        ):
            return None
        current_identity = command_authorization_identity(authorization)
        entrypoint_path = _receipt_text(authorization.get("entrypoint_path"))
        if not isinstance(current_identity, dict) or (
            entrypoint_path is not None
            and entrypoint_path.replace("\\", "/").startswith(".usertest_research/")
        ):
            return None
        if authorization_identity is None:
            authorization_identity = current_identity
        elif authorization_identity != current_identity:
            return None
        invocations.append(
            {
                "experiment_id": experiment_id,
                "executed_argv_sha256": authorization.get("executed_argv_sha256"),
                "command_authorization_sha256": authorization.get("authorization_sha256"),
            }
        )
    change_surfaces = sorted(
        [
            {
                "path": str(touchpoint.get("path")).replace("\\", "/"),
                "symbols": sorted(
                    str(symbol)
                    for symbol in (
                        touchpoint.get("symbols")
                        if isinstance(touchpoint.get("symbols"), list)
                        else []
                    )
                ),
                "inspected_content_sha256": touchpoint.get("inspected_content_sha256"),
            }
            for touchpoint in implementation_touchpoints
            if _is_nonempty_string(touchpoint.get("touchpoint_id"))
            and _is_nonempty_string(touchpoint.get("path"))
            and touchpoint.get("runner_attested") is True
        ],
        key=lambda value: json.dumps(value, sort_keys=True, separators=(",", ":")),
    )
    if authorization_identity is None or not change_surfaces:
        return None
    entrypoint = _receipt_text(authorization_identity.get("entrypoint_path")) or (
        "command_authorization:" + _canonical_sha256(authorization_identity)
    )
    consumer_projection = {
        "kind": "runner_observed_repository_consumer",
        "entrypoint": entrypoint,
        "command_authorization_identity": authorization_identity,
        "change_surfaces": change_surfaces,
        "attestation_basis": "executed_entrypoint_and_inspected_change_surface",
        "runner_attested": True,
    }
    consumer_identity = {
        **consumer_projection,
        "consumer_identity_sha256": _canonical_sha256(consumer_projection),
    }
    intervention = proof.get("intervention")
    projection = {
        "verification_method": "runner_adapter_consumer_binding_v1",
        "consumer_identity": consumer_identity,
        "invocations": sorted(invocations, key=lambda value: str(value["experiment_id"])),
        "implementation_touchpoint_ids": sorted(
            str(touchpoint["touchpoint_id"])
            for touchpoint in implementation_touchpoints
            if _is_nonempty_string(touchpoint.get("touchpoint_id"))
        ),
        "causal_target": (
            _receipt_text(intervention.get("target")) if isinstance(intervention, Mapping) else None
        ),
        "runner_attested": True,
    }
    return {**projection, "executed_consumer_sha256": _canonical_sha256(projection)}


def _validate_typed_mechanism_evidence(
    item: dict[str, Any],
    receipt: dict[str, Any],
    *,
    pid: str,
) -> list[str]:
    """Validate runner-owned mechanism evidence without prescribing one test shape."""

    if receipt.get("status") != "verified":
        return []
    errors: list[str] = []
    evidence_raw = receipt.get("mechanism_evidence")
    evidence = evidence_raw if isinstance(evidence_raw, list) else []
    hypotheses_raw = item.get("root_cause_hypotheses")
    hypotheses = {
        str(hypothesis.get("hypothesis_id")): hypothesis
        for hypothesis in (hypotheses_raw if isinstance(hypotheses_raw, list) else [])
        if isinstance(hypothesis, dict) and _is_nonempty_string(hypothesis.get("hypothesis_id"))
    }
    primary_id = next(iter(hypotheses), None)
    experiments_raw = receipt.get("experiments")
    experiments = {
        str(experiment.get("experiment_id")): experiment
        for experiment in (experiments_raw if isinstance(experiments_raw, list) else [])
        if isinstance(experiment, dict) and _is_nonempty_string(experiment.get("experiment_id"))
    }
    symbol_receipts_raw = receipt.get("inspected_symbols")
    symbol_paths = {
        str(symbol.get("symbol")): str(symbol.get("path"))
        for symbol in (symbol_receipts_raw if isinstance(symbol_receipts_raw, list) else [])
        if isinstance(symbol, dict)
        and _is_nonempty_string(symbol.get("symbol"))
        and _is_nonempty_string(symbol.get("path"))
    }
    atom_bindings_raw = receipt.get("atom_bindings")
    atom_bindings = atom_bindings_raw if isinstance(atom_bindings_raw, list) else []
    proof_adapter_raw = receipt.get("proof_adapter_receipts")
    proof_adapter_receipts = {
        str(proof.get("proof_receipt_id")): proof
        for proof in (proof_adapter_raw if isinstance(proof_adapter_raw, list) else [])
        if isinstance(proof, dict)
        and _is_nonempty_string(proof.get("proof_receipt_id"))
        and not validate_causal_proof_receipt(proof)
    }
    projection_raw = receipt.get("verified_mechanism")
    aggregate_receipts = (
        isinstance(projection_raw, dict) and projection_raw.get("schema_version") == 3
    )
    observed_ids: set[str] = set()
    primary_evidence = False
    primary_covered_symbols: set[str] = set()
    primary_origin_entrypoint_verified = False

    def valid_link(value: Any, *, mechanism_symbols: list[Any]) -> bool:
        if not isinstance(value, dict) or not _is_nonempty_string(value.get("entrypoint")):
            return False
        code_path = value.get("code_path")
        directed_edges = value.get("verified_directed_edges")
        if isinstance(directed_edges, list):
            path_symbols = {
                step.get("symbol")
                for step in (code_path if isinstance(code_path, list) else [])
                if isinstance(step, dict)
                and _is_nonempty_string(step.get("symbol"))
                and _is_nonempty_string(step.get("path"))
            }
            return (
                path_symbols == set(mechanism_symbols)
                and all(
                    isinstance(edge, dict)
                    and edge.get("from_locator") in path_symbols
                    and edge.get("to_locator") in path_symbols
                    and edge.get("runner_attested") is True
                    and _valid_sha256(edge.get("evidence_sha256"))
                    for edge in directed_edges
                )
                and _valid_sha256(value.get("mechanism_link_sha256"))
                and _is_nonempty_string(value.get("proof_receipt_id"))
                and _is_nonempty_string(value.get("intervention_id"))
            )
        method = value.get("verification_method")
        if method == "runner_python_call_chain_v1":
            code_path = value.get("code_path")
            edges = value.get("verified_call_edges")
            return (
                isinstance(code_path, list)
                and len(code_path) >= 2
                and isinstance(edges, list)
                and len(edges) == len(code_path) - 1
                and set(mechanism_symbols).issubset(
                    {step.get("symbol") for step in code_path if isinstance(step, dict)}
                )
                and all(
                    isinstance(edge, dict)
                    and isinstance(edge.get("line"), int)
                    and not isinstance(edge.get("line"), bool)
                    and edge.get("line", 0) > 0
                    and _valid_sha256(edge.get("call_ast_sha256"))
                    for edge in edges
                )
                and _valid_sha256(value.get("mechanism_link_sha256"))
            )
        if method == "runner_harness_observable_dataflow_v1":
            symbol_sinks = value.get("symbol_sinks")
            return isinstance(symbol_sinks, list) and set(mechanism_symbols) == {
                sink.get("symbol")
                for sink in symbol_sinks
                if isinstance(sink, dict) and _is_nonempty_string(sink.get("sink"))
            }
        if method == "runner_exception_symbol_trace_v1":
            code_path = value.get("code_path")
            return isinstance(code_path, list) and set(mechanism_symbols) == {
                step.get("symbol")
                for step in code_path
                if isinstance(step, dict) and _valid_sha256(step.get("trace_excerpt_sha256"))
            }
        if method == "runner_deterministic_static_trace_v1":
            code_path = value.get("code_path")
            return (
                isinstance(code_path, list)
                and bool(code_path)
                and set(mechanism_symbols)
                == {
                    step.get("symbol")
                    for step in code_path
                    if isinstance(step, dict)
                    and _is_nonempty_string(step.get("path"))
                    and _is_nonempty_string(step.get("observation"))
                }
                and value.get("environment_dependencies") == []
                and _valid_sha256(value.get("static_trace_sha256"))
            )
        if method == "runner_falsification_shared_mechanism_v1":
            code_path = value.get("code_path")
            intervention_ids = value.get("intervention_receipt_ids")
            return (
                isinstance(code_path, list)
                and bool(code_path)
                and set(mechanism_symbols)
                == {
                    step.get("symbol")
                    for step in code_path
                    if isinstance(step, dict) and _is_nonempty_string(step.get("path"))
                }
                and isinstance(intervention_ids, list)
                and bool(intervention_ids)
                and all(_is_nonempty_string(value) for value in intervention_ids)
            )
        return False

    for index, raw in enumerate(evidence):
        if not isinstance(raw, dict):
            errors.append(f"research_mechanism_evidence_invalid: {pid}: {index}")
            continue
        evidence_id = str(raw.get("mechanism_evidence_id") or "")
        expected_id = "mechanism_evidence:" + _canonical_sha256(
            {field: value for field, value in raw.items() if field != "mechanism_evidence_id"}
        )
        hypothesis_id = str(raw.get("hypothesis_id") or "")
        hypothesis = hypotheses.get(hypothesis_id)
        mechanism_symbols = raw.get("mechanism_symbols")
        mechanism_symbol_list = mechanism_symbols if isinstance(mechanism_symbols, list) else []
        expected_symbols = (
            hypothesis.get("mechanism_symbols") if isinstance(hypothesis, dict) else None
        )
        normalized_subset = _normalized_mechanism_subset(
            mechanism_symbols,
            hypothesis_symbols=expected_symbols,
        )
        experiment_ids = raw.get("experiment_ids")
        code_paths = raw.get("code_paths")
        consumer_identity = raw.get("consumer_identity")
        origin_symptom_bindings_raw = raw.get("origin_symptom_bindings")
        origin_symptom_bindings = (
            origin_symptom_bindings_raw if isinstance(origin_symptom_bindings_raw, list) else []
        )
        origin_symptom_bindings_valid = all(
            isinstance(binding, dict)
            and binding in atom_bindings
            and binding.get("experiment_id") in (experiment_ids or [])
            and (
                binding.get("binding_role") == "symptom"
                or (
                    _is_nonempty_string(binding.get("match_kind"))
                    and "explicit_" not in str(binding.get("match_kind"))
                )
            )
            for binding in origin_symptom_bindings
        )
        claimed_symbols = normalized_subset if aggregate_receipts else expected_symbols
        evidence_type = raw.get("evidence_type")
        code_path_pairs = (
            {
                (path.get("symbol"), path.get("path"))
                for path in code_paths
                if isinstance(path, dict)
            }
            if isinstance(code_paths, list)
            else set()
        )
        adapter_targets_raw = raw.get("mechanism_targets")
        adapter_targets = adapter_targets_raw if isinstance(adapter_targets_raw, list) else []
        adapter_proof = proof_adapter_receipts.get(str(raw.get("proof_receipt_id") or ""))
        adapter_evidence = (
            adapter_proof.get("adapter_evidence") if isinstance(adapter_proof, Mapping) else None
        )
        adapter_proof_touchpoints = (
            adapter_evidence.get("implementation_touchpoints")
            if isinstance(adapter_evidence, Mapping)
            and isinstance(adapter_evidence.get("implementation_touchpoints"), list)
            else []
        )
        adapter_touchpoints = (
            raw.get("implementation_touchpoints")
            if isinstance(raw.get("implementation_touchpoints"), list)
            else []
        )
        expected_executed_consumer = (
            _expected_adapter_executed_consumer(
                adapter_proof,
                experiments=experiments,
                implementation_touchpoints=[
                    touchpoint
                    for touchpoint in adapter_touchpoints
                    if isinstance(touchpoint, Mapping)
                ],
            )
            if isinstance(adapter_proof, Mapping) and adapter_touchpoints
            else None
        )
        adapter_touchpoints_valid = (
            evidence_type == "adapter_proof"
            and code_path_pairs == {(symbol, symbol) for symbol in mechanism_symbol_list}
            and {
                target.get("locator")
                for target in adapter_targets
                if isinstance(target, dict)
                and _is_nonempty_string(target.get("node_id"))
                and _is_nonempty_string(target.get("kind"))
                and _is_nonempty_string(target.get("locator"))
                and target.get("runner_attested") is True
                and _valid_sha256(target.get("evidence_sha256"))
            }
            == set(mechanism_symbol_list)
            and isinstance(adapter_proof, Mapping)
            and adapter_touchpoints == adapter_proof_touchpoints
            and raw.get("causal_target")
            == (
                adapter_proof.get("intervention", {}).get("target")
                if isinstance(adapter_proof.get("intervention"), Mapping)
                else None
            )
            and (
                (
                    bool(adapter_touchpoints)
                    and raw.get("executed_consumer") == expected_executed_consumer
                    and isinstance(expected_executed_consumer, Mapping)
                    and consumer_identity == expected_executed_consumer.get("consumer_identity")
                )
                or (
                    not adapter_touchpoints
                    and raw.get("executed_consumer") is None
                    and consumer_identity
                    == {
                        "kind": "unresolved_consumer",
                        "entrypoint": mechanism_symbol_list[0],
                    }
                )
            )
        )
        legacy_touchpoints_valid = code_path_pairs == {
            (symbol, symbol_paths.get(symbol)) for symbol in mechanism_symbol_list
        }
        valid = (
            evidence_id == expected_id
            and evidence_id not in observed_ids
            and raw.get("evidence_type") in _VALID_MECHANISM_EVIDENCE_TYPES
            and hypothesis is not None
            and claimed_symbols is not None
            and mechanism_symbols == claimed_symbols
            and isinstance(mechanism_symbols, list)
            and bool(mechanism_symbols)
            and isinstance(experiment_ids, list)
            and bool(experiment_ids)
            and all(experiment_id in experiments for experiment_id in experiment_ids)
            and isinstance(code_paths, list)
            and bool(code_paths)
            and (adapter_touchpoints_valid or legacy_touchpoints_valid)
            and isinstance(raw.get("origin_atom_ids"), list)
            and bool(raw.get("origin_atom_ids"))
            and _is_nonempty_string(raw.get("path_name"))
            and isinstance(consumer_identity, dict)
            and _is_nonempty_string(consumer_identity.get("kind"))
            and _is_nonempty_string(consumer_identity.get("entrypoint"))
            and raw.get("path_name") == consumer_identity.get("entrypoint")
            and raw.get("independence_key") == _canonical_sha256(consumer_identity)
            and (
                _content_bound_consumer_identity_valid(consumer_identity)
                if consumer_identity.get("runner_attested") is True
                else consumer_identity.get("kind")
                in {"research_harness", "evidence_selector", "unresolved_consumer"}
            )
            and raw.get("adversarial_effect") in {"supports_selection", "limits_scope"}
            and origin_symptom_bindings_valid
            and (
                not aggregate_receipts
                or raw.get("adversarial_effect") != "supports_selection"
                or isinstance(origin_symptom_bindings_raw, list)
            )
        )
        if raw.get("evidence_type") == "controlled_scenario":
            valid = valid and len(experiment_ids if isinstance(experiment_ids, list) else []) == 2
            valid = valid and isinstance(raw.get("controlled_condition"), dict)
            valid = valid and isinstance(raw.get("observable_difference"), dict)
            valid = valid and (
                valid_link(raw.get("mechanism_link"), mechanism_symbols=mechanism_symbol_list)
                or _is_nonempty_string(raw.get("strong_pytest_control_id"))
                or _is_nonempty_string(raw.get("falsification_intervention_id"))
            )
        elif raw.get("evidence_type") == "observed_output":
            valid = valid and valid_link(
                raw.get("mechanism_link"), mechanism_symbols=mechanism_symbol_list
            )
        elif raw.get("evidence_type") == "temporary_harness":
            valid = valid and isinstance(raw.get("harness_path"), str)
            valid = valid and str(raw.get("harness_path")).startswith(".usertest_research/")
            valid = valid and valid_link(
                raw.get("mechanism_link"), mechanism_symbols=mechanism_symbol_list
            )
        elif raw.get("evidence_type") == "static_trace":
            experiment = experiments.get(str(experiment_ids[0])) if experiment_ids else None
            valid = valid and isinstance(experiment, dict)
            valid = valid and experiment.get("scenario_kind") == "static_trace"
            valid = valid and valid_link(
                raw.get("mechanism_link"), mechanism_symbols=mechanism_symbol_list
            )
        elif raw.get("evidence_type") == "exception_trace":
            valid = valid and valid_link(
                raw.get("mechanism_link"), mechanism_symbols=mechanism_symbol_list
            )
        elif raw.get("evidence_type") == "live_runtime":
            requirement = raw.get("platform_requirement")
            valid = (
                valid
                and isinstance(requirement, str)
                and requirement != "any"
                and _PLATFORM_REQUIREMENT_RE.fullmatch(requirement) is not None
                and isinstance(raw.get("observed_platform"), str)
                and requirement.casefold() == str(raw.get("observed_platform")).casefold()
            )
            valid = valid and valid_link(
                raw.get("mechanism_link"), mechanism_symbols=mechanism_symbol_list
            )
        if valid:
            observed_ids.add(evidence_id)
            if (
                hypothesis_id == primary_id
                and raw.get("adversarial_effect") == "supports_selection"
            ):
                primary_evidence = True
                primary_covered_symbols.update(mechanism_symbol_list)
                mechanism_link = raw.get("mechanism_link")
                if (
                    origin_symptom_bindings
                    and isinstance(mechanism_link, dict)
                    and _is_nonempty_string(mechanism_link.get("entrypoint"))
                ):
                    primary_origin_entrypoint_verified = True
        else:
            errors.append(f"research_mechanism_evidence_invalid: {pid}: {index}")
    if primary_id is not None and not primary_evidence:
        errors.append(f"research_primary_mechanism_evidence_missing: {pid}: {primary_id}")
    if aggregate_receipts and primary_id is not None:
        primary = hypotheses.get(primary_id, {})
        primary_symbols_raw = primary.get("mechanism_symbols")
        primary_symbols = {
            symbol.strip()
            for symbol in (primary_symbols_raw if isinstance(primary_symbols_raw, list) else [])
            if isinstance(symbol, str) and symbol.strip()
        }
        if primary_covered_symbols != primary_symbols:
            errors.append(f"research_primary_mechanism_coverage_incomplete: {pid}: {primary_id}")
        if not primary_origin_entrypoint_verified:
            errors.append(
                f"research_primary_origin_symptom_entrypoint_missing: {pid}: {primary_id}"
            )
    return errors


def _atom_snapshot_field_value(snapshot: Any, field_path: Any) -> tuple[bool, Any]:
    """Resolve the restricted immutable ``$.field[0]`` binding vocabulary."""

    if not isinstance(snapshot, dict) or not isinstance(field_path, str):
        return False, None
    if field_path == "$":
        return True, snapshot
    if not field_path.startswith("$."):
        return False, None
    current: Any = snapshot
    cursor = 1
    while cursor < len(field_path):
        if field_path[cursor] == ".":
            cursor += 1
            match = re.match(r"[A-Za-z_][A-Za-z0-9_]*", field_path[cursor:])
            if match is None or not isinstance(current, dict):
                return False, None
            key = match.group(0)
            if key not in current:
                return False, None
            current = current[key]
            cursor += len(key)
            continue
        if field_path[cursor] == "[":
            match = re.match(r"\[(\d+)\]", field_path[cursor:])
            if match is None or not isinstance(current, list):
                return False, None
            index = int(match.group(1))
            if index >= len(current):
                return False, None
            current = current[index]
            cursor += len(match.group(0))
            continue
        return False, None
    return True, current


def _valid_positive_contract_postcondition(value: Any, *, oracle_kind: str) -> bool:
    if not isinstance(value, dict):
        return False
    predicate_type = value.get("type")
    if predicate_type == "causal_proof_predicate":
        return (
            oracle_kind == "causal_proof_replay"
            and _is_nonempty_string(value.get("proof_receipt_id"))
            and _is_nonempty_string(value.get("intervention_id"))
            and _is_nonempty_string(value.get("adapter_id"))
            and _is_nonempty_string(value.get("adapter_version"))
            and not proof_predicate_contract_errors(value.get("predicate"))
            and _is_nonempty_string(value.get("observation_source"))
            and _valid_sha256(value.get("positive_basis_sha256"))
        )
    if predicate_type == "command_exit_code":
        return (
            oracle_kind == "staged_replay"
            and value.get("command_index") == 0
            and value.get("equals") == 0
        )
    if predicate_type in {
        "command_stdout_equals",
        "command_stdout_contains",
        "command_stderr_equals",
        "command_stderr_contains",
        "command_combined_equals",
        "command_combined_contains",
    }:
        return (
            oracle_kind == "staged_replay"
            and value.get("command_index") == 0
            and _is_nonempty_string(value.get("value"))
        )
    if predicate_type == "artifact_json_value":
        path = value.get("path")
        pointer = value.get("json_pointer")
        return (
            oracle_kind == "staged_replay"
            and _is_nonempty_string(path)
            and not str(path).startswith(("/", "\\"))
            and ".." not in str(path).replace("\\", "/").split("/")
            and isinstance(pointer, str)
            and (not pointer or pointer.startswith("/"))
            and "equals" in value
        )
    if predicate_type == "oracle_state_equals":
        return (
            oracle_kind == "config_state"
            and _is_nonempty_string(value.get("target_id"))
            and isinstance(value.get("exists"), bool)
            and "equals" in value
        )
    return False


def _validate_positive_outcome_contracts(
    item: dict[str, Any],
    receipt: dict[str, Any],
    oracle: dict[str, Any],
    *,
    pid: str,
    oracle_index: int,
) -> list[str]:
    raw = oracle.get("positive_outcome_contracts")
    if raw is None:
        return []
    if not isinstance(raw, list):
        return [f"research_positive_outcome_contracts_invalid: {pid}: {oracle_index}"]
    errors: list[str] = []
    evidence_by_id = {
        str(value.get("mechanism_evidence_id")): value
        for value in receipt.get("mechanism_evidence", [])
        if isinstance(value, dict) and _is_nonempty_string(value.get("mechanism_evidence_id"))
    }
    provenance_raw = receipt.get("verified_mechanism_provenance")
    provenance = provenance_raw if isinstance(provenance_raw, dict) else {}
    selected_evidence_ids = {
        value
        for value in provenance.get("mechanism_evidence_ids", [])
        if isinstance(value, str) and value
    }
    primary_hypothesis_id = _receipt_text(provenance.get("primary_hypothesis_id"))
    verified_mechanism_sha256 = _receipt_text(receipt.get("verified_mechanism_sha256"))
    verified_provenance_sha256 = _receipt_text(receipt.get("verified_mechanism_provenance_sha256"))
    inspected_by_path = {
        str(value.get("path")): value
        for value in receipt.get("inspected_files", [])
        if isinstance(value, dict) and _is_nonempty_string(value.get("path"))
    }
    inspected_symbols = {
        (str(value.get("symbol")), str(value.get("path")))
        for value in receipt.get("inspected_symbols", [])
        if isinstance(value, dict)
        and _is_nonempty_string(value.get("symbol"))
        and _is_nonempty_string(value.get("path"))
    }
    interventions_by_id = {
        str(value.get("intervention_receipt_id")): value
        for value in receipt.get("falsification_interventions", [])
        if isinstance(value, dict) and _is_nonempty_string(value.get("intervention_receipt_id"))
    }
    assignment = item.get("evidence_assignment")
    assignment = assignment if isinstance(assignment, dict) else {}
    atoms_by_id = {
        str(value.get("atom_id")): value
        for value in assignment.get("atom_receipts", [])
        if isinstance(value, dict) and _is_nonempty_string(value.get("atom_id"))
    }
    proof_receipts = {
        str(value.get("proof_receipt_id")): value
        for value in receipt.get("proof_adapter_receipts", [])
        if isinstance(value, dict)
        and _is_nonempty_string(value.get("proof_receipt_id"))
        and not validate_causal_proof_receipt(value)
    }
    observed: set[str] = set()
    for contract_index, contract in enumerate(raw):
        label = f"{pid}: {oracle_index}:{contract_index}"
        if not isinstance(contract, dict):
            errors.append(f"research_positive_outcome_contract_invalid: {label}")
            continue
        contract_id = str(contract.get("positive_outcome_contract_id") or "")
        expected_id = "positive_outcome_contract:" + _canonical_sha256(
            {key: value for key, value in contract.items() if key != "positive_outcome_contract_id"}
        )
        evidence_ids = contract.get("mechanism_evidence_ids")
        postconditions = contract.get("postconditions")
        valid = (
            contract.get("schema_version") == 1
            and contract_id == expected_id
            and contract_id not in observed
            and contract.get("research_experiment_id") == oracle.get("research_experiment_id")
            and contract.get("primary_hypothesis_id") == primary_hypothesis_id
            and contract.get("primary_verified_mechanism_sha256") == verified_mechanism_sha256
            and contract.get("primary_verified_mechanism_provenance_sha256")
            == verified_provenance_sha256
            and isinstance(evidence_ids, list)
            and bool(evidence_ids)
            and all(_is_nonempty_string(evidence_id) for evidence_id in evidence_ids)
            and set(evidence_ids).issubset(selected_evidence_ids)
            and all(evidence_id in evidence_by_id for evidence_id in evidence_ids)
            and all(
                evidence_by_id[evidence_id].get("hypothesis_id") == primary_hypothesis_id
                for evidence_id in evidence_ids
                if evidence_id in evidence_by_id
            )
            and isinstance(postconditions, list)
            and bool(postconditions)
            and all(
                _valid_positive_contract_postcondition(
                    postcondition,
                    oracle_kind=str(oracle.get("kind") or ""),
                )
                for postcondition in postconditions
            )
        )
        kind = contract.get("kind")
        if kind == "causal_proof_predicate":
            proof_id = _receipt_text(contract.get("proof_receipt_id"))
            proof = proof_receipts.get(proof_id or "")
            intervention = proof.get("intervention") if isinstance(proof, dict) else None
            observations = proof.get("observations") if isinstance(proof, dict) else None
            baseline = observations.get("baseline") if isinstance(observations, dict) else None
            challenge = observations.get("challenge") if isinstance(observations, dict) else None
            positive = proof.get("positive_outcome") if isinstance(proof, dict) else None
            source_root = proof.get("source_root") if isinstance(proof, dict) else None
            positive_basis = (
                source_root.get("positive_basis") if isinstance(source_root, dict) else None
            )
            adapter_contract = contract.get("adapter_contract")
            expected_postcondition = {
                "type": "causal_proof_predicate",
                "proof_receipt_id": proof_id,
                "intervention_id": (
                    proof.get("intervention_id") if isinstance(proof, dict) else None
                ),
                "adapter_id": proof.get("adapter_id") if isinstance(proof, dict) else None,
                "adapter_version": (
                    proof.get("adapter_version") if isinstance(proof, dict) else None
                ),
                "predicate": positive.get("predicate") if isinstance(positive, dict) else None,
                "observation_source": (
                    positive.get("observation_source") if isinstance(positive, dict) else None
                ),
                "positive_basis_sha256": (
                    positive_basis.get("basis_sha256") if isinstance(positive_basis, dict) else None
                ),
            }
            valid = valid and (
                isinstance(proof, dict)
                and isinstance(intervention, dict)
                and intervention.get("baseline_experiment_id")
                == oracle.get("research_experiment_id")
                and contract.get("intervention_id") == proof.get("intervention_id")
                and isinstance(adapter_contract, dict)
                and adapter_contract.get("adapter_id") == proof.get("adapter_id")
                and adapter_contract.get("adapter_version") == proof.get("adapter_version")
                and isinstance(baseline, dict)
                and isinstance(challenge, dict)
                and adapter_contract.get("baseline_observation_sha256")
                == baseline.get("observation_sha256")
                and adapter_contract.get("challenge_observation_sha256")
                == challenge.get("observation_sha256")
                and adapter_contract.get("adapter_evidence_sha256")
                == _canonical_sha256(proof.get("adapter_evidence"))
                and contract.get("positive_basis") == positive_basis
                and contract.get("semantic_review_required")
                is (
                    isinstance(positive_basis, dict)
                    and positive_basis.get("semantic_review_required") is True
                )
                and postconditions == [expected_postcondition]
                and all(
                    evidence_by_id[evidence_id].get("proof_receipt_id") == proof_id
                    for evidence_id in evidence_ids
                )
            )
        elif kind == "repository_test_assertion":
            repository = contract.get("repository_contract")
            source_bindings = contract.get("source_case_bindings")
            baseline_failure = contract.get("baseline_failure")
            assertions = (
                repository.get("semantic_assertions") if isinstance(repository, dict) else None
            )
            touches = repository.get("mechanism_touches") if isinstance(repository, dict) else None
            reachable_contracts = (
                repository.get("reachable_function_contracts")
                if isinstance(repository, dict)
                else None
            )
            valid = valid and (
                isinstance(repository, dict)
                and repository.get("runner") == "pytest"
                and _is_nonempty_string(repository.get("test_path"))
                and _valid_sha256(repository.get("test_file_sha256"))
                and isinstance(repository.get("test_file_git_blob_sha"), str)
                and bool(
                    re.fullmatch(
                        r"[0-9a-f]{40}|[0-9a-f]{64}",
                        str(repository.get("test_file_git_blob_sha")),
                    )
                )
                and _is_nonempty_string(repository.get("selector"))
                and _valid_sha256(repository.get("test_function_source_sha256"))
                and isinstance(reachable_contracts, list)
                and bool(reachable_contracts)
                and all(
                    isinstance(function, dict)
                    and _is_nonempty_string(function.get("function"))
                    and _valid_sha256(function.get("function_ast_sha256"))
                    for function in reachable_contracts
                )
                and _valid_sha256(repository.get("relevant_module_imports_sha256"))
                and isinstance(touches, list)
                and bool(touches)
                and isinstance(assertions, list)
                and bool(assertions)
                and all(
                    isinstance(assertion, dict)
                    and isinstance(assertion.get("line"), int)
                    and not isinstance(assertion.get("line"), bool)
                    and assertion.get("line", 0) > 0
                    and _is_nonempty_string(assertion.get("expression"))
                    and _valid_sha256(assertion.get("assertion_ast_sha256"))
                    and isinstance(assertion.get("mechanism_symbols"), list)
                    and bool(assertion.get("mechanism_symbols"))
                    for assertion in assertions
                )
                and isinstance(source_bindings, list)
                and bool(source_bindings)
                and all(
                    isinstance(binding, dict)
                    and binding in receipt.get("atom_bindings", [])
                    and isinstance(atoms_by_id.get(str(binding.get("atom_id") or "")), dict)
                    and _source_observation_atom(
                        atoms_by_id[str(binding.get("atom_id"))].get("atom_snapshot")
                    )
                    for binding in source_bindings
                )
                and isinstance(baseline_failure, dict)
                and isinstance(baseline_failure.get("exit_code"), int)
                and not isinstance(baseline_failure.get("exit_code"), bool)
                and baseline_failure.get("exit_code") != 0
                and baseline_failure.get("exit_code") == oracle.get("baseline", {}).get("exit_code")
                and _valid_sha256(baseline_failure.get("stdout_sha256"))
                and _valid_sha256(baseline_failure.get("stderr_sha256"))
                and baseline_failure.get("failure_kind") == "bound_semantic_assertion_failed"
                and baseline_failure.get("matched_assertion_ast_sha256")
                == sorted(
                    str(assertion.get("assertion_ast_sha256"))
                    for assertion in assertions
                    if isinstance(assertion, dict)
                )
                and any(
                    isinstance(postcondition, dict)
                    and postcondition.get("type") == "command_exit_code"
                    and postcondition.get("equals") == 0
                    for postcondition in postconditions
                )
            )
        elif kind == "retained_research_harness_assertion":
            research_contract = contract.get("research_assertion_contract")
            semantic_basis = contract.get("semantic_basis")
            harness_path = (
                research_contract.get("harness_path")
                if isinstance(research_contract, dict)
                else None
            )
            semantic_assertions = (
                research_contract.get("semantic_assertions")
                if isinstance(research_contract, dict)
                else None
            )
            baseline_failure = (
                research_contract.get("baseline_failure")
                if isinstance(research_contract, dict)
                else None
            )
            asset = oracle.get("asset")
            manifest = asset.get("manifest") if isinstance(asset, dict) else None
            manifest_entry = (
                manifest.get(harness_path)
                if isinstance(manifest, dict) and isinstance(harness_path, str)
                else None
            )
            provenance = (
                semantic_basis.get("provenance") if isinstance(semantic_basis, dict) else None
            )
            expected_value = (
                semantic_basis.get("expected_value")
                if isinstance(semantic_basis, dict)
                else object()
            )
            provenance_valid = False
            if isinstance(provenance, dict) and provenance.get("kind") == ("source_atom_quote"):
                atom = atoms_by_id.get(str(provenance.get("atom_id") or ""))
                found, field_value = (
                    _atom_snapshot_field_value(
                        atom.get("atom_snapshot"),
                        provenance.get("field_path"),
                    )
                    if isinstance(atom, dict)
                    else (False, None)
                )
                provenance_valid = (
                    isinstance(atom, dict)
                    and _source_observation_atom(atom.get("atom_snapshot"))
                    and _semantic_quote_field_path(provenance.get("field_path"))
                    and provenance.get("atom_sha256") == atom.get("atom_sha256")
                    and found
                    and isinstance(field_value, str)
                    and provenance.get("exact_quote") in field_value
                    and provenance.get("field_value_sha256") == _canonical_sha256(field_value)
                    and _expectation_quote(
                        provenance.get("exact_quote"),
                        expected_value=expected_value,
                    )
                )
            elif isinstance(provenance, dict) and provenance.get("kind") == (
                "repository_contract_quote"
            ):
                inspected = inspected_by_path.get(str(provenance.get("path") or ""))
                locator = provenance.get("contract_locator")
                mechanism_symbols = {
                    str(symbol)
                    for evidence_id in evidence_ids
                    for evidence_receipt in [evidence_by_id.get(str(evidence_id))]
                    if isinstance(evidence_receipt, dict)
                    for symbol in evidence_receipt.get("mechanism_symbols", [])
                    if isinstance(symbol, str)
                }
                locator_valid = False
                if isinstance(locator, dict) and locator.get("kind") == "python_symbol":
                    locator_valid = (
                        locator.get("symbol") in mechanism_symbols
                        and (
                            str(locator.get("symbol")),
                            str(provenance.get("path")),
                        )
                        in inspected_symbols
                    )
                elif isinstance(locator, dict) and locator.get("kind") == ("schema_pointer"):
                    locator_valid = (
                        _is_nonempty_string(locator.get("json_pointer"))
                        and str(locator.get("json_pointer")).startswith("/")
                        and _valid_sha256(locator.get("value_sha256"))
                    )
                elif isinstance(locator, dict) and locator.get("kind") == ("mechanism_subject"):
                    subject = locator.get("subject")
                    locator_valid = subject in (
                        mechanism_symbols
                        | {symbol.rsplit(".", 1)[-1] for symbol in mechanism_symbols}
                    ) and str(subject) in str(provenance.get("exact_quote") or "")
                provenance_valid = (
                    isinstance(inspected, dict)
                    and provenance.get("contract_type")
                    in {"api_contract", "documentation", "schema"}
                    and provenance.get("sha256") == inspected.get("sha256")
                    and provenance.get("git_blob_sha") == inspected.get("git_blob_sha")
                    and provenance.get("read_event_sha256") == inspected.get("read_event_sha256")
                    and locator_valid
                    and _expectation_quote(
                        provenance.get("exact_quote"),
                        expected_value=expected_value,
                    )
                )
            adversarial = (
                semantic_basis.get("adversarial_basis")
                if isinstance(semantic_basis, dict)
                else None
            )
            adversarial_valid = adversarial is None or (
                isinstance(adversarial, dict)
                and isinstance(
                    interventions_by_id.get(str(adversarial.get("intervention_receipt_id") or "")),
                    dict,
                )
                and interventions_by_id[str(adversarial.get("intervention_receipt_id"))].get(
                    "attempt_id"
                )
                == adversarial.get("attempt_id")
            )
            valid = valid and (
                isinstance(research_contract, dict)
                and _is_nonempty_string(harness_path)
                and str(harness_path).startswith(".usertest_research/")
                and _valid_sha256(research_contract.get("harness_sha256"))
                and isinstance(manifest_entry, dict)
                and manifest_entry.get("sha256") == research_contract.get("harness_sha256")
                and isinstance(semantic_assertions, list)
                and bool(semantic_assertions)
                and all(
                    isinstance(assertion, dict)
                    and isinstance(assertion.get("line"), int)
                    and not isinstance(assertion.get("line"), bool)
                    and assertion.get("line", 0) > 0
                    and _is_nonempty_string(assertion.get("expression"))
                    and _valid_sha256(assertion.get("assertion_ast_sha256"))
                    and isinstance(assertion.get("mechanism_symbols"), list)
                    and bool(assertion.get("mechanism_symbols"))
                    and assertion.get("expected_value_sha256") == _canonical_sha256(expected_value)
                    for assertion in semantic_assertions
                )
                and isinstance(baseline_failure, dict)
                and isinstance(baseline_failure.get("exit_code"), int)
                and not isinstance(baseline_failure.get("exit_code"), bool)
                and baseline_failure.get("exit_code") != 0
                and _valid_sha256(baseline_failure.get("stderr_sha256"))
                and baseline_failure.get("failure_kind") == "semantic_assertion_failed"
                and isinstance(semantic_basis, dict)
                and semantic_basis.get("schema_version") == 1
                and _is_nonempty_string(semantic_basis.get("semantic_rationale"))
                and semantic_basis.get("semantic_judgment")
                == "researcher_interpreted_grounded_expectation"
                and semantic_basis.get("semantic_relation")
                in {
                    "exact_expected_value",
                    "logical_correction_of_source_failure",
                    "required_operational_property",
                    "repository_contract_requirement",
                }
                and semantic_basis.get("expected_value_sha256") == _canonical_sha256(expected_value)
                and semantic_basis.get("independent_review_requirement")
                == "stage5_solution_falsification"
                and provenance_valid
                and adversarial_valid
                and any(
                    isinstance(postcondition, dict)
                    and postcondition.get("type") == "command_exit_code"
                    and postcondition.get("equals") == 0
                    for postcondition in postconditions
                )
            )
        elif kind == "origin_evidence_semantic_contract":
            origin = contract.get("origin_evidence")
            atom = (
                atoms_by_id.get(str(origin.get("atom_id") or ""))
                if isinstance(origin, dict)
                else None
            )
            found, value = (
                _atom_snapshot_field_value(
                    atom.get("atom_snapshot"),
                    origin.get("field_path"),
                )
                if isinstance(atom, dict) and isinstance(origin, dict)
                else (False, None)
            )
            semantic_values = [
                postcondition.get("value")
                if str(postcondition.get("type") or "").startswith("command_")
                and postcondition.get("type") != "command_exit_code"
                else postcondition.get("equals")
                for postcondition in postconditions
                if isinstance(postcondition, dict)
                and postcondition.get("type") != "command_exit_code"
            ]
            expected_binding = (
                next(
                    (
                        binding
                        for binding in receipt.get("atom_bindings", [])
                        if isinstance(binding, dict)
                        and binding.get("experiment_id") == oracle.get("research_experiment_id")
                        and binding.get("atom_id") == origin.get("atom_id")
                        and binding.get("binding_role") == "expected_behavior"
                        and binding.get("origin_atom_field_path") == origin.get("field_path")
                    ),
                    None,
                )
                if isinstance(origin, dict)
                else None
            )
            valid = valid and (
                isinstance(origin, dict)
                and isinstance(atom, dict)
                and _source_observation_atom(atom.get("atom_snapshot"))
                and isinstance(expected_binding, dict)
                and _expected_semantic_field_path(origin.get("field_path"))
                and origin.get("atom_sha256") == atom.get("atom_sha256")
                and found
                and origin.get("value_sha256") == _canonical_sha256(value)
                and semantic_values == [value]
            )
        else:
            valid = False
        if valid:
            observed.add(contract_id)
        else:
            errors.append(f"research_positive_outcome_contract_invalid: {label}")
    return errors


def _validate_outcome_oracles(
    item: dict[str, Any],
    receipt: dict[str, Any],
    *,
    pid: str,
) -> list[str]:
    """Validate runner-minted clean-post-merge proof contracts."""

    raw_oracles = receipt.get("outcome_oracles")
    if raw_oracles is None:
        return []
    if not isinstance(raw_oracles, list):
        return [f"research_outcome_oracles_invalid: {pid}"]
    experiments_raw = receipt.get("experiments")
    experiments = {
        str(value.get("experiment_id")): value
        for value in (experiments_raw if isinstance(experiments_raw, list) else [])
        if isinstance(value, dict) and _is_nonempty_string(value.get("experiment_id"))
    }
    mechanism_raw = receipt.get("mechanism_evidence")
    mechanism = {
        str(value.get("mechanism_evidence_id")): value
        for value in (mechanism_raw if isinstance(mechanism_raw, list) else [])
        if isinstance(value, dict) and _is_nonempty_string(value.get("mechanism_evidence_id"))
    }
    provenance_raw = receipt.get("verified_mechanism_provenance")
    provenance = provenance_raw if isinstance(provenance_raw, dict) else {}
    selected_evidence_ids = {
        value
        for value in provenance.get("mechanism_evidence_ids", [])
        if isinstance(value, str) and value
    }
    primary_hypothesis_id = _receipt_text(provenance.get("primary_hypothesis_id"))
    verified_mechanism_sha256 = _receipt_text(receipt.get("verified_mechanism_sha256"))
    verified_provenance_sha256 = _receipt_text(receipt.get("verified_mechanism_provenance_sha256"))
    errors: list[str] = []
    observed: set[str] = set()
    for index, oracle in enumerate(raw_oracles):
        if not isinstance(oracle, dict):
            errors.append(f"research_outcome_oracle_invalid: {pid}: {index}")
            continue
        oracle_id = oracle.get("outcome_oracle_id")
        expected_id = "outcome_oracle:" + _canonical_sha256(
            {key: value for key, value in oracle.items() if key != "outcome_oracle_id"}
        )
        experiment_id = oracle.get("research_experiment_id")
        experiment = experiments.get(str(experiment_id))
        evidence_ids = oracle.get("mechanism_evidence_ids")
        baseline = oracle.get("baseline")
        valid = (
            oracle.get("schema_version") == 1
            and oracle_id == expected_id
            and oracle_id not in observed
            and oracle.get("case_id") == item.get("case_id")
            and oracle.get("repo_revision") == item.get("repo_revision")
            and oracle.get("primary_hypothesis_id") == primary_hypothesis_id
            and oracle.get("primary_verified_mechanism_sha256") == verified_mechanism_sha256
            and oracle.get("primary_verified_mechanism_provenance_sha256")
            == verified_provenance_sha256
            and isinstance(experiment, dict)
            and oracle.get("scenario_kind") == experiment.get("scenario_kind")
            and isinstance(evidence_ids, list)
            and bool(evidence_ids)
            and all(_is_nonempty_string(evidence_id) for evidence_id in evidence_ids)
            and set(evidence_ids).issubset(selected_evidence_ids)
            and all(
                evidence_id in mechanism
                and mechanism[evidence_id].get("hypothesis_id") == primary_hypothesis_id
                and experiment_id in mechanism[evidence_id].get("experiment_ids", [])
                for evidence_id in evidence_ids
            )
            and isinstance(oracle.get("origin_atom_ids"), list)
            and bool(oracle.get("origin_atom_ids"))
            and isinstance(baseline, dict)
            and baseline.get("exit_code") == experiment.get("exit_code")
            and baseline.get("observable_assertion") == experiment.get("observable_assertion")
            and _valid_sha256(baseline.get("stdout_sha256"))
            and _valid_sha256(baseline.get("stderr_sha256"))
        )
        if oracle.get("kind") in {"staged_replay", "causal_proof_replay"}:
            execution = oracle.get("execution")
            asset = oracle.get("asset")
            argv = execution.get("argv") if isinstance(execution, dict) else None
            authorization = (
                execution.get("command_authorization") if isinstance(execution, dict) else None
            )
            valid = valid and (
                oracle.get("proof_scope") in {"behavioral", "adapter_causal_behavior"}
                and isinstance(execution, dict)
                and isinstance(argv, list)
                and bool(argv)
                and all(
                    _is_nonempty_string(token) for token in (argv if isinstance(argv, list) else [])
                )
                and isinstance(authorization, dict)
                and _is_nonempty_string(authorization.get("authorization_kind"))
                and authorization.get("executed_argv_sha256") == _canonical_sha256(argv)
                and authorization.get("shell") is False
                and authorization.get("workspace_confined") is True
                and execution.get("shell") is False
                and (asset is None or isinstance(asset, dict))
            )
            if oracle.get("kind") == "staged_replay":
                valid = valid and (
                    oracle.get("proof_scope") == "behavioral"
                    and experiment.get("scenario_kind")
                    in {"original_replay", "faithful_replay", "live_runtime"}
                )
            else:
                proof_ids = oracle.get("proof_receipt_ids")
                proof_index = {
                    str(proof.get("proof_receipt_id")): proof
                    for proof in receipt.get("proof_adapter_receipts", [])
                    if isinstance(proof, dict)
                    and _is_nonempty_string(proof.get("proof_receipt_id"))
                    and not validate_causal_proof_receipt(proof)
                }
                setup_receipt = (
                    execution.get("replay_setup_receipt") if isinstance(execution, dict) else None
                )
                setup_reference = (
                    execution.get("replay_setup_reference") if isinstance(execution, dict) else None
                )
                valid = valid and (
                    oracle.get("proof_scope") == "adapter_causal_behavior"
                    and isinstance(proof_ids, list)
                    and bool(proof_ids)
                    and proof_ids == sorted(set(proof_ids))
                    and all(proof_id in proof_index for proof_id in proof_ids)
                    and all(
                        isinstance(proof_index[proof_id].get("intervention"), dict)
                        and proof_index[proof_id]["intervention"].get("baseline_experiment_id")
                        == experiment_id
                        for proof_id in proof_ids
                    )
                    and isinstance(setup_receipt, dict)
                    and setup_receipt.get("runner_applied") is True
                    and setup_receipt.get("replay_setup_sha256")
                    == _canonical_sha256(
                        {
                            key: value
                            for key, value in setup_receipt.items()
                            if key != "replay_setup_sha256"
                        }
                    )
                    and isinstance(setup_reference, dict)
                    and setup_reference.get("source") == "research_experiment"
                    and setup_reference.get("experiment_id") == experiment_id
                    and _valid_sha256(setup_reference.get("replay_setup_sha256"))
                )
            if isinstance(asset, dict):
                manifest = asset.get("manifest")
                relpath = asset.get("runs_relative_path")
                valid = valid and (
                    isinstance(manifest, dict)
                    and bool(manifest)
                    and asset.get("manifest_sha256") == _canonical_sha256(manifest)
                    and asset.get("asset_id")
                    == "outcome_asset:"
                    + _canonical_sha256({"schema_version": 1, "manifest": manifest})
                    and _is_nonempty_string(relpath)
                    and not str(relpath).startswith(("/", "\\"))
                    and ".." not in str(relpath).replace("\\", "/").split("/")
                    and all(
                        isinstance(path, str)
                        and path.startswith(".usertest_research/")
                        and isinstance(entry, dict)
                        and entry.get("kind") == "file"
                        and _valid_sha256(entry.get("sha256"))
                        and isinstance(entry.get("size_bytes"), int)
                        and not isinstance(entry.get("size_bytes"), bool)
                        for path, entry in manifest.items()
                    )
                )
        elif oracle.get("kind") == "config_state":
            targets = oracle.get("state_targets")
            valid = valid and (
                oracle.get("proof_scope") == "configuration_state"
                and experiment.get("scenario_kind") == "static_trace"
                and isinstance(targets, list)
                and bool(targets)
                and all(
                    mechanism[evidence_id].get("evidence_type") == "static_trace"
                    and isinstance(mechanism[evidence_id].get("mechanism_symbols"), list)
                    and bool(mechanism[evidence_id].get("mechanism_symbols"))
                    and all(
                        isinstance(symbol, str) and symbol.startswith("config:/")
                        for symbol in mechanism[evidence_id].get("mechanism_symbols", [])
                    )
                    for evidence_id in evidence_ids
                )
            )
            for target in targets if isinstance(targets, list) else []:
                if not isinstance(target, dict):
                    valid = False
                    continue
                target_id = target.get("target_id")
                expected_target_id = "config_state:" + _canonical_sha256(
                    {key: value for key, value in target.items() if key != "target_id"}
                )
                path = target.get("path")
                pointer = target.get("json_pointer")
                valid = valid and (
                    target_id == expected_target_id
                    and _is_nonempty_string(path)
                    and not str(path).startswith(("/", "\\"))
                    and ".." not in str(path).replace("\\", "/").split("/")
                    and target.get("format") in {"json", "toml", "yaml"}
                    and isinstance(pointer, str)
                    and pointer.startswith("/")
                    and target.get("baseline_exists") is True
                    and _valid_sha256(target.get("source_file_sha256"))
                    and target.get("baseline_value_sha256")
                    == _canonical_sha256(target.get("baseline_value"))
                )
        else:
            valid = False
        if valid:
            observed.add(str(oracle_id))
        else:
            errors.append(f"research_outcome_oracle_invalid: {pid}: {index}")
        errors.extend(
            _validate_positive_outcome_contracts(
                item,
                receipt,
                oracle,
                pid=pid,
                oracle_index=index,
            )
        )
    return errors


def _validate_evidence_verification(item: dict[str, Any], *, pid: str) -> list[str]:
    """Validate the runner-owned evidence receipt attached after stage execution."""
    receipt = item.get("evidence_verification")
    if not isinstance(receipt, dict):
        return [
            f"research_dossier_invalid_evidence_verification_type: {pid}: {type(receipt).__name__}"
        ]
    errors: list[str] = []
    required = (
        "verification_method",
        "status",
        "case_id",
        "problem_id",
        "repo_revision",
        "requested_repo_ref",
        "resolved_repo_ref",
        "workspace_dir",
        "workspace_head",
        "workspace_overlay",
        "replay_isolation",
        "planning_workspace_dir",
        "planning_workspace_head",
        "planning_workspace_clean",
        "run_dir",
        "origin_atom_ids",
        "normalized_events_sha256",
        "artifacts",
        "experiments",
        "inspected_files",
        "inspected_symbols",
        "hypothesis_refs",
        "causal_links",
        "mechanism_evidence",
        "verified_mechanism",
        "verified_mechanism_sha256",
        "verified_mechanism_provenance",
        "verified_mechanism_provenance_sha256",
        "test_selections",
        "control_verifications",
        "falsification_interventions",
        "deterministic_mechanism_closures",
        "failure_paths",
        "atom_bindings",
        "claims_sha256",
        "assignment_sha256",
        "run_report_sha256",
        "receipt_sha256",
        "errors",
    )
    for field in required:
        if field not in receipt:
            errors.append(f"research_evidence_verification_missing_field: {pid}: {field}")
    if receipt.get("verification_method") != "runner_artifact_binding_v1":
        errors.append(f"research_evidence_verification_method_invalid: {pid}")
    status = receipt.get("status")
    if status not in {"verified", "failed"}:
        errors.append(f"research_evidence_verification_status_invalid: {pid}: {status!r}")
    if receipt.get("problem_id") != item.get("problem_id"):
        errors.append(f"research_evidence_verification_problem_mismatch: {pid}")
    if receipt.get("case_id") != item.get("case_id"):
        errors.append(f"research_evidence_verification_case_mismatch: {pid}")
    if receipt.get("repo_revision") != item.get("repo_revision"):
        errors.append(f"research_evidence_verification_revision_mismatch: {pid}")
    if receipt.get("claims_sha256") != research_claims_sha256(item):
        errors.append(f"research_evidence_verification_claims_hash_mismatch: {pid}")
    assignment_raw = item.get("evidence_assignment")
    assignment = assignment_raw if isinstance(assignment_raw, dict) else {}
    if receipt.get("assignment_sha256") != assignment.get("assignment_sha256"):
        errors.append(f"research_evidence_verification_assignment_hash_mismatch: {pid}")
    if not _valid_sha256(receipt.get("receipt_sha256")) or receipt.get(
        "receipt_sha256"
    ) != evidence_verification_sha256(receipt):
        errors.append(f"research_evidence_verification_receipt_hash_invalid: {pid}")
    workspace_overlay = receipt.get("workspace_overlay")
    if not isinstance(workspace_overlay, dict):
        errors.append(f"research_evidence_verification_workspace_overlay_invalid: {pid}")
    elif status == "verified":
        for field in (
            "baseline_manifest_sha256",
            "research_manifest_sha256",
            "baseline_state_sha256",
            "research_state_sha256",
            "baseline_git_index_sha256",
            "research_git_index_sha256",
            "research_overlay_manifest_sha256",
        ):
            if not _valid_sha256(workspace_overlay.get(field)):
                errors.append(
                    f"research_evidence_verification_workspace_overlay_{field}_invalid: {pid}"
                )
        if not isinstance(workspace_overlay.get("research_overlay_manifest"), dict):
            errors.append(
                f"research_evidence_verification_workspace_overlay_manifest_invalid: {pid}"
            )
        if not isinstance(workspace_overlay.get("git_index_changed"), bool):
            errors.append(
                f"research_evidence_verification_workspace_overlay_git_index_invalid: {pid}"
            )
        for field in (
            "changed_baseline_paths",
            "research_overlay_paths",
            "suspicious_extra_paths",
        ):
            overlay_paths = workspace_overlay.get(field)
            if not isinstance(overlay_paths, list) or any(
                not _is_nonempty_string(path) for path in overlay_paths
            ):
                errors.append(
                    f"research_evidence_verification_workspace_overlay_{field}_invalid: {pid}"
                )

    def valid_isolation(value: Any) -> bool:
        if not isinstance(value, dict):
            return False
        executor = value.get("executor")
        keys = value.get("sanitized_environment_keys")
        if not isinstance(keys, list) or any(not _is_nonempty_string(key) for key in keys):
            return False
        if executor == "docker":
            return (
                value.get("os_sandbox") is True
                and value.get("network") == "none"
                and value.get("trust_decision") == "explicit_image"
            )
        if executor == "trusted_host":
            return (
                value.get("os_sandbox") is False
                and value.get("network") == "not_enforced"
                and value.get("trust_decision") == "approved_local_source_root"
            )
        if executor == "platform_router":
            default = value.get("default")
            routes = value.get("routes")
            return (
                value.get("trust_decision") == "explicit_routes"
                and valid_isolation(default)
                and isinstance(routes, dict)
                and bool(routes)
                and all(valid_isolation(route) for route in routes.values())
            )
        return False

    replay_isolation = receipt.get("replay_isolation")
    if not isinstance(replay_isolation, dict):
        errors.append(f"research_evidence_verification_replay_isolation_invalid: {pid}")
    elif status == "verified":
        if not valid_isolation(replay_isolation):
            errors.append(f"research_evidence_verification_replay_isolation_invalid: {pid}")

    def experiment_isolation_allowed(value: Any) -> bool:
        if not isinstance(replay_isolation, dict) or not valid_isolation(value):
            return False
        if replay_isolation.get("executor") != "platform_router":
            return value == replay_isolation
        default = replay_isolation.get("default")
        routes = replay_isolation.get("routes")
        allowed = [default]
        if isinstance(routes, dict):
            allowed.extend(routes.values())
        return value in allowed

    receipt_errors = receipt.get("errors")
    errors.extend(
        _validate_string_list(receipt_errors, field="evidence_verification_errors", pid=pid)
    )
    for field in (
        "origin_atom_ids",
        "artifacts",
        "experiments",
        "inspected_files",
        "inspected_symbols",
        "hypothesis_refs",
        "causal_links",
        "mechanism_evidence",
        "test_selections",
        "control_verifications",
        "falsification_interventions",
        "deterministic_mechanism_closures",
        "failure_paths",
        "atom_bindings",
    ):
        if not isinstance(receipt.get(field), list):
            errors.append(f"research_evidence_verification_invalid_{field}: {pid}")

    if status == "failed" and isinstance(receipt_errors, list) and not receipt_errors:
        errors.append(f"research_evidence_verification_failed_without_error: {pid}")
    if status == "verified":
        if isinstance(receipt_errors, list) and receipt_errors:
            errors.append(f"research_evidence_verification_verified_with_errors: {pid}")
        for field in (
            "workspace_dir",
            "workspace_head",
            "planning_workspace_dir",
            "planning_workspace_head",
            "run_dir",
            "requested_repo_ref",
            "resolved_repo_ref",
        ):
            if not _is_nonempty_string(receipt.get(field)):
                errors.append(f"research_evidence_verification_verified_missing_{field}: {pid}")
        if receipt.get("planning_workspace_clean") is not True:
            errors.append(f"research_evidence_verification_planning_workspace_not_clean: {pid}")
        if receipt.get("planning_workspace_head") != item.get("repo_revision"):
            errors.append(f"research_evidence_verification_planning_revision_mismatch: {pid}")
        if receipt.get("workspace_head") != item.get("repo_revision"):
            errors.append(f"research_evidence_verification_research_revision_mismatch: {pid}")
        if not _valid_sha256(receipt.get("normalized_events_sha256")):
            errors.append(f"research_evidence_verification_normalized_events_hash_invalid: {pid}")
        if not _valid_sha256(receipt.get("run_report_sha256")):
            errors.append(f"research_evidence_verification_report_hash_invalid: {pid}")
        origin_ids = receipt.get("origin_atom_ids")
        if not isinstance(origin_ids, list) or not origin_ids:
            errors.append(f"research_evidence_verification_origin_atoms_missing: {pid}")
        expected_origin_ids_raw = assignment.get("expected_atom_ids")
        expected_origin_ids = (
            expected_origin_ids_raw if isinstance(expected_origin_ids_raw, list) else []
        )
        if isinstance(origin_ids, list) and sorted(origin_ids) != sorted(expected_origin_ids):
            errors.append(f"research_evidence_verification_origin_atoms_mismatch: {pid}")
        atom_bindings_raw = receipt.get("atom_bindings")
        atom_bindings = atom_bindings_raw if isinstance(atom_bindings_raw, list) else []
        assignment_receipts = {
            str(atom_receipt.get("atom_id")): atom_receipt
            for atom_receipt in (
                assignment.get("atom_receipts", [])
                if isinstance(assignment.get("atom_receipts"), list)
                else []
            )
            if isinstance(atom_receipt, dict) and _is_nonempty_string(atom_receipt.get("atom_id"))
        }
        explicit_binding_roles = {
            "explicit_symptom_field_binding": "symptom",
            "explicit_command_field_binding": "command",
            "explicit_corroborating_field_binding": "corroborating",
            "explicit_context_field_binding": "context",
            "explicit_expected_behavior_field_binding": "expected_behavior",
        }
        for index, binding in enumerate(atom_bindings):
            if not isinstance(binding, dict):
                errors.append(
                    f"research_evidence_verification_atom_binding_invalid: {pid}: {index}"
                )
                continue
            if binding.get("match_kind") in {
                "command_and_atom_evidence_symptom",
                "faithful_atom_evidence_symptom",
            } and not _is_nonempty_string(binding.get("origin_atom_field_path")):
                errors.append(
                    f"research_evidence_verification_atom_field_binding_missing: {pid}: {index}"
                )
            match_kind = binding.get("match_kind")
            if match_kind in explicit_binding_roles:
                atom_receipt = assignment_receipts.get(
                    str(binding.get("atom_id") or ""),
                    {},
                )
                field_path = binding.get("origin_atom_field_path")
                found, value = _atom_snapshot_field_value(
                    atom_receipt.get("atom_snapshot"),
                    field_path,
                )
                if (
                    binding.get("binding_role") != explicit_binding_roles[match_kind]
                    or not _is_nonempty_string(field_path)
                    or not _valid_sha256(binding.get("origin_atom_sha256"))
                    or binding.get("origin_atom_sha256") != atom_receipt.get("atom_sha256")
                    or not _valid_sha256(binding.get("origin_atom_value_sha256"))
                    or not found
                    or binding.get("origin_atom_value_sha256") != _canonical_sha256(value)
                ):
                    errors.append(
                        f"research_evidence_verification_explicit_atom_binding_invalid: "
                        f"{pid}: {index}"
                    )
        bound_atom_ids = {
            binding.get("atom_id")
            for binding in atom_bindings
            if isinstance(binding, dict)
            and _is_nonempty_string(binding.get("atom_id"))
            and _is_nonempty_string(binding.get("experiment_id"))
            and binding.get("match_kind")
            in {
                "command_and_exit_code",
                "command_and_artifact_symptom_text",
                "faithful_artifact_symptom_text",
                "command_and_atom_evidence_symptom",
                "faithful_atom_evidence_symptom",
                *explicit_binding_roles,
            }
        }
        if (
            item.get("research_status") == "evidence_sufficient"
            and isinstance(origin_ids, list)
            and bound_atom_ids != set(origin_ids)
        ):
            errors.append(f"research_evidence_verification_atom_binding_mismatch: {pid}")

        declared_artifacts_raw = item.get("artifact_refs")
        declared_artifacts = (
            declared_artifacts_raw if isinstance(declared_artifacts_raw, list) else []
        )
        declared_artifact_ids = {
            str(ref.get("artifact_id"))
            for ref in declared_artifacts
            if isinstance(ref, dict) and _is_nonempty_string(ref.get("artifact_id"))
        }
        receipt_artifact_ids: set[str] = set()
        receipt_artifacts_raw = receipt.get("artifacts")
        receipt_artifacts = receipt_artifacts_raw if isinstance(receipt_artifacts_raw, list) else []
        for index, artifact in enumerate(receipt_artifacts):
            if not isinstance(artifact, dict):
                errors.append(f"research_evidence_verification_artifact_invalid: {pid}: {index}")
                continue
            artifact_id = artifact.get("artifact_id")
            if not _is_nonempty_string(artifact_id):
                errors.append(f"research_evidence_verification_artifact_id_invalid: {pid}: {index}")
            else:
                receipt_artifact_ids.add(str(artifact_id))
            if not _valid_sha256(artifact.get("sha256")):
                errors.append(
                    f"research_evidence_verification_artifact_hash_invalid: {pid}: {index}"
                )
            declared_artifact = next(
                (
                    candidate
                    for candidate in declared_artifacts
                    if isinstance(candidate, dict) and candidate.get("artifact_id") == artifact_id
                ),
                None,
            )
            if isinstance(declared_artifact, dict) and (
                artifact.get("kind") != declared_artifact.get("kind")
                or artifact.get("declared_path", artifact.get("path"))
                != declared_artifact.get("path")
            ):
                errors.append(
                    f"research_evidence_verification_artifact_receipt_mismatch: {pid}: {index}"
                )
        if receipt_artifact_ids != declared_artifact_ids:
            errors.append(f"research_evidence_verification_artifact_coverage_mismatch: {pid}")

        declared_experiments_raw = item.get("experiments")
        declared_experiment_list = (
            declared_experiments_raw if isinstance(declared_experiments_raw, list) else []
        )
        declared_experiment_ids = {
            str(experiment.get("experiment_id"))
            for experiment in declared_experiment_list
            if isinstance(experiment, dict) and _is_nonempty_string(experiment.get("experiment_id"))
        }
        receipt_experiments_raw = receipt.get("experiments")
        receipt_experiments = (
            receipt_experiments_raw if isinstance(receipt_experiments_raw, list) else []
        )
        receipt_experiment_ids: set[str] = set()
        for index, experiment in enumerate(receipt_experiments):
            if not isinstance(experiment, dict):
                continue
            executed_argv = experiment.get("executed_argv")
            agent_event_index = experiment.get("agent_event_index")
            output_excerpt_hash = experiment.get("agent_output_excerpt_sha256")
            setup_receipt = experiment.get("replay_setup_receipt")
            setup_valid = setup_receipt is None or (
                isinstance(setup_receipt, dict)
                and setup_receipt.get("runner_applied") is True
                and _valid_sha256(setup_receipt.get("replay_setup_sha256"))
                and setup_receipt.get("replay_setup_sha256")
                == _canonical_sha256(
                    {
                        key: value
                        for key, value in setup_receipt.items()
                        if key != "replay_setup_sha256"
                    }
                )
            )
            transitions = experiment.get("declared_state_transitions", [])
            transitions_valid = isinstance(transitions, list) and all(
                isinstance(transition, dict)
                and transition.get("runner_attested") is True
                and _valid_sha256(transition.get("transition_sha256"))
                and transition.get("transition_sha256")
                == _canonical_sha256(
                    {key: value for key, value in transition.items() if key != "transition_sha256"}
                )
                for transition in transitions
            )
            mutations = experiment.get("post_replay_mutations")
            mutation_contract_valid = (
                isinstance(mutations, bool)
                and experiment.get("undeclared_post_replay_mutations", []) == []
                and (
                    (
                        mutations is False
                        and experiment.get("pre_replay_state_sha256")
                        == experiment.get("post_replay_state_sha256")
                    )
                    or (mutations is True and bool(transitions))
                )
            )
            valid_receipt = (
                _is_nonempty_string(experiment.get("experiment_id"))
                and isinstance(executed_argv, list)
                and bool(executed_argv)
                and all(_is_nonempty_string(arg) for arg in executed_argv)
                and isinstance(agent_event_index, int)
                and not isinstance(agent_event_index, bool)
                and agent_event_index >= 0
                and _valid_sha256(experiment.get("agent_event_sha256"))
                and (output_excerpt_hash is None or _valid_sha256(output_excerpt_hash))
                and _is_nonempty_string(experiment.get("workspace_dir"))
                and experiment.get("workspace_head") == item.get("repo_revision")
                and _valid_sha256(experiment.get("baseline_state_sha256"))
                and _valid_sha256(experiment.get("pre_replay_state_sha256"))
                and mutation_contract_valid
                and setup_valid
                and transitions_valid
                and _valid_sha256(experiment.get("overlay_manifest_sha256"))
                and experiment_isolation_allowed(experiment.get("execution_isolation"))
                and isinstance(experiment.get("execution_metadata"), dict)
                and _is_nonempty_string(experiment.get("stdout_path"))
                and _is_nonempty_string(experiment.get("stderr_path"))
                and _valid_sha256(experiment.get("stdout_sha256"))
                and _valid_sha256(experiment.get("stderr_sha256"))
                and isinstance(experiment.get("artifact_refs"), list)
                and all(_is_nonempty_string(ref) for ref in experiment.get("artifact_refs", []))
                and experiment.get("assertion_passed") is True
            )
            if valid_receipt:
                receipt_experiment_ids.add(str(experiment["experiment_id"]))
            else:
                errors.append(f"research_evidence_verification_experiment_invalid: {pid}: {index}")
        if receipt_experiment_ids != declared_experiment_ids:
            errors.append(f"research_evidence_verification_experiment_coverage_mismatch: {pid}")

        receipt_experiments_by_id = {
            str(experiment.get("experiment_id")): experiment
            for experiment in receipt_experiments
            if isinstance(experiment, dict) and _is_nonempty_string(experiment.get("experiment_id"))
        }

        declared_experiments = {
            str(experiment.get("experiment_id")): experiment
            for experiment in declared_experiment_list
            if isinstance(experiment, dict) and _is_nonempty_string(experiment.get("experiment_id"))
        }
        for index, experiment in enumerate(receipt_experiments):
            if not isinstance(experiment, dict):
                continue
            experiment_id = str(experiment.get("experiment_id") or "")
            declared = declared_experiments.get(experiment_id)
            if declared is None:
                continue
            if (
                experiment.get("command") != declared.get("command")
                or experiment.get("declared_result") != declared.get("result")
                or experiment.get("exit_code") != declared.get("exit_code")
                or experiment.get("outcome") != declared.get("outcome")
                or experiment.get("scenario_kind") != declared.get("scenario_kind")
                or experiment.get("addresses_atom_ids") != declared.get("addresses_atom_ids")
                or experiment.get("observable_assertion") != declared.get("observable_assertion")
                or experiment.get("artifact_refs") != declared.get("artifact_refs")
            ):
                errors.append(
                    f"research_evidence_verification_experiment_receipt_mismatch: {pid}: {index}"
                )

        declared_files_raw = item.get("inspected_files")
        declared_files = declared_files_raw if isinstance(declared_files_raw, list) else []
        receipt_files_raw = receipt.get("inspected_files")
        receipt_files = receipt_files_raw if isinstance(receipt_files_raw, list) else []
        if len(receipt_files) != len(declared_files) or any(
            not isinstance(file_receipt, dict)
            or not _is_nonempty_string(file_receipt.get("path"))
            or not _valid_sha256(file_receipt.get("sha256"))
            or not isinstance(file_receipt.get("git_blob_sha"), str)
            or not bool(re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", file_receipt["git_blob_sha"]))
            or not _valid_sha256(file_receipt.get("read_event_sha256"))
            or file_receipt.get("read_source") not in {"tool", "shell_command"}
            or isinstance(file_receipt.get("size_bytes"), bool)
            or not isinstance(file_receipt.get("size_bytes"), int)
            or isinstance(file_receipt.get("bytes_observed"), bool)
            or not isinstance(file_receipt.get("bytes_observed"), int)
            or file_receipt.get("bytes_observed") < 0
            or not isinstance(file_receipt.get("whole_file_observed"), bool)
            or not _valid_sha256(file_receipt.get("observed_content_sha256"))
            or isinstance(file_receipt.get("observed_start_line"), bool)
            or not isinstance(file_receipt.get("observed_start_line"), int)
            or file_receipt.get("observed_start_line") < 1
            or isinstance(file_receipt.get("observed_end_line"), bool)
            or not isinstance(file_receipt.get("observed_end_line"), int)
            or file_receipt.get("observed_end_line") < file_receipt.get("observed_start_line")
            or isinstance(file_receipt.get("read_event_index"), bool)
            or not isinstance(file_receipt.get("read_event_index"), int)
            or file_receipt.get("read_event_index") < 0
            for file_receipt in receipt_files
        ):
            errors.append(f"research_evidence_verification_file_coverage_mismatch: {pid}")
        receipt_file_paths = {
            file_receipt.get("path")
            for file_receipt in receipt_files
            if isinstance(file_receipt, dict) and _is_nonempty_string(file_receipt.get("path"))
        }
        declared_file_paths = {
            path for path in declared_files if isinstance(path, str) and path.strip()
        }
        if receipt_file_paths != declared_file_paths:
            errors.append(f"research_evidence_verification_file_path_mismatch: {pid}")
        declared_symbols_raw = item.get("inspected_symbols")
        declared_symbols = {
            symbol
            for symbol in (declared_symbols_raw if isinstance(declared_symbols_raw, list) else [])
            if isinstance(symbol, str)
        }
        receipt_symbols_raw = receipt.get("inspected_symbols")
        receipt_symbols_list = receipt_symbols_raw if isinstance(receipt_symbols_raw, list) else []
        receipt_symbols = {
            symbol.get("symbol")
            for symbol in receipt_symbols_list
            if isinstance(symbol, dict) and _is_nonempty_string(symbol.get("symbol"))
        }
        if receipt_symbols != declared_symbols:
            errors.append(f"research_evidence_verification_symbol_coverage_mismatch: {pid}")
        receipt_symbol_paths = {
            str(symbol.get("symbol")): str(symbol.get("path")).replace("\\", "/")
            for symbol in receipt_symbols_list
            if isinstance(symbol, dict)
            and _is_nonempty_string(symbol.get("symbol"))
            and _is_nonempty_string(symbol.get("path"))
        }
        if any(path not in receipt_file_paths for path in receipt_symbol_paths.values()):
            errors.append(f"research_evidence_verification_symbol_path_mismatch: {pid}")
        declared_hypotheses_raw = item.get("root_cause_hypotheses")
        declared_hypotheses = {
            hypothesis.get("hypothesis_id")
            for hypothesis in (
                declared_hypotheses_raw if isinstance(declared_hypotheses_raw, list) else []
            )
            if isinstance(hypothesis, dict) and _is_nonempty_string(hypothesis.get("hypothesis_id"))
        }
        receipt_hypotheses_raw = receipt.get("hypothesis_refs")
        receipt_hypotheses_list = (
            receipt_hypotheses_raw if isinstance(receipt_hypotheses_raw, list) else []
        )
        receipt_hypotheses = {
            hypothesis.get("hypothesis_id")
            for hypothesis in receipt_hypotheses_list
            if isinstance(hypothesis, dict) and _is_nonempty_string(hypothesis.get("hypothesis_id"))
        }
        if receipt_hypotheses != declared_hypotheses:
            errors.append(f"research_evidence_verification_hypothesis_coverage_mismatch: {pid}")
        declared_hypothesis_records = {
            str(hypothesis.get("hypothesis_id")): hypothesis
            for hypothesis in (
                declared_hypotheses_raw if isinstance(declared_hypotheses_raw, list) else []
            )
            if isinstance(hypothesis, dict) and _is_nonempty_string(hypothesis.get("hypothesis_id"))
        }
        artifact_paths = {
            str(artifact.get("artifact_id")): str(artifact.get("path")).replace("\\", "/")
            for artifact in declared_artifacts
            if isinstance(artifact, dict)
            and _is_nonempty_string(artifact.get("artifact_id"))
            and _is_nonempty_string(artifact.get("path"))
        }
        for index, hypothesis_receipt in enumerate(receipt_hypotheses_list):
            if not isinstance(hypothesis_receipt, dict):
                continue
            hypothesis_id = str(hypothesis_receipt.get("hypothesis_id") or "")
            declared_hypothesis = declared_hypothesis_records.get(hypothesis_id)
            if declared_hypothesis is None:
                continue
            mechanism_symbols = declared_hypothesis.get("mechanism_symbols")
            if (
                hypothesis_receipt.get("mechanism_symbols") != mechanism_symbols
                or hypothesis_receipt.get("disposition") != declared_hypothesis.get("disposition")
                or hypothesis_receipt.get("disposition_evidence_refs")
                != declared_hypothesis.get("disposition_evidence")
            ):
                errors.append(
                    f"research_evidence_verification_hypothesis_receipt_mismatch: {pid}: {index}"
                )
                continue
            control_links_raw = hypothesis_receipt.get("control_links")
            control_links = control_links_raw if isinstance(control_links_raw, list) else []
            expected_control_ids = {
                str(ref)
                for ref in declared_hypothesis.get("counterevidence", [])
                if isinstance(ref, str)
                and declared_experiments.get(ref, {}).get("outcome") == "refutes"
                and declared_experiments.get(ref, {}).get("scenario_kind") == "control"
            }
            observed_control_ids: set[str] = set()
            for control_link in control_links:
                if not isinstance(control_link, dict):
                    continue
                control_id = str(control_link.get("control_experiment_id") or "")
                control = declared_experiments.get(control_id, {})
                relationship_raw = control.get("control_relationship")
                relationship = relationship_raw if isinstance(relationship_raw, dict) else {}
                support_id = str(control_link.get("supports_experiment_id") or "")
                support = declared_experiments.get(support_id, {})
                valid_control_link = (
                    control_id in expected_control_ids
                    and support_id in declared_hypothesis.get("supporting_evidence", [])
                    and control_link.get("mechanism_symbols") == mechanism_symbols
                    and control_link.get("shared_atom_ids")
                    == sorted(
                        {
                            atom_id
                            for atom_id in (
                                control.get("addresses_atom_ids", [])
                                if isinstance(control.get("addresses_atom_ids"), list)
                                else []
                            )
                            if isinstance(atom_id, str)
                        }
                        & {
                            atom_id
                            for atom_id in (
                                support.get("addresses_atom_ids", [])
                                if isinstance(support.get("addresses_atom_ids"), list)
                                else []
                            )
                            if isinstance(atom_id, str)
                        }
                    )
                    and control_link.get("shared_artifact_refs")
                    == sorted(
                        {
                            artifact_id
                            for artifact_id in (
                                control.get("artifact_refs", [])
                                if isinstance(control.get("artifact_refs"), list)
                                else []
                            )
                            if isinstance(artifact_id, str)
                        }
                        & {
                            artifact_id
                            for artifact_id in (
                                support.get("artifact_refs", [])
                                if isinstance(support.get("artifact_refs"), list)
                                else []
                            )
                            if isinstance(artifact_id, str)
                        }
                    )
                    and control_link.get("controlled_variable")
                    == relationship.get("controlled_variable")
                    and control_link.get("expected_difference")
                    == relationship.get("expected_difference")
                )
                if valid_control_link:
                    observed_control_ids.add(control_id)
            if observed_control_ids != expected_control_ids:
                errors.append(
                    f"research_evidence_verification_hypothesis_control_mismatch: "
                    f"{pid}: {hypothesis_id}"
                )
            supporting_refs = declared_hypothesis.get("supporting_evidence")
            supporting_ids = supporting_refs if isinstance(supporting_refs, list) else []
            supporting_artifact_paths = {
                artifact_paths[artifact_id]
                for experiment_id in supporting_ids
                if isinstance(experiment_id, str)
                and declared_experiments.get(experiment_id, {}).get("outcome") == "supports"
                and isinstance(
                    declared_experiments.get(experiment_id, {}).get("scenario_kind"),
                    str,
                )
                and declared_experiments.get(experiment_id, {}).get("scenario_kind")
                in {"original_replay", "faithful_replay", "static_trace", "live_runtime"}
                for artifact_id in (
                    declared_experiments.get(experiment_id, {}).get("artifact_refs", [])
                    if isinstance(
                        declared_experiments.get(experiment_id, {}).get("artifact_refs"),
                        list,
                    )
                    else []
                )
                if isinstance(artifact_id, str) and artifact_id in artifact_paths
            }
            if supporting_artifact_paths and any(
                receipt_symbol_paths.get(symbol) not in supporting_artifact_paths
                for symbol in (mechanism_symbols if isinstance(mechanism_symbols, list) else [])
                if isinstance(symbol, str)
            ):
                errors.append(
                    f"research_evidence_verification_mechanism_source_unbound: "
                    f"{pid}: {hypothesis_id}"
                )
        causal_links_raw = receipt.get("causal_links")
        causal_links = causal_links_raw if isinstance(causal_links_raw, list) else []
        expected_causal_pairs = {
            (str(hypothesis_id), str(symbol))
            for hypothesis_id, hypothesis in declared_hypothesis_records.items()
            for symbol in (
                hypothesis.get("mechanism_symbols", [])
                if isinstance(hypothesis.get("mechanism_symbols"), list)
                else []
            )
            if _is_nonempty_string(symbol)
        }
        observed_causal_pairs: set[tuple[str, str]] = set()
        for index, link in enumerate(causal_links):
            if not isinstance(link, dict):
                errors.append(f"research_evidence_verification_causal_link_invalid: {pid}: {index}")
                continue
            hypothesis_id = str(link.get("hypothesis_id") or "")
            experiment_id = str(link.get("experiment_id") or "")
            symbol = str(link.get("symbol") or "")
            declared_hypothesis = declared_hypothesis_records.get(hypothesis_id, {})
            supporting = declared_hypothesis.get("supporting_evidence", [])
            experiment = declared_experiments.get(experiment_id, {})
            receipt_experiment = receipt_experiments_by_id.get(experiment_id, {})
            valid_link = (
                (hypothesis_id, symbol) in expected_causal_pairs
                and experiment_id in supporting
                and experiment.get("outcome") == "supports"
                and isinstance(experiment.get("scenario_kind"), str)
                and experiment.get("scenario_kind")
                in {"original_replay", "faithful_replay", "live_runtime"}
                and link.get("path") == receipt_symbol_paths.get(symbol)
                and isinstance(link.get("stream"), str)
                and link.get("stream") in {"stdout", "stderr"}
                and isinstance(link.get("trace_kind"), str)
                and link.get("trace_kind")
                in {
                    "python_traceback",
                    "pytest_traceback",
                    "node_stack",
                    "python_import_error",
                }
                and _valid_sha256(link.get("trace_excerpt_sha256"))
                and _valid_sha256(link.get("stream_sha256"))
                and not replay_invocation_references_model_overlay(
                    experiment.get("command"),
                    receipt_experiment.get("executed_argv"),
                )
            )
            if valid_link:
                observed_causal_pairs.add((hypothesis_id, symbol))
            else:
                errors.append(f"research_evidence_verification_causal_link_invalid: {pid}: {index}")
        # Tracebacks are one strong evidence mode, not a universal requirement.
        # Typed mechanism evidence below covers wrong-output, harness, static,
        # controlled, and live-runtime proofs that legitimately have no frame.
    errors.extend(_validate_typed_mechanism_evidence(item, receipt, pid=pid))
    errors.extend(_validate_outcome_oracles(item, receipt, pid=pid))
    errors.extend(_validate_falsification_interventions(item, receipt, pid=pid))
    errors.extend(_validate_deterministic_mechanism_closures(item, receipt, pid=pid))
    errors.extend(_validate_verified_mechanism_projection(item, receipt, pid=pid))
    if receipt.get("control_verifications") or receipt.get("test_selections"):
        errors.extend(_validate_causal_control_verification(item, receipt, pid=pid))
    return errors


def _validate_material_unknowns(value: Any, *, pid: str) -> list[str]:
    """Validate material unknowns and the decisions each unknown can affect."""
    if not isinstance(value, list):
        return [f"research_dossier_invalid_material_unknowns_type: {pid}: {type(value).__name__}"]
    errors: list[str] = []
    for idx, unknown in enumerate(value):
        if not isinstance(unknown, dict):
            errors.append(
                f"research_dossier_invalid_material_unknown: {pid}: index={idx} "
                f"type={type(unknown).__name__}"
            )
            continue
        if not _is_nonempty_string(unknown.get("unknown")):
            errors.append(f"research_dossier_invalid_material_unknown_text: {pid}: index={idx}")
        if not _is_nonempty_string(unknown.get("evidence_needed")):
            errors.append(
                f"research_dossier_invalid_material_unknown_evidence_needed: {pid}: index={idx}"
            )
        hypothesis_id = unknown.get("hypothesis_id")
        if hypothesis_id is not None and not _is_nonempty_string(hypothesis_id):
            errors.append(
                f"research_dossier_invalid_material_unknown_hypothesis_id: {pid}: index={idx}"
            )
        affects = unknown.get("affects")
        errors.extend(
            _validate_string_list(
                affects,
                field=f"material_unknowns_{idx}_affects",
                pid=pid,
                require_nonempty=True,
            )
        )
        material = unknown.get("material")
        if material is not None and not isinstance(material, bool):
            errors.append(
                f"research_dossier_invalid_material_unknown_materiality: {pid}: index={idx}"
            )
    return errors


def _validate_legacy_research_dossier(item: dict[str, Any]) -> list[str]:
    """Return compatibility warnings for a historical stage-3 dossier."""
    warnings: list[str] = []
    pid = str(item.get("problem_id") or "(no problem_id)")
    for field in _LEGACY_RESEARCH_DOSSIER_REQUIRED:
        if field not in item:
            warnings.append(f"legacy_research_dossier_missing_required_field: {pid}: {field}")
    if item.get("implementation_performed") is True:
        raise ValueError(
            f"research_dossier_implementation_performed_true: {pid}: "
            "implementation_performed must be false; stage 3 is research-only"
        )
    repro = item.get("reproduction_status")
    if repro is not None and (
        not isinstance(repro, str) or repro not in _VALID_REPRODUCTION_STATUSES
    ):
        warnings.append(f"legacy_research_dossier_invalid_reproduction_status: {pid}: {repro!r}")
    return warnings


def _post_research_bundle_members(item: Mapping[str, Any]) -> list[dict[str, Any]]:
    bundle = item.get("post_research_same_mechanism_bundle")
    members = bundle.get("member_research_dossiers") if isinstance(bundle, Mapping) else None
    return (
        [dict(value) for value in members if isinstance(value, Mapping)]
        if isinstance(members, list)
        else []
    )


def _validate_post_research_same_mechanism_bundle(
    item: dict[str, Any],
    *,
    pid: str,
) -> list[str]:
    raw = item.get("post_research_same_mechanism_bundle")
    if raw is None:
        return []
    if not isinstance(raw, dict):
        return [f"research_post_relation_bundle_invalid_type: {pid}"]
    errors: list[str] = []
    supplied_hash = raw.get("bundle_sha256")
    projection = {key: value for key, value in raw.items() if key != "bundle_sha256"}
    if supplied_hash != _canonical_sha256(projection):
        errors.append(f"research_post_relation_bundle_hash_invalid: {pid}")
    required = {
        "schema_version",
        "canonical_case_id",
        "canonical_problem_id",
        "verified_mechanism_sha256",
        "verified_causal_signature_sha256",
        "repo_revision",
        "member_case_ids",
        "member_problem_ids",
        "research_proof_refs",
        "outcome_oracle_ids",
        "member_research_dossiers",
        "bundle_sha256",
    }
    if set(raw) != required or raw.get("schema_version") != 1:
        errors.append(f"research_post_relation_bundle_schema_invalid: {pid}")
    if (
        raw.get("canonical_case_id") != item.get("case_id")
        or raw.get("canonical_problem_id") != item.get("problem_id")
        or raw.get("repo_revision") != item.get("repo_revision")
        or not _valid_sha256(raw.get("verified_mechanism_sha256"))
        or not _valid_sha256(raw.get("verified_causal_signature_sha256"))
    ):
        errors.append(f"research_post_relation_bundle_identity_invalid: {pid}")
    members_raw = raw.get("member_research_dossiers")
    members = members_raw if isinstance(members_raw, list) else []
    if len(members) < 2 or any(not isinstance(value, dict) for value in members):
        errors.append(f"research_post_relation_bundle_members_invalid: {pid}")
        return errors
    case_ids = [str(value.get("case_id") or "") for value in members]
    problem_ids = [str(value.get("problem_id") or "") for value in members]
    if case_ids != raw.get("member_case_ids") or problem_ids != raw.get("member_problem_ids"):
        errors.append(f"research_post_relation_bundle_member_identity_invalid: {pid}")
    proof_refs = [
        {
            "case_id": value.get("case_id"),
            "problem_id": value.get("problem_id"),
            "repo_revision": value.get("repo_revision"),
            "evidence_verification_receipt_sha256": (
                value.get("evidence_verification", {}).get("receipt_sha256")
                if isinstance(value.get("evidence_verification"), dict)
                else None
            ),
        }
        for value in members
    ]
    if proof_refs != raw.get("research_proof_refs"):
        errors.append(f"research_post_relation_bundle_proof_refs_invalid: {pid}")
    outcome_oracle_ids = sorted(
        str(oracle.get("outcome_oracle_id"))
        for member in members
        for receipt in [member.get("evidence_verification")]
        if isinstance(receipt, dict)
        for oracle in (
            receipt.get("outcome_oracles")
            if isinstance(receipt.get("outcome_oracles"), list)
            else []
        )
        if isinstance(oracle, dict) and _is_nonempty_string(oracle.get("outcome_oracle_id"))
    )
    if outcome_oracle_ids != raw.get("outcome_oracle_ids"):
        errors.append(f"research_post_relation_bundle_oracles_invalid: {pid}")
    for index, member in enumerate(members):
        if "post_research_same_mechanism_bundle" in member:
            errors.append(f"research_post_relation_bundle_nested: {pid}: {index}")
            continue
        if member.get("repo_revision") != item.get("repo_revision"):
            errors.append(f"research_post_relation_bundle_revision_mismatch: {pid}: {index}")
        receipt = member.get("evidence_verification")
        if not isinstance(receipt, dict) or receipt.get("verified_mechanism_sha256") != raw.get(
            "verified_mechanism_sha256"
        ):
            errors.append(f"research_post_relation_bundle_mechanism_mismatch: {pid}: {index}")
        member_errors = _validate_research_dossier(member)
        errors.extend(
            f"research_post_relation_bundle_member_invalid: {pid}: {index}: {error}"
            for error in member_errors
        )
    return errors


def _validate_research_dossier(
    item: dict[str, Any],
    *,
    include_runner_contract: bool = True,
) -> list[str]:
    """Return hard validation errors for a current research proof.

    A valid proof can still be insufficient or blocked. Those are first-class
    research outcomes and are assessed for ticket readiness separately by
    :func:`assess_research_readiness`.
    """
    errors: list[str] = []
    pid = str(item.get("problem_id") or "(no problem_id)")

    unknown_fields = sorted(set(item) - _RESEARCH_DOSSIER_ALLOWED)
    if unknown_fields:
        errors.append(f"research_dossier_unknown_fields: {pid}: {unknown_fields!r}")

    required_fields = (
        _RESEARCH_DOSSIER_REQUIRED if include_runner_contract else _RESEARCH_DOSSIER_OUTPUT_REQUIRED
    )
    for field in required_fields:
        if field not in item:
            errors.append(f"research_dossier_missing_required_field: {pid}: {field}")

    if include_runner_contract:
        version = item.get("research_schema_version")
        if version != RESEARCH_PROOF_SCHEMA_VERSION:
            errors.append(
                f"research_dossier_invalid_schema_version: {pid}: {version!r} "
                f"(expected {RESEARCH_PROOF_SCHEMA_VERSION})"
            )
    for field in ("case_id", "problem_id"):
        if not _is_nonempty_string(item.get(field)):
            errors.append(f"research_dossier_invalid_{field}: {pid}")
    if include_runner_contract and not _is_nonempty_string(item.get("repo_revision")):
        errors.append(f"research_dossier_invalid_repo_revision: {pid}")

    impl_performed = item.get("implementation_performed")
    if impl_performed is True:
        raise ValueError(
            f"research_dossier_implementation_performed_true: {pid}: "
            "implementation_performed must be false; stage 3 is research-only"
        )
    if impl_performed is not False:
        errors.append(
            f"research_dossier_invalid_implementation_performed: {pid}: "
            f"{impl_performed!r} (must be false)"
        )

    method = item.get("research_method")
    if not _is_nonempty_string(method):
        errors.append(f"research_dossier_invalid_research_method: {pid}: {method!r}")
    repro = item.get("reproduction_status")
    if not isinstance(repro, str) or repro not in _VALID_REPRODUCTION_STATUSES:
        errors.append(f"research_dossier_invalid_reproduction_status: {pid}: {repro!r}")
    status = item.get("research_status")
    if not isinstance(status, str) or status not in _VALID_RESEARCH_STATUSES:
        errors.append(f"research_dossier_invalid_research_status: {pid}: {status!r}")

    writes_used = item.get("writes_used")
    if not isinstance(writes_used, bool):
        errors.append(
            f"research_dossier_invalid_writes_used_type: {pid}: {type(writes_used).__name__}"
        )
    errors.extend(
        _validate_string_list(
            item.get("writes_purpose"),
            field="writes_purpose",
            pid=pid,
            require_nonempty=True,
        )
    )

    broader = item.get("broader_class_assessment")
    if not isinstance(broader, str) or broader not in _VALID_BROADER_CLASS:
        errors.append(f"research_dossier_invalid_broader_class_assessment: {pid}: {broader!r}")
    if include_runner_contract:
        diff_cls = item.get("diff_classification")
        if not isinstance(diff_cls, str) or diff_cls not in _VALID_DIFF_CLASSIFICATIONS:
            errors.append(f"research_dossier_invalid_diff_classification: {pid}: {diff_cls!r}")

    errors.extend(_validate_artifact_refs(item.get("artifact_refs"), pid=pid))
    errors.extend(_validate_experiments(item.get("experiments"), pid=pid))
    errors.extend(
        _validate_string_list(item.get("inspected_files"), field="inspected_files", pid=pid)
    )
    errors.extend(
        _validate_string_list(item.get("inspected_symbols"), field="inspected_symbols", pid=pid)
    )
    errors.extend(_validate_hypotheses(item.get("root_cause_hypotheses"), pid=pid))
    errors.extend(_validate_research_evidence_links(item, pid=pid))
    confidence = item.get("root_cause_confidence")
    if (
        isinstance(confidence, bool)
        or not isinstance(confidence, (int, float))
        or not 0.0 <= float(confidence) <= 1.0
    ):
        errors.append(f"research_dossier_invalid_root_cause_confidence: {pid}: {confidence!r}")
    errors.extend(_validate_material_unknowns(item.get("material_unknowns"), pid=pid))
    errors.extend(
        _validate_string_list(item.get("blocking_reasons"), field="blocking_reasons", pid=pid)
    )
    errors.extend(
        _validate_string_list(item.get("evidence_boundaries"), field="evidence_boundaries", pid=pid)
    )
    if include_runner_contract:
        errors.extend(_validate_evidence_assignment(item, pid=pid))
        errors.extend(_validate_evidence_verification(item, pid=pid))
        errors.extend(_validate_post_research_same_mechanism_bundle(item, pid=pid))
        errors.extend(_validate_research_attempts(item.get("research_attempts"), pid=pid))

    blocking_reasons = item.get("blocking_reasons")
    if status == "blocked" and isinstance(blocking_reasons, list) and not blocking_reasons:
        errors.append(f"research_dossier_blocked_without_reason: {pid}")
    if status == "evidence_sufficient":
        if method == "reproduction" and repro != "reproduced":
            errors.append(f"research_dossier_sufficient_without_reproduction: {pid}: {repro!r}")
        if isinstance(repro, str) and repro in {"partial", "blocked"}:
            errors.append(
                f"research_dossier_sufficient_with_incomplete_reproduction: {pid}: {repro!r}"
            )

    return errors


def research_dossier_output_contract_errors(
    item: dict[str, Any],
    *,
    evidence_assignment: dict[str, Any] | None = None,
) -> list[str]:
    """Return model-output contract errors without judging runner-owned receipts.

    The stage-3 runner uses this boundary to decide whether one bounded full
    research retry is appropriate.  Evidence-assignment and verification
    failures are deliberately excluded: they are evidence failures, not model
    output-format failures, and must never be papered over by a formatting retry.
    """
    candidate = dict(item)
    if evidence_assignment is not None:
        candidate["evidence_assignment"] = evidence_assignment
    elif not isinstance(candidate.get("evidence_assignment"), dict):
        addressed_atom_ids = sorted(
            {
                atom_id
                for experiment in candidate.get("experiments", [])
                if isinstance(experiment, dict)
                for atom_id in experiment.get("addresses_atom_ids", [])
                if isinstance(atom_id, str) and atom_id.strip()
            }
        )
        candidate["evidence_assignment"] = {
            "status": "model_output_only",
            "expected_atom_ids": addressed_atom_ids,
        }
    return _validate_research_dossier(candidate, include_runner_contract=False)


def _verified_connected_adapter_touchpoint_locators(
    item: Mapping[str, Any],
    *,
    hypothesis_id: str,
    mechanism_symbols: list[str],
) -> set[str]:
    """Return causal locators joined to runner-observed repository touchpoints.

    A generic causal locator is not itself a repository change target.  This
    projection accepts it only when the same valid causal-proof receipt is carried
    into typed mechanism evidence and its content-addressed touchpoint is bound to
    a runner-observed file (and to every symbol it names).  Empty touchpoint symbol
    lists are intentional for file/config/schema/template/asset/platform routes.
    """

    verification = item.get("evidence_verification")
    if not isinstance(verification, Mapping) or verification.get("status") != "verified":
        return set()

    inspected_files: dict[str, Mapping[str, Any]] = {}
    files_raw = verification.get("inspected_files")
    for receipt in files_raw if isinstance(files_raw, list) else []:
        if not isinstance(receipt, Mapping) or not _is_nonempty_string(receipt.get("path")):
            continue
        path = str(receipt["path"]).replace("\\", "/").removeprefix("./")
        inspected_files[path] = receipt
    inspected_symbols = {
        (
            str(receipt.get("path")).replace("\\", "/").removeprefix("./"),
            str(receipt.get("symbol")),
        )
        for receipt in (
            verification.get("inspected_symbols")
            if isinstance(verification.get("inspected_symbols"), list)
            else []
        )
        if isinstance(receipt, Mapping)
        and _is_nonempty_string(receipt.get("path"))
        and _is_nonempty_string(receipt.get("symbol"))
    }
    proof_receipts = {
        str(receipt.get("proof_receipt_id")): receipt
        for receipt in (
            verification.get("proof_adapter_receipts")
            if isinstance(verification.get("proof_adapter_receipts"), list)
            else []
        )
        if isinstance(receipt, Mapping)
        and _is_nonempty_string(receipt.get("proof_receipt_id"))
        and not validate_causal_proof_receipt(receipt)
    }

    connected: set[str] = set()
    evidence_raw = verification.get("mechanism_evidence")
    for evidence in evidence_raw if isinstance(evidence_raw, list) else []:
        if not isinstance(evidence, Mapping):
            continue
        evidence_id = evidence.get("mechanism_evidence_id")
        expected_evidence_id = "mechanism_evidence:" + _canonical_sha256(
            {key: value for key, value in evidence.items() if key != "mechanism_evidence_id"}
        )
        proof = proof_receipts.get(str(evidence.get("proof_receipt_id") or ""))
        if (
            evidence.get("evidence_type") != "adapter_proof"
            or evidence.get("hypothesis_id") != hypothesis_id
            or evidence.get("mechanism_symbols") != mechanism_symbols
            or evidence_id != expected_evidence_id
            or not isinstance(proof, Mapping)
            or proof.get("hypothesis_id") != hypothesis_id
        ):
            continue
        intervention = proof.get("intervention")
        target = (
            str(intervention.get("target"))
            if isinstance(intervention, Mapping) and _is_nonempty_string(intervention.get("target"))
            else None
        )
        graph = proof.get("mechanism_graph")
        proof_targets = {
            str(node.get("locator"))
            for node in (
                graph.get("nodes")
                if isinstance(graph, Mapping) and isinstance(graph.get("nodes"), list)
                else []
            )
            if isinstance(node, Mapping)
            and node.get("runner_attested") is True
            and _is_nonempty_string(node.get("locator"))
            and _valid_sha256(node.get("evidence_sha256"))
        }
        mechanism_targets = {
            str(node.get("locator"))
            for node in (
                evidence.get("mechanism_targets")
                if isinstance(evidence.get("mechanism_targets"), list)
                else []
            )
            if isinstance(node, Mapping)
            and node.get("runner_attested") is True
            and _is_nonempty_string(node.get("locator"))
            and _valid_sha256(node.get("evidence_sha256"))
        }
        intervention_targets = {
            str(value.get("target"))
            for value in (
                evidence.get("intervention_targets")
                if isinstance(evidence.get("intervention_targets"), list)
                else []
            )
            if isinstance(value, Mapping)
            and value.get("intervention_id") == proof.get("intervention_id")
            and _is_nonempty_string(value.get("kind"))
            and _is_nonempty_string(value.get("target"))
        }
        adapter_evidence = proof.get("adapter_evidence")
        proof_touchpoints = (
            adapter_evidence.get("implementation_touchpoints")
            if isinstance(adapter_evidence, Mapping)
            else None
        )
        evidence_touchpoints = evidence.get("implementation_touchpoints")
        if (
            target is None
            or target not in mechanism_symbols
            or target not in proof_targets
            or target not in mechanism_targets
            or target not in intervention_targets
            or not isinstance(proof_touchpoints, list)
            or not proof_touchpoints
            or evidence_touchpoints != proof_touchpoints
        ):
            continue
        for touchpoint in proof_touchpoints:
            if not isinstance(touchpoint, Mapping):
                continue
            path = (
                str(touchpoint.get("path")).replace("\\", "/").removeprefix("./")
                if _is_nonempty_string(touchpoint.get("path"))
                else None
            )
            symbols = touchpoint.get("symbols")
            file_receipt = inspected_files.get(path or "")
            inspected_content_sha256 = (
                _receipt_text(file_receipt.get("observed_content_sha256"))
                if isinstance(file_receipt, Mapping)
                else None
            )
            if (
                inspected_content_sha256 is None
                and isinstance(file_receipt, Mapping)
                and file_receipt.get("whole_file_observed") is True
            ):
                inspected_content_sha256 = _receipt_text(file_receipt.get("sha256"))
            projection = {
                key: value
                for key, value in touchpoint.items()
                if key not in {"touchpoint_id", "evidence_sha256"}
            }
            expected_hash = _canonical_sha256(projection)
            if (
                touchpoint.get("touchpoint_id") != f"implementation_touchpoint:{expected_hash}"
                or touchpoint.get("evidence_sha256") != expected_hash
                or touchpoint.get("runner_attested") is not True
                or touchpoint.get("causal_locator") != target
                or path is None
                or not isinstance(symbols, list)
                or any(not _is_nonempty_string(symbol) for symbol in symbols)
                or len(symbols) != len(set(symbols))
                or not _is_nonempty_string(touchpoint.get("relationship"))
                or not isinstance(file_receipt, Mapping)
                or touchpoint.get("inspected_content_sha256") != inspected_content_sha256
                or any((path, str(symbol)) not in inspected_symbols for symbol in symbols)
            ):
                continue
            connected.add(target)
    return connected


def assess_research_readiness(item: dict[str, Any] | None) -> tuple[bool, list[str]]:
    """Assess whether a research proof can advance to a ready implementation ticket.

    The function never treats legacy or malformed data as success. It returns
    machine-readable reason codes so ticket assembly and policy can preserve why
    an otherwise planned item remains in research.
    """
    if not isinstance(item, dict):
        return False, ["research_proof_missing"]

    validation_errors = _validate_research_dossier(item)
    if validation_errors:
        return False, ["research_proof_invalid", *validation_errors]

    reasons: list[str] = []
    status = item.get("research_status")
    if status != "evidence_sufficient":
        reasons.append(f"research_status_{status}")
    blocking_reasons = item.get("blocking_reasons")
    if isinstance(blocking_reasons, list) and blocking_reasons:
        reasons.append("research_blocking_reasons_present")

    repro = item.get("reproduction_status")
    if isinstance(repro, str) and repro in {"partial", "blocked"}:
        reasons.append(f"reproduction_{repro}")

    repo_revision = str(item.get("repo_revision") or "").strip().lower()
    if not repo_revision or repo_revision.startswith("unavailable"):
        reasons.append("repo_revision_unavailable")

    verification_raw = item.get("evidence_verification")
    verification = verification_raw if isinstance(verification_raw, dict) else {}
    outcome_oracles = verification.get("outcome_oracles")
    provenance_raw = verification.get("verified_mechanism_provenance")
    provenance = provenance_raw if isinstance(provenance_raw, dict) else {}
    selected_evidence_ids = {
        value
        for value in provenance.get("mechanism_evidence_ids", [])
        if isinstance(value, str) and value
    }
    root_evidence_ids = {
        value
        for value in provenance.get("causal_root_evidence_ids", [])
        if isinstance(value, str) and value
    }
    primary_hypothesis_id = _receipt_text(provenance.get("primary_hypothesis_id"))
    mechanism_sha256 = _receipt_text(verification.get("verified_mechanism_sha256"))
    provenance_sha256 = _receipt_text(verification.get("verified_mechanism_provenance_sha256"))
    if not isinstance(outcome_oracles, list) or not outcome_oracles:
        reasons.append("research_post_change_outcome_oracle_missing")
    else:
        qualifying_contracts = [
            contract
            for oracle in outcome_oracles
            if isinstance(oracle, dict)
            and oracle.get("primary_hypothesis_id") == primary_hypothesis_id
            and oracle.get("primary_verified_mechanism_sha256") == mechanism_sha256
            and oracle.get("primary_verified_mechanism_provenance_sha256") == provenance_sha256
            for contract in (
                oracle.get("positive_outcome_contracts")
                if isinstance(oracle.get("positive_outcome_contracts"), list)
                else []
            )
            if isinstance(contract, dict)
            and contract.get("primary_hypothesis_id") == primary_hypothesis_id
            and contract.get("primary_verified_mechanism_sha256") == mechanism_sha256
            and contract.get("primary_verified_mechanism_provenance_sha256") == provenance_sha256
            and isinstance(contract.get("mechanism_evidence_ids"), list)
            and bool(contract.get("mechanism_evidence_ids"))
            and {
                evidence_id
                for evidence_id in contract.get("mechanism_evidence_ids", [])
                if isinstance(evidence_id, str) and evidence_id
            }.issubset(selected_evidence_ids)
        ]
        if not qualifying_contracts:
            reasons.append("research_positive_outcome_contract_missing")
        elif not any(
            {
                evidence_id
                for evidence_id in contract.get("mechanism_evidence_ids", [])
                if isinstance(evidence_id, str) and evidence_id
            }
            & root_evidence_ids
            for contract in qualifying_contracts
        ):
            reasons.append("research_primary_root_outcome_contract_missing")

    artifact_refs = item.get("artifact_refs")
    if not isinstance(artifact_refs, list) or not artifact_refs:
        reasons.append("artifact_evidence_missing")
    experiments = item.get("experiments")
    if not isinstance(experiments, list) or not experiments:
        reasons.append("experiment_evidence_missing")
    hypotheses = item.get("root_cause_hypotheses")
    if not isinstance(hypotheses, list) or not hypotheses:
        reasons.append("root_cause_hypothesis_missing")
    elif not any(
        isinstance(hypothesis, dict) and bool(hypothesis.get("supporting_evidence"))
        for hypothesis in hypotheses
    ):
        reasons.append("root_cause_supporting_evidence_missing")
    if isinstance(hypotheses, list) and hypotheses:
        primary = hypotheses[0] if isinstance(hypotheses[0], dict) else {}
        primary_id = str(primary.get("hypothesis_id") or "")
        declared_attempts = primary.get("falsification_attempts")
        if not isinstance(declared_attempts, list) or not declared_attempts:
            deterministic_closures = verified_deterministic_mechanism_closures(
                item,
                hypothesis_id=primary_id,
            )
            if not deterministic_closures:
                reasons.append("primary_hypothesis_falsification_or_deterministic_closure_missing")
        else:
            verified_attempts = verified_hypothesis_falsification_attempts(
                item,
                hypothesis_id=primary_id,
            )
            outcomes = {
                str(attempt.get("outcome"))
                for attempt in verified_attempts
                if isinstance(attempt, dict)
            }
            if "disproved" in outcomes:
                reasons.append("primary_hypothesis_disproved_by_replayed_challenge")
            if "survived" not in outcomes:
                reasons.append("primary_hypothesis_missing_survived_replayed_challenge")
        unresolved_alternative_ids = {
            str(hypothesis.get("hypothesis_id"))
            for hypothesis in hypotheses[1:]
            if isinstance(hypothesis, dict)
            and isinstance(hypothesis.get("disposition"), str)
            and hypothesis.get("disposition") in {"plausible", "unresolved"}
            and _is_nonempty_string(hypothesis.get("hypothesis_id"))
        }
        if unresolved_alternative_ids:
            unknown_hypothesis_ids = {
                str(unknown.get("hypothesis_id"))
                for unknown in (
                    item.get("material_unknowns")
                    if isinstance(item.get("material_unknowns"), list)
                    else []
                )
                if isinstance(unknown, dict) and _is_nonempty_string(unknown.get("hypothesis_id"))
            }
            if not unresolved_alternative_ids.issubset(unknown_hypothesis_ids):
                reasons.append("unresolved_alternative_hypothesis_not_materialized")

    inspected_files = item.get("inspected_files")
    inspected_symbols = item.get("inspected_symbols")
    primary_hypothesis = (
        hypotheses[0]
        if isinstance(hypotheses, list) and hypotheses and isinstance(hypotheses[0], dict)
        else {}
    )
    primary_hypothesis_id = str(primary_hypothesis.get("hypothesis_id") or "")
    primary_mechanism_symbols = [
        str(symbol)
        for symbol in (
            primary_hypothesis.get("mechanism_symbols")
            if isinstance(primary_hypothesis.get("mechanism_symbols"), list)
            else []
        )
        if _is_nonempty_string(symbol)
    ]
    declared_symbols = {
        str(symbol)
        for symbol in (inspected_symbols if isinstance(inspected_symbols, list) else [])
        if _is_nonempty_string(symbol)
    }
    declared_touchpoint_locators = _declared_adapter_touchpoint_locators(
        item,
        hypothesis_id=primary_hypothesis_id,
    )
    connected_touchpoint_locators = _verified_connected_adapter_touchpoint_locators(
        item,
        hypothesis_id=primary_hypothesis_id,
        mechanism_symbols=primary_mechanism_symbols,
    )
    unresolved_mechanism_points = {
        symbol
        for symbol in primary_mechanism_symbols
        if symbol not in declared_symbols and symbol not in connected_touchpoint_locators
    }
    if not isinstance(inspected_files, list) or not inspected_files:
        reasons.append("exact_code_path_inspection_missing")
    elif unresolved_mechanism_points:
        if unresolved_mechanism_points.issubset(declared_touchpoint_locators):
            reasons.append("connected_mechanism_touchpoint_inspection_missing")
        else:
            reasons.append("exact_code_path_inspection_missing")

    material_unknowns = item.get("material_unknowns")
    if material_unknowns_block_advancement(material_unknowns):
        reasons.append("material_unknown_blocks_implementation_decision")

    if item.get("diff_classification") == "suspicious_implementation":
        reasons.append("research_diff_suspicious")
    runner_exit_code = item.get("runner_exit_code")
    if runner_exit_code is not None and runner_exit_code != 0:
        reasons.append("research_runner_failed")
    runner_errors = item.get("runner_report_validation_errors")
    if isinstance(runner_errors, list) and runner_errors:
        reasons.append("research_report_validation_failed")
    verification = item.get("evidence_verification")
    if not isinstance(verification, dict) or verification.get("status") != "verified":
        reasons.append("research_evidence_unverified")
    elif not verification.get("mechanism_evidence"):
        reasons.append("research_mechanism_evidence_missing")
    assignment = item.get("evidence_assignment")
    if not isinstance(assignment, dict) or assignment.get("status") != "complete":
        reasons.append("research_origin_evidence_incomplete")

    for index, member in enumerate(_post_research_bundle_members(item)):
        member_ready, member_reasons = assess_research_readiness(member)
        if not member_ready:
            reasons.extend(
                f"research_post_relation_member_not_ready:{index}:{reason}"
                for reason in member_reasons
            )

    return not reasons, list(dict.fromkeys(reasons))


def _validate_solution_option(item: dict[str, Any], *, known_family_ids: set[str]) -> list[str]:
    """Return warning strings for a single solution option.

    Parameters
    ----------
    item:
        Candidate solution option dict.
    known_family_ids:
        Set of valid family IDs from the taxonomy configuration.  Pass an empty set to
        skip family validation.

    Returns
    -------
    list[str]
        Validation warnings.
    """
    warnings: list[str] = []
    oid = item.get("option_id") or "(no option_id)"

    for field in _SOLUTION_OPTION_REQUIRED:
        if field not in item:
            warnings.append(f"solution_option_missing_required_field: {oid}: {field}")

    for field in _SOLUTION_OPTION_FORBIDDEN:
        if field in item:
            warnings.append(
                f"solution_option_contains_forbidden_field: {oid}: {field} "
                "(selection fields are not allowed in stage-4 option records)"
            )

    fid = item.get("family_id")
    if (
        known_family_ids
        and fid is not None
        and (not isinstance(fid, str) or fid not in known_family_ids)
    ):
        warnings.append(
            f"solution_option_unknown_family_id: {oid}: {fid!r} (known: {sorted(known_family_ids)})"
        )

    return warnings


def _validate_selection_decision(item: dict[str, Any]) -> list[str]:
    """Return warning strings for a single selection decision.

    Parameters
    ----------
    item:
        Candidate selection decision dict.

    Returns
    -------
    list[str]
        Validation warnings.
    """
    warnings: list[str] = []
    pid = item.get("problem_id") or "(no problem_id)"

    for field in _SELECTION_DECISION_REQUIRED:
        if field not in item:
            warnings.append(f"selection_decision_missing_required_field: {pid}: {field}")

    ux = item.get("needs_ux_review")
    if ux is not None and not isinstance(ux, bool):
        warnings.append(
            f"selection_decision_invalid_needs_ux_review_type: {pid}: "
            f"{type(ux).__name__} (must be bool)"
        )

    return warnings


def _validate_change_plan(
    item: dict[str, Any],
    *,
    allow_pending_target_contract: bool = False,
) -> list[str]:
    """Return warning strings for a single change plan.

    Parameters
    ----------
    item:
        Candidate change plan dict.

    Returns
    -------
    list[str]
        Validation warnings.
    """
    warnings: list[str] = []
    cid = item.get("change_plan_id") or "(no change_plan_id)"

    for field in _CHANGE_PLAN_REQUIRED:
        if field == "target_contract" and allow_pending_target_contract:
            continue
        if field not in item:
            warnings.append(f"change_plan_missing_required_field: {cid}: {field}")

    steps = item.get("implementation_steps")
    if isinstance(steps, list) and len(steps) == 0:
        warnings.append(f"change_plan_empty_implementation_steps: {cid}")
    elif steps is not None and not isinstance(steps, list):
        warnings.append(
            f"change_plan_invalid_implementation_steps_type: {cid}: {type(steps).__name__}"
        )

    vsteps = item.get("verification_steps")
    if isinstance(vsteps, list) and len(vsteps) == 0:
        warnings.append(f"change_plan_empty_verification_steps: {cid}")
    elif vsteps is not None and not isinstance(vsteps, list):
        warnings.append(
            f"change_plan_invalid_verification_steps_type: {cid}: {type(vsteps).__name__}"
        )

    criteria = item.get("success_criteria")
    if isinstance(criteria, list) and len(criteria) == 0:
        warnings.append(f"change_plan_empty_success_criteria: {cid}")
    elif criteria is not None and not isinstance(criteria, list):
        warnings.append(
            f"change_plan_invalid_success_criteria_type: {cid}: {type(criteria).__name__}"
        )

    rollback_notes = item.get("rollback_notes")
    if isinstance(rollback_notes, str) and not rollback_notes.strip():
        warnings.append(f"change_plan_empty_rollback_notes: {cid}")
    elif rollback_notes is not None and not isinstance(rollback_notes, str):
        warnings.append(
            f"change_plan_invalid_rollback_notes_type: {cid}: {type(rollback_notes).__name__}"
        )

    related = item.get("related_change_plan_ids")
    if related is not None and not isinstance(related, list):
        warnings.append(
            f"change_plan_invalid_related_change_plan_ids_type: {cid}: {type(related).__name__}"
        )

    requires_live = item.get("requires_live_verification")
    if requires_live is not None and not isinstance(requires_live, bool):
        warnings.append(
            f"change_plan_invalid_requires_live_verification_type: {cid}: "
            f"{type(requires_live).__name__}"
        )

    target_contract = item.get("target_contract")
    if target_contract is not None:
        if not isinstance(target_contract, dict):
            warnings.append(f"change_plan_invalid_target_contract_type: {cid}")
        else:
            for field in ("case_id", "problem_id", "selected_option_id", "repo_revision"):
                if target_contract.get(field) != item.get(field):
                    warnings.append(f"change_plan_target_contract_{field}_mismatch: {cid}")
            contract_targets_raw = target_contract.get("targets")
            plan_targets_raw = item.get("change_targets")
            contract_targets = (
                contract_targets_raw if isinstance(contract_targets_raw, list) else []
            )
            plan_targets = plan_targets_raw if isinstance(plan_targets_raw, list) else []
            contract_projection = [
                {
                    "action": target.get("action"),
                    "path": target.get("path"),
                    "destination_path": target.get("destination_path"),
                    "symbols": target.get("symbols") or [],
                    "change": target.get("change"),
                }
                for target in contract_targets
                if isinstance(target, dict)
            ]
            plan_projection = [
                {
                    "action": target.get("action"),
                    "path": target.get("path"),
                    "destination_path": target.get("destination_path"),
                    "symbols": target.get("symbols") or [],
                    "change": target.get("change"),
                }
                for target in plan_targets
                if isinstance(target, dict)
            ]
            if (
                len(contract_projection) != len(contract_targets)
                or contract_projection != plan_projection
            ):
                warnings.append(f"change_plan_target_contract_targets_mismatch: {cid}")

    return warnings


# ---------------------------------------------------------------------------
# Public parse functions
# ---------------------------------------------------------------------------


def parse_problem_record_list(text: str) -> tuple[list[dict[str, Any]], list[str]]:
    """Parse and validate a stage-1 problem-record list from raw LLM output.

    Problem records must not contain solution fields.  Any item that contains a
    forbidden field receives a ``_parse_warning`` key and is logged; it is still
    included in the output so the caller can inspect the issue.

    Parameters
    ----------
    text:
        Raw text containing a JSON list of problem records.

    Returns
    -------
    tuple[list[dict[str, Any]], list[str]]
        ``(records, warnings)`` where *warnings* is a list of human-readable
        problem descriptions.  The list may contain items with ``_parse_warning``
        keys when individual items have validation problems.

    Raises
    ------
    ValueError
        When no JSON can be extracted from *text*.
    """
    raw = _extract_json(text)
    items = _as_list(raw)
    all_warnings: list[str] = []
    result: list[dict[str, Any]] = []

    for idx, item in enumerate(items):
        if not isinstance(item, dict):
            w = f"problem_record_not_a_dict: index={idx} type={type(item).__name__}"
            all_warnings.append(w)
            result.append({"_raw": item, "_parse_warning": w})
            continue

        item_warnings = _validate_problem_record(item)
        if item_warnings:
            all_warnings.extend(item_warnings)
            item = dict(item)
            item["_parse_warning"] = "; ".join(item_warnings)
            _LOG.warning("parse_problem_record_list: %s", "; ".join(item_warnings))

        # Inject canonical status if not present.
        if "problem_status" not in item:
            item = dict(item)
            item["problem_status"] = "identified"

        result.append(item)

    return result, all_warnings


def parse_priority_decision_list(text: str) -> tuple[list[dict[str, Any]], list[str]]:
    """Parse and validate a stage-2 priority-decision list from raw LLM output.

    Parameters
    ----------
    text:
        Raw text containing a JSON list of priority decisions.

    Returns
    -------
    tuple[list[dict[str, Any]], list[str]]
        ``(decisions, warnings)``.

    Raises
    ------
    ValueError
        When no JSON can be extracted from *text*.
    """
    raw = _extract_json(text)
    items = _as_list(raw)
    all_warnings: list[str] = []
    result: list[dict[str, Any]] = []

    for idx, item in enumerate(items):
        if not isinstance(item, dict):
            w = f"priority_decision_not_a_dict: index={idx} type={type(item).__name__}"
            all_warnings.append(w)
            result.append({"_raw": item, "_parse_warning": w})
            continue

        item_warnings = _validate_priority_decision(item)
        if item_warnings:
            all_warnings.extend(item_warnings)
            item = dict(item)
            item["_parse_warning"] = "; ".join(item_warnings)
            _LOG.warning("parse_priority_decision_list: %s", "; ".join(item_warnings))

        if "priority_status" not in item:
            item = dict(item)
            item["priority_status"] = "prioritized"

        result.append(item)

    return result, all_warnings


def parse_research_dossier_list(
    text: str,
    *,
    legacy: bool = False,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Parse and validate stage-3 research proof records from raw output.

    Version-3 proof records are strict by default: malformed records, missing
    fields, invalid nested evidence, and implementation work all raise
    ``ValueError``. Callers reading historical artifacts must opt into the
    warning-based legacy contract with ``legacy=True``. The compatibility path
    preserves the stored status verbatim and never injects a success-like
    ``research_status``.

    Parameters
    ----------
    text:
        Raw text containing a JSON list of research dossiers.
    legacy:
        Read historical version-1 dossiers with warning-based validation. This
        mode is for inspection only; legacy dossiers never satisfy the current
        readiness gate.

    Returns
    -------
    tuple[list[dict[str, Any]], list[str]]
        ``(dossiers, warnings)``.

    Raises
    ------
    ValueError
        When no JSON can be extracted, any new proof record is malformed, or any
        dossier sets ``implementation_performed=true``.
    """
    raw = _extract_json(text)
    items = _as_list(raw)
    all_warnings: list[str] = []
    result: list[dict[str, Any]] = []

    for idx, item in enumerate(items):
        if not isinstance(item, dict):
            w = f"research_dossier_not_a_dict: index={idx} type={type(item).__name__}"
            if not legacy:
                raise ValueError(w)
            all_warnings.append(w)
            result.append({"_raw": item, "_parse_warning": w})
            continue

        if legacy:
            item_warnings = _validate_legacy_research_dossier(item)
            if item_warnings:
                all_warnings.extend(item_warnings)
                item = dict(item)
                item["_parse_warning"] = "; ".join(item_warnings)
                _LOG.warning(
                    "parse_research_dossier_list(legacy=True): %s",
                    "; ".join(item_warnings),
                )
            result.append(item)
            continue

        item_errors = _validate_research_dossier(item)
        if item_errors:
            raise ValueError("research_dossier_invalid: " + "; ".join(item_errors))
        result.append(dict(item))

    return result, all_warnings


def parse_solution_option_sets(
    text: str, *, known_family_ids: set[str] | None = None
) -> tuple[list[dict[str, Any]], list[str]]:
    """Parse and validate a stage-4 solution-option list from raw LLM output.

    Parameters
    ----------
    text:
        Raw text containing a JSON list of solution options.
    known_family_ids:
        Set of valid family IDs from ``configs/backlog_taxonomy.json``.  Pass
        ``None`` to skip family validation.

    Returns
    -------
    tuple[list[dict[str, Any]], list[str]]
        ``(options, warnings)``.

    Raises
    ------
    ValueError
        When no JSON can be extracted from *text*.
    """
    family_ids: set[str] = known_family_ids if known_family_ids is not None else set()
    raw = _extract_json(text)
    items = _as_list(raw)
    all_warnings: list[str] = []
    result: list[dict[str, Any]] = []

    for idx, item in enumerate(items):
        if not isinstance(item, dict):
            w = f"solution_option_not_a_dict: index={idx} type={type(item).__name__}"
            all_warnings.append(w)
            result.append({"_raw": item, "_parse_warning": w})
            continue

        item_warnings = _validate_solution_option(item, known_family_ids=family_ids)
        if item_warnings:
            all_warnings.extend(item_warnings)
            item = dict(item)
            item["_parse_warning"] = "; ".join(item_warnings)
            _LOG.warning("parse_solution_option_sets: %s", "; ".join(item_warnings))

        if "option_status" not in item:
            item = dict(item)
            item["option_status"] = "optioned"

        result.append(item)

    return result, all_warnings


def parse_selection_decisions(text: str) -> tuple[list[dict[str, Any]], list[str]]:
    """Parse and validate a stage-5 selection-decision list from raw LLM output.

    Parameters
    ----------
    text:
        Raw text containing a JSON list of selection decisions.

    Returns
    -------
    tuple[list[dict[str, Any]], list[str]]
        ``(decisions, warnings)``.

    Raises
    ------
    ValueError
        When no JSON can be extracted from *text*.
    """
    raw = _extract_json(text)
    items = _as_list(raw)
    all_warnings: list[str] = []
    result: list[dict[str, Any]] = []

    for idx, item in enumerate(items):
        if not isinstance(item, dict):
            w = f"selection_decision_not_a_dict: index={idx} type={type(item).__name__}"
            all_warnings.append(w)
            result.append({"_raw": item, "_parse_warning": w})
            continue

        item_warnings = _validate_selection_decision(item)
        if item_warnings:
            all_warnings.extend(item_warnings)
            item = dict(item)
            item["_parse_warning"] = "; ".join(item_warnings)
            _LOG.warning("parse_selection_decisions: %s", "; ".join(item_warnings))

        if "selection_status" not in item:
            item = dict(item)
            item["selection_status"] = "selected"

        result.append(item)

    return result, all_warnings


def parse_change_plan_list(
    text: str,
    *,
    allow_pending_target_contract: bool = False,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Parse and validate a stage-6 change-plan list from raw LLM output.

    Parameters
    ----------
    text:
        Raw text containing a JSON list of change plans.

    Returns
    -------
    tuple[list[dict[str, Any]], list[str]]
        ``(plans, warnings)``.

    Raises
    ------
    ValueError
        When no JSON can be extracted from *text*.
    """
    raw = _extract_json(text)
    items = _as_list(raw)
    all_warnings: list[str] = []
    result: list[dict[str, Any]] = []

    for idx, item in enumerate(items):
        if not isinstance(item, dict):
            w = f"change_plan_not_a_dict: index={idx} type={type(item).__name__}"
            all_warnings.append(w)
            result.append({"_raw": item, "_parse_warning": w})
            continue

        item_warnings = _validate_change_plan(
            item,
            allow_pending_target_contract=allow_pending_target_contract,
        )
        if item_warnings:
            all_warnings.extend(item_warnings)
            item = dict(item)
            item["_parse_warning"] = "; ".join(item_warnings)
            _LOG.warning("parse_change_plan_list: %s", "; ".join(item_warnings))

        if "change_plan_status" not in item:
            item = dict(item)
            item["change_plan_status"] = "planned"

        result.append(item)

    return result, all_warnings


# ---------------------------------------------------------------------------
# Stage document builder
# ---------------------------------------------------------------------------


def build_stage_document(
    stage: str,
    items: list[dict[str, Any]],
    *,
    input_meta: dict[str, Any],
    artifacts: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the standard artifact envelope for a completed stage.

    This is the single authoritative place that knows the stage-artifact envelope
    schema.  All stage writers should use this function rather than constructing the
    envelope themselves.

    Parameters
    ----------
    stage:
        Stage identifier string (e.g. ``"problem_mining"``, ``"repro_research"``).
    items:
        Validated stage items (problem records, dossiers, etc.).
    input_meta:
        Metadata about the inputs to this stage, e.g. atom counts, stage config
        fingerprint, upstream artifact paths.
    artifacts:
        Optional mapping of artifact paths written during this stage.  Each value
        should be a string path.

    Returns
    -------
    dict[str, Any]
        Stage document dict suitable for JSON serialization.
    """
    warnings = [
        item["_parse_warning"]
        for item in items
        if isinstance(item, dict) and "_parse_warning" in item
    ]
    return {
        "stage": stage,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "item_count": len(items),
        "warning_count": len(warnings),
        "warnings": warnings,
        "input_meta": input_meta,
        "artifacts": artifacts or {},
        "items": items,
    }
