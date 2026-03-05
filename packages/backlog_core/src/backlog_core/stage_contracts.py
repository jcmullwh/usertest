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
- ``parse_research_dossier_list`` rejects any item with
  ``implementation_performed=true``.
- ``parse_solution_option_sets`` rejects any item with ``selected_solution``.
- ``build_stage_document`` is the single place that knows how to wrap stage items into
  the standard artifact envelope.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
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

_RESEARCH_DOSSIER_REQUIRED: tuple[str, ...] = (
    "problem_id",
    "reproduction_status",
    "writes_used",
    "writes_purpose",
    "implementation_performed",
    "root_cause_hypotheses",
    "broader_class_assessment",
    "unknowns",
)
_VALID_REPRODUCTION_STATUSES: frozenset[str] = frozenset(
    {"reproduced", "reproduction_failed", "partial"}
)
_VALID_BROADER_CLASS: frozenset[str] = frozenset(
    {"isolated_instance", "repeated_variant", "unknown"}
)
_VALID_DIFF_CLASSIFICATIONS: frozenset[str] = frozenset(
    {"allowed_research_edits", "suspicious_implementation", "no_changes"}
)

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
    if conf is not None and not isinstance(conf, (int, float)):
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


def _validate_research_dossier(item: dict[str, Any]) -> list[str]:
    """Return warning strings for a single research dossier.

    A dossier with ``implementation_performed=true`` is an error, not a warning.
    This function raises ``ValueError`` in that case so the caller can surface it
    prominently.

    Parameters
    ----------
    item:
        Candidate research dossier dict.

    Returns
    -------
    list[str]
        Validation warnings.

    Raises
    ------
    ValueError
        When ``implementation_performed`` is ``True``.
    """
    warnings: list[str] = []
    pid = item.get("problem_id") or "(no problem_id)"

    for field in _RESEARCH_DOSSIER_REQUIRED:
        if field not in item:
            warnings.append(f"research_dossier_missing_required_field: {pid}: {field}")

    impl_performed = item.get("implementation_performed")
    if impl_performed is True:
        raise ValueError(
            f"research_dossier_implementation_performed_true: {pid}: "
            "implementation_performed must be false; stage 3 goal is reproduction, "
            "not implementation"
        )

    repro = item.get("reproduction_status")
    if repro is not None and repro not in _VALID_REPRODUCTION_STATUSES:
        warnings.append(f"research_dossier_invalid_reproduction_status: {pid}: {repro!r}")

    broader = item.get("broader_class_assessment")
    if broader is not None and broader not in _VALID_BROADER_CLASS:
        warnings.append(
            f"research_dossier_invalid_broader_class_assessment: {pid}: {broader!r}"
        )

    diff_cls = item.get("diff_classification")
    if diff_cls is not None and diff_cls not in _VALID_DIFF_CLASSIFICATIONS:
        warnings.append(
            f"research_dossier_invalid_diff_classification: {pid}: {diff_cls!r}"
        )

    return warnings


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


def _validate_change_plan(item: dict[str, Any]) -> list[str]:
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


def parse_research_dossier_list(text: str) -> tuple[list[dict[str, Any]], list[str]]:
    """Parse and validate a stage-3 research-dossier list from raw LLM output.

    Raises ``ValueError`` for any dossier where ``implementation_performed=true``.

    Parameters
    ----------
    text:
        Raw text containing a JSON list of research dossiers.

    Returns
    -------
    tuple[list[dict[str, Any]], list[str]]
        ``(dossiers, warnings)``.

    Raises
    ------
    ValueError
        When no JSON can be extracted from *text*, or when any dossier sets
        ``implementation_performed=true``.
    """
    raw = _extract_json(text)
    items = _as_list(raw)
    all_warnings: list[str] = []
    result: list[dict[str, Any]] = []

    for idx, item in enumerate(items):
        if not isinstance(item, dict):
            w = f"research_dossier_not_a_dict: index={idx} type={type(item).__name__}"
            all_warnings.append(w)
            result.append({"_raw": item, "_parse_warning": w})
            continue

        # Raises ValueError if implementation_performed is true.
        item_warnings = _validate_research_dossier(item)
        if item_warnings:
            all_warnings.extend(item_warnings)
            item = dict(item)
            item["_parse_warning"] = "; ".join(item_warnings)
            _LOG.warning("parse_research_dossier_list: %s", "; ".join(item_warnings))

        if "research_status" not in item:
            item = dict(item)
            item["research_status"] = "researched"

        result.append(item)

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


def parse_change_plan_list(text: str) -> tuple[list[dict[str, Any]], list[str]]:
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

        item_warnings = _validate_change_plan(item)
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
