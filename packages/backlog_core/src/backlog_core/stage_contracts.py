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
_RESEARCH_DOSSIER_RUNNER_FIELDS: tuple[str, ...] = (
    "repo_workspace",
    "run_dir",
    "runner_exit_code",
    "runner_report_validation_errors",
    "diff_suspicious_reasons",
    "artifacts",
    "post_research_same_mechanism_bundle",
)
_RESEARCH_DOSSIER_ALLOWED: frozenset[str] = frozenset(
    (*_RESEARCH_DOSSIER_REQUIRED, *_RESEARCH_DOSSIER_RUNNER_FIELDS)
)
_VALID_REPRODUCTION_STATUSES: frozenset[str] = frozenset(
    {"reproduced", "reproduction_failed", "partial", "blocked"}
)
_VALID_RESEARCH_METHODS: frozenset[str] = frozenset({"reproduction", "static_trace"})
_VALID_RESEARCH_STATUSES: frozenset[str] = frozenset(
    {"evidence_sufficient", "insufficient_evidence", "blocked"}
)
_VALID_EXPERIMENT_OUTCOMES: frozenset[str] = frozenset(
    {"supports", "refutes", "inconclusive"}
)
_VALID_FALSIFICATION_ATTEMPT_OUTCOMES: frozenset[str] = frozenset(
    {"survived", "disproved", "inconclusive"}
)
_VALID_EXPERIMENT_SCENARIOS: frozenset[str] = frozenset(
    {"original_replay", "faithful_replay", "control", "static_trace", "live_runtime"}
)
_VALID_PLATFORM_REQUIREMENTS: frozenset[str] = frozenset(
    {"any", "windows", "linux", "darwin"}
)
_VALID_MECHANISM_EVIDENCE_TYPES: frozenset[str] = frozenset(
    {
        "exception_trace",
        "observed_output",
        "controlled_scenario",
        "temporary_harness",
        "static_trace",
        "live_runtime",
    }
)
_VALID_HYPOTHESIS_DISPOSITIONS: frozenset[str] = frozenset(
    {"primary", "refuted", "plausible", "unresolved"}
)
_VALID_ASSERTION_SOURCES: frozenset[str] = frozenset(
    {"exit_code", "stdout", "stderr", "combined"}
)
_VALID_ASSERTION_OPERATORS: frozenset[str] = frozenset(
    {"equals", "contains", "not_contains"}
)
_VALID_UNKNOWN_EFFECTS: frozenset[str] = frozenset(
    {"root_cause", "interface", "change_surface", "scope", "verification"}
)
_READINESS_BLOCKING_UNKNOWN_EFFECTS: frozenset[str] = frozenset(
    {"root_cause", "interface", "change_surface"}
)
RESEARCH_PROOF_SCHEMA_VERSION = 3
MIN_RESEARCH_CONFIDENCE_FOR_READY = 0.75
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
    return _canonical_sha256({field: item.get(field) for field in _RESEARCH_CLAIM_FIELDS})


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
    "family_id",
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
    "selected_family_id",
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
            warnings.append(
                f"problem_record_empty_required_text: {pid}: {field}"
            )

    for field in _PROBLEM_RECORD_FORBIDDEN:
        if field in item:
            warnings.append(
                f"problem_record_contains_forbidden_field: {pid}: {field} "
                "(solution fields are not allowed in stage-1 problem records)"
            )

    sev = item.get("severity")
    if sev is not None and sev not in _VALID_SEVERITIES:
        warnings.append(f"problem_record_invalid_severity: {pid}: {sev!r}")

    conf = item.get("confidence")
    if isinstance(conf, bool) or (
        conf is not None and not isinstance(conf, (int, float))
    ):
        warnings.append(f"problem_record_invalid_confidence_type: {pid}: {type(conf).__name__}")
    elif conf is not None and not (0.0 <= float(conf) <= 1.0):
        warnings.append(f"problem_record_confidence_out_of_range: {pid}: {conf}")

    eids = item.get("evidence_atom_ids")
    if not isinstance(eids, list) or len(eids) == 0:
        warnings.append(f"problem_record_empty_evidence_atom_ids: {pid}")

    status = item.get("problem_status")
    if status is not None and status not in _VALID_PROBLEM_STATUSES:
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
    if bucket is not None and bucket not in _VALID_PRIORITY_BUCKETS:
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
    prefixed = terminal.startswith(
        ("expected_", "desired_", "correct_", "intended_", "required_")
    )
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
        return [
            f"research_dossier_invalid_artifact_refs_type: {pid}: {type(value).__name__}"
        ]
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
                errors.append(
                    f"research_dossier_invalid_artifact_ref_{field}: {pid}: index={idx}"
                )
        artifact_id = ref.get("artifact_id")
        if _is_nonempty_string(artifact_id):
            if artifact_id in seen_ids:
                errors.append(
                    f"research_dossier_duplicate_artifact_id: {pid}: {artifact_id}"
                )
            seen_ids.add(str(artifact_id))
        description = ref.get("description")
        if description is not None and not _is_nonempty_string(description):
            errors.append(
                f"research_dossier_invalid_artifact_ref_description: {pid}: index={idx}"
            )
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
                errors.append(
                    f"research_dossier_invalid_experiment_{field}: {pid}: index={idx}"
                )
        experiment_id = experiment.get("experiment_id")
        if _is_nonempty_string(experiment_id):
            if experiment_id in seen_ids:
                errors.append(
                    f"research_dossier_duplicate_experiment_id: {pid}: {experiment_id}"
                )
            seen_ids.add(str(experiment_id))
        outcome = experiment.get("outcome")
        if outcome not in _VALID_EXPERIMENT_OUTCOMES:
            errors.append(
                f"research_dossier_invalid_experiment_outcome: {pid}: index={idx} "
                f"value={outcome!r}"
            )
        scenario_kind = experiment.get("scenario_kind")
        if scenario_kind not in _VALID_EXPERIMENT_SCENARIOS:
            errors.append(
                f"research_dossier_invalid_experiment_scenario_kind: {pid}: "
                f"index={idx} value={scenario_kind!r}"
            )
        fidelity_mapping = experiment.get("fidelity_mapping")
        if scenario_kind in {"faithful_replay", "live_runtime"}:
            if not isinstance(fidelity_mapping, dict) or any(
                not _is_nonempty_string(fidelity_mapping.get(field))
                for field in (
                    "original_condition",
                    "retained_differences",
                    "why_mechanism_equivalent",
                )
            ):
                errors.append(
                    f"research_dossier_fidelity_mapping_missing: {pid}: index={idx}"
                )
        elif fidelity_mapping is not None:
            errors.append(
                f"research_dossier_unexpected_fidelity_mapping: {pid}: index={idx}"
            )
        mechanism_link = experiment.get("mechanism_link")
        if mechanism_link is not None:
            code_path = (
                mechanism_link.get("code_path")
                if isinstance(mechanism_link, dict)
                else None
            )
            if (
                not isinstance(mechanism_link, dict)
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
        if platform_requirement not in _VALID_PLATFORM_REQUIREMENTS:
            errors.append(
                f"research_dossier_invalid_experiment_platform_requirement: {pid}: "
                f"index={idx} value={platform_requirement!r}"
            )
        if scenario_kind == "live_runtime" and platform_requirement == "any":
            errors.append(
                f"research_dossier_live_runtime_platform_required: {pid}: index={idx}"
            )
        static_trace = experiment.get("static_trace")
        if scenario_kind == "static_trace":
            if not isinstance(static_trace, dict):
                errors.append(
                    f"research_dossier_static_trace_contract_missing: {pid}: index={idx}"
                )
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
                errors.append(
                    f"research_dossier_control_relationship_missing: {pid}: index={idx}"
                )
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
                        and binding.get("field_path")
                        == positive_contract.get("field_path")
                    ),
                    None,
                )
                postcondition = positive_contract.get("postcondition")
                predicate_type = (
                    postcondition.get("type")
                    if isinstance(postcondition, dict)
                    else None
                )
                valid_predicate = False
                if predicate_type in {
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
                        and ".."
                        not in str(artifact_path).replace("\\", "/").split("/")
                        and isinstance(pointer, str)
                        and (not pointer or pointer.startswith("/"))
                        and "equals" in postcondition
                    )
                elif predicate_type == "config_state_equals":
                    valid_predicate = (
                        _is_nonempty_string(postcondition.get("mechanism_symbol"))
                        and str(postcondition.get("mechanism_symbol")).startswith(
                            "config:/"
                        )
                        and "equals" in postcondition
                        and isinstance(postcondition.get("exists", True), bool)
                    )
                valid_contract = (
                    _is_nonempty_string(positive_contract.get("atom_id"))
                    and _is_nonempty_string(positive_contract.get("field_path"))
                    and str(positive_contract.get("field_path")).startswith("$")
                    and _expected_semantic_field_path(
                        positive_contract.get("field_path")
                    )
                    and isinstance(expected_binding, dict)
                    and valid_predicate
                )
            elif contract_kind == "retained_harness_semantic_assertion":
                basis = positive_contract.get("semantic_basis")
                basis_kind = basis.get("kind") if isinstance(basis, dict) else None
                common_valid = (
                    scenario_kind in {"original_replay", "faithful_replay"}
                    and "expected_value" in positive_contract
                    and _is_nonempty_string(
                        positive_contract.get("semantic_rationale")
                    )
                    and len(str(positive_contract.get("semantic_rationale") or "").strip())
                    >= 20
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
                        basis.get("contract_type")
                        in {"api_contract", "documentation", "schema"}
                        and _is_nonempty_string(basis.get("path"))
                        and not str(basis.get("path")).startswith(("/", "\\"))
                        and ".."
                        not in str(basis.get("path")).replace("\\", "/").split("/")
                    )
                else:
                    valid_basis = False
                review_ref = positive_contract.get("adversarial_review_reference")
                valid_contract = common_valid and valid_basis and (
                    review_ref is None or _is_nonempty_string(review_ref)
                )
            if not isinstance(positive_contract, dict) or not valid_contract:
                errors.append(
                    f"research_dossier_positive_outcome_contract_invalid: {pid}: index={idx}"
                )
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
                f"research_dossier_invalid_experiment_observable_assertion: {pid}: "
                f"index={idx}"
            )
            continue
        source = assertion.get("source")
        operator = assertion.get("operator")
        expected = assertion.get("expected")
        if source not in _VALID_ASSERTION_SOURCES:
            errors.append(
                f"research_dossier_invalid_assertion_source: {pid}: "
                f"index={idx} value={source!r}"
            )
        if operator not in _VALID_ASSERTION_OPERATORS:
            errors.append(
                f"research_dossier_invalid_assertion_operator: {pid}: "
                f"index={idx} value={operator!r}"
            )
        if source == "exit_code":
            if operator != "equals" or isinstance(expected, bool) or not isinstance(expected, int):
                errors.append(
                    f"research_dossier_invalid_exit_code_assertion: {pid}: index={idx}"
                )
        elif not _is_nonempty_string(expected):
            errors.append(
                f"research_dossier_invalid_text_assertion_expected: {pid}: index={idx}"
            )
    return errors


def _validate_hypotheses(value: Any, *, pid: str) -> list[str]:
    """Validate root-cause hypotheses and explicit causal challenges."""
    if not isinstance(value, list):
        return [
            f"research_dossier_invalid_root_cause_hypotheses_type: {pid}: "
            f"{type(value).__name__}"
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
            errors.append(
                f"research_dossier_invalid_hypothesis_id: {pid}: index={idx}"
            )
        elif hypothesis_id in seen_ids:
            errors.append(
                f"research_dossier_duplicate_hypothesis_id: {pid}: {hypothesis_id}"
            )
        else:
            seen_ids.add(str(hypothesis_id))
        if not _is_nonempty_string(hypothesis.get("statement")):
            errors.append(
                f"research_dossier_invalid_hypothesis_statement: {pid}: index={idx}"
            )
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
        if disposition not in _VALID_HYPOTHESIS_DISPOSITIONS:
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
                require_nonempty=disposition in {"primary", "refuted"},
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
            if attempt.get("baseline_experiment_id") == attempt.get(
                "challenge_experiment_id"
            ):
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
                if source not in _VALID_ASSERTION_SOURCES:
                    errors.append(
                        f"research_dossier_falsification_attempt_disproof_source_invalid: "
                        f"{pid}: hypothesis={hypothesis_id} index={attempt_index}"
                    )
                if operator not in _VALID_ASSERTION_OPERATORS:
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
            if attempt.get("outcome") not in _VALID_FALSIFICATION_ATTEMPT_OUTCOMES:
                errors.append(
                    f"research_dossier_falsification_attempt_outcome_invalid: {pid}: "
                    f"hypothesis={hypothesis_id} index={attempt_index}"
                )
    return errors


def _validate_research_evidence_links(item: dict[str, Any], *, pid: str) -> list[str]:
    """Validate cross-record evidence IDs and directional hypothesis evidence."""
    errors: list[str] = []
    artifact_refs = item.get("artifact_refs")
    artifact_ids = {
        str(ref.get("artifact_id"))
        for ref in (artifact_refs if isinstance(artifact_refs, list) else [])
        if isinstance(ref, dict)
        and _is_nonempty_string(ref.get("artifact_id"))
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
        if isinstance(experiment, dict)
        and _is_nonempty_string(experiment.get("experiment_id"))
    }
    experiment_scenarios = {
        str(experiment.get("experiment_id")): str(experiment.get("scenario_kind"))
        for experiment in (experiments if isinstance(experiments, list) else [])
        if isinstance(experiment, dict)
        and _is_nonempty_string(experiment.get("experiment_id"))
    }
    experiment_artifact_refs = {
        str(experiment.get("experiment_id")): {
            ref
            for ref in experiment.get("artifact_refs", [])
            if isinstance(ref, str) and ref.strip()
        }
        for experiment in (experiments if isinstance(experiments, list) else [])
        if isinstance(experiment, dict)
        and _is_nonempty_string(experiment.get("experiment_id"))
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
        for atom_id in (
            expected_atom_ids_raw if isinstance(expected_atom_ids_raw, list) else []
        )
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

    # A blocked proof may legitimately contain no experiments (for example,
    # when policy or an unavailable runtime prevents the first command from
    # running).  Once research records any experiment, however, the recorded
    # work must account for every assigned atom.  Evidence-sufficient proofs
    # are also required to cover the full assignment; the readiness gate below
    # separately requires them to contain experiments at all.
    if (
        assignment.get("status") == "complete"
        and expected_atom_ids
        and (bool(experiments) or item.get("research_status") == "evidence_sufficient")
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
        support_refs = supporting if isinstance(supporting, list) else []
        counter = hypothesis.get("counterevidence")
        counter_refs = counter if isinstance(counter, list) else []
        mechanism_symbols_raw = hypothesis.get("mechanism_symbols")
        mechanism_symbols = (
            mechanism_symbols_raw if isinstance(mechanism_symbols_raw, list) else []
        )
        disposition = hypothesis.get("disposition")
        disposition_raw = hypothesis.get("disposition_evidence")
        disposition_refs = disposition_raw if isinstance(disposition_raw, list) else []
        if any(symbol not in inspected_symbols for symbol in mechanism_symbols):
            errors.append(
                f"research_dossier_hypothesis_symbol_uninspected: {pid}: {hypothesis_id}"
            )
        for ref in [*support_refs, *counter_refs, *disposition_refs]:
            if isinstance(ref, str) and ref not in known_refs:
                errors.append(
                    f"research_dossier_unresolved_hypothesis_evidence_ref: {pid}: "
                    f"hypothesis={hypothesis_id} ref={ref}"
                )
        if disposition == "refuted" and not any(
            experiment_outcomes.get(ref) == "refutes"
            for ref in disposition_refs
            if isinstance(ref, str)
        ):
            errors.append(
                f"research_dossier_refuted_hypothesis_missing_falsification: "
                f"{pid}: {hypothesis_id}"
            )
        attempts_raw = hypothesis.get("falsification_attempts")
        attempts = attempts_raw if isinstance(attempts_raw, list) else []
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
            and experiment_scenarios.get(ref) in advancing_scenarios
        ]
        # The first hypothesis is the implementation-driving mechanism.  Later
        # hypotheses are explicit alternatives and may be supported/refuted by
        # retained artifacts rather than an independent full reproduction.
        if index == 0 and not supporting_experiment_ids:
            errors.append(
                f"research_dossier_primary_hypothesis_missing_supporting_experiment: "
                f"{pid}: {hypothesis_id}"
            )
        if supporting_experiment_ids and not any(
            artifact_paths_by_id.get(artifact_ref) in inspected_files
            for experiment_id in supporting_experiment_ids
            for artifact_ref in experiment_artifact_refs.get(experiment_id, set())
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
            shared_code_refs = (
                experiment_artifact_refs.get(str(counter_ref), set())
                & experiment_artifact_refs.get(support_id, set())
            )
            if (
                not isinstance(support_experiment, dict)
                or support_id not in supporting_experiment_ids
                or relationship.get("mechanism_symbols") != mechanism_symbols
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
                if isinstance(attempt, dict)
                and _is_nonempty_string(attempt.get("attempt_id"))
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
        for value in (
            interventions_raw if isinstance(interventions_raw, list) else []
        )
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
    experiments_raw = item.get("experiments")
    experiments = {
        str(experiment.get("experiment_id")): experiment
        for experiment in (experiments_raw if isinstance(experiments_raw, list) else [])
        if isinstance(experiment, dict)
        and _is_nonempty_string(experiment.get("experiment_id"))
    }
    receipt_experiments_raw = verification.get("experiments")
    receipt_experiments = {
        str(experiment.get("experiment_id")): experiment
        for experiment in (
            receipt_experiments_raw if isinstance(receipt_experiments_raw, list) else []
        )
        if isinstance(experiment, dict)
        and _is_nonempty_string(experiment.get("experiment_id"))
    }
    expected_symbols = hypothesis.get("mechanism_symbols")
    mechanism_evidence_raw = verification.get("mechanism_evidence")
    mechanism_evidence_ids: dict[str, set[str]] = {}
    for evidence in (
        mechanism_evidence_raw if isinstance(mechanism_evidence_raw, list) else []
    ):
        if not isinstance(evidence, dict):
            continue
        evidence_id = str(evidence.get("mechanism_evidence_id") or "")
        expected_id = "mechanism_evidence:" + _canonical_sha256(
            {
                field: value
                for field, value in evidence.items()
                if field != "mechanism_evidence_id"
            }
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
        ref
        for ref in hypothesis.get("supporting_evidence", [])
        if isinstance(ref, str)
    }
    counter_refs = {
        ref for ref in hypothesis.get("counterevidence", []) if isinstance(ref, str)
    }
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
            or outcome not in _VALID_FALSIFICATION_ATTEMPT_OUTCOMES
            or not isinstance(baseline, dict)
            or not isinstance(challenge, dict)
            or not isinstance(baseline_receipt, dict)
            or not isinstance(challenge_receipt, dict)
            or baseline_id == challenge_id
            or baseline_id not in support_refs
            or baseline.get("outcome") != "supports"
            or challenge.get("outcome") != expected_experiment_outcomes.get(str(outcome))
            or challenge.get("scenario_kind")
            not in {
                "original_replay",
                "faithful_replay",
                "control",
                "static_trace",
                "live_runtime",
            }
            or baseline.get("command") == challenge.get("command")
            or not baseline.get("addresses_atom_ids")
            or baseline.get("addresses_atom_ids") != challenge.get("addresses_atom_ids")
            or not baseline_artifacts.intersection(challenge_artifacts)
            or str(challenge_id) not in mechanism_evidence_ids
            or str(baseline_id) not in mechanism_evidence_ids
            or challenge_receipt.get("assertion_passed") is not True
            or baseline_receipt.get("assertion_passed") is not True
            or (
                outcome in {"survived", "disproved"}
                and not isinstance(intervention_receipt, dict)
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
            (
                challenge_receipt.get(receipt_field)
                == challenge.get(declared_field)
            )
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
                "mechanism_evidence_ids": sorted(
                    mechanism_evidence_ids[str(challenge_id)]
                ),
                "intervention_receipt_id": (
                    intervention_receipt.get("intervention_receipt_id")
                    if isinstance(intervention_receipt, dict)
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
    hypotheses = [value for value in hypotheses_raw if isinstance(value, dict)] if isinstance(
        hypotheses_raw, list
    ) else []
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
    if any(
        value.get("disposition") != "refuted"
        for value in hypotheses[1:]
    ):
        return []
    if any(
        isinstance(unknown, dict)
        and "root_cause" in (
            unknown.get("affects") if isinstance(unknown.get("affects"), list) else []
        )
        for unknown in (
            item.get("material_unknowns")
            if isinstance(item.get("material_unknowns"), list)
            else []
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
    expected_alternatives = [
        str(value.get("hypothesis_id")) for value in hypotheses[1:]
    ]
    projected: list[dict[str, Any]] = []
    raw_closures = verification.get("deterministic_mechanism_closures")
    for closure in raw_closures if isinstance(raw_closures, list) else []:
        if not isinstance(closure, dict):
            continue
        support_id = str(closure.get("support_experiment_id") or "")
        experiment = experiments.get(support_id, {})
        replay = replays.get(support_id, {})
        code_path_raw = closure.get("code_path")
        code_path = code_path_raw if isinstance(code_path_raw, list) else []
        closure_id = str(closure.get("closure_receipt_id") or "")
        expected_id = "deterministic_mechanism_closure:" + _canonical_sha256(
            {
                field: value
                for field, value in closure.items()
                if field != "closure_receipt_id"
            }
        )
        if (
            closure_id != expected_id
            or closure.get("verification_method")
            != "runner_deterministic_mechanism_closure_v1"
            or closure.get("hypothesis_id") != hypothesis_id
            or closure.get("mechanism_symbols") != expected_symbols
            or closure.get("closure_basis")
            not in {"deterministic_static_trace", "complete_runner_dataflow"}
            or experiment.get("outcome") != "supports"
            or support_id not in hypothesis.get("supporting_evidence", [])
            or replay.get("assertion_passed") is not True
            or closure.get("scenario_kind") != experiment.get("scenario_kind")
            or closure.get("alternatives_disposed") != expected_alternatives
            or closure.get("origin_atom_ids")
            != sorted(set(experiment.get("addresses_atom_ids", [])))
            or not all(
                any(
                    isinstance(step, dict)
                    and step.get("symbol") == symbol
                    and step.get("path") == symbol_paths.get(symbol)
                    for step in code_path
                )
                for symbol in (expected_symbols if isinstance(expected_symbols, list) else [])
            )
            or closure.get("observed_result")
            != {
                "exit_code": replay.get("exit_code"),
                "stdout_sha256": replay.get("stdout_sha256"),
                "stderr_sha256": replay.get("stderr_sha256"),
                "assertion": experiment.get("observable_assertion"),
            }
        ):
            continue
        projected.append(dict(closure))
    return sorted(projected, key=lambda value: str(value.get("closure_receipt_id")))


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
    if not isinstance(receipts_raw, list) or (
        assignment_status == "complete" and not receipts
    ):
        errors.append(f"research_evidence_assignment_atom_receipts_missing: {pid}")
    receipt_ids: list[str] = []
    for index, receipt in enumerate(receipts):
        if not isinstance(receipt, dict):
            errors.append(
                f"research_evidence_assignment_atom_receipt_invalid: {pid}: {index}"
            )
            continue
        atom_id = receipt.get("atom_id")
        if not _is_nonempty_string(atom_id):
            errors.append(
                f"research_evidence_assignment_atom_id_invalid: {pid}: {index}"
            )
        else:
            receipt_ids.append(str(atom_id))
        if not _valid_sha256(receipt.get("atom_sha256")):
            errors.append(
                f"research_evidence_assignment_atom_hash_invalid: {pid}: {index}"
            )
        snapshot = receipt.get("atom_snapshot")
        if not isinstance(snapshot, dict) or receipt.get("atom_sha256") != _canonical_sha256(
            snapshot
        ):
            errors.append(
                f"research_evidence_assignment_atom_snapshot_invalid: {pid}: {index}"
            )
        elif snapshot.get("atom_id") != atom_id:
            errors.append(
                f"research_evidence_assignment_atom_snapshot_id_mismatch: {pid}: {index}"
            )
        artifact_receipts_raw = receipt.get("artifact_receipts")
        artifact_receipts = (
            artifact_receipts_raw if isinstance(artifact_receipts_raw, list) else []
        )
        origin_evidence_mode = receipt.get("origin_evidence_mode")
        if origin_evidence_mode is None and artifact_receipts:
            # Compatibility for retained v1 receipts written before the mode was
            # explicit. Their artifact list proves the stronger legacy shape.
            origin_evidence_mode = "snapshot_and_artifacts"
        if origin_evidence_mode not in {"signed_snapshot", "snapshot_and_artifacts"}:
            errors.append(
                f"research_evidence_assignment_origin_mode_invalid: {pid}: {atom_id}"
            )
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
                and support_argument.get("ast_sha256")
                == control_argument.get("ast_sha256")
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
        if isinstance(experiment, dict)
        and _is_nonempty_string(experiment.get("experiment_id"))
    }
    receipt_experiments_raw = receipt.get("experiments")
    receipt_experiments = {
        str(experiment.get("experiment_id")): experiment
        for experiment in (
            receipt_experiments_raw if isinstance(receipt_experiments_raw, list) else []
        )
        if isinstance(experiment, dict)
        and _is_nonempty_string(experiment.get("experiment_id"))
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
                or attempt.get("outcome") not in {"survived", "disproved"}
            ):
                continue
            attempt_id = str(attempt.get("attempt_id") or "")
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
            errors.append(
                f"research_falsification_intervention_invalid: {pid}: {index}"
            )
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
        if intervention_method == "pytest_ast_falsification_intervention_v1":
            selections_valid = (
                intervention.get("baseline_selection_id")
                == f"{key[0]}:{baseline_id}"
                and intervention.get("challenge_selection_id")
                == f"{key[0]}:{challenge_id}"
            )
            structural_valid = (
                structural.get("verification_method")
                == "python_ast_explicit_argument_delta_v1"
                and structural.get("difference_count") == 1
                and difference.get("mechanism_symbol")
                in (expected_entry.get("mechanism_symbols") or [])
                and _is_nonempty_string(difference.get("slot"))
                and difference.get("difference_kind")
                in {"added_in_control", "removed_in_control", "changed"}
            )
        elif intervention_method == "runner_argv_falsification_intervention_v1":
            baseline_argument = difference.get("baseline_argument")
            challenge_argument = difference.get("challenge_argument")
            baseline_hash = difference.get("baseline_file_sha256")
            challenge_hash = difference.get("challenge_file_sha256")
            selections_valid = (
                intervention.get("baseline_selection_id")
                == "argv_selection:"
                + _canonical_sha256(baseline_replay.get("executed_argv"))
                and intervention.get("challenge_selection_id")
                == "argv_selection:"
                + _canonical_sha256(challenge_replay.get("executed_argv"))
            )
            structural_valid = (
                structural.get("verification_method")
                == "executed_argv_repository_file_delta_v1"
                and structural.get("difference_count") == 1
                and _is_nonempty_string(difference.get("slot"))
                and str(difference.get("slot")).startswith("argv:")
                and difference.get("difference_kind")
                == "repository_file_input_changed"
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
            selections_valid = False
            structural_valid = False
        valid = (
            expected_entry is not None
            and key not in observed
            and intervention_method
            in {
                "pytest_ast_falsification_intervention_v1",
                "runner_argv_falsification_intervention_v1",
            }
            and intervention.get("baseline_experiment_id") == baseline_id
            and intervention.get("challenge_experiment_id") == challenge_id
            and intervention.get("mechanism_symbols")
            == expected_entry.get("mechanism_symbols")
            and selections_valid
            and challenge.get("scenario_kind") == "control"
            and relationship.get("supports_experiment_id") == baseline_id
            and relationship.get("mechanism_symbols")
            == expected_entry.get("mechanism_symbols")
            and intervention.get("relationship_sha256")
            == _canonical_sha256(
                {
                    "controlled_variable": relationship.get("controlled_variable"),
                    "expected_difference": relationship.get("expected_difference"),
                    "mechanism_symbols": relationship.get("mechanism_symbols"),
                }
            )
            and structural_valid
            and observation.get("verification_method")
            == "runner_replay_falsification_polarity_v1"
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
            errors.append(
                f"research_falsification_intervention_invalid: {pid}: {index}"
            )
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
    hypotheses_raw = item.get("root_cause_hypotheses")
    hypotheses = hypotheses_raw if isinstance(hypotheses_raw, list) else []
    primary = hypotheses[0] if hypotheses and isinstance(hypotheses[0], dict) else {}
    hypothesis_id = primary.get("hypothesis_id")
    symbols = primary.get("mechanism_symbols")
    evidence_raw = receipt.get("mechanism_evidence")
    evidence = [
        value
        for value in (evidence_raw if isinstance(evidence_raw, list) else [])
        if isinstance(value, dict)
        and value.get("hypothesis_id") == hypothesis_id
        and value.get("mechanism_symbols") == symbols
    ]
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
                value.get("code_paths")
                if isinstance(value.get("code_paths"), list)
                else []
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
            "mechanism_symbols": normalized_symbols,
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
        "schema_version": 2,
        "mechanism_symbols": normalized_symbols,
        "code_paths": [
            {"symbol": symbol, "path": path} for symbol, path in code_paths
        ],
    }
    expected_provenance = {
        "schema_version": 1,
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
    if (
        not isinstance(projection, dict)
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
        if isinstance(experiment, dict)
        and _is_nonempty_string(experiment.get("experiment_id"))
    }
    receipt_experiments_raw = receipt.get("experiments")
    receipt_experiments = {
        str(experiment.get("experiment_id")): experiment
        for experiment in (
            receipt_experiments_raw
            if isinstance(receipt_experiments_raw, list)
            else []
        )
        if isinstance(experiment, dict)
        and _is_nonempty_string(experiment.get("experiment_id"))
    }
    symbol_receipts_raw = receipt.get("inspected_symbols")
    symbol_paths = {
        str(symbol.get("symbol")): str(symbol.get("path"))
        for symbol in (
            symbol_receipts_raw if isinstance(symbol_receipts_raw, list) else []
        )
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
            if (
                control.get("scenario_kind") != "control"
                or control.get("outcome") != "refutes"
            ):
                continue
            relationship_raw = control.get("control_relationship")
            relationship = relationship_raw if isinstance(relationship_raw, dict) else {}
            support_id = str(relationship.get("supports_experiment_id") or "")
            support_selection_id = f"{hypothesis_id}:{support_id}"
            control_selection_id = f"{hypothesis_id}:{control_id}"
            expected_selections[support_selection_id] = (
                hypothesis_id,
                support_id,
                mechanism_symbols,
            )
            expected_selections[control_selection_id] = (
                hypothesis_id,
                control_id,
                mechanism_symbols,
            )
            expected_controls[(hypothesis_id, control_id)] = {
                "support_id": support_id,
                "support_selection_id": support_selection_id,
                "control_selection_id": control_selection_id,
                "mechanism_symbols": mechanism_symbols,
                "relationship_sha256": _canonical_sha256(
                    {
                        "controlled_variable": relationship.get("controlled_variable"),
                        "expected_difference": relationship.get("expected_difference"),
                        "mechanism_symbols": relationship.get("mechanism_symbols"),
                    }
                ),
            }

    selections_raw = receipt.get("test_selections")
    selections = selections_raw if isinstance(selections_raw, list) else []
    selections_by_id: dict[str, dict[str, Any]] = {}
    for index, selection in enumerate(selections):
        if not isinstance(selection, dict):
            errors.append(
                f"research_evidence_verification_test_selection_invalid: {pid}: {index}"
            )
            continue
        selection_id = str(selection.get("selection_id") or "")
        expected = expected_selections.get(selection_id)
        if not selection_id or selection_id in selections_by_id or expected is None:
            errors.append(
                f"research_evidence_verification_test_selection_invalid: {pid}: {index}"
            )
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
            errors.append(
                f"research_evidence_verification_test_selection_invalid: {pid}: {index}"
            )
    if set(selections_by_id) != set(expected_selections):
        errors.append(
            f"research_evidence_verification_test_selection_coverage_mismatch: {pid}"
        )

    controls_raw = receipt.get("control_verifications")
    controls = controls_raw if isinstance(controls_raw, list) else []
    observed_controls: set[tuple[str, str]] = set()
    verified_controls_by_id: dict[str, dict[str, Any]] = {}
    for index, control in enumerate(controls):
        if not isinstance(control, dict):
            errors.append(
                f"research_evidence_verification_control_invalid: {pid}: {index}"
            )
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
        observable_support = (
            observable_support if isinstance(observable_support, dict) else {}
        )
        observable_control = observable.get("control")
        observable_control = (
            observable_control if isinstance(observable_control, dict) else {}
        )
        source = support_assertion.get("source")
        common_observable_valid = (
            observable.get("verification_method") == "runner_replay_complement_v1"
            and source in {"exit_code", "stdout", "stderr", "combined"}
            and control_assertion.get("source") == source
            and support_replay.get("assertion_passed") is True
            and control_replay.get("assertion_passed") is True
            and observable_support.get("exit_code") == support_replay.get("exit_code")
            and observable_control.get("exit_code") == control_replay.get("exit_code")
            and observable_support.get("stdout_sha256")
            == support_replay.get("stdout_sha256")
            and observable_support.get("stderr_sha256")
            == support_replay.get("stderr_sha256")
            and observable_control.get("stdout_sha256")
            == control_replay.get("stdout_sha256")
            and observable_control.get("stderr_sha256")
            == control_replay.get("stderr_sha256")
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
                    support_replay.get("stdout_sha256")
                    != control_replay.get("stdout_sha256")
                    or support_replay.get("stderr_sha256")
                    != control_replay.get("stderr_sha256")
                )
                and observable.get("support_expected_sha256")
                == _canonical_sha256(support_expected)
                and observable.get("control_expected_sha256")
                == _canonical_sha256(control_expected)
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
            {
                field: value
                for field, value in control.items()
                if field != "control_verification_id"
            }
        )
        valid = (
            expected is not None
            and key not in observed_controls
            and control.get("verification_method")
            == "pytest_ast_controlled_difference_v2"
            and control.get("support_experiment_id") == expected["support_id"]
            and control.get("support_selection_id")
            == expected["support_selection_id"]
            and control.get("control_selection_id")
            == expected["control_selection_id"]
            and control.get("mechanism_symbols") == expected["mechanism_symbols"]
            and control.get("shared_verified_mechanism_symbols")
            == expected["mechanism_symbols"]
            and isinstance(control.get("same_test_file"), bool)
            and control.get("same_test_file")
            is (
                support_selection.get("test_path")
                == control_selection.get("test_path")
            )
            and control.get("relationship_sha256")
            == expected["relationship_sha256"]
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
            errors.append(
                f"research_evidence_verification_control_invalid: {pid}: {index}"
            )
    if observed_controls != set(expected_controls):
        errors.append(
            f"research_evidence_verification_control_coverage_mismatch: {pid}"
        )
    failure_paths_raw = receipt.get("failure_paths")
    failure_paths = failure_paths_raw if isinstance(failure_paths_raw, list) else []
    observed_failure_controls: set[str] = set()
    observed_failure_ids: set[str] = set()
    for index, path in enumerate(failure_paths):
        if not isinstance(path, dict):
            errors.append(
                f"research_evidence_verification_failure_path_invalid: {pid}: {index}"
            )
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
            f"{support_selection.get('test_path', '')}::"
            f"{support_selection.get('selector', '')}"
        )
        consumer_identity = {
            "kind": "evidence_selector",
            "entrypoint": path_name,
        }
        independence_key = _canonical_sha256(consumer_identity)
        observable = control.get("observable_difference") if control else None
        observable = observable if isinstance(observable, dict) else {}
        support_observation = observable.get("support")
        support_observation = (
            support_observation if isinstance(support_observation, dict) else {}
        )
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
            errors.append(
                f"research_evidence_verification_failure_path_invalid: {pid}: {index}"
            )
    if observed_failure_controls != set(verified_controls_by_id):
        errors.append(
            f"research_evidence_verification_failure_path_coverage_mismatch: {pid}"
        )
    return errors


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
        if isinstance(hypothesis, dict)
        and _is_nonempty_string(hypothesis.get("hypothesis_id"))
    }
    primary_id = next(iter(hypotheses), None)
    experiments_raw = receipt.get("experiments")
    experiments = {
        str(experiment.get("experiment_id")): experiment
        for experiment in (experiments_raw if isinstance(experiments_raw, list) else [])
        if isinstance(experiment, dict)
        and _is_nonempty_string(experiment.get("experiment_id"))
    }
    symbol_receipts_raw = receipt.get("inspected_symbols")
    symbol_paths = {
        str(symbol.get("symbol")): str(symbol.get("path"))
        for symbol in (
            symbol_receipts_raw if isinstance(symbol_receipts_raw, list) else []
        )
        if isinstance(symbol, dict)
        and _is_nonempty_string(symbol.get("symbol"))
        and _is_nonempty_string(symbol.get("path"))
    }
    observed_ids: set[str] = set()
    primary_evidence = False

    def valid_link(value: Any, *, mechanism_symbols: list[Any]) -> bool:
        if not isinstance(value, dict) or not _is_nonempty_string(
            value.get("entrypoint")
        ):
            return False
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
                    {
                        step.get("symbol")
                        for step in code_path
                        if isinstance(step, dict)
                    }
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
                if isinstance(step, dict)
                and _valid_sha256(step.get("trace_excerpt_sha256"))
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
        experiment_ids = raw.get("experiment_ids")
        code_paths = raw.get("code_paths")
        consumer_identity = raw.get("consumer_identity")
        valid = (
            evidence_id == expected_id
            and evidence_id not in observed_ids
            and raw.get("evidence_type") in _VALID_MECHANISM_EVIDENCE_TYPES
            and hypothesis is not None
            and mechanism_symbols == expected_symbols
            and isinstance(mechanism_symbols, list)
            and bool(mechanism_symbols)
            and isinstance(experiment_ids, list)
            and bool(experiment_ids)
            and all(experiment_id in experiments for experiment_id in experiment_ids)
            and isinstance(code_paths, list)
            and bool(code_paths)
            and {
                (path.get("symbol"), path.get("path"))
                for path in code_paths
                if isinstance(path, dict)
            }
            == {(symbol, symbol_paths.get(symbol)) for symbol in mechanism_symbols}
            and isinstance(raw.get("origin_atom_ids"), list)
            and bool(raw.get("origin_atom_ids"))
            and _is_nonempty_string(raw.get("path_name"))
            and isinstance(consumer_identity, dict)
            and _is_nonempty_string(consumer_identity.get("kind"))
            and _is_nonempty_string(consumer_identity.get("entrypoint"))
            and raw.get("path_name") == consumer_identity.get("entrypoint")
            and raw.get("independence_key") == _canonical_sha256(consumer_identity)
            and raw.get("adversarial_effect")
            in {"supports_selection", "limits_scope"}
        )
        if raw.get("evidence_type") == "controlled_scenario":
            valid = valid and len(experiment_ids if isinstance(experiment_ids, list) else []) == 2
            valid = valid and isinstance(raw.get("controlled_condition"), dict)
            valid = valid and isinstance(raw.get("observable_difference"), dict)
            valid = valid and (
                valid_link(raw.get("mechanism_link"), mechanism_symbols=mechanism_symbol_list)
                or _is_nonempty_string(raw.get("strong_pytest_control_id"))
            )
        elif raw.get("evidence_type") == "observed_output":
            valid = valid and valid_link(
                raw.get("mechanism_link"), mechanism_symbols=mechanism_symbol_list
            )
        elif raw.get("evidence_type") == "temporary_harness":
            valid = valid and isinstance(raw.get("harness_path"), str)
            valid = valid and str(raw.get("harness_path")).startswith(
                ".usertest_research/"
            )
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
            valid = valid and requirement in {"windows", "linux", "darwin"}
            valid = valid and requirement == raw.get("observed_platform")
            valid = valid and valid_link(
                raw.get("mechanism_link"), mechanism_symbols=mechanism_symbol_list
            )
        if valid:
            observed_ids.add(evidence_id)
            if hypothesis_id == primary_id:
                primary_evidence = True
        else:
            errors.append(f"research_mechanism_evidence_invalid: {pid}: {index}")
    if primary_id is not None and not primary_evidence:
        errors.append(f"research_primary_mechanism_evidence_missing: {pid}: {primary_id}")
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
        if isinstance(value, dict)
        and _is_nonempty_string(value.get("mechanism_evidence_id"))
    }
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
        if isinstance(value, dict)
        and _is_nonempty_string(value.get("intervention_receipt_id"))
    }
    assignment = item.get("evidence_assignment")
    assignment = assignment if isinstance(assignment, dict) else {}
    atoms_by_id = {
        str(value.get("atom_id")): value
        for value in assignment.get("atom_receipts", [])
        if isinstance(value, dict) and _is_nonempty_string(value.get("atom_id"))
    }
    observed: set[str] = set()
    for contract_index, contract in enumerate(raw):
        label = f"{pid}: {oracle_index}:{contract_index}"
        if not isinstance(contract, dict):
            errors.append(f"research_positive_outcome_contract_invalid: {label}")
            continue
        contract_id = str(contract.get("positive_outcome_contract_id") or "")
        expected_id = "positive_outcome_contract:" + _canonical_sha256(
            {
                key: value
                for key, value in contract.items()
                if key != "positive_outcome_contract_id"
            }
        )
        evidence_ids = contract.get("mechanism_evidence_ids")
        postconditions = contract.get("postconditions")
        valid = (
            contract.get("schema_version") == 1
            and contract_id == expected_id
            and contract_id not in observed
            and contract.get("research_experiment_id")
            == oracle.get("research_experiment_id")
            and isinstance(evidence_ids, list)
            and bool(evidence_ids)
            and all(evidence_id in evidence_by_id for evidence_id in evidence_ids)
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
        if kind == "repository_test_assertion":
            repository = contract.get("repository_contract")
            source_bindings = contract.get("source_case_bindings")
            baseline_failure = contract.get("baseline_failure")
            assertions = (
                repository.get("semantic_assertions")
                if isinstance(repository, dict)
                else None
            )
            touches = (
                repository.get("mechanism_touches")
                if isinstance(repository, dict)
                else None
            )
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
                and baseline_failure.get("exit_code")
                == oracle.get("baseline", {}).get("exit_code")
                and _valid_sha256(baseline_failure.get("stdout_sha256"))
                and _valid_sha256(baseline_failure.get("stderr_sha256"))
                and baseline_failure.get("failure_kind")
                == "bound_semantic_assertion_failed"
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
                semantic_basis.get("provenance")
                if isinstance(semantic_basis, dict)
                else None
            )
            expected_value = (
                semantic_basis.get("expected_value")
                if isinstance(semantic_basis, dict)
                else object()
            )
            provenance_valid = False
            if isinstance(provenance, dict) and provenance.get("kind") == (
                "source_atom_quote"
            ):
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
                    and provenance.get("field_value_sha256")
                    == _canonical_sha256(field_value)
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
                elif isinstance(locator, dict) and locator.get("kind") == (
                    "schema_pointer"
                ):
                    locator_valid = (
                        _is_nonempty_string(locator.get("json_pointer"))
                        and str(locator.get("json_pointer")).startswith("/")
                        and _valid_sha256(locator.get("value_sha256"))
                    )
                elif isinstance(locator, dict) and locator.get("kind") == (
                    "mechanism_subject"
                ):
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
                    and provenance.get("read_event_sha256")
                    == inspected.get("read_event_sha256")
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
                    interventions_by_id.get(
                        str(adversarial.get("intervention_receipt_id") or "")
                    ),
                    dict,
                )
                and interventions_by_id[
                    str(adversarial.get("intervention_receipt_id"))
                ].get("attempt_id")
                == adversarial.get("attempt_id")
            )
            valid = valid and (
                isinstance(research_contract, dict)
                and _is_nonempty_string(harness_path)
                and str(harness_path).startswith(".usertest_research/")
                and _valid_sha256(research_contract.get("harness_sha256"))
                and isinstance(manifest_entry, dict)
                and manifest_entry.get("sha256")
                == research_contract.get("harness_sha256")
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
                    and assertion.get("expected_value_sha256")
                    == _canonical_sha256(expected_value)
                    for assertion in semantic_assertions
                )
                and isinstance(baseline_failure, dict)
                and isinstance(baseline_failure.get("exit_code"), int)
                and not isinstance(baseline_failure.get("exit_code"), bool)
                and baseline_failure.get("exit_code") != 0
                and _valid_sha256(baseline_failure.get("stderr_sha256"))
                and baseline_failure.get("failure_kind")
                == "semantic_assertion_failed"
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
                and semantic_basis.get("expected_value_sha256")
                == _canonical_sha256(expected_value)
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
            expected_binding = next(
                (
                    binding
                    for binding in receipt.get("atom_bindings", [])
                    if isinstance(binding, dict)
                    and binding.get("experiment_id")
                    == oracle.get("research_experiment_id")
                    and binding.get("atom_id") == origin.get("atom_id")
                    and binding.get("binding_role") == "expected_behavior"
                    and binding.get("origin_atom_field_path")
                    == origin.get("field_path")
                ),
                None,
            ) if isinstance(origin, dict) else None
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
        if isinstance(value, dict)
        and _is_nonempty_string(value.get("mechanism_evidence_id"))
    }
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
            and isinstance(experiment, dict)
            and oracle.get("scenario_kind") == experiment.get("scenario_kind")
            and isinstance(evidence_ids, list)
            and bool(evidence_ids)
            and all(
                evidence_id in mechanism
                and experiment_id in mechanism[evidence_id].get("experiment_ids", [])
                for evidence_id in evidence_ids
            )
            and isinstance(oracle.get("origin_atom_ids"), list)
            and bool(oracle.get("origin_atom_ids"))
            and isinstance(baseline, dict)
            and baseline.get("exit_code") == experiment.get("exit_code")
            and baseline.get("observable_assertion") == experiment.get(
                "observable_assertion"
            )
            and _valid_sha256(baseline.get("stdout_sha256"))
            and _valid_sha256(baseline.get("stderr_sha256"))
        )
        if oracle.get("kind") == "staged_replay":
            execution = oracle.get("execution")
            asset = oracle.get("asset")
            argv = execution.get("argv") if isinstance(execution, dict) else None
            authorization = (
                execution.get("command_authorization")
                if isinstance(execution, dict)
                else None
            )
            valid = valid and (
                oracle.get("proof_scope") == "behavioral"
                and experiment.get("scenario_kind")
                in {"original_replay", "faithful_replay", "live_runtime"}
                and isinstance(execution, dict)
                and isinstance(argv, list)
                and bool(argv)
                and all(
                    _is_nonempty_string(token) for token in (argv if isinstance(argv, list) else [])
                )
                and isinstance(authorization, dict)
                and authorization.get("authorization_kind")
                in {
                    "standard_test_or_research_harness",
                    "immutable_source_command",
                    "declared_inspected_repository_entrypoint",
                }
                and authorization.get("executed_argv_sha256")
                == _canonical_sha256(argv)
                and authorization.get("shell") is False
                and authorization.get("workspace_confined") is True
                and execution.get("shell") is False
                and (asset is None or isinstance(asset, dict))
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
                    and isinstance(
                        mechanism[evidence_id].get("mechanism_symbols"), list
                    )
                    and bool(mechanism[evidence_id].get("mechanism_symbols"))
                    and all(
                        isinstance(symbol, str) and symbol.startswith("config:/")
                        for symbol in mechanism[evidence_id].get(
                            "mechanism_symbols", []
                        )
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
            f"research_dossier_invalid_evidence_verification_type: {pid}: "
            f"{type(receipt).__name__}"
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
        if not isinstance(keys, list) or any(
            not _is_nonempty_string(key) for key in keys
        ):
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
                errors.append(
                    f"research_evidence_verification_verified_missing_{field}: {pid}"
                )
        if receipt.get("planning_workspace_clean") is not True:
            errors.append(
                f"research_evidence_verification_planning_workspace_not_clean: {pid}"
            )
        if receipt.get("planning_workspace_head") != item.get("repo_revision"):
            errors.append(
                f"research_evidence_verification_planning_revision_mismatch: {pid}"
            )
        if receipt.get("workspace_head") != item.get("repo_revision"):
            errors.append(f"research_evidence_verification_research_revision_mismatch: {pid}")
        if not _valid_sha256(receipt.get("normalized_events_sha256")):
            errors.append(
                f"research_evidence_verification_normalized_events_hash_invalid: {pid}"
            )
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
            if isinstance(atom_receipt, dict)
            and _is_nonempty_string(atom_receipt.get("atom_id"))
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
                    f"research_evidence_verification_atom_field_binding_missing: "
                    f"{pid}: {index}"
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
                    or binding.get("origin_atom_sha256")
                    != atom_receipt.get("atom_sha256")
                    or not _valid_sha256(binding.get("origin_atom_value_sha256"))
                    or not found
                    or binding.get("origin_atom_value_sha256")
                    != _canonical_sha256(value)
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
        if isinstance(origin_ids, list) and bound_atom_ids != set(origin_ids):
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
        receipt_artifacts = (
            receipt_artifacts_raw if isinstance(receipt_artifacts_raw, list) else []
        )
        for index, artifact in enumerate(receipt_artifacts):
            if not isinstance(artifact, dict):
                errors.append(
                    f"research_evidence_verification_artifact_invalid: {pid}: {index}"
                )
                continue
            artifact_id = artifact.get("artifact_id")
            if not _is_nonempty_string(artifact_id):
                errors.append(
                    f"research_evidence_verification_artifact_id_invalid: {pid}: {index}"
                )
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
                    if isinstance(candidate, dict)
                    and candidate.get("artifact_id") == artifact_id
                ),
                None,
            )
            if isinstance(declared_artifact, dict) and (
                artifact.get("kind") != declared_artifact.get("kind")
                or artifact.get("declared_path", artifact.get("path"))
                != declared_artifact.get("path")
            ):
                errors.append(
                    f"research_evidence_verification_artifact_receipt_mismatch: "
                    f"{pid}: {index}"
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
            if isinstance(experiment, dict)
            and _is_nonempty_string(experiment.get("experiment_id"))
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
            valid_receipt = (
                _is_nonempty_string(experiment.get("experiment_id"))
                and isinstance(executed_argv, list)
                and bool(executed_argv)
                and all(_is_nonempty_string(arg) for arg in executed_argv)
                and isinstance(agent_event_index, int)
                and not isinstance(agent_event_index, bool)
                and agent_event_index >= 0
                and _valid_sha256(experiment.get("agent_event_sha256"))
                and (
                    output_excerpt_hash is None or _valid_sha256(output_excerpt_hash)
                )
                and _is_nonempty_string(experiment.get("workspace_dir"))
                and experiment.get("workspace_head") == item.get("repo_revision")
                and _valid_sha256(experiment.get("baseline_state_sha256"))
                and _valid_sha256(experiment.get("pre_replay_state_sha256"))
                and experiment.get("pre_replay_state_sha256")
                == experiment.get("post_replay_state_sha256")
                and experiment.get("post_replay_mutations") is False
                and _valid_sha256(experiment.get("overlay_manifest_sha256"))
                and experiment_isolation_allowed(
                    experiment.get("execution_isolation")
                )
                and isinstance(experiment.get("execution_metadata"), dict)
                and _is_nonempty_string(experiment.get("stdout_path"))
                and _is_nonempty_string(experiment.get("stderr_path"))
                and _valid_sha256(experiment.get("stdout_sha256"))
                and _valid_sha256(experiment.get("stderr_sha256"))
                and isinstance(experiment.get("artifact_refs"), list)
                and all(
                    _is_nonempty_string(ref) for ref in experiment.get("artifact_refs", [])
                )
                and experiment.get("assertion_passed") is True
            )
            if valid_receipt:
                receipt_experiment_ids.add(str(experiment["experiment_id"]))
            else:
                errors.append(
                    f"research_evidence_verification_experiment_invalid: {pid}: {index}"
                )
        if receipt_experiment_ids != declared_experiment_ids:
            errors.append(f"research_evidence_verification_experiment_coverage_mismatch: {pid}")

        receipt_experiments_by_id = {
            str(experiment.get("experiment_id")): experiment
            for experiment in receipt_experiments
            if isinstance(experiment, dict)
            and _is_nonempty_string(experiment.get("experiment_id"))
        }

        declared_experiments = {
            str(experiment.get("experiment_id")): experiment
            for experiment in declared_experiment_list
            if isinstance(experiment, dict)
            and _is_nonempty_string(experiment.get("experiment_id"))
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
                or experiment.get("addresses_atom_ids")
                != declared.get("addresses_atom_ids")
                or experiment.get("observable_assertion")
                != declared.get("observable_assertion")
                or experiment.get("artifact_refs") != declared.get("artifact_refs")
            ):
                errors.append(
                    f"research_evidence_verification_experiment_receipt_mismatch: "
                    f"{pid}: {index}"
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
            or file_receipt.get("observed_end_line")
            < file_receipt.get("observed_start_line")
            or isinstance(file_receipt.get("read_event_index"), bool)
            or not isinstance(file_receipt.get("read_event_index"), int)
            or file_receipt.get("read_event_index") < 0
            for file_receipt in receipt_files
        ):
            errors.append(f"research_evidence_verification_file_coverage_mismatch: {pid}")
        receipt_file_paths = {
            file_receipt.get("path")
            for file_receipt in receipt_files
            if isinstance(file_receipt, dict)
        }
        if receipt_file_paths != set(declared_files):
            errors.append(f"research_evidence_verification_file_path_mismatch: {pid}")
        declared_symbols_raw = item.get("inspected_symbols")
        declared_symbols = {
            symbol
            for symbol in (
                declared_symbols_raw if isinstance(declared_symbols_raw, list) else []
            )
            if isinstance(symbol, str)
        }
        receipt_symbols_raw = receipt.get("inspected_symbols")
        receipt_symbols_list = (
            receipt_symbols_raw if isinstance(receipt_symbols_raw, list) else []
        )
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
                declared_hypotheses_raw
                if isinstance(declared_hypotheses_raw, list)
                else []
            )
            if isinstance(hypothesis, dict)
            and _is_nonempty_string(hypothesis.get("hypothesis_id"))
        }
        receipt_hypotheses_raw = receipt.get("hypothesis_refs")
        receipt_hypotheses_list = (
            receipt_hypotheses_raw if isinstance(receipt_hypotheses_raw, list) else []
        )
        receipt_hypotheses = {
            hypothesis.get("hypothesis_id")
            for hypothesis in receipt_hypotheses_list
            if isinstance(hypothesis, dict)
            and _is_nonempty_string(hypothesis.get("hypothesis_id"))
        }
        if receipt_hypotheses != declared_hypotheses:
            errors.append(
                f"research_evidence_verification_hypothesis_coverage_mismatch: {pid}"
            )
        declared_hypothesis_records = {
            str(hypothesis.get("hypothesis_id")): hypothesis
            for hypothesis in (
                declared_hypotheses_raw
                if isinstance(declared_hypotheses_raw, list)
                else []
            )
            if isinstance(hypothesis, dict)
            and _is_nonempty_string(hypothesis.get("hypothesis_id"))
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
                or hypothesis_receipt.get("disposition")
                != declared_hypothesis.get("disposition")
                or hypothesis_receipt.get("disposition_evidence_refs")
                != declared_hypothesis.get("disposition_evidence")
            ):
                errors.append(
                    f"research_evidence_verification_hypothesis_receipt_mismatch: "
                    f"{pid}: {index}"
                )
                continue
            control_links_raw = hypothesis_receipt.get("control_links")
            control_links = (
                control_links_raw if isinstance(control_links_raw, list) else []
            )
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
                relationship = (
                    relationship_raw if isinstance(relationship_raw, dict) else {}
                )
                support_id = str(control_link.get("supports_experiment_id") or "")
                support = declared_experiments.get(support_id, {})
                valid_control_link = (
                    control_id in expected_control_ids
                    and support_id in declared_hypothesis.get("supporting_evidence", [])
                    and control_link.get("mechanism_symbols") == mechanism_symbols
                    and control_link.get("shared_atom_ids")
                    == sorted(
                        set(control.get("addresses_atom_ids", []))
                        & set(support.get("addresses_atom_ids", []))
                    )
                    and control_link.get("shared_artifact_refs")
                    == sorted(
                        set(control.get("artifact_refs", []))
                        & set(support.get("artifact_refs", []))
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
            supporting_ids = (
                supporting_refs if isinstance(supporting_refs, list) else []
            )
            supporting_artifact_paths = {
                artifact_paths[artifact_id]
                for experiment_id in supporting_ids
                if isinstance(experiment_id, str)
                and declared_experiments.get(experiment_id, {}).get("outcome") == "supports"
                and declared_experiments.get(experiment_id, {}).get("scenario_kind")
                in {"original_replay", "faithful_replay", "static_trace", "live_runtime"}
                for artifact_id in declared_experiments.get(experiment_id, {}).get(
                    "artifact_refs", []
                )
                if artifact_id in artifact_paths
            }
            if supporting_artifact_paths and any(
                receipt_symbol_paths.get(symbol) not in supporting_artifact_paths
                for symbol in (
                    mechanism_symbols if isinstance(mechanism_symbols, list) else []
                )
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
                errors.append(
                    f"research_evidence_verification_causal_link_invalid: {pid}: {index}"
                )
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
                and experiment.get("scenario_kind")
                in {"original_replay", "faithful_replay", "live_runtime"}
                and link.get("path") == receipt_symbol_paths.get(symbol)
                and link.get("stream") in {"stdout", "stderr"}
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
                errors.append(
                    f"research_evidence_verification_causal_link_invalid: {pid}: {index}"
                )
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
        return [
            f"research_dossier_invalid_material_unknowns_type: {pid}: "
            f"{type(value).__name__}"
        ]
    errors: list[str] = []
    for idx, unknown in enumerate(value):
        if not isinstance(unknown, dict):
            errors.append(
                f"research_dossier_invalid_material_unknown: {pid}: index={idx} "
                f"type={type(unknown).__name__}"
            )
            continue
        if not _is_nonempty_string(unknown.get("unknown")):
            errors.append(
                f"research_dossier_invalid_material_unknown_text: {pid}: index={idx}"
            )
        if not _is_nonempty_string(unknown.get("evidence_needed")):
            errors.append(
                f"research_dossier_invalid_material_unknown_evidence_needed: {pid}: index={idx}"
            )
        hypothesis_id = unknown.get("hypothesis_id")
        if hypothesis_id is not None and not _is_nonempty_string(hypothesis_id):
            errors.append(
                f"research_dossier_invalid_material_unknown_hypothesis_id: {pid}: "
                f"index={idx}"
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
        if isinstance(affects, list):
            invalid = sorted(
                {
                    str(effect)
                    for effect in affects
                    if not isinstance(effect, str) or effect not in _VALID_UNKNOWN_EFFECTS
                }
            )
            if invalid:
                errors.append(
                    f"research_dossier_invalid_material_unknown_effect: {pid}: "
                    f"index={idx} values={invalid}"
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
    if repro is not None and repro not in _VALID_REPRODUCTION_STATUSES:
        warnings.append(f"legacy_research_dossier_invalid_reproduction_status: {pid}: {repro!r}")
    return warnings


def _post_research_bundle_members(item: Mapping[str, Any]) -> list[dict[str, Any]]:
    bundle = item.get("post_research_same_mechanism_bundle")
    members = bundle.get("member_research_dossiers") if isinstance(bundle, Mapping) else None
    return [dict(value) for value in members if isinstance(value, Mapping)] if isinstance(
        members, list
    ) else []


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
    if case_ids != raw.get("member_case_ids") or problem_ids != raw.get(
        "member_problem_ids"
    ):
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
        if (
            not isinstance(receipt, dict)
            or receipt.get("verified_mechanism_sha256")
            != raw.get("verified_mechanism_sha256")
        ):
            errors.append(f"research_post_relation_bundle_mechanism_mismatch: {pid}: {index}")
        member_errors = _validate_research_dossier(member)
        errors.extend(
            f"research_post_relation_bundle_member_invalid: {pid}: {index}: {error}"
            for error in member_errors
        )
    return errors


def _validate_research_dossier(item: dict[str, Any]) -> list[str]:
    """Return hard validation errors for a current research proof.

    A valid proof can still be insufficient or blocked. Those are first-class
    research outcomes and are assessed for ticket readiness separately by
    :func:`assess_research_readiness`.
    """
    errors: list[str] = []
    pid = str(item.get("problem_id") or "(no problem_id)")

    unknown_fields = sorted(set(item) - _RESEARCH_DOSSIER_ALLOWED)
    if unknown_fields:
        errors.append(
            f"research_dossier_unknown_fields: {pid}: {unknown_fields!r}"
        )

    for field in _RESEARCH_DOSSIER_REQUIRED:
        if field not in item:
            errors.append(f"research_dossier_missing_required_field: {pid}: {field}")

    version = item.get("research_schema_version")
    if version != RESEARCH_PROOF_SCHEMA_VERSION:
        errors.append(
            f"research_dossier_invalid_schema_version: {pid}: {version!r} "
            f"(expected {RESEARCH_PROOF_SCHEMA_VERSION})"
        )
    for field in ("case_id", "problem_id", "repo_revision"):
        if not _is_nonempty_string(item.get(field)):
            errors.append(f"research_dossier_invalid_{field}: {pid}")

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
    if method not in _VALID_RESEARCH_METHODS:
        errors.append(f"research_dossier_invalid_research_method: {pid}: {method!r}")
    repro = item.get("reproduction_status")
    if repro not in _VALID_REPRODUCTION_STATUSES:
        errors.append(f"research_dossier_invalid_reproduction_status: {pid}: {repro!r}")
    status = item.get("research_status")
    if status not in _VALID_RESEARCH_STATUSES:
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
    if broader not in _VALID_BROADER_CLASS:
        errors.append(
            f"research_dossier_invalid_broader_class_assessment: {pid}: {broader!r}"
        )
    diff_cls = item.get("diff_classification")
    if diff_cls not in _VALID_DIFF_CLASSIFICATIONS:
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
        _validate_string_list(
            item.get("evidence_boundaries"), field="evidence_boundaries", pid=pid
        )
    )
    errors.extend(_validate_evidence_assignment(item, pid=pid))
    errors.extend(_validate_evidence_verification(item, pid=pid))
    errors.extend(_validate_post_research_same_mechanism_bundle(item, pid=pid))

    blocking_reasons = item.get("blocking_reasons")
    if status == "blocked" and isinstance(blocking_reasons, list) and not blocking_reasons:
        errors.append(f"research_dossier_blocked_without_reason: {pid}")
    if status == "evidence_sufficient":
        if method == "reproduction" and repro != "reproduced":
            errors.append(
                f"research_dossier_sufficient_without_reproduction: {pid}: {repro!r}"
            )
        if repro in {"partial", "blocked"}:
            errors.append(
                f"research_dossier_sufficient_with_incomplete_reproduction: {pid}: {repro!r}"
            )

    return errors


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
    if repro in {"partial", "blocked"}:
        reasons.append(f"reproduction_{repro}")

    confidence = float(item.get("root_cause_confidence", 0.0))
    if confidence < MIN_RESEARCH_CONFIDENCE_FOR_READY:
        reasons.append("root_cause_confidence_below_threshold")

    repo_revision = str(item.get("repo_revision") or "").strip().lower()
    if not repo_revision or repo_revision.startswith("unavailable"):
        reasons.append("repo_revision_unavailable")

    verification_raw = item.get("evidence_verification")
    verification = verification_raw if isinstance(verification_raw, dict) else {}
    outcome_oracles = verification.get("outcome_oracles")
    if not isinstance(outcome_oracles, list) or not outcome_oracles:
        reasons.append("research_post_change_outcome_oracle_missing")
    elif not any(
        isinstance(oracle, dict)
        and isinstance(oracle.get("positive_outcome_contracts"), list)
        and bool(oracle.get("positive_outcome_contracts"))
        for oracle in outcome_oracles
    ):
        reasons.append("research_positive_outcome_contract_missing")

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
                reasons.append(
                    "primary_hypothesis_falsification_or_deterministic_closure_missing"
                )
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
                if isinstance(unknown, dict)
                and _is_nonempty_string(unknown.get("hypothesis_id"))
            }
            if not unresolved_alternative_ids.issubset(unknown_hypothesis_ids):
                reasons.append("unresolved_alternative_hypothesis_not_materialized")

    inspected_files = item.get("inspected_files")
    inspected_symbols = item.get("inspected_symbols")
    if (
        not isinstance(inspected_files, list)
        or not inspected_files
        or not isinstance(inspected_symbols, list)
        or not inspected_symbols
    ):
        reasons.append("exact_code_path_inspection_missing")

    material_unknowns = item.get("material_unknowns")
    if isinstance(material_unknowns, list):
        for unknown in material_unknowns:
            if not isinstance(unknown, dict):
                continue
            affects = unknown.get("affects")
            if isinstance(affects, list) and any(
                effect in _READINESS_BLOCKING_UNKNOWN_EFFECTS for effect in affects
            ):
                reasons.append("material_unknown_blocks_implementation_decision")
                break

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
    if known_family_ids and fid is not None and fid not in known_family_ids:
        warnings.append(
            f"solution_option_unknown_family_id: {oid}: {fid!r} "
            f"(known: {sorted(known_family_ids)})"
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
            f"change_plan_invalid_success_criteria_type: {cid}: "
            f"{type(criteria).__name__}"
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
                    warnings.append(
                        f"change_plan_target_contract_{field}_mismatch: {cid}"
                    )
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
                    "symbols": target.get("symbols"),
                    "change": target.get("change"),
                }
                for target in contract_targets
                if isinstance(target, dict)
            ]
            plan_projection = [
                {
                    "action": target.get("action"),
                    "path": target.get("path"),
                    "symbols": target.get("symbols"),
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
